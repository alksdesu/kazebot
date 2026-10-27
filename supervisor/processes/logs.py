"""常驻进程的输出：按行写进 <id>.log，写满就轮转成 <id>.log.1、.2……，只留最近几段；读的时候按绝对行号分页。

行号从 1 起一直往上数，轮转掉的行不重新编号。写和读由调用方用同一把锁串起来：Windows 上开着的文件改不了名。
"""
from __future__ import annotations

import bisect
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from engine.tool_outputs import MAX_LINE_CHARS, MAX_PATTERN_CHARS, MAX_READ_LINES, MAX_SEARCH_HITS, SEARCH_CONTEXT_LINES

# 一行超过这么多字节就断开另起一行：一直不换行的进度条也撑不出一整段只有一行的日志
MAX_LINE_BYTES = 64 * 1024
DEFAULT_TAIL_LINES = 100
DEFAULT_SINCE_LINES = 200
_MIN_ROW_CAP = 200
_PATTERN_LABEL_CHARS = 100


def _char_boundary(data: bytes, start: int, end: int) -> int:
    """在 end 处切开 data[start:] 时往前挪到 UTF-8 字符边界；挪到 start 还没找到就照原样在 end 切。"""
    cut = end
    for _ in range(3):
        if cut <= start or data[cut] & 0xC0 != 0x80:
            break
        cut -= 1
    return cut if cut > start else end


def display_line(line: str) -> str:
    """回车只按终端的样子留最后一截：进度条一行里刷了上百遍，只看最新那次。"""
    if line.endswith("\r"):
        line = line[:-1]
    if "\r" not in line:
        return line
    for part in reversed(line.split("\r")):
        if part:
            return part
    return ""


class ProcessLog:
    """一个进程的输出文件。first_lines 是各段第一行的行号，从最老的留存段排到当前段。"""

    def __init__(
        self,
        directory: Path,
        process_id: str,
        *,
        max_bytes: int,
        keep_files: int,
        first_lines: list[int] | None = None,
        lines_done: int = 0,
        partial: bool = False,
    ) -> None:
        self.directory = Path(directory)
        self.process_id = process_id
        self.max_bytes = max(1, int(max_bytes))
        self.keep_files = max(0, int(keep_files))
        self.first_lines: list[int] = [int(item) for item in (first_lines or [1])][-(self.keep_files + 1):]
        self.lines_done = max(0, int(lines_done))
        self._partial = bytearray()
        # 只从记录里恢复时不知道没写完的那一行有多长，读的时候按文件内容算
        self._partial_from_record = bool(partial)
        self._stream: BinaryIO | None = None
        self._active_bytes = 0

    @property
    def active_path(self) -> Path:
        return self.directory / f"{self.process_id}.log"

    def rotated_path(self, index: int) -> Path:
        return self.directory / f"{self.process_id}.log.{index}"

    def paths(self) -> list[Path]:
        """当前段和各轮转段，不管文件在不在。"""
        return [self.active_path, *(self.rotated_path(index) for index in range(1, self.keep_files + 1))]

    @property
    def has_partial(self) -> bool:
        return bool(self._partial) or self._partial_from_record

    @property
    def last_line(self) -> int:
        """最新一行的行号，没写完的那一行也算。"""
        return self.lines_done + (1 if self.has_partial else 0)

    @property
    def first_line(self) -> int:
        return self.first_lines[0] if self.first_lines else 1

    def record(self) -> dict[str, object]:
        return {"first_lines": list(self.first_lines), "lines_done": self.lines_done, "partial": self.has_partial}

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.active_path, "ab")
        self._active_bytes = self._stream.tell()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()

    def write(self, data: bytes) -> tuple[list[str], bool]:
        """写一块输出，返回 (这块里写完整的行, 有没有轮转)。"""
        if self._stream is None:
            raise OSError("process log is not open")
        completed: list[str] = []
        rotated = False
        position = 0
        size = len(data)
        while position < size:
            room = MAX_LINE_BYTES - len(self._partial)
            if room <= 0:
                self._emit(b"\n")
                line, turned = self._complete(b"")
                completed.append(line)
                rotated = rotated or turned
                continue
            newline = data.find(b"\n", position, position + room + 1)
            if newline >= 0:
                piece = data[position:newline + 1]
                self._emit(piece)
                line, turned = self._complete(piece[:-1])
                completed.append(line)
                rotated = rotated or turned
                position = newline + 1
                continue
            if size - position <= room:
                piece = data[position:]
                self._emit(piece)
                self._partial.extend(piece)
                position = size
                continue
            cut = _char_boundary(data, position, position + room)
            piece = data[position:cut]
            self._emit(piece + b"\n")
            line, turned = self._complete(piece)
            completed.append(line)
            rotated = rotated or turned
            position = cut
        self._stream.flush()
        return completed, rotated

    def partial_text(self) -> str:
        return bytes(self._partial).decode("utf-8", "replace")

    def _emit(self, chunk: bytes) -> None:
        assert self._stream is not None
        self._stream.write(chunk)
        self._active_bytes += len(chunk)

    def _complete(self, tail: bytes) -> tuple[str, bool]:
        text = (bytes(self._partial) + tail).decode("utf-8", "replace")
        self._partial.clear()
        self._partial_from_record = False
        self.lines_done += 1
        if self._active_bytes < self.max_bytes:
            return text, False
        self._rotate()
        return text, True

    def _rotate(self) -> None:
        """只在一行刚写完时轮转，一行不会跨两段。"""
        assert self._stream is not None
        self._stream.close()
        self._stream = None
        try:
            if self.keep_files <= 0:
                self.active_path.unlink(missing_ok=True)
            else:
                self.rotated_path(self.keep_files).unlink(missing_ok=True)
                for index in range(self.keep_files - 1, 0, -1):
                    source = self.rotated_path(index)
                    if source.exists():
                        source.replace(self.rotated_path(index + 1))
                self.active_path.replace(self.rotated_path(1))
        finally:
            self.first_lines = [*self.first_lines, self.lines_done + 1][-(self.keep_files + 1):]
            self._stream = open(self.active_path, "ab")
            self._active_bytes = self._stream.tell()

    def segments(self) -> list[tuple[Path, int, int]]:
        """留存的各段 (文件, 第一行行号, 最后一行行号)，从老到新；文件不在的段不列。"""
        paths = self.paths()
        found: list[tuple[Path, int, int]] = []
        last = self.last_line
        for index, first in enumerate(reversed(self.first_lines)):
            if index >= len(paths):
                break
            path = paths[index]
            if path.exists() and first <= last:
                found.append((path, first, last))
            last = first - 1
        found.reverse()
        return found

    def read_segment(self, path: Path, first: int) -> list[tuple[int, str]]:
        try:
            raw = path.read_bytes()
        except OSError:
            return []
        if not raw:
            return []
        text = raw.decode("utf-8", "replace")
        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return [(first + offset, line) for offset, line in enumerate(parts)]

    def lines_between(self, low: int, high: int) -> list[tuple[int, str]]:
        """行号在 [low, high] 里的留存行。只读和这个范围有交集的段。"""
        found: list[tuple[int, str]] = []
        for path, first, last in self.segments():
            if last < low or first > high:
                continue
            found.extend(item for item in self.read_segment(path, first) if low <= item[0] <= high)
        return found

    def tail(self, count: int) -> list[tuple[int, str]]:
        """最后 count 行：从当前段往前读，够了就停。"""
        collected: list[tuple[int, str]] = []
        for path, first, _last in reversed(self.segments()):
            collected = self.read_segment(path, first) + collected
            if len(collected) >= count:
                break
        return collected[-count:] if count > 0 else []


@dataclass(frozen=True)
class OutputPage:
    text: str
    first_line: int
    last_line: int
    start_line: int | None
    end_line: int | None
    next_since: int
    more: bool


def _row_cap(budget: int) -> int:
    return max(_MIN_ROW_CAP, min(MAX_LINE_CHARS, budget // 2))


def _clipped(line: str, cap: int) -> str:
    if len(line) <= cap:
        return line
    return f"{line[:cap]} …[本行共 {len(line)} 字符，只显示前 {cap} 字符；用 search 能看到行内命中位置附近的内容]"


def _rows(items: list[tuple[int, str]], budget: int, *, from_end: bool) -> list[tuple[int, str]]:
    """按字数预算挑出要显示的行：tail 从最后往前挑，其余从前往后挑；至少留一行。"""
    if not items:
        return []
    width = max(4, len(str(items[-1][0])))
    cap = _row_cap(budget)
    ordered = list(reversed(items)) if from_end else list(items)
    chosen: list[tuple[int, str]] = []
    used = 0
    for number, line in ordered:
        row = f"{number:>{width}} | {_clipped(display_line(line), cap)}"
        if chosen and used + len(row) + 1 > budget:
            break
        chosen.append((number, row))
        used += len(row) + 1
    if from_end:
        chosen.reverse()
    return chosen


def _complete_upto(log: ProcessLog, shown_last: int) -> int:
    """读到 shown_last 为止，下次 since 该给几：没写完的那一行不算读过，写完了还得再给一次。"""
    if log.has_partial and shown_last >= log.last_line:
        return log.lines_done
    return shown_last


def _partial_note(log: ProcessLog, shown_last: int | None) -> str:
    return "；最后一行还没写完" if log.has_partial and shown_last is not None and shown_last >= log.last_line else ""


def page_tail(log: ProcessLog, count: int, budget: int) -> OutputPage:
    count = max(1, min(MAX_READ_LINES, int(count)))
    first, last = log.first_line, log.last_line
    if last <= 0:
        return OutputPage("还没有输出", first, 0, None, None, 0, False)
    items = log.tail(count)
    if not items:
        return OutputPage(
            f"输出共 {last} 行，留存的行读不到了（日志文件可能已被删除）", first, last, None, None, log.lines_done, False,
        )
    rows = _rows(items, budget, from_end=True)
    start, end = rows[0][0], rows[-1][0]
    since = _complete_upto(log, end)
    header = (
        f"输出共 {last} 行（留存第 {first}–{last} 行），"
        f"这次是第 {start}–{end} 行{_partial_note(log, end)}；之后有新输出用 since={since} 接着读"
    )
    return OutputPage("\n".join([header, *(row for _number, row in rows)]), first, last, start, end, since, False)


def page_since(log: ProcessLog, since: int, max_lines: int, budget: int) -> OutputPage:
    since = max(0, int(since))
    max_lines = max(1, min(MAX_READ_LINES, int(max_lines)))
    first, last = log.first_line, log.last_line
    lost = ""
    low = since + 1
    if low < first:
        lost = f"第 {low}–{first - 1} 行已经轮转掉了，从留存的第 {first} 行读起。"
        low = first
    if low > last:
        text = f"{lost}第 {since} 行之后还没有新输出（共 {last} 行）；稍后用 since={min(since, log.lines_done)} 再读"
        return OutputPage(text, first, last, None, None, min(since, log.lines_done), False)
    items = log.lines_between(low, min(last, low + max_lines - 1))
    if not items:
        text = f"{lost}第 {low}–{last} 行读不到了（日志文件可能已被删除）"
        return OutputPage(text, first, last, None, None, log.lines_done, False)
    rows = _rows(items, budget, from_end=False)
    start, end = rows[0][0], rows[-1][0]
    next_since = _complete_upto(log, end)
    more = end < last
    position = f"后面还有，用 since={next_since} 接着读" if more else f"已到当前末尾，之后有新输出用 since={next_since} 接着读"
    header = f"{lost}第 {start}–{end} 行（共 {last} 行）{_partial_note(log, end)}；{position}"
    return OutputPage("\n".join([header, *(row for _number, row in rows)]), first, last, start, end, next_since, more)


class _Matcher:
    """pattern 当正则用；不是合法正则或正则没命中时，再按普通子串找。"""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        try:
            self.regex: re.Pattern[str] | None = re.compile(pattern)
        except re.error:
            self.regex = None

    @property
    def mode(self) -> str:
        return "正则或子串" if self.regex is not None else "子串"

    def span(self, line: str) -> tuple[int, int] | None:
        spans: list[tuple[int, int]] = []
        if self.regex is not None:
            match = self.regex.search(line)
            if match is not None:
                spans.append((match.start(), match.end()))
        index = line.find(self.pattern)
        if index >= 0:
            spans.append((index, index + len(self.pattern)))
        return min(spans) if spans else None


def _pattern_label(pattern: str) -> str:
    label = repr(pattern)
    return label if len(label) <= _PATTERN_LABEL_CHARS else f"{label[:_PATTERN_LABEL_CHARS]}…"


def _hit_row(number: int, line: str, width: int, cap: int, span: tuple[int, int]) -> str:
    if len(line) <= cap:
        return f"{number:>{width}}: {line}"
    begin = max(0, span[0] - cap // 4)
    end = min(len(line), begin + cap)
    snippet = ("…" if begin > 0 else "") + line[begin:end] + ("…" if end < len(line) else "")
    return f"{number:>{width}}: {snippet} [本行共 {len(line)} 字符，显示第 {begin + 1}–{end} 字符]"


@dataclass(frozen=True)
class SearchSlice:
    """要搜的那段输出和当时的行号状态：读盘要和写串在一把锁里，匹配不用。"""

    pattern: str
    since: int
    first: int
    last: int
    low: int
    lines: tuple[tuple[int, str], ...]
    idle_since: int
    complete_upto: int


def search_slice(log: ProcessLog, pattern: str, since: int) -> SearchSlice:
    if not pattern:
        raise ValueError("search 不能是空串")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"search 太长：最多 {MAX_PATTERN_CHARS} 个字符")
    since = max(0, int(since))
    first, last = log.first_line, log.last_line
    low = max(since + 1, first)
    lines = tuple(log.lines_between(low, last)) if low <= last else ()
    return SearchSlice(
        pattern, since, first, last, low, lines, min(since, log.lines_done), _complete_upto(log, last),
    )


def page_search(log: ProcessLog, pattern: str, since: int, budget: int) -> OutputPage:
    return render_search(search_slice(log, pattern, since), budget)


def render_search(found: SearchSlice, budget: int) -> OutputPage:
    first, last, low = found.first, found.last, found.low
    label = _pattern_label(found.pattern)
    if low > last:
        text = f"第 {found.since} 行之后还没有新输出（共 {last} 行），没什么可搜的"
        return OutputPage(text, first, last, None, None, found.idle_since, False)
    matcher = _Matcher(found.pattern)
    lines = [(number, display_line(line)) for number, line in found.lines]
    spans: dict[int, tuple[int, int]] = {}
    for index, (_number, line) in enumerate(lines):
        span = matcher.span(line)
        if span is not None:
            spans[index] = span
    hits = sorted(spans)
    if not hits:
        text = f"第 {low}–{last} 行里没有匹配 {label} 的行（按{matcher.mode}）"
        return OutputPage(text, first, last, low, last, found.complete_upto, False)
    width = max(4, len(str(last)))
    cap = max(40, min(MAX_LINE_CHARS, budget // (2 * SEARCH_CONTEXT_LINES + 1) - 40))
    rows: list[str] = []
    used = 0
    rendered_to = -1
    for hit in hits:
        if hit <= rendered_to:
            continue
        if bisect.bisect_right(hits, rendered_to) >= MAX_SEARCH_HITS:
            break
        low_index = max(0, hit - SEARCH_CONTEXT_LINES, rendered_to + 1)
        high_index = min(len(lines) - 1, hit + SEARCH_CONTEXT_LINES)
        # 命中挨得近时一段上下文会带进后面的命中，停在超出上限的那一个之前，它留给下一页
        if len(hits) > MAX_SEARCH_HITS and hits[MAX_SEARCH_HITS] <= high_index:
            high_index = hits[MAX_SEARCH_HITS] - 1
        block: list[str] = []
        if rendered_to >= 0 and low_index > rendered_to + 1:
            block.append("--")
        for position in range(low_index, high_index + 1):
            number, line = lines[position]
            if position in spans:
                block.append(_hit_row(number, line, width, cap, spans[position]))
            else:
                block.append(f"{number:>{width}}- {_clipped(line, cap)}")
        cost = sum(len(row) + 1 for row in block)
        if rows and used + cost > budget:
            break
        rows.extend(block)
        used += cost
        rendered_to = high_index
    shown = bisect.bisect_right(hits, rendered_to)
    pending = hits[shown:]
    if pending:
        next_since = lines[pending[0]][0] - 1
        position_note = f"后面还有，用 since={next_since} 接着搜"
    else:
        next_since = found.complete_upto
        position_note = f"没有更多命中，之后有新输出用 since={next_since} 接着搜"
    header = f"第 {low}–{last} 行里按{matcher.mode} {label} 命中 {len(hits)} 行，这次列出 {shown} 行；{position_note}"
    end_line = lines[rendered_to][0]
    return OutputPage("\n".join([header, *rows]), first, last, low, end_line, next_since, bool(pending))
