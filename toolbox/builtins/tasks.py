"""Task management: cancel_active_tasks, get_context_window."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: task-management tools return supervisor payloads
    # whose shape can vary. How: wrap them under data with a stable result string.
    # Purpose: keep task tools in the same ok/data/error contract.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: supervisor HTTP errors should expose data.result for
    # readable history. How: normalize the error string once. Purpose: avoid legacy
    # ok=false payloads from task tools.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


async def cancel_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Cancel all active downstream tasks in the current session."""
    try:
        # [Fork/Merge 2026-05-17] Why: this tool is session-scoped, but a caller
        # may be executing inside a temporary entry branch. How: use ToolContext's
        # parent-first route session. Purpose: sibling branch tasks under the same
        # user conversation can be cancelled together.
        route_session_id = ctx.route_session_id()
        # [2026-05-28] 支持可选 node_id 过滤：只取消指定节点的活跃任务。
        # 为什么：有时只需取消某个子节点的任务，而非 session 内全部。
        # 怎么改：如果 args 中有 node_id，作为 query param 传给 supervisor。
        # 目的：更细粒度的任务取消控制。
        params: dict[str, str] = {"exclude_task_id": ctx.task_id}
        _node_id = str(args.get("node_id") or "").strip()
        if _node_id:
            params["node_id"] = _node_id
        r = await ctx.http.post(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
            params=params,
        )
        if r.status_code >= 400:
            return _err(r.text)
        payload = r.json()
        return _ok("Active tasks cancelled", response=payload)
    except Exception as e:
        return _err(e)


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _context_window_text(payload: dict[str, Any]) -> str:
    # data.result is the only part of a tool result the model reads, so the numbers have to be in it
    tokens = payload.get("effective_tokens")
    source = "reported by the model" if payload.get("source") == "llm_usage" else "estimated from the stored messages"
    lines = [f"Context in use: {tokens if tokens is not None else 'unknown'} tokens ({source})."]
    threshold = _positive(payload.get("compact_threshold"))
    if threshold is None:
        lines.append("Automatic compaction is off.")
        return " ".join(lines)
    hard = _positive(payload.get("compact_hard_threshold"))
    window = _positive(payload.get("context_window"))
    detail = f"Background compaction starts at {threshold} tokens"
    if hard is not None:
        detail += f", and at {hard} tokens the turn waits for compaction"
    if window is not None:
        detail += f"; the model's context window is {window} tokens"
    lines.append(detail + ".")
    utilization = payload.get("utilization")
    if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
        lines.append(f"{utilization:.1%} of the compaction threshold is used.")
    return " ".join(lines)


async def get_context_window(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get current context window token usage and the compaction thresholds in effect for the session."""
    try:
        # [Fork/Merge 2026-05-17] Why: context usage events are emitted to the
        # parent route session, not to branch storage sessions. How: query the
        # same parent-first route used by tool events. Purpose: avoid returning an
        # empty branch estimate while the parent has the real usage record.
        route_session_id = ctx.route_session_id()
        r = await ctx.http.get(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/context_window",
        )
        if r.status_code >= 400:
            return _err(r.text)
        payload = r.json()
        if isinstance(payload, dict):
            return _ok(_context_window_text(payload), **payload)
        return _ok("Context window retrieved", value=payload)
    except Exception as e:
        return _err(e)
