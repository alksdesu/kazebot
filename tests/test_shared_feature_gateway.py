"""共享层的社区功能网关只经宿主接口认识平台：换一个字符串 ID、通道名不同的平台照样能用。"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.feature_gateway import FeatureGateway, QuietDeliveryDeferred, TargetConversation  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    CHANNEL_PRIVATE,
    ID_STRING,
    ChannelDescriptor,
    PlatformDescriptor,
)
from platforms.inbound import InboundConversation, InboundFacts, InboundSender  # noqa: E402

_PLATFORM = PlatformDescriptor(
    id="chat",
    display_name="Chat",
    short_name="Chat",
    channels=(ChannelDescriptor("chat_room", CHANNEL_GROUP), ChannelDescriptor("chat_dm", CHANNEL_PRIVATE)),
    id_shape=ID_STRING,
)
_BOT = SimpleNamespace(self_id="bot-7")
_ROOM = {"kind": CHANNEL_GROUP, "id": "room-1"}


def _facts(text: str = "hello") -> InboundFacts:
    return InboundFacts(
        sender=InboundSender(platform_id="alice", display_name="Alice"),
        conversation=InboundConversation(
            platform=_PLATFORM.id, channel="chat_room", kind=CHANNEL_GROUP, raw_id="room-1",
            conversation_key="stable:chat_room:room-1", real_conversation_key="chat_room:room-1",
        ),
        self_id="bot-7",
        message_id="m1",
        text=text,
        sent_at=5.0,
    )


class _Host:
    def __init__(self, client: Any = None) -> None:
        self.client = client
        self.event_router = None
        self.fallback_bot: Any = None
        self.sent: list[tuple[Any, str, Any]] = []
        self.restored: list[dict[str, Any]] = []
        self.submitted: list[Any] = []

    def inbound_facts(self, bot: Any, event: Any) -> InboundFacts:
        return event.facts

    async def replay_event(self, bot: Any, facts: InboundFacts) -> Any:
        return SimpleNamespace(facts=facts)

    def stable_conversation_key(self, real_conversation_key: str) -> str:
        return f"stable:{real_conversation_key}"

    def anonymize_text_for_ai(self, text: str) -> str:
        return text

    def message_to_text(self, message: Any, bot_self_id: Any = None) -> str:
        return str(message)

    def is_group_allowed(self, group_id: Any) -> bool:
        return group_id == "room-1"

    def is_private_allowed(self, event: Any) -> bool:
        return True

    def is_direct_bot_interaction(self, event: Any, bot: Any, text: str) -> bool:
        return True

    def group_trigger_decision_once(self, event: Any, bot: Any, text: str) -> Any:
        return SimpleNamespace(signal="at")

    def get_fallback_bot(self) -> Any:
        return self.fallback_bot

    def request_send_context(self, scope: str, request_identity: str, conversation_key: str = "") -> Any:
        return (scope, request_identity)

    async def group_trigger_text(self, bot: Any, event: Any) -> str:
        return event.facts.text

    async def event_text_with_forward(self, bot: Any, event: Any) -> str:
        return event.facts.text

    async def queue_or_submit_ready(self, item: Any) -> bool:
        self.submitted.append(item)
        return True

    def describe_target(self, target: Any) -> TargetConversation:
        return TargetConversation(kind=target["kind"], identity=target["id"], reply_ref=target.get("reply", ""))

    def community_targets(self) -> list[dict[str, Any]]:
        return [dict(_ROOM)]

    def staged_event_fields(self, event: Any) -> dict[str, Any]:
        return {"event_time": event.facts.sent_at, "group_id": event.facts.conversation.raw_id}

    def restore_inbound(self, bot: Any, event: Any, data: Any, facts: InboundFacts, actor: dict[str, Any]) -> Any:
        self.restored.append({"data": dict(data), "facts": facts, "actor": actor})
        return SimpleNamespace(user_text=data["user_text"], actor=actor)

    async def send_text(self, bot: Any, target: Any, text: str, *, send_context: Any = None) -> Any:
        self.sent.append((target, text, send_context))
        return "msg-1"


def _gateway(tmp_path: Path, host: _Host | None = None, platform: PlatformDescriptor = _PLATFORM) -> FeatureGateway:
    return FeatureGateway(host or _Host(), platform=platform, burst_path=tmp_path / "bursts.sqlite3")


def _item(facts: InboundFacts) -> SimpleNamespace:
    return SimpleNamespace(
        bot=_BOT, event=SimpleNamespace(facts=facts), facts=facts,
        stable_conversation_key=facts.conversation.conversation_key,
        real_conversation_key=facts.conversation.real_conversation_key,
        text=facts.text, user_text=facts.text, attachments=[], is_dm=False, channel="chat_room",
        entry_node_id="", history_watermark=-1, direct_interaction=True, memory_user_ids=(),
        platform_updates={"_route_hints": {"source_message_refs": ["m1"]}},
        sender_id="alice", message_ref="m1",
    )


class TestTargets:
    def test_target_actor_uses_the_platform_channel_names(self, tmp_path: Path) -> None:
        actor = _gateway(tmp_path).target_actor(_BOT, _ROOM)

        assert actor == {
            "scope": "stable:chat_room:room-1", "user_id": "bot-7", "is_admin": True,
            "role": "admin", "channel": "chat_room", "bot_scope": "bot-7",
        }

    def test_a_bot_without_an_id_acts_as_bot(self, tmp_path: Path) -> None:
        actor = _gateway(tmp_path).target_actor(SimpleNamespace(self_id=""), {"kind": CHANNEL_PRIVATE, "id": "bob"})

        assert (actor["scope"], actor["user_id"], actor["bot_scope"]) == ("stable:chat_dm:bob", "bot", "")

    def test_a_platform_without_that_kind_of_channel_is_rejected(self, tmp_path: Path) -> None:
        rooms_only = PlatformDescriptor(
            id="rooms", display_name="Rooms", short_name="Rooms",
            channels=(ChannelDescriptor("rooms_room", CHANNEL_GROUP),),
        )

        with pytest.raises(LookupError, match="private"):
            _gateway(tmp_path, platform=rooms_only).target_actor(_BOT, {"kind": CHANNEL_PRIVATE, "id": "bob"})


class TestCheckSend:
    @staticmethod
    def _quiet_client(calls: list[str]) -> Any:
        async def request_feature(method: str, path: str, **kwargs: Any) -> Any:
            calls.append(path)
            return {"quiet": {"mode": "silent", "expires_at": time.time() + 60}, "settings": {}}

        return SimpleNamespace(request_feature=request_feature)

    def test_private_targets_and_quiet_controls_are_not_checked(self, tmp_path: Path) -> None:
        calls: list[str] = []
        gateway = _gateway(tmp_path, _Host(self._quiet_client(calls)))

        asyncio.run(gateway.check_send(_BOT, {"kind": CHANNEL_PRIVATE, "id": "bob"}))
        asyncio.run(gateway.check_send(_BOT, {**_ROOM, "_quiet_control": True}))

        assert calls == []

    def test_a_quiet_group_defers_ambient_delivery(self, tmp_path: Path) -> None:
        calls: list[str] = []
        gateway = _gateway(tmp_path, _Host(self._quiet_client(calls)))

        with pytest.raises(QuietDeliveryDeferred):
            asyncio.run(gateway.check_send(_BOT, {**_ROOM, "_response_purpose": "ambient"}))
        assert calls == ["/v1/community/state"]


class TestBursts:
    def test_a_staged_burst_is_restored_through_the_host(self, tmp_path: Path) -> None:
        host = _Host(SimpleNamespace(request_feature=None))
        gateway = _gateway(tmp_path, host)
        facts = _facts("pending")
        gateway.stage_burst(_item(facts))
        gateway.started_at = time.time() + 1

        asyncio.run(gateway.recover_bursts(_BOT))

        assert [item.user_text for item in host.submitted] == ["pending"]
        restored = host.restored[0]
        assert restored["data"]["event_time"] == 5.0 and restored["data"]["group_id"] == "room-1"
        assert restored["facts"] == facts
        assert restored["actor"]["scope"] == "stable:chat_room:room-1"
        assert "bot-7" in gateway.recovered_accounts

    def test_a_row_from_a_disallowed_group_is_skipped(self, tmp_path: Path) -> None:
        host = _Host()
        gateway = _gateway(tmp_path, host)
        facts = _facts()
        other = InboundFacts(
            sender=facts.sender,
            conversation=InboundConversation(
                platform=_PLATFORM.id, channel="chat_room", kind=CHANNEL_GROUP, raw_id="room-2",
                conversation_key="stable:chat_room:room-2", real_conversation_key="chat_room:room-2",
            ),
            self_id="bot-7", message_id="m2", text="elsewhere",
        )
        gateway.stage_burst(_item(other))
        gateway.started_at = time.time() + 1

        asyncio.run(gateway.recover_bursts(_BOT))

        assert host.submitted == [] and "bot-7" in gateway.recovered_accounts

    def test_a_legacy_row_is_rebuilt_under_the_gateway_platform(self, tmp_path: Path) -> None:
        facts = _gateway(tmp_path).staged_facts({
            "actor": {"user_id": "alice", "message_id": "m1", "bot_scope": "bot-7"},
            "group_id": "room-1", "channel": "chat_room", "scope": "stable:chat_room:room-1",
            "real_key": "chat_room:room-1", "user_text": "hi", "event_time": 5,
        })

        assert facts.conversation.platform == "chat"
        assert facts.conversation.is_group and facts.conversation.raw_id == "room-1"


def test_poll_sends_pending_notifications_as_plain_text(tmp_path: Path) -> None:
    requests: list[tuple[str, str, Any]] = []

    async def request_feature(method: str, path: str, *, actor: Any = None, body: Any = None, **kwargs: Any) -> Any:
        requests.append((method, path, body))
        if path.endswith("/state"):
            return {"settings": {}}
        if path.endswith("/notifications"):
            return {"items": [{"id": "n/1", "text": "hello"}]}
        return {}

    host = _Host(SimpleNamespace(request_feature=request_feature))
    host.fallback_bot = _BOT
    gateway = _gateway(tmp_path, host)

    async def scenario() -> None:
        task = asyncio.create_task(gateway.poll())
        for _ in range(200):
            if any(path.endswith("/ack") for _, path, _ in requests):
                break
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert host.sent == [(_ROOM, "hello", ("community", "n/1"))]
    assert ("POST", "/v1/community/notifications/n%2F1/ack", {"message_id": "msg-1"}) in requests
