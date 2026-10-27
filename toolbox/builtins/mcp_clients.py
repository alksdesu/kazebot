"""MCP client management tools."""
from __future__ import annotations

from typing import Any

import config_layout
from clonoth_runtime import SwitchValueError

from ..context import ToolContext
from .._common import request_guard
from .. import mcp_runtime

_CONFIG_PATH = config_layout.MCP_FILE


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: MCP client management tools also need the unified
    # data.result field. How: centralize success payload creation and keep all
    # previous structured fields under data. Purpose: make management-tool output
    # readable and schema-consistent.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: failures from MCP client management should include a
    # readable data.result. How: wrap the error string once. Purpose: avoid legacy
    # ok=false payloads without data.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


async def create_or_update_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # 开关写错、文件写坏着，都在送审前就拒，不让审批人批一份存不进去的配置
    try:
        mcp_runtime.requested_switches(args)
        mcp_runtime.editable_config(ctx.workspace_root)
    except (SwitchValueError, mcp_runtime.McpConfigError) as exc:
        return _err(exc)
    # stdio client 的 command 保存后几秒内就由每个 worker 起子进程列工具表，写这份配置
    # 等价于创建可执行工具，因此必须与 write_file 同权限——upsert_client 会直接落盘，
    # 不经 write_file 工具，少了这道 guard 就是绕过 execute_command 限制的后门。
    _op, err = await request_guard(ctx, "write_file", {"path": _CONFIG_PATH, "mcp_client": str(args.get("id") or "")})
    if err is not None:
        return _err(err.get("error", "denied"))
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as e:
        return _err(e)
    return _ok(f"MCP client saved: {spec.get('id', '')}", client=spec, path=_CONFIG_PATH)


async def list_mcp_clients(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # 返回的 spec 含 stdio 的 command/env 与 http 的 headers 原文，等价于读配置文件。
    _op, err = await request_guard(ctx, "read_file", {"path": _CONFIG_PATH})
    if err is not None:
        return _err(err.get("error", "denied"))
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
        problem = mcp_runtime.config_error(ctx.workspace_root)
    except Exception as e:
        return _err(e)
    if problem:
        # 文件写坏时列出来的是还在用的那份，不说清楚模型会以为刚改的已经生效
        return _ok(f"{problem}. {len(clients)} MCP clients in use", clients=clients, config_error=problem)
    return _ok(f"{len(clients)} MCP clients", clients=clients)


async def delete_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    client_id = str(args.get("id", "")).strip()
    if not client_id:
        return _err("empty client id")
    # 文件写坏着删不成，同样在送审前就拒
    try:
        mcp_runtime.editable_config(ctx.workspace_root)
    except mcp_runtime.McpConfigError as exc:
        return _err(exc)
    _op, err = await request_guard(ctx, "write_file", {"path": _CONFIG_PATH, "delete_mcp_client": client_id})
    if err is not None:
        return _err(err.get("error", "denied"))
    try:
        ok = mcp_runtime.delete_client(ctx.workspace_root, client_id)
        if not ok:
            return _err(f"client not found: {client_id}")
        return _ok(f"MCP client deleted: {client_id}", deleted=True, id=client_id)
    except Exception as e:
        return _err(e)
