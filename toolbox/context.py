from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# 工具按 0.2s 轮询取消，单次查询不能被无响应的 supervisor 拖住；超时当作没取消，下一轮再问。
_CANCEL_PROBE_TIMEOUT_SEC = 2.0
# 等审批期间引擎心跳仍在跑，这里的续租只是兜底，所以间隔放宽、租期放长。
_APPROVAL_RENEW_INTERVAL_SEC = 30.0
_APPROVAL_RENEW_LEASE_SEC = 300.0
_RENEW_TIMEOUT_SEC = 5.0


@dataclass
class ToolContext:
    supervisor_url: str
    session_id: str
    run_id: str
    worker_id: str
    workspace_root: Path
    http: httpx.AsyncClient
    registry: Any
    task_id: str = ""
    session_generation: int = 0
    # 工具可能跑在入口分支里，面向用户的 supervisor 接口要按父会话路由。
    parent_session_id: str = ""
    conversation_key: str = ""
    approval_poll_interval_sec: float = 0.5
    # 无人审批时按 deny 收场，免得任务一直挂在 running；CLONOTH_APPROVAL_TIMEOUT_SECONDS 可覆盖。
    approval_timeout_sec: float = 3600.0
    # 审批卡要并进对应的工具调用卡片，靠这两项对上。
    tool_call_id: str = ""
    node_id: str = ""
    platform_auth: dict[str, Any] | None = None
    # 续租和查取消都带上它：任务被回收、改由别处重领后，旧的执行方才问得出自己已经出局。
    claim_id: str = ""

    def route_session_id(self) -> str:
        """Return the user-visible session for supervisor API calls."""
        # session_id 可能是临时分支 branch_xxx，对外的副作用要落到父会话上。
        return (self.parent_session_id or self.session_id or "").strip()

    async def emit_event(self, type_: str, payload: dict[str, Any]) -> None:
        route_session_id = self.route_session_id()
        if self.parent_session_id and self.parent_session_id != self.session_id:
            payload.setdefault("parent_session_id", self.parent_session_id)
            payload.setdefault("branch_session_id", self.session_id)
        await self.http.post(
            f"{self.supervisor_url}/v1/sessions/{route_session_id}/events",
            json={"type": type_, "payload": payload},
        )

    def _op_body(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": self.route_session_id(),
            "op": op,
            "parameters": {**parameters, "platform_auth": dict(self.platform_auth or {})},
            "tool_call_id": self.tool_call_id or "",
            "node_id": self.node_id or getattr(self, "_node_id", "") or "",
            "task_id": self.task_id or "",
        }

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(f"{self.supervisor_url}/v1/ops/request", json=self._op_body(op, parameters))
        r.raise_for_status()
        return r.json()

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """和 request_op 同一套判定，但 supervisor 不创建审批：只回 safety_level 和 reason，绝不弹审批卡。"""
        r = await self.http.post(f"{self.supervisor_url}/v1/ops/evaluate", json=self._op_body(op, parameters))
        r.raise_for_status()
        return r.json()

    async def check_cancelled(self) -> bool:
        try:
            if self.task_id:
                extra: dict[str, Any] = {"params": {"claim_id": self.claim_id}} if self.claim_id else {}
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled",
                    timeout=_CANCEL_PROBE_TIMEOUT_SEC,
                    **extra,
                )
            else:
                # 没有 task_id 的工具按会话取消，取消是用户在父会话上点的。
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.route_session_id()}/cancelled",
                    timeout=_CANCEL_PROBE_TIMEOUT_SEC,
                )
            if r.status_code == 200:
                return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def renew_lease(self, lease_sec: float) -> bool | None:
        """续租本任务。False 表示 supervisor 明确回绝了这次领取，None 表示这轮没问到结果。"""
        if not self.task_id:
            return None
        body: dict[str, Any] = {"worker_id": self.worker_id, "lease_sec": lease_sec}
        if self.claim_id:
            body["claim_id"] = self.claim_id
        try:
            r = await self.http.post(
                f"{self.supervisor_url}/v1/tasks/{self.task_id}/renew_lease",
                json=body,
                timeout=_RENEW_TIMEOUT_SEC,
            )
            verdict = r.json()
        except Exception:
            return None
        if not isinstance(verdict, dict) or not isinstance(verdict.get("ok"), bool):
            return None
        # 没有领取凭据的旧任务拿不准 ok=False 是不是自己的问题，照旧只管续、不据此收手。
        if verdict["ok"] is False and not self.claim_id:
            return None
        return verdict["ok"]

    def _resolve_approval_timeout_sec(self) -> float:
        """审批等待超时上限。优先 env，其次数据类字段；<=0 表示不超时。"""
        raw = os.getenv("CLONOTH_APPROVAL_TIMEOUT_SECONDS")
        if raw is not None:
            try:
                return float(raw)
            except Exception:
                pass
        try:
            return float(self.approval_timeout_sec)
        except Exception:
            return 3600.0

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        interval = float(poll_interval) if poll_interval is not None else float(self.approval_poll_interval_sec or 0.5)
        if interval <= 0:
            interval = 0.5
        last_renew = 0.0
        timeout_sec = self._resolve_approval_timeout_sec()
        wait_started = time.monotonic()
        while True:
            if await self.check_cancelled():
                return {"approval_id": approval_id, "status": "cancelled"}
            r = await self.http.get(f"{self.supervisor_url}/v1/approvals/{approval_id}")
            r.raise_for_status()
            approval = r.json()
            if approval.get("status") != "pending":
                return approval
            if timeout_sec > 0 and (time.monotonic() - wait_started) >= timeout_sec:
                timeout_comment = f"approval timed out after {int(timeout_sec)}s with no decision; auto-denied"
                # 先让 supervisor 落库 deny，审批卡才不会残留 pending；落不了库也照样按 deny 收手。
                try:
                    await self.http.post(
                        f"{self.supervisor_url}/v1/approvals/{approval_id}",
                        json={"decision": "deny", "comment": timeout_comment},
                    )
                except Exception:
                    pass
                return {
                    "approval_id": approval_id,
                    "status": "denied",
                    "decision": "deny",
                    "timed_out": True,
                    "comment": timeout_comment,
                }
            now = time.monotonic()
            if self.task_id and now - last_renew >= _APPROVAL_RENEW_INTERVAL_SEC:
                last_renew = now
                if await self.renew_lease(_APPROVAL_RENEW_LEASE_SEC) is False:
                    return {"approval_id": approval_id, "status": "cancelled", "reason": "task claim is no longer held"}
            await asyncio.sleep(interval)
