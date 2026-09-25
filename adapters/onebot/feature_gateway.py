from __future__ import annotations

import asyncio
import importlib
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import community_commands
from .conversation_coordinator import ConversationCoordinator
from .burst_store import BurstStore
from .send_contract import OneBotSendError

logger = logging.getLogger(__name__)


class QuietDeliveryDeferred(OneBotSendError):
    def __init__(self, until: float):
        super().__init__("本群暂时安静，投递等待恢复", retryable=True, definitely_not_sent=True)
        self.retry_after = max(1.0, until - time.time())
        self.deferred = True


class FeatureGateway:
    def __init__(self, runtime):
        self.runtime = runtime
        self.coordinator = ConversationCoordinator()
        self.poll_task: asyncio.Task | None = None
        self.started_at = time.time()
        self.recovered_accounts: set[str] = set()
        self.burst_store = BurstStore(Path(runtime.CLONOTH_WORKSPACE) / "data" / "onebot_bursts.sqlite3")

    def actor(self, bot, event) -> dict:
        group_id = getattr(event, "group_id", None)
        requester = self.runtime._requester(event)
        real_key = f"qq_group:{group_id}" if group_id is not None else f"qq_private:{getattr(event, 'user_id', '')}"
        return {
            "scope": self.runtime._stable_conversation_key(real_key),
            "user_id": "" if self.runtime._anonymous_identity(event) else str(getattr(event, "user_id", "") or ""),
            "is_admin": requester.listed_admin, "role": (requester.group_role or "member") if group_id is not None else "member",
            "channel": "qq_group" if group_id is not None else "qq_private",
            "bot_scope": str(getattr(bot, "self_id", "") or ""),
            "message_id": str(getattr(event, "message_id", "") or ""),
            "display_name": self.runtime._event_display_name(event),
        }

    def target_actor(self, bot, target) -> dict:
        kind = "qq_group" if target.get("type") == "group" else "qq_private"
        identity = target.get("group_id") if kind == "qq_group" else target.get("user_id")
        return {"scope": self.runtime._stable_conversation_key(f"{kind}:{identity}"),
                "user_id": str(getattr(bot, "self_id", "") or "bot"), "is_admin": True,
                "role": "admin", "channel": kind, "bot_scope": str(getattr(bot, "self_id", "") or "")}

    @property
    def available(self):
        return callable(getattr(self.runtime._client, "request_feature", None))

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
                return await reference(self.runtime._client, actor, reply_ref)
            except Exception:
                return True
        return True

    async def bind_reminder_message(self, bot, target, context, message_id):
        delivery_id = str(getattr(context, "feature_delivery_id", "") or "")
        if not delivery_id.startswith("reminder:"):
            return
        if not message_id or str(message_id).startswith(("idempotent:", "uploaded:")):
            raise OneBotSendError("提醒已发送，但缺少真实平台消息编号；请使用提醒编号操作", retryable=False)
        if not self.available:
            raise OneBotSendError("提醒已发送，消息引用绑定等待服务恢复", retryable=True)
        scope = str(getattr(context, "conversation_key", "") or self.target_actor(bot, target)["scope"])
        try:
            await self.runtime._client.request_feature("POST", "/v1/reminders/delivery-messages", body={
                "delivery_id": delivery_id, "scope": scope,
                "bot_scope": str(getattr(bot, "self_id", "") or ""), "message_id": str(message_id),
            })
        except Exception as error:
            raise OneBotSendError("提醒已发送，消息引用绑定等待重试", retryable=True, cause=error) from error

    async def prepare(self, bot, event):
        if not self.available:
            return {}
        actor = self.actor(bot, event)
        if not actor["user_id"]:
            return {}
        text = await self.runtime._group_trigger_text(bot, event) if getattr(event, "group_id", None) is not None else await self.runtime._event_text_with_forward(bot, event)
        reply_ref = str(self.runtime._extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None)) or "")
        existing = self.coordinator.metadata(actor)
        if existing:
            return existing
        try:
            result = await self.coordinator.prepare(self.runtime._client, actor, self.runtime._anonymize_text_for_ai(text),
                                                    reply_ref=reply_ref, sender_name=self.runtime._event_display_name(event))
            result["raw_text"] = text
            result["actor"] = actor
            result["direct"] = bool(getattr(event, "group_id", None) is None or self.runtime._is_direct_bot_interaction(event, bot, text)
                                    or self.runtime._group_trigger_decision_once(event, bot, text).signal in {"name", "reply", "at", "prefix"})
            journal = await self.record_inbound(bot, event, actor, text, reply_ref)
            result["journal_recorded"] = bool((journal or {}).get("recorded"))
            return result
        except Exception:
            logger.warning("feature preparation failed", exc_info=True)
            return {}

    async def record_inbound(self, bot, event, actor, text, reply_ref):
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            return await module.record_message(self.runtime._client, actor, message_id=actor["message_id"], text=text,
                                        timestamp=float(getattr(event, "time", None) or time.time()),
                                        sender_name=self.runtime._event_display_name(event), reply_ref=reply_ref)
        except (ModuleNotFoundError, AttributeError):
            return
        except Exception:
            logger.warning("incoming message journal failed", exc_info=True)

    async def record_outbound(self, bot, target, message, message_id):
        if not self.available or not message_id:
            return
        actor = self.target_actor(bot, target)
        actor["message_id"] = str(message_id)
        text = self.runtime._message_to_text(message, getattr(bot, "self_id", None))
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            await module.record_message(self.runtime._client, actor, message_id=str(message_id), text=text,
                                        timestamp=time.time(), sender_name="Bot", direction="outbound",
                                        reply_ref=str(target.get("reply_message_id") or ""))
            await self.runtime._client.request_feature("POST", "/v1/community/messages", actor=actor, body={
                "message_id": str(message_id), "text": self.runtime._anonymize_text_for_ai(text),
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
        actor = self.actor(bot, event)
        metadata = self.coordinator.metadata(actor)
        if not actor["user_id"] or not (metadata.get("direct") or metadata.get("journal_recorded")):
            return
        try:
            module = importlib.import_module(f"{__package__}.material_commands")
            ids = []
            for attachment in attachments:
                if not attachment.get("path"):
                    continue
                source = await module.register_attachment(self.runtime._client, actor, attachment,
                                                         message_id=actor["message_id"], source_time=getattr(event, "time", None))
                attachment["material_source_id"] = source["id"]
                ids.append(source["id"])
            if metadata.get("journal_recorded"):
                await module.record_message(self.runtime._client, actor, message_id=actor["message_id"],
                                            text=metadata.get("raw_text", ""), timestamp=float(getattr(event, "time", None) or time.time()),
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
                source = await module.register_attachment(self.runtime._client, actor, attachment)
        except Exception:
            logger.warning("sent attachment registration failed", exc_info=True)

    def stage_burst(self, item):
        event = item.event
        actor = self.actor(item.bot, event)
        refs = item.platform_updates.get("_route_hints", {}).get("source_message_refs", [actor["message_id"]])
        data = {"scope": item.stable_conversation_key, "real_key": item.real_conversation_key,
                "text": item.text, "user_text": item.user_text, "attachments": item.attachments,
                "is_dm": item.is_dm, "channel": item.channel, "entry_node_id": item.entry_node_id,
                "history_watermark": item.history_watermark, "direct_interaction": item.direct_interaction,
                "memory_user_ids": item.memory_user_ids, "actor": actor, "refs": refs,
                "route_hints": item.platform_updates.get("_route_hints", {}),
                "response_purpose": item.platform_updates.get("_response_purpose", "direct"),
                "event_time": getattr(event, "time", time.time()), "group_id": getattr(event, "group_id", None)}
        self.burst_store.save(item.stable_conversation_key, actor["message_id"], actor["bot_scope"], data)

    async def recover_bursts(self, bot):
        account = str(getattr(bot, "self_id", ""))
        if account in self.recovered_accounts:
            return
        for data in self.burst_store.recover(account, self.started_at):
            if data["group_id"] is not None and not self.runtime._is_group_allowed(int(data["group_id"])):
                continue
            actor = data["actor"]
            event = SimpleNamespace(user_id=int(actor["user_id"]), message_id=int(actor["message_id"]),
                                    time=data["event_time"], sender=SimpleNamespace(role="member", card="", nickname=actor.get("display_name", "")),
                                    get_message=lambda body=data["user_text"]: self.runtime.Message(self.runtime.MessageSegment.text(body)))
            if data["group_id"] is not None:
                event.group_id = int(data["group_id"])
                try:
                    member = await bot.call_api("get_group_member_info", group_id=event.group_id, user_id=event.user_id)
                    event.sender.role = str(member.get("role", "member"))
                except Exception:
                    pass
            elif not self.runtime._is_private_allowed(event):
                continue
            platform = {"bot": bot, "event": event, "type": "private" if data["is_dm"] else "group", "user_id": event.user_id,
                        "conversation_key": data["scope"], "_route_hints": data.get("route_hints") or {"source_message_refs": data["refs"]},
                        "_response_purpose": data.get("response_purpose", "direct"), "_feature_actor": self.actor(bot, event),
                        "_source_attachments": data["attachments"]}
            if data["group_id"] is not None:
                platform["group_id"] = data["group_id"]
            item = self.runtime.QueuedInbound(None, bot, event, data["channel"], data["real_key"], data["scope"],
                                               data["text"], data["attachments"], data["is_dm"], platform, data["user_text"],
                                               entry_node_id=data["entry_node_id"], history_watermark=-1,
                                               direct_interaction=data["direct_interaction"], memory_user_ids=tuple(data["memory_user_ids"]))
            if not await self.runtime._queue_or_submit_ready(item):
                return
        self.recovered_accounts.add(account)

    async def handle_command(self, bot, event, text):
        actor = self.actor(bot, event)
        reply_ref = str(self.runtime._extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None)) or "")
        module = self.command_module(text, reply_ref)
        if module is None or not self.available or not actor["user_id"]:
            return None
        text = self.command_text(module, text)
        state = await self.coordinator.policy(self.runtime._client, actor, refresh=True)
        head = text.split(maxsplit=1)[0]
        controls = getattr(module, "CONTROL_COMMANDS", ())
        control = head in controls or head.lstrip("/") in controls
        is_control = getattr(module, "is_control", None)
        control = control or (callable(is_control) and is_control(text, reply_ref))
        if self.coordinator.quiet_blocks(state, direct=True, control=control):
            return {"text": "", "attachments": [], "control": False}
        try:
            result = await module.handle(self.runtime._client, actor, text, reply_ref)
            if result is not None:
                result["control"] = control
            self.coordinator.invalidate(actor["scope"])
            if head == "/恢复":
                resume = getattr(self.runtime._event_router, "resume_deferred", None)
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
            policy = await self.coordinator.policy(self.runtime._client, actor, refresh=True)
        except Exception as error:
            raise OneBotSendError("无法核验当前群的发送策略，等待重试", retryable=True, definitely_not_sent=True, cause=error) from error
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
            await self.runtime._client.request_feature("POST", "/v1/community/decisions", actor=actor, body={
                "action": action, "reason": reason, "topic_id": metadata.get("topic", {}).get("topic_id", ""),
            })
        except Exception:
            logger.warning("response decision logging failed", exc_info=True)

    async def poll(self):
        while True:
            try:
                bot = self.runtime._get_fallback_bot()
                if bot is not None and self.available:
                    await self.recover_bursts(bot)
                    for group_id in self.runtime.live.allowed_groups:
                        target = {"type": "group", "group_id": int(group_id), "_response_purpose": "ambient"}
                        actor = self.target_actor(bot, target)
                        previous = self.coordinator.policies.get(actor["scope"], (0, {}))[1].get("quiet")
                        current = await self.coordinator.policy(self.runtime._client, actor, refresh=True)
                        if previous and not current.get("quiet"):
                            resume = getattr(self.runtime._event_router, "resume_deferred", None)
                            if resume is not None:
                                resume(actor["scope"])
                        pending = await self.runtime._client.request_feature("GET", "/v1/community/notifications", actor=actor)
                        for notification in pending.get("items", []):
                            context = self.runtime._request_send_context("community", notification["id"], actor["scope"])
                            message_id = await self.runtime._send_qq_message(bot, target,
                                self.runtime.Message(self.runtime.MessageSegment.text(notification["text"])), send_context=context)
                            if message_id:
                                from urllib.parse import quote
                                await self.runtime._client.request_feature("POST", f"/v1/community/notifications/{quote(notification['id'], safe='')}/ack",
                                                                          actor=actor, body={"message_id": message_id})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("community notification polling failed", exc_info=True)
            await asyncio.sleep(5)
