"""回收线程按「这一程」计时，按等待目标判僵尸，回收即取消。

排队时长从重新入队算、运行时长从这次领取算，总寿命另设上限；批次挂起的 caller 只要
批次还在跑就不是僵尸；被回收的任务要让还在跑的 worker 从续租和取消查询里得知停手。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_reaper import (  # noqa: E402
    PENDING_QUEUE_LIMIT,
    TASK_LIFETIME_FACTOR,
    reap_reason,
    reap_stuck_tasks_locked,
)
from supervisor.task_ending import STOP_GRACE, TaskEnding  # noqa: E402
from supervisor.task_store import CompletionOutcome  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_MAX = 3600.0


@pytest.fixture(autouse=True)
def _default_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", raising=False)


def _task(**fields) -> Task:
    values = {
        "task_id": "t-1", "session_id": "s-1", "kind": TaskKind.node, "node_id": "test.node",
        "created_at": _NOW, "updated_at": _NOW,
    }
    values.update(fields)
    return Task(**values)


def _reason(task: Task, **flags) -> str | None:
    return reap_reason(
        task, now=_NOW, max_running_sec=_MAX, approval_pending=flags.pop("approval_pending", False),
        worker_silence=flags.pop("worker_silence", timedelta(0)), **flags,
    )


class TestPerPhaseTiming:
    def test_a_resumed_task_is_timed_from_its_requeue(self) -> None:
        task = _task(status=TaskStatus.pending, created_at=_NOW - timedelta(hours=2), queued_at=_NOW - timedelta(minutes=1))

        assert _reason(task, worker_silence=timedelta(hours=1)) is None

    def test_a_task_left_in_the_queue_with_no_worker_around_is_still_reaped(self) -> None:
        task = _task(
            status=TaskStatus.pending, created_at=_NOW - timedelta(hours=2),
            queued_at=_NOW - PENDING_QUEUE_LIMIT - timedelta(seconds=1),
        )

        assert _reason(task) is None
        reason = _reason(task, worker_silence=PENDING_QUEUE_LIMIT + timedelta(seconds=1))

        assert reason is not None and "排队 15 分钟无人接手" in reason

    def test_a_long_suspension_does_not_count_against_the_next_run(self) -> None:
        task = _task(
            status=TaskStatus.running, created_at=_NOW - timedelta(hours=2),
            claimed_at=_NOW - timedelta(minutes=1), lease_expires_at=_NOW + timedelta(minutes=2),
        )

        assert _reason(task) is None

    def test_one_run_over_the_hard_limit_is_reaped(self) -> None:
        task = _task(
            status=TaskStatus.running, created_at=_NOW - timedelta(hours=2),
            claimed_at=_NOW - timedelta(seconds=_MAX + 1), lease_expires_at=_NOW + timedelta(minutes=2),
        )

        reason = _reason(task)

        assert reason is not None and "运行过久" in reason

    @pytest.mark.parametrize("status", [TaskStatus.pending, TaskStatus.running, TaskStatus.suspended])
    def test_the_lifetime_cap_applies_to_every_live_status_even_with_an_approval(self, status: TaskStatus) -> None:
        lifetime = timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR + 1)
        task = _task(
            status=status, created_at=_NOW - lifetime, queued_at=_NOW, claimed_at=_NOW,
            lease_expires_at=_NOW + timedelta(minutes=2),
            waiting_for_task_id="child" if status == TaskStatus.suspended else None,
        )

        reason = _reason(task, approval_pending=True, wait_target_live=True)

        assert reason is not None and "总上限" in reason


class TestSuspendedWaitTargets:
    def test_a_batch_caller_is_kept_while_its_batch_runs(self) -> None:
        task = _task(status=TaskStatus.suspended, updated_at=_NOW - timedelta(hours=1), waiting_for_batch_id="batch-1")

        assert _reason(task, wait_target_live=True) is None

    def test_a_finished_batch_whose_caller_never_resumed_is_reaped_after_the_grace(self) -> None:
        task = _task(status=TaskStatus.suspended, updated_at=_NOW - timedelta(minutes=6), waiting_for_batch_id="batch-1")

        reason = _reason(task, waiting_task_terminal=True)

        assert reason is not None and "已结束" in reason


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-reaper"),
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
            input_data={"task_context": {"conversation_key": "test:sup-reaper"}},
            continuation={},
            source_inbound_seq=None,
        )


def _dispatch_batch(state: SupervisorState, caller: Task) -> list[Task]:
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {
            "action": "dispatch",
            "dispatch_batch": [
                {"kind": "node", "target": "a.node", "instruction": "one", "context_mode": "fresh"},
                {"kind": "node", "target": "b.node", "instruction": "two", "context_mode": "fresh"},
            ],
        })
    return sorted(
        (t for t in state.tasks.values() if t.caller_task_id == caller.task_id), key=lambda t: t.batch_index,
    )


class TestReaperWithRealBatches:
    def test_a_live_batch_keeps_its_caller_past_the_old_twenty_minute_cut(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
        caller = _caller(state, sid)
        children = _dispatch_batch(state, caller)
        assert caller.status == TaskStatus.suspended
        assert caller.waiting_for_task_id is None and caller.waiting_for_batch_id == children[0].batch_id
        now = datetime.now(timezone.utc)
        caller.updated_at = now - timedelta(hours=1)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=now)

        assert caller.task_id not in reaped
        assert caller.status == TaskStatus.suspended

    def test_a_legacy_batch_caller_without_the_batch_field_is_also_kept(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
        caller = _caller(state, sid)
        _dispatch_batch(state, caller)
        caller.waiting_for_batch_id = None
        now = datetime.now(timezone.utc)
        caller.updated_at = now - timedelta(hours=1)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=now)

        assert caller.task_id not in reaped

    def test_a_finished_batch_with_a_stuck_caller_is_reaped(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
        caller = _caller(state, sid)
        for child in _dispatch_batch(state, caller):
            child.status = TaskStatus.completed
        now = datetime.now(timezone.utc)
        caller.updated_at = now - timedelta(minutes=6)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=now)

        assert caller.task_id in reaped
        assert caller.status == TaskStatus.failed


class TestReapingIsCancellation:
    def test_the_worker_of_a_reaped_task_is_told_to_stop(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
        task = _caller(state, sid)
        assigned = state.assign_next_task(worker_id="worker-1")
        assert assigned is not None
        claim = str(assigned["claim_id"])
        now = datetime.now(timezone.utc)
        task.lease_expires_at = now - timedelta(minutes=5)

        with state._lock:
            assert reap_stuck_tasks_locked(state, now=now) == [task.task_id]

        assert task.status == TaskStatus.failed and task.cancel_requested
        assert state.is_task_cancelled(task.task_id, claim_id=claim)
        assert state.renew_lease(task.task_id, "worker-1", 120.0, claim_id=claim).reason == "not_running"
        late = state.complete_task(
            task_id=task.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "too late"}}, claim_id=claim,
        )
        assert late is not None and late.outcome is CompletionOutcome.stale
        assert task.result["action"] == "fail"

    def test_reaping_a_caller_cancels_the_children_it_was_waiting_for(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
        caller = _caller(state, sid)
        children = _dispatch_batch(state, caller)
        now = datetime.now(timezone.utc)
        caller.created_at = now - timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR + 60)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=now)

        assert caller.task_id in reaped
        assert caller.status == TaskStatus.failed and caller.cancel_requested
        assert all(child.status == TaskStatus.cancelled for child in children)


def _reaped_while_running(state: SupervisorState) -> tuple[Task, Task, Task, datetime]:
    """批次里一个子任务跑过了运行上限、worker 还拿着有效租约时被回收；另一个已经结束。"""
    sid = state.get_or_create_session(channel="test", conversation_key="test:sup-reaper")
    caller = _caller(state, sid)
    first, second = _dispatch_batch(state, caller)
    now = datetime.now(timezone.utc)
    first.status = TaskStatus.running
    first.worker_id = "worker-1"
    first.claim_id = "claim-1"
    first.claimed_at = now - timedelta(seconds=_MAX + 1)
    first.lease_expires_at = now + timedelta(minutes=2)
    second.status = TaskStatus.completed
    second.result = {"action": "finish", "result": {"text": "second part"}}
    caller.updated_at = now - timedelta(minutes=6)
    with state._lock:
        assert reap_stuck_tasks_locked(state, now=now, session_ids={sid}) == [first.task_id]
    return caller, first, second, now


class TestAReapedChildWhoseWorkerHasNotStopped:
    def test_its_caller_keeps_waiting_and_is_not_taken_for_a_zombie(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller, first, _second, now = _reaped_while_running(state)

        assert first.status == TaskStatus.failed and first.cancel_requested
        assert state._task_ending_locked(first) is TaskEnding.stopping
        assert caller.status == TaskStatus.suspended
        with state._lock:
            assert reap_stuck_tasks_locked(state, now=now, session_ids={caller.session_id}) == []
        assert caller.status == TaskStatus.suspended

        late = state.complete_task(
            task_id=first.task_id, worker_id="worker-1", result={"action": "cancelled"}, claim_id="claim-1",
        )

        assert late is not None and late.outcome is CompletionOutcome.stale
        assert caller.status == TaskStatus.pending
        failed, finished = caller.input["resume_data"]["entries"]
        assert failed["status"] == "fail" and "运行过久" in failed["error"]
        assert finished["text"] == "second part"

    def test_a_worker_that_never_reports_is_given_up_by_a_sweep_after_the_grace(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller, first, _second, now = _reaped_while_running(state)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state, now=now + STOP_GRACE + timedelta(seconds=1))

        assert reaped == []
        assert caller.status == TaskStatus.pending
        assert state._task_ending_locked(first) is TaskEnding.ended
        assert [entry["status"] for entry in caller.input["resume_data"]["entries"]] == ["fail", "finish"]
