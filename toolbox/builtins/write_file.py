"""write_file — write a whole text file (policy + approval guarded)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import file_state
from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots
from ..syntax_check import run_edit_checks
from ..text_files import NEW_FILE, TextLayout, encode_text


def _error_response(message: Any, *, cancelled: bool = False, **data_fields: Any) -> dict[str, Any]:
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}", **data_fields}, "cancelled": cancelled}


def _content_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    # A JSON object passed as content would otherwise land as a Python repr, which no parser accepts.
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    return str(raw)


@dataclass(frozen=True)
class _Written:
    gate: file_state.Gate
    created: bool = False
    byte_count: int = 0
    stamp: file_state.Stamp | None = None


def _commit(session: str, resolved: Path, content: str) -> _Written:
    """Write under the per-file edit lock; ``stamp`` stays None when the stamp check refuses."""
    with file_state.edit_lock(resolved):
        snapshot = file_state.take_snapshot(resolved)
        gate = file_state.inspect(session, resolved, snapshot)
        if gate.stale:
            return _Written(gate)
        layout = TextLayout.of_bytes(snapshot.data) if snapshot.exists else NEW_FILE
        data = encode_text(content, layout)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        stamp = file_state.commit_locked(resolved, data, expected=snapshot)
        # The model wrote every byte of this version, so echoing it later reveals nothing new.
        file_state.remember(session, resolved, stamp, revealable=True)
        return _Written(gate, not snapshot.exists, len(data), stamp)


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path") or "").strip()
    if not path:
        return _error_response("'path' is required.")
    if args.get("content") is None:
        return _error_response("'content' is required; pass an empty string to write an empty file.", path=path)
    content = _content_text(args["content"])

    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path,
            "content_preview": content[:200],
            "content_len": len(content),
        },
    )
    if err is not None:
        error_text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
        return _error_response(error_text, cancelled=bool(isinstance(err, dict) and err.get("cancelled")))

    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path)
    except ValueError as exc:
        return _error_response(str(exc), path=path)
    if resolved.is_dir():
        return _error_response(f"{path} 是目录，不能当文件写", path=path)

    try:
        written = await asyncio.to_thread(_commit, file_state.session_key(ctx), resolved, content)
    except file_state.ConcurrentEdit as exc:
        return _error_response(str(exc), path=path, conflict=True)
    except OSError as exc:
        return _error_response(f"写入失败：{exc}", path=path)
    if written.stamp is None:
        return _error_response(file_state.STALE_MESSAGE, path=path, stale=True)

    gate = written.gate
    unread_overwrite = not written.created and gate.unread
    lines = [f"已写入 {path}（{'新建文件' if written.created else '覆盖原文件'}，{written.byte_count} 字节）"]
    if unread_overwrite:
        lines.append("注意：本会话之前没有读过这个文件，原内容已被整份覆盖。")
    if written.created and gate.vanished:
        lines.append("注意：这个文件在你上次读取之后被删掉了，这次按新文件写入。")

    checks = await run_edit_checks(ctx, resolved, content, tool_name="write_file")
    lines.extend(checks.lines())
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "path": path,
        "bytes": written.byte_count,
        "created": written.created,
        **checks.to_data(),
    }
    if unread_overwrite:
        data["unreadOverwrite"] = True
    return {"ok": True, "data": data}
