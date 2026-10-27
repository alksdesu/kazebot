"""Tool management: create_or_update_tool, reload_tools."""
from __future__ import annotations

import pprint
import re
from typing import Any

import config_layout
from clonoth_sdk.refusals import refusal_reason

from ..context import ToolContext
from ..reload_seqs import ReloadSeqs
from ..tool_names import TOOL_NAME_RE

# The announcement only bumps counters on the supervisor; a supervisor that takes longer is treated as unreachable.
_ANNOUNCE_TIMEOUT_SEC = 10.0


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: tool management returns paths and counts but the
    # unified schema requires a readable data.result. How: put all fields under
    # data alongside a concise result message. Purpose: keep tool creation and
    # reload output consistent with other tools.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: validation and reload failures should not use legacy
    # top-level-only error payloads. How: add data.result and mirror optional
    # metadata. Purpose: keep failed tool-management calls readable.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


# ---------------------------------------------------------------------------
#  Code generation
# ---------------------------------------------------------------------------

def _render_tool_py(*, spec: dict[str, Any], script_body: str, timeout_sec: float | None) -> str:
    """Generate a tool .py file from spec and user script body."""
    lines: list[str] = []
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append(
        '"""\n'
        "External tool (Clonoth).\n"
        "\n"
        "The engine parses SPEC via AST at registration time.\n"
        "At invocation this file runs as a subprocess:\n"
        "  - Input: tool arguments as JSON on stdin\n"
        "  - Output: result as JSON on stdout\n"
        "  - Sensitive env vars are stripped\n"
        '"""'
    )
    lines.append("")
    lines.append("SPEC = " + pprint.pformat(spec, width=100))

    if timeout_sec is not None:
        lines.append("")
        lines.append(f"TIMEOUT_SEC = {float(timeout_sec)}")

    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append('    import json, sys')
    lines.append('    _input = json.loads(sys.stdin.read())')
    lines.append('    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)')
    lines.append('    def fail(error): print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)')
    lines.append('    args = _input')

    for line in script_body.splitlines():
        lines.append("    " + line)

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Tool functions
# ---------------------------------------------------------------------------

async def create_or_update_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update an external tool under config/tools/."""
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    input_schema = args.get("input_schema")
    timeout_sec = args.get("timeout_sec")
    script = args.get("script")

    if not name:
        return _err("empty tool name")
    if not TOOL_NAME_RE.fullmatch(name):
        return _err("invalid tool name: only [A-Za-z_][A-Za-z0-9_]{0,63} is allowed")
    reserved = ctx.registry.reserved_name_reason(name)
    if reserved:
        return _err(f"reserved tool name: {name} ({reserved})")
    if not isinstance(script, str) or not script.strip():
        return _err("'script' is required")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}

    spec = {"name": name, "description": description, "input_schema": input_schema}
    code = _render_tool_py(
        spec=spec,
        script_body=script.strip(),
        timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
    )

    from .write_file import write_file
    # 和仓库 tools/ 里同名同路径的脚本会顶掉仓库那份，控制台的工具页把它标成改过的版本。
    path = f"{config_layout.TOOLS_DIR}/{name}.py"
    res = await write_file({"path": path, "content": code}, ctx)
    if not res.get("ok"):
        return res

    try:
        count = ctx.registry.reload()
    except Exception as e:
        return _err(f"tool written but reload failed: {e}", path=path)

    return _ok(f"Tool written: {path}", path=path, reloaded=count)


async def _announce_reload(ctx: Any, *, mcp: bool) -> tuple[ReloadSeqs | None, str]:
    """Ask the supervisor to make every worker follow. Returns the sequences it answered with, or None and the reason."""
    http = getattr(ctx, "http", None)
    supervisor_url = str(getattr(ctx, "supervisor_url", "") or "").rstrip("/")
    if http is None or not supervisor_url:
        return None, "no supervisor connection in this context"
    try:
        response = await http.post(
            f"{supervisor_url}/v1/tools/reload", json={"mcp": mcp}, timeout=_ANNOUNCE_TIMEOUT_SEC,
        )
    except Exception as exc:
        return None, f"supervisor unreachable ({type(exc).__name__}: {exc})"
    if response.status_code >= 400:
        return None, refusal_reason(response)
    try:
        seqs = ReloadSeqs.from_payload(response.json())
    except Exception:
        seqs = None
    if seqs is None:
        return None, "the supervisor answered without reload sequences"
    return seqs, ""


async def reload_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Rescan script tools and restart every MCP client's sessions, in this worker and, through the supervisor, in every other."""
    registry = ctx.registry
    try:
        count = registry.reload()
        config_error = registry.mcp_config_error()
    except Exception as e:
        return _err(e)

    # Restarting on a broken file only reconnects with the old config and throws away every conversation's session state.
    seqs, unannounced = await _announce_reload(ctx, mcp=not config_error)
    if config_error:
        return _err(
            f"{config_error}. Script tools were rescanned ({count} tools); MCP tools were left as they are and every "
            "worker picks the file up by itself once it is fixed",
            tools=count, mcp_tools=registry.mcp_tool_count(),
        )

    try:
        if seqs is not None:
            await registry.sync_mcp_tools(seqs)
        else:
            await registry.load_mcp_tools()
        mcp_count = registry.mcp_tool_count()
    except Exception as e:
        return _err(f"script tools were rescanned ({count} tools) but relisting MCP tools failed: {e}", tools=count)

    if unannounced:
        return _ok(
            f"Tools reloaded in this worker only: {count} local, {mcp_count} MCP. The other workers were not notified "
            f"({unannounced}) and keep their tools until the config changes or they restart",
            tools=count, mcp_tools=mcp_count, other_workers_notified=False,
        )
    return _ok(
        f"Tools reloaded: {count} local, {mcp_count} MCP; the other workers follow within a few seconds",
        tools=count, mcp_tools=mcp_count, other_workers_notified=True,
    )
