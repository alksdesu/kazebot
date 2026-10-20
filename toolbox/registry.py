from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Literal

import config_layout
from clonoth_runtime import get_float, load_runtime_config

from . import builtins as _builtins
from ._common import kill_process_group as _kill_process_group
from ._common import request_guard as _request_guard
from ._common import safe_subprocess_env as _safe_subprocess_env
from . import mcp_runtime
from .tool_names import is_engine_owned_name


log = logging.getLogger(__name__)

ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


def _json_text(value: Any) -> str:
    # [AutoC 2026-05-31] Why: registry-level fallback wrapping needs a stable
    # readable transcript for arbitrary legacy tool payloads. How: serialize JSON
    # values compactly and fall back to str() if serialization fails. Purpose: let
    # every tool result expose data.result even when the tool itself is old.
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _error_tool_response(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: registry-generated failures, such as missing tools or
    # script timeouts, bypass individual tool code. How: put the error in both error
    # and data.result, with optional metadata under data and top level. Purpose:
    # keep all registry-originated failures in the unified ok/data/error shape.
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    data.update(fields)
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _ensure_tool_response_shape(result: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: user-created external tools and older built-ins may
    # still return legacy top-level fields. How: preserve already unified payloads,
    # add data.result to ok=false payloads, and otherwise move legacy fields under
    # data while mirroring attachments during migration. Purpose: enforce the new
    # ok/data/error contract at the registry boundary without losing metadata.
    if not isinstance(result, dict):
        return {"ok": True, "data": {"result": str(result), "value": result}}

    data = result.get("data") if isinstance(result.get("data"), dict) else None
    if isinstance(data, dict) and isinstance(data.get("result"), str) and "ok" in result:
        # [AutoC 2026-05-31] Why: some already-migrated tools may still provide
        # attachments only at the top level for migration compatibility. How: add
        # the nested data.attachments mirror when it is missing. Purpose: keep the
        # attachment contract consistent at the registry boundary.
        attachments = result.get("attachments")
        if isinstance(attachments, list) and not isinstance(data.get("attachments"), list):
            result = dict(result)
            data = dict(data)
            data["attachments"] = attachments
            result["data"] = data
        return result

    ok_value = False if result.get("ok") is False else True
    error_value = result.get("error")
    if ok_value is False:
        merged_data = dict(data or {})
        if not isinstance(merged_data.get("result"), str):
            merged_data["result"] = f"ERROR: {error_value or 'unknown'}"
        for key, value in result.items():
            if key not in {"ok", "data", "error"} and key not in merged_data:
                merged_data[key] = value
        return {"ok": False, "data": merged_data, "error": str(error_value or merged_data.get("result") or "unknown")}

    merged_data = dict(data or {})
    for key, value in result.items():
        if key in {"ok", "data", "error"}:
            continue
        merged_data.setdefault(key, value)
    if not isinstance(merged_data.get("result"), str):
        for key in ("text", "description", "message", "output", "result", "note"):
            value = result.get(key)
            if isinstance(value, str):
                merged_data["result"] = value
                break
    if not isinstance(merged_data.get("result"), str):
        merged_data["result"] = _json_text({k: v for k, v in result.items() if k not in {"ok", "data", "error"}})

    response = {"ok": True, "data": merged_data}
    attachments = merged_data.get("attachments") or result.get("attachments")
    if isinstance(attachments, list):
        response["attachments"] = attachments
        merged_data.setdefault("attachments", attachments)
    return response


def _extract_tool_spec(py: Path) -> tuple[dict[str, Any] | None, float | None]:
    """Extract tool spec from a python file via AST.

    External tool format:
        SPEC = { ... }
        TIMEOUT_SEC = 60  # optional

        if __name__ == "__main__":
            # reads JSON from stdin, writes JSON to stdout

    Security:
    - We do NOT import or execute the module at registration time.
    - We only parse literals via ast.literal_eval.
    - At invocation, the script runs as an isolated subprocess.
    """

    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        return None, None

    try:
        tree = ast.parse(text, filename=str(py))
    except SyntaxError:
        return None, None

    vals: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id
            if name not in {"SPEC", "TIMEOUT_SEC"}:
                continue
            try:
                vals[name] = ast.literal_eval(node.value)
            except Exception:
                continue

    spec = vals.get("SPEC")
    if not isinstance(spec, dict):
        return None, None

    timeout_sec: float | None = None
    if isinstance(vals.get("TIMEOUT_SEC"), (int, float)):
        timeout_sec = float(vals.get("TIMEOUT_SEC"))

    return spec, timeout_sec


# 注册与列举共用同一份提取器，否则「界面列得出来」和「引擎认」会各自漂移。
extract_tool_spec = _extract_tool_spec


def iter_external_tool_files(tools_dir: Path) -> Iterator[Path]:
    """一个工具目录下算作外部工具的 .py。

    stocktool、drawtools 这类工具包把入口放在子目录里，所以要递归；包初始化文件、
    下划线开头的私有模块和 .disabled.py 都不是工具入口。
    """
    if not tools_dir.is_dir():
        return
    for py in sorted(tools_dir.rglob("*.py")):
        if py.name == "__init__.py" or py.name.startswith("_") or py.name.endswith(".disabled.py"):
            continue
        if any(part == "__pycache__" for part in py.parts):
            continue
        yield py


ToolOrigin = Literal["custom", "override", "bundled"]


@dataclass(frozen=True)
class ExternalToolFile:
    """一个实际生效的脚本工具文件。rel 是它在所属工具目录里的相对路径，同一个 rel 实例那份顶掉仓库那份。"""

    path: Path
    rel: str
    origin: ToolOrigin
    base: Path

    @property
    def is_bundled(self) -> bool:
        return self.origin == "bundled"


def external_tool_files(workspace_root: Path, bundled_dir: Path | None = None) -> list[ExternalToolFile]:
    """本实例的脚本工具：config/tools/ 里自己加的和改过的排在前，仓库 tools/ 里没被顶掉的在后。

    注册表和控制台都按这个顺序取，同名时先出现的那个算数。
    """
    custom = config_layout.custom_tools_dir(workspace_root)
    bundled = bundled_dir if bundled_dir is not None else config_layout.workspace_tools_dir(workspace_root)
    # 改造前自定义工具直接写在代码目录的 tools/ 里，搬家后旧文件还在：当成仓库工具的话，删掉自己那份它又冒出来。
    leftovers = config_layout.legacy_tool_rels(workspace_root)
    bundled_files = {
        rel: path
        for path in iter_external_tool_files(bundled)
        if (rel := path.relative_to(bundled).as_posix()) not in leftovers
    }
    found: list[ExternalToolFile] = []
    for path in iter_external_tool_files(custom):
        rel = path.relative_to(custom).as_posix()
        found.append(ExternalToolFile(path, rel, "override" if rel in bundled_files else "custom", custom))
    taken = {item.rel for item in found}
    found.extend(
        ExternalToolFile(path, rel, "bundled", bundled)
        for rel, path in bundled_files.items()
        if rel not in taken
    )
    return found


def custom_tool_python_path(item: ExternalToolFile, bundled_dir: Path) -> tuple[str, ...]:
    """实例自己的工具跑起来时补的 import 路径：仓库里同一层目录、仓库 tools/ 和代码根。

    改过的 drawtools 入口还得 import 仓库那份 common，新写的工具照抄 from _channel import 也得能用。
    """
    if item.is_bundled:
        return ()
    sibling = bundled_dir / Path(item.rel).parent
    candidates = (sibling, bundled_dir, config_layout.CODE_ROOT)
    return tuple(dict.fromkeys(str(path) for path in candidates if path.is_dir()))


_SCRIPT_IDENTITY_ENV_KEYS = {
    "CLONOTH_TOOL_CALL_ID",
    "CLONOTH_REQUEST_ID",
    "CLONOTH_TOOL_INVOCATION_ID",
}


def _script_tool_env(ctx: Any) -> dict[str, str]:
    """Build the external-tool environment with an explicit identity allowlist.

    Parent-process identity variables are removed first: a missing value on the
    current ToolContext must never inherit a stale provider call from the Engine.
    ``run_id`` is intentionally excluded because it can span multiple tool calls.
    """
    env = _safe_subprocess_env()
    for key in list(env):
        if key.upper() in _SCRIPT_IDENTITY_ENV_KEYS:
            env.pop(key, None)

    env.update({
        "CLONOTH_SUPERVISOR_URL": str(getattr(ctx, "supervisor_url", "") or ""),
        "CLONOTH_SESSION_ID": str(getattr(ctx, "session_id", "") or ""),
        "CLONOTH_PARENT_SESSION_ID": str(getattr(ctx, "parent_session_id", "") or ""),
        "CLONOTH_TASK_ID": str(getattr(ctx, "task_id", "") or ""),
        "CLONOTH_NODE_ID": str(getattr(ctx, "node_id", "") or ""),
        "CLONOTH_CONVERSATION_KEY": str(getattr(ctx, "conversation_key", "") or ""),
    })

    optional_identity = (
        ("CLONOTH_TOOL_CALL_ID", ("tool_call_id",)),
        ("CLONOTH_REQUEST_ID", ("request_id", "llm_request_id", "current_llm_request_id")),
        ("CLONOTH_TOOL_INVOCATION_ID", ("tool_invocation_id", "invocation_id")),
    )
    for env_name, attr_names in optional_identity:
        for attr_name in attr_names:
            value = str(getattr(ctx, attr_name, "") or "").strip()
            if value:
                env[env_name] = value
                break
    return env


async def _script_guard_error(
    guard: dict[str, Any], tool_name: str, args: dict[str, Any], ctx: Any,
) -> dict[str, Any] | None:
    """跑脚本前先过 supervisor 的策略与审批，放行返回 None。

    脚本本体在子进程里，拿不到 ToolContext，这层 wrapper 是唯一挂得上闸门的地方。
    """
    op = str(guard.get("op") or "").strip()
    if not op:
        return None

    parameters: dict[str, Any] = {"tool_name": tool_name}
    mapping = guard.get("params")
    if isinstance(mapping, dict):
        for policy_key, arg_key in mapping.items():
            parameters[str(policy_key)] = args.get(str(arg_key), "")

    _op_res, err = await _request_guard(ctx, op, parameters)
    if err is None:
        return None
    return _error_tool_response(
        err.get("error", "denied"),
        **{k: v for k, v in err.items() if k not in {"ok", "error"}},
    )


def _with_python_path(env: dict[str, str], extra: tuple[str, ...]) -> dict[str, str]:
    if not extra:
        return env
    inherited = [part for part in str(env.get("PYTHONPATH") or "").split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*extra, *inherited]))
    return env


def _make_script_tool(
    *,
    script_path: Path,
    timeout_sec: float | None,
    tool_name: str = "",
    guard: dict[str, Any] | None = None,
    python_path: tuple[str, ...] = (),
) -> ToolFunc:
    """Create a tool function that runs a Python script as a subprocess.

    Protocol:
    - Input: tool arguments as JSON on stdin
    - Output: result as JSON on stdout
    - Environment: sensitive variables stripped
    - Timeout: configurable
    - Guard: SPEC["guard"] declares the policy op to clear before running
    """

    async def _run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if guard:
            denied = await _script_guard_error(guard, tool_name, args or {}, ctx)
            if denied is not None:
                return denied

        root = getattr(ctx, "workspace_root", None)
        default_timeout_sec = 60.0
        if isinstance(root, Path):
            try:
                runtime_cfg = load_runtime_config(root)
                default_timeout_sec = get_float(
                    runtime_cfg,
                    "tools.script.default_timeout_sec",
                    60.0,
                    min_value=1.0,
                    max_value=3600.0,
                )
            except Exception:
                default_timeout_sec = 60.0

        timeout_val = float(timeout_sec) if timeout_sec is not None else float(default_timeout_sec)
        max_output = 16000

        input_json = json.dumps(args or {}, ensure_ascii=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root) if isinstance(root, Path) else None,
                env=_with_python_path(_script_tool_env(ctx), python_path),
                # Fix: run in new session so os.killpg can kill the entire
                # process tree, preventing orphaned grandchildren from holding
                # pipe fds and causing communicate() to hang forever.
                start_new_session=True,
            )
            waiter = asyncio.create_task(proc.communicate(input=input_json.encode("utf-8")))
            started = asyncio.get_running_loop().time()
            while True:
                done, _pending = await asyncio.wait({waiter}, timeout=0.2)
                if waiter in done:
                    stdout_bytes, stderr_bytes = waiter.result()
                    break
                try:
                    if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                        # Fix: kill entire process group, not just the interpreter
                        _kill_process_group(proc)
                        # Fix: 5s safety timeout — if killpg didn't clean all
                        # grandchildren, don't hang forever waiting for pipe EOF
                        try:
                            await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        return _error_tool_response("script task cancelled", cancelled=True)
                except Exception:
                    pass
                if asyncio.get_running_loop().time() - started >= timeout_val:
                    _kill_process_group(proc)
                    try:
                        await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    return _error_tool_response(f"script timeout after {timeout_val}s")
        except asyncio.TimeoutError:
            try:
                _kill_process_group(proc)  # type: ignore[union-attr]
            except Exception:
                pass
            return _error_tool_response(f"script timeout after {timeout_val}s")
        except Exception as e:
            return _error_tool_response(f"script execution failed: {e}")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            # [AutoC 2026-05-31] Why: migrated external tools may intentionally
            # exit non-zero after printing a structured ok=false JSON payload.
            # How: parse stdout first and preserve the tool-owned error fields,
            # adding data.result only when the payload does not already have it.
            # Purpose: keep failures readable without discarding specific messages.
            try:
                parsed = json.loads(stdout_text)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    parsed_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
                    if not isinstance(parsed_data.get("result"), str):
                        parsed = dict(parsed)
                        parsed_data = dict(parsed_data)
                        parsed_data["result"] = f"ERROR: {parsed.get('error', f'script exited with code {proc.returncode}')}"
                        parsed["data"] = parsed_data
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return _error_tool_response(
                f"script exited with code {proc.returncode}",
                stderr=stderr_text[:2000] if stderr_text else "",
                stdout=stdout_text[:2000] if stdout_text else "",
            )

        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output]

        try:
            result = json.loads(stdout_text)
            if isinstance(result, dict):
                return _ensure_tool_response_shape(result)
            # [AutoC 2026-05-31] Why: script tools can print JSON scalars, but the
            # engine now expects ok/data/error. How: keep the scalar under value and
            # expose its string form as data.result. Purpose: make every successful
            # external script output readable through the same contract.
            return {"ok": True, "data": {"result": str(result), "value": result}}
        except json.JSONDecodeError:
            # [AutoC 2026-05-31] Why: legacy scripts may still print plain text.
            # How: wrap stdout as both data.result and data.output. Purpose: keep
            # plain-text scripts usable while conforming to the unified schema.
            return {"ok": True, "data": {"result": stdout_text, "output": stdout_text}}

    return _run


class ToolRegistry:
    def __init__(self, *, workspace_root: Path, tools_dir: Path, load_external: bool = True) -> None:
        self.workspace_root = workspace_root
        self.tools_dir = tools_dir

        self._tool_specs: dict[str, dict[str, Any]] = {}
        self._tool_funcs: dict[str, ToolFunc] = {}
        self._mcp_specs: dict[str, dict[str, Any]] = {}
        self._mcp_funcs: dict[str, ToolFunc] = {}
        self._plugin_conflicts: set[tuple[str, str, str]] = set()

        self._load_builtin_meta_tools()
        self._builtin_specs = dict(self._tool_specs)
        self._builtin_funcs = dict(self._tool_funcs)

        # 只想读内置工具元数据的调用方（管理接口）跳过这一步，省得每个 GET 都去扫两个工具目录。
        if load_external:
            self.reload()

    def register_builtin_tool(self, name: str, description: str, input_schema: dict[str, Any], func: ToolFunc) -> None:
        """Register one builtin tool declared by a built-in plugin."""
        # Why: some built-in tools now live with their owning plugin instead of
        # the central registry table. How: install the spec and callable into the
        # active maps and, when available, into the builtin snapshots used by
        # reload(). Purpose: plugin-owned builtin tools remain visible after tools
        # hot-reload and cannot be overridden by external script tools.
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("builtin tool name is required")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}, "required": []}
        if not callable(func):
            raise TypeError(f"builtin tool function is not callable: {clean_name}")

        spec = {
            "name": clean_name,
            "description": str(description or ""),
            "input_schema": input_schema,
        }
        self._tool_specs[clean_name] = spec
        self._tool_funcs[clean_name] = func

        # Why: plugin tools are registered after ToolRegistry.__init__ has already
        # captured its builtin snapshot. How: refresh the snapshots when they
        # exist. Purpose: later reload() calls keep plugin-declared tools.
        if hasattr(self, "_builtin_specs"):
            self._builtin_specs[clean_name] = spec
        if hasattr(self, "_builtin_funcs"):
            self._builtin_funcs[clean_name] = func

    def _load_builtin_meta_tools(self) -> None:
        builtins: list[tuple[str, str, dict[str, Any], ToolFunc]] = [
            (
                "list_dir",
                "List one or more directories under workspace root. Supports batch listing and recursive mode. Ignores .git by default.",
                {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "description": "Array of directory paths to list (relative to workspace root). MUST be an array even for single directory.",
                            "items": {"type": "string"},
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to list subdirectories recursively. Default false.",
                        },
                        "path": {"type": "string", "description": "(Legacy) Single directory path."},
                    },
                    "required": [],
                },
                _builtins.list_dir,
            ),
            (
                "read_file",
                "Read one or more files under workspace root. Supports text files (with optional line range), images (returned as multimodal), and binary files. Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "Array of file objects. Each has: path (required), startLine (optional, 1-based), endLine (optional, 1-based inclusive). MUST be an array even for single file.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "File path relative to workspace root."},
                                    "startLine": {"type": "integer", "description": "Start line (1-based, inclusive). Only for text files."},
                                    "endLine": {"type": "integer", "description": "End line (1-based, inclusive). Only for text files."},
                                },
                                "required": ["path"],
                            },
                        },
                        "path": {"type": "string", "description": "(Legacy single-file mode) File path relative to workspace root."},
                        "start_line": {"type": "integer", "description": "(Legacy) Start line number."},
                        "end_line": {"type": "integer", "description": "(Legacy) End line number."},
                    },
                    "required": [],
                },
                _builtins.read_file,
            ),
            (
                "write_file",
                "Write a text file under workspace root (policy+approval guarded).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
                _builtins.write_file,
            ),
            (
                "manage_secret",
                "Manage API keys one key at a time. "
                "action=list returns key names and where each is set; values are never returned. "
                "action=set writes or updates a single key, leaving every other line untouched: "
                "API keys go to config/secrets.env, which instances in a share group use together; "
                "instance identity (ports, hosts, platform tokens, paths) goes to this instance's .env. "
                "action=unset removes one key from both files. "
                "Reading or writing .env or config/secrets.env directly is denied, so use this tool instead. "
                "Common keys: EXA_API_KEY, XAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY. "
                "Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "set", "unset"]},
                        "name": {
                            "type": "string",
                            "description": "Key name in UPPER_SNAKE_CASE. Required for set and unset.",
                        },
                        "value": {
                            "type": "string",
                            "description": "Secret value. Required for set. Never echoed back in any result.",
                        },
                    },
                    "required": ["action"],
                },
                _builtins.manage_secret,
            ),
            (
                "apply_diff",
                "Apply sequential search/replace diffs to a file. "
                "Each diff specifies an exact 'search' string and a 'replace' string. "
                "The search must match 100% exactly (including whitespace and indentation). "
                "Multiple diffs are applied in order; each operates on the result of the previous. "
                "If 'start_line' is omitted and search matches multiple locations, the diff is rejected. "
                "Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to workspace root.",
                        },
                        "diffs": {
                            "type": "array",
                            "description": "Array of diff objects to apply sequentially.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "search": {
                                        "type": "string",
                                        "description": "Exact text to find. Must match 100% including whitespace and indentation.",
                                    },
                                    "replace": {
                                        "type": "string",
                                        "description": "Replacement text.",
                                    },
                                    "start_line": {
                                        "type": "integer",
                                        "description": "(Recommended) 1-based line number to start searching from. Required when search matches multiple locations. Note: line numbers refer to content AFTER previous diffs in the array have been applied.",
                                    },
                                },
                                "required": ["search", "replace"],
                            },
                        },
                    },
                    "required": ["path", "diffs"],
                },
                _builtins.apply_diff,
            ),
            (
                "execute_command",
                "Execute a shell command in workspace root (policy+approval guarded; secrets are stripped from env).",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["command"],
                },
                _builtins.execute_command,
            ),
            (
                "search_in_files",
                "Search substring in files under workspace root.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "description": "Directory or file path to search in (relative or absolute). Supports single file. Default '.'"},
                        "mode": {"type": "string", "description": "search (default) or replace", "enum": ["search", "replace"]},
                        "pattern": {"type": "string", "description": "file glob pattern, e.g. '*.py', '**/*.js'. Default '**/*'"},
                        "isRegex": {"type": "boolean", "description": "treat query as regex. Default false"},
                        "maxResults": {"type": "integer", "description": "max matches for search mode. Default 100"},
                        "replace": {"type": "string", "description": "replacement string for replace mode. Supports $1 $2 capture groups"},
                        "maxFiles": {"type": "integer", "description": "max files to modify in replace mode. Default 50"},
                    },
                    "required": ["query"],
                },
                _builtins.search_in_files,
            ),
            (
                "create_or_update_mcp_client",
                "Create or update an MCP client config in config/mcp.yaml. Supports stdio, sse, and streamable_http transports.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "streamable-http", "http"]},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "env": {"type": "object"},
                        "url": {"type": "string"},
                        "headers": {"type": "object"},
                    },
                    "required": ["id", "transport"],
                },
                _builtins.create_or_update_mcp_client,
            ),
            (
                "list_mcp_clients",
                "List configured MCP clients.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_mcp_clients,
            ),
            (
                "delete_mcp_client",
                "Delete an MCP client config by id.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                    "required": ["id"],
                },
                _builtins.delete_mcp_client,
            ),
            (
                "create_or_update_tool",
                "Create/update an external tool under config/tools/. "
                "Provide 'script' (Python code body). "
                "The tool runs as a subprocess: JSON in via stdin, JSON out via stdout.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "input_schema": {"type": "object"},
                        "script": {"type": "string", "description": "Python script body. Has access to: args (dict from stdin), output(result), fail(error)."},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["name", "script"],
                },
                _builtins.create_or_update_tool,
            ),
            (
                "reload_tools",
                "Reload script tools (config/tools/ and the bundled tools/) and MCP tools.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.reload_tools,
            ),
            (
                "request_restart",
                "Request supervisor to restart engine/all (policy+approval guarded; includes git diff summary).",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": ["engine", "all"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["target"],
                },
                _builtins.request_restart,
            ),
            (
                "create_schedule",
                "Create or update a scheduled task in data/schedules.yaml. "
                "Supports type='message' (default, injects text as inbound) and type='script' "
                "(runs a shell command, parses stdout JSON for inbound injection). "
                "Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "unique schedule id"},
                        "cron": {"type": "string", "description": "5-field cron: minute hour day month weekday (UTC)"},
                        "text": {"type": "string", "description": "message text injected as inbound (for message type); prefix text (for script type)"},
                        "type": {"type": "string", "enum": ["message", "script"], "description": "schedule type: message (default) or script"},
                        "command": {"type": "string", "description": "shell command to run (required for script type)"},
                        "timeout": {"type": "integer", "description": "script timeout in seconds (default 30, script type only)"},
                        "silent": {"type": "boolean", "description": "if true, skip inbound when script stdout is empty (default true, script type only)"},
                        "conversation_key": {"type": "string", "description": "conversation key (default: scheduler:{id})"},
                        "entry_node_id": {"type": "string", "description": "optional entry node override for the injected inbound"},
                        "workflow_id": {"type": "string", "description": "optional workflow override"},
                        "enabled": {"type": "boolean"},
                        "once": {"type": "boolean", "description": "if true, auto-delete after first trigger"},
                    },
                    "required": ["id", "cron"],
                },
                _builtins.create_schedule,
            ),
            (
                "list_schedules",
                "List all scheduled tasks from data/schedules.yaml.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_schedules,
            ),
            (
                "delete_schedule",
                "Delete a scheduled task by id. Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "schedule id to delete"},
                    },
                    "required": ["id"],
                },
                _builtins.delete_schedule,
            ),
            (
                "cancel_active_tasks",
                "Cancel all active downstream tasks in the current session. "
                "Use when the user's new message makes the previous task unnecessary.",
                {"type": "object", "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "可选。只取消指定节点的活跃任务（如 scout、smith），不填则取消 session 内全部下游任务。",
                    },
                }, "required": []},
                _builtins.cancel_active_tasks,
            ),
            (
                "get_context_window",
                "Get current context window token usage for this session. "
                "Returns actual LLM-reported tokens (if available), character-based estimates, "
                "compact threshold, and utilization ratio. "
                "Use this to check how much context budget remains before automatic compaction triggers.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.get_context_window,
            ),
        ]

        for name, desc, schema, func in builtins:
            # Why: built-in tools now have a public registration path shared by
            # registry-owned and plugin-owned tools. How: seed the central table
            # through register_builtin_tool(). Purpose: keep one code path for
            # validating and storing builtin specs.
            self.register_builtin_tool(name, desc, schema, func)

        from .builtins import community, context_archive, execution_plans, materials, reminders
        for module in (community, context_archive, execution_plans, materials, reminders):
            module.register_tools(self)

    def reload(self) -> int:
        """Reload script tools from config/tools/ and the bundled tools directory.

        Returns total number of tools available.
        """

        self._tool_specs = dict(self._builtin_specs)
        self._tool_funcs = dict(self._builtin_funcs)
        # 工具在子进程里以脚本跑，本进程不 import 它们；把工作区插进 sys.path 反倒让它的 tools 盖住代码目录那份。
        # 代码目录对多开实例只读，这里只扫不建。

        external_names: set[str] = set()
        for item in external_tool_files(self.workspace_root, self.tools_dir):
            py = item.path
            spec, timeout_sec = _extract_tool_spec(py)
            if spec is None:
                continue

            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            reserved = self._reserved_base_name_reason(name)
            if reserved:
                log.warning("skipping external tool %s: name %r is %s", py.name, name, reserved)
                continue
            if name in external_names:
                log.warning("skipping external tool %s: name %r is already taken by another script", item.rel, name)
                continue
            external_names.add(name)

            description = str(spec.get("description", ""))
            input_schema = spec.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}, "required": []}

            # [AutoC 2026-05-31] Why: external tools may declare SPEC.result_format
            # so result_to_raw() can route by an explicit formatter id before
            # falling back to structural predicates. How: copy the literal field
            # into the registered spec when it is a non-empty string. Purpose: keep
            # tool metadata available without changing any tool return payload.
            registered_spec = {
                "name": name,
                "description": description,
                "input_schema": input_schema,
                "async_mode": bool(spec.get("async_mode", False)),
            }
            result_format = spec.get("result_format")
            if isinstance(result_format, str) and result_format.strip():
                registered_spec["result_format"] = result_format.strip()
            self._tool_specs[name] = registered_spec
            guard = spec.get("guard")
            self._tool_funcs[name] = _make_script_tool(
                script_path=py.resolve(),
                timeout_sec=timeout_sec,
                tool_name=name,
                guard=guard if isinstance(guard, dict) else None,
                python_path=custom_tool_python_path(item, self.tools_dir),
            )

        # MCP 工具由 load_mcp_tools 单独登记；只重扫脚本时得原样留着，不然热重载一次就全没了。
        for mcp_name, mcp_spec in self._mcp_specs.items():
            if mcp_name not in self._tool_specs:
                self._tool_specs[mcp_name] = mcp_spec
                self._tool_funcs[mcp_name] = self._mcp_funcs[mcp_name]

        return len(self._tool_specs)

    def list_specs(self) -> list[dict[str, Any]]:
        with self._tool_view() as (specs, _funcs, _owners):
            return copy.deepcopy(list(specs.values()))

    @contextmanager
    def _tool_view(self, ctx: Any = None):
        from plugin_runtime import get_manager
        from plugin_runtime.manager import current_scope

        manager = get_manager(self.workspace_root)
        scope = current_scope()
        if ctx is not None:
            scope = {**scope, "node_id": getattr(ctx, "origin_node_id", "") or getattr(ctx, "node_id", "") or scope.get("node_id", "")}
        versions = manager.acquire(scope or None)
        specs, funcs = dict(self._tool_specs), dict(self._tool_funcs)
        owners: dict[str, Any] = {}
        current = {(version.api.plugin_id, version.definition["desired_hash"]) for version in versions}
        self._plugin_conflicts = {key for key in self._plugin_conflicts if key[:2] in current}
        try:
            for version in reversed(versions):
                for declaration in getattr(version.api, "tool_definitions", []):
                    spec = declaration["spec"]
                    name = spec["name"]
                    conflict = is_engine_owned_name(name) or (name in specs and not declaration.get("replace"))
                    if conflict:
                        key = (version.api.plugin_id, version.definition["desired_hash"], name)
                        if key not in self._plugin_conflicts:
                            manager.record_error(version, "tool_registration", ValueError(f"Tool {name} already exists or belongs to the engine protocol; explicit replace is required for replaceable tools"))
                            self._plugin_conflicts.add(key)
                        continue
                    specs[name] = spec
                    funcs[name] = declaration["handler"]
                    owners[name] = version
            with manager.host.lease_context(versions) if manager.host is not None else nullcontext():
                yield specs, funcs, owners
        finally:
            manager.release(versions)

    def builtin_specs(self) -> list[dict[str, Any]]:
        """内置工具的 spec 快照。外部脚本工具不在内，reload 也冲不掉。"""
        return list(self._builtin_specs.values())

    def reserved_name_reason(self, name: str) -> str | None:
        """外部工具不能用这个名字时返回原因，能用返回 None。

        内置工具以注册表快照为准，插件在启动时注册的工具也在里面；引擎伪工具不进注册表，另外判断。
        """
        reason = self._reserved_base_name_reason(name)
        if reason:
            return reason
        with self._tool_view() as (_specs, _funcs, owners):
            return "taken by an application plugin" if str(name or "").strip() in owners else None

    def _reserved_base_name_reason(self, name: str) -> str | None:
        clean_name = str(name or "").strip()
        if clean_name in self._builtin_specs:
            return "taken by a builtin tool"
        if is_engine_owned_name(clean_name):
            return "taken by an engine pseudo tool"
        return None

    def get_spec(self, name: str) -> dict[str, Any] | None:
        """按名称获取单个工具的 spec，不存在返回 None。"""
        with self._tool_view() as (specs, _funcs, _owners):
            return copy.deepcopy(specs.get(name))

    async def prepare(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        if name in {"gpt_image_2", "gemini_image", "nai_generate", "nai_generate_from_plan"}:
            from engine.materials.integration import begin_image_generation
            return await begin_image_generation(ctx, name, arguments)
        return None

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        with self._tool_view(ctx) as (specs, funcs, owners):
            if name not in funcs:
                return _error_tool_response(f"tool not found: {name}")
            owner = owners.get(name)
            guard = specs[name].get("guard") if owner else None
            if isinstance(guard, dict):
                denied = await _script_guard_error(guard, name, arguments or {}, ctx)
                if denied is not None:
                    return denied
            try:
                operation = await self.prepare(name=name, arguments=arguments, ctx=ctx)
            except Exception as error:
                return _error_tool_response(f"图像版本登记失败，尚未调用生图服务：{error}")
            func = funcs[name]
            if owner is None:
                result = await func(arguments, ctx)
            else:
                try:
                    if inspect.iscoroutinefunction(func):
                        result = await func(arguments, ctx)
                    else:
                        task = asyncio.create_task(asyncio.to_thread(func, arguments, ctx))
                        try:
                            result = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            pending = await asyncio.gather(task, return_exceptions=True)
                            if pending and inspect.iscoroutine(pending[0]):
                                pending[0].close()
                            raise
                        if inspect.isawaitable(result):
                            result = await result
                except Exception as error:
                    from plugin_runtime import get_manager
                    get_manager(self.workspace_root).record_error(owner, "tool", error)
                    return _error_tool_response(f"Plugin {owner.api.plugin_id} tool {name} failed ({type(error).__name__})")
            if name in {"gpt_image_2", "gemini_image", "nai_generate", "nai_generate_from_plan"}:
                from engine.materials.integration import persist_generated_images
                result = await persist_generated_images(ctx, name, arguments, result, operation=operation)
            return _ensure_tool_response_shape(result)

    async def load_mcp_tools(self) -> int:
        """Scan enabled MCP clients and register their tools as first-class tools.

        整批重登：删掉或停用的客户端，它的工具这一轮就摘掉。
        """
        for mcp_name, mcp_spec in self._mcp_specs.items():
            if self._tool_specs.get(mcp_name) is mcp_spec:
                self._tool_specs.pop(mcp_name, None)
                self._tool_funcs.pop(mcp_name, None)
        self._mcp_specs, self._mcp_funcs = {}, {}
        count = 0
        try:
            clients = mcp_runtime.list_clients(self.workspace_root)
        except Exception:
            return 0

        for client in clients:
            if not isinstance(client, dict):
                continue
            cid = str(client.get("id") or "").strip()
            if not cid or not client.get("enabled", True):
                continue

            try:
                result = await mcp_runtime.list_tools(self.workspace_root, cid)
                # [AutoC 2026-05-31] Why: MCP list_tools may be migrated to the
                # unified data wrapper while older code returns top-level tools.
                # How: read data.tools first, then fall back to result.tools.
                # Purpose: keep dynamic MCP registration working across schemas.
                result_data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
                tools = result_data.get("tools") if isinstance(result_data.get("tools"), list) else (result.get("tools") if isinstance(result, dict) else [])
                if not isinstance(tools, list):
                    continue
            except Exception:
                log.warning("[registry] MCP client '%s' tool list unavailable, skipping", cid)
                continue

            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw_name = str(tool.get("name") or "").strip()
                if not raw_name:
                    continue

                reg_name = f"mcp_{cid}_{raw_name}"
                if reg_name in self._tool_specs:
                    continue

                desc = str(tool.get("description") or "").strip()
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}, "required": []}

                mcp_spec = {
                    "name": reg_name,
                    "description": f"[MCP:{cid}] {desc}" if desc else f"[MCP:{cid}] {raw_name}",
                    "input_schema": schema,
                }
                mcp_func = _make_mcp_tool(self.workspace_root, cid, raw_name)
                self._mcp_specs[reg_name] = mcp_spec
                self._mcp_funcs[reg_name] = mcp_func
                self._tool_specs[reg_name] = mcp_spec
                self._tool_funcs[reg_name] = mcp_func
                count += 1

        return count


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            return await mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
        except Exception as e:
            return _error_tool_response(str(e), mcp_client=client_id, mcp_tool=tool_name)
    return _call
