"""常驻后台进程的四个工具：进程由 supervisor 持有，按会话归属，任务结束、挂起恢复、engine 重启都不影响它。"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from clonoth_runtime import get_float, load_runtime_config

from .._common import request_guard

log = logging.getLogger(__name__)

# 起进程走的是执行命令的审批规则和白名单；审批卡上写明它会一直跑
RESIDENT_REASON = "常驻进程：命令会在后台一直运行，直到被 stop_process 停掉、长时间没人读输出、运行时长到期或会话重置"
_HTTP_TIMEOUT_SEC = 30.0
# 停进程要等它退出（先 TERM，宽限期后 KILL），再等读输出的线程收尾
_STOP_TIMEOUT_SEC = 120.0
_READY_POLL_SEC = 0.5
_READY_TIMEOUT_MAX_SEC = 600.0
_START_TAIL_LINES = 20
_STATUS_LABELS = {
    "starting": "正在启动",
    "running": "运行中",
    "stopping": "正在停止",
    "exited": "已退出",
    "stopped": "已停止",
    "failed": "启动失败",
}
_ENDED = frozenset({"exited", "stopped", "failed"})


class _ProcessToolError(Exception):
    pass


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}


def _detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])
    return f"supervisor 回了 HTTP {response.status_code}"


async def _call(
    ctx: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = _HTTP_TIMEOUT_SEC,
) -> dict[str, Any]:
    if not ctx.task_id:
        raise _ProcessToolError("没有任务身份，不能管理常驻进程")
    try:
        response = await ctx.http.request(
            method, f"{ctx.supervisor_url.rstrip('/')}{path}", json=body, params=params, timeout=timeout,
        )
    except Exception as exc:
        raise _ProcessToolError(f"连不上 supervisor：{type(exc).__name__}: {exc}") from None
    if response.status_code >= 400:
        raise _ProcessToolError(_detail(response))
    data = response.json()
    if not isinstance(data, dict):
        raise _ProcessToolError("supervisor 回的不是 JSON 对象")
    return data


def _duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    hours, rest = divmod(max(0, total), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def status_line(view: dict[str, Any]) -> str:
    status = str(view.get("status") or "")
    parts = [f"{view.get('id')}（{view.get('name')}）{_STATUS_LABELS.get(status, status)}"]
    if status in _ENDED:
        if view.get("returncode") is not None:
            parts.append(f"returncode={view['returncode']}")
        reason = str(view.get("stop_reason_text") or view.get("stop_reason") or "")
        if reason:
            parts.append(reason)
        parts.append(f"共运行 {_duration(view.get('uptime_sec'))}")
    else:
        parts.append(f"已运行 {_duration(view.get('uptime_sec'))}")
        if view.get("pid"):
            parts.append(f"pid {view['pid']}")
    if view.get("ready") is True:
        parts.append(f"已就绪（第 {view.get('ready_line')} 行出现 ready_pattern）")
    elif view.get("ready") is False and status not in _ENDED:
        parts.append("还没等到 ready_pattern")
    if view.get("log_error"):
        parts.append(f"输出写不进日志：{view['log_error']}")
    return "，".join(parts)


def _node_may_run_commands(ctx: Any) -> bool:
    """起常驻进程就是跑命令：节点调不了 execute_command（比如 deny 了它），也就不许起。"""
    node_id = str(getattr(ctx, "node_id", "") or getattr(ctx, "_node_id", "") or "").strip()
    if not node_id:
        return True
    from engine.inference.pseudo_tools import tool_allowed
    from engine.node import load_node

    node = load_node(ctx.workspace_root, node_id)
    return node is not None and tool_allowed(node, "execute_command")


def _ready_timeout(ctx: Any, raw: Any) -> float:
    default = get_float(
        load_runtime_config(ctx.workspace_root), "processes.ready_timeout_sec", 60.0,
        min_value=1.0, max_value=_READY_TIMEOUT_MAX_SEC,
    )
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if value != value or value <= 0:
        return default
    return min(max(value, 1.0), _READY_TIMEOUT_MAX_SEC)


async def _wait_ready(ctx: Any, process_id: str, timeout_sec: float) -> tuple[dict[str, Any], bool]:
    """等到出现 ready_pattern、进程结束或超时；返回 (最后的状态, 是否超时)。本任务被取消就不等了，进程照样留着。"""
    deadline = time.monotonic() + timeout_sec
    while True:
        view = (await _call(ctx, "GET", f"/v1/processes/{process_id}", params={"task_id": ctx.task_id}))["process"]
        if view.get("ready") is True or view.get("status") in _ENDED:
            return view, False
        if time.monotonic() >= deadline or await ctx.check_cancelled():
            return view, True
        await asyncio.sleep(_READY_POLL_SEC)


async def start_process(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        return _failure("command 不能为空")
    try:
        allowed = await asyncio.to_thread(_node_may_run_commands, ctx)
    except Exception as exc:
        log.warning("checking whether node %s may run commands failed", getattr(ctx, "node_id", ""), exc_info=True)
        return _failure(f"核对不了这个节点能不能执行命令：{exc}")
    if not allowed:
        return _failure("这个节点不能执行命令（execute_command 不在它的工具里），也就不能起常驻进程")
    ready_pattern = str(args.get("ready_pattern") or "")
    _op, err = await request_guard(ctx, "execute_command", {
        "command": command,
        "tool_name": "start_process",
        "reason": RESIDENT_REASON,
        "resident": True,
    })
    if err is not None:
        failed = _failure(str(err.get("error") or "denied"))
        if err.get("cancelled"):
            failed["cancelled"] = True
            failed["data"]["cancelled"] = True
        return failed
    try:
        started = await _call(ctx, "POST", "/v1/processes", body={
            "task_id": ctx.task_id,
            "command": command,
            "name": str(args.get("name") or ""),
            "ready_pattern": ready_pattern,
            "max_runtime_sec": _number_arg(args.get("max_runtime_sec")),
        })
        view = started["process"]
        timed_out = False
        ready_timeout = _ready_timeout(ctx, args.get("ready_timeout_sec"))
        if ready_pattern and view.get("status") not in _ENDED and view.get("ready") is not True:
            view, timed_out = await _wait_ready(ctx, str(view["id"]), ready_timeout)
        tail = await _call(
            ctx, "GET", f"/v1/processes/{view['id']}/output",
            params={"task_id": ctx.task_id, "tail": _START_TAIL_LINES},
        )
    except _ProcessToolError as exc:
        return _failure(str(exc))
    lines = [f"已起常驻进程：{status_line(view)}。工作目录 {view.get('cwd')}，最长运行 {_duration(view.get('max_runtime_sec'))}。"]
    if ready_pattern and timed_out and view.get("status") not in _ENDED:
        lines.append(f"等了 {_duration(ready_timeout)} 还没出现 ready_pattern，进程还在跑。")
    lines.append(f"最近的输出：\n{tail.get('text') or '（还没有输出）'}")
    lines.append("用 read_process_output 读输出（since 接着读新的），stop_process 停掉；长时间没人读输出会被自动停掉。")
    return {"ok": True, "data": {"result": "\n".join(lines), "process": view, "ready_timed_out": timed_out}}


def _number_arg(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _int_arg(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    return value


async def read_process_output(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    process_id = str(args.get("id") or "").strip()
    if not process_id:
        return _failure("id 不能为空")
    params: dict[str, Any] = {"task_id": ctx.task_id}
    tail = _int_arg(args.get("tail"))
    since = _int_arg(args.get("since"))
    max_lines = _int_arg(args.get("max_lines"))
    search = str(args.get("search") or "")
    if tail is not None and tail > 0:
        params["tail"] = tail
    if since is not None and since >= 0:
        params["since"] = since
    if max_lines is not None and max_lines > 0:
        params["max_lines"] = max_lines
    if search:
        params["search"] = search
    try:
        page = await _call(ctx, "GET", f"/v1/processes/{process_id}/output", params=params)
    except _ProcessToolError as exc:
        return _failure(str(exc))
    view = page.get("process") or {}
    text = f"{status_line(view)}\n{page.get('text') or ''}"
    return {"ok": True, "data": {
        "result": text,
        "process": view,
        "next_since": page.get("next_since"),
        "more": bool(page.get("more")),
        "last_line": page.get("last_line"),
    }}


async def stop_process(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    process_id = str(args.get("id") or "").strip()
    if not process_id:
        return _failure("id 不能为空")
    try:
        stopped = await _call(
            ctx, "POST", f"/v1/processes/{process_id}/stop", body={"task_id": ctx.task_id}, timeout=_STOP_TIMEOUT_SEC,
        )
    except _ProcessToolError as exc:
        return _failure(str(exc))
    view = stopped.get("process") or {}
    prefix = "已停掉" if stopped.get("stopped") else "没有要停的：它已经结束了"
    return {"ok": True, "data": {"result": f"{prefix}。{status_line(view)}", "process": view}}


async def list_processes(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        listed = await _call(ctx, "GET", "/v1/processes", params={"task_id": ctx.task_id})
    except _ProcessToolError as exc:
        return _failure(str(exc))
    views = [view for view in listed.get("processes") or [] if isinstance(view, dict)]
    if not views:
        return {"ok": True, "data": {"result": "这个会话没有常驻进程。", "processes": []}}
    rows = [f"- {status_line(view)}；命令：{view.get('command')}；输出到第 {view.get('last_line')} 行" for view in views]
    return {"ok": True, "data": {"result": "这个会话的常驻进程：\n" + "\n".join(rows), "processes": views}}


def register_tools(registry: Any) -> None:
    registry.register_builtin_tool(
        "start_process",
        "Start a long-running background process (dev server, watcher, long build) that outlives this task: it keeps "
        "running across task ends, suspend/resume and engine restarts, and belongs to this conversation. The command "
        "runs in the system shell in workspace root under the same approval rules and allowlist as execute_command; "
        "the approval card marks it as resident. stdout and stderr are logged together. With ready_pattern (a regex) "
        "the call waits until a line of output matches, the process ends, or ready_timeout_sec passes. It is stopped "
        "by stop_process, when its output has not been read for processes.idle_timeout_minutes, when max_runtime_sec "
        "passes, when the conversation is reset, or when the supervisor exits. There is a per-conversation and a "
        "global limit on how many run at once. Use execute_command for commands that finish on their own.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line, run by the system shell in workspace root."},
                "name": {"type": "string", "description": "Short label shown in listings. Defaults to the command."},
                "ready_pattern": {
                    "type": "string",
                    "description": "Wait until a line of output matches this regex before returning, e.g. 'Listening on'.",
                },
                "ready_timeout_sec": {
                    "type": "number",
                    "description": "How long to wait for ready_pattern (default processes.ready_timeout_sec, 60; at most 600). "
                    "The process keeps running when the wait times out.",
                },
                "max_runtime_sec": {
                    "type": "number",
                    "description": "Stop the process after this many seconds (default processes.default_max_runtime_sec, "
                    "4 hours; capped by processes.max_runtime_cap_sec).",
                },
            },
            "required": ["command"],
        },
        start_process,
    )
    registry.register_builtin_tool(
        "read_process_output",
        "Read the output log of a background process started with start_process. By default returns the last lines "
        "(tail). since=N returns the lines after line N; every result gives the since to continue from, so polling "
        "with it returns only new output. search finds lines matching a regex (or plain substring) after since, with "
        "context. Line numbers are absolute and survive log rotation; rotated-out lines are reported as gone. Reading "
        "keeps the process from being stopped as idle.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Process id returned by start_process or list_processes."},
                "tail": {"type": "integer", "description": "Number of last lines to show (default 100, at most 2000)."},
                "since": {"type": "integer", "description": "Show the lines after this line number."},
                "max_lines": {"type": "integer", "description": "With since: at most this many lines (default 200, at most 2000)."},
                "search": {"type": "string", "description": "Regex or substring to search for, after since when given."},
            },
            "required": ["id"],
        },
        read_process_output,
        {"repeatable": True, "parallel_safe": True},
    )
    registry.register_builtin_tool(
        "stop_process",
        "Stop a background process started with start_process, together with everything it started (TERM, then KILL "
        "after a grace period; the whole process tree on Windows). Its log stays readable for a while.",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Process id."}},
            "required": ["id"],
        },
        stop_process,
    )
    registry.register_builtin_tool(
        "list_processes",
        "List the background processes of this conversation, running and recently ended: id, name, status, uptime, "
        "command and how far the output goes.",
        {"type": "object", "properties": {}, "required": []},
        list_processes,
        {"repeatable": True, "parallel_safe": True},
    )
