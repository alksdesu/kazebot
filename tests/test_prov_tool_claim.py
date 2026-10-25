"""工具侧带着领取凭据和 supervisor 说话。

任务被回收、改由别处重领之后，还在跑的旧工具只能靠续租和查取消这两问得知自己出局。
凭据不带上，supervisor 就分不清问话的是现任还是前任。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox._common import request_guard  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_Handler = Callable[[httpx.Request], httpx.Response]


class _Supervisor:
    """记下每一问，按路径给出预设的回答。"""

    def __init__(self, *, renew: dict[str, Any], approvals: list[str] | None = None) -> None:
        self.renew = renew
        self.approvals = list(approvals or ["pending"])
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False})
        if path.endswith("/renew_lease"):
            return httpx.Response(200, json=self.renew)
        if path.startswith("/v1/approvals/"):
            status = self.approvals.pop(0) if len(self.approvals) > 1 else self.approvals[0]
            return httpx.Response(200, json={"approval_id": "ap-1", "status": status})
        if path == "/v1/ops/request":
            return httpx.Response(200, json={"safety_level": "approval_required", "approval_id": "ap-1"})
        return httpx.Response(200, json={})

    def requests_to(self, suffix: str) -> list[httpx.Request]:
        return [request for request in self.seen if request.url.path.endswith(suffix)]


def _ctx(http: httpx.AsyncClient, *, claim_id: str = "") -> ToolContext:
    return ToolContext(
        supervisor_url="http://sv",
        session_id="sess-1",
        run_id="task-1",
        worker_id="worker-1",
        workspace_root=_ROOT,
        http=http,
        registry=None,
        task_id="task-1",
        approval_poll_interval_sec=0.01,
        claim_id=claim_id,
    )


def _run(supervisor: _Supervisor, claim_id: str, body: Callable[[ToolContext], Any]) -> Any:
    async def run() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(supervisor)) as http:
            return await body(_ctx(http, claim_id=claim_id))

    return asyncio.run(run())


def test_the_context_carries_an_empty_claim_by_default() -> None:
    ctx = ToolContext(
        supervisor_url="http://sv", session_id="s", run_id="r", worker_id="w",
        workspace_root=_ROOT, http=None, registry=None,  # type: ignore[arg-type]
    )
    assert ctx.claim_id == ""


class TestTheCancelProbe:
    def test_it_names_the_claim_it_holds(self) -> None:
        supervisor = _Supervisor(renew={"ok": True})
        assert _run(supervisor, "claim-7", lambda ctx: ctx.check_cancelled()) is False
        [probe] = supervisor.requests_to("/cancelled")
        assert probe.url.params.get("claim_id") == "claim-7"

    def test_a_task_without_a_claim_asks_the_old_way(self) -> None:
        supervisor = _Supervisor(renew={"ok": True})
        _run(supervisor, "", lambda ctx: ctx.check_cancelled())
        [probe] = supervisor.requests_to("/cancelled")
        assert "claim_id" not in probe.url.params


class TestRenewalWhileWaitingForApproval:
    def test_the_renewal_carries_the_claim(self) -> None:
        supervisor = _Supervisor(renew={"ok": True}, approvals=["pending", "allowed"])
        result = _run(supervisor, "claim-7", lambda ctx: ctx.wait_for_approval("ap-1"))
        assert result["status"] == "allowed"
        renewal = json.loads(supervisor.requests_to("/renew_lease")[0].content)
        assert renewal == {"worker_id": "worker-1", "lease_sec": 300.0, "claim_id": "claim-7"}

    def test_a_refused_renewal_ends_the_wait_as_cancelled(self) -> None:
        # 被回收的任务再等下去，批下来也只会让一个出局的执行方去动手。
        supervisor = _Supervisor(renew={"ok": False, "reason": "claim_mismatch"})
        result = _run(supervisor, "claim-7", lambda ctx: ctx.wait_for_approval("ap-1"))
        assert result["status"] == "cancelled"

    def test_the_guard_reports_the_lost_claim_as_a_cancellation(self) -> None:
        supervisor = _Supervisor(renew={"ok": False, "reason": "not_running"})
        _op, err = _run(supervisor, "claim-7", lambda ctx: request_guard(ctx, "write_file", {"path": "a.txt"}))
        assert err is not None and err["cancelled"] is True

    def test_without_a_claim_a_refusal_is_not_taken_as_eviction(self) -> None:
        # 旧任务没有凭据，ok=False 说明不了是谁出了局，照旧等审批结果。
        supervisor = _Supervisor(renew={"ok": False}, approvals=["pending", "allowed"])
        result = _run(supervisor, "", lambda ctx: ctx.wait_for_approval("ap-1"))
        assert result["status"] == "allowed"
        assert "claim_id" not in json.loads(supervisor.requests_to("/renew_lease")[0].content)

    def test_an_unreadable_renewal_answer_keeps_waiting(self) -> None:
        supervisor = _Supervisor(renew={"unexpected": True}, approvals=["pending", "allowed"])
        result = _run(supervisor, "claim-7", lambda ctx: ctx.wait_for_approval("ap-1"))
        assert result["status"] == "allowed"
