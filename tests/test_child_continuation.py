"""没做完就交回的子 agent（失败、到点收尾、ask、被软打断）：会话留到发起任务结束后再过宽限期；父节点拿句柄 continue_from 接着派，句柄要核对家族、节点和委派方式；结果里引用的超长输出复制到父节点名下。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.dispatch_handle import format_handle, parse_handle  # noqa: E402
from engine.tool_outputs import ToolOutputNotFound, ToolOutputStore, mentioned_refs  # noqa: E402
from supervisor import admin_api, child_retention, continuation  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_router import PostCompletionWork  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_group:1001"
OTHER_CONV = "qq_group:2002"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
_TOKEN = "continuation-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
REF_A = "to_" + "a" * 24
REF_B = "to_" + "b" * 24


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-child-continuation"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=key)


def _caller(state: SupervisorState, session_id: str, *, key: str = CONV) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": key, "channel": "qq", "platform_auth": dict(AUTH)}},
            continuation={},
            source_inbound_seq=None,
        )


def _inbound(state: SupervisorState, session_id: str, *, node: str = "test.orchestrator", **extra: Any) -> Task:
    payload = {
        "channel": "qq",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": f"run {node}",
        "entry_node_id": node,
        "platform_auth": dict(AUTH),
        **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _origin(session_id: str, caller: Task, mode: str = "fresh", **extra: Any) -> dict[str, Any]:
    return {
        "parent_session_id": session_id, "caller_node_id": "test.orchestrator", "parent_conversation_key": CONV,
        "context_mode": mode, "caller_task_id": caller.task_id, **extra,
    }


def _async_child(state: SupervisorState, session_id: str, caller: Task, *, mode: str = "fresh") -> tuple[str, Task]:
    """照 engine 异步委派那样投一条带 dispatch_origin 的入站，子节点在自己的 agent: 会话里跑。"""
    agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:a.node:{CONV}:{uuid.uuid4()}")
    child = _inbound(
        state, agent_sid, node="a.node", dispatch_context_mode=mode, dispatch_origin=_origin(session_id, caller, mode),
    )
    return agent_sid, child


def _item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _dispatch(state: SupervisorState, caller: Task, *items: Any) -> list[Task]:
    """caller 这一程交回 wait 批次；返回这一批新派出的子任务。"""
    before = {task.task_id for task in state.tasks.values() if task.caller_task_id == caller.task_id}
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", "dispatch_batch": list(items)})
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id and task.task_id not in before),
        key=lambda task: task.batch_index,
    )


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _fail(state: SupervisorState, task: Task, error: str = "network down") -> None:
    _report(state, task, {"action": "fail", "error": error})


def _finish(state: SupervisorState, task: Task, text: str = "done") -> None:
    _report(state, task, {"action": "finish", "result": {"text": text}})


def _wrapped(state: SupervisorState, task: Task, reason: str = "steps") -> None:
    flag = "time_limit_reached" if reason == "time" else "step_limit_reached"
    _report(state, task, {"action": "finish", "result": {"text": "做了一半", flag: True, "wrapup": {"reason": reason}}})


def _ask(state: SupervisorState, task: Task, question: str = "要查哪一年的？") -> None:
    _report(state, task, {"action": "ask", "result": {"text": question}})


def _preempt(state: SupervisorState, task: Task) -> None:
    _report(state, task, {"action": "preempted", "context_ref": "ctx-1"})


def _post_cleanup(state: SupervisorState, task: Task) -> None:
    """跑一遍完成后的会话拆除那一步，和后台 post-work 里的是同一个函数。"""
    with state._lock:
        work = PostCompletionWork(
            task=task.model_copy(deep=True),
            route_session_id=state._route_session_id_for_task_locked(task),
            session_generation=task.session_generation,
        )
    state._execute_post_cleanup(work, {"phase_id": f"test:{task.task_id}:cleanup", "kind": "cleanup"})


def _hold(state: SupervisorState, session_id: str) -> dict[str, Any] | None:
    return (state._session_store.get_entry(session_id) or {}).get("retain")


def _sweep(state: SupervisorState) -> None:
    with state._lock:
        state._cleanup_stale_sessions_locked()


def _kept(state: SupervisorState) -> tuple[str, Task, Task, str]:
    """一个调用方派出的 wait 子任务失败了：会话留着，调用方已恢复。"""
    sid = _session(state)
    caller = _caller(state, sid)
    [child] = _dispatch(state, caller, _item(call_id="w1"))
    _fail(state, child)
    return sid, caller, child, child.input["child_session_id"]


def _conversation(workspace: Path, session_id: str) -> Path:
    ConversationStore(workspace / "data" / "conversations").append_batch(session_id, [
        Message(id=f"{session_id}-1", role="user", content="帮忙查一下"),
    ])
    return workspace / "data" / "conversations" / f"{session_id}.jsonl"


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)


class TestKeeping:
    def test_a_failed_child_is_kept_and_its_result_says_how_to_continue(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "fail"
        assert entry["handle"] == f"{child.task_id}@{child_sid}"
        assert entry["continuable"] is True and "wrapup" not in entry
        hold = _hold(state, child_sid)
        assert {key: value for key, value in hold.items() if key != "since"} == {
            "task_id": child.task_id,
            "parent_task_id": caller.task_id,
            "node_id": "a.node",
            "kind": "child",
            "context_mode": "fresh",
            "reason": "failed",
            "until": "",
            "family_root": sid,
        }
        assert datetime.fromisoformat(hold["since"]) <= datetime.now(timezone.utc)

    @pytest.mark.parametrize("reason", ["steps", "time"])
    def test_a_wrapped_up_child_is_kept_with_its_reason(self, tmp_path: Path, reason: str) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item())

        _wrapped(state, child, reason)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["wrapup"] == reason and entry["continuable"] is True
        assert _hold(state, child.input["child_session_id"])["reason"] == "wrapup"

    def test_a_child_that_finished_normally_is_not_kept(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item())
        child_sid = child.input["child_session_id"]
        path = _conversation(tmp_path, child_sid)

        _finish(state, child)
        _post_cleanup(state, child)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["handle"] == f"{child.task_id}@{child_sid}" and entry["continuable"] is False
        assert state._session_store.get_entry(child_sid) is None
        assert not path.exists()

    def test_a_kept_child_survives_the_cleanup_after_its_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        path = _conversation(tmp_path, child_sid)

        _post_cleanup(state, child)

        assert state._session_store.get_entry(child_sid) is not None
        assert path.exists()
        assert child_sid in state.parent_children.get(sid, set())

    def test_a_failed_async_agent_is_kept_and_its_callback_carries_the_handle(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _finish(state, caller)

        _fail(state, child)
        _post_cleanup(state, child)

        hold = _hold(state, agent_sid)
        assert hold["kind"] == "agent" and hold["parent_task_id"] == caller.task_id and hold["context_mode"] == "fresh"
        assert agent_sid in state.sessions and state._session_store.get_entry(agent_sid) is not None
        [callback] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert callback.input["inbound_child_handle"] == f"{child.task_id}@{agent_sid}"
        assert callback.input["inbound_child_continuable"] is True
        assert "inbound_child_wrapup" not in callback.input

    def test_an_accumulate_agent_needs_no_hold(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller, mode="accumulate")
        _finish(state, caller)

        _fail(state, child)

        assert _hold(state, agent_sid) is None
        # 会话本来就一直留着，能接着做；这一行另记它属于哪段对话、哪个节点
        assert state._session_store.get_entry(agent_sid)[child_retention.LINEAGE_FIELD] == {
            "family_root": sid, "node_id": "a.node", "context_mode": "accumulate",
        }
        [callback] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert callback.input["inbound_child_handle"] == f"{child.task_id}@{agent_sid}"
        assert callback.input["inbound_child_continuable"] is True

    def test_an_accumulate_child_is_continuable_without_a_hold(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item(context_mode="accumulate"))

        _finish(state, child)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["handle"] == f"{child.task_id}@{child.input['child_session_id']}"
        assert entry["continuable"] is True
        assert _hold(state, child.input["child_session_id"]) is None

    def test_an_asking_child_is_kept_and_its_entry_says_how_to_continue(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item(call_id="w1"))
        child_sid = child.input["child_session_id"]
        path = _conversation(tmp_path, child_sid)

        _ask(state, child)
        _post_cleanup(state, child)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "ask" and entry["text"] == "要查哪一年的？"
        assert entry["handle"] == f"{child.task_id}@{child_sid}" and entry["continuable"] is True
        hold = _hold(state, child_sid)
        assert hold["reason"] == "ask" and hold["task_id"] == child.task_id and hold["parent_task_id"] == caller.task_id
        assert path.exists() and state._session_store.get_entry(child_sid) is not None

    def test_an_asking_child_picks_up_where_it_stopped(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item(call_id="w1"))
        child_sid = child.input["child_session_id"]
        _ask(state, child)
        handle = caller.input["resume_data"]["entries"][0]["handle"]

        assert continuation.resolve(state, caller.task_id, handle, "a.node", wait=True)["ok"] is True
        [again] = _dispatch(state, caller, _item(call_id="w2", continue_from=handle, instruction="查 2025 年的"))

        assert again.input["child_session_id"] == child_sid
        assert again.input["continue_from"] == handle
        assert _hold(state, child_sid) is None

    def test_a_preempted_child_is_kept_with_its_handle(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item(call_id="w1"))
        child_sid = child.input["child_session_id"]
        path = _conversation(tmp_path, child_sid)

        _preempt(state, child)
        _post_cleanup(state, child)

        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "preempted"
        assert entry["handle"] == f"{child.task_id}@{child_sid}" and entry["continuable"] is True
        assert _hold(state, child_sid)["reason"] == "preempted"
        assert path.exists()
        assert continuation.resolve(state, caller.task_id, entry["handle"], "a.node", wait=True)["ok"] is True

    def test_an_asking_async_agent_is_kept_and_its_callback_carries_the_handle(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _finish(state, caller)

        _ask(state, child)
        _post_cleanup(state, child)

        hold = _hold(state, agent_sid)
        assert hold["kind"] == "agent" and hold["reason"] == "ask" and hold["parent_task_id"] == caller.task_id
        assert agent_sid in state.sessions and state._session_store.get_entry(agent_sid) is not None
        [callback] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert callback.input["inbound_child_status"] == "ask"
        assert callback.input["inbound_child_handle"] == f"{child.task_id}@{agent_sid}"
        assert callback.input["inbound_child_continuable"] is True

    def test_a_continued_child_is_not_torn_down_by_the_late_cleanup_of_its_previous_run(self, tmp_path: Path) -> None:
        # 这一程结束到清理轮到之间，会话已被接着派出去：保留记录清掉了，可新任务正在写它
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        path = _conversation(tmp_path, child_sid)
        [again] = _dispatch(state, caller, _item(call_id="w2", continue_from=format_handle(child.task_id, child_sid)))
        assert _hold(state, child_sid) is None and again.status == TaskStatus.pending

        _post_cleanup(state, child)

        assert path.exists()
        assert state._session_store.get_entry(child_sid) is not None
        assert child_sid in state.parent_children.get(caller.session_id, set())

    def test_a_continued_async_agent_is_not_torn_down_by_the_late_cleanup_of_its_previous_run(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _fail(state, child)
        path = _conversation(tmp_path, agent_sid)
        again = _inbound(
            state, agent_sid, node="a.node", dispatch_context_mode="fresh",
            dispatch_origin=_origin(sid, caller, continue_from=format_handle(child.task_id, agent_sid)),
        )
        assert _hold(state, agent_sid) is None and not state._task_terminal(again)

        _post_cleanup(state, child)

        assert path.exists()
        assert agent_sid in state.sessions and state._session_store.get_entry(agent_sid) is not None
        assert state.conversation_map[state.sessions[agent_sid].conversation_key] == agent_sid


class TestQuotedOutputs:
    def test_an_output_a_finished_child_quoted_stays_readable_by_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())
        child_sid = child.input["child_session_id"]
        outputs = ToolOutputStore(tmp_path)
        saved = outputs.save(child_sid, "长输出\n" * 5000)

        _finish(state, child, f"全文太长，见 ref={saved.ref}")
        _post_cleanup(state, child)

        with pytest.raises(ToolOutputNotFound):
            outputs.load(child_sid, saved.ref)
        assert outputs.load(sid, saved.ref) == "长输出\n" * 5000

    def test_an_error_can_quote_an_output_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())
        outputs = ToolOutputStore(tmp_path)
        saved = outputs.save(child.input["child_session_id"], "构建日志")

        _fail(state, child, f"构建失败，日志见 ref={saved.ref}")

        assert outputs.load(sid, saved.ref) == "构建日志"

    def test_an_output_an_asking_child_quoted_stays_readable_by_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())
        outputs = ToolOutputStore(tmp_path)
        saved = outputs.save(child.input["child_session_id"], "候选列表\n" * 100)

        _ask(state, child, f"候选太多，见 ref={saved.ref}，要哪一个？")

        assert outputs.load(sid, saved.ref) == "候选列表\n" * 100

    def test_a_quoted_output_that_does_not_exist_is_skipped(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())

        _finish(state, child, f"见 ref={REF_A}")

        assert caller.status == TaskStatus.pending
        with pytest.raises(ToolOutputNotFound):
            ToolOutputStore(tmp_path).load(sid, REF_A)

    def test_mentions_are_found_in_order_without_repeats(self) -> None:
        assert mentioned_refs(f"先看 ref={REF_B}，再看 ref={REF_A}", None, 42, f"还是 ref={REF_B}") == [REF_B, REF_A]

    def test_only_written_out_mentions_count(self) -> None:
        assert mentioned_refs(f"{REF_A} ref=to_short ref=TO_{'a' * 24}") == []


class TestAdopt:
    def test_a_copy_keeps_the_age_of_the_original(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        saved = store.save("child_a", "x" * 10)
        stamp = time.time() - 3600
        os.utime(store.path_for("child_a", saved.ref), (stamp, stamp))

        assert store.adopt("child_a", "parent", saved.ref) is True
        target = store.path_for("parent", saved.ref)
        assert target.read_text(encoding="utf-8") == "x" * 10
        assert abs(target.stat().st_mtime - stamp) < 1
        assert store.adopt("child_a", "parent", saved.ref) is True

    def test_bad_missing_and_expired_outputs_are_not_copied(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        saved = store.save("child_a", "old")
        old = time.time() - 8 * 24 * 3600
        os.utime(store.path_for("child_a", saved.ref), (old, old))

        assert store.adopt("child_a", "parent", "not-a-ref") is False
        assert store.adopt("child_a", "parent", REF_A) is False
        assert store.adopt("child_a", "parent", saved.ref) is False
        assert not store.path_for("parent", saved.ref).exists()

    def test_adopting_into_the_same_session_changes_nothing(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        saved = store.save("child_a", "x")

        assert store.adopt("child_a", "child_a", saved.ref) is True
        assert store.load("child_a", saved.ref) == "x"


class TestResolve:
    def test_a_kept_child_can_be_continued(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        found = continuation.resolve(state, caller.task_id, format_handle(child.task_id, child_sid), "a.node", wait=True)

        assert found == {
            "ok": True, "session_id": child_sid, "task_id": child.task_id, "kind": "child",
            "node_id": "a.node", "context_mode": "fresh",
        }

    def test_a_kept_async_agent_is_continued_asynchronously(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _fail(state, child)
        handle = format_handle(child.task_id, agent_sid)

        found = continuation.resolve(state, caller.task_id, handle, "a.node", wait=False)
        refused = continuation.resolve(state, caller.task_id, handle, "a.node", wait=True)

        assert found["ok"] is True and found["kind"] == "agent" and found["session_id"] == agent_sid
        assert found["conversation_key"] == state.sessions[agent_sid].conversation_key
        assert refused["reason"] == "mode_async_required"

    def test_an_accumulate_child_needs_no_hold(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item(context_mode="accumulate"))
        _finish(state, child)

        found = continuation.resolve(
            state, caller.task_id, format_handle(child.task_id, child.input["child_session_id"]), "a.node", wait=True,
        )

        assert found["ok"] is True and found["context_mode"] == "accumulate"

    def test_a_finished_caller_is_only_accepted_when_routing_its_dispatch(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        handle = format_handle(child.task_id, child_sid)
        caller.status = TaskStatus.completed

        refused = continuation.resolve(state, caller.task_id, handle, "a.node", wait=True)
        with state._lock:
            found = continuation.resolve_locked(
                state, caller.task_id, parse_handle(handle), {"a.node"},
                wait=True, require_live_caller=False,
            )

        assert refused["reason"] == "caller_not_found"
        assert found["ok"] is True

    @pytest.mark.parametrize("case,reason", [
        ("garbage", "invalid_handle"),
        ("missing_caller", "caller_not_found"),
        ("gone", "session_not_found"),
        ("reset", "session_not_found"),
        ("user_session", "not_continuable"),
        ("branch", "not_continuable"),
        ("wrong_task", "handle_mismatch"),
        ("other_conversation", "not_in_family"),
        ("other_node", "target_mismatch"),
        ("async", "mode_wait_required"),
        ("busy", "busy"),
    ])
    def test_every_refusal_has_its_reason(self, tmp_path: Path, case: str, reason: str) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        caller_id, handle, target, wait = caller.task_id, format_handle(child.task_id, child_sid), "a.node", True
        if case == "garbage":
            handle = "not a handle"
        elif case == "missing_caller":
            caller_id = "no-such-task"
        elif case == "gone":
            handle = format_handle(child.task_id, "child_gone")
        elif case == "reset":
            state._session_store.set_entry_field(child_sid, "reset", True)
        elif case == "user_session":
            handle = format_handle(child.task_id, sid)
        elif case == "branch":
            with state._lock:
                branch, _meta = state._create_entry_branch_locked(sid, 77)
            handle = format_handle(child.task_id, branch)
        elif case == "wrong_task":
            handle = format_handle(caller.task_id, child_sid)
        elif case == "other_conversation":
            caller_id = _caller(state, _session(state, OTHER_CONV), key=OTHER_CONV).task_id
        elif case == "other_node":
            target = "b.node"
        elif case == "async":
            wait = False
        elif case == "busy":
            with state._lock:
                state._create_task_locked(
                    session_id=sid, session_generation=1, kind=TaskKind.node, node_id="a.node",
                    input_data={"child_session_id": child_sid}, continuation={}, source_inbound_seq=None,
                )

        refused = continuation.resolve(state, caller_id, handle, target, wait=wait)

        assert refused["ok"] is False and refused["reason"] == reason
        assert refused["message"]

    def test_the_node_is_named_when_the_target_differs(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        refused = continuation.resolve(state, caller.task_id, format_handle(child.task_id, child_sid), "b.node", wait=True)

        assert refused["message"] == "continue_from 指向的子 agent 是 a.node，要接着它做就得委派给同一个节点。"

    def test_a_child_that_finished_normally_is_not_retained_for_continuing(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item())
        _finish(state, child)

        refused = continuation.resolve(
            state, caller.task_id, format_handle(child.task_id, child.input["child_session_id"]), "a.node", wait=True,
        )

        assert refused["reason"] == "not_retained"
        assert "已经正常做完" in refused["message"]


class TestNotRetainedSaysWhatHappened:
    """fresh / fork 子会话没有保留记录时，拒绝理由按产出这个句柄的那一程真实的结局说。"""

    def test_a_cancelled_child(self, tmp_path: Path) -> None:
        # 拆除在锁外的线程里做，拆完之前句柄还查得到会话
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item())
        with state._lock:
            child.status = TaskStatus.cancelled

        refused = continuation.resolve(
            state, caller.task_id, format_handle(child.task_id, child.input["child_session_id"]), "a.node", wait=True,
        )

        assert refused["reason"] == "not_retained"
        assert "已被取消" in refused["message"] and "正常做完" not in refused["message"]

    def test_a_handle_of_an_earlier_run_after_the_child_was_continued(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        old = format_handle(child.task_id, child_sid)
        [again] = _dispatch(state, caller, _item(call_id="w2", continue_from=old))
        _finish(state, again)

        refused = continuation.resolve(state, caller.task_id, old, "a.node", wait=True)

        assert refused["reason"] == "not_retained"
        assert "又被接着派过" in refused["message"]
        latest = caller.input["resume_data"]["entries"][0]["handle"]
        assert latest == format_handle(again.task_id, child_sid)

    def test_a_handle_whose_run_left_memory(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, _item())
        _finish(state, child)
        with state._lock:
            state.tasks.pop(child.task_id)

        refused = continuation.resolve(
            state, caller.task_id, format_handle(child.task_id, child.input["child_session_id"]), "a.node", wait=True,
        )

        assert refused["reason"] == "not_retained"
        assert "结局已经查不到" in refused["message"]


class TestAsyncAccumulateContinuation:
    """异步 accumulate 的 agent: 会话没有保留记录：产出句柄的任务在不在内存里，核对结果都一样。"""

    def _finished(self, state: SupervisorState) -> tuple[str, Task, Task, str]:
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller, mode="accumulate")
        _finish(state, child)
        return sid, caller, child, agent_sid

    def test_it_can_be_continued_while_its_producer_is_in_memory(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)

        found = continuation.resolve(state, caller.task_id, format_handle(child.task_id, agent_sid), "a.node", wait=False)

        assert found["ok"] is True and found["context_mode"] == "accumulate" and found["session_id"] == agent_sid

    def test_it_can_still_be_continued_after_its_producer_left_memory(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)
        with state._lock:
            state.tasks.pop(child.task_id)

        found = continuation.resolve(state, caller.task_id, format_handle(child.task_id, agent_sid), "a.node", wait=False)

        assert found == {
            "ok": True, "session_id": agent_sid, "task_id": child.task_id, "kind": "agent",
            "node_id": "a.node", "context_mode": "accumulate",
            "conversation_key": state.sessions[agent_sid].conversation_key,
        }

    def test_another_conversation_is_still_refused_by_the_recorded_lineage(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)
        with state._lock:
            state.tasks.pop(child.task_id)
        stranger = _inbound(state, _session(state, OTHER_CONV))

        refused = continuation.resolve(
            state, stranger.task_id, format_handle(child.task_id, agent_sid), "a.node", wait=False,
        )

        assert refused["reason"] == "not_in_family"

    def test_the_recorded_node_is_checked_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)
        with state._lock:
            state.tasks.pop(child.task_id)

        refused = continuation.resolve(state, caller.task_id, format_handle(child.task_id, agent_sid), "b.node", wait=False)

        assert refused["reason"] == "target_mismatch"
        assert "a.node" in refused["message"]

    def test_an_old_row_without_a_lineage_says_so_instead_of_blaming_the_conversation(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)
        with state._lock:
            state.tasks.pop(child.task_id)
        state._session_store.set_entry_field(agent_sid, child_retention.LINEAGE_FIELD, None)

        refused = continuation.resolve(state, caller.task_id, format_handle(child.task_id, agent_sid), "a.node", wait=False)

        assert refused["reason"] == "lineage_unknown"
        assert refused["message"] == continuation.refusal("lineage_unknown")["message"]

    def test_the_lineage_is_on_disk_so_a_restart_keeps_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, agent_sid = self._finished(state)
        on_disk = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))

        assert on_disk[agent_sid][child_retention.LINEAGE_FIELD] == {
            "family_root": sid, "node_id": "a.node", "context_mode": "accumulate",
        }

    def test_a_fresh_agent_gets_a_hold_and_no_lineage(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)

        _fail(state, child)

        entry = state._session_store.get_entry(agent_sid)
        assert entry["retain"]["reason"] == "failed"
        assert child_retention.LINEAGE_FIELD not in entry


class TestContinueInABatch:
    def test_continue_from_picks_up_the_kept_session(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        handle = format_handle(child.task_id, child_sid)

        [again] = _dispatch(state, caller, _item(call_id="w2", continue_from=handle, context_mode="fork", context_key="x"))

        assert again.input["child_session_id"] == child_sid
        assert again.input["continue_from"] == handle
        assert again.input["context_mode"] == "fresh"
        assert "fork_from_session_id" not in again.input and "_context_key" not in again.input
        assert _hold(state, child_sid) is None
        assert state._session_store.get_entry(child_sid)["parent_session_id"] == caller.session_id
        assert caller.status == TaskStatus.suspended

    def test_a_refused_continuation_fails_its_item_and_resumes_the_caller_at_once(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        [refused] = _dispatch(state, caller, _item(call_id="w2", continue_from=format_handle(child.task_id, "child_gone")))

        assert refused.status == TaskStatus.failed
        assert caller.status == TaskStatus.pending
        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "fail" and entry["call_id"] == "w2"
        assert entry["error"] == continuation.refusal("session_not_found")["message"]
        assert "handle" not in entry

    def test_a_refused_item_waits_for_its_siblings(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        refused, fresh = _dispatch(
            state, caller,
            _item(call_id="w2", continue_from=format_handle(child.task_id, child_sid), target="b.node"),
            _item("b.node", call_id="w3"),
        )
        assert refused.status == TaskStatus.failed and fresh.status == TaskStatus.pending
        assert caller.status == TaskStatus.suspended

        _finish(state, fresh, "b done")

        entries = caller.input["resume_data"]["entries"]
        assert [entry["status"] for entry in entries] == ["fail", "finish"]
        assert [entry["call_id"] for entry in entries] == ["w2", "w3"]
        assert entries[0]["error"].startswith("continue_from 指向的子 agent 是 a.node")
        assert _hold(state, child_sid) is not None

    def test_a_kept_child_outlives_its_callers_branch_and_goes_with_the_next_one(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        first = _inbound(state, sid)
        branch = first.session_id
        [child] = _dispatch(state, first, _item(call_id="w1"))
        child_sid = child.input["child_session_id"]
        assert state._session_store.get_entry(child_sid)["parent_session_id"] == branch
        _fail(state, child)

        _finish(state, first, "这一轮先到这")

        assert state._session_store.get_entry(branch) is None
        assert state._session_store.get_entry(child_sid)["parent_session_id"] == sid
        assert child_sid in state.parent_children.get(sid, set())

        second = _inbound(state, sid)
        handle = format_handle(child.task_id, child_sid)
        assert continuation.resolve(state, second.task_id, handle, "a.node", wait=True)["ok"] is True
        [again] = _dispatch(state, second, _item(call_id="w2", continue_from=handle))
        assert again.input["child_session_id"] == child_sid
        assert state._session_store.get_entry(child_sid)["parent_session_id"] == second.session_id
        assert _hold(state, child_sid) is None

        _finish(state, again, "接着做完了")
        _finish(state, second, "好了")

        assert state._session_store.get_entry(child_sid) is None

    def test_an_adopted_accumulate_session_is_the_one_the_new_round_keeps_using(self, tmp_path: Path) -> None:
        # 重启后按登记表也是这样挂的：接走以后同一个节点的 accumulate 接着写这一个，不另开
        state = _state(tmp_path)
        sid = _session(state)
        first = _inbound(state, sid)
        [child] = _dispatch(state, first, _item(call_id="w1", context_mode="accumulate"))
        child_sid = child.input["child_session_id"]
        _fail(state, child)
        _finish(state, first, "这一轮先到这")
        assert state._session_store.get_entry(child_sid)["parent_session_id"] == sid

        second = _inbound(state, sid)
        handle = format_handle(child.task_id, child_sid)
        [again] = _dispatch(state, second, _item(call_id="w2", continue_from=handle))
        assert again.input["child_session_id"] == child_sid
        _finish(state, again, "接着做完了")

        [later] = _dispatch(state, second, _item(call_id="w3", context_mode="accumulate"))

        assert later.input["child_session_id"] == child_sid


class TestGracePeriod:
    def test_the_grace_period_starts_when_the_caller_ends(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        path = _conversation(tmp_path, child_sid)

        _sweep(state)
        assert _hold(state, child_sid)["until"] == ""

        caller.status = TaskStatus.completed
        _sweep(state)
        until = _hold(state, child_sid)["until"]
        left = datetime.fromisoformat(until) - datetime.now(timezone.utc)
        assert timedelta(hours=23) < left <= timedelta(hours=24)
        on_disk = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))
        assert on_disk[child_sid]["retain"]["until"] == until

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        state._session_store.set_entry_field(child_sid, "retain", {**_hold(state, child_sid), "until": past})
        _sweep(state)

        assert state._session_store.get_entry(child_sid) is None
        assert child_sid not in state.parent_children.get(sid, set())
        _wait_until(lambda: not path.exists())
        assert not path.exists()

    def test_the_grace_period_is_configurable(self, tmp_path: Path) -> None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "runtime.yaml").write_text(
            "engine:\n  child_session:\n    retain_grace_hours: 2\n", encoding="utf-8",
        )
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        caller.status = TaskStatus.completed

        _sweep(state)

        left = datetime.fromisoformat(_hold(state, child_sid)["until"]) - datetime.now(timezone.utc)
        assert timedelta(hours=1) < left <= timedelta(hours=2)

    def test_a_kept_session_in_use_is_never_swept(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        caller.status = TaskStatus.completed
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        state._session_store.set_entry_field(child_sid, "retain", {**_hold(state, child_sid), "until": past})
        with state._lock:
            state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="a.node",
                input_data={"child_session_id": child_sid}, continuation={}, source_inbound_seq=None,
            )

        _sweep(state)

        assert state._session_store.get_entry(child_sid) is not None

    def test_an_expired_async_agent_leaves_the_conversation_map_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        agent_key = state.sessions[agent_sid].conversation_key
        _fail(state, child)
        caller.status = TaskStatus.completed
        _sweep(state)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        state._session_store.set_entry_field(agent_sid, "retain", {**_hold(state, agent_sid), "until": past})

        _sweep(state)

        assert state._session_store.get_entry(agent_sid) is None
        assert agent_sid not in state.sessions
        assert agent_key not in state.conversation_map

    def test_an_async_continuation_releases_only_its_own_hold(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _fail(state, child)
        handle = format_handle(child.task_id, agent_sid)

        _inbound(
            state, agent_sid, node="a.node", dispatch_context_mode="fresh",
            dispatch_origin=_origin(sid, caller, continue_from=format_handle("other-task", agent_sid)),
        )
        assert _hold(state, agent_sid) is not None

        again = _inbound(
            state, agent_sid, node="a.node", dispatch_context_mode="fresh",
            dispatch_origin=_origin(sid, caller, continue_from=handle),
        )

        assert _hold(state, agent_sid) is None
        assert again.input["_dispatch_origin"]["continue_from"] == handle


@pytest.fixture()
def admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _get(app: Any, caller_id: str, params: dict[str, str], *, auth: bool = True) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(
                f"/v1/tasks/{caller_id}/continuation", params=params, headers=_AUTH if auth else {},
            )

    return asyncio.run(_run())


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


@pytest.mark.usefixtures("admin_token")
class TestEndpoint:
    def test_it_needs_the_admin_token(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        params = {"handle": format_handle(child.task_id, child_sid), "target": "a.node", "wait": "true"}

        response = _get(_app(state, tmp_path), caller.task_id, params, auth=False)

        assert response.status_code == 401

    def test_it_answers_with_the_session_to_continue(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        params = {"handle": format_handle(child.task_id, child_sid), "target": "a.node", "wait": "true"}

        response = _get(_app(state, tmp_path), caller.task_id, params)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True, "session_id": child_sid, "task_id": child.task_id, "kind": "child",
            "node_id": "a.node", "context_mode": "fresh",
        }

    def test_a_refusal_is_an_ordinary_answer(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)
        params = {"handle": format_handle(child.task_id, child_sid), "target": "a.node", "wait": "false"}

        response = _get(_app(state, tmp_path), caller.task_id, params)

        assert response.status_code == 200
        assert response.json()["reason"] == "mode_wait_required"

    def test_oversized_parameters_are_rejected(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, caller, child, child_sid = _kept(state)

        response = _get(_app(state, tmp_path), caller.task_id, {"handle": "x" * 400, "target": "a.node"})

        assert response.status_code == 422
