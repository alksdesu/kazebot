"""旧的异步派发端点和它的结果注入删掉之后：完成路由对普通任务、异步委派的执行体照旧，
running_tasks 的 is_user_entry 和路由里的入口判断用同一个口径。"""
from __future__ import annotations

import asyncio
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_TOKEN = "old-dispatch-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_CONV = "qq_group:old-dispatch"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _state(workspace: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="old-dispatch"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    result._defer_post_work_enqueue = True
    return result


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    return _state(tmp_path)


def _inbound(state: SupervisorState, session_id: str, **extra: Any) -> Task:
    payload = {
        "channel": "qq_group",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "request",
        **extra,
    }
    event = state.eventlog.append(
        session_id=session_id, component="shell", type_="inbound_message", payload=payload,
    )
    state.record_inbound_message_event(event)
    task = next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])
    task.status = TaskStatus.running
    task.worker_id = "worker"
    return task


def _conversation(state: SupervisorState) -> tuple[str, Task]:
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=_CONV)
    return session_id, _inbound(state, session_id, entry_node_id="qq.orchestrator")


def _delegate(state: SupervisorState, caller: Task, target: str) -> Task:
    """异步委派现在唯一的样子：engine 带着 dispatch_origin 发一条 inbound。"""
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=f"agent:{target}:{_CONV}")
    return _inbound(
        state, session_id, entry_node_id=target, dispatch_context_mode="accumulate",
        dispatch_origin={
            "parent_session_id": str(caller.input.get("parent_session_id") or caller.session_id),
            "caller_node_id": caller.node_id,
            "parent_conversation_key": _CONV,
            "context_mode": "accumulate",
            "caller_task_id": caller.task_id,
        },
    )


def _delegate_and_wait(state: SupervisorState, caller: Task, target: str) -> Task:
    with state._lock:
        state._route_dispatch_locked(caller, {
            "action": "dispatch", "target_node": target,
            "dispatch_input": {"instruction": "help", "context_mode": "fresh"},
        })
    child = state.tasks[str(caller.waiting_for_task_id)]
    child.status = TaskStatus.running
    return child


def _finish(state: SupervisorState, task: Task, text: str = "done") -> None:
    state.complete_task(
        task_id=task.task_id, worker_id="worker", result={"action": "finish", "result": {"text": text}},
    )


def _events(state: SupervisorState, event_type: str) -> list[dict[str, Any]]:
    return [event for event in state.eventlog.events if event.get("type") == event_type]


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("headers", _AUTH)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def _task(**changes: Any) -> Task:
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "task_id": "task", "session_id": "session", "kind": TaskKind.node, "status": TaskStatus.running,
        "created_at": now, "updated_at": now,
    }
    fields.update(changes)
    return Task(**fields)


class TestTheOldEndpointIsGone:
    def test_posting_to_it_creates_nothing(self, state: SupervisorState, tmp_path: Path) -> None:
        session_id, _entry = _conversation(state)
        before = set(state.tasks)

        response = _call(
            _app(state, tmp_path), "POST", "/v1/tasks/dispatch-async",
            json={"session_id": session_id, "node_id": "bob", "instruction": "work"}, headers=_AUTH,
        )

        assert response.status_code in (404, 405)
        assert set(state.tasks) == before


class TestCompletionRoutingIsUnchanged:
    def test_a_plain_entry_task_still_replies_to_the_user(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)

        _finish(state, entry, "hello there")

        [reply] = [
            event for event in _events(state, "outbound_message")
            if (event.get("payload") or {}).get("source_inbound_seq") == entry.source_inbound_seq
        ]
        assert reply["session_id"] == session_id
        assert reply["payload"]["text"] == "hello there"

    def test_a_delegated_child_still_reports_back_to_the_caller(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        _finish(state, entry, "交给 bob 了")

        _finish(state, bob, "found it")

        [callback] = [
            task for task in state.tasks.values() if task.input.get("inbound_child_task_id") == bob.task_id
        ]
        assert callback.input["parent_session_id"] == session_id
        assert callback.input["inbound_message_type"] == "dispatch_result"
        assert callback.node_id == "qq.orchestrator"
        assert not [
            event for event in _events(state, "outbound_message")
            if (event.get("payload") or {}).get("source_inbound_seq") == bob.source_inbound_seq
        ]


class TestUserEntry:
    @pytest.mark.parametrize("changes, expected", [
        pytest.param({}, True, id="用户消息"),
        pytest.param({"caller_task_id": "parent"}, False, id="同步委派的子任务"),
        pytest.param({"input": {"_dispatch_origin": {"parent_session_id": "p"}}}, False, id="异步委派的执行体"),
        pytest.param({"input": {"_system_task": True}}, False, id="系统任务"),
        pytest.param({"input": {"schedule_id": "daily"}}, False, id="定时任务"),
        pytest.param({"input": {"schedule_id": ""}}, True, id="空的定时任务 id"),
    ])
    def test_one_rule_decides_what_counts(self, changes: dict[str, Any], expected: bool) -> None:
        assert SupervisorState._is_user_entry_task(_task(**changes)) is expected

    def test_running_tasks_flag_only_user_messages(self, state: SupervisorState, tmp_path: Path) -> None:
        session_id, entry = _conversation(state)
        scheduled = _inbound(state, session_id, entry_node_id="qq.orchestrator", schedule_id="daily")
        system = _inbound(state, session_id, entry_node_id="qq.orchestrator", _system_task=True)
        bob = _delegate(state, entry, "bob")
        waiting_caller = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        helper = _delegate_and_wait(state, waiting_caller, "helper")
        app = _app(state, tmp_path)

        def flags(sid: str) -> dict[str, bool]:
            response = _call(app, "GET", f"/v1/sessions/{sid}/running_tasks")
            assert response.status_code == 200, response.text
            return {item["task_id"]: item["is_user_entry"] for item in response.json()["tasks"]}

        assert flags(session_id) == {
            entry.task_id: True, scheduled.task_id: False, system.task_id: False, helper.task_id: False,
        }
        assert flags(str(bob.input["parent_session_id"])) == {bob.task_id: False}

    def test_the_routing_lookup_uses_the_same_rule(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        scheduled = _inbound(state, session_id, entry_node_id="qq.orchestrator", schedule_id="daily")
        bob = _delegate(state, entry, "bob")

        with state._lock:
            assert state._find_running_entry_task_locked(entry.session_id) is entry
            assert state._find_running_entry_task_locked(scheduled.session_id) is None
            assert state._find_running_entry_task_locked(bob.session_id) is None


class TestSnapshots:
    def test_later_snapshots_no_longer_copy_the_old_marker(self, state: SupervisorState) -> None:
        _session_id, entry = _conversation(state)
        entry.input["_async_dispatch"] = True

        payload = state._event_payload_for_task(entry, event_type="task_started")

        assert payload["input"]["_input_slimmed"] is True
        assert "_async_dispatch" not in payload["input"]
        assert payload["input"]["task_context"]["conversation_key"] == _CONV

    def test_an_old_log_carrying_the_marker_still_replays(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        created = _task(
            task_id="legacy", status=TaskStatus.pending,
            input={"instruction": "do it", "_async_dispatch": True, "child_session_id": "child_old"},
        ).model_dump(mode="json")
        started = _task(
            task_id="legacy", status=TaskStatus.running,
            input={"_input_slimmed": True, "_async_dispatch": True, "child_session_id": "child_old"},
        ).model_dump(mode="json")

        state._apply_task_snapshot(created)
        state._apply_task_snapshot(started)

        task = state.tasks["legacy"]
        assert task.status == TaskStatus.running
        assert task.input["instruction"] == "do it"
        assert task.input["child_session_id"] == "child_old"
