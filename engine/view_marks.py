"""旧工具结果的截短、让位、清空：在批量时刻算好标记写进消息 meta 并落盘，原文不动，请求视图按标记渲染。

标记只往前推，已有的不会被撤掉或换成更弱的；当前任务自己的消息不打标记。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from clonoth_runtime import get_bool, get_float, get_int
from providers.base import content_text

from .compact import (
    DEFAULT_CHARS_PER_TOKEN,
    MARK_CLEARED,
    MARK_SUPERSEDED,
    MARK_TRUNCATED,
    TOOL_RESULT_VIEW_CHARS,
    VIEW_MARK_KEY,
    bounded_content,
    cleared_tool_result,
    is_tool_result_message,
    parse_view_mark,
    view_mark_of,
)
from .compaction_plan import record_task_id
from .tool_history import NOTE_PATHS, superseded_note, superseded_reads
from .tool_outputs import excerpt_text

# 小窗口模型上配置的保护预算可能比整个窗口还大，最多只占软阈值的这个百分比
_PROTECT_PERCENT_OF_THRESHOLD = 40
_STRENGTH = {MARK_TRUNCATED: 1, MARK_SUPERSEDED: 2, MARK_CLEARED: 3}
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
MAX_MARK_REQUESTS = 10_000


def protect_tokens(runtime_cfg: Any, compact_threshold: int) -> int:
    """从最新往前保留原文的工具结果预算：engine.compact.tool_result_protect_tokens，最多占软阈值的 40%。"""
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    protect = get_int(cfg, "engine.compact.tool_result_protect_tokens", 40_000, min_value=0, max_value=2_000_000)
    if compact_threshold > 0:
        protect = min(protect, compact_threshold * _PROTECT_PERCENT_OF_THRESHOLD // 100)
    return protect


@dataclass(frozen=True)
class MarkSettings:
    """一次批量打标记用的参数。

    保护窗外还没打标记的旧结果越过 protect_tokens × trigger_ratio 才推一次，一推推到 protect_tokens × low_ratio。
    """

    protect_tokens: int = 0
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
    supersede: bool = True
    workspace_root: Any = None
    trigger_ratio: float = 1.5
    low_ratio: float = 1.0
    truncate_chars: int = TOOL_RESULT_VIEW_CHARS


def mark_settings(
    runtime_cfg: Any, *, compact_threshold: int, chars_per_token: float, workspace_root: Any = None,
) -> MarkSettings:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    trigger = get_float(cfg, "engine.compact.tool_result_mark_trigger_ratio", 1.5, min_value=1.0, max_value=10.0)
    low = get_float(cfg, "engine.compact.tool_result_mark_low_ratio", 1.0, min_value=0.0, max_value=10.0)
    return MarkSettings(
        protect_tokens=protect_tokens(cfg, compact_threshold),
        chars_per_token=chars_per_token if chars_per_token > 0 else DEFAULT_CHARS_PER_TOKEN,
        supersede=get_bool(cfg, "engine.compact.supersede_reads", True),
        workspace_root=workspace_root,
        trigger_ratio=trigger,
        low_ratio=min(low, trigger),
    )


def mark_strength(mark: Mapping[str, Any] | None) -> int:
    """清空 > 让位 > 截短 > 没有标记。"""
    return _STRENGTH.get(str(mark.get("kind") or ""), 0) if mark else 0


def stronger(mark: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> bool:
    return mark_strength(mark) > mark_strength(current)


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def message_id_of(message: Any) -> str:
    """运行期消息的 id 在 _meta.message_id（从存储重载时带上的），存储里的记录就是它自己的 id。"""
    if isinstance(message, dict):
        meta = message.get("_meta")
        if isinstance(meta, dict) and meta.get("message_id"):
            return str(meta["message_id"])
        return str(message.get("id") or "")
    return str(getattr(message, "id", "") or "")


def mark_fingerprint(message: Any) -> str:
    """认同一条工具结果的指纹：只看 role、正文、调用 id 和工具名，运行期 dict 和存储里的 Message 算出来一样。"""
    record = {
        "role": str(_field(message, "role") or ""),
        "content": _field(message, "content"),
        "tool_call_id": str(_field(message, "tool_call_id") or ""),
        "name": str(_field(message, "name") or ""),
    }
    value = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def render_marked_content(content: Any, mark: Mapping[str, Any]) -> Any:
    kind = mark.get("kind")
    if kind == MARK_CLEARED:
        return cleared_tool_result(content if isinstance(content, str) else content_text(content))
    if kind == MARK_SUPERSEDED:
        return superseded_note(list(mark.get("paths") or []), mark.get("files"))
    if kind == MARK_TRUNCATED:
        chars = int(mark.get("chars") or TOOL_RESULT_VIEW_CHARS)
        return excerpt_text(content, chars) if isinstance(content, str) else bounded_content(content, chars)
    return content


def render_view_marks(messages: list[Any]) -> list[Any]:
    """和 messages 逐条对应的请求视图：带标记的工具结果按标记渲染，其余照原文；返回浅拷贝，原消息不动。"""
    view: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            view.append(message)
            continue
        shown = dict(message)
        if is_tool_result_message(message):
            mark = view_mark_of(message)
            if mark is not None:
                shown["content"] = render_marked_content(message.get("content"), mark)
        view.append(shown)
    return view


@dataclass(frozen=True)
class PlannedMark:
    index: int
    message_id: str
    fingerprint: str
    mark: dict[str, Any]


def _markable(message: Any, excluded: set[str]) -> bool:
    if not isinstance(message, dict) or message.get("_dynamic") or message.get("_ephemeral"):
        return False
    if message.get("role") == "system" or not is_tool_result_message(message):
        return False
    return isinstance(message.get("content"), str) and record_task_id(message) not in excluded


def _superseded_mark(paths: tuple[str, ...], by_message: Any) -> dict[str, Any]:
    mark: dict[str, Any] = {"kind": MARK_SUPERSEDED, "paths": list(paths[:NOTE_PATHS]), "files": len(paths)}
    by = message_id_of(by_message)
    if by:
        mark["by"] = by
    return mark


def plan_view_marks(
    messages: list[Any],
    *,
    settings: MarkSettings,
    exclude_task_ids: Iterable[str] = (),
    force: bool = False,
    cleared: Iterable[int] = (),
) -> list[PlannedMark]:
    """这次批量该补哪些标记。

    从最新的旧工具结果往前按渲染后的字数估 token。不带 force 时，保护窗外还没打标记、截得短的结果
    越过 保护预算 × trigger_ratio 才推一次；推的时候 保护预算 × low_ratio 之外的都截短，从这一批最早
    那条往后、读过又被重读或改写过的 read_file 结果换成让位说明。force 用在压缩、空闲这种整段缓存本来
    就不命中的时刻，不看滞回、也不限位置。cleared 是空闲清理挑出来的下标，一律标成已清空。
    """
    excluded = {str(task_id) for task_id in exclude_task_ids if task_id}
    candidates = [index for index, message in enumerate(messages) if _markable(message, excluded)]
    if not candidates:
        return []
    existing = {index: view_mark_of(messages[index]) for index in candidates}
    planned: dict[int, dict[str, Any]] = {}
    for index in cleared:
        if index in existing and stronger({"kind": MARK_CLEARED}, existing[index]):
            planned[index] = {"kind": MARK_CLEARED}

    divisor = settings.chars_per_token if settings.chars_per_token > 0 else DEFAULT_CHARS_PER_TOKEN
    depth: dict[int, float] = {}
    used = 0.0
    for index in reversed(candidates):
        mark = planned.get(index) or existing[index]
        content = messages[index]["content"]
        shown = render_marked_content(content, mark) if mark is not None else content
        used += len(shown if isinstance(shown, str) else content_text(shown)) / divisor
        depth[index] = used

    limit = settings.truncate_chars
    truncatable = [
        index for index in candidates
        if existing[index] is None and index not in planned and len(messages[index]["content"]) > limit
    ]
    beyond_low = {index for index in truncatable if depth[index] > settings.protect_tokens * settings.low_ratio}
    if force:
        start = 0
    else:
        high = settings.protect_tokens * settings.trigger_ratio
        if not any(depth[index] > high for index in truncatable):
            return _planned(messages, planned)
        start = min(beyond_low)

    supersessions = (
        superseded_reads(messages, workspace_root=settings.workspace_root) if settings.supersede else {}
    )
    for index in candidates:
        if index < start or index in planned or existing[index] is not None:
            continue
        supersession = supersessions.get(index)
        if supersession is not None:
            planned[index] = _superseded_mark(supersession.paths, messages[supersession.by])
        elif index in beyond_low:
            planned[index] = {"kind": MARK_TRUNCATED, "chars": limit}
    return _planned(messages, planned)


def _planned(messages: list[Any], planned: dict[int, dict[str, Any]]) -> list[PlannedMark]:
    return [
        PlannedMark(
            index=index, message_id=message_id_of(messages[index]),
            fingerprint=mark_fingerprint(messages[index]), mark=planned[index],
        )
        for index in sorted(planned)
    ]


def keyed_marks(planned: Iterable[PlannedMark]) -> dict[str, tuple[str, dict[str, Any]]]:
    """按消息 id 排好的 (指纹, 标记)，落盘和同步分支都按这一份认；没有 id 的落不了盘，不在里面。"""
    return {item.message_id: (item.fingerprint, dict(item.mark)) for item in planned if item.message_id}


def parse_mark_requests(raw: Any) -> dict[str, tuple[str, dict[str, Any]]] | None:
    """接口收到的标记清单换成 keyed_marks 的形状；有一条不合法或 id 重复就整份不收，返回 None。"""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MARK_REQUESTS:
        return None
    marks: dict[str, tuple[str, dict[str, Any]]] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        message_id = item.get("message_id")
        fingerprint = item.get("fingerprint")
        mark = parse_view_mark(item.get("mark"))
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 256:
            return None
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            return None
        if mark is None or message_id in marks:
            return None
        marks[message_id] = (fingerprint, mark)
    return marks


def with_view_mark(message: Any, mark: Mapping[str, Any]) -> Any:
    """带上这个标记的新消息，原消息不动。"""
    if isinstance(message, dict):
        key = "meta" if "meta" in message and "_meta" not in message else "_meta"
        meta = message.get(key) if isinstance(message.get(key), dict) else {}
        return {**message, key: {**meta, VIEW_MARK_KEY: dict(mark)}}
    meta = getattr(message, "meta", None)
    return replace(message, meta={**(meta if isinstance(meta, dict) else {}), VIEW_MARK_KEY: dict(mark)})


@dataclass(frozen=True)
class MarkMerge:
    records: list[Any]
    # 这些 id 合并后生效的标记：新补上的，或者本来就有、不比要补的弱的
    effective: dict[str, dict[str, Any]]
    changed: int
    # id 对上了、正文却对不上的消息
    mismatched: frozenset[str]


def merge_view_marks(records: list[Any], marks: Mapping[str, tuple[str, Mapping[str, Any]]]) -> MarkMerge:
    """把标记合进一份消息：按 id 找，指纹对上、比已有的更强才换。records 本身不动。"""
    result = list(records)
    effective: dict[str, dict[str, Any]] = {}
    mismatched: set[str] = set()
    changed = 0
    for index, record in enumerate(records):
        message_id = message_id_of(record)
        wanted = marks.get(message_id) if message_id else None
        if wanted is None:
            continue
        fingerprint, mark = wanted
        if mark_fingerprint(record) != fingerprint:
            mismatched.add(message_id)
            continue
        current = view_mark_of(record)
        if stronger(mark, current):
            result[index] = with_view_mark(record, mark)
            effective[message_id] = dict(mark)
            changed += 1
        elif current is not None:
            effective[message_id] = current
    return MarkMerge(records=result, effective=effective, changed=changed, mismatched=frozenset(mismatched))


def marked_prefix(prefix: list[Any], marks: Mapping[str, tuple[str, Mapping[str, Any]]]) -> list[Any] | None:
    """入口分支的父历史前缀补上同一批标记；有一条 id 对上而正文对不上就整段不动，返回 None。"""
    merged = merge_view_marks(prefix, marks)
    if merged.mismatched:
        return None
    return merged.records


def apply_view_marks_to_store(
    store: Any, session_id: str, marks: Mapping[str, tuple[str, Mapping[str, Any]]], *, attempts: int = 3,
) -> dict[str, dict[str, Any]] | None:
    """把标记合进会话文件，文件被并发改动时重读重试；返回这些 id 现在生效的标记，重试用完返回 None。阻塞 I/O。"""
    from .conversation_store import ConversationChangedError

    for _ in range(max(1, attempts)):
        merged = merge_view_marks(store.load(session_id), marks)
        if not merged.changed:
            return merged.effective
        try:
            store.replace_all(session_id, merged.records)
        except ConversationChangedError:
            continue
        return merged.effective
    return None


def with_effective_marks(
    messages: list[Any],
    marks: Mapping[str, tuple[str, Mapping[str, Any]]],
    effective: Mapping[str, Mapping[str, Any]],
) -> tuple[list[Any], int]:
    """把已经落盘的标记同步到运行期消息：按 message_id 找，指纹对得上、比现有的强才换，返回 (新列表, 换了几条)。"""
    result = list(messages)
    changed = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        message_id = message_id_of(message)
        mark = effective.get(message_id) if message_id else None
        expected = marks.get(message_id) if message_id else None
        if mark is None or expected is None or mark_fingerprint(message) != expected[0]:
            continue
        if not stronger(mark, view_mark_of(message)):
            continue
        result[index] = with_view_mark(message, mark)
        changed += 1
    return result, changed


def with_planned_marks(messages: list[Any], planned: Iterable[PlannedMark]) -> tuple[list[Any], int]:
    """没有会话存储时（旧的快照路径）按下标直接把标记打在运行期消息上，随快照落盘。"""
    result = list(messages)
    changed = 0
    for item in planned:
        if not 0 <= item.index < len(result):
            continue
        message = result[item.index]
        if not isinstance(message, dict) or mark_fingerprint(message) != item.fingerprint:
            continue
        if stronger(item.mark, view_mark_of(message)):
            result[item.index] = with_view_mark(message, item.mark)
            changed += 1
    return result, changed


def mark_counts(marks: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {MARK_TRUNCATED: 0, MARK_SUPERSEDED: 0, MARK_CLEARED: 0}
    for mark in marks:
        kind = str(mark.get("kind") or "")
        if kind in counts:
            counts[kind] += 1
    return counts
