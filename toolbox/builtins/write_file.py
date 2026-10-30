"""write_file — write a whole text file (policy + approval guarded)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config_layout

from .. import edit_journal, file_state
from ..context import ToolContext, project_origin_of, project_root_of
from .._common import request_guard, resolve_under_allowed_roots
from ..syntax_check import run_edit_checks
from ..text_files import NEW_FILE, TextLayout, encode_text
from ._edit_flow import stale_fields


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
    journal: edit_journal.JournalNote | None = None


def _commit(
    scope: edit_journal.JournalScope, resolved: Path, content: str, display: str, grant: file_state.EditGrant,
) -> _Written:
    """Write under the cross-process edit lock; ``stamp`` stays None when the stamp check refuses."""
    with file_state.edit_lock(resolved, scope.workspace_root):
        snapshot = file_state.take_snapshot(resolved)
        gate = grant.gate(snapshot)
        if gate.stale:
            return _Written(gate)
        layout = TextLayout.of_bytes(snapshot.data) if snapshot.exists else NEW_FILE
        data = encode_text(content, layout)
        # Recorded before the directories are created, so undoing a new file can remove them again.
        pending = edit_journal.record_pre_image(
            scope, resolved, snapshot, tool="write_file", display=display, revealable=gate.revealable,
        )
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            new_mode = None if snapshot.exists else config_layout.private_mode_for(scope.workspace_root, resolved)
            stamp = file_state.commit_locked(resolved, data, expected=snapshot, new_file_mode=new_mode)
        except BaseException:
            pending.discard()
            raise
        note = pending.record_post_image(stamp)
        return _Written(gate, not snapshot.exists, len(data), stamp, note)


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path") or "").strip()
    if not path:
        return _error_response("'path' is required.")
    if args.get("content") is None:
        return _error_response("'content' is required; pass an empty string to write an empty file.", path=path)
    content = _content_text(args["content"])

    try:
        resolved = resolve_under_allowed_roots(
            ctx.workspace_root, path, project_root=project_root_of(ctx), project_origin=project_origin_of(ctx),
        )
    except ValueError as exc:
        return _error_response(str(exc), path=path)
    if edit_journal.inside_journal(ctx.workspace_root, resolved):
        return _error_response(edit_journal.JOURNAL_REFUSED_MESSAGE, path=path)

    # Claim before asking: a file another task is editing should not raise an approval that is doomed anyway.
    try:
        grant = await file_state.begin_edit(ctx, resolved, path)
    except file_state.EditBlocked as blocked:
        return _error_response(blocked.message, path=path, **blocked.data)

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
        await file_state.abandon_edit(ctx, grant)
        error_text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
        return _error_response(error_text, cancelled=bool(isinstance(err, dict) and err.get("cancelled")))
    if resolved.is_dir():
        await file_state.abandon_edit(ctx, grant)
        return _error_response(f"{path} 是目录，不能当文件写", path=path)

    scope = edit_journal.JournalScope.of(ctx, session=grant.journal_session)
    try:
        written = await asyncio.to_thread(_commit, scope, resolved, content, path, grant)
    except file_state.ConcurrentEdit as exc:
        await file_state.abandon_edit(ctx, grant)
        return _error_response(str(exc), path=path, conflict=True)
    except OSError as exc:
        await file_state.abandon_edit(ctx, grant)
        return _error_response(f"写入 {path} 失败：{config_layout.config_error_text(exc)}", path=path)
    if written.stamp is None:
        await file_state.abandon_edit(ctx, grant)
        return _error_response(written.gate.stale_message, path=path, **stale_fields(written.gate))
    # The model wrote every byte of this version, so echoing it later reveals nothing new.
    stamp_recorded = await file_state.finish_edit(ctx, grant, written.stamp, revealable=True)

    gate = written.gate
    unread_overwrite = not written.created and gate.unread
    lines = [f"已写入 {path}（{'新建文件' if written.created else '覆盖原文件'}，{written.byte_count} 字节）"]
    if unread_overwrite:
        lines.append("注意：本会话之前没有读过这个文件，原内容已被整份覆盖。")
    if written.created and gate.vanished:
        lines.append("注意：这个文件在你上次读取之后被删掉了，这次按新文件写入。")
    if written.journal is not None:
        lines.extend(written.journal.lines())
    if not stamp_recorded:
        lines.append(file_state.STAMP_NOT_RECORDED_NOTE)

    checks = await run_edit_checks(ctx, resolved, content, tool_name="write_file")
    lines.extend(checks.lines())
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "path": path,
        "bytes": written.byte_count,
        "created": written.created,
        **(written.journal.to_data() if written.journal is not None else {}),
        **checks.to_data(),
    }
    if unread_overwrite:
        data["unreadOverwrite"] = True
    if not stamp_recorded:
        data["stampRecorded"] = False
    return {"ok": True, "data": data}
