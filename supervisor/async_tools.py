"""转到后台的工具问该不该停、交回结果：都按发起任务认，不按领取认。

任务挂起再恢复后领取 id 换新，后台的命令拿旧的领取 id 去问只会被当成出局；它要知道的是发起任务本身还算不算数。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .types import TaskStatus

log = logging.getLogger(__name__)


def detached_cancelled_locked(state: Any, task_id: str, session_id: str = "") -> bool:
    """转到后台的工具该不该停：发起任务被取消、失败，或它的会话被取消、重置。挂起恢复、排队重跑、正常结束都不算。

    和异步结果的丢弃规则对齐：结果反正交不回去的，命令也就不必再跑。任务记录已被清理时只看会话。
    """
    task = state.tasks.get(str(task_id or "").strip())
    if task is None:
        sid = str(session_id or "").strip()
        return bool(sid) and sid in state._cancelled_sessions
    if task.status in (TaskStatus.cancelled, TaskStatus.failed):
        return True
    task_input = task.input if isinstance(task.input, dict) else {}
    if task.cancel_requested or task_input.get("_session_reset"):
        return True
    route = state._route_session_id_for_task_locked(task) or task.session_id
    if route in state._cancelled_sessions:
        return True
    current = state._current_session_generation_locked(route)
    if task.session_generation and current and task.session_generation != current:
        return True
    return bool(state._task_cancel_pending_locked(task))


def _attachment_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def create_router(state: Any) -> APIRouter:
    from .admin_api import verify_admin_token

    router = APIRouter(prefix="/v1/async_tools", tags=["async_tools"], dependencies=[Depends(verify_admin_token)])

    @router.get("/{task_id}/cancelled")
    def cancelled(task_id: str, session_id: str = Query("", max_length=512)) -> dict[str, Any]:
        with state._lock:
            return {"cancelled": detached_cancelled_locked(state, task_id, session_id)}

    @router.post("/results")
    async def results(request: Request) -> dict[str, Any]:
        """按发起任务交回结果：落点由 supervisor 定，投递时给的会话已经不在也不报 404。"""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="body must be a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=422, detail="session_id required")
        success = body.get("success")
        result = await run_in_threadpool(
            state.inject_async_result,
            session_id,
            text=str(body.get("message") or ""),
            attachments=_attachment_paths(body.get("attachment_paths")),
            node_id=str(body.get("node_id") or ""),
            task_id=str(body.get("task_id") or ""),
            tool_name=str(body.get("tool_name") or ""),
            success=success if isinstance(success, bool) else None,
            error=str(body.get("error") or ""),
            cancelled=body.get("cancelled") is True,
        )
        if result.get("ok"):
            return result
        if result.get("error") == "session not found":
            log.warning(
                "async tool result dropped: session %s not found and task %s gone",
                session_id[:12], str(body.get("task_id") or "")[:12],
            )
            return {"ok": True, "strategy": "dropped", "reason": "session_not_found"}
        raise HTTPException(status_code=500, detail=str(result.get("error") or "unknown"))

    return router
