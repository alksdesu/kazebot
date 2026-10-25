"""execute_command — shell command execution (policy + approval guarded)."""
from __future__ import annotations

import asyncio
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from ..context import ToolContext
from .._common import request_guard, run_process, safe_subprocess_env

# UTF-8 一个字符最多 4 字节：每路输出留够截到 max_output_chars 的量，多出来的边读边扔。
_BYTES_PER_CHAR = 4


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    # 报错同时放进 error 和 data.result；cancelled 这类标记在顶层和 data 里各放一份，新旧读法都认得。
    text = str(message)
    payload: dict[str, Any] = {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}
    if extra:
        payload.update(extra)
        payload["data"].update(extra)
    return payload


async def execute_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    default_timeout_sec = get_float(
        runtime_cfg,
        "meta.execute_command.default_timeout_sec",
        90.0,
        min_value=1.0,
        max_value=3600.0,
    )
    max_output_chars = get_int(
        runtime_cfg,
        "meta.execute_command.max_output_chars",
        12000,
        min_value=1000,
        max_value=200_000,
    )

    timeout_raw = args.get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)

    _op, err = await request_guard(ctx, "execute_command", {"command": command})
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    if await ctx.check_cancelled():
        return _error_response("task cancelled", cancelled=True)

    keep = max_output_chars * _BYTES_PER_CHAR
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.workspace_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
            # 独立进程组：超时或取消时整棵进程树一起杀，孙进程攥着管道也不会把等待卡死。
            start_new_session=True,
        )
        outcome = await run_process(
            proc, timeout_sec=timeout_sec, keep_stdout=keep, keep_stderr=keep, is_cancelled=ctx.check_cancelled,
        )
    except Exception as e:
        return _error_response(e)

    if outcome.cancelled:
        return _error_response("task cancelled", cancelled=True)
    if outcome.timed_out:
        return _error_response(f"timeout after {timeout_sec}s")

    stdout_text = outcome.stdout.decode("utf-8", errors="replace")
    stderr_text = outcome.stderr.decode("utf-8", errors="replace")
    out = (stdout_text or "") + ("\n" + stderr_text if stderr_text else "")
    if len(out) > max_output_chars or outcome.stdout_overflow or outcome.stderr_overflow:
        out = out[:max_output_chars] + "\n...<truncated>"
    rc = int(outcome.returncode or 0)
    # 非零退出码是命令自己的结果，不是工具失败：照样 ok=True，退出码放进 data。
    return {"ok": True, "data": {"result": f"returncode={rc}\n{out}", "returncode": rc, "output": out}}
