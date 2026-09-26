"""OneBot 消息段与消息 id 的纯解析。不依赖 NoneBot：消息段是 dict 还是 MessageSegment 都认。"""
from __future__ import annotations

from typing import Any, Iterable


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
