"""apply_diff 的多级匹配：给 search 在文本里找唯一的位置并算出要写进去的 replace，找不到时给出最接近的候选。

全是纯函数；输入的文本须是 LF 换行（先过 text_files.normalize_newlines）。"""
from __future__ import annotations

import bisect
import difflib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from .text_files import normalize_newlines


@dataclass(frozen=True)
class Level:
    key: str
    label: str


EXACT = Level("exact", "精确匹配")
INDENTATION = Level("indentation", "缩进弹性")
LINE_TRIMMED = Level("line_trimmed", "逐行忽略首尾空白")
WHITESPACE = Level("whitespace", "空白归一")
UNESCAPED = Level("unescaped", "转义归一")
BLANK_TRIMMED = Level("blank_trimmed", "首尾空行修剪")
ANCHOR = Level("anchor", "锚点块")

# 依次尝试，第一个有命中的级别说了算。缩进弹性的命中条件是逐行去首尾空白的子集，排在它后面就永远轮不到，
# replace 也就没机会按目标处的缩进重排，所以提到它前面。
LEVELS: tuple[Level, ...] = (EXACT, INDENTATION, LINE_TRIMMED, WHITESPACE, UNESCAPED, BLANK_TRIMMED, ANCHOR)

ANCHOR_MIN_SIMILARITY = 0.65
_ANCHOR_LINE_SLACK_RATIO = 0.25
# 命中范围（空白折叠后）超过 search 的这么多倍、且多出这么多字符，或者跨的行数远多于 search，就当张冠李戴拒掉
_DISPROPORTION_RATIO = 2
_DISPROPORTION_MIN_EXTRA_CHARS = 200
# 命中超过这么多处就不再往下数：这么泛的 search 本来也定位不了
HIT_LIMIT = 10_000
_CANDIDATE_LINES_LIMIT = 20
# 逐字编辑距离是平方复杂度，相似度只比每行前这么多字符
_SIMILARITY_LINE_CAP = 120
# 找最接近的片段只在失败时做：先按整行相等投票，一行都对不上才逐行模糊比，模糊比只扫前这么多行
_CLOSEST_FUZZY_SCAN_LINES = 5000
_CLOSEST_KEY_LINES = 3
_CLOSEST_LINE_MIN_RATIO = 0.6
_CLOSEST_TOP_WINDOWS = 3
_CLOSEST_COMPARE_LINES = 40
CLOSEST_MIN_SIMILARITY = 0.35

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\", "`": "`", "$": "$"}
_ESCAPE_RE = re.compile(r"\\([ntr\"'\\`$])")


@dataclass(frozen=True)
class Match:
    """命中的区间 [start, end) 和要写进去的文本（已按命中处调整过缩进、边界换行）。"""

    start: int
    end: int
    replacement: str
    level: Level
    via: Level | None = None
    notes: tuple[str, ...] = ()
    similarity: float | None = None

    @property
    def label(self) -> str:
        return level_label(self.level, self.via)


@dataclass(frozen=True)
class Closest:
    """最接近 search 的一段：first / last 是当时内容里的行下标（0 起，含 last）。"""

    first: int
    last: int
    similarity: float


@dataclass(frozen=True)
class Failure:
    """reason：empty_search / not_found / ambiguous / out_of_range / no_match_after_start / disproportionate / too_many。

    candidate_lines 已换算成调用方的行号口径（读文件时的行号）；closest 是当时内容里的行下标。
    """

    reason: str
    message: str
    level: Level | None = None
    via: Level | None = None
    candidate_lines: tuple[int, ...] = ()
    total: int = 0
    closest: Closest | None = None


@dataclass(frozen=True)
class DiffRequest:
    index: int
    search: str
    replace: str
    start_line: int | None = None


@dataclass(frozen=True)
class AppliedDiff:
    index: int
    match: Match


@dataclass(frozen=True)
class FailedDiff:
    """content 是这个 diff 失败时的内存内容；line_delta 是此前各 diff 累计的行数变化，候选片段的行号要据此换算。"""

    index: int
    failure: Failure
    content: str
    line_delta: int


@dataclass(frozen=True)
class DiffOutcome:
    """spans：每个已应用的 diff 在最终内容里改动后的区间 [start, end)。"""

    original: str
    content: str
    applied: tuple[AppliedDiff, ...]
    failed: tuple[FailedDiff, ...]
    spans: dict[int, tuple[int, int]]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def changed(self) -> bool:
        return self.content != self.original


@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    line: int
    replacement: str
    notes: tuple[str, ...] = ()
    similarity: float | None = None
    level: Level | None = None


@dataclass(frozen=True)
class _Block:
    """按整行比的 search：开头、结尾各一个换行记成边界标记，不算空行。"""

    lines: tuple[str, ...]
    lead_newline: bool
    trail_newline: bool


class _Text:
    """content 的行视图：每行的起始偏移，按需缓存归一化之后的各行。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.lines = content.split("\n")
        # 以换行结尾时 split 多出来的那个空串不是一行
        self.count = len(self.lines) - 1 if self.lines[-1] == "" else len(self.lines)
        starts: list[int] = []
        offset = 0
        for line in self.lines:
            starts.append(offset)
            offset += len(line) + 1
        self.starts = starts
        self._keyed: dict[str, list[str]] = {}

    def keyed(self, name: str, normalize: Callable[[str], str]) -> list[str]:
        cached = self._keyed.get(name)
        if cached is None:
            cached = self._keyed[name] = [normalize(line) for line in self.lines[: self.count]]
        return cached

    def line_index(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset) - 1

    def line_end(self, index: int) -> int:
        return self.starts[index] + len(self.lines[index])


def level_label(level: Level, via: Level | None = None) -> str:
    return f"{level.label}+{via.label}" if via is not None else level.label


def line_range(content: str, start: int, end: int) -> tuple[int, int]:
    """区间 [start, end) 在 content 里占的行（1 起，含尾）；空区间落在 start 所在的那一行。"""
    first = content.count("\n", 0, start) + 1
    last = first + content.count("\n", start, max(start, end - 1))
    return first, last


def _collapse(line: str) -> str:
    return " ".join(line.split())


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _common_indent(lines: Sequence[str]) -> str:
    indents = [_leading_ws(line) for line in lines if line.strip()]
    return os.path.commonprefix(indents) if indents else ""


def _indent_shift(search_lines: Sequence[str], window_lines: Sequence[str]) -> tuple[str, str] | None:
    """search 和命中块的缩进是不是整体平移：是就返回 (search 的公共缩进, 命中块的公共缩进)，否则 None。"""
    search_indent = _common_indent(search_lines)
    window_indent = _common_indent(window_lines)
    for search_line, window_line in zip(search_lines, window_lines):
        if not search_line.strip() or not window_line.strip():
            continue
        if _leading_ws(search_line)[len(search_indent):] != _leading_ws(window_line)[len(window_indent):]:
            return None
    return search_indent, window_indent


def _shift_shallow(line: str, from_indent: str, to_indent: str) -> str:
    """比 search 的公共缩进还浅的行：三处缩进都是同一种字符时按字符数平移，否则不动。"""
    own = _leading_ws(line)
    chars = set(own + from_indent + to_indent)
    if len(chars) != 1:
        return line
    width = max(0, len(own) + len(to_indent) - len(from_indent))
    return chars.pop() * width + line[len(own):]


def _reindent(text: str, from_indent: str, to_indent: str) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
        elif line.startswith(from_indent):
            out.append(to_indent + line[len(from_indent):])
        else:
            out.append(_shift_shallow(line, from_indent, to_indent))
    return "\n".join(out)


def _block(text: str) -> _Block:
    lead = text.startswith("\n")
    if lead:
        text = text[1:]
    trail = text.endswith("\n")
    if trail:
        text = text[:-1]
    return _Block(tuple(text.split("\n")), lead, trail)


def _blank_prefix(lines: Sequence[str]) -> int:
    count = 0
    for line in lines:
        if line.strip():
            break
        count += 1
    return count


def _core(search: str, replace: str) -> tuple[str, str, bool]:
    """去掉首尾的整行空白和边上的单个换行后的 search，replace 对应地去掉同样多的空行和边界换行。

    第三项表示 search 有没有空行被去掉；search 全是空白时返回空串。
    """
    block = _block(search)
    lines = list(block.lines)
    lead = _blank_prefix(lines)
    if lead == len(lines):
        return "", replace, False
    trail = _blank_prefix(lines[::-1])
    core_lines = lines[lead:len(lines) - trail]
    rest = replace
    if block.lead_newline and rest.startswith("\n"):
        rest = rest[1:]
    if block.trail_newline and rest.endswith("\n"):
        rest = rest[:-1]
    replace_lines = rest.split("\n")
    replace_lines = replace_lines[min(lead, _blank_prefix(replace_lines)):]
    cut = min(trail, _blank_prefix(replace_lines[::-1]))
    if cut:
        replace_lines = replace_lines[:len(replace_lines) - cut]
    return "\n".join(core_lines), "\n".join(replace_lines), bool(lead or trail)


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda found: _ESCAPES[found.group(1)], text)


def _trim_like(search: str, replace: str) -> str:
    """search 首尾那段空白没算进命中范围，replace 里同样的首尾空白也去掉，免得写进去多出一截。"""
    lead = _leading_ws(search)
    trail = search[len(search.rstrip()):]
    if lead and replace.startswith(lead):
        replace = replace[len(lead):]
    if trail and replace.endswith(trail):
        replace = replace[: len(replace) - len(trail)]
    return replace


def _substring_hits(text: _Text, needle: str, replacement: str) -> list[_Hit]:
    # 不重叠地往后找，和原来按 str.count 判唯一的口径一致
    hits: list[_Hit] = []
    position = 0
    while len(hits) <= HIT_LIMIT:
        index = text.content.find(needle, position)
        if index < 0:
            break
        hits.append(_Hit(index, index + len(needle), text.line_index(index), replacement))
        position = index + max(1, len(needle))
    return hits


def _line_hit(
    text: _Text, index: int, block: _Block, replace: str, shift: tuple[str, str] | None, *, relabel: bool,
) -> _Hit:
    count = len(block.lines)
    identical = tuple(text.lines[index:index + count]) == block.lines
    start = text.starts[index]
    end = text.line_end(index + count - 1)
    replacement = replace
    notes: list[str] = []
    if shift is None:
        notes.append("命中处的缩进和 search 不一致，replace 照原样写入，请核对缩进")
    elif shift[0] != shift[1]:
        replacement = _reindent(replacement, *shift)
        notes.append("replace 已按命中处的缩进重排")
    if block.lead_newline:
        if start > 0:
            start -= 1
        elif replacement.startswith("\n"):
            replacement = replacement[1:]
    if block.trail_newline:
        if end < len(text.content):
            end += 1
        elif replacement.endswith("\n"):
            replacement = replacement[:-1]
    # 缩进弹性这一级命中、缩进其实没变时按实际差别报（只差文件边上的换行算精确，只差行尾空白算逐行去首尾空白），免得模型以为缩进动过
    level: Level | None = None
    if relabel and shift is not None and shift[0] == shift[1]:
        level = EXACT if identical else LINE_TRIMMED
    return _Hit(start, end, index, replacement, tuple(notes), level=level)


def _window_hits(text: _Text, block: _Block, replace: str, comparator: Level) -> list[_Hit]:
    count = len(block.lines)
    if count == 0 or count > text.count:
        return []
    if comparator is WHITESPACE:
        name, normalize = "collapse", _collapse
    else:
        name, normalize = "strip", str.strip
    keyed = text.keyed(name, normalize)
    target = [normalize(line) for line in block.lines]
    first = target[0]
    hits: list[_Hit] = []
    for index in range(text.count - count + 1):
        if keyed[index] != first or keyed[index:index + count] != target:
            continue
        shift = _indent_shift(block.lines, text.lines[index:index + count])
        if comparator is INDENTATION and shift is None:
            continue
        hits.append(_line_hit(text, index, block, replace, shift, relabel=comparator is INDENTATION))
        if len(hits) > HIT_LIMIT:
            break
    return hits


def _token_hits(text: _Text, search: str, replace: str) -> list[_Hit]:
    """单行 search 在一行之内按词序找，词之间的空白长短不论，命中范围从第一个词到最后一个词。

    多行的 search 只按整行折叠空白比（_window_hits）：允许跨行的话首尾空行也被当成空白吞掉，「首尾空行修剪」那一级就永远轮不到了。
    """
    core = search.strip()
    tokens = core.split()
    if not tokens or "\n" in core:
        return []
    pattern = re.compile(r"[^\S\n]+".join(re.escape(token) for token in tokens))
    replacement = _trim_like(search, replace)
    hits: list[_Hit] = []
    for found in pattern.finditer(text.content):
        hits.append(_Hit(found.start(), found.end(), text.line_index(found.start()), replacement))
        if len(hits) > HIT_LIMIT:
            break
    return hits


def _levenshtein(a: str, b: str) -> int:
    # 共同的前缀、后缀不影响编辑距离，先去掉：只改了一两处的行几乎不用算
    prefix = len(os.path.commonprefix([a, b]))
    a, b = a[prefix:], b[prefix:]
    suffix = len(os.path.commonprefix([a[::-1], b[::-1]]))
    if suffix:
        a, b = a[:-suffix], b[:-suffix]
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """Levenshtein 比率：1 - 编辑距离 / 较长者的长度。"""
    a = a[:_SIMILARITY_LINE_CAP]
    b = b[:_SIMILARITY_LINE_CAP]
    if a == b:
        return 1.0
    longest = max(len(a), len(b))
    return 1.0 - _levenshtein(a, b) / longest


def block_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """两组（已 strip 的）行的相似度。行数相同时逐行比；不同时先按行对齐，没对上的行算 0。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) == len(b):
        return sum(similarity(x, y) for x, y in zip(a, b)) / len(a)
    total = 0.0
    matcher = difflib.SequenceMatcher(None, list(a), list(b), autojunk=False)
    for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if tag == "equal":
            total += a_end - a_start
        elif tag == "replace":
            total += sum(
                similarity(a[a_start + k], b[b_start + k])
                for k in range(min(a_end - a_start, b_end - b_start))
            )
    return total / max(len(a), len(b))


def _anchor_hits(text: _Text, lines: Sequence[str], replace: str) -> list[_Hit]:
    """首行和末行 strip 后精确相等，中间按相似度判断；块的行数和 search 相差不超过约四分之一。"""
    count = len(lines)
    if count < 3:
        return []
    first = lines[0].strip()
    last = lines[-1].strip()
    if not first or not last:
        return []
    middle = [line.strip() for line in lines[1:-1]]
    slack = max(1, round(count * _ANCHOR_LINE_SLACK_RATIO))
    stripped = text.keyed("strip", str.strip)
    hits: list[_Hit] = []
    for i in range(text.count):
        if stripped[i] != first:
            continue
        best: tuple[tuple[float, int], int, float] | None = None
        low = i + max(2, count - 1 - slack)
        high = min(text.count - 1, i + count - 1 + slack)
        for j in range(low, high + 1):
            if stripped[j] != last:
                continue
            score = block_similarity(middle, stripped[i + 1:j])
            if score < ANCHOR_MIN_SIMILARITY:
                continue
            rank = (score, -abs((j - i + 1) - count))
            if best is None or rank > best[0]:
                best = (rank, j, score)
        if best is None:
            continue
        _rank, j, score = best
        window = text.lines[i:j + 1]
        shift = _indent_shift([lines[0], lines[-1]], [window[0], window[-1]])
        replacement = replace
        notes = [f"中间行和 search 不完全相同（相似度 {score:.2f}），按首末行锚定的整块替换"]
        if shift is None:
            notes.append("命中处的缩进和 search 不一致，replace 照原样写入，请核对缩进")
        elif shift[0] != shift[1]:
            replacement = _reindent(replacement, *shift)
            notes.append("replace 已按命中处的缩进重排")
        hits.append(_Hit(text.starts[i], text.line_end(j), i, replacement, tuple(notes), similarity=score))
        if len(hits) > HIT_LIMIT:
            break
    return hits


def _disproportionate(search: str, matched: str) -> bool:
    if matched.count("\n") > _DISPROPORTION_RATIO * search.count("\n") + 2:
        return True
    search_size = len(_collapse(search))
    matched_size = len(_collapse(matched))
    return (
        matched_size > _DISPROPORTION_RATIO * search_size
        and matched_size - search_size > _DISPROPORTION_MIN_EXTRA_CHARS
    )


_Finder = Callable[[_Text], list[_Hit]]


def _ladder(
    search: str, replace: str, level: Level | None = None, *, whole_lines: bool = False,
) -> Iterator[tuple[Level, Level | None, _Finder, str]]:
    """精确 → 缩进弹性 → 逐行去首尾空白 → 空白归一。给了 level 时整组算作这一级，via 记具体的比法。

    whole_lines：只按整行比，不在行中间找子串（search 本来带着整行结构时用）。
    """
    block = _block(search)
    comparators: list[tuple[Level, _Finder]] = []
    if not whole_lines:
        comparators.append((EXACT, lambda text: _substring_hits(text, search, replace)))
    comparators.append((INDENTATION, lambda text: _window_hits(text, block, replace, INDENTATION)))
    comparators.append((LINE_TRIMMED, lambda text: _window_hits(text, block, replace, LINE_TRIMMED)))
    if whole_lines:
        comparators.append((WHITESPACE, lambda text: _window_hits(text, block, replace, WHITESPACE)))
    else:
        comparators.append((
            WHITESPACE,
            lambda text: _window_hits(text, block, replace, WHITESPACE) or _token_hits(text, search, replace),
        ))
    for comparator, finder in comparators:
        if level is None:
            yield comparator, None, finder, search
        else:
            yield level, comparator, finder, search


def _steps(search: str, replace: str) -> Iterator[tuple[Level, Level | None, _Finder, str]]:
    yield from _ladder(search, replace)
    unescaped = normalize_newlines(_unescape(search))
    unescaped_replace = normalize_newlines(_unescape(replace))
    if unescaped != search:
        yield from _ladder(unescaped, unescaped_replace, UNESCAPED)
    core_search, core_replace, trimmed = _core(search, replace)
    if trimmed and core_search:
        yield from _ladder(core_search, core_replace, BLANK_TRIMMED, whole_lines=True)
    if core_search.count("\n") < 2 and unescaped != search:
        core_search, core_replace, _trimmed = _core(unescaped, unescaped_replace)
    if core_search:
        anchor_lines = core_search.split("\n")
        anchor_replace = core_replace
        yield ANCHOR, None, lambda text: _anchor_hits(text, anchor_lines, anchor_replace), core_search


def _join_lines(lines: Sequence[int], total: int) -> str:
    shown = "、".join(str(line) for line in lines)
    return f"{shown} 等" if total > len(lines) else shown


def _to_match(hit: _Hit, level: Level, via: Level | None, extra_note: str = "") -> Match:
    notes = hit.notes + ((extra_note,) if extra_note else ())
    if hit.level is not None:
        if via is None:
            level = hit.level
        else:
            via = hit.level
    return Match(hit.start, hit.end, hit.replacement, level, via, notes, hit.similarity)


def _decide(
    text: _Text, level: Level, via: Level | None, hits: list[_Hit], start_line: int | None, line_delta: int,
) -> Match | Failure:
    label = level_label(level, via)
    if len(hits) > HIT_LIMIT:
        return Failure(
            "too_many", f"search 能匹配的地方超过 {HIT_LIMIT} 处（{label}），换一段更长、更独特的 search",
            level, via, total=len(hits),
        )
    if len(hits) == 1:
        return _to_match(hits[0], level, via)
    reported = [hit.line + 1 - line_delta for hit in hits]
    shown = tuple(reported[:_CANDIDATE_LINES_LIMIT])
    lines_text = _join_lines(shown, len(hits))
    if start_line is None:
        return Failure(
            "ambiguous",
            f"search 能匹配 {len(hits)} 处（{label}），起始行：{lines_text}；用 start_line 指定其中一处",
            level, via, shown, len(hits),
        )
    if start_line < 1 or start_line > len(text.lines):
        return Failure(
            "out_of_range",
            f"start_line {start_line - line_delta} 超出了文件的行数；search 能匹配 {len(hits)} 处（{label}），起始行：{lines_text}",
            level, via, shown, len(hits),
        )
    for hit, line in zip(hits, reported):
        if hit.line >= start_line - 1:
            return _to_match(hit, level, via, f"search 能匹配 {len(hits)} 处，按 start_line 用了第 {line} 行起的那处")
    return Failure(
        "no_match_after_start",
        f"第 {start_line - line_delta} 行及之后没有匹配；search 能匹配的起始行：{lines_text}（{label}）",
        level, via, shown, len(hits),
    )


def find_match(
    content: str, search: str, replace: str, start_line: int | None = None, *, line_delta: int = 0,
) -> Match | Failure:
    """按级别依次找，第一个有命中的级别说了算：唯一就用；多处时按 start_line 取它及之后的第一处，取不到报候选。

    start_line 是换算到当前内容之后的行号；line_delta 只用来把报给模型的行号换回它读文件时的口径。
    唯一命中时 start_line 不起作用，过期的行号也不影响。
    """
    search = normalize_newlines(search)
    replace = normalize_newlines(replace)
    if not search:
        return Failure("empty_search", "search 是空的")
    text = _Text(content)
    rejected: tuple[Level, Level | None, _Hit, str] | None = None
    for level, via, finder, variant in _steps(search, replace):
        hits = finder(text)
        if not hits:
            continue
        if not (level is EXACT and via is None):
            kept = [hit for hit in hits if not _disproportionate(variant, text.content[hit.start:hit.end])]
            if not kept:
                if rejected is None:
                    rejected = (level, via, hits[0], variant)
                continue
            hits = kept
        return _decide(text, level, via, hits, start_line, line_delta)
    if rejected is not None:
        level, via, hit, variant = rejected
        first, last = line_range(content, hit.start, hit.end)
        return Failure(
            "disproportionate",
            f"{level_label(level, via)}找到的第 {first - line_delta}-{last - line_delta} 行有 "
            f"{hit.end - hit.start} 字符，search 只有 {len(variant)} 字符，范围不成比例，已拒绝",
            level, via, (first - line_delta,), 1,
        )
    return Failure(
        "not_found", "没有找到匹配的内容（精确和各级模糊匹配都没有命中）", closest=_closest(text, search),
    )


def _exact_votes(stripped: Sequence[str], wanted: Sequence[str]) -> dict[int, float]:
    """search 的每一行在文件里整行（strip 后）出现在哪，就给对应的窗口起点投一票；越常见的行票越轻。"""
    wanted_lines = {line for line in wanted if line}
    positions: defaultdict[str, list[int]] = defaultdict(list)
    for index, line in enumerate(stripped):
        if line in wanted_lines:
            positions[line].append(index)
    votes: defaultdict[int, float] = defaultdict(float)
    for offset, line in enumerate(wanted):
        found = positions.get(line)
        if not line or not found:
            continue
        for index in found:
            votes[index - offset] += 1.0 / len(found)
    return votes


def _fuzzy_votes(stripped: Sequence[str], wanted: Sequence[str]) -> dict[int, float]:
    """拿 search 里最长的几行逐行找相近的行投票。"""
    keys = sorted(
        ((offset, line) for offset, line in enumerate(wanted) if line),
        key=lambda item: (-len(item[1]), item[0]),
    )[:_CLOSEST_KEY_LINES]
    votes: defaultdict[int, float] = defaultdict(float)
    for offset, key in keys:
        matcher = difflib.SequenceMatcher(None, autojunk=False)
        matcher.set_seq2(key)
        for index, line in enumerate(stripped[:_CLOSEST_FUZZY_SCAN_LINES]):
            if not line:
                continue
            matcher.set_seq1(line)
            if matcher.real_quick_ratio() < _CLOSEST_LINE_MIN_RATIO or matcher.quick_ratio() < _CLOSEST_LINE_MIN_RATIO:
                continue
            score = similarity(line, key)
            if score >= _CLOSEST_LINE_MIN_RATIO:
                votes[index - offset] += score
    return votes


def _closest(text: _Text, search: str) -> Closest | None:
    """投票定几个候选窗口的起点，再在票数最高的几个窗口里按整块相似度挑最像的。"""
    core_search, _replace, _trimmed = _core(search, "")
    if not core_search or text.count == 0:
        return None
    wanted = [line.strip() for line in core_search.split("\n")]
    stripped = text.keyed("strip", str.strip)
    votes = _exact_votes(stripped, wanted) or _fuzzy_votes(stripped, wanted)
    if not votes:
        return None
    best: Closest | None = None
    compared = wanted[:_CLOSEST_COMPARE_LINES]
    for start, _votes in sorted(votes.items(), key=lambda item: (-item[1], item[0]))[:_CLOSEST_TOP_WINDOWS]:
        first = max(0, start)
        last = min(text.count, first + len(wanted)) - 1
        if last < first:
            continue
        score = block_similarity(compared, stripped[first:min(last + 1, first + len(compared))])
        if best is None or score > best.similarity:
            best = Closest(first, last, score)
    if best is None or best.similarity < CLOSEST_MIN_SIMILARITY:
        return None
    return best


def _shift_spans(spans: dict[int, tuple[int, int]], start: int, end: int, length: int) -> None:
    """前面 diff 改出来的区间跟着这次替换挪位置；被这次替换盖到的，并成一段。"""
    delta = length - (end - start)
    for index, (span_start, span_end) in list(spans.items()):
        if span_end <= start:
            continue
        if span_start >= end:
            spans[index] = (span_start + delta, span_end + delta)
        else:
            spans[index] = (min(span_start, start), max(span_end, end) + delta)


def apply_diffs(content: str, requests: Sequence[DiffRequest]) -> DiffOutcome:
    """在内存里依次应用；失败的跳过，后面的照常在已应用的结果上接着试，好一次报出全部失败。

    start_line 按读文件时的行号给，前面 diff 增删的行数累计在 line_delta 里补上。
    """
    original = content
    applied: list[AppliedDiff] = []
    failed: list[FailedDiff] = []
    spans: dict[int, tuple[int, int]] = {}
    line_delta = 0
    for request in requests:
        adjusted = None if request.start_line is None else request.start_line + line_delta
        result = find_match(content, request.search, request.replace, adjusted, line_delta=line_delta)
        if isinstance(result, Failure):
            failed.append(FailedDiff(request.index, result, content, line_delta))
            continue
        replaced = content[result.start:result.end]
        content = content[:result.start] + result.replacement + content[result.end:]
        _shift_spans(spans, result.start, result.end, len(result.replacement))
        spans[request.index] = (result.start, result.start + len(result.replacement))
        line_delta += result.replacement.count("\n") - replaced.count("\n")
        applied.append(AppliedDiff(request.index, result))
    return DiffOutcome(original, content, tuple(applied), tuple(failed), spans)
