"""OneBot 事件先翻译成 InboundFacts，触发判定、群历史、表情包收集和功能网关只吃它。

暂存的连发要能跨版本恢复：新行带 facts，旧行只有 actor 字段；匿名消息没有能回放的身份，
不能让它卡住整批恢复。
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE  # noqa: E402
from platforms.inbound import InboundConversation, InboundFacts, InboundSender  # noqa: E402

_BOT = SimpleNamespace(self_id="90000")


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[12345], admin_users=[10001])
    yield module
    module._adapter.outbound_idempotency.close()


def _event(segments=None, *, user_id=10001, group_id=12345, role="admin", **extra):
    segments = segments if segments is not None else [{"type": "text", "data": {"text": "hello"}}]
    return SimpleNamespace(
        user_id=user_id, message_id=7, group_id=group_id, time=1000,
        sender=SimpleNamespace(card="", nickname="Alice", role=role),
        get_message=lambda: segments, **extra,
    )


def _facts(**overrides) -> InboundFacts:
    base = InboundFacts(
        sender=InboundSender(platform_id="10001", alias="U1", display_name="Alice", group_role="owner", listed_admin=True),
        conversation=InboundConversation(
            platform="qq", channel="qq_group", kind=CHANNEL_GROUP, raw_id="12345",
            conversation_key="qq_group:" + "a" * 24, real_conversation_key="qq_group:12345",
        ),
        self_id="90000", message_id="7", text="hi @bot", text_segments=("hi ",), mentions=("90000",),
        mentions_self=True, reply_to="3", attachments=({"type": "image", "path": "x.png"},),
        marked_sticker=True, sent_at=1000.0,
    )
    return replace(base, **overrides)


class TestTheStructure:
    def test_it_round_trips_through_a_dict(self) -> None:
        facts = _facts()
        assert InboundFacts.from_dict(facts.to_dict()) == facts

    def test_ids_are_normalised(self) -> None:
        facts = _facts(self_id=90000, mentions=(1, "", "bad id"))
        assert facts.self_id == "90000" and facts.mentions == ("1",)
        assert InboundSender(platform_id=10001).platform_id == "10001"

    def test_attachments_are_copied(self) -> None:
        source = {"type": "image", "path": "x.png"}
        facts = _facts(attachments=(source,))
        source["path"] = "changed.png"
        assert facts.attachments[0]["path"] == "x.png"

    def test_conversation_kind(self) -> None:
        private = replace(_facts().conversation, kind=CHANNEL_PRIVATE)
        assert _facts().conversation.is_group and private.is_private and not private.is_group


class TestTheOneBotTranslation:
    def test_a_group_message(self, runtime) -> None:
        facts = runtime._inbound_facts(_BOT, _event([
            {"type": "at", "data": {"qq": "90000"}},
            {"type": "text", "data": {"text": " 在吗"}},
        ]))
        assert facts.conversation.is_group
        assert facts.conversation.real_conversation_key == "qq_group:12345"
        assert facts.conversation.conversation_key == runtime._stable_conversation_key("qq_group:12345")
        assert facts.sender.platform_id == "10001" and facts.sender.listed_admin
        assert facts.sender.group_role == "admin" and facts.sender.display_name
        assert facts.mentions == ("90000",) and facts.mentions_self
        assert facts.text_segments == (" 在吗",)
        assert facts.sent_at == 1000.0 and facts.message_id == "7"

    def test_the_given_text_wins(self, runtime) -> None:
        assert runtime._inbound_facts(_BOT, _event(), text="原文").text == "原文"

    def test_a_private_message_has_no_group_role(self, runtime) -> None:
        facts = runtime._inbound_facts(_BOT, _event(group_id=None, role="owner"))
        assert facts.conversation.is_private
        assert facts.conversation.real_conversation_key == "qq_private:10001"
        assert facts.sender.group_role == "" and not facts.mentions_self

    def test_an_anonymous_sender_has_no_platform_id(self, runtime) -> None:
        facts = runtime._inbound_facts(_BOT, _event(user_id=80000000, anonymous={"id": 5, "name": "x"}))
        assert facts.sender.anonymous and facts.sender.platform_id == ""
        assert not facts.sender.listed_admin and facts.sender.group_role == ""

    def test_a_marked_sticker(self, runtime) -> None:
        facts = runtime._inbound_facts(_BOT, _event([{"type": "mface", "data": {"emoji_id": "1"}}]))
        assert facts.marked_sticker

    def test_trigger_input_comes_from_the_facts(self, runtime) -> None:
        event = _event([{"type": "at", "data": {"qq": "90000"}}])
        inp = runtime._trigger_input(event, _BOT, "在吗")
        assert (inp.group_id, inp.user_id, inp.at_me, inp.text) == ("12345", "10001", True, "在吗")

    def test_the_actor_is_built_from_the_facts(self, runtime) -> None:
        actor = runtime._adapter.features.actor(_BOT, _event())
        assert actor == runtime._adapter.features.actor(_BOT, runtime._inbound_facts(_BOT, _event()))
        assert actor["user_id"] == "10001" and actor["role"] == "admin" and actor["is_admin"] is True


class TestStagedBursts:
    def test_a_new_row_carries_its_facts(self, runtime) -> None:
        facts = _facts()
        assert runtime._adapter.features.staged_facts({"facts": facts.to_dict()}) == facts

    def test_a_legacy_row_is_rebuilt_from_the_actor(self, runtime) -> None:
        facts = runtime._adapter.features.staged_facts({
            "actor": {"user_id": "10001", "message_id": "7", "display_name": "Alice", "bot_scope": "90000"},
            "group_id": 12345, "channel": "qq_group", "scope": "qq_group:abc", "real_key": "qq_group:12345",
            "user_text": "hi", "event_time": 1000,
        })
        assert facts.conversation.is_group and facts.conversation.raw_id == "12345"
        assert facts.sender.platform_id == "10001" and facts.text == "hi" and facts.sent_at == 1000.0

    def test_a_replayed_event_refreshes_the_group_role(self, runtime) -> None:
        async def call_api(name, **kwargs):
            assert name == "get_group_member_info"
            return {"role": "owner"}

        bot = SimpleNamespace(self_id="90000", call_api=call_api)
        event = asyncio.run(runtime._replay_event(bot, _facts()))
        assert (event.user_id, event.group_id, event.message_id, event.sender.role) == (10001, 12345, 7, "owner")
        assert list(event.get_message())[0]["data"]["text"] == "hi @bot"

    def test_an_anonymous_staged_row_does_not_block_recovery(self, runtime, monkeypatch) -> None:
        features = runtime._adapter.features
        anonymous = _facts(sender=InboundSender(anonymous=True, display_name="匿名成员"))
        rows = [{"facts": anonymous.to_dict(), "scope": "s", "user_text": "x"}]
        monkeypatch.setattr(features.burst_store, "recover", lambda account, started: rows)

        asyncio.run(features.recover_bursts(SimpleNamespace(self_id="90000")))

        assert "90000" in features.recovered_accounts
