"""cron 一段会命中哪些值、哪几项永远命中不了：定时任务工具按这两样判频率、拒写错的项，控制台和日志按它提醒，必须和 cron_match 实际命中的一致。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.cron import cron_faults, cron_match, field_values, unmatched_terms  # noqa: E402

_TUESDAY_9AM = datetime(2026, 10, 27, 9, 0)
_SUNDAY_9AM = datetime(2026, 11, 1, 9, 0)


def _fired_minutes(minute_field: str) -> set[int]:
    return {minute for minute in range(60) if cron_match(f"{minute_field} * * * *", _TUESDAY_9AM.replace(minute=minute))}


@pytest.mark.parametrize("field,expected", [
    pytest.param("*", set(range(60)), id="star"),
    pytest.param("*/15", {0, 15, 30, 45}, id="step"),
    pytest.param("*/7", set(range(0, 60, 7)), id="step-restarts-every-hour"),
    pytest.param("*/100", {0}, id="step-longer-than-an-hour"),
    pytest.param("0-3", {0, 1, 2, 3}, id="range"),
    pytest.param("10-12,5", {5, 10, 11, 12}, id="range-and-number"),
    pytest.param("0,,30,", {0, 30}, id="stray-commas"),
    pytest.param("5,*", {5}, id="star-inside-a-list"),
    pytest.param("7,*/5", {7}, id="step-inside-a-list"),
    pytest.param("*/5,7", set(), id="list-after-a-step"),
    pytest.param("0-59/5", set(), id="range-with-a-step"),
    pytest.param("50-10", set(), id="reversed-range"),
    pytest.param("*/0", set(), id="zero-step"),
    pytest.param("60", set(), id="past-59"),
])
def test_field_values_are_the_minutes_cron_match_fires_on(field: str, expected: set[int]) -> None:
    assert field_values(field, 0, 59) == expected
    assert _fired_minutes(field) == expected


@pytest.mark.parametrize("field,low,high,dead", [
    pytest.param("0-59/5", 0, 59, ["0-59/5"], id="range-with-a-step"),
    pytest.param("7,0-59/5", 0, 59, ["0-59/5"], id="one-dead-term-in-a-list"),
    pytest.param("*/5,7", 0, 59, ["*/5,7"], id="list-after-a-step"),
    pytest.param("5,*", 0, 59, ["*"], id="star-inside-a-list"),
    pytest.param("60,5", 0, 59, ["60"], id="past-the-range"),
    pytest.param("*/0", 0, 59, ["*/0"], id="zero-step"),
    pytest.param("*/32", 1, 31, ["*/32"], id="step-longer-than-a-month"),
    pytest.param("0", 1, 31, ["0"], id="day-zero"),
    pytest.param("MON", 0, 7, ["MON"], id="weekday-name"),
    pytest.param("7", 0, 7, [], id="sunday-as-seven"),
    pytest.param("0,,30,", 0, 59, [], id="stray-commas"),
    pytest.param(",,", 0, 59, [",,"], id="only-commas"),
    pytest.param("*", 0, 59, [], id="star"),
    pytest.param("*/5", 0, 59, [], id="step"),
])
def test_unmatched_terms(field: str, low: int, high: int, dead: list[str]) -> None:
    assert unmatched_terms(field, low, high) == dead


@pytest.mark.parametrize("expr,faults", [
    pytest.param("*/5 9 * * 1-5", [], id="fine"),
    pytest.param("0 9 * * 7", [], id="sunday-as-seven"),
    pytest.param("0 9 * *", ["不是「分 时 日 月 周」五段，不会触发"], id="four-fields"),
    pytest.param("", ["不是「分 时 日 月 周」五段，不会触发"], id="empty"),
    pytest.param("0-59/5 9 * * *", ["分钟段的 0-59/5 永远命中不了，整条不会触发"], id="whole-field"),
    pytest.param("7,0-59/5 9 * * *", ["分钟段的 0-59/5 永远命中不了"], id="one-term-of-a-list"),
    pytest.param(
        "0 9 0,32 * MON",
        ["日期段的 0、32 永远命中不了，整条不会触发", "周几段的 MON 永远命中不了，整条不会触发"],
        id="two-fields",
    ),
])
def test_cron_faults(expr: str, faults: list[str]) -> None:
    assert cron_faults(expr) == faults


def test_sunday_fires_as_seven_too() -> None:
    assert cron_match("0 9 * * 7", _SUNDAY_9AM)
    assert cron_match("0 9 * * 0", _SUNDAY_9AM)
    assert not cron_match("0 9 * * 7", _TUESDAY_9AM)
