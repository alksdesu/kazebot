"""沿用会话的委派（accumulate、continue_from）：会话此刻有任务在用、或是发起方委派链上某一级在写的会话都不派；
context_mode 写错的 engine 当场拒，supervisor 收到也照样拒。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    ScriptedRegistry,
    answered_once,
    call,
    echo,
    make_loop_state,
    make_rctx,
    results_for,
    turn,
    use_hooks,
)
from engine.dispatch_batch import (  # noqa: E402
    CONTEXT_MODES,
    INBOUND_BAD_MODE_STATUS,
    INBOUND_CONFLICT_STATUS,
    context_mode_refusal,
    parse_context_mode,
)
from engine.dispatch_handle import format_handle  # noqa: E402
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.pseudo_handlers import prepare_wait_dispatches  # noqa: E402
from engine.inference.pseudo_tools import _dispatch_delegate_spec  # noqa: E402
from supervisor import admin_api, child_retention, continuation, session_reuse  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

SCOUT = "scout"
HELPER = "helper"
HANDLE = "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0@child_9c2f"
KEPT_CHILD = {
    "ok": True, "session_id": "child_9c2f", "task_id": "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0",
    "kind": "child", "node_id": SCOUT, "context_mode": "accumulate",
}
BAD_MODES = [
    pytest.param("Fresh", id="wrong-case"),
    pytest.param("reuse", id="unknown-word"),
    pytest.param(True, id="boolean"),
    pytest.param(3, id="number"),
]

CONV = "qq_group:1001"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
_TOKEN = "session-reuse-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


# ---- engine 一侧 ----


def _loop(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    use_hooks(monkeypatch)
    ls = make_loop_state(workspace, ScriptedRegistry(tools={"probe": echo()}), rctx=make_rctx(workspace))
    ls.allowed_dispatch_targets = {SCOUT, HELPER}
    return ls


def _run(ls: Any, *tool_calls: Any) -> Any:
    return asyncio.run(_handle_tool_calls(ls, turn(*tool_calls), 1, final_step=False))


def _wait(target: str = SCOUT, *, call_id: str = "w1", **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": "查一下", "wait": True, **extra}, call_id=call_id)


def _async(target: str = SCOUT, *, call_id: str = "a1", **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": "查一下", **extra}, call_id=call_id)


def _payload(ls: Any, call_id: str) -> dict[str, Any]:
    [message] = results_for(ls.messages, call_id)
    return json.loads(message["content"])


def _response(body: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=json.dumps(body, ensure_ascii=False), json=lambda: body)


def _supervisor(answer: SimpleNamespace | None = None, continued: dict[str, Any] | None = None) -> SimpleNamespace:
    """顶替 rctx.http：GET 是 continue_from 核对，POST 是异步委派的入站。"""
    accepted = _response({"session_id": "agent-session-1", "task_id": "child-1", "accepted": True})
    return SimpleNamespace(
        get=AsyncMock(return_value=_response(continued or {})),
        post=AsyncMock(return_value=answer or accepted),
    )


class TestContextModeValues:
    @pytest.mark.parametrize("raw, expected", [
        (None, ""), ("", ""), ("   ", ""),
        ("fresh", "fresh"), (" fork ", "fork"), ("accumulate", "accumulate"),
        ("Fresh", None), ("ACCUMULATE", None), ("reuse", None), (True, None), (1, None), (["fresh"], None),
    ])
    def test_only_the_three_lowercase_modes_count(self, raw: Any, expected: str | None) -> None:
        assert parse_context_mode(raw) == expected

    def test_the_refusal_names_the_modes_and_what_was_sent(self) -> None:
        assert context_mode_refusal("Fresh") == (
            '没有派出：context_mode 只能是 fresh、fork、accumulate 之一，收到的是 "Fresh"；'
            "不填就按目标节点是否持久自动选（持久节点 accumulate，其余 fresh）。"
        )

    def test_a_long_value_is_cut(self) -> None:
        assert f'收到的是 "{"x" * 39}...<truncated>；' in context_mode_refusal("x" * 200)

    def test_the_tool_offers_exactly_these_modes_and_says_when_reusing_is_refused(self) -> None:
        mode = _dispatch_delegate_spec(SCOUT)["function"]["parameters"]["properties"]["context_mode"]

        assert mode["enum"] == list(CONTEXT_MODES) == ["fresh", "fork", "accumulate"]
        assert "此刻还有任务在跑、或是派你出来的上级在写的，委派会被拒" in mode["description"]


class TestEngineRefusesBadModes:
    @pytest.mark.parametrize("mode", BAD_MODES)
    def test_a_wait_dispatch_with_a_bad_mode_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Any,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)

        plan = asyncio.run(prepare_wait_dispatches(ls, [_wait(context_mode=mode)]))

        assert plan.items == []
        [(_refused, text)] = plan.rejected
        assert json.loads(text)["error"] == context_mode_refusal(mode)

    @pytest.mark.parametrize("mode", BAD_MODES)
    def test_an_async_dispatch_with_a_bad_mode_is_refused_before_anything_is_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Any,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor()

        _run(ls, _async(context_mode=mode))

        assert ls.rctx.http.post.await_count == 0
        assert _payload(ls, "a1") == {"success": False, "error": context_mode_refusal(mode)}

    def test_a_refused_item_leaves_the_rest_of_the_round_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait(call_id="w1", context_mode="Fork"), _wait(HELPER, call_id="w2", context_mode="fork"))

        assert [(item["call_id"], item["context_mode"]) for item in action.dispatch_batch] == [("w2", "fork")]
        assert _payload(ls, "w1") == {"success": False, "error": context_mode_refusal("Fork")}
        assert answered_once(ls.messages)

    def test_spaces_around_a_mode_are_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch)

        [item] = _run(ls, _wait(context_mode="  accumulate ")).dispatch_batch

        assert item["context_mode"] == "accumulate"

    @pytest.mark.parametrize("raw", [None, "", "  "])
    def test_an_unset_mode_still_follows_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)

        [item] = _run(ls, _wait(context_mode=raw)).dispatch_batch

        assert item["context_mode"] == "fresh"

    def test_with_continue_from_the_mode_is_not_used_so_a_bad_one_does_not_matter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(continued=KEPT_CHILD)

        [item] = _run(ls, _wait(continue_from=HANDLE, context_mode="Fresh")).dispatch_batch

        assert item["continue_from"] == HANDLE and item["context_mode"] == "accumulate"


class TestEngineShowsTheSupervisorsReason:
    @pytest.mark.parametrize("status", [INBOUND_CONFLICT_STATUS, INBOUND_BAD_MODE_STATUS])
    def test_a_refused_inbound_hands_its_reason_to_the_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int,
    ) -> None:
        reason = session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, target=SCOUT)
        ls = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(_response({"detail": reason}, status))

        _run(ls, _async(context_mode="accumulate"))

        assert _payload(ls, "a1") == {"success": False, "error": reason}

    def test_other_failures_hand_over_what_the_supervisor_said(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(_response({"detail": "boom"}, 500))

        _run(ls, _async())

        assert _payload(ls, "a1") == {"success": False, "error": "boom"}

    def test_a_validation_error_list_is_spelled_out_by_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = {"detail": [{"loc": ["body", "text"], "msg": "Field required"}]}
        ls = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(_response(body, INBOUND_BAD_MODE_STATUS))

        _run(ls, _async())

        assert _payload(ls, "a1") == {
            "success": False, "error": f"text：Field required（HTTP {INBOUND_BAD_MODE_STATUS}）",
        }


# ---- supervisor 一侧 ----


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-session-reuse"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=key)


def _caller(state: SupervisorState, session_id: str) -> Task:
    """不在入口分支上的调用方：路由当场跑完，不另起线程。"""
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": CONV, "channel": "qq", "platform_auth": dict(AUTH)}},
            continuation={},
            source_inbound_seq=None,
        )


def _inbound(state: SupervisorState, session_id: str) -> Task:
    """用户发来一条消息：入口任务跑在这个会话的入口分支上。"""
    payload = {
        "channel": "qq", "conversation_key": state.sessions[session_id].conversation_key,
        "text": "帮我查查", "entry_node_id": "test.orchestrator", "platform_auth": dict(AUTH),
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    task = state.record_inbound_message_event(event)
    assert task is not None
    return task


def _item(target: str, mode: str = "accumulate", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": mode, **extra}


def _dispatch(state: SupervisorState, caller: Task, *items: Any) -> list[Task]:
    """caller 这一程交回 wait 批次；返回这一批新建的子任务。"""
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


def _finish(state: SupervisorState, task: Task, text: str = "done") -> None:
    _report(state, task, {"action": "finish", "result": {"text": text}})


def _fail(state: SupervisorState, task: Task, error: str = "network down") -> None:
    _report(state, task, {"action": "fail", "error": error})


def _entries(task: Task) -> list[dict[str, Any]]:
    return task.input["resume_data"]["entries"]


def _in_session(state: SupervisorState, child_session_id: str) -> list[str]:
    return [task.task_id for task in state.tasks.values() if task.input.get("child_session_id") == child_session_id]


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _idle(state: SupervisorState, session_id: str) -> bool:
    """会话上的任务都结束了、入口分支也合并回去了：之后才能沿用它。"""
    def settled() -> bool:
        with state._lock:
            return not child_retention.session_busy_locked(state, session_id)

    return _wait_until(settled)


class TestWaitBatch:
    def test_a_cycle_does_not_land_in_the_session_its_ancestor_is_waiting_in(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [first] = _dispatch(state, top, _item("a.node"))
        [middle] = _dispatch(state, first, _item("b.node"))

        [again] = _dispatch(state, middle, _item("a.node", call_id="w1"))

        assert again.status == TaskStatus.failed and "child_session_id" not in again.input
        assert again.result["error"] == session_reuse.refusal_message(session_reuse.CONFLICT_LINEAGE, target="a.node")
        [entry] = _entries(middle)
        assert entry["status"] == "fail" and entry["call_id"] == "w1"
        assert entry["error"] == again.result["error"]
        assert middle.status == TaskStatus.pending
        assert first.status == TaskStatus.suspended
        assert _in_session(state, first.input["child_session_id"]) == [first.task_id]

    def test_with_fresh_the_cycle_gets_a_session_of_its_own(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [first] = _dispatch(state, top, _item("a.node"))
        [middle] = _dispatch(state, first, _item("b.node"))

        [again] = _dispatch(state, middle, _item("a.node", mode="fresh"))

        assert again.status == TaskStatus.pending
        assert again.input["child_session_id"] not in (
            first.input["child_session_id"], middle.input["child_session_id"],
        )

    def test_a_sub_agent_cannot_join_a_session_another_level_is_running(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        helper, scout = _dispatch(state, top, _item("helper.node", mode="fresh"), _item("scout.node"))

        [nested] = _dispatch(state, helper, _item("scout.node", call_id="w1"))

        assert nested.status == TaskStatus.failed
        [entry] = _entries(helper)
        assert entry["error"] == session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, target="scout.node")
        assert scout.status == TaskStatus.pending
        assert _in_session(state, scout.input["child_session_id"]) == [scout.task_id]

    def test_once_the_other_one_is_done_the_session_is_shared_as_before(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        helper, scout = _dispatch(state, top, _item("helper.node", mode="fresh"), _item("scout.node"))
        _finish(state, scout)

        [nested] = _dispatch(state, helper, _item("scout.node"))

        assert nested.status == TaskStatus.pending
        assert nested.input["child_session_id"] == scout.input["child_session_id"]

    def test_two_items_of_one_batch_cannot_share_a_session(self, tmp_path: Path) -> None:
        # engine 同一轮就会拦；supervisor 不靠它，自己照样拦
        state = _state(tmp_path)
        top = _caller(state, _session(state))

        first, second = _dispatch(state, top, _item("scout.node", call_id="w1"), _item("scout.node", call_id="w2"))

        assert first.status == TaskStatus.pending
        assert second.status == TaskStatus.failed
        assert second.result["error"] == session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, target="scout.node")
        assert top.status == TaskStatus.suspended

    def test_a_different_context_key_is_a_different_session(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))

        first, second = _dispatch(
            state, top, _item("scout.node", context_key="docs"), _item("scout.node", context_key="code"),
        )

        assert first.status == second.status == TaskStatus.pending
        assert first.input["child_session_id"] != second.input["child_session_id"]

    def test_the_caller_picks_its_session_up_again_after_the_child_is_done(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [scout] = _dispatch(state, top, _item("scout.node"))
        _finish(state, scout)
        assert top.status == TaskStatus.pending

        [again] = _dispatch(state, top, _item("scout.node"))

        assert again.status == TaskStatus.pending
        assert again.input["child_session_id"] == scout.input["child_session_id"]

    @pytest.mark.parametrize("mode", ["Fresh", "reuse", True])
    def test_a_bad_mode_fails_its_item_and_the_rest_go_out(self, tmp_path: Path, mode: Any) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))

        bad, good = _dispatch(
            state, top, _item("a.node", mode=mode, call_id="w1"), _item("b.node", mode="fresh", call_id="w2"),
        )

        assert bad.status == TaskStatus.failed and "child_session_id" not in bad.input
        assert bad.result["error"] == context_mode_refusal(mode)
        assert good.status == TaskStatus.pending
        assert top.status == TaskStatus.suspended

    def test_continue_from_cannot_pick_up_the_session_of_an_ancestor(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [first] = _dispatch(state, top, _item("a.node"))
        [middle] = _dispatch(state, first, _item("b.node"))
        handle = format_handle(first.task_id, first.input["child_session_id"])

        refused = continuation.resolve(state, middle.task_id, handle, "a.node", wait=True)
        [again] = _dispatch(state, middle, _item("a.node", continue_from=handle))

        assert refused["ok"] is False and refused["reason"] == session_reuse.CONFLICT_LINEAGE
        assert refused["message"] == session_reuse.refusal_message(session_reuse.CONFLICT_LINEAGE, continuing=True)
        assert again.status == TaskStatus.failed and again.result["error"] == refused["message"]


class TestSingleDispatch:
    @staticmethod
    def _single(state: SupervisorState, caller: Task, target: str, **dispatch_input: Any) -> None:
        _report(state, caller, {
            "action": "dispatch", "target_node": target,
            "dispatch_input": {"instruction": f"do {target}", **dispatch_input},
        })

    def test_a_busy_session_fails_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        [scout] = _dispatch(state, _caller(state, sid), _item("scout.node"))
        other = _caller(state, sid)

        self._single(state, other, "scout.node", context_mode="accumulate")

        assert other.status == TaskStatus.failed
        assert other.result["error"] == session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, target="scout.node")
        assert _in_session(state, scout.input["child_session_id"]) == [scout.task_id]

    def test_a_bad_mode_fails_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))

        self._single(state, caller, "scout.node", context_mode="Accumulate")

        assert caller.status == TaskStatus.failed
        assert caller.result["error"] == context_mode_refusal("Accumulate")
        assert not [task for task in state.tasks.values() if task.caller_task_id == caller.task_id]

    def test_an_idle_session_is_reused(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        [scout] = _dispatch(state, _caller(state, sid), _item("scout.node"))
        _finish(state, scout)
        other = _caller(state, sid)

        self._single(state, other, "scout.node", context_mode="accumulate")

        [child] = [task for task in state.tasks.values() if task.caller_task_id == other.task_id]
        assert child.input["child_session_id"] == scout.input["child_session_id"]
        assert other.status == TaskStatus.suspended


class TestChildSessionLookup:
    def test_an_unknown_mode_is_not_read_as_accumulate(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        parent = _session(state)
        with state._lock:
            existing, _new = state.get_or_create_child_session(parent, "scout.node", "", "accumulate")

            with pytest.raises(ValueError):
                state.get_or_create_child_session(parent, "scout.node", "", "Accumulate")
            assert state._reusable_child_session_locked(parent, "scout.node", "") == existing

    def test_looking_up_creates_nothing(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        parent = _session(state)

        with state._lock:
            assert state._reusable_child_session_locked(parent, "scout.node", "") == ""
        assert state.child_session_map == {}


# ---- 异步委派的入站 ----


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


def _post(app: Any, payload: dict[str, Any]) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/v1/inbound", json=payload, headers=_AUTH)

    return asyncio.run(_run())


def _agent_session(task: Task) -> str:
    """异步委派出去的任务在哪个 agent: 会话里跑。"""
    return str(task.input["parent_session_id"])


def _dispatch_payload(
    state: SupervisorState, caller: Task, target: str, *, mode: str = "accumulate", **origin: Any,
) -> dict[str, Any]:
    """和 _handle_pseudo_dispatch 发给 /v1/inbound 的一样。"""
    if mode == "accumulate":
        key = f"agent:{target}:{CONV}"
    elif origin.get("continue_from"):
        key = state.sessions[str(origin["continue_from"]).split("@", 1)[1]].conversation_key
    else:
        key = f"agent:{target}:{CONV}:{uuid.uuid4()}"
    with state._lock:
        parent = state._route_session_id_for_task_locked(caller)
    return {
        "channel": "qq",
        "conversation_key": key,
        "text": f"do {target}",
        "entry_node_id": target,
        "use_context": True,
        "platform_auth": dict(AUTH),
        "dispatch_origin": {
            "parent_session_id": parent, "caller_node_id": str(caller.node_id or ""), "parent_conversation_key": CONV,
            "context_mode": mode, "caller_task_id": caller.task_id, **origin,
        },
        "dispatch_context_mode": mode,
    }


def _accepted(state: SupervisorState, response: httpx.Response) -> Task:
    assert response.status_code == 200, response.text
    return state.tasks[response.json()["task_id"]]


def _inbound_events(state: SupervisorState) -> int:
    return sum(1 for event in state.eventlog.iter_persisted_events() if event.get("type") == "inbound_message")


@pytest.mark.usefixtures("admin_token")
class TestLineage:
    def test_it_follows_sync_and_async_dispatches_up_to_the_user(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        sid = _session(state)
        top = _inbound(state, sid)
        agent = _accepted(state, _post(app, _dispatch_payload(state, top, "y.node")))
        [sub] = _dispatch(state, agent, _item("a.node"))

        with state._lock:
            lineage = session_reuse.lineage_sessions_locked(state, sub)

        assert lineage == {sub.input["child_session_id"], _agent_session(agent), sid}

    def test_a_pruned_caller_record_still_leaves_the_session_it_dispatched_from(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        agent = _accepted(state, _post(app, _dispatch_payload(state, top, "y.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, agent, "helper.node", mode="fresh")))

        with state._lock:
            state.tasks.pop(agent.task_id)
            lineage = session_reuse.lineage_sessions_locked(state, helper)

        assert lineage == {_agent_session(helper), _agent_session(agent)}


@pytest.mark.usefixtures("admin_token")
class TestAsyncInbound:
    def test_a_nested_dispatch_to_a_busy_agent_is_refused(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        scout = _accepted(state, _post(app, _dispatch_payload(state, top, "scout.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, top, "helper.node", mode="fresh")))
        events = _inbound_events(state)

        response = _post(app, _dispatch_payload(state, helper, "scout.node"))

        assert response.status_code == INBOUND_CONFLICT_STATUS
        assert response.json()["detail"] == session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, target="scout.node")
        assert _inbound_events(state) == events
        assert [task.task_id for task in state.tasks.values() if task.input.get("parent_session_id") == _agent_session(scout)] == [
            scout.task_id,
        ]

    def test_a_second_dispatch_while_the_first_runs_is_refused(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        _accepted(state, _post(app, _dispatch_payload(state, top, "scout.node")))

        response = _post(app, _dispatch_payload(state, top, "scout.node"))

        assert response.status_code == INBOUND_CONFLICT_STATUS

    def test_once_it_is_idle_the_agent_takes_the_next_dispatch(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        scout = _accepted(state, _post(app, _dispatch_payload(state, top, "scout.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, top, "helper.node", mode="fresh")))
        _finish(state, scout)
        assert _idle(state, _agent_session(scout))

        again = _accepted(state, _post(app, _dispatch_payload(state, helper, "scout.node")))

        assert _agent_session(again) == _agent_session(scout)

    def test_a_descendant_cannot_dispatch_into_the_agent_session_it_came_from(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        agent = _accepted(state, _post(app, _dispatch_payload(state, top, "y.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, agent, "helper.node", mode="fresh")))
        # 发起它的那一程已经做完、会话也空着，这一串的结果仍要交回那里
        _finish(state, agent)
        assert _idle(state, _agent_session(agent))

        response = _post(app, _dispatch_payload(state, helper, "y.node"))

        assert response.status_code == INBOUND_CONFLICT_STATUS
        assert response.json()["detail"] == session_reuse.refusal_message(session_reuse.CONFLICT_LINEAGE, target="y.node")

    def test_continue_from_is_refused_there_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        agent = _accepted(state, _post(app, _dispatch_payload(state, top, "y.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, agent, "helper.node", mode="fresh")))
        _finish(state, agent)
        handle = format_handle(agent.task_id, _agent_session(agent))

        refused = continuation.resolve(state, helper.task_id, handle, "y.node", wait=False)

        assert refused["reason"] == session_reuse.CONFLICT_LINEAGE

    def test_a_continuation_that_became_busy_is_refused_in_its_own_words(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        helper = _accepted(state, _post(app, _dispatch_payload(state, top, "helper.node", mode="fresh")))
        _fail(state, helper)
        assert _idle(state, _agent_session(helper))
        handle = format_handle(helper.task_id, _agent_session(helper))
        assert continuation.resolve(state, top.task_id, handle, "helper.node", wait=False)["ok"] is True
        resumed = _post(app, _dispatch_payload(state, top, "helper.node", mode="fresh", continue_from=handle))
        assert resumed.status_code == 200, resumed.text

        response = _post(app, _dispatch_payload(state, top, "helper.node", mode="fresh", continue_from=handle))

        assert response.status_code == INBOUND_CONFLICT_STATUS
        assert response.json()["detail"] == session_reuse.refusal_message(session_reuse.CONFLICT_BUSY, continuing=True)

    @pytest.mark.parametrize("field", ["dispatch_context_mode", "dispatch_origin.context_mode"])
    def test_a_bad_mode_is_refused_and_nothing_is_created(self, tmp_path: Path, field: str) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        payload = _dispatch_payload(state, top, "scout.node", mode="fresh")
        if field == "dispatch_context_mode":
            payload["dispatch_context_mode"] = "Fresh"
        else:
            payload["dispatch_origin"]["context_mode"] = "Fresh"

        response = _post(app, payload)

        assert response.status_code == INBOUND_BAD_MODE_STATUS
        assert response.json()["detail"] == context_mode_refusal("Fresh")
        assert payload["conversation_key"] not in state.conversation_map

    def test_a_user_message_to_a_busy_session_still_goes_in(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        sid = _session(state)
        _inbound(state, sid)

        response = _post(app, {"channel": "qq", "conversation_key": CONV, "text": "还有一件事", "platform_auth": dict(AUTH)})

        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == sid and response.json()["task_id"]

    def test_results_still_reach_an_agent_session_that_is_busy(self, tmp_path: Path) -> None:
        # 结果回送走内部的路，不经过入站这道核对
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        top = _inbound(state, _session(state))
        agent = _accepted(state, _post(app, _dispatch_payload(state, top, "y.node")))
        helper = _accepted(state, _post(app, _dispatch_payload(state, agent, "helper.node", mode="fresh")))

        _finish(state, helper, "查到了")

        [callback] = [
            task for task in state.tasks.values()
            if task.input.get("inbound_message_type") == "dispatch_result"
            and task.input.get("inbound_child_task_id") == helper.task_id
        ]
        assert _agent_session(callback) == _agent_session(agent)
        assert agent.status == TaskStatus.pending
