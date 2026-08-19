"""Schedule management: create_schedule, list_schedules, delete_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule tools return structured management data but
    # still need a canonical readable transcript. How: place all structured fields
    # under data with a result summary. Purpose: align schedules with ok/data/error.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule validation and approval failures should not
    # return legacy ok=false shapes. How: mirror optional flags under data and top
    # level while adding data.result. Purpose: keep scheduler failures readable.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _platform_admin_can_manage_schedules(ctx: ToolContext) -> bool:
    """Return whether the current platform user may manage schedules without approval.

    [QQ schedule 2026-06-21] Why: create_schedule used the generic write_file
    guard for data/schedules.yaml. In QQ chats this produced approval_requested
    events and the task waited forever if the approval prompt was not surfaced.
    How: trust the platform_auth.is_admin flag that adapters already derive from
    server-side admin lists. Purpose: QQ admins can create/delete reminders as a
    normal chat action without blocking on a second write_file approval.
    """
    auth = getattr(ctx, "platform_auth", None)
    if not isinstance(auth, dict):
        return False
    platform = str(auth.get("platform") or "").strip().lower()
    return platform in {"qq", "onebot", "onebot11"} and bool(auth.get("is_admin"))


def _resolve_schedule_conversation_key(args: dict[str, Any], ctx: ToolContext, sid: str) -> str:
    """Resolve the conversation key a schedule should fire back to.

    [QQ schedule 2026-06-21] Why: models may pass conversation_key="scheduler:*"
    while executing inside a QQ chat. That creates a schedule that fires into an
    internal scheduler channel and cannot be delivered back to QQ. How: when the
    current ToolContext is already a QQ conversation, prefer it over model-provided
    scheduler/empty keys. Purpose: reminders created from QQ reliably return to
    the originating QQ private/group chat.
    """
    arg_key = str(args.get("conversation_key") or "").strip()
    ctx_key = str(getattr(ctx, "conversation_key", "") or "").strip()
    if ctx_key.startswith(("qq_private:", "qq_group:")):
        return ctx_key
    if arg_key:
        return arg_key
    if ctx_key:
        return ctx_key
    return f"scheduler:{sid}"


async def create_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return _err("invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed")

    cron_expr = str(args.get("cron") or "").strip()
    if not cron_expr:
        return _err("empty cron expression")
    parts = cron_expr.split()
    if len(parts) != 5:
        return _err("cron must be 5 fields: minute hour day month weekday")

    stype = str(args.get("type") or "message").strip()
    if stype not in ("message", "script"):
        return _err(f"invalid type: {stype}, must be 'message' or 'script'")

    text = str(args.get("text") or "").strip()
    if stype == "message" and not text:
        return _err("empty text (required for message type)")

    command = str(args.get("command") or "").strip()
    if stype == "script" and not command:
        return _err("empty command (required for script type)")

    conv_key = _resolve_schedule_conversation_key(args, ctx, sid)
    entry_node_id = str(args.get("entry_node_id") or "").strip()
    workflow_id = str(args.get("workflow_id") or "").strip()
    enabled = bool(args.get("enabled", True))
    once = bool(args.get("once", False))

    if stype == "script":
        # scheduler 执行 script 走 subprocess.run(shell=True)，等于一条定时的
        # execute_command。管理员快捷通道只覆盖「改 schedules.yaml」这件事，不能顺带
        # 把 deny_patterns / sensitive_patterns / cmd_reviewer 三道闸一起绕开。
        _op, err = await request_guard(ctx, "execute_command", {"command": command, "schedule_id": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    if not _platform_admin_can_manage_schedules(ctx):
        _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    entry: dict[str, Any] = {
        "id": sid,
        "cron": cron_expr,
        "conversation_key": conv_key,
        "enabled": enabled,
        "once": once,
    }
    if stype == "script":
        entry["type"] = "script"
        entry["command"] = command
        timeout = int(args.get("timeout") or 30)
        entry["timeout"] = max(5, min(timeout, 300))
        entry["silent"] = bool(args.get("silent", True))
        if text:
            entry["text"] = text
    else:
        entry["text"] = text
    if entry_node_id:
        entry["entry_node_id"] = entry_node_id
    if workflow_id:
        entry["workflow_id"] = workflow_id

    replaced = False
    for i, s in enumerate(schedules):
        if str(s.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule {'updated' if replaced else 'created'}: {sid}", schedule=entry, replaced=replaced)


async def list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List all scheduled tasks."""
    from supervisor.scheduler import load_schedules

    # 条目含 type:script 的 command 与全部会话的 conversation_key，是读 data/schedules.yaml
    # 的等价物；与 create/delete 走同一鉴权，避免三个函数策略不一致。
    if not _platform_admin_can_manage_schedules(ctx):
        _op, err = await request_guard(ctx, "read_file", {"path": "data/schedules.yaml"})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    return _ok(f"{len(schedules)} schedules", schedules=schedules)


async def delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")

    if not _platform_admin_can_manage_schedules(ctx):
        _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    before = len(schedules)
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(schedules) == before:
        return _err(f"schedule not found: {sid}")

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule deleted: {sid}", deleted=True, id=sid)
