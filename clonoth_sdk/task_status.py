"""任务进度查询的回执与时间标注。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

STATUS_TOOL = "get_task_status"
STATUS_QUERY = "status_query_id"
MAX_STATUS_TASKS = 20
MAX_DELIVERY_RECORDS = 128


def read_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("任务状态回执不是对象")
    if not all(isinstance(value.get(key), str) and value[key] for key in ("query_id", "observed_at", "version")):
        raise ValueError("任务状态回执缺少查询编号、时间或版本")
    try:
        observed = datetime.fromisoformat(value["observed_at"])
    except ValueError as exc:
        raise ValueError("任务状态查询时间无效") from exc
    if observed.tzinfo is None:
        raise ValueError("任务状态查询时间缺少时区")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > MAX_STATUS_TASKS:
        raise ValueError("任务状态列表无效")
    for task in tasks:
        if not isinstance(task, dict) or not all(isinstance(task.get(key), str) and task[key] for key in ("task_id", "status")):
            raise ValueError("任务状态条目缺少编号或状态")
    return value


def dated_progress(text: str, observed_at: str) -> str:
    stamp = datetime.fromisoformat(observed_at).isoformat(sep=" ", timespec="seconds")
    prefix = f"（以下进度截至 {stamp} 查询时，之后可能变化）\n"
    return text if text.startswith(prefix) else prefix + text
