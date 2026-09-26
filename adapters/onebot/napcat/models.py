"""QQ 适配器内部流转的数据结构。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from platforms.inbound import InboundFacts


@dataclass
class RecentAttachmentEntry:
    attachment: Dict[str, Any]
    created_at: float
    sender_id: str
    message_id: str


@dataclass
class ProactiveTarget:
    target_type: str
    target_id: int
    label: str


@dataclass
class GroupContentRecord:
    """保留最近群消息的结构化副本，供管理员自然语言转发筛选。"""

    formatted_line: str
    text: str
    sender_name: str
    sender_id: str
    timestamp: float
    message_id: str = ""
    seq: int = 0  # 与群历史行同号：deque 淘汰后位置下标会整体左移，挑选只能按这个号
    attachments: List[Dict[str, Any]] | None = None


@dataclass(frozen=True)
class ForwardSelection:
    """qq_forward 按 ref 挑选群消息的结果：命中记录、未命中 ref、错误文案。"""

    records: List[GroupContentRecord]
    missing_refs: tuple[str, ...] = ()
    error: str = ""


# 清理频率不是运营期要调的旋钮（保留期才是），所以是代码常量。
_ATTACHMENT_CLEANUP_INTERVAL_SEC = 3600.0


# data/attachments 同时住着 engine 产物和其他适配器的附件，只清 QQ 自己的会话目录。
# 前缀 = _stable_conversation_key 的三种 prefix 把 ":" 换成 "_"。
_QQ_ATTACHMENT_DIR_PREFIXES = ("qq_group_", "qq_private_", "qq_unknown_")


# Bot 已发出附件消息的 message_id 映射表上限，超出淘汰最旧的。
_MESSAGE_ATTACHMENT_MAX_ITEMS = int(os.environ.get("ONEBOT_MSG_ATTACHMENT_CACHE_ITEMS", "512") or "512")


@dataclass
class QueuedInbound:
    matcher: Any
    bot: Bot
    event: Event
    channel: str
    real_conversation_key: str
    stable_conversation_key: str
    text: str
    attachments: List[Dict[str, Any]]
    is_dm: bool
    platform_updates: Dict[str, Any]
    user_text: str
    entry_node_id: str = ""
    # 本条 inbound 文本带出的最大群历史序号；-1 = 没带历史（私聊 / 直达绘图）。
    history_watermark: int = -1
    # 发言人是不是主动找 bot（@ / 前缀 / 私聊）。只有主动的才给他建记忆档案。
    direct_interaction: bool = True
    merge_identity: tuple[Any, ...] | None = None
    memory_user_ids: tuple[str, ...] = ()
    facts: InboundFacts | None = None


@dataclass
class QueueReplyWaiter:
    event: asyncio.Event
    inbound_seq: int = 0


@dataclass(frozen=True)
class _ApprovalCard:
    """一张审批卡片的锚点：卡片对应的 approval_id 与收件管理员。"""

    approval_id: str
    admin_id: int


@dataclass(frozen=True)
class _ApprovalReplyTarget:
    """引用快捷审批的反查结果。from_card 为真表示确认引用的是 bot 发出的审批卡片。"""

    approval_id: str = ""
    settled_id: str = ""
    from_card: bool = False

    def matched(self) -> bool:
        return bool(self.approval_id or self.settled_id or self.from_card)


@dataclass(frozen=True)
class ForwardSource:
    """一张转发卡片的来源。forward_id 要调接口拉，inline 是上报里直接内嵌的子消息。"""

    forward_id: str = ""
    inline: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ImageSource:
    """一张待下载的图。sticker 决定回填进正文时叫「图片」还是「表情包」。"""

    url: str
    sticker: bool = False


@dataclass
class _ReplayedSender:
    role: str = "member"
    card: str = ""
    nickname: str = ""


@dataclass
class _ReplayedEvent:
    """重启后从暂存恢复的一条入站，只带下游真正会读的字段。"""

    user_id: int
    message_id: int
    time: float
    sender: _ReplayedSender
    text: str
    group_id: int | None = None

    def get_message(self) -> Message:
        return Message(MessageSegment.text(self.text))


@dataclass
class _AttachmentSendLock:
    lock: asyncio.Lock
    users: int = 0
