"""节点待办清单（update_todos）：参数校验、渲染，以及任务挂起恢复后从本任务的调用记录里重建。"""
from __future__ import annotations

import json
from typing import Any

TODO_TOOL_NAME = "update_todos"
TODO_STATUSES = ("pending", "in_progress", "completed")
MAX_TODO_ITEMS = 30
MAX_TODO_CONTENT_CHARS = 200
# 恢复时按这个开头认出成功的那次调用，改文案要连同 latest_todos_from_messages 一起看
TODO_UPDATED_PREFIX = "待办清单已更新"

_STATUS_LABELS = {"pending": "待办", "in_progress": "进行中", "completed": "已完成"}


class TodoListError(ValueError):
    """update_todos 的参数不合格，清单保持原样。"""


def parse_todo_items(raw: Any) -> list[dict[str, str]]:
    """校验一份完整清单，返回规范化后的 [{content, status}]；不合格抛 TodoListError，原因写给模型看。"""
    if not isinstance(raw, list):
        raise TodoListError('items 必须是数组，每一项形如 {"content": "...", "status": "pending"}。')
    if len(raw) > MAX_TODO_ITEMS:
        raise TodoListError(f"待办最多 {MAX_TODO_ITEMS} 项，这次传了 {len(raw)} 项；合并或删掉一些再传完整清单。")
    items: list[dict[str, str]] = []
    problems: list[str] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            problems.append(f"第 {index} 项不是对象")
            continue
        content = entry.get("content")
        content = content.strip() if isinstance(content, str) else ""
        status = entry.get("status")
        status = status.strip().lower() if isinstance(status, str) else ""
        if not content:
            problems.append(f"第 {index} 项缺少 content")
        elif len(content) > MAX_TODO_CONTENT_CHARS:
            problems.append(f"第 {index} 项超过 {MAX_TODO_CONTENT_CHARS} 字")
        if status not in TODO_STATUSES:
            problems.append(f"第 {index} 项的 status 只能是 {' / '.join(TODO_STATUSES)}")
        if content and len(content) <= MAX_TODO_CONTENT_CHARS and status in TODO_STATUSES:
            items.append({"content": content, "status": status})
    if problems:
        raise TodoListError("；".join(problems) + "。清单没有更新，改好后再传一次完整清单。")
    return items


def unfinished_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in items if item.get("status") != "completed"]


def render_todo_lines(items: list[dict[str, str]]) -> list[str]:
    return [
        f"{index}. [{_STATUS_LABELS.get(item.get('status', ''), item.get('status', ''))}] {item.get('content', '')}"
        for index, item in enumerate(items, start=1)
    ]


def _counts(items: list[dict[str, str]]) -> str:
    return "，".join(
        f"{_STATUS_LABELS[status]} {sum(1 for item in items if item.get('status') == status)}"
        for status in ("completed", "in_progress", "pending")
    )


def render_update_result(items: list[dict[str, str]]) -> str:
    """update_todos 成功时给模型的结果。"""
    if not items:
        return f"{TODO_UPDATED_PREFIX}：清单已清空。"
    return "\n".join([f"{TODO_UPDATED_PREFIX}（共 {len(items)} 项：{_counts(items)}）", *render_todo_lines(items)])


def reminder_text(items: list[dict[str, str]]) -> str:
    """每步推理前提醒模型清单上还有没做完的项。"""
    pending = unfinished_items(items)
    return "\n".join([
        f"[待办提醒] 本任务的待办清单还有 {len(pending)} 项没完成：",
        *render_todo_lines(items),
        "开始做某一项时把它标成 in_progress，做完立刻用 update_todos 标成 completed；清单全部完成后再 finish。",
    ])


def unfinished_summary(items: list[dict[str, str]]) -> str:
    """收尾时列出没做完的待办，没有就给空串。"""
    pending = unfinished_items(items)
    if not pending:
        return ""
    return "\n".join(["还没完成的待办：", *render_todo_lines(pending)])


def _call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def latest_todos_from_messages(messages: list[dict[str, Any]], task_id: str) -> list[dict[str, str]] | None:
    """本任务最近一次成功的 update_todos 调用参数；找不到给 None。

    结果对得上调用 id 时只认成功的那次：被拒绝、没执行或参数不合格的调用都没改过清单。
    """
    if not task_id:
        return None
    results: dict[str, str] = {}
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id"):
            content = message.get("content")
            results[str(message["tool_call_id"])] = content if isinstance(content, str) else ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("_meta")
        if not isinstance(meta, dict) or str(meta.get("source_task_id") or "") != task_id:
            continue
        for call in reversed(message.get("tool_calls") or []):
            if not isinstance(call, dict) or call.get("name") != TODO_TOOL_NAME:
                continue
            result = results.get(str(call.get("id") or ""))
            if result is not None and not result.startswith(TODO_UPDATED_PREFIX):
                continue
            try:
                return parse_todo_items(_call_arguments(call).get("items"))
            except TodoListError:
                continue
    return None
