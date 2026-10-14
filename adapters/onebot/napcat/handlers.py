"""matcher 背后的处理函数与规则。"""
from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Dict, List
from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.rule import Rule
from adapters._shared.approvals import parse_reply_verb
from adapters._shared.echo_policy import EchoEntry, detect_echo, is_echoable_text
from adapters._shared.trigger_policy import TriggerConfig, resolve_llm_intent
from ..echo_rules import QQ_ECHO_RULES
from ..live_config import live, refresh as refresh_live_config
from clonoth_sdk import IntentVerdict

from . import approval, commands, common, emotes, history, identity, inbound, media, message_text, models, outbound_attachments, permissions, proactive, queueing, routing, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


async def _feature_command_rule(bot: Bot, event: Event) -> bool:
    group_id = getattr(event, "group_id", None)
    if not hasattr(event, "get_message"):
        return False
    refresh_live_config()
    if group_id is not None:
        if not permissions._is_group_allowed(int(group_id)):
            return False
    elif not permissions._is_private_allowed(event):
        return False
    metadata = await state._adapter.features.prepare(bot, event)
    text = metadata.get("raw_text") or message_text._message_to_text(event.get_message(), getattr(bot, "self_id", None))
    head = text.strip().replace("／", "/", 1).split(maxsplit=1)
    reply_ref = str(message_text._extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None)) or "")
    return bool(await state._adapter.features.matches_command(bot, event, text, reply_ref) or (head and head[0] in {"/帮助", "/help"}))


_feature_matcher = on_message(rule=Rule(_feature_command_rule), priority=5, block=True)


@_feature_matcher.handle()
async def _handle_feature_command(bot: Bot, event: Event) -> None:
    text = (await message_text._event_text_with_forward(bot, event)).strip()
    result = await state._adapter.features.handle_command(bot, event, text)
    if result is None:
        result = {"text": commands._help_text(event), "attachments": []}
    actor = state._adapter.features.actor(bot, event)
    if getattr(event, "group_id", None) is not None:
        history._record_group_message(inbound._inbound_facts(bot, event, text=text or None))
        target = {"type": "group", "group_id": event.group_id, "conversation_key": actor["scope"], "reply_message_id": event.message_id}
    else:
        target = {"type": "private", "user_id": event.user_id, "conversation_key": actor["scope"]}
    target["_quiet_control"] = bool(result.get("control"))
    target["_feature_actor"] = actor
    context = proactive._request_send_context("feature-command", actor["message_id"], actor["scope"])
    if result.get("text"):
        await sending._send_qq_message(bot, target, Message(MessageSegment.text(result["text"])), send_context=context)
        if target["type"] == "group":
            history._record_bot_reply(target["group_id"], result["text"])
    if result.get("attachments"):
        await outbound_attachments._send_attachments_confirmed(bot, target, result["attachments"], send_context=context)
    await _feature_matcher.finish()


_community_notice_matcher = on_notice(priority=90, block=False)


@_community_notice_matcher.handle()
async def _handle_community_notice(bot: Bot, event: Event) -> None:
    if getattr(event, "notice_type", "") != "group_increase" or not state._adapter.features.available:
        return
    group_id = int(getattr(event, "group_id", 0) or 0)
    if not permissions._is_group_allowed(group_id):
        return
    actor = state._adapter.features.target_actor(bot, {"type": "group", "group_id": group_id})
    await state._adapter.client.request_feature("POST", "/v1/community/notices", actor=actor, body={
        "notice_type": "group_increase", "member_id": str(getattr(event, "user_id", "")),
        "timestamp": getattr(event, "time", time.time()), "display_name": identity._event_display_name(event),
    })


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(permissions._allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    await _record_non_trigger_message(bot, event)


async def _record_non_trigger_message(bot: Bot, event: GroupMessageEvent) -> None:
    """把一条不回复的群消息写进上下文缓存。

    意愿判断那条路 block 住了这个 matcher（判定结果要等一次模型往返，不能让整条链
    干等），判成不接话时得由它自己补记，否则这些消息会从群历史里整段消失。
    """
    message_text._remember_message_for_reply_context(event)
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        expanded_text = await message_text._event_text_with_forward(bot, event)
        # QQ 客户端经常把“问图文本 + 图片”拆成两条事件；第二条纯图片
        # 通常不会 @bot，因此不会进入 agent matcher。这里在低优先级历史
        # matcher 中把非触发附件也下载并写入近期缓存，让后续自然语言转发
        # 和图片提问能选到对应内容。
        real_conversation_key = f"qq_group:{int(event.group_id)}"
        stable_conversation_key = routing._stable_conversation_key(real_conversation_key)
        # 不回复的消息只扫顶层。下钻是为了让模型看见卡片里的图，而这条根本不会进模型；
        # 真被人引用时 _build_reply_context 会再下钻一次，那时候才值得付这几次下载。
        attachments, _errors = await media._collect_qq_attachments(
            bot, event, stable_conversation_key, expand_forward=False,
        )
        media._remember_recent_images(stable_conversation_key, event, attachments)
        await state._adapter.features.register_attachments(bot, event, attachments)
        facts = inbound._inbound_facts(bot, event, text=expanded_text or None, attachments=attachments)
        emotes._collect_stickers(facts)
        history._record_group_message(facts)
        metadata = await state._adapter.features.prepare(bot, event)
        if state._adapter.features.blocked(metadata) or metadata.get("policy", {}).get("settings", {}).get("response_policy_enabled"):
            return
        await _maybe_echo_group_message(bot, event, expanded_text)
        await emotes._maybe_join_sticker_combat(
            bot, event,
            has_image=any(item.get("type") == "image" for item in attachments),
            text=expanded_text,
        )


def _echo_key_for_message(message: Any, text: str) -> str:
    """这条消息用于复读比对的内容指纹。跟不了就返回空串。

    表情包不能用正文比：三个人各发一张不同的图，_message_to_text 都会渲染成
    「[表情包]」，光比文本会把它们判成复读。所以图片一律拿 file/url 当指纹。
    """
    segments = list(message) if message is not None else []
    kinds = set()
    image_id = ""
    face_ids: List[str] = []
    for segment in segments:
        seg_type, data = message_text._segment_type_and_data(segment)
        if seg_type == "text":
            if str(data.get("text") or "").strip():
                kinds.add("text")
            continue
        if seg_type in message_text.IMAGE_SEGMENT_TYPES:
            kinds.add("image")
            # emoji_id 是商城表情的稳定标识，url 每次取都可能带不同的鉴权参数。
            image_id = str(
                data.get("emoji_id") or data.get("file") or media._segment_image_url(data) or ""
            ).strip()
            continue
        if seg_type == "face":
            # 系统表情有稳定 id，跟文字一样能精确比对，复读它也不指向任何人。群里复读
            # 的句子十句有九句夹着表情，把它算成不可跟的类型，这个功能就等于没开。
            kinds.add("text")
            face_ids.append(str(data.get("id") or "").strip())
            continue
        # 语音、视频、卡片、@ 之类一律不跟。
        kinds.add("other")

    if kinds == {"image"} and image_id:
        return f"img:{image_id}"
    if kinds == {"text"}:
        # id 单独进指纹：正文里的「[QQ表情:捂脸]」是查表得来的显示名，两个 id 撞名就会
        # 把不同的表情比成同一条。
        return f"txt:{text.strip()}" + (f"|face:{','.join(face_ids)}" if face_ids else "")
    return ""


async def _maybe_echo_group_message(bot: Bot, event: GroupMessageEvent, text: str) -> None:
    """连着 N 个不同的人刷同一句，Bot 也跟一条。全程不进模型。"""
    if not live.enable_echo:
        return
    group_id = int(event.group_id)
    try:
        message = event.get_message()
    except Exception:
        return
    key = _echo_key_for_message(message, text)
    if not key:
        return
    # 校验正文本身而不是 key[4:]：指纹尾部还挂着表情 id，会把长度算多。
    if key.startswith("txt:") and not is_echoable_text(text, max_length=live.echo_max_length, rules=QQ_ECHO_RULES):
        return

    now = time.time()
    state._adapter.echo_recent[group_id].append(EchoEntry(key, str(event.user_id), now))
    last_key, last_at = state._adapter.echo_last.get(group_id, ("", 0.0))
    if now - last_at < live.echo_cooldown_sec:
        return
    hit = detect_echo(
        state._adapter.echo_recent[group_id],
        threshold=live.echo_threshold,
        now=now,
        max_age_seconds=live.echo_window_sec,
        already_echoed=last_key,
    )
    if not hit:
        return

    # 命中的必然是刚进来这条：detect_echo 要求尾部 N 条指纹全同，而这条就在尾部。
    # 所以文本直接原样回发 —— 拿指纹反解会把「[QQ表情:捂脸]」当字面量发出去。
    outgoing = Message(MessageSegment.image(hit[4:])) if hit.startswith("img:") else message
    try:
        await sending._send_qq_message(bot, {"type": "group", "group_id": group_id, "_response_purpose": "ambient"}, outgoing)
    except Exception:
        # 跟读失败无所谓，不值得惊动用户，更不该把异常抛回 matcher 链。
        common.logger.debug("echo send failed for group %s", group_id, exc_info=True)
        return
    state._adapter.echo_last[group_id] = (hit, now)


# 群文件上传通知记录器。部分 OneBot 实现把普通文件作为 notice 上报，而不是 message file 段。
_group_upload_matcher = on_notice(priority=99, block=False)


@_group_upload_matcher.handle()
async def _handle_group_upload_notice(bot: Bot, event: Event) -> None:
    if not isinstance(event, GroupUploadNoticeEvent):
        return
    # notice matcher 没有 rule，这里就是最早入口。
    refresh_live_config()
    if not permissions._is_group_allowed(int(event.group_id)):
        return
    message_text._remember_message_for_reply_context(event)
    real_conversation_key = f"qq_group:{int(event.group_id)}"
    stable_conversation_key = routing._stable_conversation_key(real_conversation_key)
    file_info = getattr(event, "file", None)
    if not isinstance(file_info, dict):
        file_info = {}
    source = str(file_info.get("url") or file_info.get("path") or file_info.get("file") or "").strip()
    name = media._safe_attachment_name(str(file_info.get("name") or file_info.get("file_name") or file_info.get("filename") or "文件"), "file")
    size = file_info.get("size") or file_info.get("file_size") or file_info.get("filesize") or 0
    attachments, _errors = await media._file_sources_to_attachments([
        {"source": source, "name": name, "size": size},
    ], stable_conversation_key)
    display_text = message_text._segment_to_text("file", {"name": name}, set()) or ""
    sender_id = str(getattr(event, "user_id", "") or "")
    # GroupUploadNoticeEvent 没有 sender/anonymous 属性，helper 会退回 profile / 匿名别名。
    line = history._compose_history_line(
        timestamp=getattr(event, "time", None),
        sender=getattr(event, "sender", None),
        sender_id=sender_id,
        text=display_text,
        name_override=identity._event_display_name(event),
        alias_override=identity._event_user_alias(event),
    )
    history._append_group_record(
        int(event.group_id),
        line,
        text=display_text,
        sender_name=identity._event_display_name(event),
        sender_id=sender_id,
        timestamp=float(getattr(event, "time", None) or time.time()),
        message_id=str(getattr(event, "message_id", "") or ""),
        attachments=attachments,
    )


# Agent 入口 matcher。默认只处理 @Bot；也可通过 ONEBOT_GROUP_TRIGGER=prefix/all 切换触发策略。
_agent_matcher = on_message(rule=Rule(permissions._agent_group_rule), priority=10, block=True)


@_agent_matcher.handle()
async def _handle_agent(bot: Bot, event: GroupMessageEvent) -> None:
    """把当前 QQ 群请求提交给 ClonothZX。"""
    await _process_group_message(bot, event, _agent_matcher)


async def _finish_local_command(
    matcher: Any,
    bot: Bot,
    event: GroupMessageEvent,
    *,
    user_text: str,
    attachments: List[Dict[str, Any]],
    reply: str,
) -> None:
    """回掉一条本地命令，并把命令行与回复一起写进群历史（末尾总会抛出 FinishedException）。

    本地命令不经 engine，也已经被 agent matcher block 掉了 priority=99 的历史 matcher：
    不在这里记，整段交互在群历史里就消失，后面的对话看不到「刚才有人改过模型」。
    """
    await _check_local_group_send(bot, event)
    history._record_group_message(inbound._inbound_facts(bot, event, text=user_text or None, attachments=attachments))
    history._record_bot_reply(int(event.group_id), reply)
    await matcher.finish(reply)


async def _check_local_group_send(bot: Bot, event: GroupMessageEvent) -> None:
    metadata = await state._adapter.features.prepare(bot, event)
    await state._adapter.features.check_send(bot, {"type": "group", "group_id": event.group_id,
                                    "_response_purpose": "direct" if metadata.get("direct", True) else "ambient"})


def _coordinated_response_action(verdict: IntentVerdict, metadata: Dict[str, Any]) -> str:
    if not verdict.decided:
        return "task" if metadata.get("direct") else "ignore"
    action = getattr(verdict, "action", "reply") if verdict.agreed else "ignore"
    if action == "ignore" and metadata.get("direct"):
        return "task"
    if action == "react" and (not live.enable_reactions or getattr(verdict, "reaction_id", "") not in common._REACT_MODEL_EMOJIS):
        return "short_reply"
    return action


async def _process_group_message(bot: Bot, event: GroupMessageEvent, matcher: Any) -> None:
    """一条已判定要回的群消息的完整处理。

    matcher 由调用方传进来：显式信号和意愿判断走两个不同优先级的 matcher，
    但从本地命令到 inbound 提交的整条链路必须是同一份，否则两条路会各自长歪。
    """
    state._adapter.last_bot = bot
    # on_bot_connect 已经认过一次，这里是兜底：漏认一次就是记忆分叉，代价不对等。
    routing._adopt_bot_scope(bot)

    if state._adapter.client is None or state._adapter.session_state is None:
        await _check_local_group_send(bot, event)
        await matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    message_text._remember_message_for_reply_context(event)
    group_id = int(event.group_id)
    real_conversation_key = f"qq_group:{group_id}"
    stable_conversation_key = routing._stable_conversation_key(real_conversation_key)
    raw_user_text = (await message_text._event_text_with_forward(bot, event)).strip() or "你好"
    # 前缀判定必须在剥前缀之前做，否则 all 模式下所有人都会被当成主动找 bot。
    direct_interaction = inbound._is_direct_bot_interaction(event, bot, raw_user_text)
    user_text = inbound._strip_trigger_prefix(raw_user_text)
    metadata = await state._adapter.features.prepare(bot, event)
    if metadata and state._adapter.features.coordinator.can_continue(metadata["actor"], metadata.get("reply_ref", "")):
        pending = state._adapter.features.coordinator.bursts[metadata["actor"]["scope"]].item
        metadata["direct"] = metadata.get("direct", False) or pending.platform_updates.get("_response_purpose") == "direct"
        await state._adapter.features.decision(bot, event, pending.platform_updates.get("_route_hints", {}).get("response_action", "task"), "同一用户正在补充当前请求")
    if metadata.get("policy", {}).get("settings", {}).get("response_policy_enabled") and not metadata.get("classified") and not user_text.startswith(("/", "／")):
        verdict = await inbound._judge_llm_intent(bot, event, user_text)
        action = _coordinated_response_action(verdict, metadata)
        if action != "ignore" and not state._adapter.features.coordinator.allow_response(metadata["actor"]["scope"], metadata["policy"]["settings"], direct=metadata.get("direct", False)):
            action = "ignore"
        await state._adapter.features.decision(bot, event, action, verdict.reason)
        if action == "react" and getattr(verdict, "reaction_id", "") in common._REACT_MODEL_EMOJIS:
            if await outbound_attachments._set_message_react(bot, event, verdict.reaction_id, True):
                history._record_group_message(inbound._inbound_facts(bot, event, text=user_text or None))
                await matcher.finish()
            await state._adapter.features.decision(bot, event, "short_reply", "表态未送达，改为短答")
        if action == "ignore":
            await _record_non_trigger_message(bot, event)
            await matcher.finish()
    if not identity._anonymous_identity(event):
        asyncio.create_task(inbound._auto_like_user(bot, int(event.user_id)))

    attachments, attachment_errors = await media._collect_qq_attachments(bot, event, stable_conversation_key)
    media._remember_recent_images(stable_conversation_key, event, attachments)
    await state._adapter.features.register_attachments(bot, event, attachments)
    help_reply = await commands._maybe_handle_help_command(event=event, user_text=user_text)
    if help_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=help_reply)
    private_only_reply = await commands._maybe_handle_private_only_in_group(event=event, user_text=user_text)
    if private_only_reply is not None:
        await _finish_local_command(
            matcher, bot, event, user_text=user_text, attachments=attachments, reply=private_only_reply,
        )
    clear_mem_reply = await commands._maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=clear_mem_reply)
    dream_reply = await commands._maybe_handle_dream_command(
        event=event, user_text=user_text, conversation_key=stable_conversation_key,
    )
    if dream_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=dream_reply)
    model_reply = await commands._maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=model_reply)
    drawtools_reply = await commands._maybe_handle_drawtools_command(event=event, user_text=user_text)
    if drawtools_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=drawtools_reply)
    custom_face_reply = await commands._maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=custom_face_reply)
    sticker_reply = await commands._maybe_handle_sticker_command(event=event, user_text=user_text)
    if sticker_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=sticker_reply)
    proactive_reply = await proactive._maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=proactive_reply)
    draw_direct_prompt = inbound._parse_direct_draw_command(user_text)
    if draw_direct_prompt is None:
        # /生图 不吃历史图：混进来的图会把这一轮拽到视觉节点。
        await media._merge_recent_attachments_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    facts = inbound._inbound_facts(bot, event, text=user_text or None, attachments=attachments)
    current_seq = history._record_group_message(facts)
    entry_node_id = common.DRAW_NODE_ID if draw_direct_prompt is not None else ""
    if draw_direct_prompt is not None:
        if attachments:
            attachments.clear()
            await _check_local_group_send(bot, event)
            await matcher.send(inbound._DRAW_REFERENCE_IMAGE_NOTICE)
        # 直达绘图不带群历史，水位保持不动，那些行留给下一条正常消息带出去。
        inbound_text, history_watermark = await inbound._build_draw_direct_inbound_text(event, draw_direct_prompt, False), -1
    else:
        inbound_text, history_watermark = await inbound._build_inbound_text(
            event, bot, user_text, stable_conversation_key, attachments, exclude_seq=current_seq,
        )
        inbound_text = inbound._apply_attachment_hints(inbound_text, user_text, attachments, attachment_errors)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "group",
        "group_id": group_id,
        # 群会话也登记触发者：qq_forward 的“私发给我”与权限判定都要靠它反查发起人。
        # 消费端一律先判 type 再读 user_id，因此不会被误认成私聊目标。
        "user_id": event.user_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    ok = False
    try:
        ok = await queueing._enqueue_or_submit_inbound(models.QueuedInbound(
            matcher=matcher,
            bot=bot,
            event=event,
            channel="qq_group",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=False,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
            history_watermark=history_watermark,
            direct_interaction=direct_interaction,
            facts=facts,
        ))
    except Exception as exc:
        common.logger.exception("submit inbound failed")
        await _check_local_group_send(bot, event)
        await matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _check_local_group_send(bot, event)
        await matcher.finish("Clonoth Agent 未接受本次请求。")
    await matcher.finish()


# 接话意愿 matcher。优先级排在显式信号之后、历史记录之前：显式信号不用问模型，
# 而判定期间必须把历史记录挡住，等判成不接话再由 handler 自己补记。
_intent_matcher = on_message(rule=Rule(permissions._intent_group_rule), priority=20, block=True)


@_intent_matcher.handle()
async def _handle_intent(bot: Bot, event: GroupMessageEvent) -> None:
    """问一次模型再决定这条群消息要不要接。"""
    text = await permissions._group_trigger_text(bot, event)
    verdict = await inbound._judge_llm_intent(bot, event, text)
    metadata = await state._adapter.features.prepare(bot, event)
    settings = metadata.get("policy", {}).get("settings", {})
    if settings.get("response_policy_enabled"):
        action = _coordinated_response_action(verdict, metadata)
        if action != "ignore" and not state._adapter.features.coordinator.allow_response(metadata["actor"]["scope"], settings, direct=metadata.get("direct", False)):
            action = "ignore"
        await state._adapter.features.decision(bot, event, action, verdict.reason)
        if action == "react":
            if await outbound_attachments._set_message_react(bot, event, verdict.reaction_id, True):
                await _record_non_trigger_message(bot, event)
                return
            await state._adapter.features.decision(bot, event, "short_reply", "表态未送达，改为短答")
        if action == "ignore":
            await _record_non_trigger_message(bot, event)
            return
    inp = inbound._trigger_input(event, bot, text)
    decision = resolve_llm_intent(
        inp, TriggerConfig.from_live(live), state._adapter.trigger_cooldown,
        agreed=verdict.agreed, reason=verdict.reason,
    )
    if not decision.triggered:
        common.logger.debug(
            "QQ intent declined group=%s decided=%s error=%s blocked_by=%s reason=%s",
            getattr(event, "group_id", None), verdict.decided, verdict.error,
            decision.blocked_by, decision.reason,
        )
        await _record_non_trigger_message(bot, event)
        return
    state._adapter.trigger_cooldown.record(inp)
    common.logger.info(
        "QQ intent accepted group=%s reason=%s",
        getattr(event, "group_id", None), decision.reason,
    )
    await _process_group_message(bot, event, _intent_matcher)


# 私聊入口 matcher。私聊不需要 @Bot，直接阻断后续 matcher，避免重复响应。
_private_matcher = on_message(rule=Rule(permissions._private_message_rule), priority=10, block=True)


async def _finish_approval_decision(user_id: int, approval_id: str, decision: str) -> None:
    """提交审批决策并结束当前私聊处理（末尾总会抛出 FinishedException）。

    同时服务于“引用回复审批”和“手输审批命令”两个入口，避免重复代码。
    """
    book = state._adapter.approvals
    # 先摘走再提交：多个管理员同时决策时只有抢到的人能提交。
    info = await book.claim(approval_id, user_id, decision)
    if info is None:
        await _private_matcher.finish(approval._settled_approval_notice(approval_id))
    try:
        ok = await state._adapter.client.approve(
            approval_id,
            decision=decision,
            comment=f"QQ admin {user_id} {decision}ed via private message.",
        )
    except Exception as exc:
        await book.release(approval_id, info)
        common.logger.exception("submit QQ approval decision failed")
        await _private_matcher.finish(f"提交审批失败：{exc}")
    if not ok:
        await book.release(approval_id, info)
        await _private_matcher.finish("审批提交被 Supervisor 拒绝，请检查日志。")
    common.logger.info(
        "approval decided on QQ: id=%s decision=%s admin=%s", approval_id, decision, user_id,
    )
    await _private_matcher.finish(
        f"已{('同意' if decision == 'allow' else '拒绝')}审批：{approval_id}\n"
        f"操作：{info.get('operation') or 'unknown'}"
    )


@_private_matcher.handle()
async def _handle_private_agent(bot: Bot, event: PrivateMessageEvent) -> None:
    """把当前 QQ 私聊请求提交给 ClonothZX。"""
    state._adapter.last_bot = bot
    # on_bot_connect 已经认过一次，这里是兜底：漏认一次就是记忆分叉，代价不对等。
    routing._adopt_bot_scope(bot)

    if not permissions._is_private_allowed(event):
        # 未放行的人连「这个号是不是 bot」都不该确认；要提示就自己填 channels.private_denied_reply。
        common.logger.info("QQ private message ignored: user=%s not allowed", getattr(event, "user_id", None))
        notice = live.private_denied_reply
        if notice:
            await _private_matcher.finish(notice)
        await _private_matcher.finish()

    if state._adapter.client is None or state._adapter.session_state is None:
        await _private_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    message_text._remember_message_for_reply_context(event)
    user_id = int(event.user_id)
    user_text = (await message_text._event_text_with_forward(bot, event)).strip() or "你好"

    # 裸发“同意”不是审批意图：必须引用 bot 发出的审批卡片才认。
    reply_verb = parse_reply_verb(user_text)
    if reply_verb is not None:
        target = await approval._resolve_approval_reply_target(bot, event)
        if target.matched():
            if not permissions._can("approval", event):
                denial = permissions._capability_denial(
                    "approval", event, "你不是 Clonoth 审批管理员，不能处理审批请求。",
                )
                if denial is not None:
                    await _private_matcher.finish(denial)
                # denial is None：档位关掉且不是名单，当作没有这个命令，往下走普通聊天。
            else:
                if target.settled_id:
                    await _private_matcher.finish(approval._settled_approval_notice(target.settled_id))
                if target.approval_id:
                    await _finish_approval_decision(user_id, target.approval_id, reply_verb)
                await _private_matcher.finish(approval._APPROVAL_REPLY_UNRESOLVED_HINT)

    approval_command = approval._parse_approval_command(user_text)
    if approval_command is not None:
        if not permissions._can("approval", event):
            denial = permissions._capability_denial(
                "approval", event, "你不是 Clonoth 审批管理员，不能处理审批请求。",
            )
            if denial is not None:
                await _private_matcher.finish(denial)
            # denial is None：当作没有这个命令，往下走普通聊天。
        else:
            decision, approval_token = approval_command
            approval_id, error = approval._resolve_pending_approval_id(approval_token)
            if not approval_id:
                await _private_matcher.finish(error)
            await _finish_approval_decision(user_id, approval_id, decision)

    # 裸动词只有在本人确实有待审批时才拦截；没有待审批仍是普通聊天。
    # 关闭唯一匹配开关时也要给出明确提示，不能静默把「同意」送进普通 Agent。
    bare_text = common._strip_command_prefix(user_text) or user_text
    bare_verb = parse_reply_verb(bare_text)
    if bare_verb is not None and permissions._can("approval", event):
        pending_count = state._adapter.approvals.pending_count_for(user_id)
        if live.approval_bare_verb_unique:
            unique_id = state._adapter.approvals.unique_pending_for(user_id)
            if unique_id:
                await _finish_approval_decision(user_id, unique_id, bare_verb)
        if pending_count > 0:
            await _private_matcher.finish(
                "这条私聊有待审批，但无法安全确定目标。请引用审批卡片回复，"
                "或发送：/审批 同意 <ID>。"
            )

    # 命令顺序与群侧一致：清记忆 / 死信 / 切模型在私聊里只有名单能用，而名单恒过闸门。
    help_reply = await commands._maybe_handle_help_command(event=event, user_text=user_text)
    if help_reply is not None:
        await _private_matcher.finish(help_reply)
    clear_mem_reply = await commands._maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _private_matcher.finish(clear_mem_reply)
    dead_letter_reply = await commands._maybe_handle_dead_letter_command(event=event, user_text=user_text)
    if dead_letter_reply is not None:
        await _private_matcher.finish(dead_letter_reply)
    model_reply = await commands._maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _private_matcher.finish(model_reply)

    real_conversation_key = f"qq_private:{user_id}"
    stable_conversation_key = routing._stable_conversation_key(real_conversation_key)
    attachments, attachment_errors = await media._collect_qq_attachments(bot, event, stable_conversation_key)
    media._remember_recent_images(stable_conversation_key, event, attachments)
    await state._adapter.features.register_attachments(bot, event, attachments)
    dream_reply = await commands._maybe_handle_dream_command(
        event=event, user_text=user_text, conversation_key=stable_conversation_key,
    )
    if dream_reply is not None:
        await _private_matcher.finish(dream_reply)
    drawtools_reply = await commands._maybe_handle_drawtools_command(event=event, user_text=user_text)
    if drawtools_reply is not None:
        await _private_matcher.finish(drawtools_reply)
    custom_face_reply = await commands._maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _private_matcher.finish(custom_face_reply)
    sticker_reply = await commands._maybe_handle_sticker_command(event=event, user_text=user_text)
    if sticker_reply is not None:
        await _private_matcher.finish(sticker_reply)
    proactive_reply = await proactive._maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _private_matcher.finish(proactive_reply)
    draw_direct_prompt = inbound._parse_direct_draw_command(user_text)
    if draw_direct_prompt is None:
        # /生图 不吃历史图：混进来的图会把这一轮拽到视觉节点。
        await media._merge_recent_attachments_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    entry_node_id = common.DRAW_NODE_ID if draw_direct_prompt is not None else ""
    if draw_direct_prompt is not None:
        if attachments:
            attachments.clear()
            await _private_matcher.send(inbound._DRAW_REFERENCE_IMAGE_NOTICE)
        inbound_text = await inbound._build_draw_direct_inbound_text(event, draw_direct_prompt, True)
    else:
        inbound_text = await inbound._build_private_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
        inbound_text = inbound._apply_attachment_hints(inbound_text, user_text, attachments, attachment_errors)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "private",
        "user_id": user_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    ok = False
    try:
        ok = await queueing._enqueue_or_submit_inbound(models.QueuedInbound(
            matcher=_private_matcher,
            bot=bot,
            event=event,
            channel="qq_private",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=True,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
            facts=inbound._inbound_facts(bot, event, text=user_text or None, attachments=attachments),
        ))
        try:
            await bot.call_api("set_input_status", user_id=int(event.user_id), event_type=1)
        except Exception:
            pass
    except Exception as exc:
        common.logger.exception("submit private inbound failed")
        await _private_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _private_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _private_matcher.finish()
