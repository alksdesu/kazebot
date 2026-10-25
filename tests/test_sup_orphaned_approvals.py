"""任务已经结束的待审批由 housekeeping 按拒绝收掉。

工具等审批时任务被取消或回收，等待循环直接退出，审批就一直挂着待审；适配器重连对账会把它
重新亮给管理员，管理员点了也没有任何任务在等。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.state import ORPHANED_APPROVAL_COMMENT, SupervisorState  # noqa: E402
from supervisor.types import ApprovalStatus, Task, TaskStatus  # noqa: E402
from tests.test_sup_settlement import _caller, _state  # noqa: E402


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="test", conversation_key="test:sup-orphaned-approvals")


def _approval_for(state: SupervisorState, task: Task | None, session_id: str):
    return state.create_approval(
        session_id=session_id, operation="execute_command", details={"command": "ls"},
        tool_call_id="call-1", node_id="test.node", task_id=task.task_id if task is not None else None,
    )


def _sweep(state: SupervisorState) -> list[str]:
    with state._lock:
        return state._close_orphaned_approvals_locked()


def _decided_events(state: SupervisorState) -> list[dict]:
    return [
        event.get("payload") or {}
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "approval_decided"
    ]


def test_an_approval_of_a_cancelled_task_is_closed_as_auto_denied(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = _session(state)
    task = _caller(state, sid)
    approval = _approval_for(state, task, sid)
    task.status = TaskStatus.cancelled

    assert _sweep(state) == [approval.approval_id]

    assert approval.status == ApprovalStatus.denied
    assert approval.comment == ORPHANED_APPROVAL_COMMENT
    [event] = _decided_events(state)
    assert event["approval_id"] == approval.approval_id and event["task_id"] == task.task_id


def test_an_approval_whose_task_is_gone_from_memory_is_closed_too(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = _session(state)
    task = _caller(state, sid)
    approval = _approval_for(state, task, sid)
    with state._lock:
        state.tasks.pop(task.task_id)

    assert _sweep(state) == [approval.approval_id]


def test_a_live_task_keeps_its_approval_open(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = _session(state)
    task = _caller(state, sid)
    task.status = TaskStatus.running
    approval = _approval_for(state, task, sid)

    assert _sweep(state) == []
    assert approval.status == ApprovalStatus.pending


def test_an_approval_without_a_task_is_left_alone(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = _session(state)
    approval = _approval_for(state, None, sid)

    assert _sweep(state) == []
    assert approval.status == ApprovalStatus.pending


def test_an_approval_decided_by_a_person_is_not_touched_again(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = _session(state)
    task = _caller(state, sid)
    approval = _approval_for(state, task, sid)
    state.decide_approval(approval_id=approval.approval_id, decision="allow", comment="ok")
    task.status = TaskStatus.completed

    assert _sweep(state) == []
    assert approval.status == ApprovalStatus.allowed
    assert len(_decided_events(state)) == 1
