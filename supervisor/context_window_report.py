"""get_context_window 报告的压缩阈值以 engine 随 context_usage 上报的为准：那是按模型窗口收紧后真正生效的一份。"""
from __future__ import annotations

from typing import Any

ENGINE_THRESHOLD_KEYS = ("compact_threshold", "compact_hard_threshold", "context_window")


def engine_thresholds(payload: Any) -> dict[str, int]:
    """context_usage 事件里 engine 报的有效阈值，只收正整数。"""
    if not isinstance(payload, dict):
        return {}
    values: dict[str, int] = {}
    for key in ENGINE_THRESHOLD_KEYS:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            values[key] = value
    return values


def remember_engine_thresholds(state: Any, session_id: str, payload: Any) -> None:
    """把有效阈值记进这个会话最近一次的用量记录：记录随会话清理一起删，阈值不会比用量活得久。"""
    values = engine_thresholds(payload)
    if not session_id:
        return
    with state._lock:
        entry = state._session_context_usage.get(session_id)
        if not isinstance(entry, dict):
            return
        for key in ENGINE_THRESHOLD_KEYS:
            entry.pop(key, None)
        entry.update(values)


def with_engine_thresholds(report: dict[str, Any], recorded: Any) -> dict[str, Any]:
    """用 engine 报过的有效阈值改写 get_session_context_usage 的结果；没报过就注明阈值来自 runtime 配置。"""
    values = engine_thresholds(recorded)
    result = dict(report)
    result["compact_hard_threshold"] = values.get("compact_hard_threshold")
    result["context_window"] = values.get("context_window")
    threshold = values.get("compact_threshold")
    if threshold is None:
        result["threshold_source"] = "runtime_config"
        return result
    result["threshold_source"] = "engine"
    result["compact_threshold"] = threshold
    effective = result.get("effective_tokens")
    if isinstance(effective, (int, float)) and not isinstance(effective, bool):
        result["utilization"] = round(effective / threshold, 4)
    return result
