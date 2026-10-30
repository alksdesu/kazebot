"""supervisor 一侧的异步委派：句柄和结局、结果交给谁、落点那一轮没做完时先留给它再合成一轮交回、await_dispatches 汇合、
list / cancel 只认会话家族、取消顺着派发链往下走、派发方结束后不再收新委派、wait 批次的单项超时、异步 fork 从分支复制。
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.dispatch_batch import AWAIT_KEY, AWAITING_INPUT_KEY  # noqa: E402
from engine.dispatch_handle import format_handle  # noqa: E402
from engine.dispatch_results import RESULT_FALLBACK_KEY  # noqa: E402
from engine.run_budget import AUTO_CONTINUE_KEY, CHAIN_STARTED_AT_KEY  # noqa: E402
from engine.tool_outputs import ToolOutputStore  # noqa: E402
from supervisor import admin_api, async_dispatches  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_reaper import reap_stuck_tasks_locked  # noqa: E402
from supervisor.task_router import PostCompletionWork  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_group:1001"
OTHER_CONV = "qq_group:2002"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
_TOKEN = "async-dispatch-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _default_running_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # 等待截止受任务总寿命约束，寿命按默认的运行上限算
    monkeypatch.delenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", raising=False)


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-async-dispatch"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=key)


def _inbound(state: SupervisorState, session_id: str, *, node: str = "test.orchestrator", text: str = "", **extra: Any) -> Task:
    payload = {
        "channel": "qq",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": text or f"run {node}",
        "entry_node_id": node,
        "platform_auth": dict(AUTH),
        **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    task = state.record_inbound_message_event(event)
    assert task is not None
    return task


def _caller(state: SupervisorState, session_id: str, *, key: str = CONV) -> Task:
    """不在入口分支上的调用方：被判失败时路由当场跑完，不另起线程。"""
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


def _origin(session_id: str, caller: Task, *, mode: str = "fresh", caller_node: str = "test.orchestrator") -> dict[str, Any]:
    return {
        "parent_session_id": session_id, "caller_node_id": caller_node, "parent_conversation_key": CONV,
        "context_mode": mode, "caller_task_id": caller.task_id,
    }


def _async_child(
    state: SupervisorState, session_id: str, caller: Task, *, node: str = "a.node", text: str = "",
    caller_node: str = "test.orchestrator", mode: str = "fresh",
) -> Task:
    """照 engine 异步委派那样投一条带 dispatch_origin 的入站，子节点在自己的 agent: 会话里跑。"""
    agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:{node}:{CONV}:{uuid.uuid4()}")
    return _inbound(
        state, agent_sid, node=node, text=text,
        dispatch_context_mode=mode, dispatch_origin=_origin(session_id, caller, mode=mode, caller_node=caller_node),
    )


def _handle(child: Task) -> str:
    """engine 拿 /v1/inbound 应答里的 task_id 和 session_id 拼出的句柄。"""
    return format_handle(child.task_id, child.input["parent_session_id"])


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _finish(state: SupervisorState, task: Task, text: str = "done") -> None:
    _report(state, task, {"action": "finish", "result": {"text": text}})


def _fail(state: SupervisorState, task: Task, error: str = "network down") -> None:
    _report(state, task, {"action": "fail", "error": error})


def _await(
    state: SupervisorState, caller: Task, *handles: str, mode: str = "all", timeout_sec: Any = None, call_id: str = "",
) -> None:
    """caller 这一程交回 await_dispatches，和 engine 报上来的一样。"""
    request: dict[str, Any] = {"handles": list(handles), "mode": mode}
    if timeout_sec is not None:
        request["timeout_sec"] = timeout_sec
    if call_id:
        request["call_id"] = call_id
    _report(state, caller, {"action": "dispatch", "target_node": "", "dispatch_input": {AWAIT_KEY: request}})


def _item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _wait_batch(state: SupervisorState, caller: Task, *items: dict[str, Any]) -> list[Task]:
    """caller 这一程交回 wait 批次；返回派出的子任务，按批次序号排。"""
    _report(state, caller, {"action": "dispatch", "dispatch_batch": list(items)})
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id),
        key=lambda task: task.batch_index,
    )


def _entries(task: Task) -> list[dict[str, Any]]:
    return task.input["resume_data"]["entries"]


def _callbacks(state: SupervisorState) -> list[Task]:
    return [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]


def _callback_children(state: SupervisorState) -> list[str]:
    return [task.input["inbound_child_task_id"] for task in _callbacks(state)]


def _callback_for(state: SupervisorState, child: Task) -> Task:
    return next(task for task in _callbacks(state) if task.input.get("inbound_child_task_id") == child.task_id)


def _result_inbounds(state: SupervisorState) -> list[dict[str, Any]]:
    """异步委派结果交回时写下的入站事件：每条结果一条，合成一轮交回的也一样。"""
    return [
        event["payload"] for event in state.eventlog.events
        if event["type"] == "inbound_message" and event["payload"].get("message_type") == "dispatch_result"
    ]


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


def _post_cleanup(state: SupervisorState, task: Task) -> None:
    """跑一遍完成后的会话拆除那一步，和后台 post-work 里的是同一个函数。"""
    with state._lock:
        work = PostCompletionWork(
            task=task.model_copy(deep=True),
            route_session_id=state._route_session_id_for_task_locked(task),
            session_generation=task.session_generation,
        )
    state._execute_post_cleanup(work, {"phase_id": f"test:{task.task_id}:cleanup", "kind": "cleanup"})


def _expire(state: SupervisorState, *, after: timedelta) -> None:
    with state._lock:
        async_dispatches.expire_due_locked(state, now=datetime.now(timezone.utc) + after)


def _write_runtime(workspace: Path, data: dict[str, Any]) -> None:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class TestOutcomes:
    @pytest.mark.parametrize("result, status", [
        ({"action": "finish", "result": {"text": "done"}}, "finish"),
        ({"action": "ask", "result": {"text": "要查哪个仓库？"}}, "ask"),
        ({"action": "fail", "error": "network down"}, "fail"),
    ])
    def test_the_callback_says_how_the_dispatch_ended(
        self, tmp_path: Path, result: dict[str, Any], status: str,
    ) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")

        _report(state, child, result)

        [callback] = _callbacks(state)
        assert callback.input["inbound_child_status"] == status
        assert callback.input["inbound_child_task_id"] == child.task_id
        assert callback.input["inbound_child_handle"] == _handle(child)
        assert callback.input["inbound_continues_task_id"] == caller.task_id

    def test_an_outcome_it_does_not_know_is_left_out(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)

        task = _inbound(state, sid, message_type="dispatch_result", child_status="exploded")

        assert "inbound_child_status" not in task.input


class TestAwaitAll:
    def test_one_await_collects_three_dispatches_without_extra_messages(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        children = [_async_child(state, sid, caller, text=f"job {index}") for index in range(3)]
        handles = [_handle(child) for child in children]

        _await(state, caller, *handles, call_id="aw1")

        assert caller.status == TaskStatus.suspended
        assert caller.input[AWAITING_INPUT_KEY]["handles"] == handles
        _finish(state, children[0], "first")
        _fail(state, children[1], "disk full")
        assert caller.status == TaskStatus.suspended
        _finish(state, children[2], "third")

        assert caller.status == TaskStatus.pending
        assert AWAITING_INPUT_KEY not in caller.input
        resume = caller.input["resume_data"]
        assert resume["type"] == "batch_results"
        assert resume["await"] == {"mode": "all", "timeout_sec": 600, "timed_out": False, "call_id": "aw1"}
        entries = resume["entries"]
        assert [entry["status"] for entry in entries] == ["finish", "fail", "finish"]
        assert [entry["handle"] for entry in entries] == handles
        assert [entry["child_task_id"] for entry in entries] == [child.task_id for child in children]
        assert [entry["instruction"] for entry in entries] == ["job 0", "job 1", "job 2"]
        assert [entry["child_session_id"] for entry in entries] == [child.input["parent_session_id"] for child in children]
        assert entries[0]["text"] == "first" and entries[2]["text"] == "third"
        assert entries[1]["error"] == "disk full" and entries[1]["continuable"] is True
        assert _callbacks(state) == []

        _finish(state, caller, "三个都好了")

        assert _callbacks(state) == []

    def test_results_that_came_back_before_the_await_are_not_lost(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        early = _async_child(state, sid, caller)
        late = _async_child(state, sid, caller)
        _finish(state, early, "early result")
        assert _callbacks(state) == []

        _await(state, caller, _handle(early), _handle(late))

        assert caller.status == TaskStatus.suspended
        _finish(state, late, "late result")
        first, second = _entries(caller)
        assert first["status"] == "finish" and first["text"] == "early result"
        assert "delivered_earlier" not in first
        assert second["text"] == "late result" and "delivered_earlier" not in second
        assert _callbacks(state) == []

    def test_a_result_whose_round_has_already_started_is_marked_as_delivered_earlier(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        _finish(state, child, "result")
        [callback] = _callbacks(state)
        callback.status = TaskStatus.running
        callback.claim_id = "claim-1"
        waiter = _inbound(state, sid, node="d.node")

        _await(state, waiter, _handle(child))

        [entry] = _entries(waiter)
        assert entry["text"] == "result" and entry["delivered_earlier"] == "inbound"
        assert callback.status == TaskStatus.running
        assert callback.input["inbound_child_task_id"] == child.task_id

    def test_an_await_on_dispatches_that_all_ended_resumes_at_once(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _fail(state, child, "boom")

        _await(state, caller, _handle(child))

        assert caller.status == TaskStatus.pending
        [entry] = _entries(caller)
        assert entry["status"] == "fail" and entry["error"] == "boom"
        assert "delivered_earlier" not in entry
        assert caller.input["resume_data"]["await"]["timed_out"] is False
        assert _callbacks(state) == []


class TestAwaitAny:
    def test_any_resumes_on_the_first_result_and_keeps_the_rest_for_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        slow = _async_child(state, sid, caller)
        fast = _async_child(state, sid, caller)

        _await(state, caller, _handle(slow), _handle(fast), mode="any")
        _finish(state, fast, "fast")

        assert caller.status == TaskStatus.pending
        running, done = _entries(caller)
        assert running["status"] == "running" and running["handle"] == _handle(slow)
        assert running["state"] == "pending" and "timed_out" not in running
        assert done["status"] == "finish" and done["text"] == "fast"

        _finish(state, slow, "slow")

        assert _callbacks(state) == []
        _await(state, caller, _handle(slow))
        assert caller.status == TaskStatus.pending
        [entry] = _entries(caller)
        assert entry["status"] == "finish" and entry["text"] == "slow"
        assert "delivered_earlier" not in entry
        assert _callbacks(state) == []

    def test_a_second_await_before_the_rest_finish_waits_for_them(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        slow = _async_child(state, sid, caller)
        fast = _async_child(state, sid, caller)
        _await(state, caller, _handle(slow), _handle(fast), mode="any")
        _finish(state, fast)

        _await(state, caller, _handle(slow))

        assert caller.status == TaskStatus.suspended
        _finish(state, slow, "slow")
        assert caller.status == TaskStatus.pending
        assert [entry["text"] for entry in _entries(caller)] == ["slow"]
        assert _callbacks(state) == []

    def test_a_result_kept_for_the_caller_comes_back_as_a_message_once_it_ends(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        slow = _async_child(state, sid, caller)
        fast = _async_child(state, sid, caller)
        _await(state, caller, _handle(slow), _handle(fast), mode="any")
        _finish(state, fast)
        _finish(state, slow, "slow")
        assert _callbacks(state) == []

        _finish(state, caller, "先报了快的那个")

        assert _callback_children(state) == [slow.task_id]
        [callback] = _callbacks(state)
        assert callback.input["inbound_child_status"] == "finish"


class TestAwaitDeadlines:
    def test_an_await_that_runs_out_of_time_resumes_with_what_it_has(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        done = _async_child(state, sid, caller)
        slow = _async_child(state, sid, caller)
        _await(state, caller, _handle(done), _handle(slow), timeout_sec=30)
        assert caller.input[AWAITING_INPUT_KEY]["timeout_sec"] == 30
        _finish(state, done, "ok")

        _expire(state, after=timedelta(seconds=10))
        assert caller.status == TaskStatus.suspended
        _expire(state, after=timedelta(seconds=31))

        assert caller.status == TaskStatus.pending
        resume = caller.input["resume_data"]
        assert resume["await"]["timed_out"] is True and resume["await"]["timeout_sec"] == 30
        finished, running = resume["entries"]
        assert finished["status"] == "finish"
        assert running["status"] == "running" and running["timed_out"] is True and running["waited_sec"] == 30
        assert "lifetime_capped" not in running

        # 到点时还在跑的那个仍然留给调用方：不另开消息，调用方结束时才交回
        _finish(state, slow, "late")
        assert _callbacks(state) == []
        _finish(state, caller, "先说做完的那个")
        assert _callback_children(state) == [slow.task_id]

    @pytest.mark.parametrize("raw, expected", [
        (None, 600), (999999, 3600), (1, 5), ("45", 45), (12.2, 13), ("abc", 600),
    ])
    def test_the_supervisor_clamps_the_wait_on_its_own(self, tmp_path: Path, raw: Any, expected: int) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)

        _await(state, caller, _handle(child), timeout_sec=raw)

        assert caller.input[AWAITING_INPUT_KEY]["timeout_sec"] == expected

    def test_the_limits_come_from_the_runtime_config(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_runtime(tmp_path, {"engine": {"dispatch": {"await_timeout_sec": 90, "max_timeout_sec": 120}}})
        sid = _session(state)
        unlimited = _inbound(state, sid)
        greedy = _inbound(state, sid)
        child = _async_child(state, sid, unlimited)
        other = _async_child(state, sid, greedy)

        _await(state, unlimited, _handle(child))
        _await(state, greedy, _handle(other), timeout_sec=999999)

        assert unlimited.input[AWAITING_INPUT_KEY]["timeout_sec"] == 90
        assert greedy.input[AWAITING_INPUT_KEY]["timeout_sec"] == 120

    def test_a_wait_that_would_outlive_the_task_is_cut_short(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 运行上限 300 秒，总寿命 30 分钟：截止留出 10 分钟余量，落在第 20 分钟
        monkeypatch.setenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "300")
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)

        _await(state, caller, _handle(child), timeout_sec=3600)

        deadline = datetime.fromisoformat(caller.input[AWAITING_INPUT_KEY]["deadline"])
        assert deadline <= caller.created_at + timedelta(minutes=20)
        _expire(state, after=timedelta(minutes=21))
        [entry] = _entries(caller)
        assert entry["status"] == "running" and entry["timed_out"] is True and entry["lifetime_capped"] is True
        assert entry["waited_sec"] < 3600

    def test_the_reaper_leaves_an_awaiting_task_alone(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _await(state, caller, _handle(child), timeout_sec=3600)

        with state._lock:
            reaped = reap_stuck_tasks_locked(
                state, now=datetime.now(timezone.utc) + timedelta(minutes=25), session_ids={caller.session_id},
            )

        assert reaped == []
        assert caller.status == TaskStatus.suspended

    def test_the_housekeeping_sweep_resumes_a_join_past_its_deadline(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _await(state, caller, _handle(child))

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=datetime.now(timezone.utc) + timedelta(minutes=11))

        assert reaped == []
        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["await"]["timed_out"] is True

    def test_a_task_still_marked_as_awaiting_without_its_join_is_reaped(self, tmp_path: Path) -> None:
        # 汇合登记没了（比如恢复到一半出错）的挂起任务不会一直挂着，按丢了等待目标回收
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        child = _async_child(state, sid, caller)
        _await(state, caller, _handle(child))

        with state._lock:
            async_dispatches.registry_locked(state).joins.clear()
            reaped = reap_stuck_tasks_locked(
                state, now=datetime.now(timezone.utc) + timedelta(minutes=6), session_ids={sid},
            )

        assert reaped == [caller.task_id]
        assert caller.status == TaskStatus.failed
        _finish(state, child, "still delivered")
        assert _callback_children(state) == [child.task_id]


class TestAwaitRefusals:
    def test_handles_that_cannot_be_read_or_found_come_back_as_unavailable(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)

        _await(state, caller, "not-a-handle", "abc@agent-missing", _handle(child))

        assert caller.status == TaskStatus.suspended
        _finish(state, child, "ok")
        malformed, missing, found = _entries(caller)
        assert malformed["status"] == "unavailable" and malformed["handle"] == "not-a-handle"
        assert "句柄格式不对" in malformed["error"]
        assert missing["status"] == "unavailable" and "找不到这个委派" in missing["error"]
        assert found["status"] == "finish"

    def test_a_handle_from_another_conversation_is_treated_as_unknown(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        owner = _inbound(state, sid)
        stranger = _inbound(state, _session(state, OTHER_CONV))
        child = _async_child(state, sid, owner)

        _await(state, stranger, _handle(child))

        assert stranger.status == TaskStatus.pending
        [entry] = _entries(stranger)
        assert entry["status"] == "unavailable" and "找不到这个委派" in entry["error"]
        _finish(state, owner, "派出去了")
        _finish(state, child, "for the owner")
        [callback] = _callbacks(state)
        assert callback.input["inbound_child_task_id"] == child.task_id
        assert callback.input["parent_session_id"] == sid

    def test_a_wait_dispatch_handle_is_not_awaited(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        batch_caller = _caller(state, sid)
        [child] = _wait_batch(state, batch_caller, _item())
        other = _inbound(state, sid)

        _await(state, other, format_handle(child.task_id, child.input["child_session_id"]))

        [entry] = _entries(other)
        assert entry["status"] == "unavailable" and "wait=true" in entry["error"]
        assert "await_dispatches 收不到" in entry["error"]

    def test_a_finished_wait_dispatch_handle_says_its_result_already_came_back(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        batch_caller = _caller(state, sid)
        [child] = _wait_batch(state, batch_caller, _item())
        _finish(state, child, "batch result")
        other = _inbound(state, sid)

        _await(state, other, format_handle(child.task_id, child.input["child_session_id"]))

        [entry] = _entries(other)
        assert entry["status"] == "unavailable" and "已经随那一批结果交回" in entry["error"]

    def test_a_wait_dispatch_handle_from_another_conversation_is_unknown(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        batch_caller = _caller(state, sid)
        [child] = _wait_batch(state, batch_caller, _item())
        stranger = _inbound(state, _session(state, OTHER_CONV))
        handle = format_handle(child.task_id, child.input["child_session_id"])

        _await(state, stranger, handle)
        cancelled = async_dispatches.cancel_dispatch(state, stranger.task_id, handle)

        [entry] = _entries(stranger)
        assert entry["status"] == "unavailable" and "找不到这个委派" in entry["error"]
        assert "wait=true" not in entry["error"]
        assert (cancelled["ok"], cancelled["reason"]) == (False, "not_found")
        assert child.status == TaskStatus.pending

    def test_a_task_cannot_await_itself_or_the_task_that_dispatched_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        parent = _async_child(state, sid, caller)
        grandchild = _async_child(state, parent.input["parent_session_id"], parent, node="g.node", caller_node="a.node")

        _await(state, grandchild, _handle(grandchild), _handle(parent))

        assert grandchild.status == TaskStatus.pending
        assert [entry["status"] for entry in _entries(grandchild)] == ["unavailable", "unavailable"]
        assert all("不能等自己" in entry["error"] for entry in _entries(grandchild))

    def test_the_same_dispatch_written_twice_counts_once(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)

        _await(state, caller, _handle(child), _handle(child).replace("@", " @ "))
        _finish(state, child)

        first, second = _entries(caller)
        assert first["status"] == "finish"
        assert second["status"] == "unavailable" and "同一个委派" in second["error"]

    def test_a_system_task_cannot_wait(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid, _system_task=True)
        child = _async_child(state, sid, caller)

        _await(state, caller, _handle(child))

        assert caller.status == TaskStatus.pending
        [entry] = _entries(caller)
        assert entry["status"] == "unavailable" and "系统任务" in entry["error"]

    def test_a_dispatch_another_task_is_waiting_for_is_not_taken(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        first = _inbound(state, sid)
        second = _inbound(state, sid)
        child = _async_child(state, sid, first)
        _await(state, first, _handle(child))

        _await(state, second, _handle(child))

        [entry] = _entries(second)
        assert entry["status"] == "unavailable" and "已经在等这个委派" in entry["error"]
        _finish(state, child, "ok")
        assert first.status == TaskStatus.pending
        assert [entry["text"] for entry in _entries(first)] == ["ok"]

    def test_a_result_another_waiting_task_already_collected_is_not_taken_twice(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        first = _inbound(state, sid)
        second = _inbound(state, sid)
        done = _async_child(state, sid, first)
        slow = _async_child(state, sid, first)
        _await(state, first, _handle(done), _handle(slow))
        _finish(state, done, "for the first")

        _await(state, second, _handle(done))

        [entry] = _entries(second)
        assert entry["status"] == "unavailable" and "还在等这个结果" in entry["error"]
        _finish(state, slow)
        assert [entry["status"] for entry in _entries(first)] == ["finish", "finish"]
        assert "delivered_earlier" not in _entries(first)[0]


class TestListAndCancel:
    def test_the_list_shows_running_and_finished_dispatches_of_the_family(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        running = _async_child(state, sid, caller, text="crawl the docs")
        done = _async_child(state, sid, caller, text="summarise")
        _finish(state, done, "summary text")

        listed = async_dispatches.list_dispatches(state, caller.task_id)

        assert listed["ok"] is True
        [active] = listed["active"]
        assert active["handle"] == _handle(running) and active["node_id"] == "a.node"
        assert active["status"] == "queued" and active["instruction"] == "crawl the docs"
        assert active["dispatched_by"] == "you"
        [finished] = listed["finished"]
        assert finished["handle"] == _handle(done) and finished["status"] == "finish"
        assert finished["summary"] == "summary text" and finished["delivered"] == "held_for_your_round"
        assert "await_dispatches" in finished["held_note"]
        assert listed["active_total"] == 1 and listed["finished_total"] == 1

    def test_another_task_of_the_family_sees_whose_round_a_result_waits_for(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        done = _async_child(state, sid, caller)
        _finish(state, done, "summary text")
        other = _inbound(state, sid, node="d.node")

        [finished] = async_dispatches.list_dispatches(state, other.task_id)["finished"]

        assert finished["delivered"] == "held_for_round"
        assert caller.task_id[:8] in finished["held_note"]
        _finish(state, caller, "这一轮做完了")
        [finished] = async_dispatches.list_dispatches(state, other.task_id)["finished"]
        assert finished["delivered"] == "inbound" and "held_note" not in finished

    def test_another_conversation_sees_none_of_them(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        _async_child(state, sid, caller)
        stranger = _inbound(state, _session(state, OTHER_CONV))

        listed = async_dispatches.list_dispatches(state, stranger.task_id)

        assert listed["active"] == [] and listed["finished"] == []
        assert async_dispatches.list_dispatches(state, "no-such-task") is None

    def test_cancelling_one_dispatch_leaves_the_others_running(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        target = _async_child(state, sid, caller)
        other = _async_child(state, sid, caller)

        result = async_dispatches.cancel_dispatch(state, caller.task_id, _handle(target))

        assert result == {
            "ok": True, "handle": _handle(target), "node_id": "a.node",
            "cancelled_count": 1, "stopping_count": 0, "status": "cancelled",
        }
        assert target.status == TaskStatus.cancelled
        assert other.status == TaskStatus.pending
        _finish(state, caller, "取消了一个")
        _finish(state, other, "still here")
        assert _callback_children(state) == [other.task_id]

    def test_a_running_dispatch_is_asked_to_stop_and_its_result_is_not_delivered(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        target = _async_child(state, sid, caller)
        target.status = TaskStatus.running
        target.worker_id = "worker-1"
        target.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        result = async_dispatches.cancel_dispatch(state, caller.task_id, _handle(target))

        assert result["status"] == "stopping" and result["stopping_count"] == 1 and result["cancelled_count"] == 0
        assert target.cancel_requested is True and target.status == TaskStatus.running
        state.complete_task(task_id=target.task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "late"}})
        assert target.status == TaskStatus.cancelled
        assert _callbacks(state) == []

    def test_cancelling_reaches_everything_the_dispatch_sent_out(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        worker = _async_child(state, sid, caller)
        [helper] = _wait_batch(state, worker, _item("b.node"))
        leaf = _async_child(state, worker.input["parent_session_id"], helper, node="c.node", caller_node="b.node")
        bystander = _async_child(state, sid, caller)

        result = async_dispatches.cancel_dispatch(state, caller.task_id, _handle(worker))

        assert result["ok"] is True and result["status"] == "cancelled"
        assert result["cancelled_count"] == 3
        assert [worker.status, helper.status, leaf.status] == [TaskStatus.cancelled] * 3
        assert bystander.status == TaskStatus.pending

    def test_a_finished_dispatch_still_takes_its_running_descendants_down(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        worker = _async_child(state, sid, caller)
        leaf = _async_child(state, worker.input["parent_session_id"], worker, node="c.node", caller_node="a.node")
        _finish(state, worker, "handed off")

        result = async_dispatches.cancel_dispatch(state, caller.task_id, _handle(worker))

        assert result == {
            "ok": True, "handle": _handle(worker), "node_id": "a.node",
            "cancelled_count": 1, "stopping_count": 0, "status": "finish", "already_finished": True,
        }
        assert leaf.status == TaskStatus.cancelled

    def test_cancel_refuses_what_is_not_the_callers_to_cancel(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        grandchild = _async_child(state, child.input["parent_session_id"], child, node="g.node", caller_node="a.node")
        stranger = _inbound(state, _session(state, OTHER_CONV))

        foreign = async_dispatches.cancel_dispatch(state, stranger.task_id, _handle(child))
        malformed = async_dispatches.cancel_dispatch(state, caller.task_id, "nonsense")
        upward = async_dispatches.cancel_dispatch(state, grandchild.task_id, _handle(child))
        unknown_caller = async_dispatches.cancel_dispatch(state, "no-such-task", _handle(child))

        assert (foreign["ok"], foreign["reason"]) == (False, "not_found")
        assert (malformed["ok"], malformed["reason"]) == (False, "invalid_handle")
        assert (upward["ok"], upward["reason"]) == (False, "self")
        assert (unknown_caller["ok"], unknown_caller["reason"]) == (False, "caller_not_found")
        assert child.status == TaskStatus.pending and grandchild.status == TaskStatus.pending

    def test_a_wait_dispatch_cannot_be_cancelled_on_its_own(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        batch_caller = _caller(state, sid)
        [child] = _wait_batch(state, batch_caller, _item())
        other = _inbound(state, sid)
        handle = format_handle(child.task_id, child.input["child_session_id"])

        running = async_dispatches.cancel_dispatch(state, other.task_id, handle)
        assert child.status == TaskStatus.pending
        _finish(state, child, "batch result")
        finished = async_dispatches.cancel_dispatch(state, other.task_id, handle)

        assert (running["ok"], running["reason"]) == (False, "sync_dispatch")
        assert "不能单独取消" in running["message"] and "挂起等它的那个任务" in running["message"]
        assert (finished["ok"], finished["reason"]) == (False, "sync_dispatch")
        assert "它已经结束" in finished["message"]
        assert child.status == TaskStatus.completed and batch_caller.status == TaskStatus.pending

    def test_a_waiting_task_sees_a_cancelled_dispatch_as_cancelled(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        waiter = _inbound(state, sid)
        target = _async_child(state, sid, waiter)
        other = _async_child(state, sid, waiter)
        _await(state, waiter, _handle(target), _handle(other))
        canceller = _inbound(state, sid)

        async_dispatches.cancel_dispatch(state, canceller.task_id, _handle(target))
        _finish(state, other, "ok")

        cancelled, finished = _entries(waiter)
        assert cancelled["status"] == "cancelled" and cancelled["error"] == "调用方取消了这个委派"
        assert finished["status"] == "finish"


class TestEndingTheWaiter:
    def test_a_cancelled_waiter_drops_what_it_collected(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        dispatcher = _inbound(state, sid, node="d.node")
        done = _async_child(state, sid, dispatcher, caller_node="d.node")
        slow = _async_child(state, sid, dispatcher, caller_node="d.node")
        _finish(state, dispatcher, "派出去了")
        caller = _inbound(state, sid)
        _await(state, caller, _handle(done), _handle(slow))
        _finish(state, done, "collected")

        state.cancel_active_tasks(sid, node_id="test.orchestrator")

        assert caller.status == TaskStatus.cancelled
        assert slow.status == TaskStatus.pending
        assert _callbacks(state) == []
        # 取消之后才结束的照常作为新消息交回，和没有汇合时一样
        _finish(state, slow, "arrived later")
        assert _callback_children(state) == [slow.task_id]
        # 丢掉的结果还查得到，也还能由别的任务收取
        newcomer = _inbound(state, sid)
        listed = async_dispatches.list_dispatches(state, newcomer.task_id)
        assert {item["handle"]: item["delivered"] for item in listed["finished"]}[_handle(done)] == "dropped"
        _await(state, newcomer, _handle(done))
        [entry] = _entries(newcomer)
        assert entry["text"] == "collected" and "delivered_earlier" not in entry

    def test_a_waiter_that_fails_hands_what_it_collected_back_as_messages(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        done = _async_child(state, sid, caller)
        slow = _async_child(state, sid, caller)
        _await(state, caller, _handle(done), _handle(slow), timeout_sec=3600)
        _finish(state, done, "collected")

        with state._lock:
            reaped = reap_stuck_tasks_locked(
                state, now=datetime.now(timezone.utc) + timedelta(hours=7), session_ids={sid},
            )

        assert reaped == [caller.task_id]
        assert caller.status == TaskStatus.failed
        assert _callback_children(state) == [done.task_id]
        _finish(state, slow, "after the caller")
        # 交回 done 的那一轮还没做完，slow 的结果先留给它
        assert _callback_children(state) == [done.task_id]
        [first] = _callbacks(state)
        _finish(state, first, "看过了")
        assert _callback_children(state) == [done.task_id, slow.task_id]

    def test_cancelling_the_session_reaches_its_async_dispatches(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        done = _async_child(state, sid, caller)
        slow = _async_child(state, sid, caller)
        _await(state, caller, _handle(done), _handle(slow))
        _finish(state, done)

        assert state.cancel_session(sid) is True

        assert caller.status == TaskStatus.cancelled and slow.status == TaskStatus.cancelled
        assert _callbacks(state) == []

    def test_cancelling_the_active_tasks_reaches_async_dispatches(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)

        result = state.cancel_active_tasks(sid)

        assert result == {"ok": True, "cancelled_count": 2}
        assert caller.status == TaskStatus.cancelled and child.status == TaskStatus.cancelled

    def test_cancelling_one_task_reaches_its_async_dispatches(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        other_caller = _inbound(state, sid)
        unrelated = _async_child(state, sid, other_caller)

        result = state.cancel_single_task(caller.task_id)

        assert result == {"ok": True, "cancelled_count": 2}
        assert child.status == TaskStatus.cancelled
        assert unrelated.status == TaskStatus.pending


class TestResultHomes:
    def test_a_sync_middle_layer_hands_the_result_to_the_top_of_its_chain(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        [middle] = _wait_batch(state, top, _item("b.node"))
        leaf = _async_child(state, sid, middle, node="c.node", caller_node="b.node")

        _finish(state, leaf, "leaf result")

        [callback] = _callbacks(state)
        assert callback.node_id == "test.orchestrator"
        assert callback.input["inbound_child_task_id"] == leaf.task_id
        assert callback.input["inbound_continues_task_id"] == top.task_id
        note = callback.input["inbound_delivery_note"]
        assert '"b.node"' in note and '"c.node"' in note and "you ->" in note
        [payload] = _result_inbounds(state)
        assert payload["entry_node_id"] == "test.orchestrator" and payload["entry_node_pinned"] is True
        assert payload["continues_task_id"] == top.task_id

    def test_with_the_chain_records_gone_the_result_still_goes_to_the_top_node(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        [middle] = _wait_batch(state, top, _item("b.node"))
        leaf = _async_child(state, sid, middle, node="c.node", caller_node="b.node")
        assert leaf.input[RESULT_FALLBACK_KEY]["session_id"] == sid
        assert leaf.input[RESULT_FALLBACK_KEY]["node_id"] == "test.orchestrator"
        with state._lock:
            state.tasks.pop(middle.task_id)
            state.tasks.pop(top.task_id)

        _finish(state, leaf, "leaf result")

        [callback] = _callbacks(state)
        assert callback.node_id == "test.orchestrator"
        assert "inbound_continues_task_id" not in callback.input

    def test_outputs_the_result_quotes_are_copied_to_the_conversation_that_reads_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        [middle] = _wait_batch(state, top, _item("b.node"))
        leaf = _async_child(state, sid, middle, node="c.node", caller_node="b.node")
        outputs = ToolOutputStore(tmp_path)
        saved = outputs.save(leaf.input["parent_session_id"], "长输出\n" * 100)

        _finish(state, leaf, f"全文见 ref={saved.ref}")

        assert outputs.load(sid, saved.ref) == "长输出\n" * 100

    def test_a_dispatch_from_the_node_that_receives_its_result_records_no_fallback(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)

        child = _async_child(state, sid, caller)

        assert RESULT_FALLBACK_KEY not in child.input

    def test_a_fresh_middle_layer_that_ended_passes_the_result_up(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        middle = _async_child(state, sid, top, node="b.node")
        middle_sid = middle.input["parent_session_id"]
        leaf = _async_child(state, middle_sid, middle, node="c.node", caller_node="b.node")
        _finish(state, top, "交给 b 了")
        _finish(state, middle, "交给 c 了")
        _post_cleanup(state, middle)
        assert middle_sid not in state.sessions
        [first] = _callbacks(state)
        assert first.input["inbound_child_task_id"] == middle.task_id

        _finish(state, leaf, "leaf result")

        assert _callback_children(state) == [middle.task_id]
        _finish(state, first, "收到")
        second = _callback_for(state, leaf)
        assert second.node_id == "test.orchestrator"
        assert second.input["parent_session_id"] == sid
        assert second.input["inbound_continues_task_id"] == top.task_id
        assert '"b.node"' in second.input["inbound_delivery_note"]

    def test_an_accumulate_middle_layer_continues_in_its_own_session(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        middle = _async_child(state, sid, top, node="b.node", mode="accumulate")
        middle_sid = middle.input["parent_session_id"]
        leaf = _async_child(state, middle_sid, middle, node="c.node", caller_node="b.node")
        _finish(state, top, "交给 b 了")
        _finish(state, middle, "交给 c 了")
        _finish(state, _callback_for(state, middle), "收到")

        _finish(state, leaf, "leaf result")

        resumed = _callback_for(state, leaf)
        assert resumed.node_id == "b.node"
        assert resumed.input["parent_session_id"] == middle_sid
        assert resumed.input["_dispatch_origin"]["caller_task_id"] == top.task_id
        assert resumed.input["inbound_continues_task_id"] == middle.task_id
        assert "inbound_delivery_note" not in resumed.input

        _finish(state, resumed, "b 接着做完了")

        assert "b 接着做完了" not in _outbound_texts(state)
        relayed = _callback_for(state, resumed)
        assert relayed.node_id == "test.orchestrator" and relayed.input["parent_session_id"] == sid
        assert relayed.input["inbound_continues_task_id"] == top.task_id

    def test_a_switched_conversation_still_hands_the_result_to_the_dispatching_node(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        state.session_entry_overrides[sid] = "other.node"

        _finish(state, child, "result")

        [callback] = _callbacks(state)
        assert callback.node_id == "test.orchestrator"
        [payload] = _result_inbounds(state)
        assert payload["entry_node_pinned"] is True

    def test_a_system_dispatcher_leaves_the_node_to_the_conversation_and_holds_nothing(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid, _system_task=True)
        child = _async_child(state, sid, caller)

        _finish(state, child, "result")

        [payload] = _result_inbounds(state)
        assert "entry_node_id" not in payload and "entry_node_pinned" not in payload
        [callback] = _callbacks(state)
        assert callback.input["inbound_continues_task_id"] == caller.task_id

    def test_a_dispatch_that_returned_nothing_still_opens_a_round(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")

        _finish(state, child, "")

        [callback] = _callbacks(state)
        assert callback.input["instruction"] == ""
        assert callback.input["inbound_child_status"] == "finish"


class TestHeldForTheRound:
    def test_results_wait_for_the_round_and_come_back_together(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        first = _async_child(state, sid, caller, text="job 1")
        second = _async_child(state, sid, caller, text="job 2")
        caller.status = TaskStatus.running

        _finish(state, first, "first result")
        _fail(state, second, "second broke")

        assert _callbacks(state) == []
        _finish(state, caller, "这一轮先到这")

        [callback] = _callbacks(state)
        assert callback.node_id == "test.orchestrator"
        assert callback.input["inbound_child_task_id"] == first.task_id
        assert callback.input["inbound_continues_task_id"] == caller.task_id
        results = callback.input["inbound_results"]
        assert [item["inbound_child_task_id"] for item in results] == [first.task_id, second.task_id]
        assert [item["inbound_child_status"] for item in results] == ["finish", "fail"]
        assert results[0]["text"] == "first result" and results[1]["text"] == "[错误] second broke"
        assert [payload["child_task_id"] for payload in _result_inbounds(state)] == [first.task_id, second.task_id]

    def test_a_group_interjection_round_holds_nothing(self, tmp_path: Path) -> None:
        """群里自己插话的那一轮不留结果：它被引擎重启时静默取消，留给它的结果会跟着丢，而那些结果是别的请求的。"""
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        interjection = _inbound(state, sid, route_hints={"delivery_purpose": "ambient"})
        interjection.status = TaskStatus.running

        _finish(state, child, "result")

        [callback] = _callbacks(state)
        assert callback.input["inbound_child_task_id"] == child.task_id

    def test_the_round_can_collect_held_results_itself(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, child, "result")

        _await(state, caller, _handle(child))

        [entry] = _entries(caller)
        assert entry["text"] == "result" and "delivered_earlier" not in entry
        _finish(state, caller, "收到了")
        assert _callbacks(state) == []

    def test_a_round_that_suspends_to_wait_hands_its_held_results_back(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, child, "result")
        assert _callbacks(state) == []

        _wait_batch(state, caller, _item("b.node"))

        assert caller.status == TaskStatus.suspended
        assert _callback_children(state) == [child.task_id]

    def test_a_round_suspended_for_compaction_keeps_them(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        with state._lock:
            caller.input[async_dispatches.COMPACT_PENDING_KEY] = True
            state._suspend_caller_locked(caller, waiting_task_id="compaction")

        _finish(state, child, "result")

        assert _callbacks(state) == []
        with state._lock:
            state._resume_compact_parent_locked(caller, "")
        assert _callbacks(state) == []
        _finish(state, caller, "压缩完接着做完了")
        assert _callback_children(state) == [child.task_id]

    def test_a_round_that_fails_hands_its_held_results_back(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, child, "result")
        assert _callbacks(state) == []

        _fail(state, caller, "模型出错")

        assert _callback_children(state) == [child.task_id]

    def test_a_cancelled_round_drops_them_but_they_can_still_be_collected(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, child, "result")

        state.cancel_single_task(caller.task_id)

        assert caller.status == TaskStatus.cancelled
        assert _callbacks(state) == []
        newcomer = _inbound(state, sid)
        [finished] = async_dispatches.list_dispatches(state, newcomer.task_id)["finished"]
        assert finished["delivered"] == "dropped"
        _await(state, newcomer, _handle(child))
        [entry] = _entries(newcomer)
        assert entry["text"] == "result" and "delivered_earlier" not in entry

    def test_a_queued_round_holds_them_and_hands_them_on_without_passing_them_to_the_next(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        queued = _inbound(state, sid, text="下一句")
        later = _inbound(state, sid, text="再下一句")

        _finish(state, child, "result")

        assert _callbacks(state) == []
        [finished] = async_dispatches.list_dispatches(state, queued.task_id)["finished"]
        assert finished["delivered"] == "held_for_your_round"
        _finish(state, queued, "回了下一句")
        assert later.status == TaskStatus.pending
        assert _callback_children(state) == [child.task_id]

    def test_only_rounds_of_the_receiving_node_in_its_conversation_hold_results(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        _inbound(state, sid, node="d.node")
        _inbound(state, _session(state, OTHER_CONV))

        _finish(state, child, "result")

        assert _callback_children(state) == [child.task_id]


class TestCollectingWhatWasHandedBack:
    def test_an_await_takes_the_result_back_from_a_round_that_has_not_started(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        _finish(state, child, "result")
        [callback] = _callbacks(state)
        waiter = _inbound(state, sid, node="d.node")

        _await(state, waiter, _handle(child))

        [entry] = _entries(waiter)
        assert entry["text"] == "result" and "delivered_earlier" not in entry
        assert callback.status == TaskStatus.cancelled

    def test_an_await_takes_one_result_out_of_a_merged_round(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        first = _async_child(state, sid, caller)
        second = _async_child(state, sid, caller)
        _finish(state, first, "first result")
        _finish(state, second, "second result")
        _finish(state, caller, "这一轮先到这")
        [callback] = _callbacks(state)
        assert len(callback.input["inbound_results"]) == 2
        waiter = _inbound(state, sid, node="d.node")

        _await(state, waiter, _handle(first))

        [entry] = _entries(waiter)
        assert entry["text"] == "first result" and "delivered_earlier" not in entry
        assert callback.status == TaskStatus.pending
        assert "inbound_results" not in callback.input
        assert callback.input["inbound_child_task_id"] == second.task_id
        assert callback.input["instruction"] == "second result"

    def test_a_withdrawn_round_still_hands_back_what_was_held_for_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        first = _async_child(state, sid, caller)
        second = _async_child(state, sid, caller)
        _finish(state, caller, "派出去了")
        _finish(state, first, "first result")
        [callback] = _callbacks(state)
        _finish(state, second, "second result")
        assert _callback_children(state) == [first.task_id]
        waiter = _inbound(state, sid, node="d.node")

        _await(state, waiter, _handle(first))

        assert callback.status == TaskStatus.cancelled
        assert _callback_for(state, second).status == TaskStatus.pending


class TestCancelReach:
    def test_cancelling_a_finished_dispatch_reaches_the_round_that_continues_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        middle = _async_child(state, sid, top, node="b.node", mode="accumulate")
        leaf = _async_child(state, middle.input["parent_session_id"], middle, node="c.node", caller_node="b.node")
        _finish(state, top, "交给 b 了")
        _finish(state, middle, "交给 c 了")
        _finish(state, leaf, "leaf result")
        resumed = _callback_for(state, leaf)
        canceller = _inbound(state, sid, node="d.node")

        result = async_dispatches.cancel_dispatch(state, canceller.task_id, _handle(middle))

        assert result["ok"] is True and result["already_finished"] is True
        assert resumed.status == TaskStatus.cancelled

    def test_cancelling_a_node_takes_down_what_its_rounds_dispatched(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        dispatched = _async_child(state, sid, caller)
        [helper] = _wait_batch(state, caller, _item("b.node"))
        bystander = _inbound(state, sid, node="d.node")

        state.cancel_active_tasks(sid, node_id="test.orchestrator")

        assert caller.status == TaskStatus.cancelled
        assert helper.status == TaskStatus.cancelled
        assert dispatched.status == TaskStatus.cancelled
        assert bystander.status == TaskStatus.pending

    def test_cancelling_from_inside_an_async_dispatch_spares_the_tasks_above_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        _await(state, caller, _handle(child))
        other = _inbound(state, sid)

        result = state.cancel_active_tasks(sid, exclude_task_id=child.task_id)

        assert result == {"ok": True, "cancelled_count": 1}
        assert caller.status == TaskStatus.suspended
        assert child.status == TaskStatus.pending
        assert other.status == TaskStatus.cancelled


class TestDispatcherRefusal:
    @staticmethod
    def _refusal(state: SupervisorState, sid: str, dispatcher_id: str) -> Any:
        payload = {"dispatch_origin": {
            "parent_session_id": sid, "caller_node_id": "test.orchestrator", "parent_conversation_key": CONV,
            "context_mode": "fresh", "caller_task_id": dispatcher_id,
        }}
        with state._lock:
            return async_dispatches.dispatcher_refusal_locked(state, payload)

    def test_a_dispatcher_still_at_work_may_dispatch(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)

        assert self._refusal(state, sid, caller.task_id) is None

    def test_a_dispatcher_that_ended_may_not(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        _finish(state, caller, "做完了")

        refusal = self._refusal(state, sid, caller.task_id)

        assert refusal is not None and refusal.status == 409
        assert "已经结束" in refusal.message and "finish" in refusal.message

    def test_a_dispatcher_asked_to_stop_may_not(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        caller.status = TaskStatus.running
        caller.worker_id = "worker-1"
        caller.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        state.cancel_single_task(caller.task_id)
        assert caller.status == TaskStatus.running

        refusal = self._refusal(state, sid, caller.task_id)

        assert refusal is not None and refusal.status == 409 and "要求停下" in refusal.message

    def test_a_dispatcher_no_longer_in_memory_and_ordinary_messages_are_not_judged(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)

        assert self._refusal(state, sid, "gone-task") is None
        with state._lock:
            assert async_dispatches.dispatcher_refusal_locked(state, {"text": "你好"}) is None


class TestWaitItemTimeout:
    def test_an_item_past_its_deadline_fails_and_the_rest_still_come_back(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        slow, quick = _wait_batch(
            state, caller, _item("a.node", timeout_sec=30, call_id="w1"), _item("b.node", call_id="w2"),
        )
        assert slow.input[async_dispatches.ITEM_TIMEOUT_SEC_KEY] == 30
        assert async_dispatches.ITEM_DEADLINE_KEY in slow.input
        assert async_dispatches.ITEM_TIMEOUT_SEC_KEY not in quick.input

        _expire(state, after=timedelta(seconds=10))
        assert slow.status == TaskStatus.pending
        _expire(state, after=timedelta(seconds=31))

        assert slow.status == TaskStatus.failed
        assert caller.status == TaskStatus.suspended
        _finish(state, quick, "quick result")
        assert caller.status == TaskStatus.pending
        timed_out, finished = _entries(caller)
        assert timed_out["status"] == "fail" and timed_out["timed_out"] is True
        assert timed_out["error"] == "等待超时：30 秒内没有做完，已被停下。"
        assert timed_out["call_id"] == "w1" and timed_out["continuable"] is True
        assert timed_out["handle"] == format_handle(slow.task_id, slow.input["child_session_id"])
        assert finished["status"] == "finish" and "timed_out" not in finished

    def test_a_tool_item_can_time_out_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [tool_task] = _wait_batch(
            state, caller, {"kind": "tool", "target": "slow_tool", "arguments": {"x": 1}, "call_id": "t1", "timeout_sec": 30},
        )

        _expire(state, after=timedelta(seconds=31))

        assert tool_task.status == TaskStatus.failed
        [entry] = _entries(caller)
        assert entry["kind"] == "tool" and entry["status"] == "fail" and entry["timed_out"] is True
        assert entry["name"] == "slow_tool"

    def test_an_item_that_finished_in_time_is_left_alone(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _wait_batch(state, caller, _item(timeout_sec=30))
        _finish(state, child, "in time")

        _expire(state, after=timedelta(seconds=31))

        assert child.status == TaskStatus.completed
        [entry] = _entries(caller)
        assert entry["status"] == "finish" and "timed_out" not in entry

    def test_a_finished_item_is_dropped_from_the_deadlines_at_the_next_sweep(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        done, running = _wait_batch(state, caller, _item(), _item("b.node"))
        _finish(state, done, "in time")

        _expire(state, after=timedelta(seconds=1))

        with state._lock:
            assert set(async_dispatches.registry_locked(state).item_deadlines) == {running.task_id}
        assert running.status == TaskStatus.pending

    def test_a_timeout_is_clamped_and_a_bad_one_falls_back_to_the_lifetime_cap(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _caller(state, sid)

        big, bad = _wait_batch(state, caller, _item(timeout_sec=999999), _item("b.node", timeout_sec="abc"))

        assert big.input[async_dispatches.ITEM_TIMEOUT_SEC_KEY] == 3600
        assert async_dispatches.ITEM_LIFETIME_CAPPED_KEY not in big.input
        assert async_dispatches.ITEM_TIMEOUT_SEC_KEY not in bad.input
        assert bad.input[async_dispatches.ITEM_LIFETIME_CAPPED_KEY] is True
        assert bad.input[async_dispatches.ITEM_DEADLINE_KEY] == bad.input[async_dispatches.ITEM_LIFETIME_CAP_KEY]
        with state._lock:
            assert set(async_dispatches.registry_locked(state).item_deadlines) == {big.task_id, bad.task_id}


class TestWaitLifetimeCap:
    """wait 批次的每一项最晚收在调用方总寿命前 10 分钟；再往下一层，每层再早 10 分钟。"""

    @pytest.fixture(autouse=True)
    def _short_lifetime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 运行上限 300 秒，总寿命 30 分钟：留出 10 分钟余量，调用方派出的项最晚第 20 分钟收
        monkeypatch.setenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "300")

    def test_an_item_without_a_timeout_is_still_collected_before_the_caller_runs_out(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        quiet, patient = _wait_batch(
            state, caller, _item(call_id="w1"), _item("b.node", call_id="w2", timeout_sec=3600),
        )

        cap = caller.created_at + timedelta(minutes=20)
        assert datetime.fromisoformat(quiet.input[async_dispatches.ITEM_DEADLINE_KEY]) == cap
        assert datetime.fromisoformat(patient.input[async_dispatches.ITEM_DEADLINE_KEY]) == cap
        assert patient.input[async_dispatches.ITEM_TIMEOUT_SEC_KEY] == 3600
        assert quiet.input[async_dispatches.ITEM_LIFETIME_CAPPED_KEY] is True
        assert patient.input[async_dispatches.ITEM_LIFETIME_CAPPED_KEY] is True

        _expire(state, after=timedelta(minutes=19))
        assert caller.status == TaskStatus.suspended
        _expire(state, after=timedelta(minutes=21))

        assert [quiet.status, patient.status] == [TaskStatus.failed, TaskStatus.failed]
        assert caller.status == TaskStatus.pending
        first, second = _entries(caller)
        assert first["timed_out"] is True and first["lifetime_capped"] is True
        assert "总运行时长快到上限" in first["error"] and "不是这一项自己写的 timeout_sec" in first["error"]
        assert second["lifetime_capped"] is True and first["continuable"] is True

    def test_an_explicit_timeout_inside_the_cap_is_kept(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))

        [child] = _wait_batch(state, caller, _item(timeout_sec=60))

        assert async_dispatches.ITEM_LIFETIME_CAPPED_KEY not in child.input
        cap = datetime.fromisoformat(child.input[async_dispatches.ITEM_LIFETIME_CAP_KEY])
        assert cap == caller.created_at + timedelta(minutes=20)
        _expire(state, after=timedelta(seconds=61))
        [entry] = _entries(caller)
        assert entry["error"] == "等待超时：60 秒内没有做完，已被停下。" and "lifetime_capped" not in entry

    def test_a_continued_task_counts_from_where_its_chain_started(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        chain_started = caller.created_at - timedelta(minutes=5)
        caller.input[AUTO_CONTINUE_KEY] = {CHAIN_STARTED_AT_KEY: chain_started.isoformat()}

        [child] = _wait_batch(state, caller, _item())

        assert datetime.fromisoformat(child.input[async_dispatches.ITEM_DEADLINE_KEY]) == (
            chain_started + timedelta(minutes=20)
        )

    def test_a_nested_wait_ends_one_margin_before_the_level_above(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [middle] = _wait_batch(state, top, _item("mid.node"))

        [leaf] = _wait_batch(state, middle, _item("leaf.node"))

        top_cap = top.created_at + timedelta(minutes=20)
        assert datetime.fromisoformat(middle.input[async_dispatches.ITEM_LIFETIME_CAP_KEY]) == top_cap
        assert datetime.fromisoformat(leaf.input[async_dispatches.ITEM_LIFETIME_CAP_KEY]) == top_cap - timedelta(
            minutes=10,
        )
        assert datetime.fromisoformat(leaf.input[async_dispatches.ITEM_DEADLINE_KEY]) == top_cap - timedelta(
            minutes=10,
        )

    def test_an_unreadable_inherited_cap_falls_back_to_the_tasks_own_lifetime(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [middle] = _wait_batch(state, top, _item("mid.node"))
        middle.input[async_dispatches.ITEM_LIFETIME_CAP_KEY] = "not a time"

        [leaf] = _wait_batch(state, middle, _item("leaf.node"))

        assert datetime.fromisoformat(leaf.input[async_dispatches.ITEM_DEADLINE_KEY]) == (
            middle.created_at + timedelta(minutes=20)
        )

    def test_a_wait_dispatch_past_the_cap_is_refused_item_by_item(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        caller.created_at = datetime.now(timezone.utc) - timedelta(minutes=21)

        node_child, tool_child = _wait_batch(
            state, caller, _item(call_id="w1"),
            {"kind": "tool", "target": "slow_tool", "arguments": {"x": 1}, "call_id": "t1"},
        )

        assert [node_child.status, tool_child.status] == [TaskStatus.failed, TaskStatus.failed]
        assert caller.status == TaskStatus.pending
        node_entry, tool_entry = _entries(caller)
        assert node_entry["status"] == "fail" and node_entry["error"] == async_dispatches.WAIT_CAP_PASSED
        assert node_entry["call_id"] == "w1"
        assert tool_entry["kind"] == "tool" and tool_entry["name"] == "slow_tool"
        assert tool_entry["status"] == "fail" and tool_entry["error"] == async_dispatches.WAIT_CAP_PASSED

    def test_an_await_inside_a_wait_item_ends_by_the_cap_it_inherited(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        top = _caller(state, sid)
        [middle] = _wait_batch(state, top, _item("mid.node"))
        child = _async_child(state, sid, middle, node="c.node", caller_node="mid.node")

        _await(state, middle, _handle(child), timeout_sec=3600)

        deadline = datetime.fromisoformat(middle.input[AWAITING_INPUT_KEY]["deadline"])
        assert deadline == top.created_at + timedelta(minutes=10)


class TestForkSource:
    def test_an_async_fork_from_a_branch_sees_what_the_branch_has_not_merged_yet(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        branch = caller.session_id
        store = ConversationStore(tmp_path / "data" / "conversations")
        store.append_batch(branch, [Message(id="branch-only", role="user", content="这一轮刚写下、还没合并的内容")])
        assert "branch-only" not in [message.id for message in store.load(sid)]
        agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:a.node:{CONV}:{uuid.uuid4()}")

        _inbound(
            state, agent_sid, node="a.node", dispatch_context_mode="fork",
            dispatch_origin=_origin(sid, caller, mode="fork"), dispatch_fork_from_session=branch,
        )

        copied = ConversationStore(tmp_path / "data" / "conversations").load(agent_sid)
        assert "这一轮刚写下、还没合并的内容" in [message.content for message in copied]


@pytest.fixture()
def admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _call(app: Any, method: str, path: str, *, json: Any = None, auth: bool = True) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, json=json, headers=_AUTH if auth else {})

    return asyncio.run(_run())


@pytest.mark.usefixtures("admin_token")
class TestEndpoints:
    def test_the_inbound_answer_names_the_task_it_created(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json={
            "channel": "qq", "conversation_key": CONV, "text": "你好", "platform_auth": AUTH,
        })

        assert response.status_code == 200, response.text
        body = response.json()
        assert state.tasks[body["task_id"]].source_inbound_seq == body["inbound_seq"]
        assert body["session_id"] == state.conversation_map[CONV]

    def test_an_inbound_that_creates_no_task_says_so(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json={"conversation_key": CONV, "text": "   "})

        assert response.status_code == 200
        assert response.json()["task_id"] == ""

    def test_the_list_endpoint(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        app = _app(state, tmp_path)

        listed = _call(app, "GET", f"/v1/tasks/{caller.task_id}/dispatches")

        assert listed.status_code == 200, listed.text
        assert [item["handle"] for item in listed.json()["active"]] == [_handle(child)]
        assert _call(app, "GET", f"/v1/tasks/{caller.task_id}/dispatches", auth=False).status_code == 401
        assert _call(app, "GET", "/v1/tasks/no-such-task/dispatches").status_code == 404

    def test_the_cancel_endpoint(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        child = _async_child(state, sid, caller)
        stranger = _inbound(state, _session(state, OTHER_CONV))
        app = _app(state, tmp_path)
        path = f"/v1/tasks/{caller.task_id}/dispatches/cancel"

        assert _call(app, "POST", path, json={"handle": _handle(child)}, auth=False).status_code == 401
        assert _call(app, "POST", path, json={"handle": ""}).status_code == 422
        assert _call(app, "POST", path, json={"handle": "x" * 400}).status_code == 422
        assert _call(app, "POST", "/v1/tasks/no-such-task/dispatches/cancel", json={"handle": _handle(child)}).status_code == 404
        refused = _call(app, "POST", f"/v1/tasks/{stranger.task_id}/dispatches/cancel", json={"handle": _handle(child)})
        assert refused.status_code == 200 and refused.json()["ok"] is False
        assert child.status == TaskStatus.pending

        cancelled = _call(app, "POST", path, json={"handle": _handle(child)})

        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert child.status == TaskStatus.cancelled


@pytest.mark.usefixtures("admin_token")
class TestForkCopy:
    """异步 fork 的历史在锁外复制、进锁再装；复制不了就拒收或判失败，不让子任务从空历史开始。"""

    @staticmethod
    def _payload(sid: str, caller: Task, source: str, **extra: Any) -> dict[str, Any]:
        return {
            "channel": "qq", "conversation_key": f"agent:a.node:{CONV}:{uuid.uuid4()}", "text": "接着查",
            "entry_node_id": "a.node", "platform_auth": AUTH, "dispatch_context_mode": "fork",
            "dispatch_origin": _origin(sid, caller, mode="fork"), "dispatch_fork_from_session": source, **extra,
        }

    @staticmethod
    def _staged(workspace: Path) -> list[Path]:
        return sorted((workspace / "data" / "conversations").glob(".fork.*.tmp"))

    @staticmethod
    def _history(workspace: Path, session_id: str) -> list[str]:
        return [message.id for message in ConversationStore(workspace / "data" / "conversations").load(session_id)]

    def test_the_child_starts_from_the_callers_history(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        ConversationStore(tmp_path / "data" / "conversations").append_batch(caller.session_id, [
            Message(id="persona", role="system", content="调用方的 system prompt"),
            Message(id="ask", role="user", content="帮我查一下"),
        ])

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json=self._payload(sid, caller, caller.session_id))

        assert response.status_code == 200, response.text
        body = response.json()
        assert self._history(tmp_path, body["session_id"]) == ["ask"]
        assert state.tasks[body["task_id"]].status == TaskStatus.pending
        assert self._staged(tmp_path) == []

    def test_an_unknown_source_is_refused_before_anything_is_created(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        tasks_before = set(state.tasks)
        payload = self._payload(sid, caller, "no-such-session")

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json=payload)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail.startswith("没有派出：") and "no-such-session" in detail
        assert set(state.tasks) == tasks_before
        assert payload["conversation_key"] not in state.conversation_map
        assert self._staged(tmp_path) == []

    def test_a_source_that_is_not_a_session_id_cannot_reach_outside_the_conversations(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        assert (tmp_path / "data" / "events.jsonl").is_file()

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json=self._payload(sid, caller, "../events"))

        assert response.status_code == 422
        assert self._staged(tmp_path) == []

    def test_a_copy_that_fails_is_refused_with_only_the_error_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        ConversationStore(tmp_path / "data" / "conversations").append_batch(
            caller.session_id, [Message(id="ask", role="user", content="帮我查一下")],
        )
        tasks_before = set(state.tasks)

        def broken(self: ConversationStore, source_session_id: str) -> Any:
            raise OSError("disk full at /secret/path")

        monkeypatch.setattr(ConversationStore, "stage_fork", broken)

        response = _call(_app(state, tmp_path), "POST", "/v1/inbound", json=self._payload(sid, caller, caller.session_id))

        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "OSError" in detail and "稍后重试" in detail and "/secret/path" not in detail
        assert set(state.tasks) == tasks_before

    def test_a_refused_dispatch_copies_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        staged_sources: list[str] = []
        original = ConversationStore.stage_fork

        def recording(self: ConversationStore, source_session_id: str) -> Any:
            staged_sources.append(source_session_id)
            return original(self, source_session_id)

        monkeypatch.setattr(ConversationStore, "stage_fork", recording)

        response = _call(
            _app(state, tmp_path), "POST", "/v1/inbound",
            json=self._payload(sid, caller, caller.session_id, dispatch_context_mode="sideways"),
        )

        assert response.status_code == 422
        assert staged_sources == []

    def test_a_fork_created_in_process_that_cannot_copy_fails_its_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        caller = _inbound(state, sid)
        _finish(state, caller, "派出去了")
        agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:a.node:{CONV}:{uuid.uuid4()}")

        child = _inbound(
            state, agent_sid, node="a.node", dispatch_context_mode="fork",
            dispatch_origin=_origin(sid, caller, mode="fork"), dispatch_fork_from_session="no-such-session",
        )

        assert child.status == TaskStatus.failed
        assert child.result["error"].startswith("没能开始：fork 的来源会话 no-such-session 不存在")
        # 判失败后的路由要合并入口分支，在另起的线程里跑
        deadline = time.monotonic() + 30
        while not _callbacks(state) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [task.input["inbound_child_status"] for task in _callbacks(state)] == ["fail"]
