"""running 任务的回收：租约过期、心跳沉默太久或运行超过硬上限的任务按失败收尾，走正常的完成路由。"""
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


def reap_reason(task: Task, *, now: datetime, max_running_sec: float, approval_pending: bool) -> str | None:
    """该回收时返回写进 task.result 的原因，不该回收返回 None。"""
    age_sec = (now - task.created_at).total_seconds()
    if age_sec > max_running_sec:
        return f"任务运行过久（{int(age_sec)} 秒，上限 {int(max_running_sec)} 秒），已自动终止以防止僵死阻塞聊天"

    silence = now - task.updated_at
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
    """回收卡死的 running 任务，返回被回收的 task_id。调用方必须持有 state._lock。

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
        if task.status != TaskStatus.running:
            continue
        if scope is not None and task.session_id not in scope:
            continue
        # 分支上跑的任务，审批可能挂在父会话上
        route_session_id = state._route_session_id_for_task_locked(task)
        reason = reap_reason(
            task,
            now=now,
            max_running_sec=max_running_sec,
            approval_pending=task.session_id in waiting_sessions or route_session_id in waiting_sessions,
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
