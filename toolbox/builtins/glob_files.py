"""glob — find files by name or path pattern under a directory, newest first (policy guarded like list_dir).

.gitignore is honoured like ripgrep's defaults, dependency/cache/build directories are skipped and symbolic links
are not followed (``toolbox.ignore_rules``); a directory named in ``path`` is always searched."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..context import ToolContext, project_origin_of, project_root_of
from .._common import display_namer, guard_external_read, request_guard, resolve_and_classify
from ..ignore_rules import DEFAULT_IGNORED_DIRS, MAX_WALK_FILES, GlobError, glob_regex, walk_files

DEFAULT_MAX_RESULTS = 200
MAX_RESULTS_LIMIT = 1000


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}", "files": [], "count": 0}
    data.update(extra)
    response: dict[str, Any] = {"ok": False, "success": False, "error": text, "data": data}
    response.update(extra)
    return response


def pattern_matcher(pattern: str) -> Callable[[str], bool]:
    """pattern 换成判定函数，参数是相对搜索根、用 / 分隔的路径。

    不带 / 的比文件名，任意深度都算；带 / 的从搜索根算起整段比。* ? 和 [...] 不跨目录，整段的 ** 跨任意层，
    {a,b} 是多选一；反斜杠当目录分隔符。以 / 结尾的表示那个目录下的全部文件。写坏了抛 GlobError。"""
    normalized = pattern.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if normalized.endswith("/"):
        normalized += "**"
    if not normalized:
        raise GlobError("empty pattern")
    regex = glob_regex(normalized)
    if "/" in normalized:
        return lambda rel: regex.fullmatch(rel) is not None
    return lambda rel: regex.fullmatch(rel.rsplit("/", 1)[-1]) is not None


def _max_results(raw: Any) -> int:
    if raw is None or raw == "" or isinstance(raw, bool):
        return DEFAULT_MAX_RESULTS
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_results must be a positive integer") from None
    if value <= 0:
        return DEFAULT_MAX_RESULTS
    return min(MAX_RESULTS_LIMIT, value)


def _skip_dir(name: str) -> bool:
    return name in DEFAULT_IGNORED_DIRS


def _collect(
    root: Path, matches: Callable[[str], bool], project_root: Path,
) -> tuple[list[str], bool]:
    """(按修改时间倒序的全部命中, 是否走到上限停下)。修改时间相同的按遍历顺序。"""
    name = display_namer(root, project_root)
    found: list[tuple[int, int, str]] = []
    walked = 0
    for entry, walk_rel in walk_files(root, gitignore=True, skip_dir=_skip_dir):
        walked += 1
        if walked > MAX_WALK_FILES:
            return _newest_first(found), True
        if not matches(walk_rel):
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        found.append((mtime, len(found), name(walk_rel)))
    return _newest_first(found), False


def _newest_first(found: list[tuple[int, int, str]]) -> list[str]:
    found.sort(key=lambda item: (-item[0], item[1]))
    return [display for _mtime, _order, display in found]


def _result_text(pattern: str, path: str, shown: list[str], total: int, limited: bool) -> str:
    if not total:
        lines = [
            f"No files match '{pattern}' under {path}.",
            "（.gitignore 忽略的文件和依赖、缓存、构建目录不算在内；要找那里面的文件，把 path 指到那个目录）",
        ]
    else:
        head = f"{total} files match '{pattern}' under {path}, newest first"
        if len(shown) < total:
            head += f" (showing the first {len(shown)}; pass max_results or narrow the pattern for more)"
        lines = [head + ":", *shown]
    if limited:
        lines.append(f"（走到 {MAX_WALK_FILES} 个文件就停了，结果可能不完整；把 path 缩小到具体目录再找）")
    return "\n".join(lines)


async def glob_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return _error_response("'pattern' is required, e.g. '**/*.py'")
    rel_path = str(args.get("path") or ".").strip() or "."
    try:
        max_results = _max_results(args.get("max_results", args.get("maxResults")))
        matches = pattern_matcher(pattern)
    except GlobError as exc:
        return _error_response(f"invalid pattern: {exc}", path=rel_path)
    except ValueError as exc:
        return _error_response(str(exc), path=rel_path)

    project_root = project_root_of(ctx)
    try:
        root, is_external = resolve_and_classify(
            ctx.workspace_root, rel_path, project_root=project_root, project_origin=project_origin_of(ctx),
        )
    except ValueError as exc:
        return _error_response(str(exc), path=rel_path)

    # 文件名清单和目录清单一样会暴露内容，和 list_dir 走同一个 read_file 判定；工作区外的按外部读送审。
    if is_external:
        err = await guard_external_read(ctx, True, rel_path, "glob", reason=f"glob on external path: {rel_path}")
    else:
        _op, err = await request_guard(ctx, "read_file", {"path": rel_path, "glob": pattern})
    if err is not None:
        return _error_response(
            err.get("error", "denied") if isinstance(err, dict) else err,
            path=rel_path,
            cancelled=bool(isinstance(err, dict) and err.get("cancelled")),
        )

    if not root.exists():
        return _error_response("path not found", path=rel_path)
    if not root.is_dir():
        return _error_response("Not a directory", path=rel_path)

    found, limited = await asyncio.to_thread(_collect, root, matches, project_root)
    shown = found[:max_results]
    data: dict[str, Any] = {
        "result": _result_text(pattern, rel_path, shown, len(found), limited),
        "files": shown,
        "count": len(shown),
        "total": len(found),
        "truncated": len(shown) < len(found),
        "path": rel_path,
    }
    if limited:
        data["scanLimited"] = True
    return {"ok": True, "success": True, "data": data}
