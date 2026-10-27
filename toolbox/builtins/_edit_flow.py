"""apply_diff / edit_lines 共用的改文本流程：认领、送审，在文件锁里读、比戳、改、记编辑日志、原子写入，回显改动处。"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .. import edit_journal, file_state
from .._common import request_guard, resolve_under_allowed_roots
from ..context import project_root_of
from ..hashline import hashline_enabled, hashline_prefix
from ..text_files import TextLayout, decode_text, encode_text, normalize_newlines, split_lines
from .read_file import LinePrefix, number_prefix, render_lines

MAX_TEXT_EDIT_BYTES = 1024 * 1024 * 64
ECHO_CONTEXT_LINES = 2
ECHO_MAX_CHARS = 6000
SNIPPET_LINE_CHARS = 300
_ECHO_REGION_MAX_LINES = 40
_ECHO_REGION_HEAD_LINES = 25
_ECHO_REGION_TAIL_LINES = 10
_ECHO_LINE_CHARS = 500


@dataclass(frozen=True)
class LineStyle:
    """结果里列行的样子：默认是右对齐的行号，节点开了 hashline 模式时是「行号:哈希|」。"""

    hashline: bool = False

    @property
    def label(self) -> str:
        return "行号:哈希" if self.hashline else "行号"

    def prefix(self, highest: int) -> LinePrefix:
        return hashline_prefix if self.hashline else number_prefix(max(4, len(str(highest))))


NUMBERED = LineStyle()
HASHLINED = LineStyle(hashline=True)


def line_style(ctx: Any) -> LineStyle:
    return HASHLINED if hashline_enabled(ctx) else NUMBERED


def edit_error_response(error: Any, path: str = "", **data_fields: Any) -> dict[str, Any]:
    # 失败一律带 ok=false 和 data.result；data_fields 可以拿更完整的报告盖掉 data.result
    text = str(error)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    if path:
        data["file"] = path
    data.update(data_fields)
    return {"ok": False, "success": False, "error": text, "data": data}


class EditRefused(Exception):
    """改不了；response 是原样交给模型的工具结果。"""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(str(response.get("error", "")))
        self.response = response


def guard_error_response(err: dict[str, Any], path: str, *, what: str, **cancel_fields: Any) -> dict[str, Any]:
    """审批没放行时的结果；被取消的在顶层和 data 里都带 cancelled，上层靠它判任务取消。"""
    if err.get("cancelled", False):
        response = edit_error_response(
            f"{what} was cancelled by user",
            path,
            message=f"{what} for {path} was cancelled by user.",
            status="rejected",
            **cancel_fields,
        )
        response["cancelled"] = True
        response["data"]["cancelled"] = True
        return response
    return edit_error_response(err.get("error", "denied"), path)


def journal_target_refusal(ctx: Any, path: str) -> dict[str, Any] | None:
    """要改的文件落在编辑日志目录里就直接拒，不必先为它弹审批卡。"""
    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path, project_root=project_root_of(ctx))
    except ValueError:
        return None
    if edit_journal.inside_journal(ctx.workspace_root, resolved):
        return edit_error_response(edit_journal.JOURNAL_REFUSED_MESSAGE, path)
    return None


@dataclass(frozen=True)
class EditTarget:
    """认领和送审都过了的目标：解析到底的路径，和 supervisor 给的准许（比对用的戳、编辑日志的会话）。"""

    resolved: Path
    grant: file_state.EditGrant


async def claim_or_refuse(ctx: Any, resolved: Path, path: str) -> file_state.EditGrant:
    """向 supervisor 认领要改的文件；别的任务在改、supervisor 连不上，都换成工具结果抛 EditRefused。"""
    try:
        return await file_state.begin_edit(ctx, resolved, path)
    except file_state.EditBlocked as blocked:
        raise EditRefused(edit_error_response(blocked.message, path, **blocked.data)) from None


async def guard_and_resolve(
    ctx: Any, path: str, *, tool_name: str, preview: str, what: str, **cancel_fields: Any,
) -> EditTarget:
    """把路径解析到允许的根下，先认领再过 write_file 的策略和审批；要改的文件必须已经存在。

    先认领：别的任务正在改的文件不必为它弹一张注定白批的审批卡。没走到写盘就把这次才拿的认领还回去。
    """
    refusal = journal_target_refusal(ctx, path)
    if refusal is not None:
        raise EditRefused(refusal)
    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path, project_root=project_root_of(ctx))
    except ValueError as exc:
        raise EditRefused(edit_error_response(str(exc), path)) from None
    grant = await claim_or_refuse(ctx, resolved, path)
    try:
        _op, err = await request_guard(
            ctx,
            "write_file",
            {"path": path, "content_preview": preview, "content_len": 0, "tool_name": tool_name},
        )
        if err is not None:
            raise EditRefused(guard_error_response(err, path, what=what, **cancel_fields))
        if not resolved.exists() or not resolved.is_file():
            raise EditRefused(edit_error_response(f"文件不存在：{path}", path))
    except Exception:
        await file_state.abandon_edit(ctx, grant)
        raise
    return EditTarget(resolved, grant)


class EditOutcome(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def changed(self) -> bool: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True)
class TextTransaction:
    """status：written / unchanged / failed / stale / missing / not_text / too_large。

    stamp_recorded：写成的新版本有没有记到本任务名下；没记上的话下次改这个文件会被要求重读。
    """

    status: str
    gate: file_state.Gate
    outcome: Any = None
    stamp: file_state.Stamp | None = None
    byte_count: int = 0
    size: int = 0
    journal: edit_journal.JournalNote | None = None
    stamp_recorded: bool = True


def transact_text(
    scope: edit_journal.JournalScope,
    resolved: Path,
    apply: Callable[[str], EditOutcome],
    *,
    grant: file_state.EditGrant,
    max_size: int,
    tool: str,
    display: str,
) -> TextTransaction:
    """在文件锁里读出 LF 文本交给 apply；全部改成了、内容也真变了，才记编辑日志并原子写入。新戳由调用方锁外登记。"""
    with file_state.edit_lock(resolved, scope.workspace_root):
        try:
            size = os.stat(resolved).st_size
        except FileNotFoundError:
            return TextTransaction("missing", file_state.Gate(None))
        if size > max_size:
            return TextTransaction("too_large", file_state.Gate(None), size=size)
        snapshot = file_state.take_snapshot(resolved)
        if not snapshot.exists:
            return TextTransaction("missing", file_state.Gate(None))
        gate = grant.gate(snapshot)
        if gate.stale:
            return TextTransaction("stale", gate)
        if len(snapshot.data) > max_size:
            return TextTransaction("too_large", gate, size=len(snapshot.data))
        try:
            text = decode_text(snapshot.data)
        except UnicodeDecodeError:
            return TextTransaction("not_text", gate)
        outcome = apply(normalize_newlines(text))
        if not outcome.ok:
            return TextTransaction("failed", gate, outcome)
        if not outcome.changed:
            return TextTransaction("unchanged", gate, outcome)
        data = encode_text(outcome.content, TextLayout.of_bytes(snapshot.data))
        pending = edit_journal.record_pre_image(
            scope, resolved, snapshot, tool=tool, display=display, revealable=gate.revealable,
        )
        try:
            stamp = file_state.commit_locked(resolved, data, expected=snapshot)
        except BaseException:
            pending.discard()
            raise
        note = pending.record_post_image(stamp)
        return TextTransaction("written", gate, outcome, stamp, len(data), journal=note)


def stale_fields(gate: file_state.Gate) -> dict[str, Any]:
    """读后被改过时并进结果 data 的字段：被同一会话里别的任务改过的另外标出来。"""
    return {"stale": True, "changedByOtherTask": True} if gate.foreign else {"stale": True}


async def settle_transaction(ctx: Any, grant: file_state.EditGrant, tx: TextTransaction) -> TextTransaction:
    """写成了就把新版本记到本任务名下；没写成就还回这次才拿的认领。"""
    if tx.status != "written":
        await file_state.abandon_edit(ctx, grant)
        return tx
    recorded = await file_state.finish_edit(ctx, grant, tx.stamp, revealable=tx.gate.revealable)
    return tx if recorded else replace(tx, stamp_recorded=False)


async def run_text_edit(
    ctx: Any,
    target: EditTarget,
    path: str,
    apply: Callable[[str], EditOutcome],
    *,
    tool_name: str,
    max_size: int,
) -> TextTransaction:
    """跑 transact_text；文件没了、读后被改过、太大、不是文本、写入时撞上并发改动，都换成工具结果抛 EditRefused。"""
    scope = edit_journal.JournalScope.of(ctx, session=target.grant.journal_session)
    try:
        tx = await asyncio.to_thread(
            transact_text, scope, target.resolved, apply,
            grant=target.grant, max_size=max_size, tool=tool_name, display=path,
        )
    except file_state.ConcurrentEdit as exc:
        await file_state.abandon_edit(ctx, target.grant)
        raise EditRefused(edit_error_response(str(exc), path, conflict=True)) from None
    except OSError as exc:
        await file_state.abandon_edit(ctx, target.grant)
        raise EditRefused(edit_error_response(f"读写文件失败：{exc}", path)) from None
    tx = await settle_transaction(ctx, target.grant, tx)
    if tx.status == "missing":
        raise EditRefused(edit_error_response(f"文件不存在：{path}", path))
    if tx.status == "stale":
        raise EditRefused(edit_error_response(tx.gate.stale_message, path, **stale_fields(tx.gate)))
    if tx.status == "too_large":
        raise EditRefused(edit_error_response(
            f"文件太大（{tx.size} 字节），{tool_name} 最多改 {max_size} 字节的文件。",
            path,
            fileSize=tx.size,
            maxFileSize=max_size,
        ))
    if tx.status == "not_text":
        raise EditRefused(edit_error_response(f"文件不是 UTF-8 文本，{tool_name} 改不了。", path))
    if tx.outcome is None:
        raise EditRefused(edit_error_response(f"{tool_name} 没有得到改动结果（{tx.status}）", path))
    return tx


@dataclass(frozen=True)
class EchoSpan:
    """一处改动在改后内容里占的行（1 起，含尾）；什么都没留下时 last 比 first 小。label 形如 diffs[0]。"""

    index: int
    label: str
    first: int
    last: int


@dataclass(frozen=True)
class _Region:
    low: int
    high: int
    spans: tuple[EchoSpan, ...]

    @property
    def names(self) -> str:
        return "、".join(span.label for span in sorted(self.spans, key=lambda span: span.index))


def _echo_regions(spans: Sequence[EchoSpan], total_lines: int) -> list[_Region]:
    """每处改动前后各带几行的窗口，挨着或重叠的并成一段。"""
    regions: list[_Region] = []
    for span in sorted(spans, key=lambda span: (span.first, span.last, span.index)):
        low = max(1, span.first - ECHO_CONTEXT_LINES)
        high = min(total_lines, max(span.first, span.last) + ECHO_CONTEXT_LINES)
        if regions and low <= regions[-1].high + 1:
            previous = regions[-1]
            regions[-1] = _Region(previous.low, max(previous.high, high), previous.spans + (span,))
        else:
            regions.append(_Region(low, high, (span,)))
    return regions


def _region_block(lines: list[str], region: _Region, style: LineStyle) -> list[str]:
    block = [f"── 第 {region.low}-{region.high} 行（{region.names}）──"]
    numbers = list(range(region.low, region.high + 1))
    skipped = 0
    if len(numbers) > _ECHO_REGION_MAX_LINES:
        skipped = len(numbers) - _ECHO_REGION_HEAD_LINES - _ECHO_REGION_TAIL_LINES
        numbers = numbers[:_ECHO_REGION_HEAD_LINES] + [0] + numbers[-_ECHO_REGION_TAIL_LINES:]
    prefix = style.prefix(region.high)
    for line_no in numbers:
        if line_no == 0:
            block.append(f"…（中间 {skipped} 行没有列出）")
            continue
        rendered, _clipped = render_lines([lines[line_no - 1]], line_no, prefix, limit=_ECHO_LINE_CHARS)[0]
        block.append(rendered)
    return block


def echo_lines(content: str, spans: Sequence[EchoSpan], style: LineStyle) -> list[str]:
    """改后内容里每处改动连同前后几行；总长超过 ECHO_MAX_CHARS 就停下，写明哪几处没显示全。"""
    lines = split_lines(content)
    if not lines:
        return ["改动后文件是空的。"]
    out = [f"改动后的内容（按改后的{style.label}，前后各带 {ECHO_CONTEXT_LINES} 行）："]
    used = 0
    regions = [region for region in _echo_regions(spans, len(lines)) if region.low <= region.high]
    for position, region in enumerate(regions):
        for line in _region_block(lines, region, style):
            if used + len(line) + 1 > ECHO_MAX_CHARS:
                rest = regions[position:]
                names = "、".join(item.names for item in rest)
                ranges = "、".join(f"{item.low}-{item.high}" for item in rest)
                out.append(
                    f"…回显超过 {ECHO_MAX_CHARS} 字符已截断：{names} 的改动没有完整显示，需要时用 read_file 看第 {ranges} 行。"
                )
                return out
            out.append(line)
            used += len(line) + 1
    return out
