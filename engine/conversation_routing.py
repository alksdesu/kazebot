from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import Any, Mapping

from .conversation_store import MessageType

SHORT_REPLY = "short_reply"
SHORT_REPLY_MAX_CHARS = 240
# 和适配层的插话尾句同在一条 user 消息里：只说怎么交付，不替模型决定开不开口
SHORT_REPLY_HINT = (
    f"（本轮是简短回应：要说就只接一句自然的话，最多 {SHORT_REPLY_MAX_CHARS} 字，直接用 finish 给出；"
    "不展开任务，不调用工具查资料，不委派，也不连续发消息。）"
)
# 分支合并时记在晚合并那一轮开头输入的 meta 上：{"started_at": "00:07", "finished_at": "00:10"}，按显示时区写好
LATE_MERGE_KEY = "late_merge"


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
    return routing_meta(context).get("response_action") == SHORT_REPLY


def turn_input_meta(context: dict[str, Any] | None) -> dict[str, Any]:
    """当轮用户输入落盘时带的 meta 里决定它怎么渲染的那几项，取法和 runner 落盘时一致。"""
    ctx = context if isinstance(context, dict) else {}
    meta = routing_meta(ctx)
    meta["message_type"] = str(ctx.get("inbound_message_type") or "").strip() or MessageType.USER_INPUT
    return meta


def is_short_reply_input(meta: Any) -> bool:
    """短答任务里的那句用户输入。同一任务的助手消息和工具结果也带着这个路由标记，所以只认 user_input。"""
    if not isinstance(meta, Mapping):
        return False
    return meta.get("response_action") == SHORT_REPLY and meta.get("message_type") == MessageType.USER_INPUT


def with_short_reply_hint(content: Any) -> Any:
    """在用户输入末尾接上短答要求；已经接过的原样返回，同一条消息渲染几次都只有一份。"""
    if isinstance(content, str):
        if content.endswith(SHORT_REPLY_HINT):
            return content
        return f"{content}\n\n{SHORT_REPLY_HINT}" if content.strip() else SHORT_REPLY_HINT
    if isinstance(content, list):
        last = content[-1] if content else None
        if isinstance(last, dict) and last.get("type") == "text" and last.get("text") == SHORT_REPLY_HINT:
            return content
        return [*content, {"type": "text", "text": SHORT_REPLY_HINT}]
    return content


def late_merge_meta(started: datetime, finished: datetime, zone: tzinfo) -> dict[str, str]:
    """晚合并那一轮的起止时间按显示时区写好；跨天带上日期。"""
    start, end = started.astimezone(zone), finished.astimezone(zone)
    pattern = "%H:%M" if start.date() == end.date() else "%m-%d %H:%M"
    return {"started_at": start.strftime(pattern), "finished_at": end.strftime(pattern)}


def late_merge_note(meta: Any) -> str:
    """历史按做完的先后排：比它晚开始的轮先做完排在了它前面，开头标出它自己的起止时间，模型才读得懂先后。"""
    mark = meta.get(LATE_MERGE_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(mark, Mapping):
        return ""
    started, finished = str(mark.get("started_at") or "").strip(), str(mark.get("finished_at") or "").strip()
    if not started or not finished:
        return ""
    return f"[这一轮 {started} 开始、{finished} 做完；上面有几轮比它开始得晚，先做完排在了前面]"


def with_leading_note(content: Any, note: str) -> Any:
    """在正文开头接上一行说明；已经接过的原样返回。"""
    if isinstance(content, str):
        return content if content.startswith(note) else f"{note}\n{content}"
    if isinstance(content, list):
        first = content[0] if content else None
        if isinstance(first, dict) and first.get("type") == "text" and first.get("text") == note:
            return content
        return [{"type": "text", "text": note}, *content]
    return content


def render_user_input(content: Any, meta: Any) -> Any:
    """用户输入发给模型的样子只由正文和它落盘的 meta 决定：当轮发出去的和之后从存储重载的逐字相同。"""
    shown = with_short_reply_hint(content) if is_short_reply_input(meta) else content
    note = late_merge_note(meta)
    return with_leading_note(shown, note) if note else shown


def render_history_inputs(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按各自的 meta 渲染历史里的用户消息（轮摘要顶替的那条也带着原输入的 meta）；要改的换成浅拷贝，传进来的消息不动。"""
    rendered: list[dict[str, Any]] = []
    for message in history:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            shown = render_user_input(content, message.get("_meta"))
            if shown is not content:
                message = {**message, "content": shown}
        rendered.append(message)
    return rendered


def concise_text(text: str, limit: int = SHORT_REPLY_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    boundary = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n", ". ", "! ", "? "))
    return prefix[:boundary + 1] if boundary >= limit // 2 else prefix.rstrip() + "…"


_TERMINAL_PLACEHOLDER_RE = re.compile(r"<[A-Za-z0-9_]{2,}>")


def is_void_terminal_text(text: str) -> bool:
    # finish/ask 的正文空到不该发给用户：空白、纯标点省略号、或整段只是个 <OCCUPY> 式占位符
    stripped = _TERMINAL_PLACEHOLDER_RE.sub("", text or "")
    return not re.sub(r"[\s\W_]+", "", stripped)
