"""并发任务只提示存在，详情在用户询问时查询。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ACTIVE_TASKS_INPUT_KEY = "active_tasks_summary"
ACTIVE_TASKS_OMITTED_INPUT_KEY = "active_tasks_omitted"
CONCURRENT_CONTEXT_KEY = "concurrent_tasks"
CONCURRENT_OPEN = "[当前对话里还在跑的任务]"
CONCURRENT_CLOSE = "[/当前对话里还在跑的任务]"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def bridge_input(input_data: Mapping[str, Any]) -> dict[str, Any]:
    if input_data.get("resume_data"):
        return {}
    raw = input_data.get(ACTIVE_TASKS_INPUT_KEY)
    count = sum(1 for item in raw if isinstance(item, Mapping) and item.get("task_id")) if isinstance(raw, list) else 0
    if not count:
        return {}
    return {CONCURRENT_CONTEXT_KEY: {"count": count + _count(input_data.get(ACTIVE_TASKS_OMITTED_INPUT_KEY))}}


def concurrent_parts(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    count = _count(value.get("count"))
    # 旧快照的详情不再进入提示词。
    if not count and isinstance(value.get("tasks"), list):
        count = sum(1 for item in value["tasks"] if isinstance(item, Mapping) and item.get("task_id"))
    if not count:
        return []
    return ["\n".join([
        CONCURRENT_OPEN,
        "本对话还有其他任务。普通问候和无关消息照常回答，不主动汇报这些任务。",
        "只有用户询问进度、投递情况或追加要求时，调用 get_task_status 获取最新状态和目标编号。",
        CONCURRENT_CLOSE,
    ])]
