"""七个触发信号、冷却，以及 NoneBot 把三个来源混进 to_me 之后怎么拆开。

最要紧的两条：
1. 升级后行为必须逐位不变 —— 三态开关留空时由旧的 group_mode 枚举反推。
2. 「被回复」必须能关掉。它以前和 @、昵称共用 event.to_me，根本关不掉。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_live_config, load_runtime, set_live_config  # noqa: E402

_ADAPTER_ROOT = _ROOT / "adapters" / "onebot"


def _policy():
    """trigger_policy 不依赖 NoneBot，直接按文件加载。"""
    import importlib.util

    name = "_trigger_policy_under_test"
    spec = importlib.util.spec_from_file_location(name, _ADAPTER_ROOT / "trigger_policy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器会按 cls.__module__ 回查 sys.modules，先注册再执行。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tp = _policy()


def _config(**overrides: Any):
    return tp.TriggerConfig(**overrides)


def _input(**overrides: Any):
    base = {"text": "随便说说", "group_id": 1, "user_id": 10001, "now": 1000.0}
    base.update(overrides)
    return tp.TriggerInput(**base)


# ── 升级兼容：三态开关留空时跟随旧枚举 ───────────────────────────────

class TestLegacyModeStillDecidesTheDefaults:
    def _from_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str):
        for key in ("ONEBOT_GROUP_TRIGGER", "ONEBOT_TRIGGER_PREFIXES"):
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, ONEBOT_GROUP_TRIGGER=mode)
        return tp.TriggerConfig.from_live(module.live)

    def test_mention_only_keeps_at_and_reply(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """旧实现里 @ 和被回复共用 to_me，两者当时都是开着的。"""
        config = self._from_mode(monkeypatch, tmp_path, "mention_only")

        assert config.enabled_signals() == (tp.SIGNAL_AT, tp.SIGNAL_REPLY)

    def test_prefix_mode_adds_the_prefix_signal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config = self._from_mode(monkeypatch, tmp_path, "prefix")

        assert config.enabled_signals() == (tp.SIGNAL_AT, tp.SIGNAL_REPLY, tp.SIGNAL_PREFIX)

    @pytest.mark.parametrize("mode", ["all", "always"])
    def test_all_mode_becomes_the_all_signal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
    ) -> None:
        config = self._from_mode(monkeypatch, tmp_path, mode)

        assert tp.SIGNAL_ALL in config.enabled_signals()

    def test_an_unknown_mode_behaves_like_mention_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        config = self._from_mode(monkeypatch, tmp_path, "whatever")

        assert config.enabled_signals() == (tp.SIGNAL_AT, tp.SIGNAL_REPLY)

    def test_the_new_signals_default_off(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config = self._from_mode(monkeypatch, tmp_path, "all")

        assert config.name is False and config.keyword is False and config.random is False
        assert config.llm_intent is False

    def test_an_explicit_switch_overrides_the_legacy_derivation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        for key in ("ONEBOT_GROUP_TRIGGER", "ONEBOT_TRIGGER_PREFIXES"):
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, ONEBOT_GROUP_TRIGGER="prefix")

        from _onebot_harness import write_live_config

        write_live_config(module, signal_prefix=False, signal_reply=False)
        config = tp.TriggerConfig.from_live(module.live)

        assert config.enabled_signals() == (tp.SIGNAL_AT,)

    def test_a_tristate_left_null_is_not_the_same_as_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """写 null 是「交回旧语义」，写 false 是「我要关掉」。混为一谈就等于静默改行为。"""
        for key in ("ONEBOT_GROUP_TRIGGER",):
            monkeypatch.delenv(key, raising=False)
        module = load_live_config(monkeypatch, tmp_path, ONEBOT_GROUP_TRIGGER="all")
        module.config_path().parent.mkdir(parents=True, exist_ok=True)
        module.config_path().write_text("trigger:\n  signals:\n    all: null\n", encoding="utf-8")
        module.invalidate()

        assert module.live.signal_all is None
        assert tp.TriggerConfig.from_live(module.live).all_messages is True


# ── 单个信号 ───────────────────────────────────

class TestEachSignalOnItsOwn:
    def test_at(self) -> None:
        decision = tp.evaluate(_input(at_me=True), _config(at=True))

        assert decision.triggered and decision.signal == tp.SIGNAL_AT

    def test_at_can_be_turned_off(self) -> None:
        decision = tp.evaluate(_input(at_me=True), _config(at=False, reply=False))

        assert decision.triggered is False

    def test_reply(self) -> None:
        decision = tp.evaluate(_input(reply_to_bot=True), _config(reply=True))

        assert decision.triggered and decision.signal == tp.SIGNAL_REPLY

    def test_reply_can_be_turned_off(self) -> None:
        """这是本期的头条修复：被回复以前和 @ 共用 to_me，关不掉。"""
        decision = tp.evaluate(_input(reply_to_bot=True), _config(at=True, reply=False))

        assert decision.triggered is False

    def test_all_bypasses_every_other_signal(self) -> None:
        decision = tp.evaluate(
            _input(), _config(all_messages=True, at=False, reply=False),
        )

        assert decision.triggered and decision.signal == tp.SIGNAL_ALL

    def test_prefix(self) -> None:
        decision = tp.evaluate(
            _input(text="!生图 猫"), _config(at=False, reply=False, prefix=True, prefix_words=("!", "/")),
        )

        assert decision.triggered and decision.signal == tp.SIGNAL_PREFIX

    def test_prefix_only_matches_at_the_start(self) -> None:
        decision = tp.evaluate(
            _input(text="这样写 !生图"), _config(at=False, reply=False, prefix=True, prefix_words=("!",)),
        )

        assert decision.triggered is False

    def test_keyword_matches_anywhere(self) -> None:
        decision = tp.evaluate(
            _input(text="今天天气怎么样"),
            _config(at=False, reply=False, keyword=True, keyword_words=("天气",)),
        )

        assert decision.triggered and decision.signal == tp.SIGNAL_KEYWORD

    def test_an_enabled_signal_with_an_empty_word_list_never_matches(self) -> None:
        for kwargs in ({"keyword": True, "keyword_words": ()}, {"name": True, "name_words": ()}):
            decision = tp.evaluate(_input(text="随便"), _config(at=False, reply=False, **kwargs))

            assert decision.triggered is False


class TestNameSignal:
    def _config(self, **overrides: Any):
        base = {"at": False, "reply": False, "name": True, "name_words": ("咪啪",)}
        base.update(overrides)
        return _config(**base)

    def test_it_matches_mid_sentence_by_default(self) -> None:
        decision = tp.evaluate(_input(text="你们觉得咪啪怎么样"), self._config())

        assert decision.triggered and decision.signal == tp.SIGNAL_NAME

    def test_anywhere_off_only_matches_the_start(self) -> None:
        config = self._config(name_anywhere=False)

        assert tp.evaluate(_input(text="咪啪在吗"), config).triggered is True
        assert tp.evaluate(_input(text="你们觉得咪啪怎么样"), config).triggered is False

    def test_the_original_text_is_never_rewritten(self) -> None:
        """NoneBot 的 NICKNAME 命中后会把名字删掉，自己实现就是为了留住原文。"""
        text = "咪啪你好"
        tp.evaluate(_input(text=text), self._config())

        assert text == "咪啪你好"

    def test_an_ascii_name_needs_a_word_boundary(self) -> None:
        config = self._config(name_words=("nai",))

        assert tp.evaluate(_input(text="用 nai 画一张"), config).triggered is True
        assert tp.evaluate(_input(text="naive 的想法"), config).triggered is False

    def test_an_ascii_name_is_case_insensitive(self) -> None:
        config = self._config(name_words=("Nai",))

        assert tp.evaluate(_input(text="用 NAI 画"), config).triggered is True

    def test_a_cjk_name_matches_as_a_substring(self) -> None:
        """中日韩没有词边界概念，硬套 \\b 会一个都匹配不上。"""
        config = self._config(name_words=("咪啪",))

        assert tp.evaluate(_input(text="小咪啪啊"), config).triggered is True

    def test_the_hit_word_is_reported(self) -> None:
        decision = tp.evaluate(_input(text="咪啪在吗"), self._config(name_words=("咪啪", "啪啪")))

        assert "咪啪" in decision.reason


class TestRandomSignal:
    def _config(self, probability: float):
        return _config(at=False, reply=False, random=True, random_probability=probability)

    def test_a_roll_under_the_probability_triggers(self) -> None:
        decision = tp.evaluate(_input(roll=0.01), self._config(0.02))

        assert decision.triggered and decision.signal == tp.SIGNAL_RANDOM

    def test_a_roll_at_or_above_the_probability_does_not(self) -> None:
        assert tp.evaluate(_input(roll=0.02), self._config(0.02)).triggered is False

    def test_zero_probability_never_triggers(self) -> None:
        assert tp.evaluate(_input(roll=0.0), self._config(0.0)).triggered is False

    def test_probability_one_always_triggers(self) -> None:
        assert tp.evaluate(_input(roll=0.999), self._config(1.0)).triggered is True

    def test_it_is_reported_as_undetermined_for_the_preview(self) -> None:
        """试听不能伪造随机结果 —— 说会回、实际不回，比没有试听更糟。"""
        decision = tp.evaluate(_input(roll=0.0), self._config(0.5), resolve_random=False)

        assert decision.undetermined is True and decision.triggered is False
        assert decision.signal == tp.SIGNAL_RANDOM


class TestSignalPrecedence:
    def test_at_wins_over_reply(self) -> None:
        decision = tp.evaluate(_input(at_me=True, reply_to_bot=True), _config())

        assert decision.signal == tp.SIGNAL_AT

    def test_the_reported_signal_follows_the_declared_order(self) -> None:
        decision = tp.evaluate(
            _input(text="!咪啪 天气"),
            _config(at=False, reply=False, name=True, name_words=("咪啪",),
                    prefix=True, prefix_words=("!",), keyword=True, keyword_words=("天气",)),
        )

        assert decision.signal == tp.SIGNAL_NAME
        assert tp.SIGNAL_ORDER.index(tp.SIGNAL_NAME) < tp.SIGNAL_ORDER.index(tp.SIGNAL_PREFIX)


# ── 冷却 ───────────────────────────────────

class TestCooldown:
    def test_a_second_message_in_the_same_group_is_held_back(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0)
        first = _input(text="咪啪", at_me=False, reply_to_bot=True, now=1000.0)
        assert tp.evaluate(first, config, cooldown).triggered is True
        cooldown.record(first)

        second = tp.evaluate(_input(user_id=20002, reply_to_bot=True, now=1010.0), config, cooldown)

        assert second.triggered is False
        assert second.blocked_by == tp.BLOCKED_BY_COOLDOWN
        assert 19.0 < second.cooldown_remaining <= 20.0

    def test_it_lets_through_once_the_window_passes(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0)
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(reply_to_bot=True, now=1031.0), config, cooldown).triggered is True

    def test_a_per_user_window_does_not_hold_back_someone_else(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=0.0, cooldown_user_sec=30.0)
        cooldown.record(_input(user_id=10001, now=1000.0))

        assert tp.evaluate(_input(user_id=10001, reply_to_bot=True, now=1010.0), config, cooldown).triggered is False
        assert tp.evaluate(_input(user_id=20002, reply_to_bot=True, now=1010.0), config, cooldown).triggered is True

    def test_being_at_ed_is_exempt(self) -> None:
        """用户直接叫 bot 却被静默忽略，是最差的体验。"""
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0, cooldown_exempt_at=True)
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(at_me=True, now=1010.0), config, cooldown).triggered is True

    def test_the_exemption_can_be_turned_off(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0, cooldown_exempt_at=False)
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(at_me=True, now=1010.0), config, cooldown).triggered is False

    def test_being_replied_to_is_not_exempt(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0)
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(reply_to_bot=True, now=1010.0), config, cooldown).triggered is False

    def test_zero_means_no_limit(self) -> None:
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(reply_to_bot=True, now=1000.1), _config(), cooldown).triggered is True

    def test_all_mode_is_still_subject_to_cooldown(self) -> None:
        """全量 + 无冷却 = 每条都回。冷却必须能压住它，否则这个组合没法用。"""
        cooldown = tp.CooldownState()
        config = _config(all_messages=True, cooldown_group_sec=30.0)
        cooldown.record(_input(now=1000.0))

        assert tp.evaluate(_input(now=1010.0), config, cooldown).triggered is False

    def test_a_blocked_turn_does_not_extend_the_window(self) -> None:
        """冷却期内被挡下的那些消息不该把窗口一直往后推。"""
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0)
        cooldown.record(_input(now=1000.0))

        for moment in (1005.0, 1010.0, 1020.0):
            assert tp.evaluate(_input(reply_to_bot=True, now=moment), config, cooldown).triggered is False

        assert tp.evaluate(_input(reply_to_bot=True, now=1031.0), config, cooldown).triggered is True

    def test_forgetting_a_group_clears_both_dimensions(self) -> None:
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0, cooldown_user_sec=30.0)
        cooldown.record(_input(group_id=1, user_id=10001, now=1000.0))
        cooldown.record(_input(group_id=2, user_id=10001, now=1000.0))

        cooldown.forget_group(1)

        assert tp.evaluate(_input(group_id=1, user_id=10001, reply_to_bot=True, now=1005.0), config, cooldown).triggered is True
        assert tp.evaluate(_input(group_id=2, user_id=10001, reply_to_bot=True, now=1005.0), config, cooldown).triggered is False

    def test_forgetting_everything_clears_every_group(self) -> None:
        """换号时用：上一个号的静默窗口不该让新号一上来就哑着。"""
        cooldown = tp.CooldownState()
        config = _config(cooldown_group_sec=30.0, cooldown_user_sec=30.0)
        cooldown.record(_input(group_id=1, user_id=10001, now=1000.0))
        cooldown.record(_input(group_id=2, user_id=20002, now=1000.0))

        cooldown.forget_all()

        assert tp.evaluate(_input(group_id=1, user_id=10001, reply_to_bot=True, now=1005.0), config, cooldown).triggered is True
        assert tp.evaluate(_input(group_id=2, user_id=20002, reply_to_bot=True, now=1005.0), config, cooldown).triggered is True

    def test_the_reason_names_which_dimension_held_it(self) -> None:
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=1000.0))

        decision = tp.evaluate(
            _input(user_id=20002, reply_to_bot=True, now=1005.0),
            _config(cooldown_group_sec=30.0), cooldown,
        )

        assert "本群" in decision.reason
        # reason 会被试听整句显示，中文句子里不该夹 per_group 这种内部标识。
        assert "per_group" not in decision.reason

    def test_a_per_user_hold_reads_differently_from_a_per_group_one(self) -> None:
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=1000.0))

        decision = tp.evaluate(
            _input(reply_to_bot=True, now=1005.0),
            _config(cooldown_user_sec=30.0), cooldown,
        )

        assert "这个人" in decision.reason
        assert "per_user" not in decision.reason


# ── LLM 意愿判断的位置 ───────────────────────────────────

class TestLlmIntentIsTheLastResort:
    def test_it_is_only_reached_when_nothing_else_matched(self) -> None:
        decision = tp.evaluate(_input(at_me=True), _config(llm_intent=True))

        assert decision.signal == tp.SIGNAL_AT

    def test_nothing_matched_hands_over_to_the_model(self) -> None:
        decision = tp.evaluate(_input(), _config(at=False, reply=False, llm_intent=True))

        assert decision.triggered is False
        assert decision.signal == tp.SIGNAL_LLM_INTENT
        assert decision.undetermined is True

    def test_it_stays_out_of_the_way_when_disabled(self) -> None:
        decision = tp.evaluate(_input(), _config(at=False, reply=False, llm_intent=False))

        assert decision.signal == "" and decision.undetermined is False

    def test_a_missed_roll_falls_through_to_the_model(self) -> None:
        """随机没掷中才轮到模型。两个开关是级联的，不是各掷一次骰子。"""
        config = _config(
            at=False, reply=False, random=True, random_probability=0.5, llm_intent=True,
        )

        assert tp.evaluate(_input(roll=0.9), config).signal == tp.SIGNAL_LLM_INTENT
        assert tp.evaluate(_input(roll=0.1), config).signal == tp.SIGNAL_RANDOM

    def test_the_cooldown_blocks_the_handover_before_the_model_is_asked(self) -> None:
        """冷却期内不去问：问完再丢答案等于每条不命中消息白花一次模型调用。"""
        config = _config(at=False, reply=False, llm_intent=True, cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=990.0))

        decision = tp.evaluate(_input(now=1000.0), config, cooldown)

        assert decision.signal == tp.SIGNAL_LLM_INTENT
        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN
        assert decision.cooldown_remaining == pytest.approx(20.0)
        assert decision.undetermined is False
        assert decision.awaits_llm_intent() is False

    def test_a_clear_window_still_hands_over(self) -> None:
        decision = tp.evaluate(
            _input(), _config(at=False, reply=False, llm_intent=True, cooldown_group_sec=30.0),
            tp.CooldownState(),
        )

        assert decision.awaits_llm_intent() is True

    def test_a_window_that_expires_before_the_answer_arrives_is_still_asked(self) -> None:
        """剩 5s、问一次要 8s —— 答案到手时窗口已经过去，这条消息不该被静默。"""
        config = _config(
            at=False, reply=False, llm_intent=True,
            llm_intent_timeout_sec=8.0, cooldown_group_sec=30.0,
        )
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=975.0))

        decision = tp.evaluate(_input(now=1000.0), config, cooldown)

        assert decision.awaits_llm_intent() is True
        assert decision.blocked_by == ""

    def test_a_window_outlasting_the_answer_is_blocked(self) -> None:
        config = _config(
            at=False, reply=False, llm_intent=True,
            llm_intent_timeout_sec=8.0, cooldown_group_sec=30.0,
        )
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=995.0))

        decision = tp.evaluate(_input(now=1000.0), config, cooldown)

        assert decision.awaits_llm_intent() is False
        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN

    def test_the_at_exemption_does_not_leak_onto_the_handover(self) -> None:
        # exempt_at 只豁免被 @ 那一条。意愿判断是「没人叫你，你自己想插话」，不该沾这个豁免。
        config = _config(
            at=False, reply=False, llm_intent=True,
            cooldown_group_sec=30.0, cooldown_exempt_at=True,
        )
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=990.0))

        assert tp.evaluate(_input(now=1000.0), config, cooldown).blocked_by == tp.BLOCKED_BY_COOLDOWN

    def test_the_hit_reason_of_an_explicit_signal_is_unchanged(self) -> None:
        """冷却文案会被试听整句显示，抽公共判定时一个字都不能变。"""
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=1000.0))

        decision = tp.evaluate(
            _input(reply_to_bot=True, now=1001.0), _config(cooldown_group_sec=30.0), cooldown,
        )

        assert decision.reason == "命中被回复，但本群的冷却还剩 29.0s"


# ── 适配层：把 to_me 的三个来源拆开 ───────────────────────────────────

_BOT_QQ = "90000"


def _event(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "self_id": _BOT_QQ,
        "user_id": 10001,
        "group_id": 1,
        "message_id": 7,
        "raw_message": "",
        "to_me": False,
        "reply": None,
        "get_message": lambda: [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _reply_from(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(sender=SimpleNamespace(user_id=user_id))


class TestSourceSeparation:
    @pytest.fixture
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        return load_runtime(monkeypatch, tmp_path)

    def _bot(self) -> SimpleNamespace:
        return SimpleNamespace(self_id=_BOT_QQ)

    def test_a_mid_sentence_at_is_read_from_the_message(self, runtime: Any) -> None:
        event = _event(get_message=lambda: [
            {"type": "text", "data": {"text": "帮我问问 "}},
            {"type": "at", "data": {"qq": _BOT_QQ}},
        ])

        assert runtime._event_at_mentions_bot(event, self._bot()) is True

    def test_a_stripped_leading_at_is_recovered_from_to_me(self, runtime: Any) -> None:
        """_check_at_me 命中首尾 @ 时会把那一段删掉，只剩 to_me。"""
        event = _event(to_me=True)

        assert runtime._event_at_mentions_bot(event, self._bot()) is True

    def test_a_reply_to_the_bot_is_not_counted_as_an_at(self, runtime: Any) -> None:
        """_check_reply 也置 to_me。不扣掉它，「被回复」就永远关不掉。"""
        event = _event(to_me=True, reply=_reply_from(_BOT_QQ))

        assert runtime._event_replies_to_bot(event, self._bot()) is True
        assert runtime._event_at_mentions_bot(event, self._bot()) is False

    def test_a_reply_plus_a_real_at_still_counts_as_an_at(self, runtime: Any) -> None:
        event = _event(
            to_me=True, reply=_reply_from(_BOT_QQ),
            get_message=lambda: [{"type": "at", "data": {"qq": _BOT_QQ}}],
        )

        assert runtime._event_at_mentions_bot(event, self._bot()) is True

    def test_a_reply_to_someone_else_is_not_a_bot_reply(self, runtime: Any) -> None:
        event = _event(reply=_reply_from("55555"))

        assert runtime._event_replies_to_bot(event, self._bot()) is False

    def test_a_missing_reply_object_is_not_a_bot_reply(self, runtime: Any) -> None:
        """get_msg 失败时 _check_reply 提前返回，reply 为 None。保守判为不是。"""
        event = _event(reply=None)

        assert runtime._event_replies_to_bot(event, self._bot()) is False

    def test_an_at_in_the_raw_message_is_recognised(self, runtime: Any) -> None:
        event = _event(raw_message=f"[CQ:at,qq={_BOT_QQ}] 在吗")

        assert runtime._event_at_mentions_bot(event, self._bot()) is True

    def test_an_at_to_someone_else_is_not_an_at_to_the_bot(self, runtime: Any) -> None:
        event = _event(get_message=lambda: [{"type": "at", "data": {"qq": "55555"}}])

        assert runtime._event_at_mentions_bot(event, self._bot()) is False


class TestTheAdapterHonoursTheSwitches:
    @pytest.fixture
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        return load_runtime(monkeypatch, tmp_path)

    def _decide(self, runtime: Any, event: SimpleNamespace, text: str = "在吗"):
        return runtime._group_trigger_decision(event, SimpleNamespace(self_id=_BOT_QQ), text)

    def test_turning_off_reply_actually_stops_a_reply_turn(self, runtime: Any) -> None:
        """P2 的头条：这在改造前做不到。"""
        event = _event(to_me=True, reply=_reply_from(_BOT_QQ))
        assert self._decide(runtime, event).triggered is True

        set_live_config(runtime, signal_reply=False)

        assert self._decide(runtime, event).triggered is False

    def test_turning_off_at_actually_stops_an_at_turn(self, runtime: Any) -> None:
        event = _event(get_message=lambda: [{"type": "at", "data": {"qq": _BOT_QQ}}])
        assert self._decide(runtime, event).triggered is True

        set_live_config(runtime, signal_at=False, signal_reply=False)

        assert self._decide(runtime, event).triggered is False

    def test_the_name_signal_works_through_the_adapter(self, runtime: Any) -> None:
        set_live_config(runtime, signal_name=True, name_words=["咪啪"])

        decision = self._decide(runtime, _event(), "你们说咪啪怎么样")

        assert decision.triggered and decision.signal == "name"

    def test_cooldown_state_is_shared_by_the_whole_process(self, runtime: Any) -> None:
        set_live_config(runtime, cooldown_group_sec=30, cooldown_exempt_at=False)
        event = _event(to_me=True, reply=_reply_from(_BOT_QQ))
        runtime._trigger_cooldown.record(runtime._trigger_input(event, SimpleNamespace(self_id=_BOT_QQ), "在吗"))

        decision = self._decide(runtime, event)

        assert decision.triggered is False and decision.blocked_by == "cooldown"

    def test_resetting_a_conversation_clears_the_cooldown(self, runtime: Any) -> None:
        """刚说完「重新开始」，下一句就该有人应。"""
        import asyncio

        set_live_config(runtime, cooldown_group_sec=30, cooldown_exempt_at=False)
        event = _event(to_me=True, reply=_reply_from(_BOT_QQ))
        runtime._trigger_cooldown.record(runtime._trigger_input(event, SimpleNamespace(self_id=_BOT_QQ), "在吗"))
        runtime._real_conversation_keys["conv_x"] = "qq_group:1"
        runtime._session_targets["s1"] = {"type": "group", "group_id": 1}

        asyncio.run(runtime.TangQiuCallbacks().on_context_reset("conv_x", "clear", []))

        assert self._decide(runtime, event).triggered is True

    def test_compacting_does_not_clear_the_cooldown(self, runtime: Any) -> None:
        """compact 只换掉早期原文，对话没断，冷却照旧。"""
        import asyncio

        set_live_config(runtime, cooldown_group_sec=30, cooldown_exempt_at=False)
        event = _event(to_me=True, reply=_reply_from(_BOT_QQ))
        runtime._trigger_cooldown.record(runtime._trigger_input(event, SimpleNamespace(self_id=_BOT_QQ), "在吗"))
        runtime._real_conversation_keys["conv_x"] = "qq_group:1"
        runtime._session_targets["s1"] = {"type": "group", "group_id": 1}

        asyncio.run(runtime.TangQiuCallbacks().on_context_reset("conv_x", "compact", []))

        assert self._decide(runtime, event).triggered is False

    def test_the_effective_config_can_be_rebuilt_from_the_published_values(self, runtime: Any) -> None:
        """dry-run 拿 bot 公布的扁平键值对装配 TriggerConfig，两侧必须得到同一份。

        from_live 只做属性读取，所以包一层 SimpleNamespace 就能复用同一套三态推导 ——
        supervisor 再写一份推导就是漂移的起点。
        """
        set_live_config(runtime, signal_name=True, name_words=["咪啪"], cooldown_group_sec=15)
        module = sys.modules[f"{runtime.__name__}.live_config"]
        published = module.state_payload()["values"]

        from_live = tp.TriggerConfig.from_live(runtime.live)
        from_published = tp.TriggerConfig.from_live(SimpleNamespace(**published))

        assert from_published == from_live

    def test_the_rule_records_the_cooldown_only_when_it_triggers(self, runtime: Any) -> None:
        import asyncio

        set_live_config(runtime, cooldown_group_sec=30, cooldown_exempt_at=False, allowed_groups=[1])
        quiet = _event(user_id=20002)

        asyncio.run(runtime._agent_group_rule(SimpleNamespace(self_id=_BOT_QQ), quiet))

        assert runtime._trigger_cooldown.remaining(
            runtime._trigger_input(quiet, SimpleNamespace(self_id=_BOT_QQ), ""),
            tp.TriggerConfig(cooldown_group_sec=30.0),
        ) == (0.0, "")


# ── 试听：与真实判定同源 ───────────────────────────────────

class TestDryRunSharesTheDecision:
    """判定逻辑只有一份。前端不重写，supervisor 也不重写。"""

    def test_the_policy_module_has_no_relative_imports(self) -> None:
        """supervisor 按文件路径单独加载它，一旦出现相对导入就会加载失败。"""
        import ast

        tree = ast.parse((_ADAPTER_ROOT / "trigger_policy.py").read_text(encoding="utf-8"))
        relative = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0
        ]

        assert relative == [], "trigger_policy 不能有相对导入，否则 supervisor 侧加载不了"

    def test_the_legacy_mode_sets_match_the_declared_ones(self) -> None:
        """两份枚举是同一套字面量的副本（这个模块不能有相对导入），漂了 group_mode 校验就和判定不一致。"""
        import importlib.util

        name = "_onebot_config_under_test"
        spec = importlib.util.spec_from_file_location(name, _ADAPTER_ROOT / "config.py")
        assert spec and spec.loader
        cfg = importlib.util.module_from_spec(spec)
        sys.modules[name] = cfg
        spec.loader.exec_module(cfg)

        assert tp._LEGACY_ALL_MODES == cfg.GROUP_TRIGGER_ALL_MODES
        assert tp._LEGACY_PREFIX_MODES == cfg.GROUP_TRIGGER_PREFIX_MODES

    def test_supervisor_loads_the_same_module_the_adapter_uses(self) -> None:
        from supervisor import qq_trigger

        policy = qq_trigger.load_policy(_ROOT)

        assert policy.SIGNAL_ORDER == tp.SIGNAL_ORDER
        assert policy.TriggerConfig().at is tp.TriggerConfig().at

    def test_the_module_is_loaded_once_per_workspace(self) -> None:
        from supervisor import qq_trigger

        assert qq_trigger.load_policy(_ROOT) is qq_trigger.load_policy(_ROOT)

    def _values(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "group_trigger": "mention_only",
            "signal_all": None, "signal_at": None, "signal_reply": None, "signal_prefix": None,
            "signal_name": False, "signal_keyword": False, "signal_random": False,
            "name_words": [], "name_anywhere": True, "keyword_words": [],
            "random_probability": 0.02, "trigger_prefixes": ["!", "！", "/"],
            "llm_intent_enabled": False, "llm_intent_timeout_sec": 8.0,
            "cooldown_group_sec": 0.0, "cooldown_user_sec": 0.0, "cooldown_exempt_at": True,
        }
        base.update(overrides)
        return base

    def test_a_batch_is_judged_in_order_so_cooldown_shows_up(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [
                {"text": "咪啪在吗", "user_id": 1, "group_id": 9},
                {"text": "咪啪你看", "user_id": 2, "group_id": 9},
            ],
            self._values(signal_name=True, name_words=["咪啪"], cooldown_group_sec=30),
        )

        assert out["results"][0]["triggered"] is True
        assert out["results"][1]["triggered"] is False
        assert out["results"][1]["blocked_by"] == "cooldown"

    def test_cooldown_can_be_left_out_of_the_preview(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [{"text": "咪啪", "user_id": 1, "group_id": 9}] * 3,
            self._values(signal_name=True, name_words=["咪啪"], cooldown_group_sec=30),
            apply_cooldown=False,
        )

        assert [r["triggered"] for r in out["results"]] == [True, True, True]

    def test_being_at_ed_still_bypasses_the_cooldown(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [
                {"text": "咪啪在吗", "user_id": 1, "group_id": 9},
                {"text": "帮我查", "user_id": 2, "group_id": 9, "at_me": True},
            ],
            self._values(signal_name=True, name_words=["咪啪"], cooldown_group_sec=30),
        )

        assert [r["triggered"] for r in out["results"]] == [True, True]

    def test_random_is_never_a_fabricated_answer(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [{"text": "随便聊聊", "user_id": 1, "group_id": 9}],
            self._values(signal_at=False, signal_reply=False, signal_random=True, random_probability=1.0),
        )

        result = out["results"][0]
        assert result["undetermined"] is True and result["triggered"] is False
        assert result["signal"] == "random"

    def test_llm_intent_is_reported_as_runtime_dependent(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [{"text": "随便聊聊", "user_id": 1, "group_id": 9}],
            self._values(signal_at=False, signal_reply=False, llm_intent_enabled=True),
        )

        assert out["results"][0]["signal"] == "llm_intent"
        assert out["results"][0]["undetermined"] is True

    def test_the_preview_shows_the_cooldown_holding_back_the_handover(self) -> None:
        """真实判定在冷却期内不去问模型，试听必须照样显示成被冷却挡住。"""
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [
                {"text": "咪啪在吗", "user_id": 1, "group_id": 9, "at_sec": 0},
                {"text": "随便聊聊", "user_id": 2, "group_id": 9, "at_sec": 1},
            ],
            self._values(
                signal_name=True, name_words=["咪啪"], llm_intent_enabled=True,
                cooldown_group_sec=30,
            ),
        )

        second = out["results"][1]
        assert second["signal"] == "llm_intent"
        assert second["blocked_by"] == "cooldown"
        assert second["undetermined"] is False

    def test_it_reports_which_signals_are_on(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(_ROOT, [], self._values(group_trigger="prefix"))

        assert out["enabled_signals"] == ["at", "reply", "prefix"]

    def test_explicit_timestamps_are_honoured(self) -> None:
        from supervisor import qq_trigger

        out = qq_trigger.dry_run(
            _ROOT,
            [
                {"text": "咪啪", "user_id": 1, "group_id": 9, "at_sec": 0},
                {"text": "咪啪", "user_id": 1, "group_id": 9, "at_sec": 100},
            ],
            self._values(signal_name=True, name_words=["咪啪"], cooldown_group_sec=30),
        )

        assert [r["triggered"] for r in out["results"]] == [True, True]

    @pytest.mark.parametrize(
        "text,at_me,reply_to_bot",
        [
            ("咪啪在吗", False, False),
            ("!生图 猫", False, False),
            ("帮我查天气", True, False),
            ("好的", False, True),
            ("随便聊聊", False, False),
            ("今天天气不错", False, False),
        ],
    )
    def test_the_preview_matches_what_the_adapter_would_do(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        text: str, at_me: bool, reply_to_bot: bool,
    ) -> None:
        """同一份配置、同一条消息，试听结论必须和真实判定一致。

        这是「试听与后端同源」的实测：两边各走自己那条路（适配层从事件提取事实，
        dry-run 从 JSON 构造），结论不许有差别。
        """
        from supervisor import qq_trigger

        runtime = load_runtime(monkeypatch, tmp_path)
        set_live_config(
            runtime,
            signal_name=True, name_words=["咪啪"], signal_prefix=True,
            signal_keyword=True, keyword_words=["天气"],
        )
        module = sys.modules[f"{runtime.__name__}.live_config"]

        event = _event(
            to_me=at_me and not reply_to_bot,
            reply=_reply_from(_BOT_QQ) if reply_to_bot else None,
            get_message=lambda: [{"type": "at", "data": {"qq": _BOT_QQ}}] if at_me else [],
        )
        live_decision = runtime._group_trigger_decision(event, SimpleNamespace(self_id=_BOT_QQ), text)

        preview = qq_trigger.dry_run(
            _ROOT,
            [{"text": text, "user_id": 10001, "group_id": 1, "at_me": at_me, "reply_to_bot": reply_to_bot}],
            module.state_payload()["values"],
        )["results"][0]

        assert preview["triggered"] == live_decision.triggered
        assert preview["signal"] == live_decision.signal
