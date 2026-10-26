"""子会话的保留规则：fresh / fork 空闲多久算过期，哪些登记行的历史永久保留。

supervisor 清登记、data_cleanup 删文件都按这里判；两边各写一份，就会一边留着登记、一边把历史删掉。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clonoth_runtime import get_int

CHILD_SESSION_TTL_KEY = "engine.child_session.ttl_hours"
DEFAULT_CHILD_SESSION_TTL_HOURS = 24


def child_session_ttl_hours(runtime_config: dict[str, Any]) -> int:
    """fresh / fork 子会话空闲多少小时算过期：最少 1，读不出整数按默认的 24。"""
    return get_int(runtime_config, CHILD_SESSION_TTL_KEY, DEFAULT_CHILD_SESSION_TTL_HOURS, min_value=1)


def is_retained_child_row(row: Any) -> bool:
    """sessions.json 里这一行是没 reset 的 accumulate 子会话：登记和对话历史都永久保留，不按空闲时长清。"""
    if not isinstance(row, Mapping) or not row.get("is_child") or row.get("reset"):
        return False
    return str(row.get("context_mode") or "").strip() == "accumulate"
