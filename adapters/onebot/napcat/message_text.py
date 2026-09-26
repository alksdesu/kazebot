"""消息段转文本、合并转发展开、引用解析。"""
from __future__ import annotations

import logging
import asyncio
import datetime as dt
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from nonebot.adapters.onebot.v11 import Bot, Event, Message
from ..faces import face_display_name
from ..live_config import live

from . import anonymize, common, identity, inbound, models, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _format_hhmm(timestamp: Optional[int]) -> str:
    """把 OneBot 秒级时间戳格式化为入口节点要求的 HH:MM。"""
    try:
        return dt.datetime.fromtimestamp(int(timestamp or time.time()), common._CST).strftime("%H:%M")
    except Exception:
        return dt.datetime.now(common._CST).strftime("%H:%M")


def _compact_text(text: str, limit: int | None = None) -> str:
    """把历史消息压缩到单行，避免每轮请求携带过长上下文。"""
    limit = live.history_text_limit if limit is None else limit
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


# 商城表情走图片下载通道，占位符必须和 image 一致，否则附件路径回填不到正文。
STICKER_SEGMENT_TYPES = frozenset({"mface", "marketface"})


IMAGE_SEGMENT_TYPES = frozenset({"image"}) | STICKER_SEGMENT_TYPES


# 下游用 text.replace(IMAGE_PLACEHOLDER, ...) 把附件路径回填进正文，改字面量会同时打断回填和合并转发的占位清理。
IMAGE_PLACEHOLDER = "[图片]"


FACE_PLACEHOLDER = "[QQ表情]"


VOICE_PLACEHOLDER = "[语音]"


VIDEO_PLACEHOLDER = "[视频]"


FORWARD_PLACEHOLDER = "[合并转发]"


STICKER_PLACEHOLDER = "[表情包]"


def _segment_type_and_data(segment: Any) -> tuple[str, dict[str, Any]]:
    """兼容 NoneBot MessageSegment 与 OneBot dict segment。"""
    if isinstance(segment, dict):
        seg_type = str(segment.get("type") or "")
        raw_data = segment.get("data")
        return seg_type, raw_data if isinstance(raw_data, dict) else {}
    seg_type = str(getattr(segment, "type", "") or "")
    raw_data = getattr(segment, "data", {}) or {}
    return seg_type, raw_data if isinstance(raw_data, dict) else {}


def _bot_id_set(bot_self_id: Any) -> set[str]:
    value = str(bot_self_id or "").strip()
    return {value} if value else set()


def _segment_to_text(seg_type: str, data: Mapping[str, Any], bot_ids: set[str]) -> str | None:
    """单个消息段的模型可读文本。返回 None 表示这一段不进正文。"""
    if seg_type == "text":
        return str(data.get("text") or "")
    if seg_type == "at":
        qq = str(data.get("qq") or "").strip()
        if inbound._qq_matches_bot(qq, bot_ids):
            return None
        if qq.lower() == "all":
            return "@全体成员"
        # 没有 profile 显示名时回退到匿名别名（并登记映射），避免裸 QQ 号进入上下文。
        return f"@{identity._qq_profile_display_name(qq) or anonymize._anonymize_user_id(qq)}"
    if seg_type in IMAGE_SEGMENT_TYPES:
        return IMAGE_PLACEHOLDER
    if seg_type == "face":
        face_id = str(data.get("id") or "").strip()
        if not face_id:
            return FACE_PLACEHOLDER
        return f"[QQ表情:{face_display_name(face_id) or face_id}]"
    if seg_type == "record":
        return VOICE_PLACEHOLDER
    if seg_type == "video":
        return VIDEO_PLACEHOLDER
    if seg_type == "file":
        name = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
        return f"[文件:{identity._sanitize_name(Path(name).name if name else '附件', max_len=80)}]"
    if seg_type == "forward":
        return FORWARD_PLACEHOLDER
    if seg_type == "reply":
        # 引用内容由 _build_reply_context 单独还原成引用块，正文里再放一次等于把裸 message_id 交给模型。
        return None
    return f"[{seg_type}]" if seg_type else None


def _message_to_text(
    message: Any, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    """把 OneBot 消息段转换为模型可读文本，同时用占位符保留非文本内容。

    forward_blocks 按出现顺序顶掉转发占位符，展开内容就落在卡片原来的位置。
    """
    bot_ids = _bot_id_set(bot_self_id)
    pending = list(forward_blocks or [])
    parts: List[str] = []
    for segment in message:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type == "forward" and pending:
            parts.append("\n" + pending.pop(0) + "\n")
            continue
        rendered = _segment_to_text(seg_type, data, bot_ids)
        if rendered is not None:
            parts.append(rendered)
    return "".join(parts).strip()


def _message_to_text_generic(
    message: Any, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    """把 NoneBot Message、OneBot segment list 或 CQ 字符串转换为模型可读文本。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return _format_cq_message(message, bot_self_id, forward_blocks).strip()
    # Message 是 list 子类，和裸 segment list 走同一条遍历。
    if isinstance(message, (list, tuple)):
        return _message_to_text(message, bot_self_id, forward_blocks)
    try:
        return _message_to_text(Message(message), bot_self_id, forward_blocks)
    except Exception:
        return str(message).strip()


def _parse_cq_params(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _iter_segments(message: Any) -> List[tuple[str, Dict[str, Any]]]:
    """把 Message、裸 segment list 或 CQ 字符串摊成同一串 (类型, data)。"""
    if message is None:
        return []
    if isinstance(message, str):
        return [(m.group(1), _parse_cq_params(m.group(2) or "")) for m in common._CQ_RE.finditer(message)]
    try:
        return [_segment_type_and_data(segment) for segment in message]
    except Exception:
        return []


def _forward_source_from_data(data: Mapping[str, Any]) -> models.ForwardSource | None:
    """把一个 forward 段的 data 解析成来源。两种形态都取不到时返回 None。"""
    forward_id = ""
    for key in ("id", "res_id", "resId", "forward_id", "forwardId"):
        value = data.get(key)
        if value is not None and str(value).strip():
            forward_id = str(value).strip()
            break
    # 有些实现把嵌套层直接内嵌成 node 列表而不给 res_id，不认这一形态那层就只剩占位符。
    inline: tuple[Any, ...] = ()
    for key in ("content", "messages", "nodes"):
        value = data.get(key)
        if isinstance(value, (list, tuple)) and value:
            inline = tuple(value)
            break
    if not forward_id and not inline:
        return None
    return models.ForwardSource(forward_id=forward_id, inline=inline)


def _extract_forward_sources(message: Any) -> List[models.ForwardSource]:
    """按出现顺序提取消息里的合并转发卡片。顺序要和正文里的占位符一一对上。"""
    sources: List[models.ForwardSource] = []
    for seg_type, data in _iter_segments(message):
        if seg_type != "forward":
            continue
        source = _forward_source_from_data(data)
        if source is not None:
            sources.append(source)
    return sources


_FORWARD_CACHE_TTL_SEC = 120.0


_FORWARD_CACHE_MAX = 64


def _normalize_forward_messages(data: Any) -> List[Dict[str, Any]] | None:
    """get_forward_msg 的返回在各实现间形状不一：裸列表、{messages}、{data:{messages}}。"""
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        return [m for m in messages if isinstance(m, dict)]
    return None


async def _get_forward_messages(bot: Bot, forward_id: str) -> List[Dict[str, Any]] | None:
    """通过 NapCat get_forward_msg 读取合并转发消息列表。

    结果按 res_id 短期缓存：一条消息的正文展开和图片采集各遍历一遍，同一张卡片没有
    拉两次的道理。
    """
    if not forward_id:
        return None
    now = time.time()
    cached = state._adapter.forward_msg_cache.get(forward_id)
    if cached is not None:
        if now - cached[0] <= _FORWARD_CACHE_TTL_SEC:
            state._adapter.forward_msg_cache.move_to_end(forward_id)
            return cached[1]
        state._adapter.forward_msg_cache.pop(forward_id, None)
    try:
        data = await bot.call_api("get_forward_msg", id=forward_id)
    except Exception:
        common.logger.warning("get_forward_msg failed for forward_id=%s", forward_id, exc_info=True)
        return None
    messages = _normalize_forward_messages(data)
    if messages is None:
        return None
    state._adapter.forward_msg_cache[forward_id] = (now, messages)
    while len(state._adapter.forward_msg_cache) > _FORWARD_CACHE_MAX:
        state._adapter.forward_msg_cache.popitem(last=False)
    return messages


async def _forward_source_messages(bot: Bot, source: models.ForwardSource) -> List[Dict[str, Any]] | None:
    """取一张卡片的子消息。接口拉不到时退回上报里内嵌的那份。"""
    messages = await _get_forward_messages(bot, source.forward_id) if source.forward_id else None
    if messages is None:
        messages = [m for m in source.inline if isinstance(m, dict)] or None
    return messages


# 合并转发子消息发送者无名片/昵称时的固定占位。
FORWARD_SENDER_PLACEHOLDER = "转发用户"


def _format_forward_sender(sender: Any) -> str:
    """返回合并转发子消息发送者的显示名。

    2026-07-09 修改原因：合并转发卡片里的发送者往往是与 Bot 无直接交互的路人
    （一张卡片可能带几十个陆陌号）。不应把这些无关 QQ 号写入匿名映射表（更不应持久化）。
    因此这里不再调 _anonymize_user_id（既不登记内存匿名表、也不落盘），直接用
    profile 显示名 / 群名片 / 昵称；都没有时回退到固定占位，绝不暴露裸 QQ 号。
    """
    if isinstance(sender, dict):
        user_id = str(sender.get("user_id") or "").strip()
        raw_name = str(sender.get("card") or sender.get("nickname") or "").strip()
    else:
        user_id = str(getattr(sender, "user_id", "") or "").strip()
        raw_name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
    display = identity._qq_profile_display_name(user_id) or raw_name
    return identity._sanitize_name(display) if display else FORWARD_SENDER_PLACEHOLDER


def _truncate_forward_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= live.forward_msg_text_limit:
        return text
    return text[:live.forward_msg_text_limit] + "\n…（合并转发内容过长，已截断）"


async def _format_forward_msg(
    bot: Bot,
    source: models.ForwardSource,
    bot_self_id: Any = None,
    *,
    depth: int,
    visited: set[str],
    remaining: List[int],
) -> str:
    """递归读取并格式化单张合并转发卡片。"""
    forward_id = source.forward_id
    label = f"合并转发 id={forward_id}" if forward_id else "合并转发"
    if not live.enable_forward_msg_input:
        return f"[{label}]"
    if not forward_id and not source.inline:
        return "[合并转发:缺少id]"
    if depth <= 0:
        return f"[{label}, 已达到读取深度上限]"
    if forward_id and forward_id in visited:
        return f"[{label}, 已跳过循环引用]"
    if remaining[0] <= 0:
        return "[合并转发:已达到读取条数上限]"

    if forward_id:
        visited.add(forward_id)
    messages = await _forward_source_messages(bot, source)
    if messages is None:
        visited.discard(forward_id)
        return f"[{label}, 读取失败]"

    lines: List[str] = [f"【{label}】"]
    for index, item in enumerate(messages, start=1):
        if remaining[0] <= 0:
            lines.append("…（合并转发条数过多，已截断）")
            break
        remaining[0] -= 1
        sender_name = _format_forward_sender(item.get("sender"))
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = await _message_to_text_with_forward(
            bot,
            content,
            bot_self_id,
            depth=depth - 1,
            visited=visited,
            remaining=remaining,
        )
        text = _compact_text(text or "[暂不支持的消息类型]", limit=max(live.history_text_limit, 1200))
        # 合并转发子消息只展示发送者显示名，不附带匿名 ID（避免缓存无关号）。
        lines.append(f"{index}. [{_format_hhmm(item.get('time'))}] {sender_name}: {text}")
    lines.append("【合并转发结束】")
    visited.discard(forward_id)
    return _truncate_forward_text("\n".join(lines))


async def _message_to_text_with_forward(
    bot: Bot,
    message: Any,
    bot_self_id: Any = None,
    *,
    depth: int | None = None,
    visited: set[str] | None = None,
    remaining: List[int] | None = None,
) -> str:
    """把消息转为文本，合并转发卡片就地展开成内容。"""
    if not live.enable_forward_msg_input:
        return _message_to_text_generic(message, bot_self_id)
    sources = _extract_forward_sources(message)
    if not sources:
        return _message_to_text_generic(message, bot_self_id)

    depth_value = live.forward_msg_max_depth if depth is None else depth
    visited_set = visited if visited is not None else set()
    remaining_box = remaining if remaining is not None else [live.forward_msg_max_messages]

    async def expand() -> List[str]:
        blocks: List[str] = []
        for source in sources:
            blocks.append(await _format_forward_msg(
                bot,
                source,
                bot_self_id,
                depth=depth_value,
                visited=visited_set,
                remaining=remaining_box,
            ))
        return blocks

    if depth is not None:
        return _message_to_text_generic(message, bot_self_id, await expand())

    timeout = float(live.forward_msg_timeout_sec)
    try:
        blocks = await asyncio.wait_for(expand(), timeout=timeout)
    except asyncio.TimeoutError:
        # 群触发判定也走这条路，干等下去会把后面的 matcher 一起压住。
        common.logger.warning("forward expansion timed out after %.1fs", timeout)
        blocks = ["[合并转发:读取超时]" for _ in sources]
    return _message_to_text_generic(message, bot_self_id, blocks)


_EVENT_TEXT_CACHE_MAX = 128


async def _event_text_with_forward(bot: Bot, event: Event) -> str:
    """事件正文，转发卡片已展开。同一条事件只展开一次。

    两个 rule 加 handler 会各要一份同样的文本；判定缓存和冷却记账又都拿正文当 key，
    两边给的不一样这条消息就会被判两遍。
    """
    message = event.get_message() if hasattr(event, "get_message") else None
    if not _extract_forward_sources(message):
        return _message_to_text_generic(message, getattr(bot, "self_id", None))
    key = inbound._event_identity(event)
    cached = state._adapter.event_text_cache.get(key)
    if cached is not None:
        state._adapter.event_text_cache.move_to_end(key)
        return cached
    text = await _message_to_text_with_forward(bot, message, getattr(bot, "self_id", None))
    state._adapter.event_text_cache[key] = text
    while len(state._adapter.event_text_cache) > _EVENT_TEXT_CACHE_MAX:
        state._adapter.event_text_cache.popitem(last=False)
    return text


def _format_cq_message(
    text: str, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    bot_ids = _bot_id_set(bot_self_id)
    pending = list(forward_blocks or [])

    def repl(match: re.Match[str]) -> str:
        seg_type = match.group(1)
        if seg_type == "forward" and pending:
            return "\n" + pending.pop(0) + "\n"
        rendered = _segment_to_text(seg_type, _parse_cq_params(match.group(2) or ""), bot_ids)
        return "" if rendered is None else rendered

    return common._CQ_RE.sub(repl, text)


def _scan_reply_reference(message: Any, raw_message: Any = None) -> tuple[bool, Any | None]:
    """扫描 reply 段：返回（有没有 reply 段, 被引用消息 id）。id 可能为 None（段在但无可用 id）。"""
    def pick_id(data: Dict[str, Any]) -> Any | None:
        for key in ("id", "message_id", "messageId", "messageid", "message_seq", "seq"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return value
        return None

    has_reply = False
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for seg in segments:
        seg_type, data = _segment_type_and_data(seg)
        if seg_type != "reply":
            continue
        has_reply = True
        rid = pick_id(data)
        if rid is not None:
            return True, rid

    for candidate in (message, raw_message):
        if not isinstance(candidate, str):
            continue
        for match in common._CQ_RE.finditer(candidate):
            if match.group(1) != "reply":
                continue
            has_reply = True
            rid = _parse_cq_params(match.group(2) or "").get("id")
            if rid is not None and str(rid).strip():
                return True, rid
    return has_reply, None


def _extract_reply_message_id(message: Any, raw_message: Any = None) -> Any | None:
    """从 reply segment / CQ reply 中提取被引用消息 ID。"""
    return _scan_reply_reference(message, raw_message)[1]


def _remember_message_for_reply_context(event: Event) -> None:
    """缓存最近消息，弥补 NapCat/NoneBot reply 字段不完整的情况。"""
    message_id = getattr(event, "message_id", None)
    if message_id is None or not str(message_id).strip():
        return
    sender = getattr(event, "sender", None)
    state._adapter.reply_message_cache[str(message_id)] = {
        "message": event.get_message() if hasattr(event, "get_message") else None,
        "raw_message": getattr(event, "raw_message", None),
        "sender": sender,
        "user_id": getattr(event, "user_id", None),
        "time": getattr(event, "time", None),
    }
    if str(message_id) not in state._adapter.reply_message_cache_order:
        state._adapter.reply_message_cache_order.append(str(message_id))
    while len(state._adapter.reply_message_cache_order) > 1000:
        old_id = state._adapter.reply_message_cache_order.popleft()
        state._adapter.reply_message_cache.pop(old_id, None)


async def _get_reply_message(bot: Bot, reply_message_id: Any) -> Dict[str, Any] | None:
    """通过 NapCat get_msg 兜底获取被引用消息。"""
    if reply_message_id is None:
        return None
    raw_id = str(reply_message_id).strip()
    message_id_param: Any = int(raw_id) if raw_id.isdigit() else reply_message_id
    try:
        data = await bot.call_api("get_msg", message_id=message_id_param)
    except Exception:
        common.logger.warning("get_msg failed for reply_message_id=%s", reply_message_id, exc_info=True)
        return None
    if isinstance(data, dict):
        return data
    return None


_REPLY_PAYLOAD_FIELDS = ("message", "raw_message", "sender", "user_id", "time")


def _reply_payload_from_source(source: Any) -> Dict[str, Any]:
    """把 event.reply 对象 / 缓存条目 / get_msg 返回值统一成同一份字段集。"""
    if source is None:
        return {}
    if isinstance(source, dict):
        return {key: source.get(key) for key in _REPLY_PAYLOAD_FIELDS}
    return {key: getattr(source, key, None) for key in _REPLY_PAYLOAD_FIELDS}


def _reply_payload_has_content(payload: Dict[str, Any]) -> bool:
    return bool(payload.get("message") is not None or payload.get("raw_message"))


def _fill_reply_payload(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if value is not None and payload.get(key) is None:
            payload[key] = value


async def _resolve_reply_payload(bot: Bot, event: Event, reply_message_id: Any) -> Dict[str, Any] | None:
    """event.reply → 本地缓存 → get_msg 逐级补全被引用消息，只填空位不覆盖。

    NapCat 私聊引用常常 get_msg 取不回，覆盖式回退会把 event.reply 已经给出的 raw_message/sender 清空。
    """
    payload = _reply_payload_from_source(getattr(event, "reply", None))
    if not _reply_payload_has_content(payload) and reply_message_id is not None:
        _fill_reply_payload(payload, _reply_payload_from_source(state._adapter.reply_message_cache.get(str(reply_message_id))))
    if not _reply_payload_has_content(payload) and reply_message_id is not None:
        _fill_reply_payload(payload, _reply_payload_from_source(await _get_reply_message(bot, reply_message_id)))
    return payload if any(value is not None for value in payload.values()) else None
