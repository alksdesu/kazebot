"""平台随入站送来的输出说明（分条、表态、可用表情这类写法）怎么放进提示词。

稳定的段进缓存前缀，每轮会变的段并进动态区，两处都不写进对话历史。
"""
from __future__ import annotations

from typing import Any, Iterable

OUTPUT_GUIDE_KEY = "output_guide"
# 前缀里那条 system 消息的标记：打断带来新说明时按它原位替换。
STATIC_GUIDE_MARKER = "_output_guide"

_STATIC_OPEN, _STATIC_CLOSE = "[OUTPUT_GUIDE:CONSTANT]", "[/OUTPUT_GUIDE:CONSTANT]"
_TURN_OPEN, _TURN_CLOSE = "[OUTPUT_GUIDE:ACTIVE]", "[/OUTPUT_GUIDE:ACTIVE]"


def guide_texts(raw: Any) -> tuple[list[str], list[str]]:
    """拆成（稳定的段，每轮会变的段）。形状不对的段跳过，一段坏数据不该挡住整轮推理。"""
    stable: list[str] = []
    volatile: list[str] = []
    sections = raw.get("sections") if isinstance(raw, dict) else None
    if not isinstance(sections, list):
        return stable, volatile
    for section in sections:
        if not isinstance(section, dict):
            continue
        text = section.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        (volatile if section.get("volatile") is True else stable).append(text.strip())
    return stable, volatile


def static_guide_messages(raw: Any) -> list[dict[str, Any]]:
    stable, _ = guide_texts(raw)
    if not stable:
        return []
    return [{"role": "system", "content": _wrap(_STATIC_OPEN, stable, _STATIC_CLOSE), STATIC_GUIDE_MARKER: True}]


def turn_guide_parts(raw: Any, *, include_stable: bool = False) -> list[str]:
    """并进动态区的部分。前缀里没有稳定那条时，稳定的段也放这里，不去改动前缀。"""
    stable, volatile = guide_texts(raw)
    texts = [*(stable if include_stable else ()), *volatile]
    return [_wrap(_TURN_OPEN, texts, _TURN_CLOSE)] if texts else []


def has_static_guide(messages: Iterable[dict[str, Any]]) -> bool:
    return any(message.get(STATIC_GUIDE_MARKER) for message in messages)


def _wrap(open_tag: str, texts: list[str], close_tag: str) -> str:
    return "\n".join((open_tag, "\n\n".join(texts), close_tag))
