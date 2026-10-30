"""任务到了终态不等于真正结束：交回的委派还没路由完、结局还没路由完、被判失败的 worker 还没停手。

谁要问「结束了没有」都从 task_ending() 取分类，再按自己的问题用 continues / still_writing / result_open。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from engine.run_budget import parse_timestamp

from .types import RouteStatus, Task, TaskStatus

# 交回 dispatch 或结局之后路由一直没跑完的，过了这段时间就不再等它
TRANSIT_GRACE = timedelta(minutes=10)
# 和回收线程判 worker 失联同一个口径：租约 120 秒，过期后再宽限 60 秒
STOP_GRACE = timedelta(minutes=3)

STOPPING_KEY = "_stopping_worker"

_TERMINAL = frozenset({TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled})


class TaskEnding(str, Enum):
    live = "live"
    dispatching = "dispatching"
    dispatch_failed = "dispatch_failed"
    stopping = "stopping"
    settling = "settling"
    ended = "ended"

    @property
    def continues(self) -> bool:
        """还会接着跑：没到终态，或交回的委派还可能被路由出去、挂起后接着做。"""
        return self in (TaskEnding.live, TaskEnding.dispatching, TaskEnding.dispatch_failed)

    @property
    def still_writing(self) -> bool:
        """还可能写它的会话和文件：会接着跑的，加上被判失败、旧 worker 还没停手的。"""
        return self.continues or self is TaskEnding.stopping

    @property
    def result_open(self) -> bool:
        """交回调用方的那一项还会变：在跑、委派正在路由、worker 还没停手，或结局还没路由完。"""
        return self in (TaskEnding.live, TaskEnding.dispatching, TaskEnding.stopping, TaskEnding.settling)


def task_ending(task: Task, now: datetime) -> TaskEnding:
    if task.status not in _TERMINAL:
        return TaskEnding.live
    since = stopping_since(task)
    if since is not None and now - since < STOP_GRACE:
        return TaskEnding.stopping
    within_grace = now - task.updated_at < TRANSIT_GRACE
    action = str((task.result or {}).get("action") or "").strip()
    if (
        task.status == TaskStatus.completed
        and action == "dispatch"
        and task.route_status != RouteStatus.routed
        and within_grace
    ):
        if task.route_status != RouteStatus.failed:
            return TaskEnding.dispatching
        # 路由失败后只有重推能把它救回来；已经叫停的，重推时也只会按取消收掉
        return TaskEnding.ended if task.cancel_requested else TaskEnding.dispatch_failed
    # 从没完成过就被直接取消的任务 route_status 也是 pending，但没有 delivery_id，不会再有路由
    if task.route_status == RouteStatus.pending and task.delivery_id and within_grace:
        return TaskEnding.settling
    return TaskEnding.ended


def transit_due(task: Task, ending: TaskEnding) -> datetime | None:
    """过渡段最晚到什么时候：到点还没等来路由或回报，就按结束处理。不在过渡段的给 None。"""
    if ending is TaskEnding.stopping:
        since = stopping_since(task)
        return since + STOP_GRACE if since is not None else None
    if ending in (TaskEnding.dispatching, TaskEnding.dispatch_failed, TaskEnding.settling):
        return task.updated_at + TRANSIT_GRACE
    return None


def mark_stopping(task: Task, *, claim_id: str, now: datetime) -> None:
    task.input[STOPPING_KEY] = {"claim_id": claim_id, "since": now.isoformat()}


def stopping_claim(task: Task) -> str:
    marker = task.input.get(STOPPING_KEY)
    return str(marker.get("claim_id") or "").strip() if isinstance(marker, dict) else ""


def stopping_since(task: Task) -> datetime | None:
    marker = task.input.get(STOPPING_KEY)
    if not isinstance(marker, dict):
        return None
    return parse_timestamp(marker.get("since"))


def stopping_expired(task: Task, now: datetime) -> bool:
    """有停手记号，但旧 worker 过了 STOP_GRACE 还没回报：按它已经不在了处理。"""
    if not isinstance(task.input.get(STOPPING_KEY), dict):
        return False
    since = stopping_since(task)
    return since is None or now - since >= STOP_GRACE


def clear_stopping(task: Task, claim_id: str | None = None) -> bool:
    """拿掉停手记号，返回拿掉了没有。给了 claim_id 时只认那一次领取的回报。"""
    if not isinstance(task.input.get(STOPPING_KEY), dict):
        return False
    if claim_id is not None and stopping_claim(task) != claim_id:
        return False
    task.input.pop(STOPPING_KEY, None)
    return True
