"""在场判断接进 QQ 适配层之后的整条流程。

rule 怎么分流、安静等待怎么合并、一共问几次模型、试听和真实判定是否逐条对得上、社区回复策略那条路怎么用同一份状态，
都用真实的 rule 和 handler 走；只有问模型、记群历史和提交给 engine 这几步换成记录用的替身。bot 自己说话带来的进场
不在这里：提交被替身接走了，bot 不会真的开口，试听同样不模拟它。
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
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

from _onebot_harness import live_config_module, load_runtime, patch_imported, set_live_config  # noqa: E402
from adapters._shared import quiet_wait as quiet_wait_module  # noqa: E402
from adapters._shared.quiet_wait import QuietWaitScheduler  # noqa: E402
from adapters._shared.trigger_policy import TriggerConfig  # noqa: E402
from clonoth_sdk import IntentVerdict  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor import platform_trigger  # noqa: E402

GROUP = 7
BASELINE_GROUP = 8
BOT_QQ = "900001"
USER_QQ = 10001
T0 = 1_700_000_000.0


class _Sender:
    def __init__(self, name: str = "小明") -> None:
        self.card = name
        self.nickname = name


class _Event:
    """够 rule / handler 用的最小群消息事件。to_me 就是被 @。"""

    def __init__(
        self, text: str, *, message_id: str, group_id: int = GROUP, user_id: int = USER_QQ, at_me: bool = False,
    ) -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.message_type = "group"
        self.message_id = message_id
        self.time = int(T0)
        self.sender = _Sender()
        self.to_me = at_me
        self.reply = None
        self._message = [SimpleNamespace(type="text", data={"text": text})]

    def get_message(self):
        return self._message


class _Clock:
    """判定读的 time.time() 和安静等待的计时都换成手动拨的钟。"""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _FastTimers:
    """计时按拨钟算，真等待压到几毫秒：钟一拨过到点，计时下一次醒来就收。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    async def wait_for(awaitable: Any, timeout: float) -> Any:
        return await asyncio.wait_for(awaitable, min(timeout, 0.005))


@dataclass(frozen=True)
class _Step:
    message_id: str
    at_sec: float
    text: str
    at_me: bool = False
    roll: float = 0.99


# 离场闲聊 → 被 @ → 在场连发 → 安静到点 → 被 @ 接走 → 连发撞上最长等待 → 过期离场。
_TIMELINE = (
    _Step("m00", 0, "今天好热", roll=0.5),
    _Step("m01", 5, "是啊", roll=0.05),
    _Step("m02", 10, "有人吃饭吗", roll=0.9),
    _Step("m03", 20, "在吗", at_me=True),
    _Step("m04", 22, "哈哈"),
    _Step("m05", 23, "确实"),
    _Step("m06", 25, "笑死"),
    _Step("m07", 27, "然后呢"),
    _Step("m08", 40, "好了不说了"),
    _Step("m09", 41, "嗯"),
    _Step("m10", 42, "你觉得呢", at_me=True),
    _Step("m11", 50, "随便聊聊"),
    _Step("m12", 53, "加一"),
    _Step("m13", 56, "对"),
    _Step("m14", 59, "真的假的"),
    _Step("m15", 62, "不信"),
    _Step("m16", 65, "我也是"),
    _Step("m17", 68, "好吧"),
    _Step("m18", 71, "散了散了"),
    _Step("m19", 200, "还有人吗", roll=0.05),
    _Step("m20", 205, "没了", roll=0.5),
)

_DESIGNED = [
    ("history",), ("judged", 1), ("history",), ("explicit",),
    ("superseded",), ("superseded",), ("superseded",), ("judged", 4),
    ("superseded",), ("superseded",), ("explicit",),
    ("superseded",), ("superseded",), ("superseded",), ("superseded",), ("superseded",), ("superseded",),
    ("judged", 7), ("judged", 1), ("judged", 1), ("history",),
]


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    patch_imported(monkeypatch, module, "GroupMessageEvent", _Event, raising=False)
    set_live_config(
        module,
        allowed_groups=[GROUP, BASELINE_GROUP],
        signal_at=True,
        signal_reply=True,
        llm_intent_enabled=True,
        presence_enabled=True,
        presence_window_sec=90,
        absent_judge_probability=0.1,
        quiet_wait_sec=4,
        quiet_max_wait_sec=20,
        proactive_max_replies=3,
        proactive_window_sec=300,
    )
    return module


class _Flow:
    """把一串消息按时间线喂给两个 rule，接下的交给对应的 handler，记下每条最后落到哪。"""

    def __init__(self, runtime: Any, monkeypatch: pytest.MonkeyPatch, *, answer: bool = False) -> None:
        self.runtime = runtime
        self.clock = _Clock()
        self.bot = SimpleNamespace(self_id=BOT_QQ)
        self.answer = answer
        self.rolls: dict[str, float] = {}
        self.explicit: list[str] = []
        self.history_only: list[str] = []
        self.judged: dict[str, tuple[str, int, int]] = {}
        self.settled: list[str] = []
        self.recorded: list[str] = []
        self.processed: list[tuple[str, dict[str, Any]]] = []
        self.tasks: list[asyncio.Task] = []
        presence = runtime.state._adapter.trigger_presence
        monkeypatch.setattr(runtime.inbound, "time", self.clock)
        monkeypatch.setattr(runtime.state._adapter, "quiet_waits", QuietWaitScheduler(presence, clock=self.clock))
        monkeypatch.setattr(quiet_wait_module, "asyncio", _FastTimers())
        monkeypatch.setattr(runtime.inbound, "_trigger_roll", lambda event: self.rolls.get(str(event.message_id), 0.99))
        monkeypatch.setattr(runtime.inbound, "_judge_llm_intent", self._judge)
        monkeypatch.setattr(runtime.handlers, "_record_group_inbound", self._record)
        monkeypatch.setattr(runtime.handlers, "_after_non_trigger_message", self._after)
        monkeypatch.setattr(runtime.handlers, "_record_non_trigger_message", self._declined)
        monkeypatch.setattr(runtime.handlers, "_process_group_message", self._process)

    async def _judge(self, bot: Any, event: Any, text: str, *, source: str = "", merged: int = 1, exclude_seq: int = 0):
        self.judged[str(event.message_id)] = (source, merged, exclude_seq)
        if self.answer:
            return IntentVerdict(agreed=True, decided=True, reason="可以接一句")
        return IntentVerdict(agreed=False, decided=True, reason="群友之间在聊")

    async def _record(self, bot: Any, event: Any):
        self.recorded.append(str(event.message_id))
        return self.runtime.models.RecordedInbound(seq=len(self.recorded), text="", attachments=[], errors=[])

    async def _after(self, bot: Any, event: Any, recorded: Any) -> None:
        self.settled.append(str(event.message_id))

    async def _declined(self, bot: Any, event: Any) -> None:
        self.settled.append(str(event.message_id))

    async def _process(self, bot: Any, event: Any, matcher: Any, **kwargs: Any) -> None:
        self.processed.append((str(event.message_id), kwargs))

    async def deliver(self, step: _Step, *, group: int = GROUP, user_id: int = USER_QQ) -> None:
        self.clock.now = T0 + step.at_sec
        # 先让在等的计时看到新时刻：到点的那一轮在下一条进来之前收掉，和试听的结算顺序一致。
        await asyncio.sleep(0.03)
        self.rolls[step.message_id] = step.roll
        event = _Event(step.text, message_id=step.message_id, group_id=group, user_id=user_id, at_me=step.at_me)
        if await self.runtime.permissions._agent_group_rule(self.bot, event):
            self.explicit.append(step.message_id)
            await self.runtime.handlers._handle_agent(self.bot, event)
            return
        if await self.runtime.permissions._intent_group_rule(self.bot, event):
            self.tasks.append(asyncio.create_task(self.runtime.handlers._handle_intent(self.bot, event)))
            # 让 handler 先登记进安静等待，下一条才进来。
            await asyncio.sleep(0.01)
            return
        self.history_only.append(step.message_id)

    async def finish(self) -> None:
        self.clock.now = T0 + 100_000
        await asyncio.sleep(0.05)
        await asyncio.wait_for(asyncio.gather(*self.tasks), 5)

    def run(self, steps: tuple[_Step, ...], **kwargs: Any) -> None:
        async def main() -> None:
            for step in steps:
                await self.deliver(step, **kwargs)
            await self.finish()

        asyncio.run(main())

    def outcome(self, message_id: str) -> tuple:
        if message_id in self.explicit:
            return ("explicit",)
        if message_id in self.judged:
            return ("judged", self.judged[message_id][1])
        if message_id in self.settled:
            return ("superseded",)
        if message_id in self.history_only:
            return ("history",)
        return ("lost",)


def _preview(runtime: Any, steps: tuple[_Step, ...], *, group: int = GROUP) -> dict[str, Any]:
    values = live_config_module(runtime).state_payload()["values"]
    return platform_trigger.dry_run(QQ_NAPCAT, [
        {"text": step.text, "user_id": USER_QQ, "group_id": group, "at_me": step.at_me, "at_sec": step.at_sec}
        for step in steps
    ], values)


def _expected(entry: dict[str, Any], roll: float) -> tuple:
    """试听的结论翻成真实流程里这条该落到哪。概率门试听报看运行时，按注入的骰子换算。"""
    if entry["triggered"] and not entry["undetermined"]:
        return ("explicit",)
    if entry["quiet"] == "due":
        return ("judged", entry["quiet_merged"])
    if entry["quiet"] in ("merged", "cancelled"):
        return ("superseded",)
    if entry["gate"] == "pending":
        return ("judged", 1) if roll < entry["gate_probability"] else ("history",)
    if entry["signal"] == "llm_intent" and entry["undetermined"] and not entry["blocked_by"]:
        return ("judged", 1)
    return ("history",)


class TestThePreviewMatchesTheRealFlow:
    def test_every_message_lands_where_the_preview_said(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        preview = _preview(runtime, _TIMELINE)
        flow = _Flow(runtime, monkeypatch)

        flow.run(_TIMELINE)

        expected = [_expected(entry, step.roll) for entry, step in zip(preview["results"], _TIMELINE)]
        assert [flow.outcome(step.message_id) for step in _TIMELINE] == expected
        assert expected == _DESIGNED

    def test_the_preview_reports_when_each_wait_was_due(self, runtime) -> None:
        results = _preview(runtime, _TIMELINE)["results"]
        by_id = {step.message_id: entry for step, entry in zip(_TIMELINE, results)}

        assert (by_id["m07"]["quiet_due_at"], by_id["m07"]["quiet_merged"]) == (31.0, 4)
        assert (by_id["m17"]["quiet_due_at"], by_id["m17"]["quiet_merged"]) == (70.0, 7)
        assert (by_id["m18"]["quiet_due_at"], by_id["m18"]["quiet_merged"]) == (75.0, 1)
        assert by_id["m09"]["quiet"] == "cancelled"
        assert by_id["m03"]["present_until"] == 110.0 and by_id["m10"]["present_until"] == 132.0
        assert by_id["m19"]["presence"] == "absent" and by_id["m19"]["gate_probability"] == pytest.approx(0.1)

    def test_the_model_is_asked_far_less_often(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        with_presence = _Flow(runtime, monkeypatch)
        with_presence.run(_TIMELINE)

        set_live_config(runtime, presence_enabled=False)
        baseline = _Flow(runtime, monkeypatch)
        baseline.run(
            tuple(_Step("b" + step.message_id, step.at_sec, step.text, step.at_me, step.roll) for step in _TIMELINE),
            group=BASELINE_GROUP,
        )

        # 在场时一轮连发只问最后一条，离场按概率问；不开的话每条非显式消息都要问一次。
        assert len(with_presence.judged) == 5
        assert len(baseline.judged) == len(_TIMELINE) - 2
        assert sorted(with_presence.judged) == ["m01", "m07", "m17", "m18", "m19"]

    def test_the_merged_count_and_the_source_reach_the_judge(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)

        flow.run(_TIMELINE)

        assert flow.judged["m07"][:2] == ("llm_intent", 4)
        assert flow.judged["m17"][:2] == ("llm_intent", 7)
        assert flow.judged["m01"][:2] == ("llm_intent", 1)

    def test_each_waiting_message_is_recorded_exactly_once(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)

        flow.run(_TIMELINE)

        waiting = [f"m{index:02d}" for index in (*range(4, 10), *range(11, 19))]
        assert sorted(flow.recorded) == waiting
        assert len(flow.recorded) == len(set(flow.recorded))
        # 判的时候把自己那行从上下文里摘掉：它就是【待判定的这一条】。
        assert flow.judged["m07"][2] == flow.recorded.index("m07") + 1

    def test_explicit_messages_go_straight_through(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)

        flow.run(_TIMELINE)

        assert [message_id for message_id, _ in flow.processed] == ["m03", "m10"]
        assert all(kwargs["trigger_source"] == "at" for _, kwargs in flow.processed)
        assert all(kwargs.get("proactive_stamp") is None for _, kwargs in flow.processed)
        assert "m03" not in flow.recorded and "m10" not in flow.recorded


class TestTheWaitAndTheBudget:
    def test_a_yes_after_the_wait_reuses_the_recorded_message_and_counts_once(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _Flow(runtime, monkeypatch, answer=True)

        flow.run((_Step("y1", 0, "在吗", at_me=True), _Step("y2", 2, "哈哈"), _Step("y3", 3, "好耶")))

        assert [message_id for message_id, _ in flow.processed] == ["y1", "y3"]
        kwargs = dict(flow.processed)["y3"]
        assert kwargs["trigger_source"] == "llm_intent"
        assert kwargs["recorded"].seq == flow.recorded.index("y3") + 1
        assert isinstance(kwargs["proactive_stamp"], float)
        presence = runtime.state._adapter.trigger_presence
        assert presence.proactive_count(GROUP, flow.clock.now, 1_000_000) == 1
        assert flow.settled == ["y2"]

    def test_a_full_budget_never_holds_back_being_called(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)
        presence = runtime.state._adapter.trigger_presence
        for offset in range(3):
            presence.record_proactive(GROUP, T0 + offset)

        flow.run((_Step("f1", 10, "随便聊聊", roll=0.0), _Step("f2", 11, "在吗", at_me=True)))

        # 主动回复满了，没人叫的那条连模型都不问；被 @ 的照常接，也不占额度。
        assert flow.outcome("f1") == ("history",)
        assert flow.outcome("f2") == ("explicit",)
        assert presence.proactive_count(GROUP, T0 + 11, 300) == 3

    def test_the_bot_s_own_message_is_never_held_in_a_wait(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)
        runtime.state._adapter.trigger_presence.enter(GROUP, T0)

        flow.run((_Step("s1", 1, "我自己说的"),), user_id=int(BOT_QQ))

        assert flow.recorded == []
        assert flow.judged["s1"][1] == 1

    def test_a_reset_or_an_account_switch_starts_from_absent(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        presence = runtime.state._adapter.trigger_presence
        presence.enter(GROUP, time.time())
        presence.record_proactive(GROUP, time.time())

        runtime.routing._purge_account_scoped_caches()

        assert presence.is_present(GROUP, time.time(), 90) is False
        assert presence.proactive_count(GROUP, time.time(), 300) == 0

    def test_a_conversation_reset_cancels_the_wait_of_that_group(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def main() -> Any:
            scheduler = runtime.state._adapter.quiet_waits
            presence = runtime.state._adapter.trigger_presence
            presence.enter(GROUP, time.time())
            waiter = asyncio.create_task(scheduler.join(GROUP, wait_sec=30, max_wait_sec=60))
            await asyncio.sleep(0.01)
            runtime.history._purge_conversation_side_state("qq_group:presence-reset", {"type": "group", "group_id": GROUP})
            return await asyncio.wait_for(waiter, 2), presence

        handed, presence = asyncio.run(main())

        assert handed is None
        assert presence.is_present(GROUP, time.time(), 90) is False


class TestTheJudgeIsToldTheSituation:
    def test_the_five_facts_travel_with_the_question(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock(T0 + 100)
        monkeypatch.setattr(runtime.inbound, "time", clock)
        presence = runtime.state._adapter.trigger_presence
        presence.bot_spoke(GROUP, T0 + 70)
        presence.record_proactive(GROUP, T0 + 90)
        captured: dict[str, Any] = {}

        async def _judge_reply_intent(**kwargs: Any):
            captured.update(kwargs)
            return IntentVerdict(agreed=False, decided=True)

        monkeypatch.setattr(
            runtime.state._adapter, "client", SimpleNamespace(judge_reply_intent=_judge_reply_intent), raising=False,
        )
        event = _Event("那这样行吗", message_id="j1")

        asyncio.run(runtime.inbound._judge_llm_intent(
            SimpleNamespace(self_id=BOT_QQ), event, "那这样行吗", source="llm_intent", merged=3,
        ))

        assert captured["situation"] == {
            "presence": "present",
            "since_bot_spoke_sec": 30.0,
            "merged_messages": 3,
            "proactive_recent": 1,
            "proactive_window_sec": 300.0,
            "proactive_limit": 3,
            "trigger_source": "llm_intent",
        }

    def test_a_group_where_the_bot_never_spoke_says_so(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def _judge_reply_intent(**kwargs: Any):
            captured.update(kwargs)
            return IntentVerdict(agreed=False, decided=True)

        monkeypatch.setattr(
            runtime.state._adapter, "client", SimpleNamespace(judge_reply_intent=_judge_reply_intent), raising=False,
        )

        asyncio.run(runtime.inbound._judge_llm_intent(
            SimpleNamespace(self_id=BOT_QQ), _Event("在吗", message_id="j2"), "在吗",
        ))

        situation = captured["situation"]
        assert situation["presence"] == "absent" and situation["since_bot_spoke_sec"] is None
        assert situation["merged_messages"] == 1 and situation["trigger_source"] == "llm_intent"

    def test_a_merged_turn_widens_the_context_and_leaves_its_own_line_out(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_live_config(runtime, llm_intent_context_messages=2)
        seqs = [runtime.history._append_group_history(GROUP, f"[12:0{index}] 某人(user_a1): 第{index}句") for index in range(6)]
        captured: dict[str, Any] = {}

        async def _judge_reply_intent(**kwargs: Any):
            captured.update(kwargs)
            return IntentVerdict(agreed=False, decided=True)

        monkeypatch.setattr(
            runtime.state._adapter, "client", SimpleNamespace(judge_reply_intent=_judge_reply_intent), raising=False,
        )

        asyncio.run(runtime.inbound._judge_llm_intent(
            SimpleNamespace(self_id=BOT_QQ), _Event("第5句", message_id="j3"), "第5句",
            merged=3, exclude_seq=seqs[-1],
        ))

        # 原本带 2 行，合并了 3 条再多带 2 行；正在判的那一行（第5句）不在上下文里。
        lines = captured["context_lines"]
        assert len(lines) == 4
        assert ["第1句" in lines[0], "第4句" in lines[-1]] == [True, True]
        assert not any("第5句" in line for line in lines)

    def test_a_failed_judgement_still_means_no(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        flow = _Flow(runtime, monkeypatch)

        async def _failed(*args: Any, **kwargs: Any):
            return IntentVerdict(error="timeout")

        monkeypatch.setattr(runtime.inbound, "_judge_llm_intent", _failed)
        runtime.state._adapter.trigger_presence.enter(GROUP, T0)

        flow.run((_Step("t1", 1, "哈哈"),))

        assert flow.processed == []
        assert flow.settled == ["t1"]


def _coordinated(runtime: Any, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], *, budget_ok: bool = True):
    """接上一份最小的社区功能：回复策略设置、每分钟上限、判定记录。"""
    calls: dict[str, list] = {"allow": [], "decisions": []}
    metadata = {"actor": {"scope": "qq_group:coordinated"}, "policy": {"settings": dict(settings)}, "direct": False}

    async def prepare(bot: Any, event: Any) -> dict[str, Any]:
        return metadata

    async def decision(bot: Any, event: Any, action: str, reason: str = "") -> None:
        calls["decisions"].append(action)

    def allow_response(scope: str, values: dict[str, Any], *, direct: bool = False) -> bool:
        calls["allow"].append(direct)
        return budget_ok

    monkeypatch.setattr(runtime.state._adapter, "features", SimpleNamespace(
        available=True,
        actor=lambda bot, event: metadata["actor"],
        coordinator=SimpleNamespace(
            metadata=lambda actor: metadata, allow_response=allow_response, can_continue=lambda *a, **k: False,
        ),
        prepare=prepare,
        blocked=lambda value, **kwargs: False,
        decision=decision,
    ))
    return calls, metadata


class TestTheCommunityPathSharesTheState:
    def test_the_response_policy_alone_turns_presence_on(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, llm_intent_enabled=False)
        _coordinated(runtime, monkeypatch, {"response_policy_enabled": True})
        event = _Event("随便聊聊", message_id="c0")

        assert runtime.inbound._group_trigger_config(event, SimpleNamespace(self_id=BOT_QQ)).presence_active is True

    def test_a_presence_group_is_not_also_held_to_the_per_minute_budget(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bot = SimpleNamespace(self_id=BOT_QQ)
        event = _Event("随便聊聊", message_id="c1")
        calls, metadata = _coordinated(runtime, monkeypatch, {"response_policy_enabled": True}, budget_ok=False)

        assert runtime.handlers._response_budget_allows(event, bot, metadata, direct=False) is True
        assert calls["allow"] == []

        calls, metadata = _coordinated(
            runtime, monkeypatch, {"response_policy_enabled": True, "interject_presence": False}, budget_ok=False,
        )

        assert runtime.handlers._response_budget_allows(event, bot, metadata, direct=False) is False
        assert calls["allow"] == [False]

    def test_a_coordinated_yes_over_the_budget_becomes_ignore(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        calls, _ = _coordinated(runtime, monkeypatch, {"response_policy_enabled": True})
        presence = runtime.state._adapter.trigger_presence
        processed: list[Any] = []
        declined: list[Any] = []

        async def _judge(*args: Any, **kwargs: Any):
            # 问模型的这几秒里别处已经把额度用完了。
            for offset in range(3):
                presence.record_proactive(GROUP, time.time() - offset)
            return IntentVerdict(agreed=True, decided=True, action="reply", reason="想接")

        async def _process(*args: Any, **kwargs: Any) -> None:
            processed.append(args)

        async def _record(bot: Any, event: Any) -> None:
            declined.append(event.message_id)

        monkeypatch.setattr(runtime.inbound, "_trigger_roll", lambda event: 0.0)
        monkeypatch.setattr(runtime.inbound, "_judge_llm_intent", _judge)
        monkeypatch.setattr(runtime.handlers, "_process_group_message", _process)
        monkeypatch.setattr(runtime.handlers, "_record_non_trigger_message", _record)

        asyncio.run(runtime.handlers._handle_intent(SimpleNamespace(self_id=BOT_QQ), _Event("随便聊聊", message_id="c2")))

        assert calls["decisions"] == ["ignore"]
        assert processed == [] and declined == ["c2"]

    def test_a_reaction_is_not_speech_and_passes_a_full_budget(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, enable_reactions=True)
        calls, _ = _coordinated(runtime, monkeypatch, {"response_policy_enabled": True})
        presence = runtime.state._adapter.trigger_presence
        reaction = sorted(runtime.common._REACT_MODEL_EMOJIS)[0]
        reacted: list[str] = []

        async def _judge(*args: Any, **kwargs: Any):
            for offset in range(3):
                presence.record_proactive(GROUP, time.time() - offset)
            return IntentVerdict(agreed=True, decided=True, action="react", reaction_id=reaction)

        async def _react(bot: Any, event: Any, emoji_id: str, enabled: bool) -> bool:
            reacted.append(emoji_id)
            return True

        async def _record(bot: Any, event: Any) -> None:
            return None

        monkeypatch.setattr(runtime.inbound, "_trigger_roll", lambda event: 0.0)
        monkeypatch.setattr(runtime.inbound, "_judge_llm_intent", _judge)
        monkeypatch.setattr(runtime.outbound_attachments, "_set_message_react", _react)
        monkeypatch.setattr(runtime.handlers, "_record_non_trigger_message", _record)

        asyncio.run(runtime.handlers._handle_intent(SimpleNamespace(self_id=BOT_QQ), _Event("哈哈哈", message_id="c3")))

        assert calls["decisions"] == ["react"]
        assert reacted == [reaction]


class TestConfiguration:
    def test_the_new_keys_reload_hot(self, runtime) -> None:
        set_live_config(
            runtime, presence_window_sec=30, absent_judge_probability=0.25, quiet_wait_sec=2, quiet_max_wait_sec=9,
            proactive_max_replies=5, proactive_window_sec=60,
        )

        config = TriggerConfig.from_live(runtime.state.live)

        assert config.presence_active is True
        assert (
            config.presence_window_sec, config.absent_judge_probability, config.quiet_wait_sec,
            config.quiet_max_wait_sec, config.proactive_max_replies, config.proactive_window_sec,
        ) == (30.0, 0.25, 2.0, 9.0, 5, 60.0)

        set_live_config(runtime, presence_enabled=False)

        assert TriggerConfig.from_live(runtime.state.live).presence_active is False

    def test_a_fresh_install_keeps_presence_off(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        module = load_runtime(monkeypatch, tmp_path / "fresh")
        live = module.state.live

        assert live.presence_enabled is False
        assert (
            live.presence_window_sec, live.absent_judge_probability, live.quiet_wait_sec, live.quiet_max_wait_sec,
            live.proactive_max_replies, live.proactive_window_sec,
        ) == (90.0, 0.1, 4.0, 20.0, 3, 300.0)
        assert tuple(live.reply_to_trigger_sources) == ("at", "reply", "prefix", "name")

    def test_unknown_quote_sources_are_dropped(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger_sources=["at", "teleport", "keyword"])

        assert tuple(runtime.state.live.reply_to_trigger_sources) == ("at", "keyword")

    def test_a_list_of_only_unknown_sources_falls_back_to_the_default(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger_sources=["teleport"])

        assert tuple(runtime.state.live.reply_to_trigger_sources) == ("at", "reply", "prefix", "name")

    def test_an_empty_list_quotes_nobody(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger_sources=[])

        assert tuple(runtime.state.live.reply_to_trigger_sources) == ()

    def test_a_group_override_beats_the_platform(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = SimpleNamespace(self_id=BOT_QQ)
        event = _Event("随便聊聊", message_id="g1")
        monkeypatch.setattr(
            runtime.inbound, "_group_policy_settings",
            lambda event, bot: {"interject_presence": False, "interject_absent_probability": 0.5},
        )

        config = runtime.inbound._group_trigger_config(event, bot)

        assert config.presence is False and config.absent_judge_probability == pytest.approx(0.5)

    def test_an_unset_group_override_follows_the_platform(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runtime.inbound, "_group_policy_settings",
            lambda event, bot: {"interject_presence": None, "interject_absent_probability": None},
        )

        config = runtime.inbound._group_trigger_config(_Event("x", message_id="g2"), SimpleNamespace(self_id=BOT_QQ))

        assert config.presence is True and config.absent_judge_probability == pytest.approx(0.1)
