"""5 字段 cron 匹配（minute hour day month weekday）。

supervisor 的用户定时任务与 engine 的 dream / data_cleanup 共用这一份，
两边各留一份副本时周几语义已经跑偏过一次。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial

__all__ = ["CRON_FIELDS", "cron_match", "field_values", "unmatched_terms"]

# 各段的名字和 cron_match 会拿来比的取值范围；周几的 7 也是周日
CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))


def _match_step(step_text: str, value: int) -> bool:
    try:
        step = int(step_text)
    except ValueError:
        return False
    return step > 0 and value % step == 0


def _match_part(part: str, value: int) -> bool:
    try:
        if "-" in part:
            lo, hi = part.split("-", 1)
            return int(lo) <= value <= int(hi)
        return int(part) == value
    except ValueError:
        return False


def _terms(field: str) -> list[tuple[str, Callable[[int], bool]]]:
    """一段拆成各自匹配的项：* 和 */step 只认整段写，逗号列表里的每一项只认单个数或 a-b 区间。"""
    field = field.strip()
    if field == "*":
        return [(field, lambda _value: True)]
    if field.startswith("*/"):
        return [(field, partial(_match_step, field[2:]))]
    parts = [part.strip() for part in field.split(",")]
    return [(part, partial(_match_part, part)) for part in parts]


def _match_field(field: str, value: int) -> bool:
    """判断 cron 单个字段是否匹配。支持 * / , - 和 */step。"""
    return any(match(value) for _term, match in _terms(field))


def field_values(field: str, low: int, high: int) -> frozenset[int]:
    """这一段在 [low, high] 里会命中的值，和 cron_match 走同一份解析。"""
    return frozenset(value for value in range(low, high + 1) if _match_field(field, value))


def unmatched_terms(field: str, low: int, high: int) -> list[str]:
    """这一段里在 [low, high] 一个值也命中不了的项，比如 0-59/5 这种认不出的写法、越界的数；多写的逗号留下的空项不算，整段只有逗号时给整段。"""
    span = range(low, high + 1)
    terms = [(term, match) for term, match in _terms(field) if term]
    if not terms:
        return [field]
    return [term for term, match in terms if not any(match(value) for value in span)]


def _match_weekday(field: str, dt: datetime) -> bool:
    # cron 惯例 0=周日，而 datetime.weekday() 0=周一；直接用会整体错一天。
    value = (dt.weekday() + 1) % 7
    if _match_field(field, value):
        return True
    # cron 里 7 也表示周日，range 与 list 写法都可能用到它。
    return value == 0 and _match_field(field, 7)


def cron_match(expr: str, dt: datetime) -> bool:
    """判断 5 字段 cron 表达式是否匹配指定时间。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, dt.minute)
        and _match_field(hour, dt.hour)
        and _match_field(day, dt.day)
        and _match_field(month, dt.month)
        and _match_weekday(weekday, dt)
    )
