from __future__ import annotations

from typing import Any


def routing_meta(context: dict[str, Any]) -> dict[str, Any]:
    hints = context.get("route_hints")
    if not isinstance(hints, dict):
        return {}
    result = {}
    for key in ("response_action", "reply_message_id", "delivery_purpose"):
        value = hints.get(key)
        if isinstance(value, str) and value:
            result[key] = value[:256]
    refs = hints.get("source_message_refs")
    if isinstance(refs, list):
        result["source_message_refs"] = [str(ref)[:128] for ref in refs[:50]]
    return result


def topic_history(history: list[dict[str, Any]], topic_id: str, *, current_task_id: str = "") -> list[dict[str, Any]]:
    # 旧插件仍可能导入此函数，历史标签不再影响当前会话的上下文。
    return history


def short_reply(context: dict[str, Any]) -> bool:
    return routing_meta(context).get("response_action") == "short_reply"


def concise_text(text: str, limit: int = 240) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    boundary = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n", ". ", "! ", "? "))
    return prefix[:boundary + 1] if boundary >= limit // 2 else prefix.rstrip() + "…"
