"""转到后台的工具问该不该停、交回结果：都按发起任务认，不按领取认。

任务挂起再恢复后领取 id 换新，后台的命令拿旧的领取 id 去问只会被当成出局；它要知道的是发起任务本身还算不算数。
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .types import TaskStatus

log = logging.getLogger(__name__)

# engine 给的投递 id 加上这个前缀再写进事件，和别处 inbound、outbound 的 delivery_id 分开
_DELIVERY_PREFIX = "async-tool:"
# engine 落盘的结果最晚一天后补投，按一天里交得完的异步结果留足
_DELIVERED_LIMIT = 8192
_STRATEGY_OF_EVENT = {"inbound_message": "inbound", "outbound_message": "outbound_attachment"}


def delivery_key(delivery_id: Any) -> str:
    raw = str(delivery_id or "").strip()
    return f"{_DELIVERY_PREFIX}{raw}" if raw else ""


class DeliveredResults:
    """交过的异步工具结果，按投递 id 记着当时怎么交的。engine 没收到应答会重投，同一份结果只交一次。

    只在 supervisor 的全局锁里读写。
    """

    def __init__(self, limit: int = _DELIVERED_LIMIT) -> None:
        self._limit = limit
        self._strategies: OrderedDict[str, str] = OrderedDict()

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]]) -> "DeliveredResults":
        """从事件日志认回交过的：交下去之后、应答之前 supervisor 重启了，engine 补投时照样认得出。"""
        delivered = cls()
        for event in events:
            strategy = _STRATEGY_OF_EVENT.get(str(event.get("type") or ""))
            payload = event.get("payload")
            if strategy is None or not isinstance(payload, dict):
                continue
            key = str(payload.get("delivery_id") or "")
            if key.startswith(_DELIVERY_PREFIX):
                delivered.remember(key, strategy)
        return delivered

    def repeat(self, key: str) -> dict[str, Any] | None:
        """key 交过就回当时的交法，标明这次是重投；没交过回 None。"""
        strategy = self._strategies.get(key) if key else None
        if strategy is None:
            return None
        return {"ok": True, "strategy": strategy, "deduped": True}

    def recall(self, key: str, eventlog: Any) -> dict[str, Any] | None:
        """同 repeat；这里只记着最近交过的，没记着的再去事件日志的回执表里认（更早交的、重启前交的）。"""
        repeat = self.repeat(key)
        if repeat is not None or not key:
            return repeat
        for event_type, strategy in _STRATEGY_OF_EVENT.items():
            if eventlog.find_delivery_event(key, event_type=event_type) is not None:
                self.remember(key, strategy)
                return self.repeat(key)
        return None

    def remember(self, key: str, strategy: str) -> None:
        if not key:
            return
        self._strategies[key] = strategy
        self._strategies.move_to_end(key)
        while len(self._strategies) > self._limit:
            self._strategies.popitem(last=False)


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
            delivery_id=str(body.get("delivery_id") or ""),
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
