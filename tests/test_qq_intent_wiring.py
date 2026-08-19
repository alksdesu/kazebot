"""适配层这一侧：什么时候去问模型、拿到答案之后怎么收尾。

三件事最容易出错：
1. 判定必须只在显式信号全不命中时才发生 —— 被 @ 了还去问一次模型，是白花一次调用。
2. 判成不接话时要自己把这条消息补进群历史。判定 matcher 把历史记录那层 block 掉了。
3. rule 里不能等模型。等在那儿的是这条消息后面的整条处理链。
"""
from __future__ import annotations

import ast
import asyncio
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

from _onebot_harness import load_runtime, set_live_config  # noqa: E402


class _Sender:
    def __init__(self, name: str = "小明") -> None:
        self.card = name
        self.nickname = name


class _Event:
    """够 rule / handler 用的最小群消息事件。"""

    def __init__(self, runtime: Any, text: str, *, group_id: int = 7, user_id: int = 10001) -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.message_type = "group"
        self.message_id = "m-1"
        self.time = 1_700_000_000
        self.sender = _Sender()
        self.to_me = False
        self.reply = None
        # _message_to_text 用 getattr 读 .type/.data，dict 形态的消息段会被整段跳过。
        self._message = [SimpleNamespace(type="text", data={"text": text})]

    def get_message(self):
        return self._message


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "GroupMessageEvent", _Event, raising=False)
    set_live_config(
        module,
        allowed_groups=[7],
        llm_intent_enabled=True,
        signal_at=True,
        signal_reply=False,
    )
    return module


def _bot() -> Any:
    return SimpleNamespace(self_id="900001")


def _verdict(runtime: Any, **kwargs: Any):
    from clonoth_sdk import IntentVerdict

    return IntentVerdict(**kwargs)


class TestWhenTheModelIsAsked:
    def test_the_event_stub_really_carries_text(self, runtime) -> None:
        # 文本提取不出来的话，下面每条 rule 测试都会因为「空消息谁都不命中」而假绿。
        assert runtime._message_to_text(
            _Event(runtime, "那这样行吗").get_message(), "900001",
        ) == "那这样行吗"

    def test_a_plain_message_reaches_the_rule(self, runtime) -> None:
        assert asyncio.run(runtime._intent_group_rule(_bot(), _Event(runtime, "那这样行吗"))) is True

    def test_an_at_mention_never_reaches_the_rule(self, runtime) -> None:
        # 被 @ 是显式信号，p10 已经接走了。再问一次模型是白花一次调用。
        event = _Event(runtime, "在吗")
        event.to_me = True

        assert asyncio.run(runtime._intent_group_rule(_bot(), event)) is False

    def test_the_switch_gates_the_rule(self, runtime) -> None:
        set_live_config(runtime, llm_intent_enabled=False)

        assert asyncio.run(runtime._intent_group_rule(_bot(), _Event(runtime, "那这样行吗"))) is False

    def test_the_switch_short_circuits_before_the_text_is_parsed(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 关掉之后判定结论本来就不会是 llm_intent，所以这个早退是纯性能短路：
        # 每条群消息省掉一次消息段遍历。删了不会错，但活跃群里是实打实的开销。
        set_live_config(runtime, llm_intent_enabled=False)
        parsed: list[Any] = []
        monkeypatch.setattr(
            runtime, "_message_to_text", lambda *a, **k: parsed.append(a) or "",
        )

        asyncio.run(runtime._intent_group_rule(_bot(), _Event(runtime, "那这样行吗")))

        assert parsed == []

    def test_a_group_outside_the_allowlist_never_reaches_the_rule(self, runtime) -> None:
        assert asyncio.run(
            runtime._intent_group_rule(_bot(), _Event(runtime, "那这样行吗", group_id=999)),
        ) is False

    def test_a_keyword_hit_never_reaches_the_rule(self, runtime) -> None:
        set_live_config(runtime, signal_keyword=True, keyword_words=["报错"])

        assert asyncio.run(runtime._intent_group_rule(_bot(), _Event(runtime, "又报错了"))) is False

    def test_the_rule_does_not_call_the_model(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # rule 里等一次几秒的往返，会把这条消息后面的 matcher 连同群历史记录一起卡住。
        called: list[str] = []

        async def _judge(*_args: Any, **_kwargs: Any):
            called.append("judged")
            return _verdict(runtime, agreed=True, decided=True)

        monkeypatch.setattr(runtime, "_judge_llm_intent", _judge)
        asyncio.run(runtime._intent_group_rule(_bot(), _Event(runtime, "那这样行吗")))

        assert called == []

    def test_the_rule_declines_inside_the_cooldown(self, runtime) -> None:
        # 冷却期内问完只会把答案丢掉：每条不命中消息白花一次调用，还占满 max_inflight。
        set_live_config(runtime, cooldown_group_sec=60)
        event = _Event(runtime, "那这样行吗")
        runtime._trigger_cooldown.record(runtime._trigger_input(event, _bot(), "上一句"))

        assert asyncio.run(runtime._intent_group_rule(_bot(), event)) is False

    def test_the_rule_still_asks_when_the_window_expires_before_the_answer(self, runtime) -> None:
        set_live_config(runtime, cooldown_group_sec=5, llm_intent_timeout_sec=8)
        event = _Event(runtime, "那这样行吗")
        runtime._trigger_cooldown.record(runtime._trigger_input(event, _bot(), "上一句"))

        assert asyncio.run(runtime._intent_group_rule(_bot(), event)) is True


class TestAfterTheModelAnswers:
    def _handle(self, runtime, monkeypatch: pytest.MonkeyPatch, verdict, event=None) -> dict[str, list]:
        seen: dict[str, list] = {"processed": [], "recorded": []}

        async def _judge(*_args: Any, **_kwargs: Any):
            return verdict

        async def _process(bot, evt, matcher):
            seen["processed"].append(evt)

        async def _record(bot, evt):
            seen["recorded"].append(evt)

        monkeypatch.setattr(runtime, "_judge_llm_intent", _judge)
        monkeypatch.setattr(runtime, "_process_group_message", _process)
        monkeypatch.setattr(runtime, "_record_non_trigger_message", _record)
        asyncio.run(runtime._handle_intent(_bot(), event or _Event(runtime, "那这样行吗")))
        return seen

    def test_a_yes_runs_the_normal_pipeline(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._handle(runtime, monkeypatch, _verdict(runtime, agreed=True, decided=True))

        assert len(seen["processed"]) == 1
        assert seen["recorded"] == []

    def test_a_no_records_the_message_into_the_group_history(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # 判定 matcher 把历史记录那层 block 掉了。不补记，这些消息会整段消失。
        seen = self._handle(runtime, monkeypatch, _verdict(runtime, agreed=False, decided=True))

        assert seen["processed"] == []
        assert len(seen["recorded"]) == 1

    def test_a_timeout_records_the_message_and_stays_quiet(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._handle(runtime, monkeypatch, _verdict(runtime, error="timeout"))

        assert seen["processed"] == []
        assert len(seen["recorded"]) == 1

    def test_a_yes_inside_the_cooldown_stays_quiet(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # 意愿判断这条路必须过冷却，否则「开了意愿判断」等于把冷却整层关掉。
        set_live_config(runtime, cooldown_group_sec=60)
        event = _Event(runtime, "那这样行吗")
        runtime._trigger_cooldown.record(runtime._trigger_input(event, _bot(), "上一句"))

        seen = self._handle(runtime, monkeypatch, _verdict(runtime, agreed=True, decided=True), event=event)

        assert seen["processed"] == []
        assert len(seen["recorded"]) == 1

    def test_a_yes_records_the_cooldown(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, cooldown_group_sec=60)
        runtime._trigger_cooldown.forget_group(7)

        self._handle(runtime, monkeypatch, _verdict(runtime, agreed=True, decided=True))

        event = _Event(runtime, "再来一句")
        decision = runtime._group_trigger_decision(event, _bot(), "再来一句")
        assert runtime._trigger_cooldown.remaining(
            runtime._trigger_input(event, _bot(), "再来一句"),
            runtime.TriggerConfig.from_live(runtime.live),
        )[0] > 0
        assert decision.signal  # 判定链路仍然可用，只是被冷却挡住

    def test_a_no_does_not_record_the_cooldown(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, cooldown_group_sec=60)
        runtime._trigger_cooldown.forget_group(7)

        self._handle(runtime, monkeypatch, _verdict(runtime, agreed=False, decided=True))

        event = _Event(runtime, "再来一句")
        assert runtime._trigger_cooldown.remaining(
            runtime._trigger_input(event, _bot(), "再来一句"),
            runtime.TriggerConfig.from_live(runtime.live),
        )[0] == 0


class TestWhatIsSentToTheModel:
    def _sent(self, runtime, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def _judge_qq_intent(**kwargs: Any):
            captured.update(kwargs)
            return _verdict(runtime, agreed=False, decided=True)

        monkeypatch.setattr(
            runtime, "_client", SimpleNamespace(judge_qq_intent=_judge_qq_intent), raising=False,
        )
        event = _Event(runtime, overrides.pop("text", "那这样行吗"))
        asyncio.run(runtime._judge_llm_intent(_bot(), event, "那这样行吗"))
        return captured

    def test_the_real_group_number_is_never_sent(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # 匿名化整套机制就是为了不让真实号码进模型上下文，判定这条新链路不是例外。
        sent = self._sent(runtime, monkeypatch)

        assert sent["conversation_key"] != "qq_group:7"
        assert sent["conversation_key"].startswith("qq_group:")
        assert "10001" not in str(sent)

    def test_the_live_knobs_reach_the_call(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(
            runtime,
            llm_intent_node_id="qq.custom_gate",
            llm_intent_timeout_sec=12.5,
            llm_intent_max_inflight=3,
        )
        sent = self._sent(runtime, monkeypatch)

        assert sent["node_id"] == "qq.custom_gate"
        assert sent["timeout_sec"] == pytest.approx(12.5)
        assert sent["max_inflight"] == 3

    def test_the_bot_names_come_from_the_name_signal_words(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, name_words=["咪啪", "mipa"])
        sent = self._sent(runtime, monkeypatch)

        assert sent["bot_names"] == ["咪啪", "mipa"]

    def test_the_context_window_is_capped_by_the_live_key(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, llm_intent_context_messages=2)
        for index in range(5):
            runtime._append_group_history(7, f"[12:0{index}] 某人(user_a1): 第{index}句")

        sent = self._sent(runtime, monkeypatch)

        assert len(sent["context_lines"]) == 2
        assert "第4句" in sent["context_lines"][-1]

    def test_zero_context_sends_only_the_current_line(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, llm_intent_context_messages=0)
        runtime._append_group_history(7, "[12:00] 某人(user_a1): 在吗")

        sent = self._sent(runtime, monkeypatch)

        assert sent["context_lines"] == []

    def test_a_missing_client_answers_undecided(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "_client", None, raising=False)

        verdict = asyncio.run(runtime._judge_llm_intent(_bot(), _Event(runtime, "在吗"), "在吗"))

        assert verdict.agreed is False
        assert verdict.decided is False


class TestTheDiceIsRolledOncePerMessage:
    """随机插话和意愿判断是级联的：掷中就接话，没中才问模型。

    骰子每判一次现掷一个的话，同一条消息在两个 rule 里会掷出不同结果 —— 先掷不中让
    显式信号那条路放手，再掷中让意愿判断那条路也放手，这条消息就没人处理了。
    """

    def _event(self, runtime, index: int):
        event = _Event(runtime, "随便聊聊")
        event.message_id = f"m-{index}"
        return event

    def test_the_same_message_always_gets_the_same_roll(self, runtime) -> None:
        event = _Event(runtime, "随便聊聊")

        rolls = {runtime._trigger_input(event, _bot(), "随便聊聊").roll for _ in range(20)}

        assert len(rolls) == 1

    def test_exactly_one_rule_takes_a_message(self, runtime) -> None:
        set_live_config(
            runtime,
            signal_at=False, signal_reply=False, signal_random=True,
            random_probability=0.5, llm_intent_enabled=True,
        )

        for index in range(200):
            event = self._event(runtime, index)
            took = asyncio.run(runtime._agent_group_rule(_bot(), event))
            asked = asyncio.run(runtime._intent_group_rule(_bot(), event))

            assert took != asked, f"message {index}: agent={took} intent={asked}"

    def test_different_messages_get_different_rolls(self, runtime) -> None:
        # 全常量的骰子会让「恰好一条路接手」假绿：两条路的分工照样成立，但概率没了。
        rolls = [runtime._trigger_roll(self._event(runtime, index)) for index in range(500)]

        hit_rate = len([roll for roll in rolls if roll < 0.5]) / len(rolls)
        assert 0.3 < hit_rate < 0.7
        assert min(rolls) >= 0.0 and max(rolls) < 1.0

    def test_two_senders_reusing_a_message_id_do_not_share_the_dice(self, runtime) -> None:
        # 有的 OneBot 实现补发事件时 message_id 会重复，身份不能只看它。
        first = _Event(runtime, "随便聊聊", user_id=10001)
        second = _Event(runtime, "随便聊聊", user_id=10002)

        assert runtime._trigger_roll(first) != runtime._trigger_roll(second)

    def test_the_salt_keeps_the_dice_unpredictable(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 盐是常量的话，外人能按 message_id 算出哪条消息会触发随机插话。
        event = _Event(runtime, "随便聊聊")
        before = runtime._trigger_roll(event)

        monkeypatch.setattr(runtime, "_TRIGGER_ROLL_SALT", b"\x00" * 16)

        assert runtime._trigger_roll(event) != before


class TestOneMessageIsJudgedOnce:
    def test_the_two_rules_share_one_judgement(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        judged: list[str] = []
        original = runtime._group_trigger_decision

        def _decide(event: Any, bot: Any, text: str):
            judged.append(text)
            return original(event, bot, text)

        monkeypatch.setattr(runtime, "_group_trigger_decision", _decide)
        event = _Event(runtime, "那这样行吗")

        assert asyncio.run(runtime._agent_group_rule(_bot(), event)) is False
        assert asyncio.run(runtime._intent_group_rule(_bot(), event)) is True
        assert judged == ["那这样行吗"]

    def test_the_cache_does_not_grow_without_bound(self, runtime) -> None:
        for index in range(runtime._TRIGGER_DECISION_CACHE_MAX + 50):
            event = _Event(runtime, "随便聊聊")
            event.message_id = f"m-{index}"
            asyncio.run(runtime._intent_group_rule(_bot(), event))

        assert len(runtime._trigger_decisions) == runtime._TRIGGER_DECISION_CACHE_MAX


class TestTheMatcherTopology:
    def _matcher_calls(self) -> dict[str, dict[str, Any]]:
        """从源码里读回每个 matcher 的注册参数。运行期 stub 把它们吃掉了。"""
        tree = ast.parse((_ROOT / "adapters/onebot/__init__.py").read_text(encoding="utf-8"))
        found: dict[str, dict[str, Any]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in {"on_message", "on_notice"}:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            found[target.id] = {
                kw.arg: ast.literal_eval(kw.value)
                for kw in node.value.keywords
                if kw.arg in {"priority", "block"}
            }
        return found

    def test_the_intent_matcher_runs_after_the_explicit_signals(self) -> None:
        calls = self._matcher_calls()

        assert calls["_intent_matcher"]["priority"] > calls["_agent_matcher"]["priority"]

    def test_the_intent_matcher_runs_before_the_history_recorder(self) -> None:
        calls = self._matcher_calls()

        assert calls["_intent_matcher"]["priority"] < calls["_history_matcher"]["priority"]

    def test_the_intent_matcher_blocks_the_history_recorder(self) -> None:
        # 不 block 的话，判成接话时这条消息会同时进群历史和 inbound 正文，AI 看两遍。
        assert self._matcher_calls()["_intent_matcher"]["block"] is True
