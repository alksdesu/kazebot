"""异步工具的结果交给发起它的节点：收得到新消息的就交给它自己，同步委派的子节点交给这条链最上面的任务，
用完即删的异步子 agent 交给派它出来的一方并附上说明，accumulate 的子 agent 在自己的会话里接着做。

转到后台的工具按发起任务问该不该停：挂起恢复、重排、正常结束都不算，被取消、失败、会话被取消或重置才算。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
from engine.dispatch_batch import CONTINUE_FROM_KEY  # noqa: E402
from engine.tool_outputs import ToolOutputStore  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.async_tools import detached_cancelled_locked  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

CONV = "qq_group:5005"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
_TOKEN = "async-tools-token"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_NOTE = "[Note] This tool call was made by sub-agent"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="async-tool-routing"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _app(state: SupervisorState, tmp_path: Path) -> Any:
    return create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=key)


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


def _caller(state: SupervisorState, session_id: str) -> Task:
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


def _item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _dispatch(state: SupervisorState, caller: Task, *items: Any) -> list[Task]:
    """caller 这一程交回 wait 批次，自己挂起；返回这一批新派出的子任务。"""
    before = {task.task_id for task in state.tasks.values() if task.caller_task_id == caller.task_id}
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", "dispatch_batch": list(items)})
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id and task.task_id not in before),
        key=lambda task: task.batch_index,
    )


def _origin(session_id: str, caller: Task, mode: str, **extra: Any) -> dict[str, Any]:
    return {
        "parent_session_id": session_id, "caller_node_id": "test.orchestrator", "parent_conversation_key": CONV,
        "context_mode": mode, "caller_task_id": caller.task_id, **extra,
    }


def _async_child(
    state: SupervisorState, session_id: str, caller: Task, *, mode: str = "fresh", **origin: Any,
) -> tuple[str, Task]:
    """照 engine 异步委派那样投一条带 dispatch_origin 的入站，子节点在自己的 agent: 会话里跑。"""
    agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:a.node:{CONV}:{uuid.uuid4()}")
    child = _inbound(
        state, agent_sid, node="a.node", dispatch_context_mode=mode,
        dispatch_origin=_origin(session_id, caller, mode, **origin),
    )
    return agent_sid, child


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _finish(state: SupervisorState, task: Task) -> None:
    _report(state, task, {"action": "finish", "result": {"text": "done"}})


def _fail(state: SupervisorState, task: Task) -> None:
    _report(state, task, {"action": "fail", "error": "network down"})


def _relays(state: SupervisorState, session_id: str) -> list[dict[str, Any]]:
    """交到这个会话的异步工具结果入站。"""
    return [
        event["payload"] for event in state.eventlog.events
        if event["type"] == "inbound_message" and event["payload"].get("message_id") == f"async_tool_result:{session_id}"
    ]


def _created_since(state: SupervisorState, before: set[str]) -> list[Task]:
    return [task for task_id, task in state.tasks.items() if task_id not in before]


class TestWhereResultsGo:
    def test_an_entry_task_gets_its_result_in_its_own_conversation_and_node(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid, node="bootstrap.coder")
        _finish(state, launcher)
        # 会话之后被 switch_node 换过入口，结果照样交给发起的节点
        state.session_entry_overrides[sid] = "other.node"
        before = set(state.tasks)

        outcome = state.inject_async_result(
            sid, "✅ done", task_id=launcher.task_id, node_id="bootstrap.coder", tool_name="execute_command", success=True,
        )

        assert outcome == {"ok": True, "strategy": "inbound"}
        [payload] = _relays(state, sid)
        assert payload["text"] == "✅ done" and "dispatch_origin" not in payload
        assert payload["entry_node_id"] == "bootstrap.coder" and payload["entry_node_pinned"] is True
        assert payload["route_hints"]["async_tool_success"] is True
        assert payload["continues_task_id"] == launcher.task_id
        [relay] = _created_since(state, before)
        assert relay.node_id == "bootstrap.coder" and relay.input["parent_session_id"] == sid
        assert relay.input["inbound_child_task_id"] == launcher.task_id
        assert relay.input["inbound_continues_task_id"] == launcher.task_id

    def test_a_wait_child_hands_its_result_to_the_caller_with_a_note(self, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())
        before = set(state.tasks)

        outcome = state.inject_async_result(
            sid, "✅ built", task_id=child.task_id, node_id="a.node", tool_name="execute_command", success=True,
        )

        assert outcome["strategy"] == "inbound"
        [payload] = _relays(state, sid)
        assert payload["entry_node_id"] == "test.orchestrator" and payload["entry_node_pinned"] is True
        assert payload["text"].startswith(f'✅ built\n\n{_NOTE} "a.node" (task {child.task_id[:8]}), which is still running.')
        assert "continue_from" not in payload["text"]
        assert payload["continues_task_id"] == caller.task_id
        [relay] = _created_since(state, before)
        assert relay.node_id == "test.orchestrator"

    def test_a_failed_fresh_sub_agent_hands_its_result_to_the_dispatcher_with_a_handle(
        self, state: SupervisorState,
    ) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _fail(state, child)
        before = set(state.tasks)

        outcome = state.inject_async_result(
            agent_sid, "❌ tests failed", task_id=child.task_id, node_id="a.node", tool_name="execute_command",
            success=False, error="exit 1",
        )

        assert outcome["strategy"] == "inbound"
        assert _relays(state, agent_sid) == []
        [payload] = _relays(state, sid)
        assert payload["entry_node_id"] == "test.orchestrator" and payload["entry_node_pinned"] is True
        assert "has already finished" in payload["text"] and 'dispatch to it again with continue_from="' in payload["text"]
        assert payload["route_hints"]["async_tool_failed"] is True and payload["route_hints"]["async_tool_error"] == "exit 1"
        [relay] = _created_since(state, before)
        assert relay.node_id == "test.orchestrator" and relay.input["parent_session_id"] == sid

    def test_a_finished_fresh_sub_agent_has_no_handle_to_offer(self, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        _finish(state, child)

        state.inject_async_result(agent_sid, "✅ ok", task_id=child.task_id, tool_name="execute_command", success=True)

        [payload] = _relays(state, sid)
        assert "has already finished" in payload["text"] and "continue_from" not in payload["text"]

    def test_an_accumulating_sub_agent_takes_its_own_result_and_still_reports_back(self, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller, mode="accumulate", continue_from="old-handle")
        before = set(state.tasks)

        outcome = state.inject_async_result(
            agent_sid, "✅ tests passed", task_id=child.task_id, node_id="a.node", tool_name="execute_command",
            success=True,
        )

        assert outcome["strategy"] == "inbound"
        assert _relays(state, sid) == []
        [payload] = _relays(state, agent_sid)
        assert payload["text"] == "✅ tests passed"
        assert payload["entry_node_id"] == "a.node" and "entry_node_pinned" not in payload
        assert payload["dispatch_context_mode"] == "accumulate"
        origin = payload["dispatch_origin"]
        assert origin["caller_task_id"] == caller.task_id and origin["parent_session_id"] == sid
        assert CONTINUE_FROM_KEY not in origin
        assert payload["continues_task_id"] == child.task_id
        [relay] = _created_since(state, before)
        assert relay.node_id == "a.node" and relay.input["parent_session_id"] == agent_sid
        assert relay.input["_dispatch_origin"]["caller_task_id"] == caller.task_id

    def test_an_accumulating_sub_agent_whose_session_is_gone_falls_back_to_the_dispatcher(
        self, state: SupervisorState,
    ) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller, mode="accumulate")
        with state._lock:
            state._forget_session_locked(agent_sid)

        outcome = state.inject_async_result(agent_sid, "✅ late", task_id=child.task_id, success=True)

        assert outcome["strategy"] == "inbound"
        [payload] = _relays(state, sid)
        assert payload["entry_node_id"] == "test.orchestrator" and _NOTE in payload["text"]

    def test_a_wait_child_whose_caller_record_is_gone_lands_in_its_conversation(self, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())
        with state._lock:
            state.tasks.pop(caller.task_id)

        state.inject_async_result(sid, "✅ built", task_id=child.task_id, success=True)

        [payload] = _relays(state, sid)
        assert "entry_node_id" not in payload and _NOTE in payload["text"]

    def test_a_sub_agent_whose_dispatcher_record_is_gone_goes_to_the_dispatching_node(
        self, state: SupervisorState,
    ) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        with state._lock:
            state.tasks.pop(caller.task_id)

        state.inject_async_result(agent_sid, "✅ built", task_id=child.task_id, success=True)

        [payload] = _relays(state, sid)
        assert payload["entry_node_id"] == "test.orchestrator" and payload["entry_node_pinned"] is True
        assert "is still running" in payload["text"]
        assert "continues_task_id" not in payload

    def test_a_result_whose_task_record_is_gone_lands_in_the_posted_conversation(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid, node="bootstrap.coder")
        _finish(state, launcher)
        with state._lock:
            state.tasks.pop(launcher.task_id)

        outcome = state.inject_async_result(sid, "✅ late", task_id=launcher.task_id, success=True)

        assert outcome["strategy"] == "inbound"
        [payload] = _relays(state, sid)
        assert "entry_node_id" not in payload and payload["text"] == "✅ late"

    def test_with_neither_task_nor_conversation_there_is_nowhere_to_go(self, state: SupervisorState) -> None:
        assert state.inject_async_result("gone-session", "x", task_id="gone-task") == {
            "ok": False, "error": "session not found",
        }

    def test_outputs_the_result_quotes_are_copied_to_where_it_lands(
        self, state: SupervisorState, tmp_path: Path,
    ) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        outputs = ToolOutputStore(tmp_path)
        saved = outputs.save(agent_sid, "完整构建日志\n" * 100)

        state.inject_async_result(agent_sid, f"✅ done, full log ref={saved.ref}", task_id=child.task_id, success=True)

        assert outputs.load(sid, saved.ref) == "完整构建日志\n" * 100

    def test_media_from_a_sub_agent_is_sent_to_the_conversation_it_lands_in(self, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())

        outcome = state.inject_async_result(
            sid, "", ["data/attachments/cat.png"], task_id=child.task_id, tool_name="draw", success=True,
        )

        assert outcome == {"ok": True, "strategy": "outbound_attachment"}
        [sent] = [
            event for event in state.eventlog.events
            if event["type"] == "outbound_message" and event["payload"].get("message_type") == "async_tool_attachment"
        ]
        assert sent["payload"]["attachments"] == [{"path": "data/attachments/cat.png"}]
        assert sent["payload"]["task_id"] == child.task_id and sent["session_id"] == sid

    def test_a_failure_with_attachments_is_still_told_as_a_message(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)

        outcome = state.inject_async_result(
            sid, "❌ failed", ["data/attachments/partial.png"], task_id=launcher.task_id, success=False, error="boom",
        )

        assert outcome["strategy"] == "inbound" and len(_relays(state, sid)) == 1


class TestDroppedResults:
    def test_a_cancelled_tool_call_is_dropped(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        before = set(state.tasks)

        outcome = state.inject_async_result(sid, "late", task_id=launcher.task_id, cancelled=True)

        assert outcome == {"ok": True, "strategy": "dropped", "reason": "cancelled"}
        assert _relays(state, sid) == [] and _created_since(state, before) == []

    def test_a_reset_conversation_takes_no_late_results(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        state.reset_session(session_id=sid)

        outcome = state.inject_async_result(sid, "late", task_id=launcher.task_id, success=True)

        assert outcome == {"ok": True, "strategy": "dropped", "reason": "session_cancelled"}

    def test_a_cancelled_launching_task_takes_no_late_results(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        state.cancel_single_task(launcher.task_id)
        state.clear_cancelled(sid)

        outcome = state.inject_async_result(sid, "late", task_id=launcher.task_id, success=True)

        assert outcome == {"ok": True, "strategy": "dropped", "reason": "origin_cancelled"}

    def test_a_result_posted_to_another_conversation_is_refused(self, state: SupervisorState) -> None:
        sid = _session(state)
        other = _session(state, "qq_group:6006")
        launcher = _inbound(state, sid)

        outcome = state.inject_async_result(other, "late", task_id=launcher.task_id, success=True)

        assert outcome == {"ok": True, "strategy": "dropped", "reason": "route_mismatch"}


def _attachments_sent(state: SupervisorState) -> list[dict[str, Any]]:
    return [
        event for event in state.eventlog.events
        if event["type"] == "outbound_message" and event["payload"].get("message_type") == "async_tool_attachment"
    ]


class TestRedelivery:
    """engine 没收到应答会带着同一个投递 id 重投：交过的照当时的交法回，不再交第二次。"""

    def test_a_repeated_result_is_answered_but_not_delivered_again(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        _finish(state, launcher)
        before = set(state.tasks)

        first = state.inject_async_result(sid, "✅ done", task_id=launcher.task_id, success=True, delivery_id="d-1")
        again = state.inject_async_result(sid, "✅ done", task_id=launcher.task_id, success=True, delivery_id="d-1")

        assert first == {"ok": True, "strategy": "inbound"}
        assert again == {"ok": True, "strategy": "inbound", "deduped": True}
        [payload] = _relays(state, sid)
        assert payload["delivery_id"] == "async-tool:d-1"
        assert len(_created_since(state, before)) == 1

    def test_a_repeated_media_result_is_sent_once(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)

        for _ in range(2):
            state.inject_async_result(
                sid, "", ["data/attachments/cat.png"], task_id=launcher.task_id, tool_name="draw", success=True,
                delivery_id="d-1",
            )

        [sent] = _attachments_sent(state)
        assert sent["payload"]["delivery_id"] == "async-tool:d-1"
        assert state.inject_async_result(
            sid, "", ["data/attachments/cat.png"], task_id=launcher.task_id, success=True, delivery_id="d-1",
        ) == {"ok": True, "strategy": "outbound_attachment", "deduped": True}

    def test_other_results_and_results_without_an_id_are_all_delivered(self, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        _finish(state, launcher)

        for delivery_id in ("d-1", "d-2", "", ""):
            state.inject_async_result(sid, "✅ done", task_id=launcher.task_id, success=True, delivery_id=delivery_id)

        assert len(_relays(state, sid)) == 4

    def test_a_repeat_landing_between_planning_and_delivery_is_not_delivered_twice(
        self, state: SupervisorState, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        agent_sid, child = _async_child(state, sid, caller)
        adopt = state._adopt_async_result_refs
        raced: list[dict[str, Any]] = []

        def adopt_while_a_repeat_lands(source: str, target: str, text: str) -> int:
            # 第一次复制超长输出的空当里，同一份结果的另一次重投抢先交下去
            if not raced:
                raced.append({})
                raced[0] = state.inject_async_result(
                    agent_sid, "✅ built", task_id=child.task_id, success=True, delivery_id="d-1",
                )
            return adopt(source, target, text)

        monkeypatch.setattr(state, "_adopt_async_result_refs", adopt_while_a_repeat_lands)

        outcome = state.inject_async_result(agent_sid, "✅ built", task_id=child.task_id, success=True, delivery_id="d-1")

        assert raced == [{"ok": True, "strategy": "inbound"}]
        assert outcome == {"ok": True, "strategy": "inbound", "deduped": True}
        assert len(_relays(state, sid)) == 1

    def test_a_restarted_supervisor_still_knows_what_it_delivered(self, state: SupervisorState, tmp_path: Path) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        _finish(state, launcher)
        state.inject_async_result(sid, "✅ done", task_id=launcher.task_id, success=True, delivery_id="d-1")

        restarted = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="async-tool-routing-restarted"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )

        assert restarted.inject_async_result(
            sid, "✅ done", task_id=launcher.task_id, success=True, delivery_id="d-1",
        ) == {"ok": True, "strategy": "inbound", "deduped": True}


def _cancelled(state: SupervisorState, task_id: str, session_id: str = "") -> bool:
    with state._lock:
        return detached_cancelled_locked(state, task_id, session_id)


class TestDetachedCancelRule:
    @pytest.mark.parametrize("status", [TaskStatus.pending, TaskStatus.running, TaskStatus.suspended, TaskStatus.completed])
    def test_suspension_requeueing_and_completion_do_not_count(self, state: SupervisorState, status: TaskStatus) -> None:
        sid = _session(state)
        task = _inbound(state, sid)
        task.status = status
        task.claim_id = ""

        assert _cancelled(state, task.task_id, sid) is False

    @pytest.mark.parametrize("how", ["cancelled", "failed", "cancel_requested", "session_reset"])
    def test_a_stopped_task_counts(self, state: SupervisorState, how: str) -> None:
        sid = _session(state)
        task = _inbound(state, sid)
        if how == "cancelled":
            task.status = TaskStatus.cancelled
        elif how == "failed":
            task.status = TaskStatus.failed
        elif how == "cancel_requested":
            task.cancel_requested = True
        else:
            task.input["_session_reset"] = True

        assert _cancelled(state, task.task_id, sid) is True

    def test_cancelling_the_conversation_counts(self, state: SupervisorState) -> None:
        sid = _session(state)
        task = _inbound(state, sid)

        state.cancel_session(sid)

        assert _cancelled(state, task.task_id, sid) is True

    def test_a_newer_generation_of_the_conversation_counts(self, state: SupervisorState) -> None:
        sid = _session(state)
        task = _inbound(state, sid)
        with state._lock:
            state._next_session_generation_locked(sid)

        assert _cancelled(state, task.task_id, sid) is True

    def test_a_pruned_task_is_judged_by_its_conversation(self, state: SupervisorState) -> None:
        sid = _session(state)

        assert _cancelled(state, "gone-task", sid) is False
        assert _cancelled(state, "gone-task") is False

        state.cancel_session(sid)

        assert _cancelled(state, "gone-task", sid) is True


class TestEndpoints:
    @pytest.fixture
    def client(self, state: SupervisorState, tmp_path: Path) -> TestClient:
        return TestClient(_app(state, tmp_path), headers=_HEADERS)

    def test_results_are_routed_by_their_task(self, client: TestClient, state: SupervisorState) -> None:
        sid = _session(state)
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _item())

        response = client.post("/v1/async_tools/results", json={
            "session_id": sid, "message": "✅ built", "task_id": child.task_id, "node_id": "a.node",
            "tool_name": "execute_command", "success": True,
        })

        assert response.status_code == 200 and response.json() == {"ok": True, "strategy": "inbound"}
        [payload] = _relays(state, sid)
        assert payload["entry_node_id"] == "test.orchestrator" and payload["route_hints"]["async_tool_success"] is True

    def test_a_result_posted_again_to_either_route_is_delivered_once(
        self, client: TestClient, state: SupervisorState,
    ) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)
        _finish(state, launcher)
        body = {"message": "✅ built", "task_id": launcher.task_id, "success": True, "delivery_id": "d-1"}

        first = client.post("/v1/async_tools/results", json={**body, "session_id": sid})
        again = client.post("/v1/async_tools/results", json={**body, "session_id": sid})
        by_session = client.post(f"/v1/sessions/{sid}/async_tool_result", json=body)

        assert first.json() == {"ok": True, "strategy": "inbound"}
        assert again.json() == by_session.json() == {"ok": True, "strategy": "inbound", "deduped": True}
        assert len(_relays(state, sid)) == 1

    def test_a_cancelled_result_is_acknowledged_and_dropped(self, client: TestClient, state: SupervisorState) -> None:
        sid = _session(state)
        launcher = _inbound(state, sid)

        response = client.post("/v1/async_tools/results", json={
            "session_id": sid, "message": "late", "task_id": launcher.task_id, "success": False, "cancelled": True,
        })

        assert response.json() == {"ok": True, "strategy": "dropped", "reason": "cancelled"}

    def test_a_result_with_nowhere_to_go_is_acknowledged_not_retried(self, client: TestClient) -> None:
        response = client.post("/v1/async_tools/results", json={"session_id": "gone", "task_id": "gone", "message": "x"})

        assert response.status_code == 200
        assert response.json() == {"ok": True, "strategy": "dropped", "reason": "session_not_found"}

    @pytest.mark.parametrize("body", [{"message": "no session"}, ["not", "an", "object"]])
    def test_malformed_bodies_are_refused(self, client: TestClient, body: Any) -> None:
        assert client.post("/v1/async_tools/results", json=body).status_code == 422

    def test_a_body_that_is_not_json_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/v1/async_tools/results", content=b"{not json", headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422

    def test_the_cancel_probe(self, client: TestClient, state: SupervisorState) -> None:
        sid = _session(state)
        task = _inbound(state, sid)

        def probe(task_id: str) -> Any:
            return client.get(f"/v1/async_tools/{task_id}/cancelled", params={"session_id": sid}).json()

        assert probe(task.task_id) == {"cancelled": False}
        assert probe("gone-task") == {"cancelled": False}

        state.cancel_session(sid)

        assert probe(task.task_id) == {"cancelled": True}
        assert probe("gone-task") == {"cancelled": True}

    def test_the_admin_token_is_required(self, client: TestClient) -> None:
        wrong = {"Authorization": "Bearer wrong"}

        assert client.get("/v1/async_tools/t/cancelled", headers=wrong).status_code == 401


class TestDetachedToolAgainstTheSupervisor:
    def test_suspend_and_resume_do_not_stop_a_command_already_in_the_background(
        self, state: SupervisorState, tmp_path: Path,
    ) -> None:
        app = _app(state, tmp_path)
        sid = _session(state)
        task = _inbound(state, sid)
        task.status, task.worker_id, task.claim_id = TaskStatus.running, "worker-1", "claim-1"

        def probe(*, detached: bool) -> bool:
            async def run() -> bool:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=_HEADERS) as http:
                    ctx = ToolContext(
                        supervisor_url="http://test", session_id=task.session_id, run_id="run-1", worker_id="worker-1",
                        workspace_root=tmp_path, http=http, registry=None, task_id=task.task_id,
                        parent_session_id=sid, claim_id="claim-1", detached=detached,
                    )
                    return await ctx.check_cancelled()

            return asyncio.run(run())

        assert probe(detached=True) is False
        # 挂起等子任务：领取作废
        task.status, task.worker_id, task.claim_id = TaskStatus.suspended, None, ""
        assert probe(detached=True) is False
        # 恢复后换了领取
        task.status, task.worker_id, task.claim_id = TaskStatus.running, "worker-2", "claim-2"
        assert probe(detached=True) is False
        # 还在任务里的工具拿旧领取去问，就是已经出局
        assert probe(detached=False) is True

        state.cancel_single_task(task.task_id)

        assert probe(detached=True) is True
