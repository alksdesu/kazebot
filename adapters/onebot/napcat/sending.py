"""文本发送、分条、@ 校验与发送防重。"""
from __future__ import annotations

import logging
import asyncio
import base64
import contextlib
import hashlib
import os
import time
from typing import Any, Awaitable, Callable, Dict, List
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from ..config import ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS
from ..live_config import live
from adapters._shared.send_guard import (
    AmbiguousAckError,
    IdempotencyClaim,
    IdempotencyOwnershipError,
    OutboundSendContext,
    SendContractError,
)
from ..send_contract import (
    classify_send_exception,
    is_at_uid_failure,
    is_temp_file_missing,
    make_idempotency_key,
    protected_claim_send,
    target_identity,
    validate_send_request,
)
from ..emoji_handler import has_emoji_markers

from . import common, emotes, identity, media, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _truncate_qq_text(text: str) -> str:
    """限制单条 QQ 消息长度，避免超过 OneBot 实现的消息上限。"""
    if len(text) <= live.message_limit:
        return text
    suffix = "\n（内容过长，已截断）"
    return text[: live.message_limit - len(suffix)] + suffix


def _message_from_processed_segments(segments: List[Dict[str, Any]]) -> Message:
    """把 emoji_handler 的轻量段描述转换为 OneBot Message。"""
    message_segments: List[MessageSegment] = []
    for segment in segments:
        if segment.get("type") == "text":
            content = str(segment.get("content", ""))
            if content:
                message_segments.append(MessageSegment.text(content))
        elif segment.get("type") == "image":
            url = segment.get("url")
            if url:
                if segment.get("emoji"):
                    # QQ 收藏表情：在普通 image 段基础上补 sub_type=1（表情子类型），
                    # 让 QQ 客户端按小图/贴纸渲染而不是普通大图；summary 提供
                    # "[表情]" 占位，兼容不支持 sub_type 的客户端摘要显示。
                    emoji_seg = MessageSegment.image(url)
                    emoji_seg.data["sub_type"] = 1
                    emoji_seg.data.setdefault("summary", "[表情]")
                    message_segments.append(emoji_seg)
                else:
                    message_segments.append(MessageSegment.image(url))
        elif segment.get("type") == "at":
            qq_id = segment.get("qq")
            if qq_id:
                message_segments.append(MessageSegment.at(qq_id))
        elif segment.get("type") == "reply":
            message_id = segment.get("message_id")
            if message_id:
                message_segments.append(MessageSegment.reply(message_id))
    return Message(message_segments)


def _bind_qq_reply_waiter(conversation_key: str, inbound_seq: int) -> None:
    waiter = state._adapter.qq_waiting_replies.get(conversation_key)
    if waiter is not None and inbound_seq > 0:
        waiter.inbound_seq = inbound_seq
        if inbound_seq in state._adapter.qq_completed_replies:
            waiter.event.set()


def _mark_qq_reply_finished(inbound_seq: int) -> None:
    if inbound_seq <= 0:
        return
    state._adapter.qq_completed_replies[inbound_seq] = None
    state._adapter.qq_completed_replies.move_to_end(inbound_seq)
    while len(state._adapter.qq_completed_replies) > 512:
        state._adapter.qq_completed_replies.popitem(last=False)
    for waiter in state._adapter.qq_waiting_replies.values():
        if waiter.inbound_seq == inbound_seq:
            waiter.event.set()


def _message_dedup_text(message: Any) -> str:
    """生成用于 QQ 发送幂等判断的稳定文本。"""
    try:
        if isinstance(message, Message):
            return str(message)
    except Exception:
        pass
    return str(message or "")


_GROUP_MEMBER_CACHE_TTL = float(os.environ.get("ONEBOT_GROUP_MEMBER_CACHE_TTL", "300") or "300")


async def _load_group_members(
    bot: Bot, group_id: int
) -> tuple[set[str], Dict[str, str], Dict[str, tuple[str, str]]] | None:
    """拉取并缓存群成员。

    返回 ({user_id 集合}, {名片/昵称小写 -> user_id}, {user_id -> (card, nickname)})。
    取不到时返回 None（表示无法校验/反查，由调用方决定不做拦截）。
    """
    try:
        gid = int(group_id)
    except Exception:
        return None
    now = time.time()
    cached = state._adapter.group_member_cache.get(gid)
    if cached and cached[0] > now:
        return cached[1], cached[2], cached[3]
    try:
        data = await bot.call_api("get_group_member_list", group_id=gid)
    except Exception:
        common.logger.debug("get_group_member_list failed for group=%s", gid, exc_info=True)
        return None
    members: set[str] = set()
    name_map: Dict[str, str] = {}
    card_map: Dict[str, tuple[str, str]] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("user_id") is None:
                continue
            uid = str(item.get("user_id"))
            members.add(uid)
            card = str(item.get("card") or "").strip()
            nickname = str(item.get("nickname") or "").strip()
            card_map[uid] = (card, nickname)
            # 名片(card)优先于昵称(nickname)；同名时不覆盖已有映射（保留先遇到的）。
            for key in (card, nickname):
                name = str(key or "").strip()
                if name:
                    name_map.setdefault(name.casefold(), uid)
    if not members:
        return None
    for stale_gid in [g for g, item in state._adapter.group_member_cache.items() if item[0] <= now]:
        state._adapter.group_member_cache.pop(stale_gid, None)
    state._adapter.group_member_cache[gid] = (now + _GROUP_MEMBER_CACHE_TTL, members, name_map, card_map)
    return members, name_map, card_map


async def _group_member_ids(bot: Bot, group_id: int) -> set[str] | None:
    """获取群成员 QQ 号集合（带 TTL 缓存）。取不到时返回 None。"""
    loaded = await _load_group_members(bot, group_id)
    return loaded[0] if loaded else None


def _group_member_display_name(group_id: Any, user_id: Any) -> str:
    """返回 @ 某群友时应展示的名字：本群群名片(card) > 本群 QQ 昵称(nickname)。

    仅使用已缓存的群成员数据（不发网络请求）；缓存未就绪或未命中时返回空串，
    由调用方回退到 profile 显示名 / 匿名别名。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = state._adapter.group_member_cache.get(gid)
    if not cached:
        return ""
    card, nickname = cached[3].get(uid, ("", ""))
    name = card or nickname
    return identity._sanitize_name(name) if name else ""


def _resolve_at_alias_in_group(group_id: Any, token: str) -> str:
    """在已缓存的群成员名片/昵称里反查 token 对应的真实 QQ 号（小写匹配）。

    仅使用缓存（不发网络请求）；缓存未就绪或未命中时返回空串。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = state._adapter.group_member_cache.get(gid)
    if not cached:
        return ""
    return cached[2].get(raw.casefold(), "")


async def _sanitize_group_at_segments(bot: Bot, group_id: Any, message: Any) -> Any:
    """把群消息里"非本群成员/无法解析"的 at 段降级为文本，避免整条消息发送失败。

    2026-07-14 修复：一条消息 @ 多人时，只要其中一个 at 的 uid 被 NapCat 判为
    Get Uid Error，send_group_msg 就会整条失败。这里在发送前用群成员列表校验，
    命中不了的 at 用 @昵称/@别名 文本代替，@全体成员(all) 放行。
    """
    if not isinstance(message, Message):
        return message
    loaded = await _load_group_members(bot, group_id)
    if loaded is None:
        # 拿不到成员列表时不拦截，保持原样（避免误伤把所有 at 变文本）。
        return message
    members, name_map, _card_map = loaded
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
            # 特例：[at:all] 但群里真有成员名片/昵称叫 "all"，优先当成 @ 那个人，
            # 避免把他误伤为 @全体成员。
            if qq.lower() == "all":
                mapped = name_map.get("all")
                if mapped:
                    common.logger.info("resolve [at:all] to group member %s in group=%s", mapped, group_id)
                    new_segments.append(MessageSegment.at(mapped))
                    continue
                # 确实是 @全体成员，放行。
                new_segments.append(seg)
                continue
            if not qq:
                new_segments.append(seg)
                continue
            # 以 members（本群真实 QQ 号集合）为权威，按理想顺序判定：
            #   1) qq 是数字且 ∈ members  -> 就是这个真实 QQ 号，放行。
            #   2) 否则先按 name_map（群名片/昵称，card 优先）反查：命中则改用真实 QQ 号。
            #     这能修复“群名片恰好是纯数字（如名片叫 1）”被误当成 QQ 号的问题。
            #   3) 都不中 -> 丢弃这个非法 at（不再降级成 @匿名代号发到群里）。
            if qq in members:
                new_segments.append(seg)
                continue
            mapped = _resolve_at_alias_in_group(group_id, qq)
            if mapped and mapped in members:
                common.logger.info("resolve at token %r to group member %s in group=%s", qq, mapped, group_id)
                new_segments.append(MessageSegment.at(mapped))
                continue
            # 既不是本群成员 QQ 号，也没有群友名片/昵称叫这个名字：判为无效 at，直接丢弃。
            # Why: 这种情况多半是模型凭空捧造的 at（如 qq=1），降级成 @UserXX 反而产生脗肿文本。
            common.logger.warning("drop invalid at: token=%r not a member/card in group=%s", qq, group_id)
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _strip_at_to_text(message: Any, group_id: Any = None) -> Any:
    """把消息里所有 at 段降级为 @文本，用于发送失败后的兜底重发。

    传入 group_id 时，@文本优先取本群群名片/昵称，其次 profile 显示名。
    拿不到可读名字时直接丢弃这个 at（不再用 _anonymize_user_id 把匿名代号发到群里）。
    """
    if not isinstance(message, Message):
        return message
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            data = getattr(seg, "data", {}) or {}
            qq = str(data.get("qq", "")).strip()
            if qq.lower() == "all":
                new_segments.append(MessageSegment.text("@全体成员"))
            elif qq:
                name = _group_member_display_name(group_id, qq) or identity._qq_profile_display_name(qq)
                if name:
                    new_segments.append(MessageSegment.text(f"@{name}"))
                # 无可读名字：丢弃该 at，避免 @匿名代号泄露到群里。
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _extract_sent_message_id(result: Any) -> str:
    """从 send_*_msg 返回值里提取 message_id（兼容 dict / 标量 两种形式）。"""
    if isinstance(result, dict):
        mid = result.get("message_id")
        if mid is None:
            mid = result.get("data", {}).get("message_id") if isinstance(result.get("data"), dict) else None
        return str(mid).strip() if mid is not None else ""
    if isinstance(result, (int, str)):
        return str(result).strip()
    return ""


def _resend_plan(exc: Exception, sent_message: Any, at_as_text: Any | None) -> tuple[str, Any] | None:
    """决定失败后要不要再物理发一次、发哪条；None = 不重发。

    这一层重发发生在幂等 claim 内部，claim 既看不见也拦不住第二次投递，因此只认
    “确定没发出”的失败；歧义 ack 一律不重发。
    """
    if not classify_send_exception(exc).definitely_not_sent:
        return None
    if at_as_text is not None and is_at_uid_failure(exc) and str(at_as_text) != str(sent_message):
        return "at_as_text", at_as_text
    if is_temp_file_missing(exc):
        return "same_message", sent_message
    return None


async def _send_qq_message_once(
    bot: Bot, target: Dict[str, Any], message: Any, *, idempotency_key: str = "",
) -> str:
    """执行一次 OneBot 发送；失败后最多再物理发一次，且只在确认没发出时才发。"""
    await state._adapter.features.check_send(bot, target)
    sender: Callable[[Any], Awaitable[Any]]
    if target.get("type") == "private":
        user_id = target.get("user_id")
        if user_id is None:
            raise ValueError("private target missing user_id")

        async def send_private(payload: Any) -> Any:
            return await bot.send_private_msg(user_id=int(user_id), message=payload)

        sender, outbound = send_private, message
    elif target.get("type") == "group":
        group_id = target.get("group_id")
        if group_id is None:
            raise ValueError("group target missing group_id")

        async def send_group(payload: Any) -> Any:
            return await bot.send_group_msg(group_id=int(group_id), message=payload)

        # 非本群/无法解析的 at 会让 NapCat 整条失败，发送前先清洗。
        sender, outbound = send_group, await _sanitize_group_at_segments(bot, group_id, message)
    else:
        raise SendContractError(f"unknown QQ target type: {target!r}")
    try:
        message_id = _extract_sent_message_id(await sender(outbound))
        await state._adapter.features.record_outbound(bot, target, outbound, message_id)
        return message_id
    except ActionFailed as exc:
        at_as_text = (
            _strip_at_to_text(outbound, target.get("group_id"))
            if target.get("type") == "group" else None
        )
        plan = _resend_plan(exc, outbound, at_as_text)
        if plan is None:
            raise
        reason, resend_message = plan
        common.logger.warning(
            "onebot_send_resend",
            extra={
                "idempotency_key": idempotency_key,
                "resend_reason": reason,
                "target": target_identity(target),
                "send_error": str(exc),
            },
        )
        message_id = _extract_sent_message_id(await sender(resend_message))
        await state._adapter.features.record_outbound(bot, target, resend_message, message_id)
        return message_id


def _send_audit_fields(
    context: OutboundSendContext,
    *,
    platform_message_id: str,
    attempt: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    return {
        "event_seq": context.event_seq,
        "event_id": context.event_id,
        "task_id": context.task_id,
        "source_inbound_seq": context.source_inbound_seq,
        "conversation_key": context.conversation_key,
        "platform_message_id": platform_message_id,
        "attempt": attempt,
        "idempotency_key": idempotency_key,
    }


def _sent_ttl_for_context(context: OutboundSendContext) -> float | None:
    """Use short retention only for the last-resort context-free content key."""
    if context.idempotency_key or context.event_id:
        return None
    return ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS


async def _heartbeat_idempotency_claim(claim: IdempotencyClaim) -> None:
    """Renew one logical-send owner until its platform operation finishes."""
    interval = max(0.05, min(30.0, state._adapter.outbound_idempotency.lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            await state._adapter.outbound_idempotency.heartbeat(claim)
        except IdempotencyOwnershipError:
            # The protected send commits pending -> sent/ambiguous atomically. A
            # heartbeat already queued at that boundary will then observe that its
            # pending ownership is gone; this is the normal terminal condition.
            # A real takeover is still detected by the protected operation's strict
            # owner-checked commit/release path.
            return


async def _send_qq_message(
    bot: Bot,
    target: Dict[str, Any],
    message: Any,
    *,
    send_context: OutboundSendContext | None = None,
    content_identity: str = "",
    idempotency_key: str = "",
    attempt: int = 1,
    message_factory: Callable[[], Awaitable[Any]] | None = None,
    sticker_delivery_id: str = "",
) -> str:
    """Send with an explicit contract and pending/sent two-phase idempotency."""
    validate_send_request(bot, target)
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )
    identity = content_identity or _message_dedup_text(message).strip()
    key = idempotency_key or make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._adapter.outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._adapter.outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await state._adapter.outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key=key, acquired=False, state=resolved)
    fields = _send_audit_fields(
        context,
        platform_message_id="",
        attempt=attempt,
        idempotency_key=key,
    )
    if not claim.acquired:
        common.logger.info("onebot_send_duplicate", extra={**fields, "claim_state": claim.state})
        platform_message_id = await state._adapter.outbound_idempotency.sent_message_id(key)
        if sticker_delivery_id:
            await emotes._reconcile_sticker_receipts(sticker_delivery_id)
        await state._adapter.features.bind_reminder_message(bot, target, context, platform_message_id)
        return platform_message_id or f"idempotent:{key}"
    common.logger.info("onebot_send_started", extra=fields)
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        async def send_once() -> str:
            payload = await message_factory() if message_factory is not None else message
            result = await _send_qq_message_once(bot, target, payload, idempotency_key=key)
            if sticker_delivery_id and not result:
                raise AmbiguousAckError("平台未返回消息编号，表情包投递状态待确认")
            return result

        platform_message_id = await protected_claim_send(
            state._adapter.outbound_idempotency,
            claim,
            send_once,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=lambda result: str(result or ""),
        )
        common.logger.info(
            "onebot_send_sent",
            extra=_send_audit_fields(
                context, platform_message_id=platform_message_id,
                attempt=attempt, idempotency_key=key,
            ),
        )
        if sticker_delivery_id:
            await emotes._reconcile_sticker_receipts(sticker_delivery_id)
        await state._adapter.features.bind_reminder_message(bot, target, context, platform_message_id)
        return platform_message_id
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_split_text(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """按 [SPLIT] 拆分文本，并把每段回复绑定到本轮来源图片。"""
    # 2026-05-01 修改原因：文本拆分逻辑对群聊和私聊相同，实际发送交给
    # _send_qq_message 处理，确保私聊也能复用表情替换和分段发送能力。
    await state._adapter.features.check_send(bot, target)
    sent_any = False
    parts = text.split(common._SPLIT_SIGNAL) if text else []
    conversation_key = emotes._sticker_conversation_key(target, bot)
    conversation_token = state._adapter.sticker_send_conversation.set(conversation_key)
    selected_token = state._adapter.sticker_selected.set(set())
    try:
        async with emotes._serialize_sticker_delivery(conversation_key):
            sent_any = await _send_split_parts(
                bot, target, parts,
                source_attachments=source_attachments, send_context=send_context,
            )
    finally:
        state._adapter.sticker_selected.reset(selected_token)
        state._adapter.sticker_send_conversation.reset(conversation_token)
    return sent_any


def _freeze_sticker_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    frozen = []
    for segment in segments:
        item = dict(segment)
        if item.get("sticker_sha256"):
            encoded = str(item.pop("url", "")).removeprefix("base64://")
            item["content_sha256"] = hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        frozen.append(item)
    return frozen


async def _materialize_sticker_segments(segments: List[Dict[str, Any]]) -> Message:
    materialized = []
    for segment in segments:
        item = dict(segment)
        digest = str(item.get("sticker_sha256") or "")
        if digest:
            store = emotes._sticker_store_handle()
            row = store.get(digest) if store is not None else None
            raw = await asyncio.to_thread(emotes._read_sticker, row) if row is not None else b""
            if not raw or hashlib.sha256(raw).hexdigest() != item.get("content_sha256"):
                raise SendContractError("已选表情包不可用，保持原投递内容且不自动换图")
            item["url"] = "base64://" + base64.b64encode(raw).decode("ascii")
        materialized.append(item)
    return _message_from_processed_segments(materialized)


async def _send_planned_sticker_message(
    bot: Bot,
    target: Dict[str, Any],
    identity: str,
    builder: Callable[[], Awaitable[Dict[str, Any]]],
    *,
    send_context: OutboundSendContext | None = None,
) -> str:
    identity = f"sticker:v1:{getattr(bot, 'self_id', '')}:{identity}"
    context = send_context.child(identity) if send_context else OutboundSendContext()
    parent_id = (send_context.idempotency_key or send_context.event_id) if send_context else ""
    if parent_id and await state._adapter.outbound_idempotency.has_legacy_message_claims(parent_id, target):
        raise AmbiguousAckError("旧版消息存在投递记录，无法安全恢复选图，需要人工确认")
    key = make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    plan = await state._adapter.outbound_idempotency.get_or_create_message_plan(
        key, target, builder, sent_ttl=_sent_ttl_for_context(context),
    )
    selected = state._adapter.sticker_selected.get()
    if selected is not None:
        selected.update(plan.selected_images)
    if not plan.processed_segments:
        return ""

    async def materialize() -> Message:
        return await _materialize_sticker_segments(plan.processed_segments)

    return await _send_qq_message(
        bot, target, None, send_context=context, content_identity=plan.send_identity,
        idempotency_key=plan.claim_key, message_factory=materialize,
        sticker_delivery_id=plan.delivery_id,
    )


async def _send_split_parts(
    bot: Bot,
    target: Dict[str, Any],
    parts: List[str],
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> bool:
    sent_any = False
    for index, raw_part in enumerate(parts):
        part = _truncate_qq_text(raw_part.strip())
        if not part:
            continue
        async def prepare() -> List[Dict[str, Any]]:
            segments = await state._adapter.emoji.process_emojis(
                part, bot, state._adapter.bqbs, emotes._current_custom_face_names(), emotes._current_custom_face_metadata(),
                strip_asterisk_styles=live.strip_asterisk_styles,
                strip_underscore_styles=live.strip_underscore_styles,
            )
            if segments and live.reply_to_trigger and not sent_any and target.get("reply_message_id"):
                prefix = [{"type": "reply", "message_id": target["reply_message_id"]}]
                if target.get("reply_sender_id"):
                    prefix.extend([
                        {"type": "at", "qq": target["reply_sender_id"]},
                        {"type": "text", "content": " "},
                    ])
                segments = prefix + segments
            return segments

        if has_emoji_markers(part):
            async def build_plan() -> Dict[str, Any]:
                segments = await prepare()
                return {
                    "processed_segments": _freeze_sticker_segments(segments),
                    "send_identity": hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    "selected_images": list(dict.fromkeys(
                        str(item["sticker_sha256"]) for item in segments if item.get("sticker_sha256")
                    )),
                    "conversation_key": state._adapter.sticker_send_conversation.get(""),
                }

            sent_message_id = await _send_planned_sticker_message(
                bot, target, f"text:{index}:{part}", build_plan, send_context=send_context,
            )
            if not sent_message_id:
                continue
        else:
            segments = await prepare()
            if not segments:
                continue
            msg = _message_from_processed_segments(segments)
            segment_identity = f"text:{index}:{_message_dedup_text(msg).strip()}"
            segment_context = send_context.child(segment_identity) if send_context else None
            sent_message_id = await _send_qq_message(
                bot, target, msg, send_context=segment_context, content_identity=segment_identity,
            )
        if sent_message_id and source_attachments:
            media._remember_reply_attachments(
                sent_message_id,
                str(target.get("conversation_key") or ""),
                str(getattr(bot, "self_id", "") or ""),
                source_attachments,
            )
        sent_any = True
        if index < len(parts) - 1:
            await asyncio.sleep(0.5)
    return sent_any
