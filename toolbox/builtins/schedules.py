"""Schedule management: create_schedule, list_schedules, delete_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
# 非管理员的额度：够用来记几件事，又不至于让一个人把 schedules.yaml 撑爆。
_MAX_SCHEDULES_PER_USER = 10
_MIN_INTERVAL_MINUTES = 5
_MAX_MINUTE_POINTS = 4


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


def _creator_id(ctx: ToolContext) -> str:
    auth = getattr(ctx, "platform_auth", None)
    return str((auth or {}).get("user_id") or "").strip() if isinstance(auth, dict) else ""


def _originating_conversation_key(ctx: ToolContext) -> str:
    """真正发起这一轮的 QQ 会话 key，取不到返回空串。

    dispatch 子节点运行期的 conversation_key 是 agent:...，只看它就等于让模型绕一层
    委派便能自己指定投递目标；route/parent 才是发起会话，与记忆工具的寻址口径一致。
    """
    for candidate in (
        getattr(ctx, "_route_conversation_key", ""),
        getattr(ctx, "_parent_conversation_key", ""),
        getattr(ctx, "conversation_key", ""),
    ):
        text = str(candidate or "").strip()
        if text.startswith(("qq_private:", "qq_group:")):
            return text
    return ""


def _minute_field_is_too_frequent(cron_expr: str) -> bool:
    """非管理员不得建每分钟级的任务：一条 `* * * * *` 就是永久刷屏 + 每分钟一个 task。"""
    minute = cron_expr.split()[0].strip()
    if minute == "*":
        return True
    if minute.startswith("*/"):
        try:
            return int(minute[2:]) < _MIN_INTERVAL_MINUTES
        except ValueError:
            return True
    # 逗号列出十几个分钟点同样是高频，按点数判。
    return len([p for p in minute.split(",") if p.strip()]) > _MAX_MINUTE_POINTS


def _resolve_schedule_conversation_key(args: dict[str, Any], ctx: ToolContext, sid: str) -> str:
    """Resolve the conversation key a schedule should fire back to.

    [QQ schedule 2026-06-21] Why: models may pass conversation_key="scheduler:*"
    while executing inside a QQ chat. That creates a schedule that fires into an
    internal scheduler channel and cannot be delivered back to QQ. How: when the
    current ToolContext is already a QQ conversation, prefer it over model-provided
    scheduler/empty keys. Purpose: reminders created from QQ reliably return to
    the originating QQ private/group chat.
    """
    origin = _originating_conversation_key(ctx)
    if origin:
        return origin
    arg_key = str(args.get("conversation_key") or "").strip()
    ctx_key = str(getattr(ctx, "conversation_key", "") or "").strip()
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

    is_admin = _platform_admin_can_manage_schedules(ctx)
    origin = _originating_conversation_key(ctx)
    creator = _creator_id(ctx)
    existing = {str(s.get("id") or "").strip(): s for s in load_schedules(ctx.workspace_root)}
    if not is_admin and origin:
        # QQ 普通群友：放开定时提醒，但投递目标与入口节点必须由服务端钉死。
        # 到点注入的 inbound 直接走 entry_node，指定成 cmd_reviewer 之类就绕开了
        # qq.orchestrator 的白名单 —— 这一条不设限等于给群友一条命令执行通道。
        if entry_node_id or workflow_id:
            return _err("only Clonoth admins may pin an entry node or workflow on a schedule")
        if stype == "script":
            return _err("only Clonoth admins may create script schedules")
        if _minute_field_is_too_frequent(cron_expr):
            return _err(
                f"schedules more frequent than every {_MIN_INTERVAL_MINUTES} minutes are admin-only"
            )
        prior = existing.get(sid)
        if prior is not None and str(prior.get("created_by") or "") != creator:
            return _err(f"schedule {sid} belongs to someone else")
        owned = sum(1 for s in existing.values() if str(s.get("created_by") or "") == creator)
        if prior is None and owned >= _MAX_SCHEDULES_PER_USER:
            return _err(f"you already have {_MAX_SCHEDULES_PER_USER} schedules; delete one first")
    elif not is_admin:
        # 非 QQ 渠道解析不出发起会话，维持原样交给 policy 与人工审批。
        _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))
    enabled = bool(args.get("enabled", True))
    once = bool(args.get("once", False))

    if stype == "script":
        # scheduler 执行 script 走 subprocess.run(shell=True)，等于一条定时的
        # execute_command。管理员快捷通道只覆盖「改 schedules.yaml」这件事，不能顺带
        # 把 deny_patterns / sensitive_patterns / cmd_reviewer 三道闸一起绕开。
        _op, err = await request_guard(ctx, "execute_command", {"command": command, "schedule_id": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = list(existing.values())
    entry: dict[str, Any] = {
        "id": sid,
        "cron": cron_expr,
        "conversation_key": conv_key,
        "enabled": enabled,
        "once": once,
    }
    # 到点注入时按它回填 platform_auth：没有创建者身份，运行期就拿不到「非管理员」
    # 这个事实，QQ 那套硬规则会整段落空。
    if creator:
        entry["created_by"] = creator
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

    is_admin = _platform_admin_can_manage_schedules(ctx)
    origin = _originating_conversation_key(ctx)
    if not is_admin and not origin:
        _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
        if err is not None:
            return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    target = next((s for s in schedules if str(s.get("id") or "").strip() == sid), None)
    if target is None:
        return _err(f"schedule not found: {sid}")
    # 条目本身不带渠道信息，没有归属校验的话群友能删掉管理员建的任何定时任务。
    if not is_admin and str(target.get("created_by") or "") != _creator_id(ctx):
        return _err(f"schedule {sid} belongs to someone else")
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule deleted: {sid}", deleted=True, id=sid)
