from __future__ import annotations

from typing import Any


def routing_meta(context: dict[str, Any]) -> dict[str, Any]:
    hints = context.get("route_hints")
    if not isinstance(hints, dict):
        return {}
    result = {}
    for key in ("topic_id", "response_action", "reply_message_id", "delivery_purpose"):
        value = hints.get(key)
        if isinstance(value, str) and value:
            result[key] = value[:256]
    refs = hints.get("source_message_refs")
    if isinstance(refs, list):
        result["source_message_refs"] = [str(ref)[:128] for ref in refs[:50]]
    return result


def topic_history(history: list[dict[str, Any]], topic_id: str, *, current_task_id: str = "") -> list[dict[str, Any]]:
    if not topic_id:
        return history
    groups: dict[str, list[int]] = {}
    selected: set[int] = set()
    call_groups: dict[str, str] = {}
    legacy_group = "legacy:0"
    for index, message in enumerate(history):
        meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else {}
        message_type = meta.get("message_type") or message.get("message_type")
        if message.get("role") == "system" or message.get("_dynamic"):
            selected.add(index)
            continue
        if not meta.get("topic_id") and (message_type == "summary" or meta.get("source_task_id") == "compact_summary"):
            selected.add(index)
            continue
        if message.get("role") == "user" and not meta.get("source_task_id") and message_type != "tool_result":
            legacy_group = f"legacy:{index}"
        key = str(meta.get("source_task_id") or legacy_group)
        call_id = str(message.get("tool_call_id") or "")
        if call_id and call_id in call_groups:
            key = call_groups[call_id]
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                call_groups[str(call["id"])] = key
        groups.setdefault(key, []).append(index)
    legacy: list[list[int]] = []
    for key, indices in groups.items():
        topics = {history[index]["_meta"].get("topic_id") for index in indices if isinstance(history[index].get("_meta"), dict)}
        topics.discard(None)
        topics.discard("")
        if topic_id in topics or (current_task_id and key == current_task_id):
            selected.update(indices)
        elif not topics:
            legacy.append(indices)
    # 旧历史按完整轮次保留少量背景，不能拆开工具调用和结果。
    for indices in legacy[-3:]:
        selected.update(indices)
    return [message for index, message in enumerate(history) if index in selected]


def short_reply(context: dict[str, Any]) -> bool:
    return routing_meta(context).get("response_action") == "short_reply"


def concise_text(text: str, limit: int = 240) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    boundary = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n", ". ", "! ", "? "))
    return prefix[:boundary + 1] if boundary >= limit // 2 else prefix.rstrip() + "…"
