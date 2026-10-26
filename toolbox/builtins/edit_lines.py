"""edit_lines：按 read_file 在 hashline 模式下给出的「行号:哈希」锚点改行（策略 + 审批）。

锚点都指改动前的文件。有一个对不上就整体拒绝、不写盘，结果里给出锚点附近的最新内容；锚点不会自动挪到别的行。
写盘流程和 apply_diff 是同一条：认领、送审、读后改前比戳、记编辑日志、原子写入、改后检查。"""
from __future__ import annotations

from typing import Any

from .. import edit_journal, file_state
from ..context import ToolContext
from ..hashline import (
    DELETE,
    INSERT_OPS,
    OPS,
    AnchorMismatch,
    LineEdit,
    LineEditOutcome,
    apply_line_edits,
    find_overlaps,
    hashline_prefix,
    looks_hashline_prefixed,
    parse_anchor,
)
from ..syntax_check import EditChecks, run_edit_checks
from ..text_files import normalize_newlines, split_lines
from ._edit_flow import (
    HASHLINED,
    MAX_TEXT_EDIT_BYTES,
    SNIPPET_LINE_CHARS,
    EchoSpan,
    EditRefused,
    echo_lines,
    edit_error_response,
    guard_and_resolve,
    run_text_edit,
)
from .read_file import render_lines

_MAX_FILE_SIZE = MAX_TEXT_EDIT_BYTES
_SNIPPET_CONTEXT_LINES = 3
_SNIPPET_MAX_CHARS = 4000


def _content_lines(raw: Any) -> tuple[str, ...]:
    """content 换成要写进去的行：字符串按换行切，结尾的一个换行不多算一行；也认一项一行的字符串数组。"""
    if isinstance(raw, str):
        return tuple(split_lines(normalize_newlines(raw)))
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return tuple(part for item in raw for part in normalize_newlines(item).split("\n"))
    raise ValueError(f"content 要是字符串，收到 {type(raw).__name__}")


def _parse_edit(index: int, item: Any) -> LineEdit:
    if not isinstance(item, dict):
        raise ValueError("edit 必须是对象，含 op 和 start，按 op 还要 end 或 content")
    op = str(item.get("op") or "").strip().lower()
    if op not in OPS:
        raise ValueError(f"op 只能是 {' / '.join(OPS)}，收到 {item.get('op')!r}")
    if item.get("start") in (None, ""):
        raise ValueError("缺少 start（「行号:哈希」锚点，从 read_file 的结果里抄）")
    try:
        start = parse_anchor(item.get("start"))
    except ValueError as exc:
        raise ValueError(f"start {exc}") from None
    end = None
    raw_end = item.get("end")
    if raw_end is not None and raw_end != "":
        if op in INSERT_OPS:
            raise ValueError(f"{op} 只认 start，不要给 end")
        try:
            end = parse_anchor(raw_end)
        except ValueError as exc:
            raise ValueError(f"end {exc}") from None
        if end.line < start.line:
            raise ValueError(f"end（第 {end.line} 行）在 start（第 {start.line} 行）之前")
    content = item.get("content")
    if op == DELETE:
        if content is not None and content != "" and content != []:
            raise ValueError("delete 不用 content；要换成别的内容用 replace")
        return LineEdit(index, op, start, end)
    if content is None:
        raise ValueError("缺少 content")
    lines = _content_lines(content)
    if op in INSERT_OPS and not lines:
        raise ValueError("content 是空的，没有要插入的内容")
    if looks_hashline_prefixed(lines):
        raise ValueError("content 的每一行都带着「行号:哈希|」前缀，像是把 read_file 的结果原样抄了进来；content 只写行的内容本身")
    return LineEdit(index, op, start, end, lines)


def _parse_edits(raw_edits: list[Any]) -> tuple[list[LineEdit], list[tuple[int, str]]]:
    edits: list[LineEdit] = []
    problems: list[tuple[int, str]] = []
    for index, item in enumerate(raw_edits):
        try:
            edits.append(_parse_edit(index, item))
        except ValueError as exc:
            problems.append((index, str(exc)))
    if not problems:
        problems = [(later, f"和 edits[{earlier}] 冲突：{reason}") for earlier, later, reason in find_overlaps(edits)]
    return edits, problems


def _fresh_snippets(lines: list[str], mismatches: list[AnchorMismatch]) -> list[str]:
    """对不上的锚点附近现在的内容，带新的锚点；挨着的窗口并成一段，总长有上限。"""
    total = len(lines)
    windows: list[list[int]] = []
    for line in sorted({min(item.anchor.line, total) for item in mismatches}):
        low = max(1, line - _SNIPPET_CONTEXT_LINES)
        high = min(total, line + _SNIPPET_CONTEXT_LINES)
        if windows and low <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], high)
        else:
            windows.append([low, high])
    out = ["文件现在的内容（行号:哈希）："]
    used = 0
    for position, (low, high) in enumerate(windows):
        block = [f"── 第 {low}-{high} 行 ──"]
        rendered = render_lines(lines[low - 1:high], low, hashline_prefix, limit=SNIPPET_LINE_CHARS)
        block.extend(line for line, _clipped in rendered)
        cost = sum(len(line) + 1 for line in block)
        if position and used + cost > _SNIPPET_MAX_CHARS:
            rest = "、".join(f"{rest_low}-{rest_high}" for rest_low, rest_high in windows[position:])
            out.append(f"…片段超过 {_SNIPPET_MAX_CHARS} 字符，第 {rest} 行没有列出，需要时用 read_file 看。")
            break
        out.extend(block)
        used += cost
    return out


def _mismatch_response(path_str: str, outcome: LineEditOutcome, edit_count: int, *, reveal: bool) -> dict[str, Any]:
    mismatches = sorted(outcome.mismatches, key=lambda item: (item.index, item.role != "start"))
    failed = sorted({item.index for item in mismatches})
    total = outcome.total_lines
    lines = [
        f"ERROR: edit_lines 没有修改 {path_str}：{edit_count} 个 edit 中有 {len(failed)} 个的锚点和文件现在的内容对不上，"
        "文件保持原样。锚点不会自动挪到别的行；按最新的内容改好锚点，把全部 edits 一起重新提交。",
    ]
    if total == 0:
        lines.append("文件是空的（0 行），没有可以定位的行；往空文件里写内容用 write_file。")
    entries: list[dict[str, Any]] = []
    for item in mismatches:
        label = f"edits[{item.index}].{item.role} {item.anchor}"
        entry: dict[str, Any] = {
            "index": item.index, "anchor": item.role, "expected": str(item.anchor), "line": item.anchor.line,
        }
        if item.out_of_range:
            entry["reason"] = "out_of_range"
            lines.append(f"{label} 超出了文件的行数（共 {total} 行）")
        else:
            entry["reason"] = "hash_mismatch"
            if reveal:
                current = f"{item.anchor.line}:{item.actual}"
                entry["actual"] = current
                lines.append(f"{label} 对不上：第 {item.anchor.line} 行现在是 {current}")
            else:
                lines.append(f"{label} 对不上")
        entries.append(entry)
    if not reveal:
        lines.append("（本会话没有读过这个文件的当前版本，不提供最新内容；先 read_file 再改）")
    elif total:
        lines.extend(_fresh_snippets(split_lines(outcome.original), mismatches))
    first = mismatches[0]
    reason = "超出了文件的行数" if first.out_of_range else "哈希对不上"
    return edit_error_response(
        f"{len(failed)} 个 edit 的锚点对不上，文件没有改动：edits[{first.index}].{first.role} {first.anchor} {reason}",
        path_str,
        result="\n".join(lines),
        message=f"{path_str} 没有改动：{len(failed)} 个 edit 的锚点对不上",
        failedEdits=entries,
        appliedCount=0,
        failedCount=len(failed),
        editCount=edit_count,
        totalLines=total,
    )


def _edit_summaries(outcome: LineEditOutcome, edits: list[LineEdit]) -> tuple[list[str], list[dict[str, Any]]]:
    new_total = len(split_lines(outcome.content))
    lines: list[str] = []
    entries: list[dict[str, Any]] = []
    for edit in sorted(edits, key=lambda item: item.index):
        first, last = outcome.spans.get(edit.index, (0, -1))
        if last < first:
            where = "原处在改动后的文件末尾" if first > new_total else f"原处在改动后的第 {first} 行"
        elif first == last:
            where = f"改动后在第 {first} 行"
        else:
            where = f"改动后在第 {first}-{last} 行"
        lines.append(f"edits[{edit.index}] {edit.describe()}，{where}")
        entry: dict[str, Any] = {
            "index": edit.index, "op": edit.op, "start": str(edit.start),
            "startLine": first, "endLine": last, "lineCount": len(edit.lines),
        }
        if edit.end is not None:
            entry["end"] = str(edit.end)
        entries.append(entry)
    return lines, entries


def _echo_spans(outcome: LineEditOutcome) -> list[EchoSpan]:
    return [EchoSpan(index, f"edits[{index}]", first, last) for index, (first, last) in outcome.spans.items()]


def _success_response(
    path_str: str,
    outcome: LineEditOutcome,
    edits: list[LineEdit],
    *,
    written: bool,
    byte_count: int,
    reveal: bool,
    checks: EditChecks | None,
    journal: edit_journal.JournalNote | None = None,
    stamp_recorded: bool = True,
) -> dict[str, Any]:
    count = len(edits)
    if written:
        head = f"已修改 {path_str}：{count} 个 edit 全部应用并保存（{byte_count} 字节）。"
    else:
        head = f"{path_str} 没有变化：{count} 个 edit 的锚点都对上了，但改完的内容和原文件相同，没有写盘。"
    summary_lines, entries = _edit_summaries(outcome, edits)
    lines = [head, *summary_lines]
    if journal is not None:
        lines.extend(journal.lines())
    if not stamp_recorded:
        lines.append(file_state.STAMP_NOT_RECORDED_NOTE)
    if written:
        lines.extend(
            echo_lines(outcome.content, _echo_spans(outcome), HASHLINED) if reveal
            else ["（本会话没有读过这个文件的当前版本，不回显改动处的内容；接着改之前先 read_file 拿新的锚点）"]
        )
    if checks is not None:
        lines.extend(checks.lines())
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "file": path_str,
        "message": head,
        "status": "accepted",
        "editCount": count,
        "appliedCount": count,
        "failedCount": 0,
        "changed": written,
        "edits": entries,
        "echoed": bool(written and reveal),
    }
    if written:
        data["bytes"] = byte_count
    if not stamp_recorded:
        data["stampRecorded"] = False
    if journal is not None:
        data.update(journal.to_data())
    if checks is not None:
        data.update(checks.to_data())
    return {"ok": True, "success": True, "data": data}


async def edit_lines(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path_str = str(args.get("path") or "").strip()
    raw_edits = args.get("edits")
    # 参数不对就在送审之前拒掉：一次写错的调用不该弹审批卡
    if not path_str:
        return edit_error_response("'path' is required.")
    if not isinstance(raw_edits, list) or not raw_edits:
        return edit_error_response("'edits' must be a non-empty array.", path_str)
    edits, problems = _parse_edits(raw_edits)
    if problems:
        detail = "；".join(f"edits[{index}] {problem}" for index, problem in problems)
        return edit_error_response(
            f"edit 参数有误，文件没有改动：{detail}",
            path_str,
            failedEdits=[{"index": index, "reason": "invalid", "error": problem} for index, problem in problems],
            appliedCount=0,
            failedCount=len({index for index, _problem in problems}),
            editCount=len(raw_edits),
        )

    try:
        target = await guard_and_resolve(
            ctx,
            path_str,
            tool_name="edit_lines",
            preview=f"edit_lines: {len(edits)} edit(s)",
            what="Edit",
            editCount=len(edits),
            appliedCount=0,
            failedCount=0,
        )
        tx = await run_text_edit(
            ctx, target, path_str, lambda text: apply_line_edits(text, edits),
            tool_name="edit_lines", max_size=_MAX_FILE_SIZE,
        )
    except EditRefused as refused:
        return refused.response

    resolved = target.resolved
    outcome: LineEditOutcome = tx.outcome
    if tx.status == "failed":
        reveal = await file_state.reveal_allowed(ctx, tx.gate, path_str)
        return _mismatch_response(path_str, outcome, len(edits), reveal=reveal)
    if tx.status == "unchanged":
        return _success_response(path_str, outcome, edits, written=False, byte_count=0, reveal=False, checks=None)

    reveal = await file_state.reveal_allowed(ctx, tx.gate, path_str)
    checks = await run_edit_checks(ctx, resolved, outcome.content, tool_name="edit_lines")
    return _success_response(
        path_str, outcome, edits,
        written=True, byte_count=tx.byte_count, reveal=reveal, checks=checks, journal=tx.journal,
        stamp_recorded=tx.stamp_recorded,
    )
