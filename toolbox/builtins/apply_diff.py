"""apply_diff — apply sequential search/replace diffs to a file (policy + approval guarded).

All diffs are matched in memory first (see ``toolbox.edit_match``); the file is written only
when every one of them applies. diffs[1] operates on the result of diffs[0], and so on."""
from __future__ import annotations

from typing import Any

from .. import edit_journal, file_state
from ..context import ToolContext
from ..edit_match import DiffOutcome, DiffRequest, FailedDiff, apply_diffs, line_range
from ..syntax_check import EditChecks, run_edit_checks
from ..text_files import split_lines
from ._edit_flow import (
    MAX_TEXT_EDIT_BYTES,
    SNIPPET_LINE_CHARS,
    EchoSpan,
    EditRefused,
    LineStyle,
    echo_lines,
    edit_error_response,
    guard_and_resolve,
    line_style,
    run_text_edit,
)
from .read_file import render_lines


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = MAX_TEXT_EDIT_BYTES
_SNIPPET_MAX_LINES = 12


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

_apply_diff_error_response = edit_error_response


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


def _closest_lines(failed: FailedDiff, style: LineStyle) -> list[str]:
    closest = failed.failure.closest
    if closest is None:
        return []
    # Shown in the numbering start_line uses: the file as read, before the earlier diffs of this call.
    first_shown = closest.first + 1 - failed.line_delta
    last_shown = closest.last + 1 - failed.line_delta
    out = [f"  最接近的是第 {first_shown}-{last_shown} 行（相似度 {closest.similarity:.2f}）："]
    window = split_lines(failed.content)[closest.first:closest.last + 1]
    rendered = render_lines(
        window[:_SNIPPET_MAX_LINES], first_shown, style.prefix(last_shown), limit=SNIPPET_LINE_CHARS,
    )
    out.extend("    " + line for line, _clipped in rendered)
    if len(window) > _SNIPPET_MAX_LINES:
        out.append(f"    …（还有 {len(window) - _SNIPPET_MAX_LINES} 行没有列出）")
    return out


def _failure_response(
    path_str: str, outcome: DiffOutcome, diff_count: int, *, reveal: bool, style: LineStyle,
) -> dict[str, Any]:
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
                lines.extend(_closest_lines(item, style))
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


def _echo_spans(outcome: DiffOutcome) -> list[EchoSpan]:
    return [
        EchoSpan(index, f"diffs[{index}]", *line_range(outcome.content, start, end))
        for index, (start, end) in outcome.spans.items()
    ]


def _success_response(
    path_str: str,
    outcome: DiffOutcome,
    diff_count: int,
    *,
    written: bool,
    byte_count: int,
    reveal: bool,
    checks: EditChecks | None,
    style: LineStyle,
    journal: edit_journal.JournalNote | None = None,
) -> dict[str, Any]:
    summary_lines, entries = _diff_summaries(outcome)
    if written:
        head = f"已修改 {path_str}：{diff_count} 个 diff 全部应用并保存（{byte_count} 字节）。"
    else:
        head = f"{path_str} 没有变化：{diff_count} 个 diff 都匹配上了，但应用后的内容和原文件相同，没有写盘。"
    lines = [head, *summary_lines]
    if journal is not None:
        lines.extend(journal.lines())
    if written:
        lines.extend(
            echo_lines(outcome.content, _echo_spans(outcome), style) if reveal
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
    if journal is not None:
        data.update(journal.to_data())
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
    #  Policy + approval guard (same flow as write_file), then read + match + write as one locked transaction
    # ------------------------------------------------------------------
    try:
        resolved = await guard_and_resolve(
            ctx,
            path_str,
            tool_name="apply_diff",
            preview=f"apply_diff: {len(diffs)} diff(s)",
            what="Diff",
            diffCount=len(diffs),
            appliedCount=0,
            failedCount=0,
        )
        tx = await run_text_edit(
            ctx, resolved, path_str, lambda text: apply_diffs(text, requests),
            tool_name="apply_diff", max_size=_MAX_FILE_SIZE,
        )
    except EditRefused as refused:
        return refused.response

    style = line_style(ctx)
    outcome: DiffOutcome = tx.outcome
    if tx.status == "failed":
        needs_reveal = any(item.failure.closest is not None for item in outcome.failed)
        reveal = needs_reveal and file_state.reveal_allowed(tx.gate)
        return _failure_response(path_str, outcome, len(diffs), reveal=reveal, style=style)

    if tx.status == "unchanged":
        return _success_response(
            path_str, outcome, len(diffs), written=False, byte_count=0, reveal=False, checks=None, style=style,
        )

    reveal = file_state.reveal_allowed(tx.gate)
    checks = await run_edit_checks(ctx, resolved, outcome.content, tool_name="apply_diff")
    return _success_response(
        path_str, outcome, len(diffs),
        written=True, byte_count=tx.byte_count, reveal=reveal, checks=checks, style=style, journal=tx.journal,
    )
