from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


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
    # [Fork/Merge 2026-05-17] Why: tools can execute inside an entry branch while
    # user-visible supervisor APIs are keyed by the parent session. How: carry the
    # parent separately and let route_session_id() choose it when present. Purpose:
    # approvals, tool events, and session-scoped tools do not target a temporary branch.
    parent_session_id: str = ""
    conversation_key: str = ""
    approval_poll_interval_sec: float = 0.5
    # [AutoC] 审批等待超时上限（秒）。超时后自动按 deny 处理，避免任务无人
    # 审批时长期挂在 running 状态，直到硬上限才被 scheduler 回收。
    # 默认 3600s（1 小时），可由 CLONOTH_APPROVAL_TIMEOUT_SECONDS 覆盖。
    approval_timeout_sec: float = 3600.0
    # [AutoC 2026-05-31] Why: request_guard runs inside a ToolContext after the
    # engine has selected a concrete tool call. How: carry the active provider
    # tool_call_id and node_id on the context. Purpose: policy approvals can be
    # merged into the matching ToolCallCard.
    tool_call_id: str = ""
    node_id: str = ""
    platform_auth: dict[str, Any] | None = None

    def route_session_id(self) -> str:
        """Return the user-visible session for supervisor API calls."""
        # [Fork/Merge 2026-05-17] Why: session_id remains the runtime storage
        # session and may be branch_xxx. How: prefer parent_session_id and fall
        # back to session_id for old tasks and ordinary child tools. Purpose:
        # route outward-facing tool side effects to the durable conversation.
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

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        route_session_id = self.route_session_id()
        r = await self.http.post(
            f"{self.supervisor_url}/v1/ops/request",
            json={
                "session_id": route_session_id,
                "op": op,
                "parameters": {**parameters, "platform_auth": dict(self.platform_auth or {})},
                # [AutoC 2026-05-31] Why: approval_requested events need the tool
                # execution identity. How: send optional context fields only known
                # at runtime. Purpose: the supervisor can attach approval state to
                # the active tool card while preserving legacy empty values.
                "tool_call_id": self.tool_call_id or "",
                "node_id": self.node_id or getattr(self, "_node_id", "") or "",
                "task_id": self.task_id or "",
            },
        )
        r.raise_for_status()
        return r.json()

    async def check_cancelled(self) -> bool:
        # [硬取消] 显式 2s 超时，防止 supervisor 无响应时拖延 cancel 检测。
        # 工具层的 cancel 轮询间隔为 0.2s（execute_command / script tool），
        # 如果 HTTP 请求无超时，单次 check 可能阻塞远超轮询间隔。
        # 超时后 except 兜底返回 False，下次轮询再试。
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                # [Fork/Merge 2026-05-17] Why: session-level cancellation is a
                # user-visible operation. How: query the parent route session when
                # present. Purpose: tools without task_id still honor parent cancel.
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.route_session_id()}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

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
        import time as _time
        interval = float(poll_interval) if poll_interval is not None else float(self.approval_poll_interval_sec or 0.5)
        if interval <= 0:
            interval = 0.5
        _last_renew = 0.0  # monotonic timestamp of last lease renewal
        _RENEW_INTERVAL = 30.0  # renew lease every 30s during approval wait
        _RENEW_LEASE_SEC = 300.0  # request 5-minute lease to survive long waits
        # [AutoC] 审批等待超时兜底：无人审批时不再无限等待，避免任务挂到硬上限。
        _timeout_sec = self._resolve_approval_timeout_sec()
        _wait_started = _time.monotonic()
        while True:
            if await self.check_cancelled():
                return {"approval_id": approval_id, "status": "cancelled"}
            r = await self.http.get(f"{self.supervisor_url}/v1/approvals/{approval_id}")
            r.raise_for_status()
            approval = r.json()
            if approval.get("status") != "pending":
                return approval
            # [AutoC] 超时自动拒绝：先请求 supervisor 落库 deny，再返回 deny 结果，
            # 保证审批状态与任务结果一致，避免残留 pending。
            if _timeout_sec > 0 and (_time.monotonic() - _wait_started) >= _timeout_sec:
                _timeout_comment = f"approval timed out after {int(_timeout_sec)}s with no decision; auto-denied"
                try:
                    await self.http.post(
                        f"{self.supervisor_url}/v1/approvals/{approval_id}",
                        json={"decision": "deny", "comment": _timeout_comment},
                    )
                except Exception:
                    pass  # best-effort；即便落库失败也返回 deny，让工具侧终止等待
                return {
                    "approval_id": approval_id,
                    "status": "denied",
                    "decision": "deny",
                    "timed_out": True,
                    "comment": _timeout_comment,
                }
            # Renew task lease during approval wait to prevent zombie reaper timeout.
            # This is defense-in-depth alongside the heartbeat in engine/runner.py.
            _now_mono = _time.monotonic()
            if self.task_id and _now_mono - _last_renew >= _RENEW_INTERVAL:
                try:
                    await self.http.post(
                        f"{self.supervisor_url}/v1/tasks/{self.task_id}/renew_lease",
                        json={"worker_id": self.worker_id, "lease_sec": _RENEW_LEASE_SEC},
                    )
                except Exception:
                    pass  # best-effort; heartbeat is the primary renewal mechanism
                _last_renew = _now_mono
            await asyncio.sleep(interval)
