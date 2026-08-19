"""后台事件轮询，将 Supervisor 事件转化为 Textual Message。"""
from __future__ import annotations

import re
import asyncio
from typing import Any, TYPE_CHECKING

from clonoth_runtime import strip_tool_trace_blocks

from ..models import (
    ApprovalRequested,
    AssistantReply,
    ConfigUpdated,
    ToolActivity,
    NodeCompleted,
    NodeStarted,
    StreamEnd,
    StreamText,
    StreamThinking,
    TaskCancelled,
)

if TYPE_CHECKING:
    from textual.app import App
    from .supervisor_client import SupervisorClient


class EventPoller:
    """管理 session 事件轮询和全局事件轮询。"""

    def __init__(
        self,
        client: "SupervisorClient",
        app: "App",
        *,
        poll_fast: float = 0.1,
        poll_slow: float = 0.5,
    ) -> None:
        self._client = client
        self._app = app
        self._poll_fast = poll_fast
        self._poll_slow = poll_slow

        self._session_id: str | None = None
        self._session_seq: int = 0
        self._global_seq: int = 0
        self._streaming: bool = False

        self._session_task: asyncio.Task | None = None
        self._global_task: asyncio.Task | None = None
        self._running: bool = False

    # ---- 控制 ----

    def start(self, global_seq: int = 0) -> None:
        self._global_seq = global_seq
        self._running = True
        self._global_task = asyncio.create_task(self._poll_global_loop())

    def start_session(self, session_id: str, after_seq: int = 0) -> None:
        self.stop_session()
        self._session_id = session_id
        self._session_seq = after_seq
        self._streaming = False
        self._session_task = asyncio.create_task(self._poll_session_loop())

    def stop_session(self) -> None:
        if self._session_task and not self._session_task.done():
            self._session_task.cancel()
        self._session_task = None
        self._session_id = None

    def stop(self) -> None:
        self._running = False
        self.stop_session()
        if self._global_task and not self._global_task.done():
            self._global_task.cancel()
        self._global_task = None

    # ---- session 轮询 ----

    async def _poll_session_loop(self) -> None:
        try:
            while self._running and self._session_id:
                try:
                    await self._poll_session_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # 网络错误等，静默重试
                interval = self._poll_fast if self._streaming else self._poll_slow
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _poll_session_tick(self) -> None:
        if not self._session_id:
            return
        events = await self._client.get_session_events(
            self._session_id, after_seq=self._session_seq,
        )
        for e in events:
            seq = int(e.get("seq", 0))
            if seq > self._session_seq:
                self._session_seq = seq
            msg = self._parse_event(e)
            if msg is not None:
                self._app.post_message(msg)
            if e.get("type") in ("outbound_message", "cancel_acknowledged"):
                self._streaming = False

    # ---- 全局轮询 ----

    async def _poll_global_loop(self) -> None:
        try:
            while self._running:
                try:
                    await self._poll_global_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(self._poll_slow * 4)
        except asyncio.CancelledError:
            pass

    async def _poll_global_tick(self) -> None:
        events = await self._client.get_global_events(
            after_seq=self._global_seq,
            types="outbound_message,approval_requested,config_updated,config_reloaded,restart_completed",
        )
        for e in events:
            seq = int(e.get("seq", 0))
            if seq > self._global_seq:
                self._global_seq = seq
            et = e.get("type", "")
            if et in ("config_updated", "config_reloaded", "restart_completed"):
                self._app.post_message(ConfigUpdated(et))

    # ---- 事件解析 ----

    def _parse_event(self, e: dict[str, Any]) -> Any:
        et = e.get("type", "")
        payload = e.get("payload") or {}

        if et == "stream_delta":
            self._streaming = True
            kind = payload.get("type", "text")
            content = payload.get("content", "")
            if not content:
                return None
            if kind == "thinking":
                return StreamThinking(content)
            return StreamText(content)

        if et == "stream_end":
            return StreamEnd()

        if et == "outbound_message":
            text = payload.get("text", "")
            if isinstance(text, str) and text.strip():
                cleaned = strip_tool_trace_blocks(text)
                if cleaned:
                    return AssistantReply(cleaned)
            return None

        if et == "node_started":
            name = payload.get("node_name") or payload.get("node_id", "")
            nid = payload.get("node_id", "")
            return NodeStarted(name, nid)

        if et == "node_completed":
            name = payload.get("node_name") or payload.get("node_id", "")
            outcome = payload.get("outcome", "")
            summary = payload.get("summary", "")
            return NodeCompleted(name, outcome, summary)

        if et == "handoff_progress":
            message = str(payload.get("message") or "").strip()
            if not message:
                return None
            # 去掉 [node_id] 或 [tool] 前缀，只保留有意义的内容
            message = re.sub(r"^\[[^\]]*\]\s*", "", message)
            tool_name = str(payload.get("tool_name") or "").strip()
            node_id = str(payload.get("node_id") or "").strip()
            return ToolActivity(message=message, tool_name=tool_name, node_id=node_id)

        if et == "approval_requested":
            return ApprovalRequested(
                approval_id=payload.get("approval_id", ""),
                operation=payload.get("operation", ""),
                details=payload.get("details") or {},
                fingerprint=payload.get("fingerprint", ""),
            )

        if et == "cancel_acknowledged":
            return TaskCancelled()

        return None
