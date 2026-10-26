"""从工具调用记录里认出读过哪些文件和行、改过哪些文件、跑过哪些命令。

请求视图里 read_file 结果的让位和任务进度摘要的操作清单都按这一份认。
"""
from __future__ import annotations

import json
import math
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from providers.base import content_text, tool_call_fields

from .compact import is_tool_result_message
from .tool_step import TOOL_SUCCESS

READ_TOOL = "read_file"
COMMAND_TOOL = "execute_command"
REWRITE_TOOLS = frozenset({"write_file", "apply_diff", "edit_lines", "undo_edits", "search_in_files"})
_SINGLE_PATH_REWRITES = frozenset({"write_file", "apply_diff", "edit_lines"})

# read_file 不带行范围时只显示开头这么多行
DEFAULT_READ_LINES = 500

_READ_HEADER = re.compile(r"^── (?P<path>.+?) ── (?P<rest>.+)$", re.MULTILINE)
_SHOWN_RANGE = re.compile(r"显示了?\s*(?P<first>\d+)-(?P<last>\d+) 行")
_WHOLE_FILE = re.compile(r"(?:共 \d+ 行$|空文件（0 行）|\[(?:image|binary):)")
_REPLACE_SUMMARY = re.compile(r"^\d+ files modified, \d+ replacements$")
_REPLACE_LINE = re.compile(r"^(?P<path>.+): \d+ replacements$")


@dataclass(frozen=True)
class Span:
    """文件里的一段行，last 为 math.inf 表示读到文件末尾。"""

    first: int
    last: float

    def covers(self, other: "Span") -> bool:
        return self.first <= other.first and self.last >= other.last


WHOLE_FILE = Span(1, math.inf)


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ReadEntry:
    """一次 read_file 里的一个文件。

    shown 是结果头里写明的实际显示范围，header_found 为假时结果里没找到这个文件的头
    （旧格式，或落盘预览把它省略掉了），只能按参数里要的范围算。
    """

    path: str
    raw_path: str
    requested: Span
    shown: Span | None
    header_found: bool

    @property
    def known_span(self) -> Span | None:
        """拿来判断它有没有被后来的读取覆盖时用的范围；None 表示这次没读到内容。"""
        return self.shown if self.header_found else self.requested


def message_meta(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    for key in ("_meta", "meta"):
        meta = message.get(key)
        if isinstance(meta, dict):
            return meta
    return {}


def tool_status(message: Any) -> str:
    return str(message_meta(message).get("tool_status") or "")


def call_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def tool_calls_of(message: Any) -> list[ToolCallRecord]:
    """assistant 消息里的工具调用，存储形状和 chat/completions 形状都认。"""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    records: list[ToolCallRecord] = []
    for call in calls:
        fields = tool_call_fields(call)
        if fields is None:
            continue
        call_id, name, arguments = fields
        records.append(ToolCallRecord(call_id=call_id, name=name, arguments=call_arguments(arguments)))
    return records


def resolve_tool_results(messages: list[Any]) -> dict[int, ToolCallRecord]:
    """工具结果下标 → 它回答的那次调用。

    同一个 id 取结果之前最近那条 assistant 里的调用：有的渠道每轮都从 call_0 重新编号。
    """
    latest: dict[str, ToolCallRecord] = {}
    resolved: dict[int, ToolCallRecord] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        for record in tool_calls_of(message):
            if record.call_id:
                latest[record.call_id] = record
        if not is_tool_result_message(message):
            continue
        record = latest.get(str(message.get("tool_call_id") or ""))
        if record is None:
            continue
        result_name = str(message.get("name") or "").strip()
        if result_name and result_name != record.name:
            continue
        resolved[index] = record
    return resolved


def _fold(text: str) -> str:
    return text.casefold() if os.name == "nt" else text


def normalize_path(raw: Any, workspace_root: Any = None) -> str:
    """路径比较用的形式：统一斜杠、去掉 ./ 和 ..，工作区里的绝对路径换成相对路径。"""
    text = _fold(str(raw or "").strip().replace("\\", "/"))
    if not text:
        return ""
    root = _fold(str(workspace_root or "").strip().replace("\\", "/").rstrip("/"))
    if root and text.startswith(root + "/"):
        text = text[len(root) + 1:]
    normalized = posixpath.normpath(text)
    return "" if normalized == "." else normalized


def _line_number(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _requested_span(start: Any, end: Any) -> Span:
    """和 read_file 的取法一致：只给 startLine 读到文件末尾，只给 endLine 从第 1 行起，都不给只读开头。"""
    first_raw = _line_number(start)
    last_raw = _line_number(end)
    if first_raw is None and last_raw is None:
        return Span(1, DEFAULT_READ_LINES)
    first = first_raw if first_raw is not None and first_raw > 0 else 1
    if last_raw is None or last_raw < first:
        return Span(first, math.inf)
    return Span(first, last_raw)


def read_requests(arguments: dict[str, Any]) -> list[tuple[str, Span]]:
    """read_file 参数里要读的 (路径原文, 行范围)，files 数组和旧的单文件写法都认。"""
    files = arguments.get("files")
    if isinstance(files, list) and files:
        requests: list[tuple[str, Span]] = []
        for item in files:
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            start = item.get("startLine") if item.get("startLine") is not None else item.get("start_line")
            end = item.get("endLine") if item.get("endLine") is not None else item.get("end_line")
            requests.append((path, _requested_span(start, end)))
        return requests
    path = str(arguments.get("path") or "").strip()
    if not path:
        return []
    return [(path, _requested_span(arguments.get("start_line"), arguments.get("end_line")))]


def _header_span(rest: str) -> Span | None:
    rest = rest.strip()
    if _WHOLE_FILE.match(rest):
        return WHOLE_FILE
    match = _SHOWN_RANGE.search(rest)
    if match is not None:
        return Span(int(match.group("first")), int(match.group("last")))
    return None


def read_entries(arguments: dict[str, Any], content: Any, *, workspace_root: Any = None) -> list[ReadEntry]:
    """一次 read_file 读了哪些文件、各自实际显示了哪些行。"""
    headers: dict[str, list[Span | None]] = {}
    for match in _READ_HEADER.finditer(content_text(content)):
        key = normalize_path(match.group("path"), workspace_root)
        headers.setdefault(key, []).append(_header_span(match.group("rest")))
    entries: list[ReadEntry] = []
    for raw_path, requested in read_requests(arguments):
        key = normalize_path(raw_path, workspace_root)
        queue = headers.get(key)
        if queue:
            entries.append(ReadEntry(key, raw_path, requested, queue.pop(0), True))
        else:
            entries.append(ReadEntry(key, raw_path, requested, None, False))
    return entries


def _replaced_files(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    if not lines or not _REPLACE_SUMMARY.match(lines[0]):
        return []
    return [match.group("path") for line in lines[1:] if (match := _REPLACE_LINE.match(line))]


def rewritten_paths(name: str, arguments: dict[str, Any], content: Any) -> list[str]:
    """改写类工具这次动了哪些文件（路径原文）。看不出来的情况返回空，宁可不让位也不错让。

    undo_edits 不点名 paths 时撤的是整个任务的改动，从参数看不出是哪些文件；
    search_in_files 的 replace 模式只能从结果里列出的文件认。
    """
    if name in _SINGLE_PATH_REWRITES:
        path = arguments.get("path")
        return [path.strip()] if isinstance(path, str) and path.strip() else []
    if name == "undo_edits":
        if str(arguments.get("action") or "undo").strip().lower() != "undo":
            return []
        paths = arguments.get("paths")
        if not isinstance(paths, list):
            return []
        return [path.strip() for path in paths if isinstance(path, str) and path.strip()]
    if name == "search_in_files":
        if str(arguments.get("mode") or "search").strip().lower() != "replace":
            return []
        return _replaced_files(content_text(content))
    return []


def command_text(arguments: dict[str, Any]) -> str:
    return str(arguments.get("command") or "").strip()


SUPERSEDED_READ_NOTE = "[这次读取的内容已省略：{paths} 之后又被读取或修改过，以后面的为准]"
NOTE_PATHS = 5


def superseded_note(paths: list[str], files: int | None = None) -> str:
    """让位说明：列出前 NOTE_PATHS 个路径，文件更多时补一句一共几个。"""
    total = max(files or 0, len(paths))
    shown = "、".join(paths[:NOTE_PATHS])
    if total > NOTE_PATHS:
        shown += f" 等 {total} 个文件"
    return SUPERSEDED_READ_NOTE.format(paths=shown)


@dataclass(frozen=True)
class Supersession:
    """一条 read_file 结果为什么过时：读过的路径原文（去重、按读取顺序）和让它完全过时的那条消息的下标。"""

    paths: tuple[str, ...]
    by: int

    @property
    def note(self) -> str:
        return superseded_note(list(self.paths))


def _superseded_at(
    index: int,
    entry: ReadEntry,
    later_reads: dict[str, list[tuple[int, Span]]],
    rewrites: dict[str, list[int]],
) -> int | None:
    """这个文件在 index 之后最早被哪条消息取代：成功改写过，或重读的行范围盖住了旧的。"""
    if not entry.path:
        return None
    earlier = entry.known_span
    candidates = [later for later in rewrites.get(entry.path, ()) if later > index]
    candidates.extend(
        later for later, span in later_reads.get(entry.path, ())
        if later > index and (earlier is None or span.covers(earlier))
    )
    return min(candidates) if candidates else None


def superseded_reads(messages: list[Any], *, workspace_root: Any = None) -> dict[int, Supersession]:
    """哪些 read_file 结果已经过时，下标 → 过时的原因。

    同一路径之后被成功改写过（以 meta 里的 tool_status 为准，没有这个字段的旧消息不算），
    或之后又读过且新读的行范围盖住了旧的，就算过时；一次读了多个文件的，每个文件都过时才算整条过时。
    """
    resolved = resolve_tool_results(messages)
    reads: list[tuple[int, list[ReadEntry]]] = []
    later_reads: dict[str, list[tuple[int, Span]]] = {}
    rewrites: dict[str, list[int]] = {}
    for index, record in sorted(resolved.items()):
        message = messages[index]
        content = message.get("content")
        if record.name == READ_TOOL:
            entries = read_entries(record.arguments, content, workspace_root=workspace_root)
            if not entries:
                continue
            reads.append((index, entries))
            for entry in entries:
                if entry.header_found and entry.shown is not None:
                    later_reads.setdefault(entry.path, []).append((index, entry.shown))
        elif record.name in REWRITE_TOOLS and tool_status(message) == TOOL_SUCCESS:
            for raw_path in rewritten_paths(record.name, record.arguments, content):
                path = normalize_path(raw_path, workspace_root)
                if path:
                    rewrites.setdefault(path, []).append(index)
    result: dict[int, Supersession] = {}
    for index, entries in reads:
        moments = [_superseded_at(index, entry, later_reads, rewrites) for entry in entries]
        if all(moment is not None for moment in moments):
            paths = tuple(dict.fromkeys(entry.raw_path for entry in entries))
            result[index] = Supersession(paths=paths, by=max(moments))
    return result


def superseded_read_notes(messages: list[Any], *, workspace_root: Any = None) -> dict[int, str]:
    """哪些 read_file 结果已经过时，下标 → 换上去的那行说明。"""
    return {
        index: supersession.note
        for index, supersession in superseded_reads(messages, workspace_root=workspace_root).items()
    }
