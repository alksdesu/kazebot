"""排队 / 连发合并只接上后一条自己的引用和指令；「是不是同一个人」全按核验过的号判。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from clonoth_sdk.state import SessionState, TriggerInfo
from tests._onebot_harness import load_runtime, set_live_config

_HISTORY = "【群聊上下文记录】"


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[12345])
    module.state._adapter.session_state = SessionState()
    monkeypatch.setattr(module.routing, "_remember_route_state", AsyncMock())
    return module


def _event(user_id: int = 10001, message_id: int = 1, *, anonymous: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, message_id=message_id, group_id=12345, time=1000, anonymous=anonymous,
        sender=SimpleNamespace(card="", nickname="Alice", role="member", user_id=user_id),
        get_message=lambda: [{"type": "text", "data": {"text": "x"}}],
    )


def _queued(runtime: Any, text: str, merge_text: str, *, message_id: int, watermark: int, user_id: int = 10001) -> Any:
    return runtime.models.QueuedInbound(
        None, SimpleNamespace(self_id="90000"), _event(user_id, message_id), "qq_group", "qq_group:12345", "scope",
        text, [], False, {"_route_hints": {"source_message_refs": [str(message_id)]}}, merge_text.split(": ", 1)[-1],
        history_watermark=watermark, merge_text=merge_text,
    )


def _full(history: str, instruction: str) -> str:
    return f"{_HISTORY}\n{history}\n\n【当前用户身份】\n...\n\n【当前用户指令】\n{instruction}\n\n请根据以上上下文，执行当前用户的指令并给出回复。"


class TestMerge:
    def test_only_the_follow_up_instruction_is_appended(self, runtime: Any) -> None:
        first = _queued(runtime, _full("[10:00] 乙: 早", "甲(UserA): 帮我查下"), "甲(UserA): 帮我查下", message_id=1, watermark=10)
        second = _queued(runtime, _full("[10:00] 乙: 早\n[10:01] 甲: 帮我查下", "甲(UserA): 顺便看看天气"),
                         "甲(UserA): 顺便看看天气", message_id=2, watermark=12)

        runtime.queueing._merge_queued_inbound(first, second)

        assert first.text.count(_HISTORY) == 1
        assert first.text.endswith("【排队期间追加消息】\n甲(UserA): 顺便看看天气")
        assert first.history_watermark == 10
        assert first.merge_text == "甲(UserA): 帮我查下\n\n【排队期间追加消息】\n甲(UserA): 顺便看看天气"

    def test_a_third_message_keeps_appending(self, runtime: Any) -> None:
        first = _queued(runtime, _full("", "甲: 一"), "甲: 一", message_id=1, watermark=5)
        for index, word in enumerate(("二", "三"), start=2):
            runtime.queueing._merge_queued_inbound(first, _queued(runtime, _full("", f"甲: {word}"), f"甲: {word}", message_id=index, watermark=5))

        assert first.text.count(_HISTORY) == 1
        assert first.merge_text.count("【排队期间追加消息】") == 2
        assert first.user_text == "一\n二\n三"

    def test_a_correction_keeps_its_heading(self, runtime: Any) -> None:
        first = _queued(runtime, _full("", "甲: 订明天的票"), "甲: 订明天的票", message_id=1, watermark=1)
        correction = _queued(runtime, _full("", "甲: 更正 订后天的"), "甲: 更正 订后天的", message_id=2, watermark=1)

        runtime.queueing._merge_queued_inbound(first, correction)

        assert "以这条更正为准" in first.text

    def test_an_item_without_merge_text_appends_its_whole_text(self, runtime: Any) -> None:
        first = _queued(runtime, "old context", "", message_id=1, watermark=-1)
        legacy = _queued(runtime, "legacy whole text", "", message_id=2, watermark=-1)

        runtime.queueing._merge_queued_inbound(first, legacy)

        assert first.text == "old context\n\n【排队期间追加消息】\nlegacy whole text"
        assert first.merge_text == first.text


class TestBurstStaging:
    def test_the_merge_text_survives_a_restart(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = SimpleNamespace(self_id="90000", call_api=AsyncMock(return_value={"role": "member"}))
        runtime.state._adapter.client = SimpleNamespace(request_feature=AsyncMock())
        item = _queued(runtime, _full("", "甲: 一"), "甲: 一", message_id=7, watermark=-1)
        item.bot = bot
        item.stable_conversation_key = runtime.state._adapter.features.actor(bot, item.event)["scope"]
        runtime.state._adapter.features.stage_burst(item)
        runtime.state._adapter.features.started_at = time.time() + 1
        recovered: list[Any] = []

        async def submit(restored: Any) -> bool:
            recovered.append(restored)
            return True

        monkeypatch.setattr(runtime.queueing, "_queue_or_submit_ready", submit)
        asyncio.run(runtime.state._adapter.features.recover_bursts(bot))

        assert [restored.merge_text for restored in recovered] == ["甲: 一"]
        assert "user_id" not in recovered[0].platform_updates

    def test_a_row_staged_before_this_field_existed_falls_back_to_the_text(self, runtime: Any) -> None:
        bot = SimpleNamespace(self_id="90000")
        event = _event(10001, 9)
        data = {
            "is_dm": False, "scope": "scope", "refs": ["9"], "attachments": [], "group_id": 12345,
            "channel": "qq_group", "real_key": "qq_group:12345", "text": "整段", "user_text": "x",
            "entry_node_id": "", "direct_interaction": True, "memory_user_ids": [],
        }

        restored = runtime.queueing._restore_staged_inbound(bot, event, data, runtime.inbound._inbound_facts(bot, event), {})

        assert restored.merge_text == "" and restored.merge_segment == "整段"


class TestSameSender:
    @pytest.mark.parametrize("event,expected", [
        (_event(10001), "10001"),
        (_event(80000000), ""),
        (_event(10001, anonymous={"id": 5, "name": "匿名", "flag": "f"}), ""),
        (SimpleNamespace(user_id=None), ""),
    ])
    def test_the_verified_sender_id(self, runtime: Any, event: Any, expected: str) -> None:
        assert runtime.identity._verified_sender_id(event) == expected

    def test_queued_items_compare_by_the_verified_id(self, runtime: Any) -> None:
        anonymous = _queued(runtime, "t", "t", message_id=1, watermark=-1)
        anonymous.event = _event(80000000, 1, anonymous={"id": 1, "name": "匿名", "flag": "a"})

        assert anonymous.sender_id == ""
        assert not runtime.state._adapter.features.coordinator.can_continue({"scope": "scope", "user_id": ""})

    def test_an_empty_sender_never_picks_up_recent_attachments(self, runtime: Any) -> None:
        entries = [SimpleNamespace(attachment={"type": "image", "path": "a.png"}, created_at=100.0, sender_id="", message_id="m1")]

        assert runtime.media.select_recent_attachment_entries(
            entries, sender_id="", now=101.0, max_age_seconds=180.0, max_items=3,
        ) == []


class TestPreempt:
    @staticmethod
    def _wire(runtime: Any, *, running_seq: int, registered: TriggerInfo) -> AsyncMock:
        set_live_config(runtime, enable_preempt=True)
        runtime.state._adapter.session_state.register_session("group-1", "session")
        runtime.state._adapter.session_state.register_trigger(registered)
        preempt = AsyncMock(return_value=True)
        runtime.state._adapter.client = SimpleNamespace(
            get_running_tasks=AsyncMock(return_value=[SimpleNamespace(task_id="task", is_user_entry=True, source_inbound_seq=running_seq)]),
            preempt_task=preempt,
        )
        return preempt

    @staticmethod
    def _try(runtime: Any, event: Any, *, is_dm: bool = False) -> bool:
        return asyncio.run(runtime.queueing._try_preempt_running_task(
            bot=SimpleNamespace(self_id="90000"), event=event, conversation_key="group-1", inbound_text="hello",
            attachments=None, is_dm=is_dm, platform_updates={"type": "group", "group_id": 12345, "event": event},
        ))

    @pytest.mark.parametrize("is_dm", [False, True])
    def test_a_task_without_its_own_trigger_is_never_injected(self, runtime: Any, is_dm: bool) -> None:
        # 会话里登记着别的 trigger（另一条入站），但这条任务自己的那条已经没了。
        mine = _event(10001, 2)
        other = TriggerInfo(10, "group-1", "session", is_dm, platform_data={"event": mine})
        preempt = self._wire(runtime, running_seq=11, registered=other)

        assert self._try(runtime, mine, is_dm=is_dm) is False
        preempt.assert_not_called()

    def test_two_anonymous_members_are_not_the_same_sender(self, runtime: Any) -> None:
        first = _event(80000000, 1, anonymous={"id": 1, "name": "匿名甲", "flag": "a"})
        second = _event(80000000, 2, anonymous={"id": 2, "name": "匿名乙", "flag": "b"})
        preempt = self._wire(runtime, running_seq=10, registered=TriggerInfo(10, "group-1", "session", False, platform_data={"event": first}))

        assert self._try(runtime, second) is False
        preempt.assert_not_called()

    def test_the_same_member_still_takes_over_their_own_task(self, runtime: Any) -> None:
        first = _event(10001, 1)
        preempt = self._wire(runtime, running_seq=10, registered=TriggerInfo(10, "group-1", "session", False, platform_data={"event": first}))

        assert self._try(runtime, _event(10001, 2)) is True
        preempt.assert_awaited_once()
