"""超长工具输出：完整文本按会话落盘，上下文里只放开头和结尾的预览，read_tool_output 按行读回。

存储位置 data/tool_outputs/<会话目录>/<ref>.txt，会话键和 context archive 用同一个。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int, load_runtime_config

logger = logging.getLogger(__name__)

READ_TOOL_OUTPUT = "read_tool_output"

DEFAULT_READ_LINES = 200
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_SEARCH_HITS = 50
SEARCH_CONTEXT_LINES = 2
MAX_PATTERN_CHARS = 500

_MIN_ROWS_BUDGET = 200
_PATTERN_LABEL_CHARS = 100
_SEARCH_BLOCK_ROWS = 2 * SEARCH_CONTEXT_LINES + 1
# 行号前缀、截断说明和换行加起来的上限
_SEARCH_ROW_OVERHEAD = 80
_MIN_SEARCH_LINE_CHARS = 40

_HEAD_SHARE = 0.6
_MIN_PREVIEW_BODY = 200
_MIN_EXCERPT_BODY = 120
_MAX_PINNED_LINE = 400
_REF = re.compile(r"to_[0-9a-f]{24}\Z")
_REF_MENTION = re.compile(r"ref=to_[0-9a-f]{24}")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_SECONDS_PER_DAY = 86400
# 最近一轮超出保护预算时请求视图只给它的工具结果留这么多字，预览或分页再长，尾部连同下一页的位置都会被截掉
LATEST_ROUND_VIEW_CHARS = 8000
# 事件写进事件日志、推给控制台，只供展示：超长的字符串换成摘录，单条事件不至于带上几 MB 的工具输出
EVENT_TEXT_CHARS = 16_000
# 逐个字符串截完，一条工具结果在事件里按 JSON 算还超过这么多字的，是条目多的结构化结果，只留正文摘录
EVENT_RESULT_CHARS = 64_000
_EVENT_MAX_DEPTH = 16
_EVENT_RESULT_KEYS = ("ok", "success", "error", "cancelled", "status")


@dataclass(frozen=True)
class ToolOutputLimits:
    max_inline_chars: int
    view_chars: int
    retention_days: int


def tool_output_limits(runtime_cfg: Any) -> ToolOutputLimits:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    max_inline = get_int(cfg, "engine.tool_output.max_inline_chars", 32000, min_value=2000, max_value=2_000_000)
    view = get_int(
        cfg, "engine.tool_output.view_chars", LATEST_ROUND_VIEW_CHARS,
        min_value=1000, max_value=min(max_inline, LATEST_ROUND_VIEW_CHARS),
    )
    retention = get_int(cfg, "engine.tool_output.retention_days", 7, min_value=1, max_value=3650)
    return ToolOutputLimits(max_inline_chars=max_inline, view_chars=view, retention_days=retention)


@dataclass(frozen=True)
class SavedOutput:
    ref: str
    chars: int
    lines: int


class ToolOutputNotFound(FileNotFoundError):
    """ref 不在这个会话名下，或者已经过了保留期。"""


def is_valid_ref(ref: Any) -> bool:
    return isinstance(ref, str) and _REF.fullmatch(ref) is not None


def mentioned_refs(*texts: Any) -> list[str]:
    """文本里以 ref=to_… 引用到的超长输出，按第一次出现的顺序去重。"""
    found: list[str] = []
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for match in _REF_MENTION.finditer(text):
            ref = match.group(0)[len("ref="):]
            if ref not in found:
                found.append(ref)
    return found


def session_dir_name(session_id: str) -> str:
    """会话键换成目录名：只留安全字符，再接一段原值的哈希，不同会话不会撞到同一个目录。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("tool output session id is empty")
    readable = _UNSAFE_NAME_CHARS.sub("_", sid).strip("_-")[:48] or "session"
    digest = hashlib.sha256(sid.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{readable}_{digest}"


def count_lines(text: str) -> int:
    return len(text.splitlines())


class ToolOutputStore:
    def __init__(self, workspace_root: Path | str, *, retention_days: int | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.root = self.workspace_root / "data" / "tool_outputs"
        self._retention_days = retention_days

    @property
    def retention_days(self) -> int:
        if self._retention_days is not None:
            return max(1, int(self._retention_days))
        return tool_output_limits(load_runtime_config(self.workspace_root)).retention_days

    def _cutoff(self) -> float:
        return time.time() - self.retention_days * _SECONDS_PER_DAY

    def session_dir(self, session_id: str) -> Path:
        directory = self.root / session_dir_name(session_id)
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("tool output directory escapes data/tool_outputs")
        return directory

    def path_for(self, session_id: str, ref: str) -> Path:
        if not is_valid_ref(ref):
            raise ValueError(f"invalid tool output ref: {ref!r}")
        return self.session_dir(session_id) / f"{ref}.txt"

    def save(self, session_id: str, text: str) -> SavedOutput:
        directory = self.session_dir(session_id)
        payload = text.encode("utf-8", "surrogatepass")
        for _attempt in range(3):
            directory.mkdir(parents=True, exist_ok=True)
            ref = f"to_{secrets.token_hex(12)}"
            target = directory / f"{ref}.txt"
            if target.exists():
                continue
            try:
                handle, temporary = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
            except FileNotFoundError:
                # 清理任务刚好把这个空目录收走了，重建再写
                continue
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(payload)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
            return SavedOutput(ref=ref, chars=len(text), lines=count_lines(text))
        raise OSError(f"could not store tool output under {directory}")

    def load(self, session_id: str, ref: str) -> str:
        path = self.path_for(session_id, ref)
        try:
            if path.stat().st_mtime <= self._cutoff():
                raise ToolOutputNotFound(ref)
            data = path.read_bytes()
        except ToolOutputNotFound:
            raise
        except FileNotFoundError:
            raise ToolOutputNotFound(ref) from None
        try:
            return data.decode("utf-8", "surrogatepass")
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace")

    def purge(self, *, dry_run: bool = False) -> int:
        """删掉过了保留期的输出（含写到一半留下的临时文件），返回过期文件数。"""
        if not self.root.is_dir():
            return 0
        cutoff = self._cutoff()
        expired = 0
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            for path in sorted(directory.iterdir()):
                try:
                    if not path.is_file() or path.stat().st_mtime > cutoff:
                        continue
                except FileNotFoundError:
                    continue
                expired += 1
                if dry_run:
                    logger.info("[dry-run] would delete file %s", path)
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("failed to remove tool output %s", path, exc_info=True)
            if not dry_run:
                _remove_if_empty(directory)
        return expired

    def adopt(self, source_session_id: str, target_session_id: str, ref: str) -> bool:
        """把 source 会话名下的一份输出复制到 target 会话名下，ref 和修改时间不变，保留期照旧从原来那一刻算。

        target 已经有了、source 里找不到或过了保留期都不复制，返回 target 名下现在有没有这份。
        """
        if not is_valid_ref(ref):
            return False
        try:
            source = self.path_for(source_session_id, ref)
            target = self.path_for(target_session_id, ref)
        except ValueError:
            return False
        if source == target or target.exists():
            return target.exists()
        try:
            if source.stat().st_mtime <= self._cutoff():
                return False
        except FileNotFoundError:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".tmp")
        os.close(handle)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        except FileNotFoundError:
            Path(temporary).unlink(missing_ok=True)
            return False
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return True

    def purge_session(self, session_id: str) -> int:
        """会话被拆掉时连同它名下的全部输出一起删，返回删掉的文件数。"""
        try:
            directory = self.session_dir(session_id)
        except ValueError:
            return 0
        if not directory.is_dir():
            return 0
        removed = sum(1 for path in directory.iterdir() if path.is_file())
        shutil.rmtree(directory, ignore_errors=True)
        return removed


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def _line_starts(parts: list[str]) -> list[int]:
    starts: list[int] = []
    offset = 0
    for part in parts:
        starts.append(offset)
        offset += len(part)
    return starts


def _line_number(starts: list[int], index: int) -> int:
    return bisect.bisect_right(starts, index)


def _head_end(parts: list[str], budget: int) -> int:
    used = 0
    for part in parts:
        if used + len(part) > budget:
            break
        used += len(part)
    # 整行切下来连一半预算都用不到（遇上超长单行）时改按字符切，免得预览只剩零头
    return used if used >= budget // 2 else budget


def _tail_start(parts: list[str], total: int, budget: int) -> int:
    used = 0
    for part in reversed(parts):
        if used + len(part) > budget:
            break
        used += len(part)
    return total - (used if used >= budget // 2 else budget)


def _ended(text: str) -> str:
    return text if text.endswith(("\n", "\r")) else f"{text}\n"


def _omitted_marker(first: int, last: int, lines: int, chars: int) -> str:
    return f"[……中间省略第 {first}–{last} 行，共 {lines} 行、{chars} 字符……]"


def _preview_frame(saved: SavedOutput | None, chars: int, lines: int) -> tuple[str, str]:
    if saved is None:
        return (
            f"[输出过长（共 {chars} 字符 / {lines} 行），完整内容没能保存，下面只放开头和结尾]",
            f"[完整输出共 {chars} 字符 / {lines} 行，中间部分没能保存、读不回来；需要的话换更精确的参数重新调用]",
        )
    return (
        f"[输出过长，完整内容已保存为 ref={saved.ref}（共 {chars} 字符 / {lines} 行），下面只放开头和结尾]",
        f"[完整输出共 {chars} 字符 / {lines} 行，已保存为 ref={saved.ref}，可用 read_tool_output 按行读取或搜索]",
    )


def preview_text(text: str, view_chars: int, *, saved: SavedOutput | None) -> str:
    """开头约 60%、结尾约 40% 的预览，尽量在行边界切开，中间写明省略了哪些行。

    ref 在第一行也写一遍：请求视图会把较早的工具结果截到只剩开头。
    """
    parts = text.splitlines(keepends=True)
    total_chars = len(text)
    total_lines = len(parts)
    header, footer = _preview_frame(saved, total_chars, total_lines)
    marker_room = len(_omitted_marker(total_lines, total_lines, total_lines, total_chars))
    body = max(_MIN_PREVIEW_BODY, view_chars - len(header) - len(footer) - marker_room - 4)
    head_budget = int(body * _HEAD_SHARE)
    tail_budget = body - head_budget
    if total_chars <= body:
        return f"{header}\n{_ended(text)}{footer}"
    head_end = _head_end(parts, head_budget)
    tail_start = max(head_end, _tail_start(parts, total_chars, tail_budget))
    starts = _line_starts(parts)
    first = _line_number(starts, head_end)
    last = _line_number(starts, tail_start - 1)
    marker = _omitted_marker(first, last, last - first + 1, tail_start - head_end)
    return f"{header}\n{_ended(text[:head_end])}{marker}\n{_ended(text[tail_start:])}{footer}"


def _ref_pins(parts: list[str]) -> list[tuple[int, str]]:
    """截短时必须留下的 ref 行：(行号, 落进省略部分时另外补上的文字)，取第一处和最后一处。

    这一行本身很长（比如单行 JSON 里提到 ref）时只补 ref 本身，不然整行补回去等于没截。
    """
    hits = [index for index, part in enumerate(parts) if _REF_MENTION.search(part)]
    pins: list[tuple[int, str]] = []
    for index in dict.fromkeys(hits[:1] + hits[-1:]):
        line = parts[index]
        if len(line) > _MAX_PINNED_LINE:
            line = "[" + "，".join(dict.fromkeys(_REF_MENTION.findall(line))) + "]"
        pins.append((index, _ended(line)))
    return pins


def _inside_windows(starts: list[int], parts: list[str], index: int, head_end: int, tail_start: int) -> bool:
    begin = starts[index]
    return begin + len(parts[index]) <= head_end or begin >= tail_start


def _cut_marker(chars: int) -> str:
    return f"[……后面还有 {chars} 字符没有显示……]"


def _head_only(text: str, budget: int) -> str:
    keep = max(0, budget - len(_cut_marker(len(text))) - 1)
    if keep <= 0:
        return text[:max(0, budget)]
    return f"{text[:keep]}\n{_cut_marker(len(text) - keep)}"


def _excerpt_with(
    text: str, parts: list[str], starts: list[int], body: int, pins: list[tuple[int, str]],
) -> str | None:
    total = len(text)
    reserved = 0
    # 给落进中间的 ref 行让出位置后窗口变小，可能又有一行掉进中间，最多重算到每行都让过一次
    for _attempt in range(len(pins) + 1):
        room = body - reserved
        if room < _MIN_EXCERPT_BODY:
            return None
        head_budget = int(room * _HEAD_SHARE)
        head_end = _head_end(parts, head_budget)
        tail_start = max(head_end, _tail_start(parts, total, room - head_budget))
        stranded = [pin for pin in pins if not _inside_windows(starts, parts, pin[0], head_end, tail_start)]
        needed = sum(len(kept) for _index, kept in stranded)
        if needed <= reserved:
            break
        reserved = needed
    else:
        return None
    first = _line_number(starts, head_end)
    last = _line_number(starts, tail_start - 1)
    marker = _omitted_marker(first, last, last - first + 1, tail_start - head_end)
    head = text[:head_end]
    kept = "".join(kept for _index, kept in stranded)
    return f"{_ended(head) if head else ''}{kept}{marker}\n{text[tail_start:]}"


def excerpt_text(text: str, budget: int) -> str:
    """长文本截到 budget 字左右：开头约 60%、结尾约 40%，尽量在行边界切开，中间写明省略了哪些行。

    带 ref 的落盘提示行（第一处和最后一处）一定留下，读回全文只能靠它；预算小到放不下一段开头时只截开头。
    """
    if len(text) <= budget:
        return text
    parts = text.splitlines(keepends=True)
    starts = _line_starts(parts)
    marker_room = len(_omitted_marker(len(parts), len(parts), len(parts), len(text))) + 3
    pins = _ref_pins(parts)
    for keep in ([pins, pins[:1]] if len(pins) > 1 else [pins]):
        excerpt = _excerpt_with(text, parts, starts, budget - marker_room, keep)
        if excerpt is not None:
            return excerpt
    if pins:
        # 预算连 ref 提示行带一段开头都放不下：宁可超出预算也不能把读回全文的入口截掉
        index, kept = pins[0]
        return f"{kept}{_cut_marker(len(text) - len(parts[index]))}"
    return _head_only(text, budget)


def bounded_for_event(value: Any, limit: int = EVENT_TEXT_CHARS) -> tuple[Any, bool]:
    """value 放进事件用的样子：超过 limit 字的字符串换成摘录，其余原样；没截过就是原对象。第二项表示截没截过。"""
    return _bounded(value, limit, 0)


def _bounded(value: Any, limit: int, depth: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        return (excerpt_text(value, limit), True) if len(value) > limit else (value, False)
    if not isinstance(value, (dict, list, tuple)):
        return value, False
    if depth >= _EVENT_MAX_DEPTH:
        # 嵌套这么深已经没法逐层截，整段转成文本再看长短
        dumped = json.dumps(value, ensure_ascii=False, default=str)
        return (excerpt_text(dumped, limit), True) if len(dumped) > limit else (value, False)
    changed = False
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            out[key], clipped = _bounded(item, limit, depth + 1)
            changed = changed or clipped
        return (out, True) if changed else (value, False)
    items: list[Any] = []
    for item in value:
        bounded, clipped = _bounded(item, limit, depth + 1)
        items.append(bounded)
        changed = changed or clipped
    if not changed:
        return value, False
    return (tuple(items) if isinstance(value, tuple) else items), True


def tool_result_for_event(result: Any) -> tuple[Any, bool]:
    """工具结果放进 tool_call_end 事件用的样子：先逐个截长字符串；整体还超过 EVENT_RESULT_CHARS，
    就只留成败标记、报错、data.result 正文和附件，结构化的条目（递归列出的目录项之类）不进事件。"""
    bounded, clipped = bounded_for_event(result)
    if len(json.dumps(bounded, ensure_ascii=False, default=str)) <= EVENT_RESULT_CHARS:
        return bounded, clipped
    if not isinstance(bounded, dict):
        return excerpt_text(json.dumps(bounded, ensure_ascii=False, default=str), EVENT_TEXT_CHARS), True
    data = bounded.get("data") if isinstance(bounded.get("data"), dict) else {}
    slim: dict[str, Any] = {key: bounded[key] for key in _EVENT_RESULT_KEYS if key in bounded}
    slim_data: dict[str, Any] = {key: data[key] for key in ("result", "attachments") if key in data}
    slim_data["omittedForEvent"] = True
    slim["data"] = slim_data
    if len(json.dumps(slim, ensure_ascii=False, default=str)) > EVENT_RESULT_CHARS:
        # 附件清单本身就超了：正文已经截过，附件只能不带
        slim_data.pop("attachments", None)
    return slim, True


def inline_tool_output(
    workspace_root: Path | str | None,
    session_id: str,
    tool_name: str,
    text: str,
    limits: ToolOutputLimits,
) -> str:
    """一条工具结果进上下文的样子：没超阈值原样返回，超了落盘并换成预览。阻塞 I/O，调用方放到线程里。

    没有工作区时存不了，只给预览。
    """
    if len(text) <= limits.max_inline_chars:
        return text
    saved: SavedOutput | None = None
    # read_tool_output 自己的结果再落盘就成了套娃，只截不存
    if tool_name != READ_TOOL_OUTPUT and workspace_root is not None:
        try:
            saved = ToolOutputStore(workspace_root).save(session_id, text)
        except (OSError, ValueError):
            logger.warning("storing tool output failed: tool=%s session=%s", tool_name, session_id, exc_info=True)
    return preview_text(text, limits.view_chars, saved=saved)


@dataclass(frozen=True)
class ReadPage:
    text: str
    total_lines: int
    start_line: int
    end_line: int
    next_start_line: int | None


def _line_cap(char_budget: int) -> int:
    return max(200, min(MAX_LINE_CHARS, char_budget // 2))


def _clipped(line: str, cap: int) -> str:
    if len(line) <= cap:
        return line
    return f"{line[:cap]} …[本行共 {len(line)} 字符，只显示前 {cap} 字符；用 pattern 搜索能看到行内命中位置附近的内容]"


def _rows_budget(char_budget: int, widest_header: str) -> int:
    # 首行按数字最长的样子先占位，整页（首行加各行）才不会超过 char_budget
    return max(_MIN_ROWS_BUDGET, char_budget - len(widest_header) - 1)


def _read_header(ref: str, total: int, chars: int, start_line: int, end_line: int, next_start: int | None) -> str:
    position = f"下一页 start_line={next_start}" if next_start is not None else "已到末尾"
    return f"ref={ref}：共 {total} 行 {chars} 字符，本次第 {start_line}–{end_line} 行；{position}"


def read_lines(text: str, ref: str, *, start_line: int, max_lines: int, char_budget: int) -> ReadPage:
    start_line = max(1, start_line)
    max_lines = max(1, min(MAX_READ_LINES, max_lines))
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ReadPage(f"ref={ref}：这份输出是空的", 0, 0, 0, None)
    if start_line > total:
        raise ValueError(f"start_line={start_line} 超出范围：这份输出共 {total} 行")
    last_allowed = min(total, start_line + max_lines - 1)
    width = max(4, len(str(last_allowed)))
    budget = _rows_budget(
        char_budget, _read_header(ref, total, len(text), start_line, last_allowed, last_allowed + 1),
    )
    cap = _line_cap(budget)
    rows: list[str] = []
    used = 0
    for number in range(start_line, last_allowed + 1):
        row = f"{number:>{width}} | {_clipped(lines[number - 1], cap)}"
        if rows and used + len(row) + 1 > budget:
            break
        rows.append(row)
        used += len(row) + 1
    end_line = start_line + len(rows) - 1
    next_start = end_line + 1 if end_line < total else None
    header = _read_header(ref, total, len(text), start_line, end_line, next_start)
    return ReadPage("\n".join([header, *rows]), total, start_line, end_line, next_start)


class _Matcher:
    """pattern 当正则用；不是合法正则或正则没命中时，再按普通子串找。"""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        try:
            self.regex: re.Pattern[str] | None = re.compile(pattern)
        except re.error:
            self.regex = None

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


def _hit_row(number: int, line: str, width: int, cap: int, span: tuple[int, int]) -> str:
    if len(line) <= cap:
        return f"{number:>{width}}: {line}"
    begin = max(0, span[0] - cap // 4)
    end = min(len(line), begin + cap)
    snippet = ("…" if begin > 0 else "") + line[begin:end] + ("…" if end < len(line) else "")
    return f"{number:>{width}}: {snippet} [本行共 {len(line)} 字符，显示第 {begin + 1}–{end} 字符]"


def _pattern_label(pattern: str) -> str:
    label = repr(pattern)
    return label if len(label) <= _PATTERN_LABEL_CHARS else f"{label[:_PATTERN_LABEL_CHARS]}…"


def _search_header(
    ref: str, start_line: int, total: int, mode: str, pattern: str, hits: int, shown: int, next_start: int | None,
) -> str:
    header = f"ref={ref}：第 {start_line}–{total} 行里按{mode} {_pattern_label(pattern)} 命中 {hits} 行，这次列出 {shown} 行"
    if next_start is not None:
        header += f"；后面还有，用 start_line={next_start} 接着搜"
    return header


def _search_line_cap(rows_budget: int) -> int:
    # 第一组命中连同上下文总会列出来，单行上限得让整组放得进预算
    return max(_MIN_SEARCH_LINE_CHARS, min(MAX_LINE_CHARS, rows_budget // _SEARCH_BLOCK_ROWS - _SEARCH_ROW_OVERHEAD))


def search_lines(text: str, ref: str, *, pattern: str, start_line: int, char_budget: int) -> ReadPage:
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern 太长：最多 {MAX_PATTERN_CHARS} 个字符")
    start_line = max(1, start_line)
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ReadPage(f"ref={ref}：这份输出是空的", 0, 0, 0, None)
    if start_line > total:
        raise ValueError(f"start_line={start_line} 超出范围：这份输出共 {total} 行")
    matcher = _Matcher(pattern)
    spans: dict[int, tuple[int, int]] = {}
    for index in range(start_line - 1, total):
        span = matcher.span(lines[index])
        if span is not None:
            spans[index] = span
    hits = sorted(spans)
    if not hits:
        text_out = f"ref={ref}：第 {start_line}–{total} 行里没有匹配 {_pattern_label(pattern)} 的行（共 {total} 行）"
        return ReadPage(text_out, total, start_line, total, None)

    mode = "正则或子串" if matcher.regex is not None else "子串"
    budget = _rows_budget(
        char_budget, _search_header(ref, start_line, total, mode, pattern, len(hits), len(hits), total),
    )
    width = max(4, len(str(total)))
    cap = _search_line_cap(budget)
    rows: list[str] = []
    used = 0
    last_rendered = -1
    for hit in hits:
        if hit <= last_rendered:
            continue
        listed = bisect.bisect_right(hits, last_rendered)
        if listed >= MAX_SEARCH_HITS:
            break
        low = max(start_line - 1, hit - SEARCH_CONTEXT_LINES, last_rendered + 1)
        high = min(total - 1, hit + SEARCH_CONTEXT_LINES)
        # 命中挨得近时一段上下文会带进后面的命中，停在超出上限的那一个之前，它留给下一页
        if len(hits) > MAX_SEARCH_HITS and hits[MAX_SEARCH_HITS] <= high:
            high = hits[MAX_SEARCH_HITS] - 1
        block: list[str] = []
        if last_rendered >= 0 and low > last_rendered + 1:
            block.append("--")
        for index in range(low, high + 1):
            line = lines[index]
            if index in spans:
                block.append(_hit_row(index + 1, line, width, cap, spans[index]))
            else:
                block.append(f"{index + 1:>{width}}- {_clipped(line, cap)}")
        cost = sum(len(row) + 1 for row in block)
        if rows and used + cost > budget:
            break
        rows.extend(block)
        used += cost
        last_rendered = high

    shown = bisect.bisect_right(hits, last_rendered)
    pending = hits[shown:]
    next_start = pending[0] + 1 if pending else None
    header = _search_header(ref, start_line, total, mode, pattern, len(hits), shown, next_start)
    end_line = min(total, last_rendered + 1)
    return ReadPage("\n".join([header, *rows]), total, start_line, end_line, next_start)
