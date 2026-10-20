from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from engine.compact import DEFAULT_CHARS_PER_TOKEN, segments_within_token_budget


def record_dict(message: Any) -> dict[str, Any]:
    return message.to_dict() if hasattr(message, "to_dict") else dict(message)


def record_fingerprint(message: Any) -> str:
    value = json.dumps(record_dict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record_meta(message: Any) -> dict[str, Any]:
    record = record_dict(message)
    for key in ("meta", "_meta"):
        value = record.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def record_task_id(message: Any) -> str:
    record = record_dict(message)
    return str(record.get("source_task_id") or record_meta(message).get("source_task_id") or "")


def _segments(messages: list[Any]) -> tuple[list[Any], list[list[Any]]]:
    summaries: list[Any] = []
    segments: list[list[Any]] = []
    previous = ""
    for message in messages:
        record = record_dict(message)
        if record.get("role") == "system" or record.get("_dynamic") or record.get("_ephemeral") or record.get("ephemeral"):
            continue
        task_id = record_task_id(message)
        if task_id == "compact_summary":
            summaries.append(message)
            continue
        if not segments or task_id != previous or (not task_id and record.get("role") == "user" and not record.get("tool_call_id")):
            segments.append([])
        segments[-1].append(message)
        previous = task_id
    return summaries, _join_tool_segments(segments)


def _tool_call_ids(record: dict[str, Any]) -> list[str]:
    if record.get("role") != "assistant":
        return []
    calls = record.get("tool_calls")
    if isinstance(calls, list) and calls:
        return [str(call["id"]) for call in calls if isinstance(call, dict) and call.get("id")]
    values = record_meta(record).get("tool_call_ids")
    return [str(value) for value in values if value] if isinstance(values, list) else []


def _join_tool_segments(segments: list[list[Any]]) -> list[list[Any]]:
    if not segments:
        return segments
    pending: dict[str, int] = {}
    boundaries = [0] * (len(segments) + 1)
    for index, segment in enumerate(segments):
        for message in segment:
            record = record_dict(message)
            for call_id in _tool_call_ids(record):
                pending.setdefault(call_id, index)
            call_id = str(record.get("tool_call_id") or "")
            if call_id and call_id in pending:
                start = pending.pop(call_id)
                if start < index:
                    boundaries[start + 1] += 1
                    boundaries[index + 1] -= 1
    # 未完成的调用必须跟随最近段保留，结果可能在压缩期间追加。
    if pending:
        boundaries[min(pending.values()) + 1] += 1
    merged: list[list[Any]] = []
    active = 0
    for index, segment in enumerate(segments):
        active += boundaries[index]
        if not merged or not active:
            merged.append(list(segment))
        else:
            merged[-1].extend(segment)
    return merged


def _render(messages: list[Any]) -> str:
    # 保留完整记录，避免工具参数、时间或多模态引用在摘要输入里被截掉。
    return "\n".join(json.dumps(record_dict(message), ensure_ascii=False, default=str) for message in messages)


def build_compaction_plan(
    messages: list[Any], *, keep_recent: int = 6, keep_recent_tokens: int = 0,
    threshold_tokens: int = 0, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    max_chars: int = 300_000,
) -> tuple[dict[str, Any], str]:
    summaries, segments = _segments(messages)
    if keep_recent_tokens > 0:
        keep_recent = segments_within_token_budget(segments, keep_recent_tokens, chars_per_token)
    keep_recent = max(keep_recent, 1)
    selected = list(summaries)
    selected_chars = len(_render(selected))
    selected_count = 0
    partial_segment = False
    for segment in segments[:-keep_recent]:
        candidate_chars = selected_chars + len(_render(segment)) + bool(selected)
        if candidate_chars > max_chars:
            if not selected_count:
                for block in _join_tool_segments([[message] for message in segment]):
                    block_chars = selected_chars + len(_render(block)) + bool(selected)
                    if block_chars > max_chars:
                        break
                    selected.extend(block)
                    selected_chars = block_chars
                    selected_count = 1
                    partial_segment = True
            break
        selected.extend(segment)
        selected_chars = candidate_chars
        selected_count += 1
    if not selected_count:
        return {}, ""
    remaining = Counter(id(message) for message in selected)
    indexed = []
    for index, message in enumerate(messages):
        if remaining[id(message)]:
            indexed.append({"index": index, "fingerprint": record_fingerprint(message)})
            remaining[id(message)] -= 1
    selected = [messages[item["index"]] for item in indexed]
    selected_indices = {item["index"] for item in indexed}
    uncovered_tasks = {record_task_id(message) for index, message in enumerate(messages) if index not in selected_indices}
    plan = {
        "version": 1,
        "fingerprints": [item["fingerprint"] for item in indexed],
        "source_length": len(messages),
        "indexed_records": indexed,
        "total_segments": len(segments),
        "compressed_segments": selected_count,
        "partial_segment": partial_segment,
        "full_task_ids": [task_id for task_id in compressed_task_ids(selected) if task_id not in uncovered_tasks],
    }
    return plan, _render(selected)


def covered_indices(messages: list[Any], plan: dict[str, Any]) -> list[int] | None:
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1:
        return None
    wanted = plan.get("fingerprints")
    if not isinstance(wanted, list) or not wanted or not all(isinstance(value, str) for value in wanted):
        return None
    indexed = plan.get("indexed_records")
    source_length = plan.get("source_length")
    if not isinstance(indexed, list) or len(indexed) != len(wanted):
        return None
    if type(source_length) is not int or source_length < 1:
        return None
    indices = []
    for record, fingerprint in zip(indexed, wanted):
        if not isinstance(record, dict) or record.get("fingerprint") != fingerprint:
            return None
        index = record.get("index")
        if type(index) is not int or index < 0 or index >= min(source_length, len(messages)):
            return None
        if indices and index <= indices[-1]:
            return None
        if record_fingerprint(messages[index]) != fingerprint:
            return None
        indices.append(index)
    return indices


def replace_covered(messages: list[Any], plan: dict[str, Any], summary: Any) -> list[Any] | None:
    indices = covered_indices(messages, plan)
    if indices is None:
        return None
    selected = set(indices)
    result = []
    for index, message in enumerate(messages):
        if index == indices[0]:
            result.append(summary)
        if index not in selected:
            result.append(message)
    return result


def compressed_task_ids(messages: list[Any]) -> list[str]:
    result: set[str] = set()
    for message in messages:
        task_id = record_task_id(message)
        if task_id and task_id != "compact_summary":
            result.add(task_id)
        previous = record_meta(message).get("compressed_task_ids")
        if isinstance(previous, list):
            result.update(str(value) for value in previous if value)
    return sorted(result)


def fully_covered_task_ids(messages: list[Any], indices: list[int]) -> list[str]:
    selected = set(indices)
    remaining = {record_task_id(message) for index, message in enumerate(messages) if index not in selected}
    return [task_id for task_id in compressed_task_ids([messages[index] for index in indices]) if task_id not in remaining]


def archive_hint(archive_id: str) -> str:
    return f"\n\n[原始上下文归档：{archive_id}。需要细节时使用 search_context_archive 按时间或关键词检索，再用 read_context_archive 分页读取。]"
