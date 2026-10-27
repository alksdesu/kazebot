"""read_file — read one or more files (text / image / binary).

Supports batch reading via ``files`` array parameter.
Backward-compatible with legacy single-file ``path`` parameter.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import file_state
from ..context import ToolContext, project_origin_of, project_root_of
from .._common import request_guard, resolve_under_allowed_roots
from ..hashline import hashline_enabled, hashline_prefix
from ..text_files import decode_text, normalize_newlines, split_lines


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_LINES = 500
MAX_LINE_CHARS = 2000
MAX_RESULT_BYTES = 50 * 1024
# Below this much remaining budget a text file is not read at all, only listed as skipped.
_MIN_FILE_BUDGET_BYTES = 512
# Kept free for a section header while deciding how many lines still fit.
_HEADER_RESERVE_BYTES = 256

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Map from sniffed MIME to canonical extension
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _sniff_image_mime(data: bytes) -> str | None:
    """Detect actual image MIME type from magic bytes."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 4 and data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    return None


# ---------------------------------------------------------------------------
#  Image dimension helpers (pure stdlib, no Pillow needed)
# ---------------------------------------------------------------------------

def _make_dimensions(w: int, h: int) -> dict[str, Any]:
    from math import gcd
    g = gcd(w, h) if w > 0 and h > 0 else 1
    return {"width": w, "height": h, "aspectRatio": f"{w // g}:{h // g}"}


def _parse_jpeg_dimensions(data: bytes) -> dict[str, Any] | None:
    import struct
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 9 <= len(data):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return _make_dimensions(w, h)
            break
        if marker in (0xD9, 0xDA):
            break
        if i + 4 <= len(data):
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
        else:
            break
    return None


def _get_image_dimensions(data: bytes, ext: str) -> dict[str, Any] | None:
    try:
        import struct
        if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return _make_dimensions(w, h)
        if ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
            return _parse_jpeg_dimensions(data)
    except Exception:
        pass
    return None


LinePrefix = Callable[[int, str], str]


def number_prefix(width: int) -> LinePrefix:
    """Default line label: the right-aligned line number and a bar, e.g. ``  12 | ``."""
    def prefix(line_no: int, _line: str) -> str:
        return f"{line_no:>{width}} | "
    return prefix


def clip_line(line: str, limit: int = MAX_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= limit:
        return line, False
    return f"{line[:limit]}…（本行共 {len(line)} 字符，只显示了前 {limit} 个）", True


def render_lines(
    lines: Sequence[str], first_line_no: int, prefix: LinePrefix, *, limit: int = MAX_LINE_CHARS,
) -> list[tuple[str, bool]]:
    """Label and clip ``lines`` numbered from ``first_line_no``; returns ``(rendered, clipped)`` pairs.

    ``prefix`` gets the whole original line, so a label derived from the content is
    computed before the line is clipped.
    """
    rendered: list[tuple[str, bool]] = []
    for offset, line in enumerate(lines):
        shown, clipped = clip_line(line, limit)
        rendered.append((prefix(first_line_no + offset, line) + shown, clipped))
    return rendered


# ---------------------------------------------------------------------------
#  Single-file reader
# ---------------------------------------------------------------------------

@dataclass
class _Budget:
    """Bytes of model-visible text still available in this call."""

    remaining: int = MAX_RESULT_BYTES

    def spend(self, text: str) -> None:
        self.remaining -= len(text.encode("utf-8")) + 1


@dataclass
class _Section:
    entry: dict[str, Any]
    header: str
    body: list[str] = field(default_factory=list)
    multimodal: dict[str, Any] | None = None
    # Set when the file was not shown in full; listed again at the top of a multi-file result.
    notice: str = ""

    @property
    def text(self) -> str:
        return "\n".join([self.header, *self.body])


def _budget_label() -> str:
    return f"{MAX_RESULT_BYTES // 1024}KB"


def _error_section(path_str: str, error: str, **extra: Any) -> _Section:
    entry: dict[str, Any] = {"path": path_str, "success": False, "error": error, **extra}
    return _Section(entry, f"── {path_str} ── ERROR: {error}")


def _skipped_section(path_str: str) -> _Section:
    reason = f"本次返回已达 {_budget_label()} 上限"
    note = f"未读取：{reason}，需要时单独读取这个文件"
    entry = {"path": path_str, "success": True, "type": "omitted", "omitted": True, "content": note, "reason": reason}
    return _Section(entry, f"── {path_str} ── {note}", notice=f"{path_str} 未读取")


def _line_number(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _read_image(p: Path, path_str: str, workspace_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = p.read_bytes()
    ext = p.suffix.lower()
    # Sniff actual MIME from magic bytes; fall back to extension
    mime = _sniff_image_mime(raw) or _MIME_MAP.get(ext, "application/octet-stream")
    real_ext = _MIME_TO_EXT.get(mime, ext)
    entry: dict[str, Any] = {
        "path": path_str, "success": True,
        "type": "multimodal", "mimeType": mime, "size": len(raw),
    }
    dims = _get_image_dimensions(raw, ext)
    if dims:
        entry["dimensions"] = dims
    # Save a copy under data/attachments/ so the multimodal pipeline
    # can resolve it to a base64 data-URL for the LLM.
    att_dir = workspace_root / "data" / "attachments" / "read_file"
    att_dir.mkdir(parents=True, exist_ok=True)
    att_path = att_dir / f"{_uuid.uuid4().hex}{real_ext}"
    shutil.copy2(p, att_path)
    mm: dict[str, Any] = {
        "type": "image",
        "path": att_path.relative_to(workspace_root).as_posix(),
        "mime_type": mime,
        "name": p.stem + real_ext,
    }
    return entry, mm


def _text_section(
    path_str: str, text: str, start_line: Any, end_line: Any, budget: _Budget, *, hashline: bool = False,
) -> _Section:
    lines = split_lines(normalize_newlines(text))
    total = len(lines)
    start_val = _line_number(start_line)
    end_val = _line_number(end_line)
    has_range = start_val is not None or end_val is not None

    if total == 0:
        entry = {"path": path_str, "success": True, "type": "text", "content": "", "lineCount": 0, "totalLines": 0}
        return _Section(entry, f"── {path_str} ── 空文件（0 行）")
    if start_val is not None and start_val > total:
        return _error_section(
            path_str, f"startLine {start_val} 超出了文件的行数（共 {total} 行）", totalLines=total,
        )

    first = start_val if start_val is not None and start_val > 0 else 1
    last = min(end_val, total) if end_val is not None and end_val >= first else total
    auto_truncated = not has_range and total > DEFAULT_MAX_LINES
    if auto_truncated:
        last = DEFAULT_MAX_LINES

    selected = lines[first - 1:last]
    prefix = hashline_prefix if hashline else number_prefix(max(4, len(str(last))))
    rendered = render_lines(selected, first, prefix)
    available = budget.remaining - _HEADER_RESERVE_BYTES
    used = 0
    shown = 0
    for line, _clipped in rendered:
        cost = len(line.encode("utf-8")) + 1
        if used + cost > available:
            break
        used += cost
        shown += 1
    budget_truncated = shown < len(rendered)
    shown_last = first + shown - 1

    entry: dict[str, Any] = {
        "path": path_str, "success": True, "type": "text",
        "content": "\n".join(line for line, _clipped in rendered[:shown]),
        "lineCount": shown, "totalLines": total,
    }
    if has_range:
        entry["startLine"] = first
        entry["endLine"] = last
    clipped_lines = [first + offset for offset, (_line, clipped) in enumerate(rendered[:shown]) if clipped]
    if clipped_lines:
        entry["clippedLines"] = clipped_lines

    notice = ""
    if auto_truncated or budget_truncated:
        next_line = shown_last + 1
        limit_note = f"（本次返回已达 {_budget_label()} 上限）" if budget_truncated else ""
        if shown == 0:
            hint = f"文件共 {total} 行，本次返回已达 {_budget_label()} 上限，没有显示内容；继续读用 startLine={next_line}"
            notice = f"{path_str} 没有显示内容（继续读用 startLine={next_line}）"
        else:
            hint = f"文件共 {total} 行，只显示了 {first}-{shown_last} 行{limit_note}；继续读用 startLine={next_line}"
            notice = f"{path_str} 只显示了 {first}-{shown_last} 行（继续读用 startLine={next_line}）"
        entry.update({
            "truncated": True,
            "truncatedBy": "budget" if budget_truncated else "default_limit",
            "shownLines": shown,
            "nextStartLine": next_line,
            "hint": hint,
        })
        header = f"── {path_str} ── {hint}"
    elif has_range and (first > 1 or last < total):
        header = f"── {path_str} ── 文件共 {total} 行，显示 {first}-{last} 行"
    else:
        header = f"── {path_str} ── 共 {total} 行"
    return _Section(entry, header, [line for line, _clipped in rendered[:shown]], notice=notice)


async def _read_single_file(
    path_str: str,
    start_line: Any,
    end_line: Any,
    ctx: ToolContext,
    budget: _Budget,
    *,
    hashline: bool = False,
) -> _Section:
    if not path_str:
        return _error_section("", "路径为空")

    is_image = Path(path_str).suffix.lower() in _IMAGE_EXTENSIONS
    if not is_image and budget.remaining < _MIN_FILE_BUDGET_BYTES:
        return _skipped_section(path_str)

    _op, err = await request_guard(
        ctx, "read_file",
        {"path": path_str, "start_line": start_line, "end_line": end_line},
    )
    if err is not None:
        return _error_section(path_str, str(err.get("error", "denied")))

    try:
        p = resolve_under_allowed_roots(
            ctx.workspace_root, path_str, project_root=project_root_of(ctx), project_origin=project_origin_of(ctx),
        )
    except ValueError as exc:
        return _error_section(path_str, str(exc))

    if not p.exists() or not p.is_file():
        return _error_section(path_str, "文件不存在")

    # ---- multimodal (image) ----
    if p.suffix.lower() in _IMAGE_EXTENSIONS:
        try:
            entry, mm = await asyncio.to_thread(_read_image, p, path_str, ctx.workspace_root)
        except Exception as exc:
            return _error_section(path_str, str(exc))
        return _Section(entry, f"── {path_str} ── [image: {entry['mimeType']}, {entry['size']} bytes]", multimodal=mm)

    # ---- text file ----
    try:
        snapshot = await asyncio.to_thread(file_state.take_snapshot, p)
    except Exception as exc:
        return _error_section(path_str, str(exc))
    if not snapshot.exists or snapshot.stamp is None:
        return _error_section(path_str, "文件不存在")
    try:
        text = decode_text(snapshot.data)
    except UnicodeDecodeError:
        entry = {"path": path_str, "success": True, "type": "binary", "size": len(snapshot.data)}
        return _Section(entry, f"── {path_str} ── [binary: {len(snapshot.data)} bytes]")

    # Even a partial read stamps the whole file: edits are checked against what was on disk.
    await file_state.record_stamp(ctx, p, snapshot.stamp, revealable=True)
    return _text_section(path_str, text, start_line, end_line, budget, hashline=hashline)


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------


def _read_file_result_text(sections: list[_Section]) -> str:
    parts = [section.text for section in sections]
    notices = [section.notice for section in sections if section.notice]
    if len(sections) > 1 and notices:
        parts.insert(0, f"本次 read_file 没有全部显示：{'；'.join(notices)}。")
    return "\n".join(parts) if parts else "No files read"


def _file_entries(args: dict[str, Any]) -> list[dict[str, Any]] | None:
    files_arg = args.get("files")
    if isinstance(files_arg, list) and files_arg:
        return [
            entry if isinstance(entry, dict) else {"path": entry}
            for entry in files_arg
            if isinstance(entry, dict) or isinstance(entry, str)
        ]
    path = str(args.get("path", ""))
    if not path:
        return None
    entry_dict: dict[str, Any] = {"path": path}
    if args.get("start_line") is not None:
        entry_dict["startLine"] = args["start_line"]
    if args.get("end_line") is not None:
        entry_dict["endLine"] = args["end_line"]
    return [entry_dict]


def _empty_response(error: str) -> dict[str, Any]:
    return {
        "ok": False, "success": False, "error": error,
        "data": {
            "result": f"ERROR: {error}", "results": [], "successCount": 0, "failCount": 0,
            "omittedCount": 0, "totalCount": 0, "multiRoot": False,
        },
    }


async def read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    file_entries = _file_entries(args)
    if file_entries is None:
        return _empty_response("no path specified")
    if not file_entries:
        return _empty_response("empty files list")

    budget = _Budget()
    hashline = hashline_enabled(ctx)
    sections: list[_Section] = []
    for fe in file_entries:
        path_str = str(fe.get("path", "")).strip()
        sl = fe.get("startLine") if fe.get("startLine") is not None else fe.get("start_line")
        el = fe.get("endLine") if fe.get("endLine") is not None else fe.get("end_line")
        section = await _read_single_file(path_str, sl, el, ctx, budget, hashline=hashline)
        budget.spend(section.text)
        sections.append(section)

    results = [section.entry for section in sections]
    multimodal_data = [section.multimodal for section in sections if section.multimodal is not None]
    omitted_count = sum(1 for r in results if r.get("omitted"))
    fail_count = sum(1 for r in results if not r.get("success"))
    success_count = len(results) - fail_count - omitted_count
    response: dict[str, Any] = {
        "ok": fail_count == 0,
        "success": fail_count == 0,
        "data": {
            "result": _read_file_result_text(sections),
            "results": results,
            "successCount": success_count,
            "failCount": fail_count,
            "omittedCount": omitted_count,
            "totalCount": len(results),
            "multiRoot": False,
        },
    }
    if fail_count > 0:
        response["error"] = f"{fail_count} 个文件读取失败"
    if multimodal_data:
        # Attachments are mirrored at the top level for collectors that only look there.
        response["data"]["attachments"] = multimodal_data
        response["attachments"] = multimodal_data

    # Backward compat: single text file → set top-level path/content
    if len(results) == 1:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success") and r0.get("type") == "text":
            response["content"] = r0.get("content", "")
        elif not r0.get("success"):
            response["error"] = r0.get("error", "read failed")

    return response
