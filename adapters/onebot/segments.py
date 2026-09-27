"""OneBot 消息段与消息 id 的纯解析。不依赖 NoneBot：消息段是 dict 还是 MessageSegment 都认。"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# 收藏表情发出来是带这个子类型的 image 段；商城表情、群友表情另有段类型。
_STICKER_SUB_TYPE = 1
_STICKER_SEGMENT_TYPES = frozenset({"mface", "marketface"})
_STICKER_SUMMARY_HINTS = ("表情", "emoji", "sticker", "贴纸")


def normalize_message_id(message_id: Any) -> str:
    """消息 id 归一化成稳定字符串。

    reply 段、get_msg 和 send_msg 回来的 id 可能是 int、带小数点的字符串或带空白，写入和反查都走这里才对得上。
    """
    if message_id is None:
        return ""
    key = str(message_id).strip()
    if not key:
        return ""
    try:
        return str(int(float(key))) if "." in key else str(int(key))
    except (ValueError, TypeError):
        return key


def at_segment_user_ids(message: Iterable[Any] | None) -> list[str]:
    """Return the raw user ids of every `at` segment, skipping @全体成员."""
    result: list[str] = []
    for segment in message or []:
        seg_type = segment.get("type") if isinstance(segment, dict) else getattr(segment, "type", "")
        if str(seg_type) != "at":
            continue
        data = segment.get("data") if isinstance(segment, dict) else getattr(segment, "data", {})
        raw = str((data or {}).get("qq") or "").strip()
        # qq=all 是 @全体成员，不对应任何一个人。
        if raw and raw.lower() != "all" and raw not in result:
            result.append(raw)
    return result


def marked_as_sticker(seg_type: str, data: Mapping[str, Any]) -> bool:
    """QQ 自己说这是表情包吗。

    命中任一即算：商城/群友表情段、收藏表情的子类型、摘要文案、表情资源 id。
    """
    if str(seg_type or "").strip().lower() in _STICKER_SEGMENT_TYPES:
        return True
    raw_sub = data.get("sub_type", data.get("subType"))
    try:
        if raw_sub is not None and int(raw_sub) == _STICKER_SUB_TYPE:
            return True
    except (TypeError, ValueError):
        pass
    summary = str(data.get("summary") or "").lower()
    if any(hint in summary for hint in _STICKER_SUMMARY_HINTS):
        return True
    return bool(str(data.get("emoji_id") or data.get("emojiId") or "").strip())


def any_marked_segment(segments: Iterable[tuple[str, Mapping[str, Any]]]) -> bool:
    """这条消息里有没有被 QQ 标成表情包的段。附件层只认商城表情，收藏表情要看段。"""
    return any(marked_as_sticker(seg_type, data) for seg_type, data in segments)
