"""Supervisor HTTP API 异步客户端。"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx


class SupervisorClient:
    """封装 Supervisor 全部 HTTP 端点，全部 async。"""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            trust_env=False,
        )

    # ---- 生命周期 ----

    async def wait_ready(self, *, timeout: float = 30.0, poll: float = 0.5) -> None:
        """轮询 /v1/health 直到 supervisor 就绪。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            try:
                r = await self._client.get("/v1/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"supervisor 未在 {timeout}s 内就绪")
            await asyncio.sleep(poll)

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 消息 ----

    async def send_message(
        self,
        *,
        channel: str = "cli",
        conversation_key: str,
        text: str,
        message_id: str | None = None,
        entry_node_id: str | None = None,
    ) -> str:
        """发送用户消息，返回 session_id。"""
        mid = message_id or str(uuid.uuid4())
        body: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conversation_key,
            "message_id": mid,
            "text": text,
        }
        if entry_node_id:
            body["entry_node_id"] = entry_node_id
        r = await self._client.post("/v1/inbound", json=body)
        r.raise_for_status()
        return r.json()["session_id"]

    # ---- 事件 ----

    async def get_session_events(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"/v1/sessions/{session_id}/events",
            params={"after_seq": after_seq},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    async def get_global_events(self, after_seq: int = 0, types: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"after_seq": after_seq}
        if types:
            params["types"] = types
        r = await self._client.get("/v1/events", params=params)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    # ---- 会话控制 ----

    async def cancel_session(self, session_id: str) -> bool:
        try:
            r = await self._client.post(f"/v1/sessions/{session_id}/cancel")
            return r.status_code == 200
        except Exception:
            return False

    # ---- 审批 ----

    async def decide_approval(self, approval_id: str, decision: str, comment: str = "") -> None:
        await self._client.post(
            f"/v1/approvals/{approval_id}",
            json={"decision": decision, "comment": comment or "via TUI"},
        )

    # ---- 工具 ----

    async def fetch_latest_global_seq(self) -> int:
        """获取当前全局事件最大 seq。"""
        try:
            events = await self.get_global_events(after_seq=0)
            seq = 0
            for e in events:
                if isinstance(e, dict):
                    seq = max(seq, int(e.get("seq", 0) or 0))
            return seq
        except Exception:
            return 0

    async def fetch_model_name(self) -> str:
        """从 /v1/config/openai 获取当前模型名。"""
        try:
            r = await self._client.get("/v1/config/openai")
            if r.status_code == 200:
                data = r.json()
                return str(data.get("model", ""))
        except Exception:
            pass
        return ""

    async def restart(self, target: str = "all", reason: str = "") -> bool:
        """请求 supervisor 重启。target: 'engine' 仅重启引擎，'all' 重启整个程序。"""
        try:
            r = await self._client.post(
                "/v1/admin/restart",
                json={"target": target, "reason": reason or "via TUI /restart"},
            )
            return r.status_code == 200
        except Exception:
            return False
