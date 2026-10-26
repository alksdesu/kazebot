"""回收卡住的任务，并沿用正常完成路由通知调用方。"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from clonoth_runtime import task_max_running_seconds
from engine.run_budget import TASK_LIFETIME_FACTOR, lifetime_origin

from .file_claims import release_if_ended_locked
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


def _queued_since(task: Task) -> datetime:
    return task.queued_at or task.created_at


def _claimed_since(task: Task) -> datetime:
    return task.claimed_at or task.created_at


def reap_reason(
    task: Task,
    *,
    now: datetime,
    max_running_sec: float,
    approval_pending: bool,
    waiting_task_terminal: bool = False,
    waiting_task_missing: bool = False,
    wait_target_live: bool = False,
    max_lifetime_sec: float | None = None,
) -> str | None:
    """该回收时返回写进 task.result 的原因，不该回收返回 None。

    waiting_* 描述挂起任务的等待目标（单个子任务或整个批次）：全部结束、找不到，或还有没结束的。
    总寿命从续跑链第一个任务创建时算，一条链上的续跑任务不各自重新起算。
    """
    silence = now - task.updated_at
    lifetime_sec = (now - lifetime_origin(task.created_at, task.input)).total_seconds()
    lifetime_limit = max_lifetime_sec if max_lifetime_sec is not None else max_running_sec * TASK_LIFETIME_FACTOR
    if task.status == TaskStatus.pending:
        if lifetime_sec > lifetime_limit:
            return _lifetime_reason(lifetime_sec, lifetime_limit)
        if approval_pending:
            return None
        queued = now - _queued_since(task)
        if queued > PENDING_QUEUE_LIMIT:
            return f"任务排队过久（{_minutes(queued)} 分钟无 worker 接手），已自动终止"
        return None
    if task.status == TaskStatus.suspended:
        if lifetime_sec > lifetime_limit:
            return _lifetime_reason(lifetime_sec, lifetime_limit)
        if approval_pending:
            return None
        if waiting_task_terminal and silence > SUSPENDED_CHILD_GRACE:
            return "等待的子任务已结束但入口未恢复，按僵尸任务回收"
        if waiting_task_missing and silence > SUSPENDED_CHILD_GRACE:
            return "等待的子任务已丢失，按僵尸任务回收"
        has_target = bool(task.waiting_for_task_id or task.waiting_for_batch_id or wait_target_live)
        if not has_target and silence > SUSPENDED_SILENCE_LIMIT:
            return f"任务挂起过久（{_minutes(silence)} 分钟无等待目标），按僵尸任务回收"
        return None
    if task.status != TaskStatus.running:
        return None

    age_sec = (now - _claimed_since(task)).total_seconds()
    if age_sec > max_running_sec:
        return f"任务运行过久（{int(age_sec)} 秒，上限 {int(max_running_sec)} 秒），已自动终止以防止僵死阻塞聊天"
    if lifetime_sec > lifetime_limit:
        return _lifetime_reason(lifetime_sec, lifetime_limit)

    if silence > HEARTBEAT_SILENCE_LIMIT:
        return f"任务运行超时（{_minutes(silence)} 分钟无响应，疑似 worker 崩溃）"

    # 等审批是合法阻塞：租约类判定对它网开一面，上面几条兜底照样生效
    if approval_pending:
        return None

    if task.lease_expires_at is None:
        if silence > MISSING_LEASE_GRACE:
            return f"任务没有租约且 {_minutes(silence)} 分钟无更新，按僵尸任务回收"
        return None
    if task.lease_expires_at + LEASE_GRACE < now:
        return "任务租约过期，worker 已失联，按僵尸任务回收"
    return None


def _wait_target_state(
    state: "SupervisorState", task: Task, children: dict[str, list[Task]],
) -> tuple[bool, bool, bool]:
    """挂起任务等待目标的 (全部结束, 找不到, 还有没结束的)。"""
    if task.status != TaskStatus.suspended:
        return False, False, False
    if task.waiting_for_task_id:
        child = state.tasks.get(task.waiting_for_task_id)
        if child is None:
            return False, True, False
        terminal = state._task_terminal(child)
        return terminal, False, not terminal
    if task.waiting_for_batch_id:
        siblings = [
            child for child in children.get(task.task_id, [])
            if child.batch_id == task.waiting_for_batch_id
        ]
        if not siblings:
            return False, True, False
        live = any(not state._task_terminal(child) for child in siblings)
        return not live, False, live
    # 旧快照里批次挂起不记等待字段：还有没结束的批次子任务就算在等
    live_batch = any(
        child.batch_id and not state._task_terminal(child)
        for child in children.get(task.task_id, [])
    )
    return False, False, live_batch


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
    children: dict[str, list[Task]] = {}
    for candidate in state.tasks.values():
        caller_id = (candidate.caller_task_id or "").strip()
        if caller_id:
            children.setdefault(caller_id, []).append(candidate)

    reaped: list[str] = []
    # 完成路由会增删 state.tasks，先拍快照再遍历
    for task in list(state.tasks.values()):
        if task.status not in {TaskStatus.running, TaskStatus.pending, TaskStatus.suspended}:
            continue
        if scope is not None and task.session_id not in scope:
            continue
        # 分支上跑的任务，审批可能挂在父会话上
        route_session_id = state._route_session_id_for_task_locked(task)
        waiting_terminal, waiting_missing, waiting_live = _wait_target_state(state, task, children)
        reason = reap_reason(
            task,
            now=now,
            max_running_sec=max_running_sec,
            approval_pending=task.session_id in waiting_sessions or route_session_id in waiting_sessions,
            waiting_task_terminal=waiting_terminal,
            waiting_task_missing=waiting_missing,
            wait_target_live=waiting_live,
        )
        if reason is None:
            continue
        _fail_locked(state, task, reason=reason, now=now)
        reaped.append(task.task_id)
    return reaped


def _fail_locked(state: "SupervisorState", task: Task, *, reason: str, now: datetime) -> None:
    """回收即取消：置 cancel_requested 让还在跑的 worker 从续租和取消查询里得知停手，再按失败走完成路由。"""
    log.warning("reaping %s task %s (node=%s): %s", task.status.value, task.task_id[:12], task.node_id, reason)
    state._fail_task_locked(task, error=reason, now=now)
    release_if_ended_locked(state, task)


def _lifetime_reason(lifetime_sec: float, limit_sec: float) -> str:
    return f"任务存在过久（{int(lifetime_sec)} 秒，总上限 {int(limit_sec)} 秒），已自动终止"


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)
