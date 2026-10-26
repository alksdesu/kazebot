"""hashline 编辑模式：每行带一个由内容算出的短哈希，edit_lines 用「行号:哈希」锚点定位，哈希对不上就整体拒绝。

全是纯函数；输入的文本须是 LF 换行（先过 text_files.normalize_newlines）。"""
from __future__ import annotations

import bisect
import re
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .text_files import LF, split_lines

HASHLINE = "hashline"
HASH_CHARS = 3
_HASH_MASK = (1 << (4 * HASH_CHARS)) - 1
# 模型偶尔把整行「12:a3f|内容」当锚点传进来，| 之后的部分不看
_ANCHOR = re.compile(rf"\s*([0-9]+)\s*:\s*([0-9A-Fa-f]{{{HASH_CHARS}}})\s*(?:\|.*)?", re.DOTALL)
_PREFIXED_LINE = re.compile(rf"\s*[0-9]+:[0-9A-Fa-f]{{{HASH_CHARS}}}\|")

REPLACE = "replace"
DELETE = "delete"
INSERT_AFTER = "insert_after"
INSERT_BEFORE = "insert_before"
OPS = (REPLACE, INSERT_AFTER, INSERT_BEFORE, DELETE)
RANGE_OPS = frozenset({REPLACE, DELETE})
INSERT_OPS = frozenset({INSERT_AFTER, INSERT_BEFORE})


def line_hash(line: str) -> str:
    """一行内容的 3 位十六进制哈希。行尾空白（包括残留的 \\r）不算，行首缩进算。"""
    digest = zlib.crc32(line.rstrip().encode("utf-8", "surrogatepass")) & _HASH_MASK
    return f"{digest:0{HASH_CHARS}x}"


def format_anchor(line_no: int, line: str) -> str:
    return f"{line_no}:{line_hash(line)}"


def hashline_prefix(line_no: int, line: str) -> str:
    """列行时的前缀，形如 ``12:a3f|``；签名和 read_file 的 LinePrefix 一致，拿到的是截断前的整行。"""
    return f"{format_anchor(line_no, line)}|"


def edit_mode(ctx: Any) -> str:
    """节点 yaml 的 edit_mode，经 ToolContext._node_extra 传进来；没写是空串。"""
    extra = getattr(ctx, "_node_extra", None)
    value = extra.get("edit_mode") if isinstance(extra, Mapping) else None
    return str(value or "").strip().lower()


def hashline_enabled(ctx: Any) -> bool:
    return edit_mode(ctx) == HASHLINE


@dataclass(frozen=True)
class Anchor:
    line: int
    hash: str

    def __str__(self) -> str:
        return f"{self.line}:{self.hash}"


def parse_anchor(raw: Any) -> Anchor:
    """「行号:哈希」。两边的空白、从 read_file 结果里带出来的 | 和它后面的内容都认；认不出抛 ValueError，消息可以直接给模型看。"""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError(f"要写成「行号:哈希」字符串，例如 12:a3f，收到 {type(raw).__name__}")
    text = str(raw)
    found = _ANCHOR.fullmatch(text)
    if found is None:
        raise ValueError(
            f"{text!r} 认不出来：要写成「行号:哈希」，例如 12:a3f；哈希是 read_file 每行前缀里冒号后面的 {HASH_CHARS} 位十六进制"
        )
    line = int(found.group(1))
    if line < 1:
        raise ValueError(f"{text!r} 的行号要从 1 开始")
    return Anchor(line, found.group(2).lower())


def looks_hashline_prefixed(lines: Sequence[str]) -> bool:
    """每一个非空行都以「行号:哈希|」开头：多半是把 read_file 的结果连前缀一起抄了进来。"""
    filled = [line for line in lines if line.strip()]
    return bool(filled) and all(_PREFIXED_LINE.match(line) for line in filled)


@dataclass(frozen=True)
class LineEdit:
    """start / end 都指改动前的文件。end 只有 replace / delete 有，缺省等于 start；lines 是要写进去的行。"""

    index: int
    op: str
    start: Anchor
    end: Anchor | None = None
    lines: tuple[str, ...] = ()

    @property
    def is_range(self) -> bool:
        return self.op in RANGE_OPS

    @property
    def last(self) -> int:
        return (self.end or self.start).line

    @property
    def gap(self) -> int:
        """插入点：落在第 gap 行之后，0 表示文件开头。"""
        return self.start.line if self.op == INSERT_AFTER else self.start.line - 1

    def anchors(self) -> tuple[tuple[str, Anchor], ...]:
        if self.end is None:
            return (("start", self.start),)
        return (("start", self.start), ("end", self.end))

    def splice(self) -> tuple[int, int]:
        """要换掉的原文行，按 0 起的半开区间 [lo, hi)；插入时 lo == hi。"""
        if self.is_range:
            return self.start.line - 1, self.last
        return self.gap, self.gap

    def describe(self) -> str:
        lines = f"第 {self.start.line} 行" if self.last == self.start.line else f"第 {self.start.line}-{self.last} 行"
        if self.op == DELETE:
            return f"删除{lines}"
        if self.op == REPLACE:
            return f"把{lines}换成 {len(self.lines)} 行"
        where = "之后" if self.op == INSERT_AFTER else "之前"
        return f"在第 {self.start.line} 行{where}插入 {len(self.lines)} 行"


def find_overlaps(edits: Sequence[LineEdit]) -> list[tuple[int, int, str]]:
    """互相冲突的 edit，返回 (先出现的 index, 后出现的 index, 原因)。

    两个替换 / 删除的行范围不能相交；两个插入不能落在同一个位置（先后说不清）；插入点不能落在替换 / 删除的范围中间。
    插在范围紧挨着的前后是允许的：按改动前的行号算，位置是确定的。
    """
    problems: list[tuple[int, int, str]] = []
    ranges = sorted((edit for edit in edits if edit.is_range), key=lambda edit: (edit.start.line, edit.last, edit.index))
    widest: LineEdit | None = None
    # 前缀里伸得最远的那个范围：插入点只要落在它的 [start, last) 里就冲突
    reach: list[LineEdit] = []
    for edit in ranges:
        if widest is not None and edit.start.line <= widest.last:
            problems.append(_ordered(widest, edit, "两个替换 / 删除的行范围有重叠"))
        if widest is None or edit.last > widest.last:
            widest = edit
        reach.append(widest)
    starts = [edit.start.line for edit in ranges]
    gaps: dict[int, LineEdit] = {}
    for edit in sorted((edit for edit in edits if not edit.is_range), key=lambda edit: edit.index):
        other = gaps.get(edit.gap)
        if other is not None:
            problems.append(_ordered(other, edit, "两个插入落在同一个位置，先后顺序说不清"))
        else:
            gaps[edit.gap] = edit
        position = bisect.bisect_right(starts, edit.gap) - 1
        if position >= 0 and reach[position].last > edit.gap:
            problems.append(_ordered(reach[position], edit, "插入点落在另一个 edit 要替换或删除的行中间"))
    return sorted(problems, key=lambda item: (item[1], item[0]))


def _ordered(a: LineEdit, b: LineEdit, reason: str) -> tuple[int, int, str]:
    first, second = sorted((a.index, b.index))
    return first, second, reason


@dataclass(frozen=True)
class AnchorMismatch:
    """actual 是那一行现在的哈希；行号超出文件时是 None。"""

    index: int
    role: str
    anchor: Anchor
    actual: str | None

    @property
    def out_of_range(self) -> bool:
        return self.actual is None


@dataclass(frozen=True)
class LineEditOutcome:
    """spans：每个 edit 在改动后内容里占的行（1 起，含尾）；什么都没留下的（删除）last 比 first 小 1。"""

    original: str
    content: str
    mismatches: tuple[AnchorMismatch, ...] = ()
    spans: dict[int, tuple[int, int]] = field(default_factory=dict)
    total_lines: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches

    @property
    def changed(self) -> bool:
        return self.content != self.original


def check_anchors(lines: Sequence[str], edits: Sequence[LineEdit]) -> tuple[AnchorMismatch, ...]:
    mismatches: list[AnchorMismatch] = []
    for edit in edits:
        for role, anchor in edit.anchors():
            if anchor.line > len(lines):
                mismatches.append(AnchorMismatch(edit.index, role, anchor, None))
                continue
            actual = line_hash(lines[anchor.line - 1])
            if actual != anchor.hash:
                mismatches.append(AnchorMismatch(edit.index, role, anchor, actual))
    return tuple(mismatches)


def apply_line_edits(content: str, edits: Sequence[LineEdit]) -> LineEditOutcome:
    """锚点全部对上才动。edits 须已过 find_overlaps；按位置从后往前替换，前面的行号不受后面的改动影响。"""
    lines = split_lines(content)
    mismatches = check_anchors(lines, edits)
    if mismatches:
        return LineEditOutcome(content, content, mismatches, total_lines=len(lines))
    updated = list(lines)
    # 同一个起点上范围先换、插入后放：插在范围之前的那几行才留在它前面
    for edit in sorted(edits, key=lambda edit: (edit.splice()[0], edit.is_range), reverse=True):
        lo, hi = edit.splice()
        updated[lo:hi] = edit.lines
    spans: dict[int, tuple[int, int]] = {}
    delta = 0
    for edit in sorted(edits, key=lambda edit: (edit.splice()[0], edit.is_range)):
        lo, hi = edit.splice()
        first = lo + delta + 1
        spans[edit.index] = (first, first + len(edit.lines) - 1)
        delta += len(edit.lines) - (hi - lo)
    rebuilt = LF.join(updated)
    if updated and content.endswith(LF):
        rebuilt += LF
    return LineEditOutcome(content, rebuilt, spans=spans, total_lines=len(lines))
