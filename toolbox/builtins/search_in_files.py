"""search_in_files — search (and replace) text in workspace files.

ripgrep (when installed) finds the files that match, otherwise a Python walk does; both honour .gitignore like ripgrep's
defaults, and line/column/context are always rendered by the same Python code. Replace mode claims each file with the
supervisor and writes through the edit journal."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int, load_runtime_config, parse_bool, switch_arg

from .. import edit_journal, file_state
from ..context import ToolContext, project_origin_of, project_root_of
from .._common import (
    display_namer,
    guard_external_read,
    request_guard,
    resolve_and_classify,
    run_process,
    safe_subprocess_env,
)
from ..ignore_rules import DEFAULT_IGNORED_DIRS, MAX_WALK_FILES, GlobError, gitignore_applies, glob_regex, walk_files
from ..syntax_check import EditChecks, run_edit_checks
from ..text_files import BOM, normalize_newlines
from ._edit_flow import settle_transaction, transact_text

logger = logging.getLogger(__name__)

_SKIP_DIRS = DEFAULT_IGNORED_DIRS | frozenset({
    # 运行时数据目录，里面是大块的 JSONL 和产物
    "conversations", "node_contexts", "artifacts", "logs",
    # 前端构建产物和缓存
    ".nuxt", ".output", ".turbo", ".cache", ".parcel-cache", "coverage",
})
_SKIP_DIR_PREFIXES = ("dist.backup", "backup.", ".backup")
_SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".whl", ".egg",
    ".jsonl", ".log", ".sqlite", ".sqlite3", ".db", ".bak", ".map",
})
# 按文件名结尾比，和传给 rg 的 !*.pyc 这类 glob 口径一致
_SKIP_FILE_ENDINGS = tuple(sorted(_SKIP_SUFFIXES | {".min.js"}))
_MAX_CANDIDATE_FILES = 10_000
_MAX_SCAN_BYTES = 256 * 1024 * 1024

# Hard limit on total chars returned in search results to prevent context explosion
_MAX_TOTAL_RESULT_CHARS = 50_000
_MAX_CONTEXT_LINE_CHARS = 500
_CLIPPED_MARK = "...<truncated>"

_DEFAULT_MAX_RESULTS = 100
_MAX_RESULTS_LIMIT = 10_000
_DEFAULT_MAX_FILES = 50
_MAX_FILES_LIMIT = 10_000

_RG_TIMEOUT_SEC = 30.0
# rg 只输出文件路径，这么多足够几十万个
_RG_OUTPUT_LIMIT_BYTES = 32 * 1024 * 1024
_RG_STDERR_LIMIT_BYTES = 64 * 1024
_RG_PATTERN_TYPE = "clonothpattern"
_GLOB_META = frozenset("*?[]{}\\,:")

_REPLACE_TOOL = "search_in_files"
REPLACE_SWITCH = "search_replace"
REPLACE_DISABLED_MESSAGE = (
    "这个节点不允许用 search_in_files 的 replace 模式改文件（节点设置 search_replace: false），只能用搜索模式"
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def replace_disabled(ctx: Any) -> bool:
    """节点 yaml 写了 search_replace: false 时不许用 replace 模式；经 ToolContext._node_extra 传进来，没写照常可用。"""
    extra = getattr(ctx, "_node_extra", None)
    if not isinstance(extra, Mapping) or REPLACE_SWITCH not in extra:
        return False
    # 写了却认不出的值按关掉算：这个开关是给只读节点用的，配错了宁可不让改
    return parse_bool(extra.get(REPLACE_SWITCH), False) is not True


@functools.lru_cache(maxsize=64)
def _double_star_regex(normalized: str) -> re.Pattern[str] | None:
    try:
        return glob_regex(normalized)
    except GlobError:
        return None


def _match_pattern(rel_path: str, pattern: str) -> bool:
    """Check if *rel_path* matches a glob *pattern*."""
    normalized = pattern.replace("\\", "/").strip()
    if normalized in ("**/*", "*"):
        return True
    rel = rel_path.replace("\\", "/")
    if fnmatch(rel, normalized):
        return True
    if "/" not in normalized:
        return fnmatch(rel.rsplit("/", 1)[-1], normalized)
    if normalized.startswith("**/") and fnmatch(rel, normalized[3:]):
        return True
    # fnmatch 里 src/**/*.py 至少要隔一层目录；** 也能是零层，和 glob、.gitignore 的口径一致
    regex = _double_star_regex(normalized) if "**" in normalized else None
    return regex is not None and regex.fullmatch(rel) is not None


def _get_context(lines: list[str], line_idx: int, n: int = 1) -> str:
    """Return ±n lines of context around *line_idx*, with line-number prefixes.

    Each line is capped at _MAX_CONTEXT_LINE_CHARS to prevent single-line
    files (e.g. JSONL) from blowing up the result.
    """
    start = max(0, line_idx - n)
    end = min(len(lines), line_idx + n + 1)
    parts = []
    for i in range(start, end):
        parts.append(f"{i + 1}: {_clip_line(lines[i])}")
    return "\n".join(parts)


def _line_col(text: str, pos: int) -> tuple[int, int]:
    """Return 1-based (line, column) for character offset *pos* in *text*."""
    line = text.count("\n", 0, pos) + 1
    last_nl = text.rfind("\n", 0, pos)
    col = pos - last_nl  # works even when last_nl == -1 → gives pos+1
    return line, col


def _clip_line(text: str) -> str:
    """超过 _MAX_CONTEXT_LINE_CHARS 的匹配或上下文行截到连同截断标记正好这么长。"""
    if len(text) <= _MAX_CONTEXT_LINE_CHARS:
        return text
    return text[:_MAX_CONTEXT_LINE_CHARS - len(_CLIPPED_MARK)] + _CLIPPED_MARK


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    # 失败也要带 data.result；cancelled、path 这类附加字段在顶层和 data 里各放一份
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    data.update(extra)
    response: dict[str, Any] = {"ok": False, "success": False, "error": text, "data": data}
    response.update(extra)
    return response


def _search_result_text(matches: list[dict[str, Any]], truncated: bool) -> str:
    lines = [f"{len(matches)} results found{' (truncated)' if truncated else ''}:"]
    for m in matches:
        lines.append(f"\n{m['file']}:{m['line']} | {m['match']}")
        if m.get("context"):
            for cl in str(m["context"]).splitlines():
                lines.append(f"  {cl}")
    return "\n".join(lines)


def _bounded_int(raw: Any, default: int, upper: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive integer") from None
    return max(1, min(upper, value))


def _aliased_switch(args: Mapping[str, Any], key: str, alias: str) -> bool:
    """驼峰和下划线两种写法都收，默认 false；报错时用模型实际写的那个名字。"""
    return switch_arg(args, key if key in args else alias, False)


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(_SKIP_DIR_PREFIXES)


@dataclass(frozen=True)
class _Request:
    regex: re.Pattern[str]
    query: str
    is_regex: bool
    ignore_case: bool
    pattern: str
    include_ignored: bool
    max_file_size: int


# ---------------------------------------------------------------------------
#  Candidate files: Python walk
# ---------------------------------------------------------------------------

class _Collector:
    """Python 这一路：按 os.walk 的顺序给出要搜的文件 (Path, 显示路径)。

    For paths under *project_root*, the display path is project-relative; under extra_roots it is absolute.
    遍历超过 MAX_WALK_FILES 个文件就停，limited 写明原因；读多少个、多少字节的上限由 _Reader 管，两条路一样。"""

    def __init__(
        self, root: Path, pattern: str, max_file_size: int, project_root: Path, *, gitignore: bool = True,
    ) -> None:
        self.root = root
        self.pattern = pattern
        self.max_file_size = max_file_size
        self.project_root = project_root
        self.gitignore = gitignore
        self.limited = ""

    def __iter__(self) -> Iterator[tuple[Path, str]]:
        name = display_namer(self.root, self.project_root)
        if self.root.is_file():
            # 直接点名的单个文件不看 pattern、跳过名单和 .gitignore
            try:
                if self.root.stat().st_size <= self.max_file_size:
                    yield self.root, name("")
            except OSError:
                pass
            return
        walked = 0
        for entry, walk_rel in walk_files(self.root, gitignore=self.gitignore, skip_dir=_skip_dir):
            walked += 1
            if walked > MAX_WALK_FILES:
                self.limited = f"遍历了 {MAX_WALK_FILES} 个文件就停了"
                return
            if entry.name.endswith(_SKIP_FILE_ENDINGS):
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if size > self.max_file_size:
                continue
            display = name(walk_rel)
            if not _match_pattern(display, self.pattern):
                continue
            yield Path(entry.path), display


def _collect_files(
    root: Path, pattern: str, max_file_size: int, project_root: Path, *, gitignore: bool = True,
) -> _Collector:
    """Iterable of (Path, display path) for the files to search under *root*, in os.walk order."""
    return _Collector(root, pattern, max_file_size, project_root, gitignore=gitignore)


# ---------------------------------------------------------------------------
#  Candidate files: ripgrep
# ---------------------------------------------------------------------------

def _ripgrep_binary() -> str | None:
    return shutil.which("rg")


def _basename_type_glob(pattern: str) -> str | None:
    """pattern 最后一段是 * 接一串普通字符（*.py、*_test.go）时，给 rg 按文件类型先筛一遍用的 glob。

    只为让 rg 少搜些文件：类型过滤只比文件名、区分大小写，字母都写成大小写皆可的字符类，最终由 _match_pattern 把关。"""
    last = pattern.replace("\\", "/").strip().rsplit("/", 1)[-1]
    if len(last) < 2 or not last.startswith("*"):
        return None
    suffix = last[1:]
    if not suffix.isascii() or any(ch in _GLOB_META for ch in suffix):
        return None
    return "*" + "".join(f"[{ch.lower()}{ch.upper()}]" if ch.isalpha() else ch for ch in suffix)


def _ripgrep_argv(binary: str, request: _Request, root: Path) -> list[str]:
    """rg 只负责找出有命中的文件。参数和 Python 那一路逐条对应：

    只认 .gitignore（--no-ignore-dot/-exclude/-global），隐藏文件照搜，整个文件当一段文本匹配（-U，正则能跨行），
    CRLF 当换行（$ 和 Python 读入时去掉 \\r 的效果一致）。pattern 不能交给 -g：-g 命中的文件会绕过 .gitignore。"""
    argv = [
        binary,
        "--files-with-matches", "--null", "--no-config", "--no-messages", "--color", "never",
        "--hidden", "--no-ignore-dot", "--no-ignore-exclude", "--no-ignore-global",
        "--multiline", "--crlf",
        "--max-filesize", str(request.max_file_size),
        "--ignore-case" if request.ignore_case else "--case-sensitive",
    ]
    if request.include_ignored:
        argv.append("--no-ignore-vcs")
    if not request.is_regex:
        argv.append("--fixed-strings")
    for name in sorted(_SKIP_DIRS):
        argv += ["--glob", f"!{name}/"]
    for prefix in _SKIP_DIR_PREFIXES:
        argv += ["--glob", f"!{prefix}*/"]
    for ending in _SKIP_FILE_ENDINGS:
        argv += ["--glob", f"!*{ending}"]
    type_glob = _basename_type_glob(request.pattern)
    if type_glob is not None:
        argv += ["--type-add", f"{_RG_PATTERN_TYPE}:{type_glob}", "--type", _RG_PATTERN_TYPE]
    argv += ["--regexp", request.query, "--", str(root)]
    return argv


@dataclass(frozen=True)
class _RipgrepRun:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    overflow: bool = False
    timed_out: bool = False
    cancelled: bool = False


async def _run_ripgrep(argv: list[str], cwd: Path, ctx: Any) -> _RipgrepRun | None:
    """跑一次 rg，有自己的超时和输出上限；起不来（参数里带 NUL、命令行太长、文件不能执行）返回 None。"""
    env = safe_subprocess_env()
    env.pop("RIPGREP_CONFIG_PATH", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # 独立进程组：超时或取消时连同子进程一起杀
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        logger.info("ripgrep could not start (%s); searching in Python", exc)
        return None
    outcome = await run_process(
        proc,
        timeout_sec=_RG_TIMEOUT_SEC,
        keep_stdout=_RG_OUTPUT_LIMIT_BYTES,
        keep_stderr=_RG_STDERR_LIMIT_BYTES,
        is_cancelled=getattr(ctx, "check_cancelled", None),
    )
    return _RipgrepRun(
        outcome.returncode, outcome.stdout, outcome.stderr,
        outcome.stdout_overflow, outcome.timed_out, outcome.cancelled,
    )


def _parse_ripgrep_paths(stdout: bytes, *, cut_short: bool) -> list[str]:
    """--null 分隔的路径。输出被截断或进程被杀时最后一个 NUL 之后只有半截路径，丢掉。"""
    chunks = stdout.split(b"\0")
    tail = chunks.pop()
    if tail and not cut_short:
        chunks.append(tail)
    return [os.fsdecode(chunk) for chunk in chunks if chunk]


def _walk_order(walk_rel: str) -> tuple[tuple[int, str], ...]:
    """os.walk 自上而下的顺序：同一目录里文件排在子目录前面，同类按名字排。"""
    parts = walk_rel.split("/")
    return tuple((1, part) for part in parts[:-1]) + ((0, parts[-1]),)


def _ripgrep_survivor(path: Path, walk_rel: str, display: str, request: _Request) -> bool:
    """rg 报出来的文件再按 Python 那一路的规则过一遍，两条路给出的候选只会一样。"""
    parts = walk_rel.split("/")
    if any(_skip_dir(part) for part in parts[:-1]):
        return False
    if parts[-1].endswith(_SKIP_FILE_ENDINGS):
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size > request.max_file_size:
        return False
    return _match_pattern(display, request.pattern)


class _SearchCancelled(Exception):
    pass


@dataclass
class _Candidates:
    files: Iterable[tuple[Path, str]]
    searcher: str
    limited: str = ""

    @property
    def limit_reason(self) -> str:
        collector_limit = getattr(self.files, "limited", "")
        return self.limited or collector_limit


async def _ripgrep_candidates(binary: str, root: Path, request: _Request, ctx: Any) -> _Candidates | None:
    argv = _ripgrep_argv(binary, request, root)
    run = await _run_ripgrep(argv, root, ctx)
    if run is None:
        return None
    if run.cancelled:
        raise _SearchCancelled()
    if run.returncode not in (0, 1) and not run.timed_out:
        problem = run.stderr.decode("utf-8", errors="replace").strip()
        # 退出码 2 而 stderr 为空只是个别文件读不了（--no-messages 把那些消息收了），结果照用
        if run.returncode != 2 or problem:
            logger.info("ripgrep failed (exit %s): %s; searching in Python", run.returncode, problem[:500])
            return None
    limited = ""
    if run.timed_out:
        limited = f"ripgrep 跑了 {_RG_TIMEOUT_SEC:g} 秒还没搜完，只用了已经找到的文件"
    elif run.overflow:
        limited = "ripgrep 找到的文件太多，只用了前一部分"
    name = display_namer(root, project_root_of(ctx))
    found: list[tuple[tuple[tuple[int, str], ...], Path, str]] = []
    for raw in _parse_ripgrep_paths(run.stdout, cut_short=run.overflow or run.timed_out):
        path = Path(raw)
        try:
            walk_rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        display = name(walk_rel)
        if _ripgrep_survivor(path, walk_rel, display, request):
            found.append((_walk_order(walk_rel), path, display))
    found.sort(key=lambda item: item[0])
    return _Candidates([(path, display) for _order, path, display in found], "ripgrep", limited)


async def _find_candidates(root: Path, request: _Request, ctx: Any) -> _Candidates:
    if not root.is_file():
        binary = _ripgrep_binary()
        if binary:
            listing = await _ripgrep_candidates(binary, root, request, ctx)
            if listing is not None:
                return listing
    collector = _collect_files(
        root, request.pattern, request.max_file_size, project_root_of(ctx), gitignore=not request.include_ignored,
    )
    return _Candidates(collector, "python")


# ---------------------------------------------------------------------------
#  Reading candidates
# ---------------------------------------------------------------------------

def _searchable_text(path: Path, max_file_size: int) -> tuple[str | None, int]:
    """(按 Python 正则搜的文本, 读了多少字节)。含 NUL 字节的当二进制跳过、文本为 None，去掉 BOM，CRLF 换成 LF（行号和 read_file 一致）。"""
    try:
        with open(path, "rb") as stream:
            data = stream.read(max_file_size + 1)
    except OSError:
        return None, 0
    if len(data) > max_file_size or b"\x00" in data:
        return None, len(data)
    text = data.decode("utf-8", errors="ignore")
    if text.startswith(BOM):
        text = text[len(BOM):]
    return normalize_newlines(text), len(data)


def _read_limit_reason(files: int, read_bytes: int) -> str:
    if files >= _MAX_CANDIDATE_FILES:
        return f"读了 {_MAX_CANDIDATE_FILES} 个文件就停了"
    if read_bytes >= _MAX_SCAN_BYTES:
        return f"读到 {_MAX_SCAN_BYTES // (1024 * 1024)}MB 就停了"
    return ""


class _Reader:
    """按候选顺序读文件。两条路都只算真读了的文件，二进制的也算：读够 _MAX_CANDIDATE_FILES 个或 _MAX_SCAN_BYTES 字节、
    后面还有候选时停下，limited 写明原因。"""

    def __init__(self, candidates: _Candidates, max_file_size: int) -> None:
        self._candidates = candidates
        self._iterator = iter(candidates.files)
        self._max_file_size = max_file_size
        self._read_files = 0
        self._read_bytes = 0
        self.limited = ""

    def __iter__(self) -> Iterator[tuple[Path, str, str]]:
        return self

    def __next__(self) -> tuple[Path, str, str]:
        while True:
            path, display = next(self._iterator)
            reason = _read_limit_reason(self._read_files, self._read_bytes)
            if reason:
                self.limited = reason
                raise StopIteration
            text, size = _searchable_text(path, self._max_file_size)
            self._read_files += 1
            self._read_bytes += size
            if text is None:
                continue
            return path, display, text

    @property
    def limit_reason(self) -> str:
        return self._candidates.limit_reason or self.limited


def _limit_note(reason: str) -> str:
    return f"（{reason}，结果可能不完整；缩小 path 或 pattern 再搜）"


# ---------------------------------------------------------------------------
#  Search mode
# ---------------------------------------------------------------------------

def _scan_matches(reader: _Reader, regex: re.Pattern[str], max_results: int) -> tuple[list[dict[str, Any]], bool]:
    matches: list[dict[str, Any]] = []
    total_chars = 0
    for _path, display, text in reader:
        lines = text.split("\n")
        for m in regex.finditer(text):
            line, col = _line_col(text, m.start())
            context = _get_context(lines, line - 1, n=1)
            match_text = _clip_line(m.group())
            total_chars += len(context) + len(match_text) + len(display) + 50
            matches.append({
                "file": display,
                "line": line,
                "column": col,
                "match": match_text,
                "context": context,
            })
            if len(matches) >= max_results or total_chars >= _MAX_TOTAL_RESULT_CHARS:
                return matches, True
    return matches, False


async def _do_search(root: Path, request: _Request, max_results: int, ctx: ToolContext) -> dict[str, Any]:
    try:
        candidates = await _find_candidates(root, request, ctx)
    except _SearchCancelled:
        return _error_response("task cancelled", cancelled=True)
    reader = _Reader(candidates, request.max_file_size)
    matches, truncated = await asyncio.to_thread(_scan_matches, reader, request.regex, max_results)
    lines = [_search_result_text(matches, truncated)]
    limited = "" if truncated else reader.limit_reason
    if limited:
        lines.append("")
        lines.append(_limit_note(limited))
    if not matches and not request.include_ignored and not root.is_file():
        if await asyncio.to_thread(gitignore_applies, root):
            lines.append("")
            lines.append("（.gitignore 忽略的文件没有搜；要搜就传 includeIgnored=true，或者把 path 指到那个目录）")
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "results": matches,
        "count": len(matches),
        "truncated": truncated,
        "searcher": candidates.searcher,
    }
    if limited:
        data["scanLimited"] = limited
    return {"ok": True, "success": True, "data": data}


# ---------------------------------------------------------------------------
#  Replace mode
# ---------------------------------------------------------------------------

def _python_template(replace: str, groups: int) -> str:
    """replace 里的 $1、$12、${name}、$&、$0、$$ 换成 re.sub 认的写法；反斜杠写法（\\1、\\g<name>、\\n）照 re.sub 的规矩。

    $12 在有第 12 组时引用它，否则是第 1 组后面跟一个 2；$ 后面不是这些写法时就是字面的 $。"""
    out: list[str] = []
    i = 0
    n = len(replace)
    while i < n:
        ch = replace[i]
        if ch == "\\":
            out.append(replace[i:i + 2])
            i += 2
            continue
        if ch != "$" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = replace[i + 1]
        if nxt == "$":
            out.append("$")
            i += 2
        elif nxt in "&0":
            out.append("\\g<0>")
            i += 2
        elif nxt == "{":
            close = replace.find("}", i + 2)
            if close > i + 2:
                out.append(f"\\g<{replace[i + 2:close]}>")
                i = close + 1
            else:
                out.append("$")
                i += 1
        elif "1" <= nxt <= "9":
            two = replace[i + 1:i + 3]
            if len(two) == 2 and "0" <= two[1] <= "9" and int(two) <= groups:
                out.append(f"\\g<{two}>")
                i += 3
            else:
                out.append(f"\\g<{nxt}>")
                i += 2
        else:
            out.append("$")
            i += 1
    return "".join(out)


def _replacement(regex: re.Pattern[str], replace: str, is_regex: bool) -> str | Callable[[re.Match[str]], str]:
    """re.sub 用的替换：非正则模式原样插入；正则模式先转成 re.sub 的模板，写坏了（引用不存在的分组之类）抛 ValueError。"""
    if not is_regex:
        return lambda _match: replace
    template = _python_template(replace, regex.groups)
    try:
        # 模板在调用时就整份校验，不用等真有命中
        regex.sub(template, "")
    except (re.error, IndexError) as exc:
        raise ValueError(f"replace 写法不对：{exc}。正则模式里 $1、${{name}}、$& 引用分组，字面的 $ 写成 $$") from None
    return template


@dataclass(frozen=True)
class _ReplaceOutcome:
    """transact_text 要的改动结果；matches 是改之前的命中位置。"""

    ok: bool
    changed: bool
    content: str
    matches: tuple[dict[str, Any], ...] = ()
    replacements: int = 0


def _replace_apply(
    regex: re.Pattern[str], replacement: str | Callable[[re.Match[str]], str], display: str,
) -> Callable[[str], _ReplaceOutcome]:
    def apply(text: str) -> _ReplaceOutcome:
        found: list[dict[str, Any]] = []
        for m in regex.finditer(text):
            line, col = _line_col(text, m.start())
            found.append({"file": display, "line": line, "column": col, "match": _clip_line(m.group())})
        if not found:
            return _ReplaceOutcome(True, False, text)
        content, count = regex.subn(replacement, text)
        return _ReplaceOutcome(True, content != text, content, tuple(found), count)

    return apply


class _MatchingFiles:
    """按候选顺序挑出有命中的文件；分批取，前面的文件改不成时接着往后找，凑够 max_files 个改成的为止。"""

    def __init__(self, reader: _Reader, regex: re.Pattern[str]) -> None:
        self._reader = reader
        self._regex = regex
        self.exhausted = False

    def take(self, count: int) -> list[tuple[Path, str]]:
        batch: list[tuple[Path, str]] = []
        while len(batch) < count:
            try:
                path, display, text = next(self._reader)
            except StopIteration:
                self.exhausted = True
                break
            if self._regex.search(text) is not None:
                batch.append((path, display))
        return batch


_TX_REASONS = {
    "missing": "文件已经不存在",
    "not_text": "文件不是 UTF-8 文本",
}


@dataclass
class _ReplaceReport:
    modified: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    check_failures: list[str] = field(default_factory=list)
    checks_passed: int = 0
    unstamped: list[str] = field(default_factory=list)
    # supervisor 连不上时后面的文件一个也认领不了，停下来，不再一个个等超时
    halted: str = ""

    @property
    def total_replacements(self) -> int:
        return sum(int(entry["replacements"]) for entry in self.modified)

    def skip(self, display: str, reason: str) -> None:
        self.skipped.append({"file": display, "reason": reason})


async def _replace_one(
    ctx: ToolContext,
    path: Path,
    display: str,
    apply: Callable[[str], _ReplaceOutcome],
    max_size: int,
    report: _ReplaceReport,
) -> None:
    if edit_journal.inside_journal(ctx.workspace_root, path):
        report.skip(display, edit_journal.JOURNAL_REFUSED_MESSAGE)
        return
    try:
        grant = await file_state.begin_edit(ctx, path, display)
    except file_state.EditBlocked as blocked:
        report.skip(display, blocked.message)
        if blocked.unavailable:
            report.halted = blocked.message
        return
    scope = edit_journal.JournalScope.of(ctx, session=grant.journal_session)
    try:
        tx = await asyncio.to_thread(
            transact_text, scope, path, apply, grant=grant, max_size=max_size, tool=_REPLACE_TOOL, display=display,
        )
    except file_state.ConcurrentEdit as exc:
        await file_state.abandon_edit(ctx, grant)
        report.skip(display, str(exc))
        return
    except OSError as exc:
        await file_state.abandon_edit(ctx, grant)
        report.skip(display, f"读写文件失败：{exc}")
        return
    tx = await settle_transaction(ctx, grant, tx)
    if tx.status == "too_large":
        report.skip(display, f"文件太大（{tx.size} 字节），最多改 {max_size} 字节的文件")
        return
    if tx.status == "stale":
        report.skip(display, tx.gate.stale_message)
        return
    if tx.status in _TX_REASONS:
        report.skip(display, _TX_REASONS[tx.status])
        return
    outcome: _ReplaceOutcome | None = tx.outcome
    if outcome is None or tx.status != "written":
        if outcome is not None and outcome.matches:
            report.unchanged.append(display)
        return
    report.matches.extend(outcome.matches)
    entry: dict[str, Any] = {"file": display, "replacements": outcome.replacements, "bytes": tx.byte_count}
    if tx.journal is not None:
        entry.update(tx.journal.to_data())
        if tx.journal.message:
            report.notes.append(f"{display}：{tx.journal.message}")
    if not tx.stamp_recorded:
        entry["stampRecorded"] = False
        report.unstamped.append(display)
    checks: EditChecks = await run_edit_checks(ctx, path, outcome.content, tool_name=_REPLACE_TOOL)
    entry.update(checks.to_data())
    if checks.failed:
        report.check_failures.extend(f"{display}：{line}" for line in checks.lines())
    elif checks.syntax is not None or checks.commands:
        report.checks_passed += 1
    report.modified.append(entry)


def _replace_result_text(report: _ReplaceReport, limited: str) -> str:
    # 头一行和逐个文件的「路径: N replacements」由 engine.tool_history 解析，用来让旧的 read_file 结果让位，格式不能动
    lines = [f"{len(report.modified)} files modified, {report.total_replacements} replacements"]
    for entry in report.modified:
        lines.append(f"{entry['file']}: {entry['replacements']} replacements")
    if report.modified:
        lines.append(
            "这些改动可以用 undo_edits 撤销，下面注明不可撤销的除外。" if report.notes else "这些改动可以用 undo_edits 撤销。"
        )
    if report.checks_passed:
        lines.append(f"改后检查：{report.checks_passed} 个文件通过。")
    if report.check_failures:
        lines.append("改后检查没通过（改动已经写入，没有回滚）：")
        lines.extend(f"  {line}" for line in report.check_failures)
    lines.extend(report.notes)
    if report.unstamped:
        lines.append(
            f"改后的版本没能登记到 supervisor：{'、'.join(report.unstamped[:10])}；再改这些文件之前先 read_file。"
        )
    if report.unchanged:
        lines.append(f"{len(report.unchanged)} 个文件有命中，但替换后内容不变，没有写盘：{'、'.join(report.unchanged[:10])}")
    if report.skipped:
        lines.append(f"跳过了 {len(report.skipped)} 个有命中的文件：")
        lines.extend(f"  {item['file']}：{item['reason']}" for item in report.skipped)
    if report.halted:
        lines.append("连不上 supervisor，后面还有命中的文件没有处理。")
    if limited:
        lines.append(_limit_note(limited))
    return "\n".join(lines)


def _replace_data(report: _ReplaceReport, text: str, searcher: str, limited: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "result": text,
        "matches": report.matches,
        "results": report.modified,
        "filesModified": len(report.modified),
        "totalReplacements": report.total_replacements,
        "searcher": searcher,
    }
    if report.skipped:
        data["skipped"] = report.skipped
    if report.unchanged:
        data["unchanged"] = report.unchanged
    if report.halted:
        data["halted"] = report.halted
    if limited:
        data["scanLimited"] = limited
    return data


async def _do_replace(
    root: Path,
    request: _Request,
    replacement: str | Callable[[re.Match[str]], str],
    max_files: int,
    ctx: ToolContext,
) -> dict[str, Any]:
    # Approval guard — replace is a write operation
    _op, err = await request_guard(
        ctx, "write_file",
        {"path": "(search_replace)", "reason": f"replace '{request.query}' in up to {max_files} files"},
    )
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    try:
        candidates = await _find_candidates(root, request, ctx)
    except _SearchCancelled:
        return _error_response("task cancelled", cancelled=True)
    reader = _Reader(candidates, request.max_file_size)
    matching = _MatchingFiles(reader, request.regex)
    report = _ReplaceReport()
    while len(report.modified) < max_files and not matching.exhausted and not report.halted:
        batch = await asyncio.to_thread(matching.take, max_files - len(report.modified))
        for path, display in batch:
            if await ctx.check_cancelled():
                # 已经改了的文件照样列出来：它们记进了编辑日志，undo_edits 撤得回去
                return _error_response(
                    "task cancelled", cancelled=True, filesModified=len(report.modified), results=report.modified,
                )
            await _replace_one(
                ctx, path, display, _replace_apply(request.regex, replacement, display),
                request.max_file_size, report,
            )
            if report.halted:
                break
    if report.halted and not report.modified:
        return _error_response(report.halted, unavailable=True, skipped=report.skipped)
    limited = "" if len(report.modified) >= max_files or report.halted else reader.limit_reason
    text = _replace_result_text(report, limited)
    return {
        "ok": True,
        "success": True,
        "cancelled": False,
        "data": _replace_data(report, text, candidates.searcher, limited),
    }


# ---------------------------------------------------------------------------
#  Main entry
# ---------------------------------------------------------------------------

async def search_in_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query", ""))
    mode = str(args.get("mode", "search")).lower()
    rel_path = str(args.get("path", "."))
    pattern = str(args.get("pattern") or "**/*")

    if not query:
        return _error_response("empty query")
    # 只读节点的开关：任何送审之前就拒，不弹审批卡
    if mode == "replace" and replace_disabled(ctx):
        return _error_response(REPLACE_DISABLED_MESSAGE, refused=REPLACE_SWITCH)
    try:
        is_regex = _aliased_switch(args, "isRegex", "is_regex")
        include_ignored = _aliased_switch(args, "includeIgnored", "include_ignored")
        # 0 和没传一样按默认值算
        max_results = _bounded_int(
            args.get("maxResults") or args.get("max_results"), _DEFAULT_MAX_RESULTS, _MAX_RESULTS_LIMIT, "maxResults",
        )
        max_files = _bounded_int(
            args.get("maxFiles") or args.get("max_files"), _DEFAULT_MAX_FILES, _MAX_FILES_LIMIT, "maxFiles",
        )
    except ValueError as exc:
        return _error_response(str(exc))
    if mode == "replace" and args.get("replace") is None:
        return _error_response("replace mode needs 'replace'; pass an empty string to delete the matches")
    replace_str = str(args.get("replace", ""))

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    max_file_size_bytes = get_int(
        runtime_cfg,
        "meta.search.max_file_size_bytes",
        3_000_000,
        min_value=100_000,
        max_value=50_000_000,
    )

    try:
        root, is_ext = resolve_and_classify(
            ctx.workspace_root, rel_path, project_root=project_root_of(ctx), project_origin=project_origin_of(ctx),
        )
    except ValueError as exc:
        return _error_response(str(exc))
    if not root.exists():
        return _error_response("path not found", path=rel_path)

    # ---- External path approval guard ----
    err = await guard_external_read(ctx, is_ext, rel_path, "search_in_files")
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    # Compile regex — search: case-insensitive (gim); replace: case-sensitive (g)
    ignore_case = mode != "replace"
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    try:
        if is_regex:
            regex = re.compile(query, flags)
        else:
            regex = re.compile(re.escape(query), flags)
    except re.error as e:
        return _error_response(f"invalid regex: {e}")

    request = _Request(
        regex=regex,
        query=query,
        is_regex=is_regex,
        ignore_case=ignore_case,
        pattern=pattern,
        include_ignored=include_ignored,
        max_file_size=max_file_size_bytes,
    )
    if mode == "replace":
        try:
            replacement = _replacement(regex, replace_str, is_regex)
        except ValueError as exc:
            return _error_response(str(exc))
        return await _do_replace(root, request, replacement, max_files, ctx)
    return await _do_search(root, request, max_results, ctx)
