"""没人再等的待审批按拒绝收掉：任务取消、失败或会话重置时当场全收，正常结束时和 housekeeping 巡检只收没人来读的。

工具等审批时每 0.5 秒来读一次；任务没了、等待循环退出以后审批就一直挂着，适配器重连对账会把它重新亮给管理员，
点了也没有任何任务在等。转到后台的工具在任务正常结束后还可能在等，它还在读的那条不能收。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.state import APPROVAL_WAITER_SILENCE, ORPHANED_APPROVAL_COMMENT, SupervisorState  # noqa: E402
from supervisor.types import Approval, ApprovalStatus, RouteStatus, Task, TaskStatus  # noqa: E402
from tests.test_sup_settlement import _caller, _state  # noqa: E402

_STALE = APPROVAL_WAITER_SILENCE + timedelta(seconds=1)


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="test", conversation_key="test:sup-orphaned-approvals")


def _approval_for(state: SupervisorState, task: Task | None, session_id: str, *, call_id: str = "call-1") -> Approval:
    return state.create_approval(
        session_id=session_id, operation="execute_command", details={"command": "ls"},
        tool_call_id=call_id, node_id="test.node", task_id=task.task_id if task is not None else None,
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


def _unread(state: SupervisorState, approval: Approval) -> None:
    """等它的工具已经超过 APPROVAL_WAITER_SILENCE 没来读过。"""
    approval.requested_at = approval.requested_at - _STALE
    state._approval_read_at.pop(approval.approval_id, None)


def _last_read_long_ago(state: SupervisorState, approval: Approval) -> None:
    state._approval_read_at[approval.approval_id] = datetime.now(timezone.utc) - _STALE


def _complete(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _expired(approval: Approval, reason: str) -> bool:
    return (
        approval.status == ApprovalStatus.denied
        and approval.comment == ORPHANED_APPROVAL_COMMENT
        and approval.expired_reason == reason
    )


class TestSweep:
    def test_an_approval_of_a_cancelled_task_is_closed_as_auto_denied(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        task.status = TaskStatus.cancelled

        assert _sweep(state) == [approval.approval_id]

        assert _expired(approval, "任务已取消")
        [event] = _decided_events(state)
        assert event["approval_id"] == approval.approval_id and event["task_id"] == task.task_id
        assert event["expired_reason"] == "任务已取消"

    def test_an_approval_whose_task_is_gone_from_memory_is_closed_once_nobody_reads_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        with state._lock:
            state.tasks.pop(task.task_id)

        assert _sweep(state) == []

        _unread(state, approval)

        assert _sweep(state) == [approval.approval_id]
        assert _expired(approval, "发起这条审批的任务已不在")

    def test_a_live_task_keeps_its_approval_open(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        task.status = TaskStatus.running
        approval = _approval_for(state, task, sid)

        assert _sweep(state) == []
        assert approval.status == ApprovalStatus.pending

    def test_a_live_task_keeps_an_approval_its_tool_still_reads(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        task.status = TaskStatus.running
        approval = _approval_for(state, task, sid)
        _unread(state, approval)
        state.note_approval_read(approval.approval_id)

        assert _sweep(state) == []
        assert approval.status == ApprovalStatus.pending

    def test_an_approval_nobody_reads_any_more_is_closed_even_while_its_task_lives(self, tmp_path: Path) -> None:
        # 比如重排之前那次领取提的审批：任务还活着，等它的那个工具已经不在了
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        task.status = TaskStatus.running
        approval = _approval_for(state, task, sid)
        _unread(state, approval)

        assert _sweep(state) == [approval.approval_id]
        assert _expired(approval, "已经没有人在等这条审批")

    def test_reading_an_approval_through_the_api_counts_as_waiting_for_it(
        self, tmp_path: Path, monkeypatch: "Any",
    ) -> None:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-token")
        monkeypatch.setattr(admin_api, "_admin_token", "")
        monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        task.status = TaskStatus.running
        approval = _approval_for(state, task, sid)
        _unread(state, approval)
        app = create_app(
            state=state,
            process_manager=None,
            config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
        )

        async def _read() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 读审批现在要管理员令牌
                return await client.get(
                    f"/v1/approvals/{approval.approval_id}",
                    headers={"Authorization": "Bearer test-token"},
                )

        assert asyncio.run(_read()).status_code == 200
        assert _sweep(state) == []

    def test_an_approval_without_a_task_is_left_alone(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        approval = _approval_for(state, None, sid)
        _unread(state, approval)

        assert _sweep(state) == []
        assert approval.status == ApprovalStatus.pending

    def test_an_approval_decided_by_a_person_is_not_touched_again(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        state.decide_approval(approval_id=approval.approval_id, decision="allow", comment="ok")
        task.status = TaskStatus.completed

        assert _sweep(state) == []
        assert approval.status == ApprovalStatus.allowed
        assert approval.expired_reason is None
        [event] = _decided_events(state)
        assert event["expired_reason"] is None


class TestWhenTheTaskEnds:
    def test_a_cancelled_task_closes_its_approvals_at_once_even_while_they_are_read(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        state.note_approval_read(approval.approval_id)

        with state._lock:
            state._cancel_task_locked(task, datetime.now(timezone.utc), force=True)

        assert task.status == TaskStatus.cancelled
        assert _expired(approval, "任务已取消")
        [event] = _decided_events(state)
        assert event["task_id"] == task.task_id and event["expired_reason"] == "任务已取消"

    def test_a_failed_task_closes_its_approvals_with_the_error(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        state.note_approval_read(approval.approval_id)

        with state._lock:
            state._fail_task_locked(task, error="worker 失联")

        assert _expired(approval, "任务失败：worker 失联")

    def test_a_reset_session_closes_the_approvals_of_live_and_finished_tasks(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        live = _caller(state, sid)
        finished = _caller(state, sid)
        live_approval = _approval_for(state, live, sid)
        # 正常结束的任务转到后台的工具还在等这条：重置后它也要停
        finished_approval = _approval_for(state, finished, sid, call_id="call-2")
        finished.status = TaskStatus.completed
        finished.route_status = RouteStatus.routed
        state.note_approval_read(live_approval.approval_id)
        state.note_approval_read(finished_approval.approval_id)

        assert state.reset_session(session_id=sid)["ok"] is True

        assert live.status == TaskStatus.cancelled
        assert _expired(live_approval, "会话已重置")
        assert _expired(finished_approval, "会话已重置")

    def test_a_normal_completion_closes_only_the_approvals_nobody_reads(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        read = _approval_for(state, task, sid, call_id="call-read")
        unread = _approval_for(state, task, sid, call_id="call-unread")
        _unread(state, unread)
        state.note_approval_read(read.approval_id)

        _complete(state, task, {"action": "finish", "result": {"text": "done"}})

        assert task.status == TaskStatus.completed
        assert _expired(unread, "任务已结束，没有人再等这条审批")
        assert read.status == ApprovalStatus.pending

        _last_read_long_ago(state, read)

        assert _sweep(state) == [read.approval_id]
        assert _expired(read, "任务已结束，没有人再等这条审批")

    def test_a_dispatch_on_its_way_to_suspending_does_not_count_as_the_end(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        task = _caller(state, sid)
        approval = _approval_for(state, task, sid)
        _unread(state, approval)

        _complete(state, task, {"action": "dispatch", "dispatch_batch": [
            {"kind": "node", "target": "a.node", "instruction": "do a", "context_mode": "fresh"},
        ]})

        assert task.status == TaskStatus.suspended
        assert approval.status == ApprovalStatus.pending
        assert _sweep(state) == [approval.approval_id]
        assert _expired(approval, "已经没有人在等这条审批")
