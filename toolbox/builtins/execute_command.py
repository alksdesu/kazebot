"""execute_command — shell command execution (policy + approval guarded)."""
from __future__ import annotations

import asyncio
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from ..context import ToolContext, project_root_of
from .._common import ProcessOutcome, fit_output, join_outputs, request_guard, run_process, safe_subprocess_env

# 每一路输出留开头和结尾各这么多字节，中间的边读边扔；超长输出交给 engine 落盘，模型按需翻页
CAPTURE_HEAD_BYTES = 256 * 1024
CAPTURE_TAIL_BYTES = 256 * 1024
MIN_OUTPUT_CHARS = 1000
MAX_OUTPUT_CHARS = 2_000_000


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    # 报错同时放进 error 和 data.result；cancelled 这类标记在顶层和 data 里各放一份，新旧读法都认得。
    text = str(message)
    payload: dict[str, Any] = {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}
    if extra:
        payload.update(extra)
        payload["data"].update(extra)
    return payload


def timeout_bounds(runtime_cfg: dict[str, Any]) -> tuple[float, float, float]:
    """(默认, 下限, 上限) 秒：模型传的 timeout_sec 夹在上下限之间，默认值也一样。"""
    minimum = get_float(runtime_cfg, "meta.execute_command.min_timeout_sec", 1.0, min_value=0.1, max_value=3600.0)
    maximum = max(
        minimum,
        get_float(runtime_cfg, "meta.execute_command.max_timeout_sec", 3600.0, min_value=1.0, max_value=86400.0),
    )
    default = get_float(
        runtime_cfg,
        "meta.execute_command.default_timeout_sec",
        90.0,
        min_value=1.0,
        max_value=3600.0,
    )
    return min(max(default, minimum), maximum), minimum, maximum


def command_timeout(runtime_cfg: dict[str, Any], raw: Any) -> float:
    """这次命令的硬时限。没传、不是数、不大于 0 的按默认值；其余夹到配置的上下限。

    0 和负数当成没给：照字面夹到下限等于一起跑就超时。
    """
    default, minimum, maximum = timeout_bounds(runtime_cfg)
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if value != value or value <= 0:
        return default
    return min(max(value, minimum), maximum)


def requested_budget(raw: Any) -> int | None:
    """模型传的 max_output_chars；没传、不是正整数时是 None，太小的按下限算。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    if value <= 0:
        return None
    return max(MIN_OUTPUT_CHARS, min(MAX_OUTPUT_CHARS, value))


async def execute_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    # 0 表示不按字数截，超长的交给 engine 落盘；实例在 runtime 里配了正数时，模型没传就按它截
    configured_budget = get_int(
        runtime_cfg,
        "meta.execute_command.max_output_chars",
        0,
        min_value=0,
        max_value=MAX_OUTPUT_CHARS,
    )

    timeout_sec = command_timeout(runtime_cfg, args.get("timeout_sec"))

    budget = requested_budget(args.get("max_output_chars"))
    if budget is None and configured_budget > 0:
        budget = max(MIN_OUTPUT_CHARS, configured_budget)

    _op, err = await request_guard(ctx, "execute_command", {"command": command})
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    if await ctx.check_cancelled():
        return _error_response("task cancelled", cancelled=True)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(project_root_of(ctx)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
            # 独立进程组：超时或取消时整棵进程树一起杀，孙进程攥着管道也不会把等待卡死。
            start_new_session=True,
        )
        outcome = await run_process(
            proc,
            timeout_sec=timeout_sec,
            keep_stdout=CAPTURE_HEAD_BYTES,
            keep_stderr=CAPTURE_HEAD_BYTES,
            tail_stdout=CAPTURE_TAIL_BYTES,
            tail_stderr=CAPTURE_TAIL_BYTES,
            is_cancelled=ctx.check_cancelled,
        )
    except Exception as e:
        return _error_response(e)

    if outcome.cancelled:
        return _stopped_response("task cancelled", outcome, budget, cancelled=True)
    if outcome.timed_out:
        return _stopped_response(f"timeout after {timeout_sec}s", outcome, budget, timed_out=True)

    out, extra = _output_fields(outcome, budget)
    rc = int(outcome.returncode or 0)
    # 非零退出码是命令自己的结果，不是工具失败：照样 ok=True，退出码放进 data。
    data: dict[str, Any] = {"result": f"returncode={rc}\n{out}", "returncode": rc, "output": out, **extra}
    return {"ok": True, "data": data}


def _output_fields(outcome: ProcessOutcome, budget: int | None) -> tuple[str, dict[str, Any]]:
    """收到的输出按预算截好，连同截掉多少的那几项。"""
    joined = join_outputs(outcome.stdout_capture, outcome.stderr_capture)
    out, omitted = fit_output(joined, budget) if budget is not None else (joined.text, 0)
    extra: dict[str, Any] = {}
    if joined.dropped or omitted:
        extra["truncated"] = True
    if joined.dropped:
        extra["droppedBytes"] = joined.dropped
    if omitted:
        extra["omittedChars"] = omitted
    return out, extra


def _stopped_response(message: str, outcome: ProcessOutcome, budget: int | None, **flags: Any) -> dict[str, Any]:
    """超时或被取消：照样按失败报，已经读到的输出一起带回去。"""
    payload = _error_response(message, **flags)
    out, extra = _output_fields(outcome, budget)
    if out.strip():
        payload["data"].update({
            "result": f"ERROR: {message}; the command was killed. Output received before that:\n{out}",
            "output": out,
            **extra,
        })
    return payload
