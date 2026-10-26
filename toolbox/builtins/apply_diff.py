"""apply_diff — apply sequential search/replace diffs to a file (policy + approval guarded).

All diffs are matched in memory first (see ``toolbox.edit_match``); the file is written only
when every one of them applies. diffs[1] operates on the result of diffs[0], and so on."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import file_state
from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots
from ..edit_match import DiffOutcome, DiffRequest, FailedDiff, apply_diffs, line_range
from ..syntax_check import EditChecks, run_edit_checks
from ..text_files import TextLayout, decode_text, encode_text, normalize_newlines, split_lines
from .read_file import number_prefix, render_lines


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = 1024 * 1024 * 64  # 64 MiB — generous but bounded
ECHO_CONTEXT_LINES = 2
ECHO_MAX_CHARS = 6000
_ECHO_REGION_MAX_LINES = 40
_ECHO_REGION_HEAD_LINES = 25
_ECHO_REGION_TAIL_LINES = 10
_ECHO_LINE_CHARS = 500
_SNIPPET_MAX_LINES = 12
_SNIPPET_LINE_CHARS = 300


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _apply_diff_error_response(error: Any, path: str = "", **data_fields: Any) -> dict[str, Any]:
    # Every failure carries ok=false and data.result; data_fields may override data.result with a fuller report.
    text = str(error)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    if path:
        data["file"] = path
    data.update(data_fields)
    return {"ok": False, "success": False, "error": text, "data": data}


def _start_line(raw: Any) -> int | None:
    """``start_line`` as an int; raises ValueError for anything that is not a whole number."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(raw)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(raw) from exc


def _parse_diffs(diffs: list[Any]) -> tuple[list[DiffRequest], list[tuple[int, str]]]:
    requests: list[DiffRequest] = []
    problems: list[tuple[int, str]] = []
    for index, entry in enumerate(diffs):
        if not isinstance(entry, dict):
            problems.append((index, "diff 必须是对象，含 search 和 replace"))
            continue
        search = entry.get("search")
        replace = entry.get("replace")
        if search is None:
            problems.append((index, "缺少 search"))
            continue
        if replace is None:
            problems.append((index, "缺少 replace"))
            continue
        search_text = search if isinstance(search, str) else str(search)
        if search_text == "":
            problems.append((index, "search 是空的；要在文件里插入内容，就把插入点附近的一段原文连同新内容一起写进 replace"))
            continue
        try:
            start_line = _start_line(entry.get("start_line"))
        except ValueError:
            problems.append((index, f"start_line 必须是整数，收到 {type(entry.get('start_line')).__name__}"))
            continue
        requests.append(DiffRequest(index, search_text, replace if isinstance(replace, str) else str(replace), start_line))
    return requests, problems


@dataclass(frozen=True)
class _Transaction:
    """status: written / unchanged / failed / stale / missing / not_text / too_large."""

    status: str
    gate: file_state.Gate
    outcome: DiffOutcome | None = None
    stamp: file_state.Stamp | None = None
    byte_count: int = 0
    size: int = 0


def _transact(session: str, resolved: Path, requests: list[DiffRequest]) -> _Transaction:
    """Read, check the stamp, apply every diff in memory and write only if all of them applied."""
    with file_state.edit_lock(resolved):
        try:
            size = os.stat(resolved).st_size
        except FileNotFoundError:
            return _Transaction("missing", file_state.Gate(None))
        if size > _MAX_FILE_SIZE:
            return _Transaction("too_large", file_state.Gate(None), size=size)
        snapshot = file_state.take_snapshot(resolved)
        if not snapshot.exists:
            return _Transaction("missing", file_state.Gate(None))
        gate = file_state.inspect(session, resolved, snapshot)
        if gate.stale:
            return _Transaction("stale", gate)
        if len(snapshot.data) > _MAX_FILE_SIZE:
            return _Transaction("too_large", gate, size=len(snapshot.data))
        try:
            text = decode_text(snapshot.data)
        except UnicodeDecodeError:
            return _Transaction("not_text", gate)
        outcome = apply_diffs(normalize_newlines(text), requests)
        if not outcome.ok:
            return _Transaction("failed", gate, outcome)
        if not outcome.changed:
            return _Transaction("unchanged", gate, outcome)
        data = encode_text(outcome.content, TextLayout.of_bytes(snapshot.data))
        stamp = file_state.commit_locked(resolved, data, expected=snapshot)
        file_state.remember(session, resolved, stamp, revealable=gate.revealable)
        return _Transaction("written", gate, outcome, stamp, len(data))


def _closest_lines(failed: FailedDiff) -> list[str]:
    closest = failed.failure.closest
    if closest is None:
        return []
    # Shown in the numbering start_line uses: the file as read, before the earlier diffs of this call.
    first_shown = closest.first + 1 - failed.line_delta
    last_shown = closest.last + 1 - failed.line_delta
    out = [f"  最接近的是第 {first_shown}-{last_shown} 行（相似度 {closest.similarity:.2f}）："]
    window = split_lines(failed.content)[closest.first:closest.last + 1]
    prefix = number_prefix(max(4, len(str(last_shown))))
    rendered = render_lines(window[:_SNIPPET_MAX_LINES], first_shown, prefix, limit=_SNIPPET_LINE_CHARS)
    out.extend("    " + line for line, _clipped in rendered)
    if len(window) > _SNIPPET_MAX_LINES:
        out.append(f"    …（还有 {len(window) - _SNIPPET_MAX_LINES} 行没有列出）")
    return out


def _failure_response(path_str: str, outcome: DiffOutcome, diff_count: int, *, reveal: bool) -> dict[str, Any]:
    failed = sorted(outcome.failed, key=lambda item: item.index)
    first_reason = failed[0].failure.message
    error = f"{len(failed)} 个 diff 没能应用，文件没有改动：diffs[{failed[0].index}] {first_reason}"
    lines = [
        f"ERROR: apply_diff 没有修改 {path_str}：{diff_count} 个 diff 中有 {len(failed)} 个没能应用，文件保持原样。"
        "修正后把全部 diff 一起重新提交。",
    ]
    failed_entries: list[dict[str, Any]] = []
    for item in failed:
        failure = item.failure
        lines.append(f"diffs[{item.index}] 没能应用：{failure.message}")
        entry: dict[str, Any] = {"index": item.index, "reason": failure.reason, "error": failure.message}
        if failure.level is not None:
            entry["level"] = failure.level.key
        if failure.via is not None:
            entry["via"] = failure.via.key
        if failure.candidate_lines:
            entry["candidateLines"] = list(failure.candidate_lines)
        if failure.total:
            entry["totalMatches"] = failure.total
        if failure.closest is not None:
            if reveal:
                lines.extend(_closest_lines(item))
                entry["closest"] = {
                    "startLine": failure.closest.first + 1 - item.line_delta,
                    "endLine": failure.closest.last + 1 - item.line_delta,
                    "similarity": round(failure.closest.similarity, 2),
                }
            else:
                lines.append("  （本会话没有读过这个文件的当前版本，不提供相近的片段；先 read_file 再改）")
        failed_entries.append(entry)
    matched_entries = []
    for applied in sorted(outcome.applied, key=lambda item: item.index):
        lines.append(f"diffs[{applied.index}] 可以应用（{applied.match.label}），因为有 diff 失败没有写入")
        matched_entries.append({"index": applied.index, "level": applied.match.level.key})
    return _apply_diff_error_response(
        error,
        path_str,
        result="\n".join(lines),
        message=f"{path_str} 没有改动：{len(failed)} 个 diff 没能应用",
        failedDiffs=failed_entries,
        matchedDiffs=matched_entries,
        appliedCount=0,
        matchedCount=len(matched_entries),
        failedCount=len(failed_entries),
        diffCount=diff_count,
        totalCount=diff_count,
    )


def _diff_summaries(outcome: DiffOutcome) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    entries: list[dict[str, Any]] = []
    for applied in sorted(outcome.applied, key=lambda item: item.index):
        match = applied.match
        start, end = outcome.spans.get(applied.index, (match.start, match.start + len(match.replacement)))
        first, last = line_range(outcome.content, start, end)
        if start == end:
            where = f"删掉了内容，原处在改后的第 {first} 行"
        elif first == last:
            where = f"改动后在第 {first} 行"
        else:
            where = f"改动后在第 {first}-{last} 行"
        text = f"diffs[{applied.index}] {match.label}，{where}"
        if match.notes:
            text += "；" + "；".join(match.notes)
        lines.append(text)
        entry: dict[str, Any] = {
            "index": applied.index, "level": match.level.key, "label": match.label,
            "startLine": first, "endLine": last,
        }
        if match.via is not None:
            entry["via"] = match.via.key
        if match.notes:
            entry["notes"] = list(match.notes)
        if match.similarity is not None:
            entry["similarity"] = round(match.similarity, 2)
        entries.append(entry)
    return lines, entries


def _echo_regions(outcome: DiffOutcome, total_lines: int) -> list[tuple[int, int, list[int]]]:
    """Line windows (1-based, inclusive) around every edit, merged when they touch."""
    regions: list[tuple[int, int, list[int]]] = []
    for index, (start, end) in sorted(outcome.spans.items(), key=lambda item: item[1]):
        first, last = line_range(outcome.content, start, end)
        low = max(1, first - ECHO_CONTEXT_LINES)
        high = min(total_lines, last + ECHO_CONTEXT_LINES)
        if regions and low <= regions[-1][1] + 1:
            prev_low, prev_high, indexes = regions[-1]
            regions[-1] = (prev_low, max(prev_high, high), indexes + [index])
        else:
            regions.append((low, high, [index]))
    return regions


def _region_block(lines: list[str], low: int, high: int, indexes: list[int]) -> list[str]:
    names = "、".join(f"diffs[{index}]" for index in sorted(indexes))
    block = [f"── 第 {low}-{high} 行（{names}）──"]
    numbers = list(range(low, high + 1))
    if len(numbers) > _ECHO_REGION_MAX_LINES:
        skipped = len(numbers) - _ECHO_REGION_HEAD_LINES - _ECHO_REGION_TAIL_LINES
        numbers = numbers[:_ECHO_REGION_HEAD_LINES] + [0] + numbers[-_ECHO_REGION_TAIL_LINES:]
    else:
        skipped = 0
    prefix = number_prefix(max(4, len(str(high))))
    for line_no in numbers:
        if line_no == 0:
            block.append(f"…（中间 {skipped} 行没有列出）")
            continue
        rendered, _clipped = render_lines([lines[line_no - 1]], line_no, prefix, limit=_ECHO_LINE_CHARS)[0]
        block.append(rendered)
    return block


def _echo_lines(outcome: DiffOutcome) -> list[str]:
    lines = split_lines(outcome.content)
    if not lines:
        return ["改动后文件是空的。"]
    out = [f"改动后的内容（按改后的行号，前后各带 {ECHO_CONTEXT_LINES} 行）："]
    used = 0
    regions = [region for region in _echo_regions(outcome, len(lines)) if region[0] <= region[1]]
    for position, (low, high, indexes) in enumerate(regions):
        for line in _region_block(lines, low, high, indexes):
            if used + len(line) + 1 > ECHO_MAX_CHARS:
                rest = regions[position:]
                names = "、".join(f"diffs[{index}]" for _low, _high, idx in rest for index in sorted(idx))
                spans = "、".join(f"{rest_low}-{rest_high}" for rest_low, rest_high, _idx in rest)
                out.append(
                    f"…回显超过 {ECHO_MAX_CHARS} 字符已截断：{names} 的改动没有完整显示，需要时用 read_file 看第 {spans} 行。"
                )
                return out
            out.append(line)
            used += len(line) + 1
    return out


def _success_response(
    path_str: str,
    outcome: DiffOutcome,
    diff_count: int,
    *,
    written: bool,
    byte_count: int,
    reveal: bool,
    checks: EditChecks | None,
) -> dict[str, Any]:
    summary_lines, entries = _diff_summaries(outcome)
    if written:
        head = f"已修改 {path_str}：{diff_count} 个 diff 全部应用并保存（{byte_count} 字节）。"
    else:
        head = f"{path_str} 没有变化：{diff_count} 个 diff 都匹配上了，但应用后的内容和原文件相同，没有写盘。"
    lines = [head, *summary_lines]
    if written:
        lines.extend(
            _echo_lines(outcome) if reveal
            else ["（本会话没有读过这个文件的当前版本，不回显改动处的原文；需要时用 read_file 查看上面列出的行）"]
        )
    if checks is not None:
        lines.extend(checks.lines())
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "file": path_str,
        "message": head,
        "status": "accepted",
        "diffCount": diff_count,
        "appliedCount": diff_count,
        "failedCount": 0,
        "changed": written,
        "diffs": entries,
        "echoed": bool(written and reveal),
    }
    if written:
        data["bytes"] = byte_count
    if checks is not None:
        data.update(checks.to_data())
    return {"ok": True, "success": True, "data": data}


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------


async def apply_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path_str = str(args.get("path") or "").strip()
    diffs = args.get("diffs")

    # ------------------------------------------------------------------
    #  Basic validation (before the guard: a malformed call should not raise an approval)
    # ------------------------------------------------------------------
    if not path_str:
        return _apply_diff_error_response("'path' is required.")

    if not isinstance(diffs, list) or len(diffs) == 0:
        return _apply_diff_error_response("'diffs' must be a non-empty array.", path_str)

    requests, problems = _parse_diffs(diffs)
    if problems:
        detail = "；".join(f"diffs[{index}] {problem}" for index, problem in problems)
        return _apply_diff_error_response(
            f"diff 参数有误，文件没有改动：{detail}",
            path_str,
            failedDiffs=[{"index": index, "reason": "invalid", "error": problem} for index, problem in problems],
            appliedCount=0,
            failedCount=len(problems),
            diffCount=len(diffs),
            totalCount=len(diffs),
        )

    # ------------------------------------------------------------------
    #  Policy + approval guard (same flow as write_file)
    # ------------------------------------------------------------------
    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path_str,
            "content_preview": f"apply_diff: {len(diffs)} diff(s)",
            "content_len": 0,
            "tool_name": "apply_diff",
        },
    )
    if err is not None:
        cancelled = err.get("cancelled", False)
        if cancelled:
            response = _apply_diff_error_response(
                "Diff was cancelled by user",
                path_str,
                message=f"Diff for {path_str} was cancelled by user.",
                status="rejected",
                diffCount=len(diffs),
                appliedCount=0,
                failedCount=0,
            )
            response["cancelled"] = True
            response["data"]["cancelled"] = True
            return response
        return _apply_diff_error_response(err.get("error", "denied"), path_str)

    # ------------------------------------------------------------------
    #  Resolve, then read + match + write as one locked transaction
    # ------------------------------------------------------------------
    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path_str)
    except ValueError as exc:
        return _apply_diff_error_response(str(exc), path_str)

    if not resolved.exists() or not resolved.is_file():
        return _apply_diff_error_response(f"文件不存在：{path_str}", path_str)

    session = file_state.session_key(ctx)
    try:
        tx = await asyncio.to_thread(_transact, session, resolved, requests)
    except file_state.ConcurrentEdit as exc:
        return _apply_diff_error_response(str(exc), path_str, conflict=True)
    except OSError as exc:
        return _apply_diff_error_response(f"读写文件失败：{exc}", path_str)

    if tx.status == "missing":
        return _apply_diff_error_response(f"文件不存在：{path_str}", path_str)
    if tx.status == "stale":
        return _apply_diff_error_response(file_state.STALE_MESSAGE, path_str, stale=True)
    if tx.status == "too_large":
        return _apply_diff_error_response(
            f"文件太大（{tx.size} 字节），apply_diff 最多改 {_MAX_FILE_SIZE} 字节的文件。",
            path_str,
            fileSize=tx.size,
            maxFileSize=_MAX_FILE_SIZE,
        )
    if tx.status == "not_text":
        return _apply_diff_error_response("文件不是 UTF-8 文本，apply_diff 改不了。", path_str)

    outcome = tx.outcome
    if outcome is None:
        return _apply_diff_error_response(f"apply_diff 没有得到匹配结果（{tx.status}）", path_str)
    if tx.status == "failed":
        needs_reveal = any(item.failure.closest is not None for item in outcome.failed)
        reveal = needs_reveal and file_state.reveal_allowed(tx.gate)
        return _failure_response(path_str, outcome, len(diffs), reveal=reveal)

    if tx.status == "unchanged":
        return _success_response(
            path_str, outcome, len(diffs), written=False, byte_count=0, reveal=False, checks=None,
        )

    reveal = file_state.reveal_allowed(tx.gate)
    checks = await run_edit_checks(ctx, resolved, outcome.content, tool_name="apply_diff")
    return _success_response(
        path_str, outcome, len(diffs), written=True, byte_count=tx.byte_count, reveal=reveal, checks=checks,
    )
