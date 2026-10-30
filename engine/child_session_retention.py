"""子会话的保留规则：fresh / fork 空闲多久算过期，哪些登记行的历史永久保留，哪些在发起任务结束后再留一段。

supervisor 清登记时按这里判；data_cleanup 不动还登记着的子会话，只按同一个 TTL 删没登记的文件。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clonoth_runtime import get_int

CHILD_SESSION_TTL_KEY = "engine.child_session.ttl_hours"
DEFAULT_CHILD_SESSION_TTL_HOURS = 24
RETAIN_GRACE_KEY = "engine.child_session.retain_grace_hours"
DEFAULT_RETAIN_GRACE_HOURS = 24
# sessions.json 行里的保留记录：{task_id, parent_task_id, reason, since, until, family_root}
RETAIN_FIELD = "retain"


def child_session_ttl_hours(runtime_config: dict[str, Any]) -> int:
    """fresh / fork 子会话空闲多少小时算过期：最少 1，读不出整数按默认的 24。"""
    return get_int(runtime_config, CHILD_SESSION_TTL_KEY, DEFAULT_CHILD_SESSION_TTL_HOURS, min_value=1)


def retain_grace_hours(runtime_config: dict[str, Any]) -> int:
    """没做完就交回的子会话（失败、到点收尾、ask、被软打断），在发起它的任务结束后再留多少小时：最少 1，读不出整数按默认的 24。"""
    return get_int(runtime_config, RETAIN_GRACE_KEY, DEFAULT_RETAIN_GRACE_HOURS, min_value=1)


def is_retained_child_row(row: Any) -> bool:
    """sessions.json 里这一行是没 reset 的 accumulate 子会话：登记和对话历史都永久保留，不按空闲时长清。"""
    if not isinstance(row, Mapping) or not row.get("is_child") or row.get("reset"):
        return False
    return str(row.get("context_mode") or "").strip() == "accumulate"


def retention_hold(row: Any) -> dict[str, Any] | None:
    """这一行的保留记录；没有、已清掉或格式不对给 None。"""
    if not isinstance(row, Mapping) or row.get("reset"):
        return None
    hold = row.get(RETAIN_FIELD)
    if not isinstance(hold, Mapping) or not str(hold.get("task_id") or "").strip():
        return None
    return dict(hold)


def retention_until(hold: Mapping[str, Any]) -> datetime | None:
    """保留到什么时候；发起任务还没结束时没有这一项。"""
    text = str(hold.get("until") or "").strip()
    if not text:
        return None
    try:
        until = datetime.fromisoformat(text)
    except ValueError:
        return None
    return until if until.tzinfo is not None else until.replace(tzinfo=timezone.utc)


def is_held_row(row: Any, *, now: datetime) -> bool:
    """还在保留期里：发起任务没结束（没有截止时刻），或截止时刻还没到。截止时刻读不出来的按还在保留算。"""
    hold = retention_hold(row)
    if hold is None:
        return False
    if not str(hold.get("until") or "").strip():
        return True
    until = retention_until(hold)
    return until is None or until > now


def is_expired_hold(row: Any, *, now: datetime) -> bool:
    """保留期已经过了：截止时刻读得出来，而且不晚于 now。"""
    hold = retention_hold(row)
    if hold is None:
        return False
    until = retention_until(hold)
    return until is not None and until <= now
