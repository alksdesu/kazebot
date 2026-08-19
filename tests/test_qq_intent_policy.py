"""模型说完 yes 之后那一段：冷却照样要过，解析不出来一律按不接话。

最要紧的两条：
1. 意愿判断这条路必须走冷却层 —— 漏掉就等于「开了意愿判断 = 关掉冷却」，而它恰好
   是七个信号里最容易刷屏的那个。
2. 判定不可用（超时 / 解析失败 / 任务失败）时一律不接话。反过来是 bot 在群里乱插话。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import qq_intent  # noqa: E402

_ADAPTER_ROOT = _ROOT / "adapters" / "onebot"


def _policy():
    name = "_trigger_policy_intent_under_test"
    spec = importlib.util.spec_from_file_location(name, _ADAPTER_ROOT / "trigger_policy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tp = _policy()


def _input(**overrides: Any):
    base = {"text": "那这样行吗", "group_id": 7, "user_id": 10001, "now": 1000.0}
    base.update(overrides)
    return tp.TriggerInput(**base)


# ── 模型答完之后的收尾判定 ─────────────────────────────────────────

class TestTheVerdictStillGoesThroughCooldown:
    def test_a_yes_triggers(self) -> None:
        decision = tp.resolve_llm_intent(_input(), tp.TriggerConfig(llm_intent=True), agreed=True)

        assert decision.triggered is True
        assert decision.signal == tp.SIGNAL_LLM_INTENT

    def test_a_no_does_not_trigger(self) -> None:
        decision = tp.resolve_llm_intent(_input(), tp.TriggerConfig(llm_intent=True), agreed=False)

        assert decision.triggered is False
        assert decision.signal == tp.SIGNAL_LLM_INTENT
        assert decision.blocked_by == ""

    def test_a_yes_inside_the_group_cooldown_is_held_back(self) -> None:
        config = tp.TriggerConfig(llm_intent=True, cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=990.0))

        decision = tp.resolve_llm_intent(_input(now=1000.0), config, cooldown, agreed=True)

        assert decision.triggered is False
        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN
        assert decision.cooldown_remaining == pytest.approx(20.0)

    def test_a_yes_inside_the_user_cooldown_is_held_back(self) -> None:
        config = tp.TriggerConfig(llm_intent=True, cooldown_user_sec=15.0)
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=995.0))

        decision = tp.resolve_llm_intent(_input(now=1000.0), config, cooldown, agreed=True)

        assert decision.triggered is False
        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN

    def test_the_at_exemption_does_not_leak_onto_the_intent_signal(self) -> None:
        # exempt_at 只豁免被 @ 那一条。意愿判断是「没人叫你，你自己想插话」，
        # 拿豁免等于把冷却整层删掉。
        config = tp.TriggerConfig(llm_intent=True, cooldown_group_sec=30.0, cooldown_exempt_at=True)
        cooldown = tp.CooldownState()
        cooldown.record(_input(now=990.0))

        decision = tp.resolve_llm_intent(_input(now=1000.0), config, cooldown, agreed=True)

        assert decision.triggered is False
        assert decision.blocked_by == tp.BLOCKED_BY_COOLDOWN

    def test_a_no_carries_the_model_reason(self) -> None:
        decision = tp.resolve_llm_intent(
            _input(), tp.TriggerConfig(llm_intent=True), agreed=False, reason="群友之间在对话",
        )

        assert decision.reason == "群友之间在对话"

    def test_the_cooldown_is_not_recorded_by_the_judgement_itself(self) -> None:
        # 记账是调用方的事。这里顺手记一笔，被冷却挡住的那次也会算成「开过口」。
        config = tp.TriggerConfig(llm_intent=True, cooldown_group_sec=30.0)
        cooldown = tp.CooldownState()

        tp.resolve_llm_intent(_input(), config, cooldown, agreed=True)

        assert cooldown.remaining(_input(now=1000.1), config) == (0.0, "")


class TestEvaluateStillHandsOffToTheModel:
    def test_no_signal_plus_llm_intent_reports_undetermined(self) -> None:
        decision = tp.evaluate(_input(), tp.TriggerConfig(llm_intent=True))

        assert decision.triggered is False
        assert decision.signal == tp.SIGNAL_LLM_INTENT
        assert decision.undetermined is True

    def test_llm_intent_off_reports_a_plain_miss(self) -> None:
        decision = tp.evaluate(_input(), tp.TriggerConfig(llm_intent=False))

        assert decision.signal == ""
        assert decision.undetermined is False

    def test_an_explicit_signal_never_reaches_the_model(self) -> None:
        decision = tp.evaluate(_input(at_me=True), tp.TriggerConfig(at=True, llm_intent=True))

        assert decision.triggered is True
        assert decision.signal == tp.SIGNAL_AT


# ── 判定文本解析 ───────────────────────────────────────────────

class TestParsingTheVerdict:
    @pytest.mark.parametrize("raw", ["yes", "YES", "yes|在追问上一条", " yes ", "是", "y"])
    def test_an_affirmative_is_agreed(self, raw: str) -> None:
        result = qq_intent.parse_intent_text(raw)

        assert result.agreed is True
        assert result.error == ""

    @pytest.mark.parametrize("raw", ["no", "NO", "no|群友之间在对话", "否", "不要"])
    def test_a_negative_is_not_agreed(self, raw: str) -> None:
        result = qq_intent.parse_intent_text(raw)

        assert result.agreed is False
        assert result.error == ""

    def test_the_reason_after_the_pipe_survives(self) -> None:
        assert qq_intent.parse_intent_text("no|群友之间在对话").reason == "群友之间在对话"

    def test_a_full_sentence_starting_with_yes_still_parses(self) -> None:
        result = qq_intent.parse_intent_text("yes, 这句话是在问 bot")

        assert result.agreed is True

    def test_an_empty_verdict_is_an_error_and_not_agreed(self) -> None:
        result = qq_intent.parse_intent_text("   ")

        assert result.agreed is False
        assert result.error == "empty"
        assert result.as_dict()["decided"] is False

    def test_an_unparsable_verdict_falls_back_to_not_agreed(self) -> None:
        # 模型答成一段散文时不能猜。猜错的方向是 bot 在群里插话。
        result = qq_intent.parse_intent_text("这个嘛，看情况吧")

        assert result.agreed is False
        assert result.error == "unparsable"

    def test_a_no_prefixed_sentence_is_not_agreed(self) -> None:
        assert qq_intent.parse_intent_text("no because 群友在聊别的").agreed is False


class TestReadingTheFinishedTask:
    def _task(self, result: dict[str, Any]):
        return SimpleNamespace(result=result, task_id="t-1")

    def test_a_finish_text_becomes_the_verdict(self) -> None:
        result = qq_intent.result_from_task(self._task({
            "action": "finish", "result": {"text": "yes|在追问"},
        }))

        assert result.agreed is True
        assert result.reason == "在追问"

    def test_a_failed_task_is_not_agreed(self) -> None:
        result = qq_intent.result_from_task(self._task({"action": "fail", "error": "provider down"}))

        assert result.agreed is False
        assert result.error == "fail"
        assert result.as_dict()["decided"] is False

    def test_a_cancelled_task_is_not_agreed(self) -> None:
        result = qq_intent.result_from_task(self._task({"action": "cancelled"}))

        assert result.agreed is False
        assert result.error == "cancelled"

    def test_a_missing_result_is_not_agreed(self) -> None:
        assert qq_intent.result_from_task(self._task({})).agreed is False

    def test_a_non_dict_result_does_not_explode(self) -> None:
        assert qq_intent.result_from_task(SimpleNamespace(result=None)).agreed is False


# ── instruction 组装：真实号码一个都不能进 ──────────────────────────

class TestTheInstructionCarriesNoRealIdentifiers:
    def test_the_group_and_user_numbers_never_appear(self) -> None:
        instruction = qq_intent.build_instruction(
            text="那这样行吗",
            context_lines=["[12:00] 小明(user_a1): 在吗"],
            bot_names=["咪啪"],
            speaker="小明",
        )

        assert "那这样行吗" in instruction
        assert "咪啪" in instruction
        assert "在吗" in instruction
        assert "987654321" not in instruction

    def test_the_bot_names_line_is_dropped_when_unset(self) -> None:
        instruction = qq_intent.build_instruction(
            text="行吗", context_lines=[], bot_names=[], speaker="",
        )

        assert "平时被叫作" not in instruction
        assert "最近的群聊" not in instruction

    def test_an_empty_message_still_produces_a_judgeable_instruction(self) -> None:
        instruction = qq_intent.build_instruction(
            text="   ", context_lines=[], bot_names=[], speaker="",
        )

        assert "（空消息）" in instruction

    def test_blank_context_lines_are_filtered_out(self) -> None:
        instruction = qq_intent.build_instruction(
            text="行吗", context_lines=["真的一行"], bot_names=[], speaker="",
        )

        assert instruction.count("\n\n") >= 1
        assert "真的一行" in instruction


# ── 在飞表 ────────────────────────────────────────────────────

class TestTheInflightRegistry:
    def test_a_registered_task_is_counted_and_keyed_by_conversation(self) -> None:
        async def _run() -> None:
            registry = qq_intent.IntentRegistry()
            registry.register("t-1", "qq_group:abc")

            assert registry.inflight() == 1
            assert registry.has_conversation("qq_group:abc") is True
            assert registry.has_conversation("qq_group:other") is False

        asyncio.run(_run())

    def test_settling_delivers_the_result_and_frees_the_slot(self) -> None:
        async def _run() -> None:
            registry = qq_intent.IntentRegistry()
            future = registry.register("t-1", "qq_group:abc")

            assert registry.settle("t-1", qq_intent.IntentResult(agreed=True)) is True
            result = await asyncio.wait_for(future, timeout=1.0)

            assert result.agreed is True
            assert registry.inflight() == 0
            assert registry.has_conversation("qq_group:abc") is False

        asyncio.run(_run())

    def test_discarding_frees_the_conversation_slot(self) -> None:
        async def _run() -> None:
            registry = qq_intent.IntentRegistry()
            registry.register("t-1", "qq_group:abc")
            registry.discard("t-1")

            assert registry.inflight() == 0
            assert registry.has_conversation("qq_group:abc") is False
            # 已经放弃等待之后，迟到的结果不该再找到人交付。
            assert registry.settle("t-1", qq_intent.IntentResult(agreed=True)) is False

        asyncio.run(_run())

    def test_settling_an_unknown_task_is_a_no_op(self) -> None:
        async def _run() -> None:
            registry = qq_intent.IntentRegistry()

            assert registry.settle("nope", qq_intent.IntentResult()) is False

        asyncio.run(_run())

    def test_the_result_hops_through_the_loop_thread(self) -> None:
        # 直接 set_result 在小测试里通常也「能过」—— 别的线程 call_soon 不会叫醒
        # selector，loop 要等到下一次醒来才发现，短超时下恰好赶上。真实负载里就是
        # 判定卡满整个超时窗口。所以这里盯的是路径本身。
        async def _run() -> None:
            registry = qq_intent.IntentRegistry()
            registry.register("t-1", "qq_group:abc")
            hops: list[tuple] = []
            registry._pending["t-1"].loop = SimpleNamespace(
                call_soon_threadsafe=lambda fn, *args: hops.append((fn, args)),
            )

            registry.settle("t-1", qq_intent.IntentResult(agreed=True))

            assert len(hops) == 1

        asyncio.run(_run())

    def test_a_result_delivered_from_another_thread_still_arrives(self) -> None:
        # engine worker 的完成回调可能在 branch-route 线程上跑，直接 set_result 会
        # 在别的线程里碰这个 loop 的 future。
        import threading

        async def _run() -> None:
            registry = qq_intent.IntentRegistry()
            future = registry.register("t-1", "qq_group:abc")
            threading.Thread(
                target=registry.settle,
                args=("t-1", qq_intent.IntentResult(agreed=True, reason="跨线程")),
                daemon=True,
            ).start()

            result = await asyncio.wait_for(future, timeout=3.0)

            assert result.agreed is True
            assert result.reason == "跨线程"

        asyncio.run(_run())
