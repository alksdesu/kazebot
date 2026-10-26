"""服务中途重启时，给最后一轮没来得及写结果的工具调用补上「中断，结果未知」。

只看历史末尾那一轮：调用 id 在不同轮次里可能重复，结果必须紧跟在发出调用的那条 assistant 消息后面才配得上对。
"""
from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime, timezone
from typing import Any

from toolbox.tool_names import is_pseudo_tool_name

from .conversation_store import ConversationStore, Message, MessageType

INTERRUPTED_RESUME_TYPE = "interrupted"
INTERRUPTED_TOOL_STATUS = "interrupted"
INTERRUPTED_MARK_KEY = "interrupted_call"
INTERRUPTED_RESULT_TEXT = (
    "中断，结果未知：执行这个调用时服务重启了，它可能没执行、只执行了一部分，也可能已经执行完。"
    "先核对实际状态（比如重新读相关文件、查看命令的产物），再决定要不要重做。"
)
# finish / ask 被打断时本来就不留结果，配对修复会把它们当作没发生过，补上结果反倒让模型以为交付过
_SKIPPED_CALLS = frozenset({"finish", "ask"})
_ROUTING_META_KEYS = ("response_action", "reply_message_id", "delivery_purpose", "source_message_refs")


def _call_id(call: Any) -> str:
    return str(call.get("id") or "").strip() if isinstance(call, dict) else ""


def _call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or function.get("name") or "").strip()


def _last_round(messages: Sequence[Message]) -> int | None:
    """末尾那一轮的 assistant 下标：它后面只跟着工具结果；末尾不是这种形状就是 None。"""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "tool":
            continue
        if message.role == "assistant" and message.tool_calls:
            return index
        return None
    return None


def dangling_tail_calls(
    messages: Sequence[Message], *, task_ids: Collection[str] | None = None,
) -> tuple[Message | None, list[tuple[str, str]]]:
    """末尾那一轮里还没有结果的调用，按发出顺序给 (调用 id, 工具名)；task_ids 给了就只认这些任务发出的。"""
    index = _last_round(messages)
    if index is None:
        return None, []
    assistant = messages[index]
    if task_ids is not None and str(assistant.source_task_id or "") not in set(task_ids):
        return None, []
    answered = [str(message.tool_call_id or "") for message in messages[index + 1:]]
    missing: list[tuple[str, str]] = []
    for call in assistant.tool_calls or []:
        call_id, name = _call_id(call), _call_name(call)
        if not call_id or not name or name in _SKIPPED_CALLS:
            continue
        if call_id in answered:
            answered.remove(call_id)
            continue
        missing.append((call_id, name))
    return assistant, missing


def interrupted_results(
    messages: Sequence[Message], *, task_ids: Collection[str] | None = None, now: str = "",
) -> list[Message]:
    """给末尾那一轮缺结果的调用各造一条「中断，结果未知」的工具结果，格式和正常落盘的工具结果一致。"""
    assistant, missing = dangling_tail_calls(messages, task_ids=task_ids)
    if assistant is None or not missing:
        return []
    stamp = now or datetime.now(timezone.utc).isoformat()
    source_meta = assistant.meta if isinstance(assistant.meta, dict) else {}
    base_meta: dict[str, Any] = {key: source_meta[key] for key in _ROUTING_META_KEYS if key in source_meta}
    base_meta.update({
        "tool_mode": str(source_meta.get("tool_mode") or "fake-native"),
        "message_type": MessageType.TOOL_RESULT,
        INTERRUPTED_MARK_KEY: True,
    })
    if assistant.source_task_id:
        base_meta["source_task_id"] = assistant.source_task_id
    results: list[Message] = []
    for call_id, name in missing:
        meta = dict(base_meta)
        # 执行状态只对真工具有意义，伪工具的结果不记
        if not is_pseudo_tool_name(name):
            meta["tool_status"] = INTERRUPTED_TOOL_STATUS
        results.append(Message(
            id=str(uuid.uuid4()),
            role="tool",
            content=INTERRUPTED_RESULT_TEXT,
            message_type=MessageType.TOOL_RESULT,
            created_at=stamp,
            meta=meta,
            source_node_id=assistant.source_node_id,
            source_task_id=assistant.source_task_id,
            tool_call_id=call_id,
            name=name,
        ))
    return results


def append_interrupted_results(
    store: ConversationStore, session_id: str, *, task_ids: Collection[str] | None = None,
) -> list[Message]:
    """读出会话、补上末尾那一轮缺的结果并追加落盘，返回补了哪些。"""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    added = interrupted_results(store.load(sid), task_ids=task_ids)
    if added:
        store.append_batch(sid, added)
    return added
