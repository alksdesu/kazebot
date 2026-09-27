"""只在测试里存在的聊天平台 fakechat：平台描述、收发、SDK 回调全用 platforms/、adapters/_shared/、stickers/ 和 SDK 拼成。

不导入任何 QQ 代码。契约测试拿它和 QQ 跑同一套断言，共享层缺了什么，这里最先拼不起来。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable, Mapping

from adapters._shared import outbound
from adapters._shared.aliases import AliasBook
from adapters._shared.approvals import (
    DECISION_ALLOW,
    DECISION_DENY,
    LOOKUP_FOUND,
    LOOKUP_SETTLED,
    LOOKUP_UNKNOWN,
    ApprovalBook,
    ApprovalLookup,
    parse_command,
    parse_reply_verb,
)
from adapters._shared.conversation_hash import ConversationKeys
from adapters._shared.feature_gateway import FeatureGateway, TargetConversation
from adapters._shared.history_window import ClearBarriers, HistoryWindows
from adapters._shared.memory_hints import collect_subjects
from adapters._shared.outbound import (
    EVERYONE,
    SEGMENT_STICKER,
    EmojiPiece,
    MarkerGrammar,
    MentionPiece,
    Piece,
    Rendered,
    TextPiece,
    join_rendered,
    limit_parts,
    mention_as_text,
    planned_stickers,
    resolve_mention,
    split_reply,
    sticker_segment,
    strip_markup,
    tokenize,
)
from adapters._shared.output_guide import OutputGuide, emoji_section, split_section
from adapters._shared.send_guard import (
    AmbiguousAckError,
    IdempotencyClaim,
    MessagePlan,
    OutboundSendContext,
    SendContractError,
    SendError,
    SendNotStartedError,
    TwoPhaseIdempotencyStore,
    context_from_sources,
    idempotency_key,
    protected_claim_send,
)
from adapters._shared.sticker_delivery import ConversationLocks, StickerDelivery
from adapters._shared.trigger_policy import CooldownState, TriggerConfig, TriggerInput, evaluate
from clonoth_sdk.state import SessionState, TriggerInfo
from platforms.descriptor import (
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    HOOK_TRIGGER_POLICY,
    ID_STRING,
    MENTION_NATIVE,
    ChannelDescriptor,
    ImageSupport,
    PlatformCapabilities,
    PlatformDescriptor,
)
from platforms.ids import normalize_id
from platforms.inbound import InboundConversation, InboundFacts, InboundSender
from stickers.collect import sticker_root, store_path
from stickers.roster import RosterRules, offered, prompt_roster, roster_entry
from stickers.store import Sticker, StickerStore

IMAGE_MAX_BYTES = 1024 * 1024

FAKE_CHAT = PlatformDescriptor(
    id="fakechat",
    display_name="Fake Chat",
    short_name="Fake",
    channels=(
        ChannelDescriptor("fake_room", CHANNEL_GROUP),
        ChannelDescriptor("fake_dm", CHANNEL_PRIVATE),
        ChannelDescriptor("fake_unknown", CHANNEL_OTHER),
    ),
    id_shape=ID_STRING,
    hooks={HOOK_TRIGGER_POLICY: "adapters._shared.trigger_policy"},
    capabilities=PlatformCapabilities(
        max_text_length=2000, markdown=True, max_split_parts=3, mention=MENTION_NATIVE, quote=True,
        images=ImageSupport(formats=frozenset({"png", "gif", "jpeg", "webp"}), max_bytes=IMAGE_MAX_BYTES),
    ),
)

# 平台自己的提及写法：服务端从正文里认出它，模型直接写出来驱动也认。
MENTION_SYNTAX = r"<@([\w-]+)>"
FAKE_MARKERS = MarkerGrammar(native_mentions=(MENTION_SYNTAX,))
TRUNCATED_NOTICE = " …(truncated)"
_MENTION_RE = re.compile(MENTION_SYNTAX)

ROOM_TARGET = "room"
DM_TARGET = "dm"
HISTORY_LINES = 20
APPROVAL_TTL_SECONDS = 600.0
FAULT_LOST_ACK = "lost_ack"
FAULT_REFUSED = "refused"
FAULT_HANG = "hang"
FAULTS = frozenset({FAULT_LOST_ACK, FAULT_REFUSED, FAULT_HANG})

# 平台自己的房间角色词表映射到能力档位的词表。
_ROLE_OF = MappingProxyType({"owner": "owner", "moderator": "admin", "member": "member"})

APPROVAL_PENDING_NOTICE = "This operation is waiting for an admin to approve it."


def channel_name(kind: str) -> str:
    return next(channel.name for channel in FAKE_CHAT.channels if channel.kind == kind)


def mention_syntax(user_id: str) -> str:
    return f"<@{user_id}>"


@dataclass(frozen=True)
class ChatUser:
    id: str
    name: str
    role: str = "member"


@dataclass(frozen=True)
class ChatMessage:
    """平台推过来的一条消息。room 为空串是私聊。"""

    id: str
    sender: ChatUser
    text: str
    room: str = ""
    mentions: tuple[str, ...] = ()
    reply_to: str = ""
    sent_at: float = 0.0


@dataclass(frozen=True)
class Posted:
    """bot 发到平台上的一条消息。room 为空串是私聊，这时 user 是收件人。"""

    id: str
    room: str
    user: str
    text: str
    reply_to: str = ""
    mentions: tuple[str, ...] = ()
    # 随这条发出的图，记内容哈希。
    images: tuple[str, ...] = ()


class ChatTimeout(TimeoutError):
    """平台收下了消息，回执没回来。"""


class ChatRefused(ConnectionRefusedError):
    """请求没到平台。"""


class ChatServer:
    """假平台的服务端：记下 bot 发出的每条消息，可以预约下一次发送出错。"""

    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self.posted: list[Posted] = []
        self._faults: list[str] = []

    def fail_next(self, fault: str) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault: {fault!r}")
        self._faults.append(fault)

    async def post(
        self, *, room: str = "", user: str = "", text: str, reply_to: str = "", images: tuple[bytes, ...] = (),
    ) -> str:
        fault = self._faults.pop(0) if self._faults else ""
        if fault == FAULT_REFUSED:
            raise ChatRefused("connection refused")
        message_id = f"p{len(self.posted) + 1}"
        mentions = tuple(dict.fromkeys(_MENTION_RE.findall(text)))
        digests = tuple(hashlib.sha256(image).hexdigest() for image in images)
        self.posted.append(Posted(message_id, room, user, text, reply_to, mentions, digests))
        if fault == FAULT_LOST_ACK:
            raise ChatTimeout(f"acknowledgement lost after {message_id}")
        if fault == FAULT_HANG:
            await asyncio.Event().wait()
        return message_id


def target_identity(target: Mapping[str, Any]) -> str:
    kind = str(target.get("kind") or "")
    if kind not in {ROOM_TARGET, DM_TARGET}:
        raise SendContractError(f"unknown fakechat target kind: {dict(target)!r}")
    identity = normalize_id(target.get("id"), FAKE_CHAT.id_shape)
    if not identity:
        raise SendContractError(f"fakechat {kind} target has a malformed id: {target.get('id')!r}")
    return f"{kind}:{identity}"


def classify_send_failure(exc: BaseException) -> SendError:
    if isinstance(exc, SendError):
        return exc
    if isinstance(exc, ChatRefused):
        return SendNotStartedError(f"fakechat post did not reach the server: {exc}", cause=exc)
    # 认不出的失败一律当回执不明：宁可进死信等人看，也不能冒重复投递的险。
    return AmbiguousAckError(f"fakechat post acknowledgement is unknown: {exc}", cause=exc)


def _image_fits(size: int) -> bool:
    limit = FAKE_CHAT.capabilities.images.max_bytes
    return size > 0 and (not limit or size <= limit)


@dataclass(frozen=True)
class RenderedPart:
    """要发的一条：渲染好的正文，和模型点名要发的表情。表情到发送时才去图库挑，挑中的冻结进发送计划。"""

    text: str
    emoji: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return json.dumps([self.text, list(self.emoji)], ensure_ascii=False)


class FakeChatAdapter:
    """fakechat 适配器：进程内状态、入站翻译与提交、按能力声明渲染出站、受防重保护的发送、表情包选图与记账、审批与上下文重置。"""

    def __init__(
        self,
        workspace: Path,
        server: ChatServer,
        client: Any,
        *,
        admins: Iterable[str] = (),
        rooms: Iterable[str] = (),
        secret: str = "fakechat-test-secret",
    ) -> None:
        shape = FAKE_CHAT.id_shape
        data = Path(workspace) / "data"
        self.server = server
        self.client = client
        self.event_router = None
        self.bot = SimpleNamespace(self_id=server.bot_id)
        self.account = normalize_id(server.bot_id, shape)
        self.admins = frozenset(filter(None, (normalize_id(value, shape) for value in admins)))
        self.rooms = frozenset(filter(None, (normalize_id(value, shape) for value in rooms)))
        self.keys = ConversationKeys(FAKE_CHAT, secret)
        self.aliases = AliasBook(data / "fakechat_aliases.json", workspace=Path(workspace), id_shape=shape)
        self.session_state = SessionState()
        self.history = HistoryWindows(
            capacity=lambda: HISTORY_LINES,
            soft_cap=lambda: HISTORY_LINES,
            watermark=self.session_state.get_high_watermark,
        )
        self.barriers = ClearBarriers()
        self.approvals = ApprovalBook(id_shape=shape, normalize_ref=str, ttl_seconds=APPROVAL_TTL_SECONDS)
        self.outbound = TwoPhaseIdempotencyStore(
            data / "fakechat_outbound.sqlite3", target_identity=target_identity, log_namespace="fakechat",
        )
        self.workspace = Path(workspace)
        self.sticker_store = StickerStore(store_path(self.workspace))
        # 库里有发得出去的图就每轮都摊清单，测试里不掷骰子。
        self.sticker_rules = RosterRules(probability=1.0)
        self.stickers = StickerDelivery(
            plans=self.outbound, open_store=lambda: self.sticker_store, read=self.read_sticker,
        )
        self.sticker_locks = ConversationLocks()
        self.trigger = TriggerConfig()
        self.cooldown = CooldownState()
        self.routes: dict[str, dict[str, Any]] = {}
        self.features = FeatureGateway(
            FakeChatFeatureHost(self), platform=FAKE_CHAT, burst_path=data / "fakechat_bursts.sqlite3",
        )
        self.callbacks = FakeChatCallbacks(self)

    def close(self) -> None:
        self.outbound.close()
        self.sticker_store.close()

    def stable_key(self, real_key: str) -> str:
        return self.keys.stable_key(real_key, bot_scope=self.account)

    def anonymize(self, text: str) -> str:
        return self.aliases.substitute(text)

    def conversation_of(self, message: ChatMessage) -> InboundConversation:
        kind = CHANNEL_GROUP if message.room else CHANNEL_PRIVATE
        raw_id = normalize_id(message.room or message.sender.id, FAKE_CHAT.id_shape)
        channel = channel_name(kind)
        real_key = f"{channel}:{raw_id}"
        return InboundConversation(
            platform=FAKE_CHAT.id, channel=channel, kind=kind, raw_id=raw_id,
            conversation_key=self.stable_key(real_key), real_conversation_key=real_key,
        )

    def facts(self, message: ChatMessage) -> InboundFacts:
        conversation = self.conversation_of(message)
        sender_id = normalize_id(message.sender.id, FAKE_CHAT.id_shape)
        if conversation.is_group:
            self.aliases.group_alias(conversation.raw_id)
        return InboundFacts(
            sender=InboundSender(
                platform_id=sender_id,
                alias=self.aliases.user_alias(sender_id),
                display_name=message.sender.name,
                group_role=_ROLE_OF.get(message.sender.role, "member") if conversation.is_group else "",
                listed_admin=sender_id in self.admins,
            ),
            conversation=conversation,
            self_id=self.account,
            message_id=message.id,
            text=message.text,
            mentions=message.mentions,
            mentions_self=self.account in message.mentions,
            reply_to=message.reply_to,
            reply_to_self=bool(message.reply_to) and any(
                posted.id == message.reply_to for posted in self.server.posted
            ),
            sent_at=message.sent_at or time.time(),
        )

    def platform_auth(self, facts: InboundFacts) -> dict[str, Any]:
        group = facts.conversation.is_group
        return {
            "platform": FAKE_CHAT.id,
            "user_id": facts.sender.platform_id,
            "is_admin": facts.sender.listed_admin,
            "group_role": facts.sender.group_role if group else "",
            "group_scope": facts.conversation.conversation_key if group else "",
            "bot_scope": self.account,
        }

    def target_of(self, conversation: InboundConversation, *, reply_to: str = "") -> dict[str, Any]:
        return {
            "kind": ROOM_TARGET if conversation.is_group else DM_TARGET,
            "id": conversation.raw_id,
            "conversation_key": conversation.conversation_key,
            "reply_to": reply_to,
        }

    def dm_target(self, user_id: str) -> dict[str, Any]:
        user = normalize_id(user_id, FAKE_CHAT.id_shape)
        return {
            "kind": DM_TARGET, "id": user,
            "conversation_key": self.stable_key(f"{channel_name(CHANNEL_PRIVATE)}:{user}"),
        }

    def history_line(self, facts: InboundFacts) -> str:
        name = self.anonymize(facts.sender.display_name) or facts.sender.alias
        return f"{name}({facts.sender.alias}): {self.anonymize(facts.text)}"

    async def receive(self, message: ChatMessage) -> bool:
        """平台推来一条消息。返回这条有没有交给核心。"""
        facts = self.facts(message)
        if facts.conversation.is_group:
            if facts.conversation.raw_id not in self.rooms:
                return False
            seq = self.history.append(facts.conversation.raw_id, self.history_line(facts))
            trigger_input = TriggerInput.from_facts(facts, now=facts.sent_at)
            if not evaluate(trigger_input, self.trigger, self.cooldown).triggered:
                return False
            self.cooldown.record(trigger_input)
            return await self.submit(facts, exclude_seq=seq)
        if await self.handle_approval_reply(facts):
            return False
        if not facts.sender.listed_admin:
            return False
        return await self.submit(facts)

    def inbound_text(self, facts: InboundFacts, *, exclude_seq: int) -> tuple[str, int]:
        lines: list[str] = []
        covered = -1
        if facts.conversation.is_group:
            pending = self.history.pending(facts.conversation.raw_id)
            covered = pending.covered_seq
            lines.append("[recent messages]")
            if pending.lost:
                lines.append(f"({pending.lost} earlier messages did not fit and are missing)")
            lines.extend(line.text for line in pending.lines if line.seq != exclude_seq)
            lines.append("")
        lines.append("[current message]")
        lines.append(f"{facts.sender.alias}: {self.anonymize(facts.text)}")
        return "\n".join(lines), covered

    def memory_hints(self, facts: InboundFacts) -> dict[str, Any]:
        mentioned = [user for user in facts.mentions if user != self.account]
        return {"subjects": collect_subjects(
            sender_alias=facts.sender.alias,
            anonymized_text=self.anonymize(facts.text),
            at_user_ids=mentioned,
            alias_of_user_id=self.aliases.user_alias,
        )}

    def output_guide(self, conversation_key: str) -> OutputGuide:
        """按能力声明给：分条，库里有发得出去的图时加上表情包清单；表态和平台自己的表情 fakechat 都没有。"""
        return OutputGuide.of(
            split_section(FAKE_CHAT),
            emoji_section(FAKE_CHAT, stickers=self.sticker_roster(conversation_key)),
        )

    def sticker_roster(self, conversation_key: str) -> list[str]:
        if not offered(self.sticker_rules, random.random()):
            return []
        rows = prompt_roster(
            self.sticker_store, conversation_key, self.sticker_rules,
            sendable=lambda row: self.sticker_file(row) is not None,
        )
        return [roster_entry(row) for row in rows]

    def sticker_file(self, row: Sticker) -> Path | None:
        """按图片声明判断发不发得出去：落在库外的路径、空文件、超过大小上限的都不发。"""
        root = sticker_root(self.workspace).resolve()
        path = (self.workspace / row.rel_path).resolve()
        try:
            return path if path.is_relative_to(root) and _image_fits(path.stat().st_size) else None
        except OSError:
            return None

    def read_sticker(self, row: Sticker) -> bytes:
        path = self.sticker_file(row)
        if path is None:
            return b""
        try:
            raw = path.read_bytes()
        except OSError:
            return b""
        return raw if _image_fits(len(raw)) else b""

    async def submit(self, facts: InboundFacts, *, exclude_seq: int = 0) -> bool:
        conversation = facts.conversation
        text, covered = self.inbound_text(facts, exclude_seq=exclude_seq)
        channel_id = conversation.raw_id if conversation.is_group else ""
        generation = self.session_state.watermark_generation(channel_id) if channel_id else None
        result = await self.client.submit_inbound(
            channel=conversation.channel,
            conversation_key=conversation.conversation_key,
            text=text,
            message_id=facts.message_id,
            use_context=True,
            platform_auth=self.platform_auth(facts),
            route_hints={
                "platform": FAKE_CHAT.id,
                "channel": conversation.channel,
                "has_image": False,
                "target_type": conversation.kind,
            },
            memory_hints=self.memory_hints(facts),
            output_guide=self.output_guide(conversation.conversation_key).payload(),
        )
        if not result.session_id or not result.accepted:
            return False
        self.barriers.note_inbound(result.inbound_seq)
        if channel_id and covered >= 0:
            if result.inbound_seq:
                self.session_state.register_watermark(result.inbound_seq, channel_id, covered, generation=generation)
            elif generation == self.session_state.watermark_generation(channel_id):
                self.session_state.advance_watermark(channel_id, covered)
        target = self.target_of(conversation, reply_to=facts.message_id)
        self.routes[conversation.conversation_key] = target
        self.session_state.register_session(conversation.conversation_key, result.session_id)
        if result.inbound_seq:
            self.session_state.register_trigger(TriggerInfo(
                inbound_seq=result.inbound_seq,
                conversation_key=conversation.conversation_key,
                session_id=result.session_id,
                is_dm=conversation.is_private,
                platform_data={"target": dict(target)},
            ))
        return True

    async def send(self, target: Mapping[str, Any], text: str, *, context: OutboundSendContext | None = None) -> str:
        """发一条纯文本：先过群里的发送策略，再受防重保护地发出去。"""
        await self.features.check_send(self.bot, target)
        return await self.post(target, text, context=context)

    async def post(
        self, target: Mapping[str, Any], text: str, *, context: OutboundSendContext | None = None,
        content_identity: str = "",
    ) -> str:
        """发一条，发送策略由调用方核验过。同一投递身份只会真正发出一次，回执不明时不重发。"""
        identity = target_identity(target)
        context = context or OutboundSendContext(conversation_key=str(target.get("conversation_key") or ""))
        key = idempotency_key(identity, content_identity or text, event_id=context.idempotency_key or context.event_id)
        return await self.send_claimed(target, key, text, context=context)

    async def post_planned(
        self, target: Mapping[str, Any], part: RenderedPart, *, context: OutboundSendContext, selected: set[str],
    ) -> str:
        """带表情的一条：挑图的结果先冻结进发送计划，重试照原样发、不重新挑；一张都挑不出就只发正文。"""
        conversation_key = str(target.get("conversation_key") or "")
        planned = context.child("stickers")

        async def build() -> dict[str, Any]:
            segments: list[dict[str, Any]] = [{"type": "text", "content": part.text}] if part.text else []
            for name in part.emoji:
                sticker = await self.stickers.pick(
                    name, conversation_key=conversation_key,
                    repeat_window_sec=self.sticker_rules.repeat_window_sec, selected=selected,
                )
                if sticker is not None:
                    segments.append(sticker_segment(sticker.sha256, sticker.content_sha256))
            return {
                "processed_segments": segments,
                "send_identity": hashlib.sha256(part.identity.encode("utf-8")).hexdigest(),
                "selected_images": planned_stickers(segments),
                "conversation_key": conversation_key,
            }

        key = idempotency_key(
            target_identity(target), part.identity, event_id=planned.idempotency_key or planned.event_id,
        )
        plan = await self.outbound.get_or_create_message_plan(key, target, build)
        selected.update(plan.selected_images)
        if not plan.processed_segments:
            return ""
        text = "".join(
            str(segment.get("content") or "") for segment in plan.processed_segments if segment.get("type") == "text"
        )
        return await self.send_claimed(target, plan.claim_key, text, context=planned, plan=plan)

    async def send_claimed(
        self, target: Mapping[str, Any], key: str, text: str, *, context: OutboundSendContext,
        plan: MessagePlan | None = None,
    ) -> str:
        """按防重键发一条，同一个键只真正发出一次。带发送计划的，发之前按计划读图，确认送达后按回执记账。"""
        claim = await self.outbound.begin(key)
        if not claim.acquired and claim.state == "pending":
            resolved = await self.outbound.wait_for_resolution(key)
            claim = await self.outbound.begin(key) if resolved is None else IdempotencyClaim(
                key=key, acquired=False, state=resolved,
            )
        if not claim.acquired:
            message_id = await self.outbound.sent_message_id(key)
            if plan is not None:
                await self.stickers.settle(plan.delivery_id)
            await self.features.bind_reminder_message(self.bot, target, context, message_id)
            return message_id or f"idempotent:{key}"
        room = str(target["id"]) if target["kind"] == ROOM_TARGET else ""
        user = str(target["id"]) if target["kind"] == DM_TARGET else ""

        async def send() -> str:
            return await self.server.post(
                room=room, user=user, text=text, reply_to=str(target.get("reply_to") or ""),
                images=await self.planned_images(plan),
            )

        message_id = await protected_claim_send(self.outbound, claim, send, classify=classify_send_failure)
        if plan is not None:
            await self.stickers.settle(plan.delivery_id)
        await self.features.record_outbound(self.bot, target, text, message_id)
        await self.features.bind_reminder_message(self.bot, target, context, message_id)
        return message_id

    async def planned_images(self, plan: MessagePlan | None) -> tuple[bytes, ...]:
        if plan is None:
            return ()
        segments = [segment for segment in plan.processed_segments if segment.get("type") == SEGMENT_STICKER]
        return tuple([await self.stickers.load(segment) for segment in segments])

    def render(self, text: str) -> list[RenderedPart]:
        """按能力声明把一条模型回复渲染成要发的几条；正文和表情都没有的不算一条。"""
        capabilities = FAKE_CHAT.capabilities
        parts: list[RenderedPart] = []
        for part in limit_parts(split_reply(text), capabilities.max_split_parts):
            body = strip_markup(part, FAKE_MARKERS, keep_markdown=capabilities.markdown)
            pieces = tokenize(body, FAKE_MARKERS)
            chunks = [chunk for piece in pieces for chunk in self.render_piece(piece)]
            message = join_rendered(chunks, capabilities.max_text_length, suffix=TRUNCATED_NOTICE).strip()
            # 发不了图就没有图库可找，表情标记整个丢掉。
            emoji = tuple(piece.name for piece in pieces if isinstance(piece, EmojiPiece)) if capabilities.images else ()
            if message or emoji:
                parts.append(RenderedPart(message, emoji))
        return parts

    def render_piece(self, piece: Piece) -> list[Rendered]:
        if isinstance(piece, TextPiece):
            return [Rendered(piece.text)]
        if isinstance(piece, MentionPiece):
            return self.render_mention(piece)
        # 表情不进正文：图库里挑中的图随这一条一起发。
        return []

    def render_mention(self, piece: MentionPiece) -> list[Rendered]:
        """提及渲染成平台语法，几个之间补空格。没声明全体提及，全体成员和认不出的名字一样退回文字。"""
        if not piece.tokens:
            user = normalize_id(piece.platform_id, FAKE_CHAT.id_shape)
            return [Rendered(mention_syntax(user), atomic=True)] if user else []
        chunks: list[Rendered] = []
        for index, token in enumerate(piece.tokens):
            if index:
                chunks.append(Rendered(" "))
            user = resolve_mention(token, self.aliases.users.real_of, id_shape=FAKE_CHAT.id_shape)
            if user and user != EVERYONE:
                chunks.append(Rendered(mention_syntax(user), atomic=True))
            else:
                chunks.append(Rendered(mention_as_text(token)))
        return chunks

    def reply_history_line(self, text: str) -> str:
        """bot 的回复按模型写的样子记进窗口：下一轮它照自己的写法接话，不该看到平台语法。"""
        body = strip_markup(text, FAKE_MARKERS, keep_markdown=FAKE_CHAT.capabilities.markdown)
        return f"Bot: {self.anonymize(body.replace(outbound.SPLIT_MARKER, ' '))}"

    async def deliver(
        self, target: dict[str, Any], text: str, *, context: OutboundSendContext, record: bool,
    ) -> list[str]:
        """发一条模型回复，返回发出去的每条消息的 ID。"""
        purpose = getattr(context, "purpose", "direct")
        if purpose == "ambient" or "_response_purpose" not in target:
            target["_response_purpose"] = purpose
        parts = self.render(text)
        if not parts:
            return []
        await self.features.check_send(self.bot, target)
        in_room = target["kind"] == ROOM_TARGET
        # 清空上下文时还在飞的回复照发，但它是上一段上下文的产物，不能落回刚清空的窗口。
        stale = in_room and self.barriers.is_stale(target["id"], int(context.source_inbound_seq or 0))
        sent: list[str] = []
        # 这条回复里挑过的图，后面几条不再挑。
        selected: set[str] = set()
        async with self.sticker_locks.hold(str(target.get("conversation_key") or "")):
            for index, part in enumerate(parts):
                # 引用只挂在第一条上，条条都引用同一句就成了刷屏。
                quoted = index == 0 and FAKE_CHAT.capabilities.quote
                destination = target if quoted else {**target, "reply_to": ""}
                child = context.child(f"part:{index}")
                if part.emoji:
                    message_id = await self.post_planned(destination, part, context=child, selected=selected)
                else:
                    message_id = await self.post(
                        destination, part.text, context=child, content_identity=f"part:{index}:{part.text}",
                    )
                if message_id:
                    sent.append(message_id)
        if record and in_room and not stale:
            self.history.append(target["id"], self.reply_history_line(text))
        return sent

    def target_for(self, conversation_key: str, platform_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        stored = (platform_data or {}).get("target") or self.routes.get(conversation_key)
        if not stored:
            raise SendContractError(f"fakechat has no route for conversation {conversation_key!r}")
        return dict(stored)

    async def request_approval(
        self, approval_id: str, operation: str, details: Mapping[str, Any], *, conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        self.approvals.open(
            approval_id, operation=operation, details=dict(details or {}),
            conversation_key=conversation_key, session_id=session_id,
        )
        if not self.admins:
            # 没人能批，挂着只会等到超时。
            await self.client.approve(approval_id, decision=DECISION_DENY, comment="fakechat has no admins to review this.")
            self.approvals.close(approval_id, decision=DECISION_DENY)
            return
        card = f"Approval needed: {operation}\nID: {approval_id}\nReply 'approve' or 'deny' to this message."
        for admin in sorted(self.admins):
            message_id = await self.send(
                self.dm_target(admin), card,
                context=OutboundSendContext(idempotency_key=f"approval:{approval_id}:{admin}"),
            )
            self.approvals.remember_card(message_id, approval_id, admin)
        target = self.routes.get(conversation_key)
        if target:
            await self.send(
                dict(target), APPROVAL_PENDING_NOTICE,
                context=OutboundSendContext(idempotency_key=f"approval-notice:{approval_id}", conversation_key=conversation_key),
            )

    def approval_intent(self, facts: InboundFacts) -> tuple[str, ApprovalLookup] | None:
        """这条私聊是不是审批决定：引用自己收到的卡片回一个动词，或者带引导符的审批命令。"""
        verb = parse_reply_verb(facts.text)
        if verb and facts.reply_to:
            card = self.approvals.resolve_card(facts.reply_to, facts.sender.platform_id)
            if card.approval_id:
                return verb, ApprovalLookup(LOOKUP_FOUND, card.approval_id, card.approval_id)
            if card.settled_id:
                return verb, ApprovalLookup(LOOKUP_SETTLED, card.settled_id, card.settled_id)
            if card.matched():
                return verb, ApprovalLookup(LOOKUP_UNKNOWN, facts.reply_to)
        body = facts.text.strip()
        command = parse_command(body[1:]) if body.startswith("/") else None
        if command is None:
            return None
        decision, token = command
        return decision, self.approvals.lookup(token)

    async def handle_approval_reply(self, facts: InboundFacts) -> bool:
        """私聊里的审批决定。返回这条是不是审批；是的话已经回过话了。"""
        intent = self.approval_intent(facts)
        if intent is None:
            return False
        decision, lookup = intent
        if not facts.sender.listed_admin:
            await self.reply_private(facts, "Only admins can decide approvals.")
            return True
        if lookup.outcome == LOOKUP_SETTLED:
            await self.reply_private(facts, f"Approval {lookup.approval_id} was already handled.")
            return True
        if lookup.outcome != LOOKUP_FOUND:
            await self.reply_private(facts, f"No pending approval matches {lookup.token!r}.")
            return True
        approval_id = lookup.approval_id
        info = await self.approvals.claim(approval_id, facts.sender.platform_id, decision)
        if info is None:
            await self.reply_private(facts, f"Approval {approval_id} was already handled.")
            return True
        try:
            accepted = await self.client.approve(
                approval_id, decision=decision, comment=f"fakechat admin {facts.sender.platform_id} {decision}",
            )
        except Exception:
            await self.approvals.release(approval_id, info)
            raise
        if not accepted:
            await self.approvals.release(approval_id, info)
            await self.reply_private(facts, f"The supervisor refused the decision on {approval_id}.")
            return True
        outcome = "Approved" if decision == DECISION_ALLOW else "Denied"
        await self.reply_private(facts, f"{outcome} {approval_id} ({info.get('operation') or 'unknown'}).")
        return True

    async def reply_private(self, facts: InboundFacts, text: str) -> str:
        return await self.send(
            self.dm_target(facts.sender.platform_id), text,
            context=OutboundSendContext(idempotency_key=f"reply:{facts.message_id}"),
        )

    def reset_context(self, conversation_key: str, reason: str, cleaned_triggers: Iterable[TriggerInfo]) -> None:
        target = self.routes.get(conversation_key)
        if not target or target["kind"] != ROOM_TARGET:
            return
        room = target["id"]
        # compact 只把早期原文换成摘要：留着窗口、重置水位，下一轮把近期原文补回去。
        if reason != "compact":
            self.history.drop(room)
            self.barriers.raise_for(room, [trigger.inbound_seq for trigger in cleaned_triggers])
        self.session_state.reset_channel_watermark(room)
        self.history.forget_gap(room)

    def engine_restarted(self) -> None:
        self.barriers.clear()
        for room in self.history.channels():
            self.session_state.reset_channel_watermark(room)
            self.history.forget_gap(room)


class FakeChatCallbacks:
    """AdapterCallbacks 的 fakechat 实现。没有文件发送能力，带附件的投递明确报错。"""

    def __init__(self, adapter: FakeChatAdapter) -> None:
        self._adapter = adapter

    @staticmethod
    def _refuse_attachments(attachments: list[dict[str, Any]] | None) -> None:
        if attachments:
            raise SendContractError("fakechat cannot send attachments")

    async def send_reply(
        self, trigger: TriggerInfo, text: str, attachments: list[dict[str, Any]], *,
        main_state: Any = None, delivery_context: OutboundSendContext | None = None, **callback_data: Any,
    ) -> None:
        self._refuse_attachments(attachments)
        target = self._adapter.target_for(trigger.conversation_key, trigger.platform_data)
        context = delivery_context or context_from_sources(
            trigger=trigger, main_state=main_state, platform_data=callback_data.get("platform_data"),
        )
        if text:
            await self._adapter.deliver(target, text, context=context, record=True)

    async def send_intermediate_reply(
        self, trigger: TriggerInfo, text: str, *, delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        if not text:
            return
        target = self._adapter.target_for(trigger.conversation_key, trigger.platform_data)
        context = delivery_context or context_from_sources(
            trigger=trigger, platform_data=callback_data.get("platform_data"),
        )
        await self._adapter.deliver(target, text, context=context, record=False)

    async def send_to_channel(
        self, conversation_key: str, text: str, attachments: list[dict[str, Any]], *,
        node_id: str = "", is_final: bool = True, delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        self._refuse_attachments(attachments)
        target = self._adapter.target_for(conversation_key)
        context = delivery_context or context_from_sources(
            platform_data=callback_data.get("platform_data"), conversation_key=conversation_key,
        )
        if text:
            await self._adapter.deliver(target, text, context=context, record=True)

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        return None

    async def update_progress(self, trigger: TriggerInfo, state: Any) -> None:
        return None

    async def create_child_progress(self, task_key: str, state: Any, **kwargs: Any) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: Any) -> None:
        return None

    async def finalize_child_progress(self, task_key: str, state: Any, status: str, **kwargs: Any) -> None:
        return None

    async def show_approval_ui(
        self, approval_id: str, operation: str, details: dict[str, Any], *,
        conversation_key: str = "", session_id: str = "",
    ) -> None:
        await self._adapter.request_approval(
            approval_id, operation, details, conversation_key=conversation_key, session_id=session_id,
        )

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: list[str]) -> None:
        return None

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: list[TriggerInfo]) -> None:
        self._adapter.reset_context(conversation_key, reason, cleaned_triggers)

    async def on_engine_restarted(self, payload: dict[str, Any]) -> None:
        self._adapter.engine_restarted()


class FakeChatFeatureHost:
    """FeatureGateway 对 fakechat 的全部依赖。没有连发合并与社区通知，对应的问法如实答空。"""

    def __init__(self, adapter: FakeChatAdapter) -> None:
        self._adapter = adapter

    @property
    def client(self) -> Any:
        return self._adapter.client

    @property
    def event_router(self) -> Any:
        return self._adapter.event_router

    def inbound_facts(self, bot: Any, event: Any) -> InboundFacts:
        return self._adapter.facts(event)

    async def replay_event(self, bot: Any, facts: InboundFacts) -> Any:
        conversation = facts.conversation
        return ChatMessage(
            id=facts.message_id,
            sender=ChatUser(facts.sender.platform_id, facts.sender.display_name),
            text=facts.text,
            room=conversation.raw_id if conversation.is_group else "",
            mentions=facts.mentions,
            reply_to=facts.reply_to,
            sent_at=facts.sent_at,
        )

    def stable_conversation_key(self, real_conversation_key: str) -> str:
        return self._adapter.stable_key(real_conversation_key)

    def anonymize_text_for_ai(self, text: str) -> str:
        return self._adapter.anonymize(text)

    def message_to_text(self, message: Any, bot_self_id: Any = None) -> str:
        return message.text if isinstance(message, ChatMessage) else str(message or "")

    def is_group_allowed(self, group_id: Any) -> bool:
        return normalize_id(group_id, FAKE_CHAT.id_shape) in self._adapter.rooms

    def is_private_allowed(self, event: Any) -> bool:
        return normalize_id(event.sender.id, FAKE_CHAT.id_shape) in self._adapter.admins

    def is_direct_bot_interaction(self, event: Any, bot: Any, text: str) -> bool:
        facts = self._adapter.facts(event)
        return facts.conversation.is_private or facts.mentions_self or facts.reply_to_self

    def group_trigger_decision_once(self, event: Any, bot: Any, text: str) -> Any:
        facts = self._adapter.facts(event)
        return evaluate(TriggerInput.from_facts(facts, now=facts.sent_at), self._adapter.trigger)

    def get_fallback_bot(self) -> Any:
        return self._adapter.bot

    def request_send_context(self, scope: str, request_identity: str, conversation_key: str = "") -> Any:
        return OutboundSendContext(idempotency_key=f"{scope}:{request_identity}", conversation_key=conversation_key)

    async def group_trigger_text(self, bot: Any, event: Any) -> str:
        return event.text

    async def event_text_with_forward(self, bot: Any, event: Any) -> str:
        return event.text

    async def queue_or_submit_ready(self, item: Any) -> bool:
        return await self._adapter.submit(item.facts)

    def describe_target(self, target: Mapping[str, Any]) -> TargetConversation:
        kind = CHANNEL_GROUP if target.get("kind") == ROOM_TARGET else CHANNEL_PRIVATE
        return TargetConversation(kind=kind, identity=target.get("id"), reply_ref=str(target.get("reply_to") or ""))

    def community_targets(self) -> Iterable[Mapping[str, Any]]:
        return [{"kind": ROOM_TARGET, "id": room} for room in sorted(self._adapter.rooms)]

    def staged_event_fields(self, event: Any) -> Mapping[str, Any]:
        return {"event_time": event.sent_at, "group_id": event.room}

    def restore_inbound(
        self, bot: Any, event: Any, data: Mapping[str, Any], facts: InboundFacts, actor: dict[str, Any],
    ) -> Any:
        return SimpleNamespace(facts=facts, user_text=data.get("user_text", ""), actor=actor)

    async def send_text(self, bot: Any, target: Mapping[str, Any], text: str, *, send_context: Any = None) -> Any:
        return await self._adapter.send(target, text, context=send_context)
