"""5 字段 cron 匹配（minute hour day month weekday）。

supervisor 的用户定时任务与 engine 的 dream / data_cleanup 共用这一份，
两边各留一份副本时周几语义已经跑偏过一次。
"""
from __future__ import annotations

from datetime import datetime

__all__ = ["cron_match"]


def _match_field(field: str, value: int) -> bool:
    """判断 cron 单个字段是否匹配。支持 * / , - 和 */step。"""
    field = field.strip()
    if field == "*":
        return True

    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False

    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue

    return False


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
