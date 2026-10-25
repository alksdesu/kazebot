"""Shared utilities and constants for built-in tools."""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import classify_path, load_policy_config, parse_extra_roots
from platforms.protection import secret_env_names

from .context import ToolContext


_READ_CHUNK_BYTES = 64 * 1024
# 杀掉进程组后管道应当很快关上；漏网的孙进程还攥着管道时最多再等这么久，别为它卡死。
_KILL_GRACE_SEC = 5.0


def kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the entire process group to avoid orphaned children holding pipes.

    proc.kill() only kills the direct child (shell); grandchildren (e.g. a PM2 daemon)
    inherit the pipe fds and keep the reader waiting for EOF. Processes are started with
    start_new_session=True so os.killpg takes the whole tree down.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # 没有进程组可杀（Windows）或者 killpg 失败：退而只杀直接子进程。
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


@dataclass
class ProcessOutcome:
    """一次子进程运行的结果。超出保留上限的输出边读边扔，只留下是否溢出的标记。"""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_overflow: bool = False
    stderr_overflow: bool = False
    timed_out: bool = False
    cancelled: bool = False


async def _drain(stream: asyncio.StreamReader | None, keep: int) -> tuple[bytes, bool]:
    """读到 EOF，只留前 keep 字节。多出来的照读照扔：不读走的话子进程写满管道就会卡住。"""
    if stream is None:
        return b"", False
    kept = bytearray()
    overflow = False
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(kept), overflow
        room = keep - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > room:
            overflow = True


async def _feed_and_collect(
    proc: "asyncio.subprocess.Process", stdin: bytes | None, readers: "asyncio.Future[Any]",
) -> Any:
    if proc.stdin is not None:
        try:
            if stdin:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
        except OSError:
            # 子进程没读完输入就退出了（断管道、连接重置，Windows 上还有管道正在关闭）：照常收它的输出和退出码。
            pass
        finally:
            proc.stdin.close()
    outputs = await readers
    await proc.wait()
    return outputs


async def _cancel_requested(probe: Callable[[], Awaitable[bool]] | None) -> bool:
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False


async def run_process(
    proc: "asyncio.subprocess.Process",
    *,
    timeout_sec: float,
    keep_stdout: int,
    keep_stderr: int,
    stdin: bytes | None = None,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    poll_interval: float = 0.2,
) -> ProcessOutcome:
    """等进程跑完、收齐输出。

    超时、任务被取消，以及等待本身被取消（CancelledError）时都会杀掉整个进程组，
    所以调用方须以 start_new_session=True 启动进程。
    """
    readers = asyncio.gather(_drain(proc.stdout, keep_stdout), _drain(proc.stderr, keep_stderr))
    finished = asyncio.ensure_future(_feed_and_collect(proc, stdin, readers))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_sec), 0.0)

    async def stop(**flags: bool) -> ProcessOutcome:
        kill_process_group(proc)
        try:
            outputs = await asyncio.wait_for(asyncio.shield(finished), timeout=_KILL_GRACE_SEC)
        except Exception:
            outputs = ((b"", False), (b"", False))
        (stdout, stdout_overflow), (stderr, stderr_overflow) = outputs
        return ProcessOutcome(proc.returncode, stdout, stderr, stdout_overflow, stderr_overflow, **flags)

    try:
        while True:
            done, _pending = await asyncio.wait({finished}, timeout=poll_interval)
            if finished in done:
                (stdout, stdout_overflow), (stderr, stderr_overflow) = finished.result()
                return ProcessOutcome(proc.returncode, stdout, stderr, stdout_overflow, stderr_overflow)
            if await _cancel_requested(is_cancelled):
                return await stop(cancelled=True)
            if loop.time() >= deadline:
                return await stop(timed_out=True)
    finally:
        if proc.returncode is None:
            kill_process_group(proc)
        for pending in (finished, readers):
            if not pending.done():
                pending.cancel()


_SENSITIVE_ENV_KEYS_UPPER = {
    # Bearer for the whole admin API, policy included: a tool holding it could approve itself.
    "CLONOTH_ADMIN_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


def safe_subprocess_env() -> dict[str, str]:
    """Return a subprocess env with common secret variables stripped.

    This is not a complete security boundary, but it meaningfully reduces
    accidental leakage from command execution.
    """
    env = os.environ.copy()
    platform_secrets = secret_env_names()
    for k in list(env.keys()):
        ku = k.upper()

        if ku == "NOVELAI_API_KEY":
            continue
        if ku in _SENSITIVE_ENV_KEYS_UPPER:
            env.pop(k, None)
            continue
        if ku.endswith("_API_KEY"):
            env.pop(k, None)
            continue
        # Declared by platforms/; a tool that needs one reads data/platform_secrets.yaml or .env itself.
        if ku in platform_secrets:
            env.pop(k, None)
            continue
    return env


def resolve_under_root(root: Path, rel_path: str) -> Path:
    """Resolve *rel_path* under *root*, raising if it escapes."""
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace root")
    return p


def _load_allowed_extra_roots(workspace_root: Path) -> list[Path]:
    """Load extra_roots from policy.yaml for defense in depth path checks."""
    data = load_policy_config(workspace_root)
    return parse_extra_roots(
        workspace_root,
        data.get("extra_roots") if isinstance(data, dict) else None,
    )


def resolve_and_classify(workspace_root: Path, path_str: str) -> tuple[Path, bool]:
    """Resolve path and classify as internal (False) or external (True).

    Returns (resolved_path, is_external).
    Raises ValueError for invalid or escaping relative paths.
    """
    extra_roots = _load_allowed_extra_roots(workspace_root)
    resolved, display, is_external = classify_path(workspace_root, extra_roots, path_str)
    if resolved is None:
        raise ValueError(display)
    return resolved, is_external


def resolve_under_allowed_roots(workspace_root: Path, path_str: str) -> Path:
    """Resolve path under workspace_root or policy-configured extra_roots.

    Allows untrusted external absolute paths (policy layer enforces approval).
    """
    resolved, _is_external = resolve_and_classify(workspace_root, path_str)
    return resolved


async def request_guard(
    ctx: ToolContext,
    op: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Request supervisor policy decision and wait for approval when needed.

    Returns (op_response, error_result).
    error_result is None when the operation may proceed.
    """
    op_res = await ctx.request_op(op, parameters)
    safety_level = str(op_res.get("safety_level") or "")

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    if safety_level == "deny":
        return op_res, {"ok": False, "error": op_res.get("reason", "denied")}

    if safety_level == "approval_required":
        approval_id = op_res.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") == "cancelled":
            return op_res, {"ok": False, "error": "task cancelled", "approval_id": approval_id, "cancelled": True}
        if approval.get("status") != "allowed":
            return op_res, {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    return op_res, None


async def guard_external_read(
    ctx: ToolContext,
    is_external: bool,
    rel_path: str,
    tool_name: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Request approval for external paths. Returns error dict or None (proceed)."""
    if not is_external:
        return None
    _op, err = await request_guard(
        ctx, "read_file",
        {"path": rel_path, "tool_name": tool_name,
         "reason": reason or f"{tool_name} on external path: {rel_path}"},
    )
    if err is not None:
        return {
            "success": False,
            "cancelled": err.get("cancelled", False),
            "error": err.get("error", "denied"),
        }
    return None
