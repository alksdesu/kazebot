from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE
from platforms.inbound import InboundConversation, InboundFacts, InboundSender

from . import community_commands
from adapters._shared.burst_store import BurstStore
from adapters._shared.conversation_coordinator import ConversationCoordinator
from adapters._shared.send_guard import SendError

logger = logging.getLogger(__name__)


class QuietDeliveryDeferred(SendError):
    def __init__(self, until: float):
        super().__init__("本群暂时安静，投递等待恢复", retryable=True, definitely_not_sent=True)
        self.retry_after = max(1.0, until - time.time())
        self.deferred = True


class FeatureHost(Protocol):
    """FeatureGateway 对插件的全部依赖，插件入口按这份接口实现。"""

    workspace: Path
    live: Any
    Message: Any
    MessageSegment: Any
    QueuedInbound: Any

    @property
    def client(self) -> Any: ...

    @property
    def event_router(self) -> Any: ...

    def inbound_facts(self, bot: Any, event: Any) -> InboundFacts: ...

    async def replay_event(self, bot: Any, facts: InboundFacts) -> Any: ...

    def stable_conversation_key(self, real_conversation_key: str) -> str: ...

    def anonymize_text_for_ai(self, text: str) -> str: ...

    def message_to_text(self, message: Any, bot_self_id: Any = None) -> str: ...

    def is_group_allowed(self, group_id: int) -> bool: ...

    def is_private_allowed(self, event: Any) -> bool: ...

    def is_direct_bot_interaction(self, event: Any, bot: Any, text: str) -> bool: ...

    def group_trigger_decision_once(self, event: Any, bot: Any, text: str) -> Any: ...

    def get_fallback_bot(self) -> Any: ...

    def request_send_context(self, scope: str, request_identity: str, conversation_key: str = "") -> Any: ...

    async def group_trigger_text(self, bot: Any, event: Any) -> str: ...

    async def event_text_with_forward(self, bot: Any, event: Any) -> str: ...

    async def queue_or_submit_ready(self, item: Any) -> bool: ...

    async def send_qq_message(
        self, bot: Any, target: dict[str, Any], message: Any, *, send_context: Any = None,
    ) -> Any: ...


class FeatureGateway:
    def __init__(self, host: FeatureHost):
        self.host = host
        self.coordinator = ConversationCoordinator()
        self.poll_task: asyncio.Task | None = None
        self.started_at = time.time()
        self.recovered_accounts: set[str] = set()
        self.burst_store = BurstStore(Path(host.workspace) / "data" / "onebot_bursts.sqlite3")

    def facts_of(self, bot, source) -> InboundFacts:
        return source if isinstance(source, InboundFacts) else self.host.inbound_facts(bot, source)

    def actor(self, bot, source) -> dict:
        facts = self.facts_of(bot, source)
        group = facts.conversation.is_group
        return {
            "scope": facts.conversation.conversation_key,
            "user_id": facts.sender.platform_id,
            "is_admin": facts.sender.listed_admin,
            "role": (facts.sender.group_role or "member") if group else "member",
            "channel": facts.conversation.channel,
            "bot_scope": facts.self_id,
            "message_id": facts.message_id,
            "display_name": facts.sender.display_name,
        }

    def target_actor(self, bot, target) -> dict:
        kind = "qq_group" if target.get("type") == "group" else "qq_private"
        identity = target.get("group_id") if kind == "qq_group" else target.get("user_id")
        return {"scope": self.host.stable_conversation_key(f"{kind}:{identity}"),
                "user_id": str(getattr(bot, "self_id", "") or "bot"), "is_admin": True,
                "role": "admin", "channel": kind, "bot_scope": str(getattr(bot, "self_id", "") or "")}

    @property
    def available(self):
        return callable(getattr(self.host.client, "request_feature", None))

    def modules(self):
        yield community_commands
        for name in ("execution_commands", "reminder_commands", "material_commands"):
            try:
                yield importlib.import_module(f"{__package__}.{name}")
            except ModuleNotFoundError as error:
                if error.name != f"{__package__}.{name}":
                    raise

    @staticmethod
    def command_text(module, text):
        canonicalize = getattr(module, "canonicalize", None)
        return canonicalize(text) if callable(canonicalize) else text.strip().replace("／", "/", 1)

    def command_module(self, text, reply_ref=""):
        for module in self.modules():
            canonical = self.command_text(module, text)
            head = canonical.split(maxsplit=1)
            if not head:
                continue
            commands = getattr(module, "COMMANDS", ())
            if head[0] in commands or head[0].lstrip("/") in commands:
                return module
            matches = getattr(module, "matches", None)
            if callable(matches) and matches(canonical, reply_ref):
                return module
        return None

    async def matches_command(self, bot, event, text, reply_ref=""):
        module = self.command_module(text, reply_ref)
        if module is None:
            return False
        canonical = self.command_text(module, text)
        matches = getattr(module, "matches", None)
        reference = getattr(module, "matches_reference", None)
        if callable(matches) and matches(canonical, reply_ref) and callable(reference):
            actor = self.actor(bot, event)
            if not actor["user_id"] or not self.available:
                return False
            try:
                return await reference(self.host.client, actor, reply_ref)
            except Exception:
                return True
        return True

    async def bind_reminder_message(self, bot, target, context, message_id):
        delivery_id = str(getattr(context, "feature_delivery_id", "") or "")
        if not delivery_id.startswith("reminder:"):
            return
        if not message_id or str(message_id).startswith(("idempotent:", "uploaded:")):
            raise SendError("提醒已发送，但缺少真实平台消息编号；请使用提醒编号操作", retryable=False)
        if not self.available:
            raise SendError("提醒已发送，消息引用绑定等待服务恢复", retryable=True)
        scope = str(getattr(context, "conversation_key", "") or self.target_actor(bot, target)["scope"])
        try:
            await self.host.client.request_feature("POST", "/v1/reminders/delivery-messages", body={
                "delivery_id": delivery_id, "scope": scope,
                "bot_scope": str(getattr(bot, "self_id", "") or ""), "message_id": str(message_id),
            })
        except Exception as error:
            raise SendError("提醒已发送，消息引用绑定等待重试", retryable=True, cause=error) from error

    async def prepare(self, bot, event):
        if not self.available:
            return {}
        facts = self.facts_of(bot, event)
        actor = self.actor(bot, facts)
        if not actor["user_id"]:
            return {}
        text = await self.host.group_trigger_text(bot, event) if facts.conversation.is_group else await self.host.event_text_with_forward(bot, event)
        facts = replace(facts, text=text)
        reply_ref = facts.reply_to
        existing = self.coordinator.metadata(actor)
        if existing:
            return existing
        try:
            result = await self.coordinator.prepare(self.host.client, actor, self.host.anonymize_text_for_ai(text),
                                                    reply_ref=reply_ref, sender_name=facts.sender.display_name)
            result["raw_text"] = text
            result["actor"] = actor
            result["direct"] = bool(not facts.conversation.is_group or self.host.is_direct_bot_interaction(event, bot, text)
                                    or self.host.group_trigger_decision_once(event, bot, text).signal in {"name", "reply", "at", "prefix"})
            journal = await self.record_inbound(facts, actor)
            result["journal_recorded"] = bool((journal or {}).get("recorded"))
            return result
        except Exception:
            logger.warning("feature preparation failed", exc_info=True)
            return {}

    async def record_inbound(self, facts: InboundFacts, actor):
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            return await module.record_message(self.host.client, actor, message_id=actor["message_id"], text=facts.text,
                                        timestamp=facts.sent_at, sender_name=facts.sender.display_name,
                                        reply_ref=facts.reply_to)
        except (ModuleNotFoundError, AttributeError):
            return
        except Exception:
            logger.warning("incoming message journal failed", exc_info=True)

    async def record_outbound(self, bot, target, message, message_id):
        if not self.available or not message_id:
            return
        actor = self.target_actor(bot, target)
        actor["message_id"] = str(message_id)
        text = self.host.message_to_text(message, getattr(bot, "self_id", None))
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            await module.record_message(self.host.client, actor, message_id=str(message_id), text=text,
                                        timestamp=time.time(), sender_name="Bot", direction="outbound",
                                        reply_ref=str(target.get("reply_message_id") or ""))
            await self.host.client.request_feature("POST", "/v1/community/messages", actor=actor, body={
                "message_id": str(message_id), "text": self.host.anonymize_text_for_ai(text),
                "reply_ref": str(target.get("reply_message_id") or ""), "sender_name": "Bot",
                "topic_id": str(target.get("_topic_id") or ""),
            })
        except (ModuleNotFoundError, AttributeError):
            return
        except Exception:
            logger.warning("outgoing message journal failed", exc_info=True)

    async def register_attachments(self, bot, event, attachments):
        if not self.available or not attachments:
            return
        facts = self.facts_of(bot, event)
        actor = self.actor(bot, facts)
        metadata = self.coordinator.metadata(actor)
        if not actor["user_id"] or not (metadata.get("direct") or metadata.get("journal_recorded")):
            return
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            ids = []
            for attachment in attachments:
                if not attachment.get("path"):
                    continue
                source = await module.register_attachment(self.host.client, actor, attachment,
                                                         message_id=actor["message_id"], source_time=facts.sent_at)
                attachment["material_source_id"] = source["id"]
                ids.append(source["id"])
            if metadata.get("journal_recorded"):
                await module.record_message(self.host.client, actor, message_id=actor["message_id"],
                                            text=metadata.get("raw_text", ""), timestamp=facts.sent_at,
                                            sender_name=actor.get("display_name", ""), attachments=ids,
                                            reply_ref=metadata.get("reply_ref", ""))
        except Exception:
            logger.warning("incoming attachment registration failed", exc_info=True)

    async def register_sent_attachments(self, bot, target, attachments, context):
        if not self.available:
            return
        actor = target.get("_feature_actor") or self.target_actor(bot, target)
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            for attachment in attachments:
                if not isinstance(attachment, dict) or not attachment.get("path"):
                    continue
                source = await module.register_attachment(self.host.client, actor, attachment)
        except Exception:
            logger.warning("sent attachment registration failed", exc_info=True)

    def stage_burst(self, item):
        event = item.event
        facts = replace(item.facts or self.host.inbound_facts(item.bot, event), text=item.user_text)
        actor = self.actor(item.bot, facts)
        refs = item.platform_updates.get("_route_hints", {}).get("source_message_refs", [actor["message_id"]])
        data = {"scope": item.stable_conversation_key, "real_key": item.real_conversation_key,
                "text": item.text, "user_text": item.user_text, "attachments": item.attachments,
                "is_dm": item.is_dm, "channel": item.channel, "entry_node_id": item.entry_node_id,
                "history_watermark": item.history_watermark, "direct_interaction": item.direct_interaction,
                "memory_user_ids": item.memory_user_ids, "actor": actor, "refs": refs,
                "route_hints": item.platform_updates.get("_route_hints", {}),
                "response_purpose": item.platform_updates.get("_response_purpose", "direct"),
                "event_time": getattr(event, "time", time.time()), "group_id": getattr(event, "group_id", None),
                "facts": facts.to_dict()}
        self.burst_store.save(item.stable_conversation_key, actor["message_id"], actor["bot_scope"], data)

    async def recover_bursts(self, bot):
        account = str(getattr(bot, "self_id", ""))
        if account in self.recovered_accounts:
            return
        for data in self.burst_store.recover(account, self.started_at):
            staged = self.staged_facts(data)
            if not staged.sender.platform_id or not staged.message_id:
                # 匿名消息没有能回放的真实身份，丢掉这一条，不能让它卡住整批恢复。
                logger.warning("skip staged burst without a replayable sender: scope=%s", data.get("scope"))
                continue
            if staged.conversation.is_group and not self.host.is_group_allowed(int(staged.conversation.raw_id)):
                continue
            event = await self.host.replay_event(bot, staged)
            if staged.conversation.is_private and not self.host.is_private_allowed(event):
                continue
            facts = replace(self.host.inbound_facts(bot, event), text=data["user_text"])
            platform = {"bot": bot, "event": event, "type": "private" if data["is_dm"] else "group", "user_id": event.user_id,
                        "conversation_key": data["scope"], "_route_hints": data.get("route_hints") or {"source_message_refs": data["refs"]},
                        "_response_purpose": data.get("response_purpose", "direct"), "_feature_actor": self.actor(bot, facts),
                        "_source_attachments": data["attachments"]}
            if data["group_id"] is not None:
                platform["group_id"] = data["group_id"]
            item = self.host.QueuedInbound(None, bot, event, data["channel"], data["real_key"], data["scope"],
                                               data["text"], data["attachments"], data["is_dm"], platform, data["user_text"],
                                               entry_node_id=data["entry_node_id"], history_watermark=-1,
                                               direct_interaction=data["direct_interaction"], memory_user_ids=tuple(data["memory_user_ids"]),
                                               facts=facts)
            if not await self.host.queue_or_submit_ready(item):
                return
        self.recovered_accounts.add(account)

    @staticmethod
    def staged_facts(data: Mapping[str, Any]) -> InboundFacts:
        """暂存行里的入站事实。只有 actor 字段的旧行按当时的写法拼回来。"""
        if isinstance(data.get("facts"), Mapping):
            return InboundFacts.from_dict(data["facts"])
        actor = data.get("actor") or {}
        group_id = data.get("group_id")
        return InboundFacts(
            sender=InboundSender(platform_id=actor.get("user_id", ""), display_name=str(actor.get("display_name") or "")),
            conversation=InboundConversation(
                platform=QQ_NAPCAT.id, channel=str(data.get("channel") or ""),
                kind=CHANNEL_GROUP if group_id is not None else CHANNEL_PRIVATE,
                raw_id=group_id if group_id is not None else actor.get("user_id", ""),
                conversation_key=str(data.get("scope") or ""), real_conversation_key=str(data.get("real_key") or ""),
            ),
            self_id=actor.get("bot_scope", ""),
            message_id=str(actor.get("message_id") or ""),
            text=str(data.get("user_text") or ""),
            sent_at=float(data.get("event_time") or 0.0),
        )

    async def handle_command(self, bot, event, text):
        facts = self.facts_of(bot, event)
        actor = self.actor(bot, facts)
        reply_ref = facts.reply_to
        module = self.command_module(text, reply_ref)
        if module is None or not self.available or not actor["user_id"]:
            return None
        text = self.command_text(module, text)
        state = await self.coordinator.policy(self.host.client, actor, refresh=True)
        head = text.split(maxsplit=1)[0]
        controls = getattr(module, "CONTROL_COMMANDS", ())
        control = head in controls or head.lstrip("/") in controls
        is_control = getattr(module, "is_control", None)
        control = control or (callable(is_control) and is_control(text, reply_ref))
        if self.coordinator.quiet_blocks(state, direct=True, control=control):
            return {"text": "", "attachments": [], "control": False}
        try:
            result = await module.handle(self.host.client, actor, text, reply_ref)
            if result is not None:
                result["control"] = control
            self.coordinator.invalidate(actor["scope"])
            if head == "/恢复":
                resume = getattr(self.host.event_router, "resume_deferred", None)
                if resume is not None:
                    resume(actor["scope"])
            return result
        except Exception as error:
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    detail = response.json().get("detail")
                    return {"text": str(detail or "操作未完成，请重试。"), "attachments": [], "control": False}
                except Exception:
                    pass
            logger.warning("feature command failed", exc_info=True)
            return {"text": "操作未完成，请检查功能服务后重试。", "attachments": []}

    def blocked(self, metadata, *, direct=None):
        return self.coordinator.quiet_blocks(metadata.get("policy", {}), direct=metadata.get("direct", False) if direct is None else direct)

    async def check_send(self, bot, target):
        if not self.available or target.get("type") != "group" or target.get("_quiet_control"):
            return
        actor = self.target_actor(bot, target)
        try:
            policy = await self.coordinator.policy(self.host.client, actor, refresh=True)
        except Exception as error:
            raise SendError("无法核验当前群的发送策略，等待重试", retryable=True, definitely_not_sent=True, cause=error) from error
        direct = target.get("_response_purpose", "direct") == "direct"
        if self.coordinator.quiet_blocks(policy, direct=direct):
            raise QuietDeliveryDeferred(float(policy["quiet"]["expires_at"]))

    async def decision(self, bot, event, action, reason=""):
        if not self.available:
            return
        actor = self.actor(bot, event)
        metadata = self.coordinator.metadata(actor)
        metadata["action"] = action
        metadata["classified"] = True
        try:
            await self.host.client.request_feature("POST", "/v1/community/decisions", actor=actor, body={
                "action": action, "reason": reason, "topic_id": metadata.get("topic", {}).get("topic_id", ""),
            })
        except Exception:
            logger.warning("response decision logging failed", exc_info=True)

    async def poll(self):
        while True:
            try:
                bot = self.host.get_fallback_bot()
                if bot is not None and self.available:
                    await self.recover_bursts(bot)
                    for group_id in self.host.live.allowed_groups:
                        target = {"type": "group", "group_id": int(group_id), "_response_purpose": "ambient"}
                        actor = self.target_actor(bot, target)
                        previous = self.coordinator.policies.get(actor["scope"], (0, {}))[1].get("quiet")
                        current = await self.coordinator.policy(self.host.client, actor, refresh=True)
                        if previous and not current.get("quiet"):
                            resume = getattr(self.host.event_router, "resume_deferred", None)
                            if resume is not None:
                                resume(actor["scope"])
                        pending = await self.host.client.request_feature("GET", "/v1/community/notifications", actor=actor)
                        for notification in pending.get("items", []):
                            context = self.host.request_send_context("community", notification["id"], actor["scope"])
                            message_id = await self.host.send_qq_message(bot, target,
                                self.host.Message(self.host.MessageSegment.text(notification["text"])), send_context=context)
                            if message_id:
                                from urllib.parse import quote
                                await self.host.client.request_feature("POST", f"/v1/community/notifications/{quote(notification['id'], safe='')}/ack",
                                                                          actor=actor, body={"message_id": message_id})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("community notification polling failed", exc_info=True)
            await asyncio.sleep(5)
