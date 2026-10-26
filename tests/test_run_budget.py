"""一段运行的预算：时间串解析、总寿命起点跟着续跑链走、收尾比例的读法、累计步数和收尾说明的读法、按领取时刻算出的收尾时刻。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.run_budget import (  # noqa: E402
    AUTO_CONTINUE_KEY,
    CHAIN_STARTED_AT_KEY,
    STEP_COUNT_RESULT_KEY,
    STEPS_USED_KEY,
    TASK_LIFETIME_FACTOR,
    RunBudget,
    lifetime_limit_seconds,
    lifetime_origin,
    parse_timestamp,
    reported_step_count,
    steps_used,
    wrapup_info,
    wrapup_reserve_seconds,
    wrapup_time_ratio,
)

_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
_MAX = 3600.0


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _chain(started_at: Any) -> dict[str, Any]:
    return {AUTO_CONTINUE_KEY: {"round": 2, CHAIN_STARTED_AT_KEY: started_at}}


class TestParseTimestamp:
    @pytest.mark.parametrize("raw", [None, "", "   ", "yesterday", 12])
    def test_unreadable_values_are_none(self, raw: Any) -> None:
        assert parse_timestamp(raw) is None

    def test_a_z_suffix_is_utc(self) -> None:
        assert parse_timestamp("2026-03-01T12:00:00Z") == _NOW

    def test_text_without_a_zone_is_utc(self) -> None:
        assert parse_timestamp("2026-03-01T12:00:00") == _NOW

    def test_an_offset_is_kept(self) -> None:
        parsed = parse_timestamp("2026-03-01T20:00:00+08:00")

        assert parsed == _NOW and parsed.utcoffset() == timedelta(hours=8)

    def test_a_naive_datetime_is_utc_and_an_aware_one_is_untouched(self) -> None:
        assert parse_timestamp(datetime(2026, 3, 1, 12, 0)) == _NOW
        assert parse_timestamp(_NOW) is _NOW


class TestLifetimeOrigin:
    def test_a_task_outside_a_chain_counts_from_its_creation(self) -> None:
        assert lifetime_origin(_NOW, {}) == _NOW
        assert lifetime_origin(_NOW, None) == _NOW

    def test_a_continuation_counts_from_the_start_of_its_chain(self) -> None:
        started = _NOW - timedelta(hours=2)

        assert lifetime_origin(_NOW, _chain(started.isoformat())) == started

    @pytest.mark.parametrize("raw", [
        pytest.param("garbage", id="unreadable"),
        pytest.param("", id="empty"),
        pytest.param((_NOW + timedelta(minutes=5)).isoformat(), id="after-the-task"),
    ])
    def test_a_chain_start_that_cannot_be_trusted_falls_back_to_the_creation(self, raw: str) -> None:
        assert lifetime_origin(_NOW, _chain(raw)) == _NOW

    @pytest.mark.parametrize("task_input", [
        pytest.param({AUTO_CONTINUE_KEY: "round 2"}, id="chain-not-a-mapping"),
        pytest.param({AUTO_CONTINUE_KEY: None}, id="chain-null"),
        pytest.param("not a mapping", id="input-not-a-mapping"),
    ])
    def test_odd_inputs_fall_back_to_the_creation(self, task_input: Any) -> None:
        assert lifetime_origin(_NOW, task_input) == _NOW

    def test_the_lifetime_is_a_multiple_of_the_run_limit(self) -> None:
        assert lifetime_limit_seconds(_MAX) == _MAX * TASK_LIFETIME_FACTOR


class TestWrapupRatio:
    @pytest.mark.parametrize("config,ratio", [
        pytest.param({}, 0.9, id="unset"),
        pytest.param(None, 0.9, id="not-a-dict"),
        pytest.param({"engine": {"wrapup": {"time_ratio": 0.8}}}, 0.8, id="configured"),
        pytest.param({"engine": {"wrapup": {"time_ratio": "0.75"}}}, 0.75, id="quoted"),
        pytest.param({"engine": {"wrapup": {"time_ratio": 0.1}}}, 0.5, id="too-small"),
        pytest.param({"engine": {"wrapup": {"time_ratio": 1.5}}}, 0.99, id="too-large"),
        pytest.param({"engine": {"wrapup": {"time_ratio": "abc"}}}, 0.9, id="not-a-number"),
    ])
    def test_the_ratio_is_read_and_clamped(self, config: Any, ratio: float) -> None:
        assert wrapup_time_ratio(config) == pytest.approx(ratio)

    def test_the_reserve_is_what_the_ratio_leaves_over(self) -> None:
        assert wrapup_reserve_seconds(_MAX, 0.9) == pytest.approx(360.0)
        assert wrapup_reserve_seconds(_MAX, 1.0) == 0.0
        assert wrapup_reserve_seconds(_MAX, 1.2) == 0.0


class TestStepCounts:
    @pytest.mark.parametrize("task_input,expected", [
        pytest.param(None, 0, id="none"),
        pytest.param({}, 0, id="missing"),
        pytest.param({STEPS_USED_KEY: 5}, 5, id="number"),
        pytest.param({STEPS_USED_KEY: "7"}, 7, id="text"),
        pytest.param({STEPS_USED_KEY: -3}, 0, id="negative"),
        pytest.param({STEPS_USED_KEY: "abc"}, 0, id="unreadable"),
    ])
    def test_steps_used_before_this_segment(self, task_input: Any, expected: int) -> None:
        assert steps_used(task_input) == expected

    @pytest.mark.parametrize("result,expected", [
        pytest.param(None, 0, id="none"),
        pytest.param({}, 0, id="missing"),
        pytest.param({STEP_COUNT_RESULT_KEY: 12}, 12, id="number"),
        pytest.param({STEP_COUNT_RESULT_KEY: "4"}, 4, id="text"),
        pytest.param({STEP_COUNT_RESULT_KEY: -1}, 0, id="negative"),
        pytest.param({STEP_COUNT_RESULT_KEY: [1]}, 0, id="unreadable"),
    ])
    def test_the_step_count_the_engine_reported(self, result: Any, expected: int) -> None:
        assert reported_step_count(result) == expected


class TestWrapupInfo:
    @pytest.mark.parametrize("result", [
        pytest.param(None, id="none"),
        pytest.param({"text": "done"}, id="no-flag"),
        pytest.param({"step_limit_reached": "true", "wrapup": {"reason": "steps"}}, id="flag-not-true"),
    ])
    def test_only_a_wrap_up_carries_it(self, result: Any) -> None:
        assert wrapup_info(result) is None

    def test_the_reason_comes_from_the_flag_when_missing(self) -> None:
        assert wrapup_info({"step_limit_reached": True}) == {"reason": "steps"}
        assert wrapup_info({"time_limit_reached": True}) == {"reason": "time"}

    def test_an_unknown_reason_is_replaced(self) -> None:
        info = wrapup_info({"time_limit_reached": True, "wrapup": {"reason": "bored", "steps_used": 9}})

        assert info == {"reason": "time", "steps_used": 9}

    def test_the_info_is_a_copy(self) -> None:
        raw = {"reason": "steps", "todos": []}
        info = wrapup_info({"step_limit_reached": True, "wrapup": raw})
        info["reason"] = "changed"

        assert raw["reason"] == "steps"


class TestRunBudget:
    def test_due_and_elapsed_follow_the_clock(self) -> None:
        clock = _Clock(100.0)
        budget = RunBudget(deadline=150.0, started=40.0, clock=clock)

        assert budget.due() is False and budget.elapsed_seconds() == 60.0
        clock.now = 150.0
        assert budget.due() is True and budget.elapsed_seconds() == 110.0

    def test_elapsed_never_goes_negative(self) -> None:
        assert RunBudget(deadline=10.0, started=50.0, clock=_Clock(20.0)).elapsed_seconds() == 0.0

    def test_a_fresh_claim_wraps_up_at_the_ratio_of_the_run_limit(self) -> None:
        clock = _Clock()
        claimed = _NOW - timedelta(minutes=10)
        task = {"claimed_at": claimed.isoformat(), "created_at": claimed.isoformat(), "input": {}}

        budget = RunBudget.for_task(task, max_running_sec=_MAX, ratio=0.9, now=_NOW, clock=clock)

        # 领取后 3600 秒到上限，留 360 秒收尾，已经过去 600 秒
        assert budget.deadline == pytest.approx(clock.now + 3240 - 600)
        assert budget.elapsed_seconds() == pytest.approx(600)
        assert budget.due() is False

    def test_an_old_task_is_cut_short_by_its_lifetime(self) -> None:
        clock = _Clock()
        task = {
            "claimed_at": _NOW.isoformat(),
            "created_at": (_NOW - timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR - 360)).isoformat(),
            "input": {},
        }

        budget = RunBudget.for_task(task, max_running_sec=_MAX, ratio=0.9, now=_NOW, clock=clock)

        assert budget.deadline == pytest.approx(clock.now)
        assert budget.due() is True

    def test_a_continuation_shares_the_lifetime_of_its_chain(self) -> None:
        clock = _Clock()
        chain_start = _NOW - timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR)
        task = {
            "claimed_at": _NOW.isoformat(),
            "created_at": (_NOW - timedelta(seconds=5)).isoformat(),
            "input": _chain(chain_start.isoformat()),
        }

        budget = RunBudget.for_task(task, max_running_sec=_MAX, ratio=0.9, now=_NOW, clock=clock)

        assert budget.deadline == pytest.approx(clock.now - 360)
        assert budget.due() is True

    def test_missing_timestamps_count_from_now(self) -> None:
        clock = _Clock()

        budget = RunBudget.for_task({}, max_running_sec=_MAX, ratio=0.9, now=_NOW, clock=clock)

        assert budget.deadline == pytest.approx(clock.now + 3240)
        assert budget.elapsed_seconds() == 0.0

    def test_the_wall_clock_only_sets_the_deadline_once(self) -> None:
        # 算出来以后只看单调时钟，系统时间回拨不影响
        clock = _Clock()
        task = {"claimed_at": _NOW.isoformat(), "created_at": _NOW.isoformat(), "input": {}}
        budget = RunBudget.for_task(task, max_running_sec=_MAX, ratio=0.9, now=_NOW, clock=clock)

        clock.now += 3239
        assert budget.due() is False
        clock.now += 1
        assert budget.due() is True
