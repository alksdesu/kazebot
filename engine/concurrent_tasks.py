"""新消息进来时这段对话里还在跑的任务，并进那一轮的动态区：问进度直接答，补充要求追加给在跑的那个，无关的照常答。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# supervisor 入站时算好写进入口任务 input 的两项
ACTIVE_TASKS_INPUT_KEY = "active_tasks_summary"
ACTIVE_TASKS_OMITTED_INPUT_KEY = "active_tasks_omitted"
# runner 桥接进 task_context 的那一项：{"tasks": [...], "omitted": 没列出来的个数}
CONCURRENT_CONTEXT_KEY = "concurrent_tasks"

CONCURRENT_OPEN = "[当前对话里还在跑的任务]"
CONCURRENT_CLOSE = "[/当前对话里还在跑的任务]"

_PHASES = {
    "thinking": "在思考",
    "generating": "在写回复",
    "tool_call": "在调用工具",
    "awaiting_approval": "在等审批",
}
# 只有调工具、等审批时 detail 才是工具名
_PHASES_WITH_TOOL = frozenset({"tool_call", "awaiting_approval"})
_STATUSES = {"pending": "排队中", "running": "在跑", "suspended": "挂起等子任务"}

_GUIDE = (
    "这是收到这条消息那一刻的情况，之后可能已经变了。",
    "- 用户问进度、问怎么还没好：照上面说明在做什么、过了多久，不要把那件事再做一遍。",
    "- 用户这条是给那件事的补充或修改要求：用 preempt_task 追加给标着「可追加」的任务，task_id 填它的编号，"
    "再告诉用户已经追加进去；挂起等子任务的追加不了，追加给它下面在跑的子任务。"
    "追加没成功（已经结束、找不到）就自己处理这条消息。",
    "- 和那件事无关：照常回答，不用提它。",
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def bridge_input(input_data: Mapping[str, Any]) -> dict[str, Any]:
    """入口任务 input 里的摘要换成 task_context 的一项；没有在跑的任务时是空字典。

    挂起后恢复、重启后重排再跑的不给：那份摘要已经过时，这一轮也已经照它做过了。
    """
    if input_data.get("resume_data"):
        return {}
    raw = input_data.get(ACTIVE_TASKS_INPUT_KEY)
    tasks = [dict(item) for item in raw if isinstance(item, Mapping) and _text(item.get("task_id"))] if isinstance(raw, list) else []
    if not tasks:
        return {}
    value: dict[str, Any] = {"tasks": tasks}
    omitted = _count(input_data.get(ACTIVE_TASKS_OMITTED_INPUT_KEY))
    if omitted:
        value["omitted"] = omitted
    return {CONCURRENT_CONTEXT_KEY: value}


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def _doing(item: Mapping[str, Any]) -> str:
    status = _text(item.get("status"))
    phase = _text(item.get("phase"))
    if status == "running" and phase in _PHASES:
        detail = _text(item.get("detail"))
        return f"{_PHASES[phase]} {detail}" if detail and phase in _PHASES_WITH_TOOL else _PHASES[phase]
    return _STATUSES.get(status, status or "在跑")


def _item_line(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    instruction = _text(item.get("instruction"))
    if instruction:
        parts.append(f"「{instruction}」")
    parts.append(_text(item.get("node_id")) or "任务")
    parts.append(_doing(item))
    age = _count(item.get("age_sec"))
    if age is not None:
        parts.append(f"{'已等' if _text(item.get('status')) == 'pending' else '已跑'} {_duration(age)}")
    if item.get("mine") is False:
        parts.append("不是你发起的")
    if item.get("steerable") is True:
        parts.append(f"可追加，编号 {_text(item.get('task_id'))}")
    return "｜".join(parts)


def _tree_lines(items: list[Mapping[str, Any]]) -> list[str]:
    """派出去的子任务缩进挂在派它的任务下面；上级不在列表里的当顶层。"""
    ids = {_text(item.get("task_id")) for item in items}
    children: dict[str, list[Mapping[str, Any]]] = {}
    roots: list[Mapping[str, Any]] = []
    for item in items:
        task_id = _text(item.get("task_id"))
        parent = _text(item.get("parent_task_id"))
        if parent and parent != task_id and parent in ids:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)
    lines: list[str] = []
    seen: set[str] = set()

    def walk(item: Mapping[str, Any], depth: int) -> None:
        task_id = _text(item.get("task_id"))
        if task_id in seen:
            return
        seen.add(task_id)
        lines.append(f"{'  ' * depth}- {_item_line(item)}")
        for child in children.get(task_id, []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    # 上级链成环、谁也挂不上的，照原顺序补在最后，不丢
    for item in items:
        walk(item, 0)
    return lines


def concurrent_parts(value: Any) -> list[str]:
    """task_context 里那一项渲染成动态区的一段；没有就是空列表，提示词一字不变。"""
    if not isinstance(value, Mapping):
        return []
    raw = value.get("tasks")
    items = [item for item in raw if isinstance(item, Mapping) and _text(item.get("task_id"))] if isinstance(raw, list) else []
    if not items:
        return []
    lines = [CONCURRENT_OPEN, *_tree_lines(items)]
    omitted = _count(value.get("omitted"))
    if omitted:
        lines.append(f"（还有 {omitted} 个没列出来）")
    lines += [*_GUIDE, CONCURRENT_CLOSE]
    return ["\n".join(lines)]
