"""OneBot 消息段的纯解析。不依赖 NoneBot：消息段是 dict 还是 MessageSegment 都认。"""
from __future__ import annotations

from typing import Any, Iterable


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
