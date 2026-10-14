"""回收卡住的任务，并沿用正常完成路由通知调用方。"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from clonoth_runtime import task_max_running_seconds

from .types import ApprovalStatus, Task, TaskStatus

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

# engine 每 60 秒续一次 120 秒的租约，过期后再宽限一分钟才算失联
LEASE_GRACE = timedelta(seconds=60)
# running 却没有租约只可能是状态没收干净，给出和续租周期同量级的余量
MISSING_LEASE_GRACE = timedelta(minutes=5)
# 续租会顺带刷新 updated_at；十分钟没动静说明 worker 已经不在了，哪怕它还挂着审批
HEARTBEAT_SILENCE_LIMIT = timedelta(minutes=10)
# 队列长时间没有被 worker 接走时，避免 pending 任务永久占据会话。
PENDING_QUEUE_LIMIT = timedelta(minutes=15)
# 没有合法等待目标的 suspended 任务，超过这个时间按丢失的等待链收尾。
SUSPENDED_SILENCE_LIMIT = timedelta(minutes=20)
# 子任务已终态但 caller 仍挂起时，给路由线程一小段完成收尾的时间。
SUSPENDED_CHILD_GRACE = timedelta(minutes=5)


def reap_reason(
    task: Task,
    *,
    now: datetime,
    max_running_sec: float,
    approval_pending: bool,
    waiting_task_terminal: bool = False,
    waiting_task_missing: bool = False,
) -> str | None:
    """该回收时返回写进 task.result 的原因，不该回收返回 None。"""
    silence = now - task.updated_at
    if task.status == TaskStatus.pending:
        if approval_pending:
            return None
        age = now - task.created_at
        if age > PENDING_QUEUE_LIMIT:
            return f"任务排队过久（{_minutes(age)} 分钟无 worker 接手），已自动终止"
        return None
    if task.status == TaskStatus.suspended:
        if approval_pending:
            return None
        if waiting_task_terminal and silence > SUSPENDED_CHILD_GRACE:
            return "等待的子任务已结束但入口未恢复，按僵尸任务回收"
        if waiting_task_missing and silence > SUSPENDED_CHILD_GRACE:
            return "等待的子任务已丢失，按僵尸任务回收"
        if not task.waiting_for_task_id and silence > SUSPENDED_SILENCE_LIMIT:
            return f"任务挂起过久（{_minutes(silence)} 分钟无等待目标），按僵尸任务回收"
        return None
    if task.status != TaskStatus.running:
        return None

    age_sec = (now - task.created_at).total_seconds()
    if age_sec > max_running_sec:
        return f"任务运行过久（{int(age_sec)} 秒，上限 {int(max_running_sec)} 秒），已自动终止以防止僵死阻塞聊天"

    if silence > HEARTBEAT_SILENCE_LIMIT:
        return f"任务运行超时（{_minutes(silence)} 分钟无响应，疑似 worker 崩溃）"

    # 等审批是合法阻塞：租约类判定对它网开一面，上面两条兜底照样生效
    if approval_pending:
        return None

    if task.lease_expires_at is None:
        if silence > MISSING_LEASE_GRACE:
            return f"任务没有租约且 {_minutes(silence)} 分钟无更新，按僵尸任务回收"
        return None
    if task.lease_expires_at + LEASE_GRACE < now:
        return "任务租约过期，worker 已失联，按僵尸任务回收"
    return None


def reap_stuck_tasks_locked(
    state: "SupervisorState",
    *,
    now: datetime | None = None,
    session_ids: Iterable[str] | None = None,
) -> list[str]:
    """回收卡住的任务，返回被回收的 task_id。调用方必须持有 state._lock。

    给了 session_ids 就只看这些会话里的任务。
    """
    now = now or datetime.now(timezone.utc)
    max_running_sec = task_max_running_seconds()
    scope = set(session_ids) if session_ids is not None else None
    waiting_sessions = {
        approval.session_id
        for approval in state.approvals.values()
        if approval.status == ApprovalStatus.pending
    }

    reaped: list[str] = []
    # 完成路由会增删 state.tasks，先拍快照再遍历
    for task in list(state.tasks.values()):
        if task.status not in {TaskStatus.running, TaskStatus.pending, TaskStatus.suspended}:
            continue
        if scope is not None and task.session_id not in scope:
            continue
        # 分支上跑的任务，审批可能挂在父会话上
        route_session_id = state._route_session_id_for_task_locked(task)
        waiting_task = state.tasks.get(task.waiting_for_task_id or "")
        reason = reap_reason(
            task,
            now=now,
            max_running_sec=max_running_sec,
            approval_pending=task.session_id in waiting_sessions or route_session_id in waiting_sessions,
            waiting_task_terminal=bool(
                task.status == TaskStatus.suspended
                and task.waiting_for_task_id
                and waiting_task is not None
                and state._task_terminal(waiting_task)
            ),
            waiting_task_missing=bool(
                task.status == TaskStatus.suspended
                and task.waiting_for_task_id
                and waiting_task is None
            ),
        )
        if reason is None:
            continue
        _fail_locked(state, task, reason=reason, now=now)
        reaped.append(task.task_id)
    return reaped


def _fail_locked(state: "SupervisorState", task: Task, *, reason: str, now: datetime) -> None:
    task.status = TaskStatus.failed
    task.updated_at = now
    task.lease_expires_at = None
    task.result = {"action": "fail", "node_id": task.node_id or "", "error": reason}
    state._reset_task_route_state_locked(task)
    state._event_task_snapshot("task_completed", task, component="supervisor")
    # 分支任务失败也要合并回父会话、给用户发错误，和正常完成走同一条路
    state._route_completed_task_locked(task)
    log.warning("reaped running task %s (node=%s): %s", task.task_id[:12], task.node_id, reason)


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)
