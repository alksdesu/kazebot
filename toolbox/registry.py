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
from typing import Any, Awaitable, Callable, Iterator, Literal, Mapping

import config_layout
from clonoth_runtime import get_float, load_runtime_config, parse_bool

from . import builtins as _builtins
from . import edit_journal
from ._common import OUTPUT_HEAD_SHARE, ProcessOutcome, run_process
from ._common import request_guard as _request_guard
from ._common import safe_subprocess_env as _safe_subprocess_env
from .context import project_origin_of
from . import mcp_runtime
from .builtins.execute_command import CAPTURE_HEAD_BYTES, CAPTURE_TAIL_BYTES, MAX_OUTPUT_CHARS, MIN_OUTPUT_CHARS
from .builtins.glob_files import DEFAULT_MAX_RESULTS as _GLOB_DEFAULT_RESULTS
from .builtins.glob_files import MAX_RESULTS_LIMIT as _GLOB_MAX_RESULTS
from .hashline import HASHLINE
from .ignore_rules import DEFAULT_IGNORED_DIRS, MAX_WALK_FILES
from .tool_names import is_engine_owned_name


log = logging.getLogger(__name__)

ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]
_BuiltinEntry = (
    tuple[str, str, dict[str, Any], ToolFunc]
    | tuple[str, str, dict[str, Any], ToolFunc, dict[str, Any]]
)
_CORE_SPEC_KEYS = frozenset({"name", "description", "input_schema"})
_MIB = 1024 * 1024

# 生图工具要在调用前登记素材版本、调用后归档产物。
_IMAGE_TOOLS = frozenset({"gpt_image_2", "gemini_image", "nai_generate", "nai_generate_from_plan"})
# data.result 的保险上限：进上下文前 engine 会把超长结果落盘、只给预览，这里只防一份结果撑爆内存和事件日志。
# 截的是解析出来的正文，不是 stdout，JSON 结构和附件不受影响。
_SCRIPT_RESULT_LIMIT_CHARS = 1_000_000
# 脚本 stdout 最多收这么多字节再解析；超了就整份不认，半截 JSON 解析不出来也不该当文本成功交出去。
_SCRIPT_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
_SCRIPT_STDERR_LIMIT_BYTES = 64 * 1024
_DIAGNOSTIC_CHARS = 2000

_IGNORED_DIR_NAMES = ", ".join(sorted(DEFAULT_IGNORED_DIRS))
_HEAD_PERCENT = round(OUTPUT_HEAD_SHARE * 100)


def _describe_exception(error: BaseException) -> str:
    name = type(error).__name__
    text = str(error).strip()
    if not text:
        return name
    return text if name in text else f"{name}: {text}"


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _error_tool_response(message: Any, **fields: Any) -> dict[str, Any]:
    # 附加字段在顶层和 data 里各放一份：判取消等场合有调用方只看顶层。
    text = str(message)
    data: dict[str, Any] = {"result": f"ERROR: {text}"}
    data.update(fields)
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _ensure_tool_response_shape(result: Any) -> dict[str, Any]:
    """把外部脚本和旧内置工具的返回统一成 ok/data/error，原有字段收进 data。"""
    if not isinstance(result, dict):
        return {"ok": True, "data": {"result": str(result), "value": result}}

    data = result.get("data") if isinstance(result.get("data"), dict) else None
    if isinstance(data, dict) and isinstance(data.get("result"), str) and "ok" in result:
        # 已经是新格式但附件只放在顶层的工具：补一份 data.attachments。
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
        response = {"ok": False, "data": merged_data, "error": str(error_value or merged_data.get("result") or "unknown")}
        # 和 _error_tool_response 一样顶层留一份取消标记，只看顶层的调用方才认得出被取消。
        if merged_data.get("cancelled"):
            response["cancelled"] = True
        return response

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
    env = _safe_subprocess_env(project_origin=project_origin_of(ctx))
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


def _truncated_note(omitted: int) -> str:
    return f"\n...<truncated {omitted} chars>"


def _clip_text(text: str, limit: int) -> str:
    """超过 limit 字符时只留开头，连同末尾的截断说明一共不超过 limit；limit 连说明都放不下时只截不说明。"""
    if len(text) <= limit:
        return text
    # 省略的字数不会多于全文，说明按全文长度的位数占位
    keep = limit - len(_truncated_note(len(text)))
    if keep < 0:
        return text[:max(0, limit)]
    return text[:keep] + _truncated_note(len(text) - keep)


def _clip_result(response: dict[str, Any], limit: int = _SCRIPT_RESULT_LIMIT_CHARS) -> dict[str, Any]:
    """只截 data.result 这段给模型看的正文，其余字段（附件、引用）原样保留；data.output 是同一份正文时一起截。"""
    data = response.get("data")
    text = data.get("result") if isinstance(data, dict) else None
    if not isinstance(text, str) or len(text) <= limit:
        return response
    shortened = _clip_text(text, limit)
    clipped_data = {**data, "result": shortened}
    # 纯文本脚本把同一份正文再放进 output 给旧读法用，只截 result 的话工具事件里照样带着整份
    if data.get("output") == text:
        clipped_data["output"] = shortened
    clipped = dict(response)
    clipped["data"] = clipped_data
    return clipped


def _parse_script_stdout(text: str) -> tuple[Any, bool]:
    """(解析结果, 是否解析成功)。"""
    try:
        return json.loads(text), True
    except ValueError:
        pass
    # 依赖库往 stdout 打的日志会排在前面，协议要求的那个 JSON 对象在最后一行。
    head, _sep, last = text.rpartition("\n")
    last = last.strip()
    if head and last.startswith("{"):
        try:
            parsed = json.loads(last)
        except ValueError:
            return None, False
        if isinstance(parsed, dict):
            return parsed, True
    return None, False


def _with_error_result(payload: dict[str, Any], fallback: str) -> dict[str, Any]:
    """工具自己报的 ok=false 原样保留，只在缺 data.result 时补上一句。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if isinstance(data.get("result"), str):
        return payload
    filled = dict(payload)
    filled["data"] = {**data, "result": f"ERROR: {payload.get('error') or fallback}"}
    return filled


def _script_response(outcome: ProcessOutcome) -> dict[str, Any]:
    stdout_text = outcome.stdout.decode("utf-8", errors="replace").strip()
    stderr_text = outcome.stderr.decode("utf-8", errors="replace").strip()
    diagnostics = {"stderr": stderr_text[:_DIAGNOSTIC_CHARS], "stdout": stdout_text[:_DIAGNOSTIC_CHARS]}
    rc = outcome.returncode

    if outcome.stdout_overflow:
        return _error_tool_response(
            f"script output exceeded {_SCRIPT_OUTPUT_LIMIT_BYTES} bytes and was discarded", **diagnostics,
        )

    parsed, parsed_ok = _parse_script_stdout(stdout_text)
    if rc != 0:
        # 迁移过的脚本会先打印结构化的 ok=false 再以非零码退出，具体报错要原样留着。
        if parsed_ok and isinstance(parsed, dict) and parsed.get("ok") is False:
            return _clip_result(_with_error_result(parsed, f"script exited with code {rc}"))
        return _error_tool_response(f"script exited with code {rc}", **diagnostics)

    if parsed_ok:
        if isinstance(parsed, dict):
            return _clip_result(_ensure_tool_response_shape(parsed))
        # 打印 JSON 标量的旧脚本：值放进 value，字符串形式给 data.result。
        return _clip_result({"ok": True, "data": {"result": str(parsed), "value": parsed}})
    if stdout_text[:1] in ("{", "["):
        # 看着是 JSON 却解析不了，多半是写到一半崩了，不能当成功的纯文本交出去。
        return _error_tool_response("script printed malformed JSON", **diagnostics)
    # 只打印纯文本的旧脚本照旧可用。
    return _clip_result({"ok": True, "data": {"result": stdout_text, "output": stdout_text}})


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
        input_json = json.dumps(args or {}, ensure_ascii=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root) if isinstance(root, Path) else None,
                env=_with_python_path(_script_tool_env(ctx), python_path),
                # 独立进程组：超时或取消时整棵进程树一起杀，孙进程攥着管道也不会把等待卡死。
                start_new_session=True,
            )
            outcome = await run_process(
                proc,
                stdin=input_json.encode("utf-8"),
                timeout_sec=timeout_val,
                keep_stdout=_SCRIPT_OUTPUT_LIMIT_BYTES,
                keep_stderr=_SCRIPT_STDERR_LIMIT_BYTES,
                is_cancelled=getattr(ctx, "check_cancelled", None),
            )
        except Exception as e:
            return _error_tool_response(f"script execution failed: {e}")

        if outcome.cancelled:
            return _error_tool_response("script task cancelled", cancelled=True)
        if outcome.timed_out:
            return _error_tool_response(f"script timeout after {timeout_val}s")
        return _script_response(outcome)

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

    def register_builtin_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        func: ToolFunc,
        extra_spec: Mapping[str, Any] | None = None,
    ) -> None:
        """Register one builtin tool declared by a built-in plugin.

        extra_spec adds fields such as requires_edit_mode to the spec; it may not override name, description or input_schema.
        """
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("builtin tool name is required")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}, "required": []}
        if not callable(func):
            raise TypeError(f"builtin tool function is not callable: {clean_name}")
        extra = dict(extra_spec or {})
        overridden = sorted(_CORE_SPEC_KEYS & extra.keys())
        if overridden:
            raise ValueError(f"extra spec fields of {clean_name} may not override {', '.join(overridden)}")

        spec = {
            "name": clean_name,
            "description": str(description or ""),
            "input_schema": input_schema,
            **extra,
        }
        self._tool_specs[clean_name] = spec
        self._tool_funcs[clean_name] = func

        # 插件在 __init__ 拍完内置快照之后才注册，快照不补的话 reload() 一次就丢，还会被同名脚本工具盖掉。
        if hasattr(self, "_builtin_specs"):
            self._builtin_specs[clean_name] = spec
        if hasattr(self, "_builtin_funcs"):
            self._builtin_funcs[clean_name] = func

    def _load_builtin_meta_tools(self) -> None:
        builtins: list[_BuiltinEntry] = [
            (
                "list_dir",
                "List one or more directories under workspace root (batch listing via paths, optional recursive mode). "
                "Directories come first, then files, each sorted by name. "
                f"Dependency, cache and build directories ({_IGNORED_DIR_NAMES}) are listed by name and marked as "
                "omitted, without their contents, and recursive mode does not enter them; pass include_ignored=true to "
                "list them like any other directory, or list such a directory directly. .gitignore is not applied here. "
                "To find files by name or path pattern use glob; to search file contents use search_in_files. "
                "Policy guarded like read_file.",
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
                        "include_ignored": {
                            "type": "boolean",
                            "description": "Also list the contents of the dependency, cache and build directories that are "
                            "otherwise shown by name only. Default false.",
                        },
                        "path": {"type": "string", "description": "(Legacy) Single directory path."},
                    },
                    "required": [],
                },
                _builtins.list_dir,
            ),
            (
                "read_file",
                "Read one or more files under workspace root. "
                "Text comes back with line numbers ('  12 | code'); each file's header gives its total line count and the range shown. "
                "In nodes with edit_mode: hashline each line comes back as 'LINE:HASH|code' instead (e.g. '12:a3f|code'): "
                "'LINE:HASH' is the anchor edit_lines takes, and the lines apply_diff shows use the same form. "
                "Without a line range at most 500 lines are shown; a line longer than 2000 characters is cut and marked; "
                "one call returns about 50KB of text at most and files past that are listed as not read. "
                "When a file is not shown in full, its header tells the startLine to continue from. "
                "Images come back as multimodal attachments, binary files as their size. "
                "Reading a text file remembers its version for this conversation: write_file, apply_diff and edit_lines refuse "
                "to change it, and search_in_files' replace mode skips it, if it was modified after the read. "
                "Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "Array of file objects. Each has: path (required), startLine (optional, 1-based), endLine (optional, 1-based inclusive). MUST be an array even for single file. Give a line range for large files.",
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
                "Create a text file, or replace an existing one with complete new content, under workspace root. "
                "Prefer apply_diff for changing part of an existing file. "
                "An existing file keeps its line endings (LF/CRLF), UTF-8 BOM and permissions, and is replaced atomically. "
                "If this task read the file and it has changed since, or another task of this conversation changed it after "
                "this task started, the write is refused: read it again first. "
                "While another task that has not ended is editing the same file (any conversation), the write is refused and "
                "the result names that task and its node. "
                "Overwriting a file you have not read is allowed and noted in the result. "
                "After writing, .py/.json/.yaml/.yml/.toml files are syntax-checked and the commands configured under edit_checks run; "
                "problems are reported at the end of the result and the write is not rolled back. Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to workspace root."},
                        "content": {
                            "type": "string",
                            "description": "The complete file content. Pass an empty string to write an empty file.",
                        },
                    },
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
                "Edit a file with search/replace diffs, applied in order; each diff operates on the result of the previous ones. "
                "All diffs are matched in memory first: if any of them cannot be applied, nothing is written and the result lists "
                "every failing diff with its reason and the candidate line numbers; fix those and send all diffs again. "
                "Matching levels, tried in order, the first that finds anything decides: exact; same text with different overall "
                "indentation (replace is re-indented to the target); each line trimmed of leading/trailing whitespace; runs of "
                "whitespace collapsed; \\n, \\t, \\\", \\\\ escapes in search and replace taken as the real characters; leading/trailing "
                "blank lines of search ignored; anchor block (first and last lines equal after trimming, middle lines similar). "
                "A level counts only when it matches one place; with several matches start_line picks the first one at or after "
                "that line, otherwise the diff fails with the candidate lines. A fuzzy match spanning far more text than search is rejected. "
                "The result reports the level used for each diff and the edited line ranges. It also echoes the edited regions "
                "with line numbers and 2 lines of context, and a failure shows the closest matching snippet, when this "
                "conversation has read the current version of the file or the read policy lets it be read without approval; "
                "otherwise only line numbers are given and no read approval is asked for, so use read_file to see the text. "
                "Refused if the file changed after you last read it (or another task of this conversation changed it after "
                "this task started), and while another task that has not ended is editing it; that error names the task and "
                "its node. Line endings, UTF-8 BOM and permissions are preserved and the "
                "write is atomic. After writing, the same syntax check and edit_checks commands as write_file run. "
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
                                        "description": "Text to find, copied from the file without the line-number prefix. Exact text works best; whitespace and indentation differences are tolerated.",
                                    },
                                    "replace": {
                                        "type": "string",
                                        "description": "Replacement text. When the match was found at a different indentation, it is re-indented to the target.",
                                    },
                                    "start_line": {
                                        "type": "integer",
                                        "description": "1-based line number in the file as you read it, before any diff of this call (line shifts from earlier diffs are compensated). Only used when search matches more than one place: the first match at or after this line is taken. Ignored when search matches exactly one place.",
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
                "edit_lines",
                "Edit a file by line anchors. In hashline mode read_file shows each line as 'LINE:HASH|content', "
                "e.g. '12:a3f|    return x'; the 'LINE:HASH' part ('12:a3f') is the anchor. "
                "Each edit is {op, start, end, content}: replace puts content in place of lines start..end (end defaults to start); "
                "delete removes lines start..end; insert_after / insert_before put content after / before line start (no end). "
                "All anchors refer to the file as you read it, before any edit of this call. "
                "Every anchor's hash must match that line now: if any does not, nothing is written and the result names the "
                "anchors that failed, with the current lines around them and fresh anchors when the text may be shown (see "
                "below); an anchor is never moved to another line. "
                "Edits may not overlap: replaced or deleted ranges may not share lines, two inserts may not go to the same place, "
                "and an insert may not fall inside a replaced or deleted range. They are applied bottom-up and written atomically. "
                "content is the new lines without anchors; a trailing newline is optional. "
                "Refused if the file changed after you last read it (or another task of this conversation changed it after "
                "this task started), and while another task that has not ended is editing it; that error names the task and "
                "its node. Line endings, UTF-8 BOM and permissions are preserved; "
                "after writing, the same syntax check and edit_checks commands as write_file run, and the change can be undone "
                "with undo_edits. The result echoes the edited regions with fresh anchors when this conversation has read the "
                "current version of the file or the read policy lets it be read without approval; otherwise it gives line "
                "numbers only and asks for no read approval, so read the file again for new anchors. Lines below an edit shift, "
                "so take the next anchors from that echo or a fresh read. Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to workspace root."},
                        "edits": {
                            "type": "array",
                            "description": "Edits applied together; every anchor refers to the file before any of them.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string", "enum": ["replace", "insert_after", "insert_before", "delete"]},
                                    "start": {
                                        "type": "string",
                                        "description": "Anchor 'LINE:HASH' of the first line, copied from read_file, e.g. '12:a3f'.",
                                    },
                                    "end": {
                                        "type": "string",
                                        "description": "Anchor of the last line, for replace and delete only; defaults to start.",
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": "New lines for replace / insert_after / insert_before, without anchors. Omit for delete.",
                                    },
                                },
                                "required": ["op", "start"],
                            },
                        },
                    },
                    "required": ["path", "edits"],
                },
                _builtins.edit_lines,
                {"requires_edit_mode": HASHLINE},
            ),
            (
                "undo_edits",
                "Undo file changes made by write_file, apply_diff, edit_lines and search_in_files' replace mode, one task at a time. "
                "Before a task first changes a file its original content is saved; undoing puts it back "
                "(a file the task created is deleted, together with directories created for it that are now empty). "
                "Changes made by sub-agents this conversation dispatched are recorded here too, so they can be undone from here. "
                "action=list shows this conversation's recent tasks (sub-agents included), the files each changed and whether "
                "they were undone. "
                "action=undo (default) undoes one task: the one named by task_id (a unique prefix of at least 8 characters works). "
                "Without task_id it undoes the current task only, and only when no other task of this conversation has changes "
                "left to undo; otherwise nothing is undone and the result lists the candidate tasks, so pass task_id. "
                "paths limits it to some of that task's files. A file changed again after that task, by anything, is skipped "
                "with the reason unless force=true; a file another task that has not ended is editing is skipped and that task "
                "is named. Each file goes through the same approval as write_file. "
                "A change can be undone only once. "
                f"Files over {edit_journal.MAX_FILE_BYTES // _MIB}MB and changes past a task's "
                f"{edit_journal.MAX_TASK_BYTES // _MIB}MB journal limit are not saved and cannot be undone; "
                "the edit's own result says so. Records are kept for edit_journal.retention_days days "
                f"(default {edit_journal.DEFAULT_RETENTION_DAYS}). Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["undo", "list"], "description": "undo (default) or list."},
                        "task_id": {
                            "type": "string",
                            "description": "Task whose changes to undo, as action=list shows it. Default: the current task, "
                            "refused while another task of this conversation also has changes left to undo.",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Only undo these files of that task, written as you passed them to the edit tools.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Also restore files changed again after that task, discarding the later changes. Default false.",
                        },
                    },
                    "required": [],
                },
                _builtins.undo_edits,
            ),
            (
                "execute_command",
                "Execute a shell command in workspace root (policy+approval guarded; secrets are stripped from env). "
                "The result is 'returncode=N', then stdout, then stderr; a non-zero exit code is still a successful call. "
                f"Each stream keeps its first {CAPTURE_HEAD_BYTES // 1024}KB and last {CAPTURE_TAIL_BYTES // 1024}KB and drops "
                "the middle, with a note saying how many bytes and lines were dropped there. "
                "Without max_output_chars everything kept is returned and the engine stores a long result, showing its head "
                "and tail with a ref that read_tool_output pages through or searches (an instance may set a default budget in "
                "meta.execute_command.max_output_chars). With max_output_chars the output is cut to about that many "
                f"characters: about {_HEAD_PERCENT}% from the start and {100 - _HEAD_PERCENT}% from the end, where errors and "
                "summaries usually are, with a note in between. timeout_sec (default meta.execute_command.default_timeout_sec, "
                "90; kept within meta.execute_command.min_timeout_sec and max_timeout_sec, 1-3600) is a hard limit: the "
                "command and everything it started are killed, and the output received until then comes back with the "
                "error. When timeout_sec is longer than about a minute, the engine stops waiting after about 60 seconds "
                "(meta.execute_command.async_upgrade): the command keeps running in the background, also while this task "
                "waits for a sub-agent, and its result arrives later as a new message, to this node or, if this node "
                "cannot receive messages, to the node that dispatched it. For servers, watchers and other processes that "
                "should keep running, use start_process instead.",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command line, run by the system shell in workspace root."},
                        "timeout_sec": {
                            "type": "number",
                            "description": "Hard time limit in seconds; the command is killed when it is reached. "
                            "Default 90; values outside 1-3600 (configurable) are moved to the nearest bound, 0 or "
                            "less means the default.",
                        },
                        "max_output_chars": {
                            "type": "integer",
                            "description": f"Cut the returned output to about this many characters ({MIN_OUTPUT_CHARS}-"
                            f"{MAX_OUTPUT_CHARS}), keeping about {_HEAD_PERCENT}% from the start and {100 - _HEAD_PERCENT}% "
                            "from the end. Omit it to get everything kept; long results are stored by the engine and previewed.",
                        },
                    },
                    "required": ["command"],
                },
                _builtins.execute_command,
            ),
            (
                "search_in_files",
                "Search file contents under workspace root, or replace text in them. path is a directory or a single file "
                "(default '.'). Uses ripgrep when it is installed and a Python scan otherwise; both give the same results. "
                "Search is case-insensitive; replace is case-sensitive. query is literal text unless isRegex=true, which "
                "takes Python regular expression syntax. "
                "pattern is a file glob matched against the workspace-relative path (the absolute path outside the "
                "workspace), not against a path relative to path: without '/' it matches the file name at any depth "
                "('*.py'); with '/' it is matched from the workspace root ('engine/**/*.py'). "
                "Skipped: files ignored by .gitignore inside a git work tree (includeIgnored=true searches them; list_dir "
                "spells it include_ignored and both spellings work here); dependency, cache, build and runtime data "
                "directories such as node_modules, .venv, dist, build, conversations and logs; binary files; compiled, "
                "database, .log, .jsonl and .min.js files; files over meta.search.max_file_size_bytes (3MB by default). "
                "A file named in path is searched even when the rules above would skip it, unless it is binary or over "
                "the size limit. "
                "Search mode returns '<N> results found:' then 'file:line | match' with one line of context on each side, "
                "at most maxResults matches (default 100) and about 50000 characters; a long scan stops early with a note. "
                "Replace mode (mode='replace') needs replace, and an empty string deletes the matches. With isRegex=true, "
                "$1, ${name}, $& and $0 refer to groups and $$ is a literal $; Python's \\1 and \\g<name> work too. With "
                "isRegex=false, replace is inserted verbatim. It changes up to maxFiles files (default 50) after one "
                "write_file approval, skips a file that changed after you read it or that another task that has not ended "
                "is editing (the skip names that task), keeps line endings, UTF-8 BOM and "
                "permissions, writes atomically, runs the same syntax check and edit_checks commands as write_file, and the "
                "changes can be undone with undo_edits. Files under data/edit_journal/ are never changed. In nodes with "
                "search_replace: false, replace mode is refused before any approval.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to find, or a regular expression when isRegex=true."},
                        "path": {"type": "string", "description": "Directory or file path to search in (relative or absolute). Supports single file. Default '.'"},
                        "mode": {"type": "string", "description": "search (default) or replace", "enum": ["search", "replace"]},
                        "pattern": {
                            "type": "string",
                            "description": "File glob matched against the workspace-relative path, not relative to path: "
                            "'*.py' (file name, any depth) or 'engine/**/*.py'. Default '**/*'",
                        },
                        "isRegex": {"type": "boolean", "description": "Treat query as a Python regular expression. Default false."},
                        "includeIgnored": {
                            "type": "boolean",
                            "description": "Also search files ignored by .gitignore; include_ignored is accepted too. Default false.",
                        },
                        "maxResults": {"type": "integer", "description": "Max matches in search mode. Default 100."},
                        "replace": {
                            "type": "string",
                            "description": "Replacement for replace mode, required there; an empty string deletes the matches. "
                            "Regex mode: $1, ${name}, $&, $0 refer to groups, $$ is a literal $. Literal mode: inserted verbatim.",
                        },
                        "maxFiles": {"type": "integer", "description": "Max files to change in replace mode. Default 50."},
                    },
                    "required": ["query"],
                },
                _builtins.search_in_files,
            ),
            (
                "glob",
                "Find files by name or path pattern under a directory, newest first (by modification time). "
                "Use glob to locate files by name, search_in_files to search inside files, and list_dir to see what a "
                "directory holds. pattern is relative to path: without '/' it matches file names at any depth ('*.py'); "
                "with '/' it matches the whole path from path ('src/**/*.ts'). '*', '?' and '[...]' stay within one "
                "directory level, '**' spans any number of levels, '{a,b}' matches either, and a trailing '/' means every "
                "file under that directory. Files ignored by .gitignore inside a git work tree and dependency, cache and "
                "build directories (node_modules, .venv, dist, build, ...) are skipped and symbolic links are not followed; "
                "the directory named in path is searched even when it is one of those, so point path into it to look inside. "
                f"Returns at most max_results paths (default {_GLOB_DEFAULT_RESULTS}, at most {_GLOB_MAX_RESULTS}) and stops "
                f"after walking {MAX_WALK_FILES} files. Policy guarded like read_file and list_dir.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob such as '**/*.py', '*.md' or 'src/**/test_*.py', relative to path.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search (relative to workspace root, or absolute). Default '.'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": f"Max paths to return, newest first. Default {_GLOB_DEFAULT_RESULTS}, at most "
                            f"{_GLOB_MAX_RESULTS}.",
                        },
                    },
                    "required": ["pattern"],
                },
                _builtins.glob_files,
            ),
            (
                "create_or_update_mcp_client",
                "Create or update an MCP client config in config/mcp.yaml. Supports stdio, sse, and streamable_http transports. "
                "The client's entry is replaced as a whole: omitted fields go back to their defaults, except share_sessions, "
                "which keeps its current value. Saving closes the client's open sessions in every conversation (a call in "
                "progress finishes first), so the next call starts a new session; call reload_tools to pick up a changed tool list.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "share_sessions": {
                            "type": "boolean",
                            "description": "true: every conversation uses the same sessions of this client. "
                            "Default false: each conversation gets its own sessions, so a stateful server "
                            "(browser, memory) never carries one conversation's state into another. "
                            "Set true only for stateless servers that are slow to start. "
                            "Omit to keep the current setting.",
                        },
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
                        "cron": {
                            "type": "string",
                            "description": (
                                "5-field cron: minute hour day month weekday (UTC); a field is * or */n on its own, "
                                "or comma-separated numbers and a-b ranges"
                            ),
                        },
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

        for name, desc, schema, func, *extra in builtins:
            self.register_builtin_tool(name, desc, schema, func, extra[0] if extra else None)

        from .builtins import community, context_archive, execution_plans, materials, processes, reminders, worktrees
        for module in (community, context_archive, execution_plans, materials, processes, reminders, worktrees):
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

            # result_format 原样带进注册表：result_to_raw() 先按它选格式化器，没有才按结构猜。
            registered_spec = {
                "name": name,
                "description": description,
                "input_schema": input_schema,
                "async_mode": bool(parse_bool(spec.get("async_mode"), False)),
            }
            result_format = spec.get("result_format")
            if isinstance(result_format, str) and result_format.strip():
                registered_spec["result_format"] = result_format.strip()
            # engine 按这两个键决定同轮能不能并发、连续几轮同参数调用算不算空转
            for flag in ("parallel_safe", "repeatable"):
                if parse_bool(spec.get(flag), False):
                    registered_spec[flag] = True
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
        if name in _IMAGE_TOOLS:
            from engine.materials.integration import begin_image_generation
            return await begin_image_generation(ctx, name, arguments)
        return None

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # 不管内置、脚本、MCP 还是插件工具，连同插件视图的借还，出错都只算这一次调用失败，不能把整个任务带崩；取消照常上抛。
        try:
            with self._tool_view(ctx) as (specs, funcs, owners):
                if name not in funcs:
                    return _error_tool_response(f"tool not found: {name}")
                return await self._execute_resolved(name, specs[name], funcs[name], owners.get(name), arguments, ctx)
        except Exception as error:
            log.exception("tool %s failed", name)
            return _error_tool_response(f"tool {name} failed: {_describe_exception(error)}")

    async def _execute_resolved(
        self, name: str, spec: dict[str, Any], func: ToolFunc, owner: Any, arguments: dict[str, Any], ctx: Any,
    ) -> dict[str, Any]:
        guard = spec.get("guard") if owner else None
        if isinstance(guard, dict):
            denied = await _script_guard_error(guard, name, arguments or {}, ctx)
            if denied is not None:
                return denied
        try:
            operation = await self.prepare(name=name, arguments=arguments, ctx=ctx)
        except Exception as error:
            return _error_tool_response(f"图像版本登记失败，尚未调用生图服务：{error}")
        if owner is None:
            result = await func(arguments, ctx)
        else:
            try:
                result = await _call_plugin_tool(func, arguments, ctx)
            except Exception as error:
                from plugin_runtime import get_manager
                get_manager(self.workspace_root).record_error(owner, "tool", error)
                return _error_tool_response(f"Plugin {owner.api.plugin_id} tool {name} failed ({type(error).__name__})")
        if name in _IMAGE_TOOLS:
            from engine.materials.integration import persist_generated_images
            result = await persist_generated_images(ctx, name, arguments, result, operation=operation)
        return _ensure_tool_response_shape(result)

    async def load_mcp_tools(self) -> int:
        """Scan enabled MCP clients and register their tools as first-class tools.

        整批重登：删掉或停用的客户端，它的工具这一轮就摘掉。
        已有的 MCP 会话先全部退役（正在用的等调用结束再关），工具表从新会话里取：
        配置没变、服务端程序改了的 stdio 服务也得换新进程才看得到。
        """
        for mcp_name, mcp_spec in self._mcp_specs.items():
            if self._tool_specs.get(mcp_name) is mcp_spec:
                self._tool_specs.pop(mcp_name, None)
                self._tool_funcs.pop(mcp_name, None)
        self._mcp_specs, self._mcp_funcs = {}, {}
        count = 0
        try:
            await mcp_runtime.retire_sessions(self.workspace_root)
        except Exception:
            log.warning("[registry] retiring MCP sessions before the rescan failed", exc_info=True)
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
                annotations = tool.get("annotations")
                # 只读提示是服务端自报的；engine 按 parallel_safe 让同一轮里的这类调用并发
                if isinstance(annotations, dict) and parse_bool(annotations.get("readOnlyHint"), False):
                    mcp_spec["parallel_safe"] = True
                mcp_func = _make_mcp_tool(self.workspace_root, cid, raw_name)
                self._mcp_specs[reg_name] = mcp_spec
                self._mcp_funcs[reg_name] = mcp_func
                self._tool_specs[reg_name] = mcp_spec
                self._tool_funcs[reg_name] = mcp_func
                count += 1

        return count


async def _call_plugin_tool(func: Any, arguments: dict[str, Any], ctx: Any) -> Any:
    """插件工具可以是协程函数，也可以是同步函数；同步的放到线程里跑，不堵事件循环。"""
    if inspect.iscoroutinefunction(func):
        return await func(arguments, ctx)
    task = asyncio.create_task(asyncio.to_thread(func, arguments, ctx))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        # 线程停不下来，得等它跑完；它若返回了协程，关掉免得「从未 await」的告警。
        pending = await asyncio.gather(task, return_exceptions=True)
        if pending and inspect.iscoroutine(pending[0]):
            pending[0].close()
        raise
    if inspect.isawaitable(result):
        result = await result
    return result


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            response = await mcp_runtime.call_tool(
                workspace_root, client_id, tool_name, args, scope=mcp_runtime.session_scope(ctx),
            )
        except Exception as e:
            # 会话断开一类的异常常常没有消息文本，只给 str(e) 模型会拿到一句空的 ERROR
            return _error_tool_response(_describe_exception(e), mcp_client=client_id, mcp_tool=tool_name)
        return _clip_mcp_response(response)
    return _call


def _clip_mcp_response(response: dict[str, Any]) -> dict[str, Any]:
    """和脚本工具一样经 _clip_result 截 data.result。

    同一份正文在 error（isError 时）和 data.mcp_result 的文本块里还各有一份，工具结束事件会把整份结果带上，所以一起截。
    """
    clipped = _clip_result(response)
    before = response.get("data") if isinstance(response.get("data"), dict) else {}
    after = clipped.get("data") if isinstance(clipped.get("data"), dict) else {}
    original = before.get("result")
    if clipped is response or not isinstance(original, str) or after.get("result") == original:
        return clipped
    if clipped.get("error") == original:
        clipped["error"] = after["result"]
    mcp_result = after.get("mcp_result")
    if isinstance(mcp_result, dict) and isinstance(mcp_result.get("content"), list):
        after["mcp_result"] = {**mcp_result, "content": [_clip_mcp_text_part(part) for part in mcp_result["content"]]}
    return clipped


def _clip_mcp_text_part(part: Any) -> Any:
    text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(text, str):
        return part
    shortened = _clip_result({"data": {"result": text}})["data"]["result"]
    return part if shortened == text else {**part, "text": shortened}
