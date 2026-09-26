"""群历史窗口、水位、清空门槛与会话侧状态清理。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Deque, Dict, List
from nonebot.adapters.onebot.v11 import Bot, Event
from platforms.inbound import InboundFacts
from ..config import CLONOTH_WORKSPACE
from ..live_config import live
from ..emoji_handler import strip_output_markers

from . import anonymize, common, identity, inbound, media, message_text, models, outbound_attachments, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _append_group_history(group_id: int, line: str) -> int:
    """把一行写入群历史缓存并返回它的序号。所有写入点都必须走这里。"""
    gid = int(group_id)
    seq = state._adapter.group_history_seq[gid] + 1
    state._adapter.group_history_seq[gid] = seq
    lines = state._adapter.group_history[gid]
    _evict_delivered_history(gid, lines)
    if lines.maxlen and len(lines) == lines.maxlen:
        # group_history_max 允许配 0（关掉群历史），那不是缺口，所以先看 maxlen。
        _note_history_gap(gid, lines[0].seq)
    lines.append(models.GroupHistoryLine(seq=seq, text=line))
    return seq


def _evict_delivered_history(group_id: int, lines: Deque["GroupHistoryLine"]) -> None:
    """软上限之上的空间只留给还没送到 engine 的行，已送达的行到点就让位。"""
    soft_cap = max(int(live.group_history_max), 0)
    watermark = _group_history_watermark(group_id)
    while lines and len(lines) >= soft_cap and lines[0].seq <= watermark:
        lines.popleft()


def _note_history_gap(group_id: int, dropped_seq: int) -> None:
    """记下一条因为缓存满而被丢掉的行；只有还没送到 engine 的行才算缺口。"""
    gid = int(group_id)
    if int(dropped_seq) <= _group_history_watermark(gid):
        return
    if gid not in state._adapter.group_history_gap:
        common.logger.warning("QQ group history overflow: group=%s dropped undelivered seq=%s", gid, dropped_seq)
    state._adapter.group_history_gap[gid] = max(state._adapter.group_history_gap.get(gid, 0), int(dropped_seq))


def _group_history_lost(group_id: int, watermark: int) -> int:
    """缓存上限吃掉了几条还没送出去的行。

    seq 从 1 开始逐 1 递增，缺的是 (watermark, gap] 这一段；水位 -1（什么都没送出去）
    要按 0 算，否则第一行会被多数一条。
    """
    return max(0, state._adapter.group_history_gap.get(int(group_id), 0) - max(int(watermark), 0))


def _group_history_watermark(group_id: int) -> int:
    """engine 侧已确认收到的最大群历史序号；-1 表示这一轮要带完整历史。"""
    if state._adapter.session_state is None:
        return -1
    return state._adapter.session_state.get_high_watermark(int(group_id))


def _reset_group_history_watermark(group_id: int) -> None:
    """重置水位，让下一轮重新带完整历史。

    会话键对 SDK 不透明，SDK 不碰水位；上下文重置时只有这里知道对应哪个群。
    """
    if state._adapter.session_state is not None:
        state._adapter.session_state.reset_channel_watermark(int(group_id))
    # 缺口是按旧水位算出来的差值，水位一被重定义它就不再成立。
    state._adapter.group_history_gap.pop(int(group_id), None)


def _note_inbound_seq(seq: Any) -> None:
    """记下见过的最大 inbound 序号。清空上下文时拿它划界。"""
    value = int(seq or 0)
    if value > state._adapter.last_inbound_seq:
        state._adapter.last_inbound_seq = value


def _raise_context_clear_barrier(group_id: int, cleaned_triggers: Any = None) -> None:
    """把清空时刻记成一道门槛，序号不高于它的投递都算上一段上下文的产物。"""
    seqs = [
        int(getattr(trigger, "inbound_seq", 0) or 0)
        for trigger in (cleaned_triggers or [])
    ]
    state._adapter.context_clear_barrier[int(group_id)] = max([state._adapter.last_inbound_seq, *seqs])


def _is_stale_delivery(group_id: Any, send_context: Any) -> bool:
    """这次投递是不是清空之前那段上下文的产物。"""
    if group_id is None:
        return False
    barrier = state._adapter.context_clear_barrier.get(int(group_id), 0)
    if not barrier:
        return False
    seq = int(getattr(send_context, "source_inbound_seq", 0) or 0)
    # 取不到来源序号就放行：宁可多留一条，也不能把正常回复从群历史里抹掉。
    return bool(seq) and seq <= barrier


def _purge_conversation_side_state(conversation_key: str, target: Dict[str, Any]) -> None:
    """清掉一个会话散落在 bot 进程与附件目录里的上下文副本，让它真正从零重新积累。

    只在 clear 语义下调用 —— 漏掉任何一份，重置后它都会把旧上下文重新喂回模型。
    """
    state._adapter.recent_images.pop(conversation_key, None)
    state._adapter.recent_files.pop(conversation_key, None)
    state._adapter.sticky_subjects.pop(conversation_key, None)
    bucket = outbound_attachments._sent_attachment_bucket_key(target) or f"conv:{conversation_key}"
    state._adapter.recent_sent_attachments.pop(bucket, None)
    state._adapter.sent_attachment_seq.pop(bucket, None)
    state._adapter.features.coordinator.clear_context(conversation_key)
    state._adapter.features.burst_store.clear_scope(conversation_key)
    group_id = target.get("group_id")
    if target.get("type") == "group" and group_id is not None:
        state._adapter.group_history.pop(int(group_id), None)
        # qq_forward 的转发候选是群历史的第二份副本，只清前者等于没清。
        state._adapter.group_content_records.pop(int(group_id), None)
        # 清了会话还让它继续静默没有道理：运营者刚说「重新开始」，下一句就该有人应。
        state._adapter.trigger_cooldown.forget_group(group_id)
    _remove_conversation_attachment_dir(conversation_key)


def _remove_conversation_attachment_dir(conversation_key: str) -> None:
    """删掉这个会话的入站附件目录。历史一清，里面的路径就再没有东西引用得到。"""
    name = str(conversation_key or "").replace(":", "_")
    # 前缀校验挡住异常 key：conversation_key 为空时拼出的会是 data/attachments 本身。
    if not name.startswith(models._QQ_ATTACHMENT_DIR_PREFIXES):
        return
    att_dir = Path(CLONOTH_WORKSPACE) / "data" / "attachments" / name
    if not att_dir.is_dir():
        return
    import shutil as _shutil
    try:
        _shutil.rmtree(att_dir)
    except OSError:
        common.logger.warning("remove conversation attachment dir failed: %s", att_dir, exc_info=True)


def _compose_history_line(
    *,
    timestamp: Any,
    sender: Any,
    sender_id: Any,
    text: str,
    name_override: str = "",
    alias_override: str = "",
) -> str:
    """群历史行的唯一拼装点：时间、显示名、匿名 ID、正文的匿名化与压缩都在这里做。

    匿名消息的显示名/别名由调用方预解析后传入，避免占位号被当成一个真实用户。
    """
    safe_text = anonymize._anonymize_text_for_ai(message_text._compact_text(text))
    name = anonymize._anonymize_text_for_ai(name_override or identity._sender_display_name(sender, sender_id))
    alias = alias_override or anonymize._anonymize_user_id(sender_id)
    return f"[{message_text._format_hhmm(timestamp)}] {name}({alias}): {safe_text}"


def _history_line(facts: InboundFacts) -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行，并匿名化 QQ ID。"""
    return _compose_history_line(
        timestamp=facts.sent_at,
        sender=None,
        sender_id=facts.sender.platform_id,
        text=facts.text,
        name_override=facts.sender.display_name,
        alias_override=facts.sender.alias,
    )


def _append_group_record(
    group_id: int,
    line: str,
    *,
    text: str,
    sender_name: str,
    sender_id: str,
    timestamp: float,
    message_id: str = "",
    attachments: List[Dict[str, Any]] | None = None,
) -> int:
    """一条群历史同时进水位队列和结构化副本；两边缺一个，转发筛选就会看不到这条。

    返回这一行在群历史里的序号。
    """
    gid = int(group_id)
    seq = _append_group_history(gid, line)
    state._adapter.group_content_records[gid].append(models.GroupContentRecord(
        formatted_line=line,
        text=text,
        sender_name=sender_name,
        sender_id=sender_id,
        timestamp=timestamp,
        message_id=message_id,
        seq=seq,
        attachments=[dict(att) for att in (attachments or []) if isinstance(att, dict)],
    ))
    return seq


def _record_group_message(facts: InboundFacts) -> int:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。

    返回这条消息在群历史里的序号，0 = 正文为空没有记。
    """
    if facts.text.strip():
        # 转发 node 的 user_id 要真实号；匿名占位号发不出去，落库留空由 self_id 兜底。
        return _append_group_record(
            int(facts.conversation.raw_id),
            _history_line(facts),
            text=message_text._compact_text(facts.text, limit=max(live.history_text_limit, 1200)),
            sender_name=facts.sender.display_name,
            sender_id=facts.sender.platform_id,
            timestamp=facts.sent_at,
            message_id=facts.message_id,
            attachments=[dict(item) for item in facts.attachments] or None,
        )
    return 0


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(
        text,
        strip_asterisk_styles=live.strip_asterisk_styles,
        strip_underscore_styles=live.strip_underscore_styles,
    ).replace(common._SPLIT_SIGNAL, " ")
    text = common._QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = anonymize._anonymize_text_for_ai(message_text._compact_text(text))
    if text:
        # Bot 自己的回复同样进历史块：它在 durable history 里已有一条 assistant 消息，
        # 这里是第二份，但 compact 重置水位后重发的那 20 行需要完整时序，不能只有别人的话。
        # Bot 行没有发送者身份，不走 _compose_history_line。
        _append_group_record(
            int(group_id),
            f"[{message_text._format_hhmm(None)}] Bot: {text}",
            text=text,
            sender_name="Bot",
            sender_id="",
            timestamp=time.time(),
        )


async def _build_reply_context(event: Event, bot: Bot, conversation_key: str) -> tuple[str, List[Dict[str, Any]]]:
    """增强引用消息解析：event.reply → 本地缓存 → NapCat get_msg，并采集引用图片附件。"""
    reply_message_id = message_text._extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply = await message_text._resolve_reply_payload(bot, event, reply_message_id)

    if not reply:
        if reply_message_id is not None:
            return anonymize._anonymize_text_for_ai(f"（无法获取引用消息内容：message_id={reply_message_id}）"), []
        return "", []

    message = reply.get("message")
    raw_message = reply.get("raw_message")
    text = await message_text._message_to_text_with_forward(bot, message, getattr(bot, "self_id", None))
    if not text:
        text = await message_text._message_to_text_with_forward(bot, raw_message, getattr(bot, "self_id", None))

    # 判断引用的消息是否为 Bot 自己发出的。QQ Bot 的回复里可能带表情包图片，
    # 用户引用这类回复时不应该把 Bot 自己的图片下载成附件再送去识图——那既浪费
    # 算力，也会让模型误以为用户在要求识别一张图片。这里仅当被引用消息不是 Bot
    # 自身发出时才采集图片附件，否则保留 [图片] 文本占位符。
    reply_sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else None
    reply_sender_qq = ""
    if reply_sender is not None:
        reply_sender_qq = str(reply_sender.get("user_id") or reply.get("user_id") or "")
    else:
        reply_sender_qq = str(reply.get("user_id") or "")
    bot_ids = inbound._bot_self_id_candidates(event, bot)
    reply_from_bot = inbound._qq_matches_bot(reply_sender_qq, bot_ids)

    if reply_from_bot:
        # Bot 自己的回复：默认不下载图片附件，避免把表情包送去识图。
        # 但若这条消息是 Bot 发出的真实附件（如绘图产物，已在发送时登记
        # message_id -> 本地路径），则把原图作为附件送给 AI，支持“引用该图继续改”。
        quoted_attachments = []
        bot_msg_atts = outbound_attachments._message_attachment_records(reply_message_id, only_images=True)
        if not bot_msg_atts:
            # Text-only Bot replies are bound to the source images of their task
            # when sent. This persistent cache lets a user's follow-up reply keep
            # the original image even after adapter restart.
            bot_msg_atts = media._cached_reply_image_attachments(reply_message_id, conversation_key)
        # 合并转发把整批图绑到一个卡片 message_id，引用卡片时按每轮上限截断，避免一次喂几张。
        bot_msg_atts = bot_msg_atts[: live.max_images_per_turn]
        if bot_msg_atts:
            for att in bot_msg_atts:
                rel_path = outbound_attachments._to_workspace_rel_path(att["path"])
                quoted_attachments.append({
                    "type": "image",
                    "path": rel_path,
                    "name": att.get("name", "") or Path(att["path"]).name,
                    "source": "onebot_bot_reply",
                })
                text = text.replace(message_text.IMAGE_PLACEHOLDER, f"[{media._attachment_label(att)}: {rel_path}]", 1)
            # 剩余未匹配的图片占位符（如同消息里的表情包）仍标为表情包。
            text = text.replace(message_text.IMAGE_PLACEHOLDER, message_text.STICKER_PLACEHOLDER)
        else:
            text = text.replace(message_text.IMAGE_PLACEHOLDER, message_text.STICKER_PLACEHOLDER)
    else:
        images, files = await media._collect_message_media(bot, message)
        if not images and not files:
            images, files = await media._collect_message_media(bot, raw_message)
        quoted_attachments, quoted_errors = await media._image_sources_to_attachments(images, conversation_key)
        for att in quoted_attachments:
            text = text.replace(message_text.IMAGE_PLACEHOLDER, f"[{media._attachment_label(att)}: {att['path']}]", 1)
        # 引用一条群文件消息是最明确的「读这个」，而群文件带不了 @，只能这样指。
        # 正文里没有占位符可替，直接挂到附件上让 engine 去读内容。
        if files and live.enable_file_input:
            file_atts, file_errors = await media._file_sources_to_attachments(files, conversation_key)
            quoted_attachments.extend(file_atts)
            quoted_errors.extend(file_errors)
        # 下载失败的图（太大/格式不支持）把占位符换成原因，否则引用块里会静默少一张图。
        for note in dict.fromkeys(quoted_errors):
            text = text.replace(message_text.IMAGE_PLACEHOLDER, f"[{note}]", 1)

    text = anonymize._anonymize_text_for_ai(message_text._compact_text(text))
    if not text:
        if reply_message_id is not None:
            return anonymize._anonymize_text_for_ai(f"（引用消息为空或暂不支持的消息类型：message_id={reply_message_id}）"), quoted_attachments
        return "", quoted_attachments

    sender = reply.get("sender")
    sender_id = ""
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id") or reply.get("user_id") or "")
        name_raw = identity._sanitize_name(str(sender.get("card") or sender.get("nickname") or sender_id or "原作者"))
    elif sender is not None:
        sender_id = str(getattr(sender, "user_id", "") or "")
        name_raw = identity._sender_display_name(sender, sender_id)
    else:
        sender_id = str(reply.get("user_id") or "")
        name_raw = identity._sanitize_name(sender_id or "原作者")
    name = anonymize._anonymize_text_for_ai(name_raw)
    user = anonymize._anonymize_user_id(sender_id) if sender_id else "UserUnknown"
    return f"[{message_text._format_hhmm(reply.get('time'))}] {name}({user}): {text}", quoted_attachments
