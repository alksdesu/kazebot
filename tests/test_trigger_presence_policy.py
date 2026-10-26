"""在场 / 离场、安静等待、概率门和主动回复密度的纯判定。

判定只在 trigger_policy 写一份，试听和适配层共用；这里按时间线把状态机逐格走一遍，骰子全部注入。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import trigger_policy as tp  # noqa: E402

GROUP = "70001"
OTHER_GROUP = "70002"


def _config(**overrides: Any) -> tp.TriggerConfig:
    values: dict[str, Any] = {
        "llm_intent": True,
        "presence": True,
        "presence_window_sec": 90.0,
        "absent_judge_probability": 0.1,
        "quiet_wait_sec": 4.0,
        "quiet_max_wait_sec": 20.0,
        "proactive_max_replies": 3,
        "proactive_window_sec": 300.0,
    }
    values.update(overrides)
    return tp.TriggerConfig(**values)


def _msg(
    text: str = "随便聊聊",
    *,
    now: float = 0.0,
    roll: float = 0.99,
    at_me: bool = False,
    reply_to_bot: bool = False,
    user: str = "10001",
    group: str = GROUP,
) -> tp.TriggerInput:
    return tp.TriggerInput(
        text=text, group_id=group, user_id=user, at_me=at_me, reply_to_bot=reply_to_bot, now=now, roll=roll,
    )


class TestPresenceWindow:
    def test_a_group_starts_absent(self) -> None:
        state = tp.PresenceState()

        assert state.is_present(GROUP, 0.0, 90.0) is False
        assert state.present_until(GROUP, 90.0) is None

    def test_being_called_enters_and_the_window_runs_out(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 10.0)

        assert state.is_present(GROUP, 99.9, 90.0) is True
        assert state.is_present(GROUP, 100.0, 90.0) is False
        assert state.present_until(GROUP, 90.0) == pytest.approx(100.0)

    def test_a_new_entry_renews_the_window(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 10.0)
        state.enter(GROUP, 50.0)

        assert state.is_present(GROUP, 139.0, 90.0) is True
        assert state.is_present(GROUP, 140.0, 90.0) is False

    def test_a_late_event_does_not_rewind_the_window(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 50.0)
        state.enter(GROUP, 10.0)

        assert state.present_until(GROUP, 90.0) == pytest.approx(140.0)

    def test_bot_speech_counts_as_an_entry(self) -> None:
        state = tp.PresenceState()
        state.bot_spoke(GROUP, 30.0)

        assert state.is_present(GROUP, 100.0, 90.0) is True
        assert state.since_bot_spoke(GROUP, 45.0) == pytest.approx(15.0)

    def test_a_group_where_the_bot_never_spoke_has_no_age(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 30.0)

        assert state.since_bot_spoke(GROUP, 45.0) is None

    def test_a_zero_window_keeps_the_group_absent(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 10.0)

        assert state.is_present(GROUP, 10.0, 0.0) is False
        assert state.present_until(GROUP, 0.0) is None

    def test_groups_are_independent(self) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 10.0)

        assert state.is_present(OTHER_GROUP, 20.0, 90.0) is False

    def test_numeric_and_text_group_ids_are_the_same_group(self) -> None:
        state = tp.PresenceState()
        state.enter(int(GROUP), 10.0)

        assert state.is_present(GROUP, 20.0, 90.0) is True

    def test_forgetting_a_group_starts_it_over(self) -> None:
        state = tp.PresenceState()
        state.bot_spoke(GROUP, 10.0)
        state.record_proactive(GROUP, 11.0)
        state.join_quiet(GROUP, 12.0, wait_sec=4.0, max_wait_sec=20.0)
        state.enter(OTHER_GROUP, 10.0)

        state.forget_group(GROUP)

        assert state.is_present(GROUP, 13.0, 90.0) is False
        assert state.since_bot_spoke(GROUP, 13.0) is None
        assert state.proactive_count(GROUP, 13.0, 300.0) == 0
        assert state.quiet(GROUP) is None
        assert state.is_present(OTHER_GROUP, 13.0, 90.0) is True

    def test_forgetting_everything_clears_every_group(self) -> None:
        state = tp.PresenceState()
        for group in (GROUP, OTHER_GROUP):
            state.enter(group, 10.0)
            state.record_proactive(group, 10.0)

        state.forget_all()

        assert not any(state.is_present(group, 11.0, 90.0) for group in (GROUP, OTHER_GROUP))
        assert all(state.proactive_count(group, 11.0, 300.0) == 0 for group in (GROUP, OTHER_GROUP))


class TestQuietWaitCycles:
    def test_each_message_restarts_the_timer_until_the_cap(self) -> None:
        state = tp.PresenceState()
        deadlines = []
        for now in (0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 19.0):
            deadlines.append(state.join_quiet(GROUP, now, wait_sec=4.0, max_wait_sec=20.0).deadline)

        assert deadlines == [4.0, 7.0, 10.0, 13.0, 16.0, 19.0, 20.0, 20.0]
        assert state.quiet(GROUP).count == 8

    def test_a_join_after_the_deadline_opens_a_new_cycle(self) -> None:
        state = tp.PresenceState()
        first = state.join_quiet(GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)
        second = state.join_quiet(GROUP, 4.0, wait_sec=4.0, max_wait_sec=20.0)

        assert second.cycle != first.cycle
        assert (second.started, second.count) == (4.0, 1)

    def test_closing_hands_the_cycle_over_once(self) -> None:
        state = tp.PresenceState()
        wait = state.join_quiet(GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)

        assert state.close_quiet(GROUP, wait.cycle + 1) is None
        assert state.close_quiet(GROUP, wait.cycle) == wait
        assert state.close_quiet(GROUP, wait.cycle) is None
        assert state.quiet(GROUP) is None

    def test_cancelling_drops_the_cycle(self) -> None:
        state = tp.PresenceState()
        wait = state.join_quiet(GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)

        assert state.cancel_quiet(GROUP) == wait
        assert state.quiet(GROUP) is None
        assert state.cancel_quiet(GROUP) is None

    def test_a_hot_reload_applies_from_the_next_message(self) -> None:
        state = tp.PresenceState()
        state.join_quiet(GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)
        wait = state.join_quiet(GROUP, 2.0, wait_sec=10.0, max_wait_sec=30.0)

        assert (wait.wait_sec, wait.max_wait_sec, wait.deadline) == (10.0, 30.0, 12.0)

    def test_negative_durations_count_as_zero(self) -> None:
        wait = tp.QuietWait(cycle=1, started=5.0, last=8.0, count=2, wait_sec=-3.0, max_wait_sec=-1.0)

        assert wait.deadline == 5.0

    def test_cycles_of_different_groups_do_not_collide(self) -> None:
        state = tp.PresenceState()
        first = state.join_quiet(GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)
        other = state.join_quiet(OTHER_GROUP, 0.0, wait_sec=4.0, max_wait_sec=20.0)

        assert first.cycle != other.cycle
        assert state.close_quiet(GROUP, other.cycle) is None


class TestDensityBookkeeping:
    def test_only_replies_inside_the_window_count(self) -> None:
        state = tp.PresenceState()
        config = _config(proactive_max_replies=3, proactive_window_sec=300.0)
        for now in (0.0, 100.0, 200.0):
            state.record_proactive(GROUP, now)

        assert state.density_left(GROUP, 250.0, config) == 0
        assert state.density_left(GROUP, 300.0, config) == 1

    def test_zero_means_no_limit(self) -> None:
        state = tp.PresenceState()
        state.record_proactive(GROUP, 0.0)

        assert state.density_left(GROUP, 1.0, _config(proactive_max_replies=0)) is None

    def test_a_refund_removes_exactly_one_stamp(self) -> None:
        state = tp.PresenceState()
        state.record_proactive(GROUP, 5.0)
        stamp = state.record_proactive(GROUP, 5.0)

        assert state.refund_proactive(GROUP, stamp) is True
        assert state.proactive_count(GROUP, 6.0, 300.0) == 1
        assert state.refund_proactive(GROUP, 99.0) is False
        assert state.refund_proactive(OTHER_GROUP, stamp) is False

    def test_the_history_is_bounded(self) -> None:
        state = tp.PresenceState()
        for index in range(tp._PROACTIVE_HISTORY_MAX + 40):
            state.record_proactive(GROUP, float(index))

        assert state.proactive_count(GROUP, 1000.0, 10_000.0) == tp._PROACTIVE_HISTORY_MAX


class TestAbsent:
    def test_a_hit_on_the_gate_goes_to_the_model(self) -> None:
        decision = tp.evaluate(_msg(roll=0.05), _config(), presence=tp.PresenceState())

        assert decision.triggered is False and decision.undetermined is True
        assert decision.signal == tp.SIGNAL_LLM_INTENT
        assert (decision.presence, decision.gate) == (tp.PRESENCE_ABSENT, tp.GATE_PASSED)
        assert decision.gate_probability == pytest.approx(0.1)
        assert decision.awaits_llm_intent() is True and decision.awaits_quiet() is False
        assert decision.source == tp.SIGNAL_LLM_INTENT

    def test_a_miss_only_goes_into_the_history(self) -> None:
        decision = tp.evaluate(_msg(roll=0.5), _config(), presence=tp.PresenceState())

        assert decision.triggered is False and decision.undetermined is False
        assert (decision.blocked_by, decision.gate) == (tp.BLOCKED_BY_GATE, tp.GATE_MISSED)
        assert decision.awaits_llm_intent() is False and decision.awaits_quiet() is False

    def test_a_roll_on_the_boundary_misses(self) -> None:
        decision = tp.evaluate(_msg(roll=0.1), _config(), presence=tp.PresenceState())

        assert decision.gate == tp.GATE_MISSED

    def test_a_zero_gate_never_asks(self) -> None:
        decision = tp.evaluate(_msg(roll=0.0), _config(absent_judge_probability=0.0), presence=tp.PresenceState())

        assert (decision.blocked_by, decision.gate) == (tp.BLOCKED_BY_GATE, tp.GATE_MISSED)

    def test_the_preview_does_not_roll(self) -> None:
        decision = tp.evaluate(_msg(roll=0.0), _config(), presence=tp.PresenceState(), resolve_random=False)

        assert decision.gate == tp.GATE_PENDING and decision.undetermined is True
        assert decision.blocked_by == ""

    def test_random_folds_into_the_gate(self) -> None:
        config = _config(random=True, random_probability=0.5, absent_judge_probability=0.1)

        decision = tp.evaluate(_msg(roll=0.3), config, presence=tp.PresenceState())

        assert decision.signal == tp.SIGNAL_LLM_INTENT and decision.triggered is False
        assert (decision.gate, decision.gate_probability) == (tp.GATE_PASSED, pytest.approx(0.5))

    def test_the_bigger_of_the_two_probabilities_wins(self) -> None:
        config = _config(random=True, random_probability=0.02, absent_judge_probability=0.3)

        decision = tp.evaluate(_msg(roll=0.2), config, presence=tp.PresenceState())

        assert (decision.gate, decision.gate_probability) == (tp.GATE_PASSED, pytest.approx(0.3))

    def test_random_never_replies_directly_in_a_presence_group(self) -> None:
        config = _config(random=True, random_probability=1.0)

        decision = tp.evaluate(_msg(roll=0.0), config, presence=tp.PresenceState())

        assert decision.triggered is False and decision.signal != tp.SIGNAL_RANDOM

    def test_without_presence_random_still_replies_directly(self) -> None:
        config = _config(presence=False, random=True, random_probability=0.5)

        decision = tp.evaluate(_msg(roll=0.1), config, presence=tp.PresenceState())

        assert decision.triggered is True and decision.signal == tp.SIGNAL_RANDOM
        assert (decision.presence, decision.gate, decision.quiet) == ("", "", "")


class TestPresent:
    @staticmethod
    def _present(now: float = 0.0) -> tp.PresenceState:
        state = tp.PresenceState()
        state.enter(GROUP, now)
        return state

    def test_a_present_group_waits_instead_of_asking(self) -> None:
        decision = tp.evaluate(_msg(now=10.0), _config(), presence=self._present())

        assert decision.quiet == tp.QUIET_WAITING and decision.presence == tp.PRESENCE_PRESENT
        assert decision.awaits_quiet() is True and decision.awaits_llm_intent() is False
        assert decision.gate == "" and decision.undetermined is True

    def test_the_wait_does_not_depend_on_the_dice(self) -> None:
        for roll in (0.0, 0.5, 0.999):
            assert tp.evaluate(_msg(now=10.0, roll=roll), _config(), presence=self._present()).awaits_quiet()

    def test_presence_ends_with_the_window(self) -> None:
        decision = tp.evaluate(_msg(now=95.0, roll=0.5), _config(), presence=self._present())

        assert decision.presence == tp.PRESENCE_ABSENT and decision.gate == tp.GATE_MISSED

    @pytest.mark.parametrize("flags,source", [({"at_me": True}, tp.SIGNAL_AT), ({"reply_to_bot": True}, tp.SIGNAL_REPLY)])
    def test_being_called_never_waits(self, flags: dict[str, bool], source: str) -> None:
        decision = tp.evaluate(_msg(now=10.0, **flags), _config(), presence=self._present())

        assert decision.triggered is True and decision.quiet == ""
        assert decision.source == source and decision.explicit is True

    def test_a_name_counts_only_when_the_name_signal_hits(self) -> None:
        on = tp.evaluate(_msg("咪啪你好", now=10.0), _config(name=True, name_words=("咪啪",)), presence=self._present())
        off = tp.evaluate(_msg("咪啪你好", now=10.0), _config(name=False, name_words=("咪啪",)), presence=self._present())

        assert on.triggered is True and on.source == tp.SIGNAL_NAME
        assert off.awaits_quiet() is True and off.source == tp.SIGNAL_LLM_INTENT

    def test_a_bang_is_tone_while_the_prefix_signal_is_off(self) -> None:
        on = tp.evaluate(_msg("！离谱", now=10.0), _config(prefix=True, prefix_words=("！",)), presence=self._present())
        off = tp.evaluate(_msg("！离谱", now=10.0), _config(prefix=False, prefix_words=("！",)), presence=self._present())

        assert on.triggered is True and on.source == tp.SIGNAL_PREFIX
        assert off.awaits_quiet() is True and off.source == tp.SIGNAL_LLM_INTENT

    def test_an_at_with_the_at_signal_off_is_judged_at_once(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, at_me=True), _config(at=False), presence=self._present())

        assert decision.source == tp.SIGNAL_AT
        assert decision.awaits_llm_intent() is True
        assert (decision.quiet, decision.gate) == ("", "")

    def test_an_absent_at_with_the_signal_off_skips_the_gate(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, roll=0.99, at_me=True), _config(at=False), presence=tp.PresenceState())

        assert decision.awaits_llm_intent() is True and decision.gate == ""

    def test_the_cooldown_still_holds_back_a_wait(self) -> None:
        config = _config(cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()
        cooldown.record(_msg(now=0.0))

        decision = tp.evaluate(_msg(now=5.0), config, cooldown, presence=self._present())

        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN and decision.awaits_quiet() is False

    def test_a_cooldown_ending_inside_the_wait_does_not_block(self) -> None:
        config = _config(cooldown_group_sec=30.0, quiet_wait_sec=4.0)
        cooldown = tp.CooldownState()
        cooldown.record(_msg(now=0.0))

        decision = tp.evaluate(_msg(now=27.0), config, cooldown, presence=self._present(20.0))

        assert decision.awaits_quiet() is True


class TestDensityInTheDecision:
    @staticmethod
    def _full(count: int = 3) -> tp.PresenceState:
        state = tp.PresenceState()
        for index in range(count):
            state.record_proactive(GROUP, float(index))
        return state

    def test_a_full_budget_blocks_what_nobody_asked_for(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, roll=0.0), _config(), presence=self._full())

        assert decision.blocked_by == tp.BLOCKED_BY_DENSITY and decision.density_left == 0
        assert decision.awaits_llm_intent() is False and decision.awaits_quiet() is False

    def test_a_full_budget_never_blocks_being_called(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, at_me=True), _config(), presence=self._full())

        assert decision.triggered is True

    def test_the_remaining_budget_is_reported(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, roll=0.5), _config(), presence=self._full(1))

        assert decision.density_left == 2

    def test_an_unlimited_budget_reports_nothing(self) -> None:
        decision = tp.evaluate(_msg(now=10.0, roll=0.5), _config(proactive_max_replies=0), presence=self._full(5))

        assert decision.density_left is None and decision.blocked_by == tp.BLOCKED_BY_GATE

    def test_a_keyword_replies_directly_but_takes_from_the_budget(self) -> None:
        config = _config(keyword=True, keyword_words=("天气",))
        state = tp.PresenceState()
        inp = _msg("今天天气不错", now=10.0)

        decision = tp.evaluate(inp, config, presence=state)
        stamp = tp.accept(inp, decision, presence=state)

        assert decision.triggered is True and decision.source == tp.SIGNAL_KEYWORD
        assert stamp == pytest.approx(10.0)
        assert state.density_left(GROUP, 11.0, config) == 2

    def test_a_keyword_is_held_back_when_the_budget_is_full(self) -> None:
        config = _config(keyword=True, keyword_words=("天气",))

        decision = tp.evaluate(_msg("今天天气不错", now=10.0), config, presence=self._full())

        assert decision.triggered is False and decision.blocked_by == tp.BLOCKED_BY_DENSITY
        assert decision.signal == tp.SIGNAL_KEYWORD


class TestTheAnswer:
    def test_a_yes_is_held_back_by_a_budget_that_filled_meanwhile(self) -> None:
        state = TestDensityInTheDecision._full()

        decision = tp.resolve_llm_intent(_msg(now=10.0), _config(), presence=state, agreed=True, reason="在接话")

        assert decision.triggered is False and decision.blocked_by == tp.BLOCKED_BY_DENSITY
        assert decision.density_left == 0

    def test_a_yes_for_a_called_message_ignores_the_budget(self) -> None:
        state = TestDensityInTheDecision._full()

        decision = tp.resolve_llm_intent(
            _msg(now=10.0, at_me=True), _config(at=False), presence=state, agreed=True,
        )

        assert decision.triggered is True and decision.source == tp.SIGNAL_AT

    def test_a_no_stays_a_no(self) -> None:
        decision = tp.resolve_llm_intent(_msg(now=10.0), _config(), presence=tp.PresenceState(), agreed=False)

        assert decision.triggered is False and decision.blocked_by == ""

    def test_the_cooldown_still_applies_after_the_answer(self) -> None:
        config = _config(cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()
        cooldown.record(_msg(now=0.0))

        decision = tp.resolve_llm_intent(_msg(now=5.0), config, cooldown, presence=tp.PresenceState(), agreed=True)

        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN

    def test_without_presence_the_budget_is_not_consulted(self) -> None:
        state = TestDensityInTheDecision._full()

        decision = tp.resolve_llm_intent(_msg(now=10.0), _config(presence=False), presence=state, agreed=True)

        assert decision.triggered is True


class TestBookkeeping:
    def test_only_a_called_message_enters(self) -> None:
        state = tp.PresenceState()
        plain = _msg(now=5.0, roll=0.5)
        tp.observe(plain, tp.evaluate(plain, _config(), presence=state), state)

        assert state.is_present(GROUP, 6.0, 90.0) is False

        called = _msg(now=6.0, at_me=True)
        tp.observe(called, tp.evaluate(called, _config(), presence=state), state)

        assert state.is_present(GROUP, 7.0, 90.0) is True

    def test_entry_is_recorded_even_when_the_cooldown_holds_the_reply(self) -> None:
        config = _config(cooldown_group_sec=30.0, cooldown_exempt_at=False)
        cooldown = tp.CooldownState()
        cooldown.record(_msg(now=0.0))
        state = tp.PresenceState()
        called = _msg(now=5.0, at_me=True)

        decision = tp.evaluate(called, config, cooldown, presence=state)
        tp.observe(called, decision, state)

        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN
        assert state.is_present(GROUP, 6.0, 90.0) is True

    def test_accepting_a_judged_message_counts_it_and_ends_the_wait(self) -> None:
        config = _config(cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()
        state = tp.PresenceState()
        state.join_quiet(GROUP, 1.0, wait_sec=4.0, max_wait_sec=20.0)
        inp = _msg(now=5.0)
        decision = tp.resolve_llm_intent(inp, config, cooldown, presence=state, agreed=True)

        stamp = tp.accept(inp, decision, cooldown=cooldown, presence=state)

        assert stamp == pytest.approx(5.0)
        assert state.quiet(GROUP) is None
        assert state.proactive_count(GROUP, 6.0, 300.0) == 1
        assert cooldown.remaining(_msg(now=6.0), config)[0] > 0

    def test_accepting_a_called_message_takes_nothing_from_the_budget(self) -> None:
        state = tp.PresenceState()
        inp = _msg(now=5.0, at_me=True)

        assert tp.accept(inp, tp.evaluate(inp, _config(), presence=state), presence=state) is None
        assert state.proactive_count(GROUP, 6.0, 300.0) == 0

    def test_accepting_without_a_presence_state_only_records_the_cooldown(self) -> None:
        cooldown = tp.CooldownState()
        inp = _msg(now=5.0)

        assert tp.accept(inp, tp.TriggerDecision(triggered=True, signal=tp.SIGNAL_KEYWORD), cooldown=cooldown) is None
        assert cooldown.remaining(_msg(now=6.0), _config(cooldown_group_sec=30.0))[0] > 0


class TestATimeline:
    """进场 → 续期 → 安静等待（重置、最长等待）→ 退场，一步一步按真实那几次调用走。"""

    def test_the_state_machine_over_a_minute_and_a_half(self) -> None:
        config = _config(presence_window_sec=60.0)
        state = tp.PresenceState()
        outcomes: list[tuple[float, str]] = []

        def feed(inp: tp.TriggerInput) -> tp.TriggerDecision:
            wait = state.quiet(GROUP)
            if wait is not None and inp.now >= wait.deadline:
                closed = state.close_quiet(GROUP, wait.cycle)
                outcomes.append((closed.deadline, f"due:{closed.count}"))
            decision = tp.evaluate(inp, config, presence=state)
            tp.observe(inp, decision, state)
            if decision.triggered and not decision.undetermined:
                tp.accept(inp, decision, presence=state)
                outcomes.append((inp.now, "reply"))
            elif decision.awaits_quiet():
                state.join_quiet(GROUP, inp.now, wait_sec=config.quiet_wait_sec, max_wait_sec=config.quiet_max_wait_sec)
                outcomes.append((inp.now, "wait"))
            elif decision.awaits_llm_intent():
                outcomes.append((inp.now, "ask"))
            else:
                outcomes.append((inp.now, decision.blocked_by or "none"))
            return decision

        feed(_msg(now=0.0, roll=0.5))
        feed(_msg(now=5.0, at_me=True))
        for now in (8.0, 10.0, 13.0, 16.0, 19.0, 22.0, 25.0):
            feed(_msg(now=now, roll=0.5))
        feed(_msg(now=40.0, at_me=True))
        feed(_msg(now=99.0, roll=0.5))
        feed(_msg(now=101.0, roll=0.05))

        assert outcomes == [
            (0.0, tp.BLOCKED_BY_GATE),
            (5.0, "reply"),
            (8.0, "wait"), (10.0, "wait"), (13.0, "wait"), (16.0, "wait"), (19.0, "wait"), (22.0, "wait"),
            (25.0, "wait"),
            # 8 秒开的这一轮最长等到 28 秒，40 秒那次 @ 来之前已经到点，七条只问最后一条。
            (28.0, "due:7"),
            (40.0, "reply"),
            # 最后一次进场在 40 秒、窗口 60 秒：99 秒还在场，101 秒已经离场，改走概率门。
            (99.0, "wait"),
            (101.0, "ask"),
        ]
        pending = state.quiet(GROUP)
        assert pending is not None and (pending.count, pending.deadline) == (1, 103.0)


class TestGroupSettings:
    def test_no_settings_change_nothing(self) -> None:
        config = _config()

        assert tp.with_group_settings(config, None) is config
        assert tp.with_group_settings(config, {}) is config

    def test_the_response_policy_counts_as_llm_intent(self) -> None:
        config = _config(llm_intent=False)

        assert config.presence_active is False
        assert tp.with_group_settings(config, {"response_policy_enabled": True}).presence_active is True

    def test_a_disabled_response_policy_does_not_switch_llm_intent_off(self) -> None:
        assert tp.with_group_settings(_config(), {"response_policy_enabled": False}).llm_intent is True

    @pytest.mark.parametrize("platform,group,expected", [(True, False, False), (False, True, True)])
    def test_a_group_switch_beats_the_platform(self, platform: bool, group: bool, expected: bool) -> None:
        merged = tp.with_group_settings(_config(presence=platform), {"interject_presence": group})

        assert merged.presence is expected

    def test_none_follows_the_platform(self) -> None:
        config = _config(presence=True, absent_judge_probability=0.25)

        merged = tp.with_group_settings(config, {"interject_presence": None, "interject_absent_probability": None})

        assert (merged.presence, merged.absent_judge_probability) == (True, 0.25)

    def test_a_group_probability_beats_the_platform(self) -> None:
        merged = tp.with_group_settings(_config(absent_judge_probability=0.1), {"interject_absent_probability": 0.4})

        assert merged.absent_judge_probability == pytest.approx(0.4)

    @pytest.mark.parametrize("value,expected", [(1.5, 1.0), (-0.2, 0.0), (0, 0.0), (1, 1.0)])
    def test_the_probability_is_clamped(self, value: float, expected: float) -> None:
        merged = tp.with_group_settings(_config(), {"interject_absent_probability": value})

        assert merged.absent_judge_probability == pytest.approx(expected)

    @pytest.mark.parametrize("value", ["0.5", True, math.nan, math.inf, [], {}])
    def test_a_bad_probability_is_ignored(self, value: Any) -> None:
        merged = tp.with_group_settings(_config(absent_judge_probability=0.1), {"interject_absent_probability": value})

        assert merged.absent_judge_probability == pytest.approx(0.1)

    @pytest.mark.parametrize("value", ["true", 1, 0, "off"])
    def test_a_bad_switch_is_ignored(self, value: Any) -> None:
        assert tp.with_group_settings(_config(presence=False), {"interject_presence": value}).presence is False

    def test_the_override_names_match_the_community_fields(self) -> None:
        from supervisor.community.service import POLICY_FIELDS

        assert set(tp.GROUP_OVERRIDE_FIELDS) <= set(POLICY_FIELDS)
        assert set(tp.GROUP_OVERRIDE_FIELDS.values()) <= set(tp.TriggerConfig.__dataclass_fields__)


_OLD_LIVE = {
    "group_trigger": "mention_only",
    "signal_all": None, "signal_at": None, "signal_reply": None, "signal_prefix": None,
    "signal_name": False, "signal_keyword": False, "signal_random": False,
    "name_words": [], "name_anywhere": True, "keyword_words": [],
    "random_probability": 0.02, "trigger_prefixes": ["!", "！", "/"],
    "llm_intent_enabled": True, "llm_intent_timeout_sec": 8.0,
    "cooldown_group_sec": 0.0, "cooldown_user_sec": 0.0, "cooldown_exempt_at": True,
}


class TestOldConfigsBehaveAsBefore:
    def test_a_value_table_without_the_new_keys_keeps_presence_off(self) -> None:
        config = tp.TriggerConfig.from_live(SimpleNamespace(**_OLD_LIVE))

        assert config.presence is False and config.presence_active is False
        assert (
            config.presence_window_sec, config.absent_judge_probability, config.quiet_wait_sec,
            config.quiet_max_wait_sec, config.proactive_max_replies, config.proactive_window_sec,
        ) == (90.0, 0.1, 4.0, 20.0, 3, 300.0)

    def test_the_new_keys_are_read_when_present(self) -> None:
        config = tp.TriggerConfig.from_live(SimpleNamespace(
            **_OLD_LIVE, presence_enabled=True, presence_window_sec=30, absent_judge_probability=0.5,
            quiet_wait_sec=2, quiet_max_wait_sec=9, proactive_max_replies=7, proactive_window_sec=60,
        ))

        assert config.presence_active is True
        assert (
            config.presence_window_sec, config.absent_judge_probability, config.quiet_wait_sec,
            config.quiet_max_wait_sec, config.proactive_max_replies, config.proactive_window_sec,
        ) == (30.0, 0.5, 2.0, 9.0, 7, 60.0)

    @pytest.mark.parametrize("inp", [
        _msg("随便聊聊", now=5.0, roll=0.01),
        _msg("随便聊聊", now=5.0, roll=0.9),
        _msg("在吗", now=5.0, at_me=True),
        _msg("好的", now=5.0, reply_to_bot=True),
        _msg("咪啪你看", now=5.0),
        _msg("今天天气不错", now=5.0, roll=0.3),
        _msg("！离谱", now=5.0),
    ])
    @pytest.mark.parametrize("config", [
        _config(presence=False),
        _config(presence=True, llm_intent=False),
        _config(presence=False, random=True, random_probability=0.5, keyword=True, keyword_words=("天气",)),
        _config(presence=False, name=True, name_words=("咪啪",), prefix=True, prefix_words=("！",)),
    ])
    def test_without_presence_every_message_takes_the_old_path(
        self, inp: tp.TriggerInput, config: tp.TriggerConfig,
    ) -> None:
        state = tp.PresenceState()
        state.enter(GROUP, 0.0)
        for index in range(5):
            state.record_proactive(GROUP, float(index))

        with_state = tp.evaluate(inp, config, presence=state)
        without = tp.evaluate(inp, config)

        assert with_state == without
        assert (with_state.presence, with_state.quiet, with_state.gate, with_state.density_left) == ("", "", "", None)
        assert with_state.awaits_quiet() is False

    def test_the_verdict_shape_seen_by_the_snapshot_is_unchanged(self) -> None:
        decision = tp.evaluate(_msg(now=10.0), _config(), presence=tp.PresenceState())

        assert set(decision.as_dict()) == {
            "triggered", "signal", "blocked_by", "reason", "undetermined", "cooldown_remaining",
        }
        assert set(decision.status_dict()) == {
            "source", "presence", "quiet", "gate", "gate_probability", "density_left",
        }


class TestTriggerSource:
    @pytest.mark.parametrize("flags,expected", [({"at_me": True}, tp.SIGNAL_AT), ({"reply_to_bot": True}, tp.SIGNAL_REPLY)])
    def test_being_addressed_is_explicit_even_with_its_signal_off(self, flags: dict[str, bool], expected: str) -> None:
        assert tp.trigger_source(_msg(**flags), tp.SIGNAL_LLM_INTENT) == expected

    @pytest.mark.parametrize("signal", [tp.SIGNAL_NAME, tp.SIGNAL_PREFIX, tp.SIGNAL_AT, tp.SIGNAL_REPLY])
    def test_a_hit_explicit_signal_names_itself(self, signal: str) -> None:
        assert tp.trigger_source(_msg(), signal) == signal

    @pytest.mark.parametrize("signal", [tp.SIGNAL_KEYWORD, tp.SIGNAL_RANDOM, tp.SIGNAL_ALL, tp.SIGNAL_LLM_INTENT, ""])
    def test_other_sources_are_reported_as_they_are(self, signal: str) -> None:
        assert tp.trigger_source(_msg(), signal) == signal

    def test_the_legacy_path_fills_the_source_too(self) -> None:
        config = _config(presence=False, keyword=True, keyword_words=("天气",))

        assert tp.evaluate(_msg("今天天气不错"), config).source == tp.SIGNAL_KEYWORD
        assert tp.evaluate(_msg("在吗", at_me=True), config).source == tp.SIGNAL_AT

    def test_every_source_is_a_name_the_quote_setting_accepts(self) -> None:
        assert tp.EXPLICIT_SIGNALS <= tp.TRIGGER_SOURCE_NAMES
        assert tp.SIGNAL_LLM_INTENT in tp.TRIGGER_SOURCE_NAMES
