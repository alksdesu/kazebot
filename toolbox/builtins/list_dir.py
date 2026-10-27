"""list_dir — list one or more directories under workspace root (batch ``paths``, optional recursion).

Directories first, then files, each sorted by name; dependency, cache and build directories
(``ignore_rules.DEFAULT_IGNORED_DIRS``) are listed by name only unless ``include_ignored`` is true."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from clonoth_runtime import SwitchValueError, switch_arg

from ..context import ToolContext, project_origin_of, project_root_of
from .._common import request_guard, resolve_and_classify, guard_external_read
from ..ignore_rules import DEFAULT_IGNORED_DIRS

IGNORED_NOTE = "（内容已省略）"


def _list_dir_result_text(results: list[dict[str, Any]], total_files: int, total_dirs: int) -> str:
    parts: list[str] = []
    ignored_count = 0
    for section in results:
        path = section.get("path", "?")
        fc = section.get("fileCount", 0)
        dc = section.get("dirCount", 0)
        if section.get("success") is False:
            parts.append(f"── {path} ── ERROR: {section.get('error', 'unknown')}")
            parts.append("")
            continue
        parts.append(f"── {path} ── ({fc} files, {dc} dirs)")
        for entry in section.get("entries", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if entry.get("type") == "directory":
                shown = name if str(name).endswith("/") else str(name) + "/"
                if entry.get("ignored"):
                    ignored_count += 1
                    parts.append(f"  📁 {shown} {IGNORED_NOTE}")
                else:
                    parts.append(f"  📁 {shown}")
            else:
                parts.append(f"  📄 {name}")
        parts.append("")
    parts.append(f"Total: {total_files} files, {total_dirs} dirs")
    if ignored_count:
        parts.append(
            f"{ignored_count} 个依赖、缓存或构建目录只列了名字，没有展开；要看里面的内容就传 include_ignored=true，"
            "或者直接列那个目录。"
        )
    return "\n".join(parts)


def _empty_error_response(message: Any) -> dict[str, Any]:
    text = str(message)
    return {
        "ok": False,
        "success": False,
        "error": text,
        "data": {"result": f"ERROR: {text}", "results": [], "totalFiles": 0, "totalDirs": 0, "totalPaths": 0},
    }


_DEFAULT_IGNORE_DIRS = DEFAULT_IGNORED_DIRS
_NO_IGNORED_DIRS: frozenset[str] = frozenset()


def _directory_entry(name: str, ignored: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "type": "directory"}
    if ignored:
        entry["ignored"] = True
    return entry


def _list_entries_flat(
    base: Path,
    ignore_dirs: frozenset[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Non-recursive: list immediate children. Returns (entries, fileCount, dirCount)."""
    dirs: list[tuple[str, bool]] = []
    files: list[str] = []

    try:
        children = sorted(base.iterdir(), key=lambda c: c.name)
    except Exception:
        children = []

    for child in children:
        if child.is_dir():
            dirs.append((child.name + "/", child.name in ignore_dirs))
        elif child.is_file():
            files.append(child.name)

    entries: list[dict[str, Any]] = [_directory_entry(name, ignored) for name, ignored in dirs]
    for f in files:
        entries.append({"name": f, "type": "file"})

    return entries, len(files), len(dirs)


def _list_entries_recursive(
    base: Path,
    ignore_dirs: frozenset[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Recursive: walk tree. Returns (entries, fileCount, dirCount)."""
    dirs: list[tuple[str, bool]] = []
    files: list[str] = []

    # 忽略的目录边走边剪：列出名字但不进去，先整棵树遍历再过滤的话 .git、node_modules 里成千上万个文件也要挨个过一遍。
    for root, dirnames, filenames in os.walk(base):
        rel_root = Path(root).relative_to(base)
        kept: list[str] = []
        for name in dirnames:
            ignored = name in ignore_dirs
            dirs.append(((rel_root / name).as_posix() + "/", ignored))
            if not ignored:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if not os.path.isfile(os.path.join(root, name)):
                continue
            files.append((rel_root / name).as_posix())

    entries: list[dict[str, Any]] = [_directory_entry(name, ignored) for name, ignored in sorted(dirs)]
    for f in sorted(files):
        entries.append({"name": f, "type": "file"})

    return entries, len(files), len(dirs)


async def list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    paths_arg = args.get("paths")
    if isinstance(paths_arg, list) and paths_arg:
        dir_paths = [str(p).strip() for p in paths_arg if isinstance(p, str) and str(p).strip()]
    else:
        # Legacy single-path mode
        dir_paths = [str(args.get("path", "."))]

    if not dir_paths:
        return _empty_error_response("no paths specified")

    try:
        recursive = switch_arg(args, "recursive", False)
        include_ignored = switch_arg(args, "include_ignored", False)
    except SwitchValueError as exc:
        return _empty_error_response(exc)
    ignore_dirs = _NO_IGNORED_DIRS if include_ignored else _DEFAULT_IGNORE_DIRS

    # 工作区外的路径整批只送审一次。
    project_root = project_root_of(ctx)
    project_origin = project_origin_of(ctx)
    _external_paths: list[str] = []
    for _dp in dir_paths:
        try:
            _resolved, _is_ext = resolve_and_classify(
                ctx.workspace_root, _dp, project_root=project_root, project_origin=project_origin,
            )
            if _is_ext:
                _external_paths.append(_dp)
        except ValueError:
            continue

    if _external_paths:
        _reason = f"list_dir on external path(s): {', '.join(_external_paths[:5])}"
        if len(_external_paths) > 5:
            _reason += f" (+{len(_external_paths) - 5} more)"
        err = await guard_external_read(ctx, True, _external_paths[0], "list_dir", reason=_reason)
        if err is not None:
            return _empty_error_response(err.get("error", "denied") if isinstance(err, dict) else err)

    results: list[dict[str, Any]] = []
    total_files = 0
    total_dirs = 0

    _external = set(_external_paths)
    for dir_path in dir_paths:
        # 目录清单会暴露附件名、会话 id 等内容，与 read_file 走同一策略判定：
        # 否则 read_file 拒读 data/ 而 list_dir 却能把它列个遍，两者结论不一致。
        # 工作区外路径已由上面的 guard_external_read 批量审批，不重复送审。
        if dir_path not in _external:
            _op, err = await request_guard(ctx, "read_file", {"path": dir_path, "list_dir": True})
            if err is not None:
                results.append({
                    "path": dir_path, "success": False,
                    "error": err.get("error", "denied") if isinstance(err, dict) else str(err),
                    "entries": [], "fileCount": 0, "dirCount": 0,
                })
                continue
        try:
            p, _ext = resolve_and_classify(
                ctx.workspace_root, dir_path, project_root=project_root, project_origin=project_origin,
            )
        except ValueError as exc:
            results.append({
                "path": dir_path, "success": False, "error": str(exc),
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if not p.exists():
            results.append({
                "path": dir_path, "success": False, "error": "File not found",
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if not p.is_dir():
            results.append({
                "path": dir_path, "success": False, "error": "Not a directory",
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if recursive:
            # 整棵树的遍历放线程里跑，大目录不至于卡住同进程的其他任务。
            entries, fc, dc = await asyncio.to_thread(_list_entries_recursive, p, ignore_dirs)
        else:
            entries, fc, dc = _list_entries_flat(p, ignore_dirs)

        results.append({
            "path": dir_path, "success": True,
            "entries": entries, "fileCount": fc, "dirCount": dc,
        })
        total_files += fc
        total_dirs += dc

    all_ok = all(r.get("success") for r in results)
    response: dict[str, Any] = {
        "ok": all_ok,
        "success": all_ok,
        "data": {
            "result": _list_dir_result_text(results, total_files, total_dirs),
            "results": results,
            "totalFiles": total_files,
            "totalDirs": total_dirs,
            "totalPaths": len(dir_paths),
        },
    }
    if not all_ok:
        fail_count = sum(1 for r in results if not r.get("success"))
        response["error"] = "Some directories failed to list" if fail_count > 1 else results[next(i for i, r in enumerate(results) if not r.get("success"))].get("error", "unknown")

    # Backward compat: single path → set top-level path/items
    if len(dir_paths) == 1 and results:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success"):
            response["items"] = r0.get("entries", [])

    return response
