"""read_tool_output：按行分页读取或搜索本会话落盘的超长工具输出。

只读当前会话名下的 ref，读的是本会话自己产生的输出，所以不走 policy。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clonoth_runtime import load_runtime_config
from engine.tool_outputs import (
    DEFAULT_READ_LINES,
    MAX_PATTERN_CHARS,
    MAX_READ_LINES,
    READ_TOOL_OUTPUT,
    SEARCH_CONTEXT_LINES,
    ReadPage,
    ToolOutputNotFound,
    ToolOutputStore,
    is_valid_ref,
    read_lines,
    search_lines,
    tool_output_limits,
)

PLUGIN_META = {
    "handler_class": "ToolOutputReader",
    "hook_points": [],
    "tools": [],
}


class ToolOutputReader:
    name = "tool_output_reader"


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}


def _session(ctx: Any) -> str:
    return str(getattr(ctx, "_context_archive_session_id", "") or getattr(ctx, "session_id", "") or "").strip()


def _int_argument(args: dict[str, Any], key: str, default: int) -> int:
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数，收到 {raw!r}") from None


def _read(
    workspace_root: Path, session_id: str, ref: str, *, start_line: int, max_lines: int, pattern: str | None,
) -> ReadPage:
    limits = tool_output_limits(load_runtime_config(workspace_root))
    text = ToolOutputStore(workspace_root, retention_days=limits.retention_days).load(session_id, ref)
    if pattern:
        return search_lines(text, ref, pattern=pattern, start_line=start_line, char_budget=limits.view_chars)
    return read_lines(text, ref, start_line=start_line, max_lines=max_lines, char_budget=limits.view_chars)


async def read_tool_output(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ref = str(args.get("ref") or "").strip()
    if not is_valid_ref(ref):
        return _error(f"ref 格式不对：{ref!r}，应当照抄预览里给出的 ref（to_ 加 24 位十六进制）")
    session_id = _session(ctx)
    workspace_root = getattr(ctx, "workspace_root", None)
    if not session_id or workspace_root is None:
        return _error("拿不到当前会话，读不了工具输出")
    raw_pattern = args.get("pattern")
    pattern = raw_pattern if isinstance(raw_pattern, str) and raw_pattern != "" else None
    try:
        start_line = max(1, _int_argument(args, "start_line", 1))
        max_lines = max(1, min(MAX_READ_LINES, _int_argument(args, "max_lines", DEFAULT_READ_LINES)))
        page = await asyncio.to_thread(
            _read, Path(workspace_root), session_id, ref,
            start_line=start_line, max_lines=max_lines, pattern=pattern,
        )
    except ToolOutputNotFound:
        return _error(f"当前会话里没有 ref={ref} 的输出：它不属于本会话，或者已过保留期被清理")
    except ValueError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"读取 ref={ref} 失败：{exc}")
    return {
        "ok": True,
        "data": {
            "result": page.text,
            "ref": ref,
            "total_lines": page.total_lines,
            "start_line": page.start_line,
            "end_line": page.end_line,
            "next_start_line": page.next_start_line,
        },
    }


PLUGIN_META["tools"] = [
    {
        "name": READ_TOOL_OUTPUT,
        "description": (
            "读取本会话里被截短的超长工具输出。工具结果太长时上下文里只放开头和结尾，"
            "完整内容存成一个 ref（预览里写着 ref=to_...），用本工具按行分页读，或用 pattern 搜索。"
            "只能读当前会话产生的 ref；输出过了保留期会被清理。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "照抄预览里给出的 ref，例如 to_0123456789abcdef01234567。",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "从第几行开始（从 1 数），默认 1。搜索时从这一行往后找。",
                },
                "max_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LINES,
                    "description": (
                        f"最多返回多少行，默认 {DEFAULT_READ_LINES}，上限 {MAX_READ_LINES}。"
                        "单次返回的字数另有上限，没读完时结果第一行会给出下一页的 start_line。"
                    ),
                },
                "pattern": {
                    "type": "string",
                    "maxLength": MAX_PATTERN_CHARS,
                    "description": (
                        "可选。给了就改成搜索：按正则或普通子串匹配，一行匹配任意一种就算命中，"
                        f"返回带行号的命中行（行号后是冒号）和前后各 {SEARCH_CONTEXT_LINES} 行上下文（行号后是短横）。"
                        "区分大小写，正则里可用 (?i) 忽略大小写；命中太多时分批返回。"
                    ),
                },
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
        "func": read_tool_output,
    },
]
