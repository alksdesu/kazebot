"""子任务的结局只交给正在等它的 caller。

caller 已经不在等（被回收、取消，或在等别的子任务）时结果只记日志丢弃，绝不改投给用户；
批次在兄弟全部结束后恢复一次；批次里的子节点自己再派发或被抢占时照常路由。
三条取消路径共用一个结算：取消运行中的子任务，worker 停下后 caller 恢复。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-settlement"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _caller(state: SupervisorState, session_id: str) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": "test:sup-settlement"}},
            continuation={},
            source_inbound_seq=None,
        )


def _dispatch(state: SupervisorState, caller: Task, action: dict[str, Any]) -> list[Task]:
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", **action})
    return sorted(
        (t for t in state.tasks.values() if t.caller_task_id == caller.task_id and not state._task_terminal(t)),
        key=lambda t: t.batch_index,
    )


def _single(target: str = "child.node") -> dict[str, Any]:
    return {"target_node": target, "dispatch_input": {"instruction": "go", "context_mode": "fresh"}}


def _batch(*targets: str) -> dict[str, Any]:
    return {"dispatch_batch": [
        {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh"}
        for target in targets
    ]}


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _finish(state: SupervisorState, task: Task, text: str) -> None:
    _report(state, task, {"action": "finish", "result": {"text": text}})


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


class TestResultsOnlyReachAWaitingCaller:
    def test_a_child_of_a_caller_that_stopped_waiting_is_dropped_not_sent_to_the_user(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        caller.status = TaskStatus.failed

        _finish(state, child, "internal notes for the orchestrator")

        assert "internal notes for the orchestrator" not in _outbound_texts(state)
        assert caller.status == TaskStatus.failed

    def test_a_caller_waiting_on_another_child_ignores_a_stale_one(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        caller.waiting_for_task_id = "a-different-child"

        _finish(state, child, "stale child output")

        assert caller.status == TaskStatus.suspended
        assert "stale child output" not in _outbound_texts(state)

    def test_an_engine_reported_cancel_still_resumes_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())

        _report(state, child, {"action": "cancelled"})

        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["type"] == "child_cancelled"


class TestBatches:
    def test_the_caller_resumes_once_after_every_sibling_ends(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _finish(state, first, "first part")
        assert caller.status == TaskStatus.suspended
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        entries = caller.input["resume_data"]["entries"]
        assert [entry["text"] for entry in entries] == ["first part", "second part"]
        resumed_at = caller.updated_at
        state.complete_task(
            task_id=first.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "first part"}},
        )
        assert caller.updated_at == resumed_at
        assert not {"first part", "second part"} & set(_outbound_texts(state))

    def test_cancelling_a_pending_sibling_waits_for_the_rest(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        assert state.cancel_single_task(first.task_id)["cancelled_count"] == 1
        assert caller.status == TaskStatus.suspended

        _finish(state, second, "survivor")
        assert caller.status == TaskStatus.pending
        statuses = [entry["status"] for entry in caller.input["resume_data"]["entries"]]
        assert statuses == ["cancelled", "finish"]

    def test_a_batch_child_that_dispatches_gets_its_own_children(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _report(state, first, {"action": "dispatch", **_single("grand.node")})

        assert first.status == TaskStatus.suspended and first.batch_id
        [grandchild] = [t for t in state.tasks.values() if t.caller_task_id == first.task_id]
        assert caller.status == TaskStatus.suspended
        _finish(state, grandchild, "grandchild work")
        assert first.status == TaskStatus.pending
        _finish(state, first, "first after grandchild")
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        assert [entry["text"] for entry in caller.input["resume_data"]["entries"]] == [
            "first after grandchild", "second part",
        ]

    def test_a_preempted_batch_child_counts_as_ended(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _report(state, first, {"action": "preempted", "context_ref": "ctx-1"})
        assert caller.status == TaskStatus.suspended
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        assert [entry["status"] for entry in caller.input["resume_data"]["entries"]] == ["preempted", "finish"]


class TestCancellingARunningChild:
    def test_the_caller_resumes_once_the_worker_stops(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        child.status = TaskStatus.running
        child.worker_id = "worker-1"
        child.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        assert state.cancel_single_task(child.task_id)["cancelled_count"] == 1
        assert child.cancel_requested and child.status == TaskStatus.running
        assert caller.status == TaskStatus.suspended

        state.complete_task(
            task_id=child.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "ignored after cancel"}},
        )

        assert child.status == TaskStatus.cancelled
        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["type"] == "child_cancelled"
        assert caller.input["_last_child_action_type"] == "cancelled"
        assert "ignored after cancel" not in _outbound_texts(state)
