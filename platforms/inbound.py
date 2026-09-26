"""平台无关的入站消息事实：matcher 把平台事件翻译成它，之后的通用逻辑只吃它。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE
from .ids import normalize_id


@dataclass(frozen=True)
class InboundSender:
    # 匿名或无法核验身份时为空串：落库、判权都不能拿占位号当真人。
    platform_id: str = ""
    # 模型可见的稳定别名，不含真实账号。
    alias: str = ""
    display_name: str = ""
    # 只在群里有意义，私聊恒为空串。
    group_role: str = ""
    anonymous: bool = False
    listed_admin: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform_id", normalize_id(self.platform_id))


@dataclass(frozen=True)
class InboundConversation:
    platform: str
    channel: str
    kind: str
    raw_id: str
    conversation_key: str
    real_conversation_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_id", normalize_id(self.raw_id))

    @property
    def is_group(self) -> bool:
        return self.kind == CHANNEL_GROUP

    @property
    def is_private(self) -> bool:
        return self.kind == CHANNEL_PRIVATE


@dataclass(frozen=True)
class InboundFacts:
    sender: InboundSender
    conversation: InboundConversation
    self_id: str = ""
    message_id: str = ""
    # 原文：未剥前缀、未删名字，点名信号不能在进模型前就丢掉。
    text: str = ""
    text_segments: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    mentions_self: bool = False
    reply_to: str = ""
    reply_to_self: bool = False
    attachments: tuple[Mapping[str, Any], ...] = ()
    # 平台自己把这条标成了表情包（商城表情、收藏表情），不必再靠图形判断。
    marked_sticker: bool = False
    sent_at: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "self_id", normalize_id(self.self_id))
        object.__setattr__(self, "mentions", tuple(m for m in (normalize_id(v) for v in self.mentions) if m))
        object.__setattr__(self, "attachments", tuple(dict(item) for item in self.attachments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": {
                "platform_id": self.sender.platform_id, "alias": self.sender.alias,
                "display_name": self.sender.display_name, "group_role": self.sender.group_role,
                "anonymous": self.sender.anonymous, "listed_admin": self.sender.listed_admin,
            },
            "conversation": {
                "platform": self.conversation.platform, "channel": self.conversation.channel,
                "kind": self.conversation.kind, "raw_id": self.conversation.raw_id,
                "conversation_key": self.conversation.conversation_key,
                "real_conversation_key": self.conversation.real_conversation_key,
            },
            "self_id": self.self_id, "message_id": self.message_id, "text": self.text,
            "text_segments": list(self.text_segments), "mentions": list(self.mentions),
            "mentions_self": self.mentions_self, "reply_to": self.reply_to, "reply_to_self": self.reply_to_self,
            "attachments": [dict(item) for item in self.attachments], "marked_sticker": self.marked_sticker,
            "sent_at": self.sent_at, "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InboundFacts":
        sender = data.get("sender") or {}
        conversation = data.get("conversation") or {}
        return cls(
            sender=InboundSender(
                platform_id=sender.get("platform_id", ""), alias=str(sender.get("alias") or ""),
                display_name=str(sender.get("display_name") or ""), group_role=str(sender.get("group_role") or ""),
                anonymous=bool(sender.get("anonymous")), listed_admin=bool(sender.get("listed_admin")),
            ),
            conversation=InboundConversation(
                platform=str(conversation.get("platform") or ""), channel=str(conversation.get("channel") or ""),
                kind=str(conversation.get("kind") or ""), raw_id=conversation.get("raw_id", ""),
                conversation_key=str(conversation.get("conversation_key") or ""),
                real_conversation_key=str(conversation.get("real_conversation_key") or ""),
            ),
            self_id=data.get("self_id", ""), message_id=str(data.get("message_id") or ""),
            text=str(data.get("text") or ""), text_segments=tuple(str(s) for s in data.get("text_segments") or ()),
            mentions=tuple(data.get("mentions") or ()), mentions_self=bool(data.get("mentions_self")),
            reply_to=str(data.get("reply_to") or ""), reply_to_self=bool(data.get("reply_to_self")),
            attachments=tuple(item for item in data.get("attachments") or () if isinstance(item, Mapping)),
            marked_sticker=bool(data.get("marked_sticker")), sent_at=float(data.get("sent_at") or 0.0),
            extra=dict(data.get("extra") or {}),
        )
