"""审批和 supervisor 对账：别处定案、超时、任务结束都收起本地卡片；重启和重连后把待审批补回来。

supervisor 对已定案的审批照样回 200，决定只有在回执状态和它一致时才算生效。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from adapters._shared import approvals as approval_book  # noqa: E402
from clonoth_sdk import (  # noqa: E402
    APPROVAL_ALLOWED,
    APPROVAL_DENIED,
    APPROVAL_EXPIRED,
    APPROVAL_MISSING,
    ApprovalDecision,
    ClonothClient,
    TaskOrigin,
)
from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.state import SessionState  # noqa: E402
from clonoth_sdk.types import Event  # noqa: E402

_TIMEOUT_COMMENT = "approval timed out after 300s with no decision; auto-denied"


class _Supervisor:
    """对账要用到的那几个读接口。"""

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.origins: dict[str, TaskOrigin] = {}
        self.listed = 0

    async def list_pending_approvals(self) -> list[dict[str, Any]]:
        self.listed += 1
        return [dict(item) for item in self.pending]

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return self.approvals.get(approval_id)

    async def get_task_origin(self, task_id: str) -> TaskOrigin | None:
        return self.origins.get(task_id)

    async def approve(self, approval_id: str, *, decision: str, comment: str = "") -> bool:
        raise AssertionError("这组用例都不该自动放行")


class _Cards:
    def __init__(self, *, fail_first_show: bool = False) -> None:
        self.shown: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str, str]] = []
        self._fail_next = fail_first_show

    async def show_approval_ui(self, approval_id: str, operation: str, details: dict[str, Any], *,
                               conversation_key: str = "", session_id: str = "") -> None:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("NapCat 掉线")
        self.shown.append((approval_id, conversation_key))

    async def close_approval_ui(self, approval_id: str, *, status: str, comment: str = "") -> None:
        self.closed.append((approval_id, status, comment))

    async def finalize_child_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def delete_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def edit_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture()
def wired(tmp_path: Path):
    supervisor = _Supervisor()
    cards = _Cards()
    router = EventRouter(
        supervisor, SessionState(), cards,
        BotConfig(base_url="http://127.0.0.1:1", workspace_root=tmp_path, conversation_key_prefix="chat",
                  auto_approve_internal=False),
    )
    yield router, supervisor, cards
    router._outbound_store.close()


def _event(event_type: str, payload: dict[str, Any], *, seq: int = 1, session_id: str = "s-room") -> Event:
    return Event(
        seq=seq, event_id=f"evt-{seq}", ts="", run_id="run", session_id=session_id,
        component="supervisor", type=event_type, payload=payload,
    )


def _requested(approval_id: str, task_id: str = "t1", **extra: Any) -> Event:
    return _event("approval_requested", {
        "approval_id": approval_id, "task_id": task_id, "conversation_key": "chat:room",
        "operation": "run_command", "details": {"tool_name": "run_command", "command": "rm -rf /tmp/x"}, **extra,
    })


def _show(router: EventRouter, *approval_ids: str, task_id: str = "t1") -> None:
    for approval_id in approval_ids:
        asyncio.run(router._dispatch(_requested(approval_id, task_id)))


class TestClosingCards:
    def test_a_decision_made_elsewhere_closes_the_local_card_once(self, wired) -> None:
        router, _, cards = wired
        _show(router, "a1")

        decided = _event("approval_decided", {"approval_id": "a1", "decision": "deny", "comment": "控制台拒了"})
        asyncio.run(router._dispatch(decided))
        asyncio.run(router._dispatch(decided))

        assert cards.shown == [("a1", "chat:room")]
        assert cards.closed == [("a1", APPROVAL_DENIED, "控制台拒了")]

    def test_a_timeout_closes_the_card_with_its_comment(self, wired) -> None:
        router, _, cards = wired
        _show(router, "a1")

        asyncio.run(router._dispatch(_event("approval_decided", {
            "approval_id": "a1", "decision": "deny", "comment": _TIMEOUT_COMMENT,
        })))

        assert cards.closed == [("a1", APPROVAL_DENIED, _TIMEOUT_COMMENT)]

    @pytest.mark.parametrize("event_type,reason", [("task_cancelled", "任务已取消"), ("task_completed", "任务已结束")])
    def test_ending_the_task_expires_only_its_own_cards(self, wired, event_type: str, reason: str) -> None:
        router, _, cards = wired
        _show(router, "a1", task_id="t1")
        _show(router, "a2", task_id="t2")

        asyncio.run(router._dispatch(_event(event_type, {"task_id": "t1", "node_id": "worker", "status": "failed"})))

        assert cards.closed == [("a1", APPROVAL_EXPIRED, reason)]

    def test_approvals_this_adapter_never_showed_are_left_alone(self, wired) -> None:
        router, _, cards = wired

        asyncio.run(router._dispatch(_event("approval_decided", {"approval_id": "web-1", "decision": "allow"})))

        assert cards.closed == []


class TestReconcile:
    def test_cards_settled_while_disconnected_are_closed(self, wired) -> None:
        router, supervisor, cards = wired
        _show(router, "a1", "a2")
        supervisor.pending = [{"approval_id": "a2", "task_id": "t1", "conversation_key": "chat:room"}]
        supervisor.approvals["a1"] = {"approval_id": "a1", "status": "allowed", "comment": "ok"}

        asyncio.run(router._reconcile_approvals())

        assert cards.closed == [("a1", APPROVAL_ALLOWED, "ok")]
        assert cards.shown == [("a1", "chat:room"), ("a2", "chat:room")]

    def test_a_vanished_approval_counts_as_expired(self, wired) -> None:
        router, _, cards = wired
        _show(router, "a1")

        asyncio.run(router._reconcile_approvals())

        assert cards.closed == [("a1", APPROVAL_EXPIRED, "")]

    def test_a_restart_brings_pending_cards_back_through_the_task_origin(self, wired) -> None:
        router, supervisor, cards = wired
        supervisor.pending = [{
            "approval_id": "a9", "task_id": "t9", "session_id": "branch_lost_after_restart",
            "operation": "run_command", "details": {"tool_name": "run_command"},
        }]
        supervisor.origins["t9"] = TaskOrigin(task_id="t9", origin_task_id="t0", conversation_key="chat:room", channel="chat")

        asyncio.run(router._reconcile_approvals())
        asyncio.run(router._reconcile_approvals())

        assert cards.shown == [("a9", "chat:room")]

    def test_pending_approvals_of_other_adapters_are_not_shown(self, wired) -> None:
        router, supervisor, cards = wired
        supervisor.pending = [{"approval_id": "w1", "task_id": "tw", "details": {"tool_name": "run_command"}}]
        supervisor.origins["tw"] = TaskOrigin(task_id="tw", origin_task_id="tw", conversation_key="web:1", channel="web")

        asyncio.run(router._reconcile_approvals())

        assert cards.shown == []

    def test_a_card_that_failed_to_show_is_tried_again_next_time(self, tmp_path: Path) -> None:
        supervisor = _Supervisor()
        cards = _Cards(fail_first_show=True)
        router = EventRouter(
            supervisor, SessionState(), cards,
            BotConfig(base_url="http://127.0.0.1:1", workspace_root=tmp_path, conversation_key_prefix="chat",
                      auto_approve_internal=False),
        )
        try:
            supervisor.pending = [{"approval_id": "a1", "task_id": "t1", "conversation_key": "chat:room",
                                   "details": {"tool_name": "run_command"}}]

            asyncio.run(router._reconcile_approvals())
            asyncio.run(router._reconcile_approvals())

            assert cards.shown == [("a1", "chat:room")]
        finally:
            router._outbound_store.close()

    def test_the_first_frame_after_connecting_triggers_a_reconcile(self, wired) -> None:
        router, supervisor, _ = wired

        class _Replay:
            async def ws_connect(self, last_seq=None):
                yield {"type": "outbound_checkpoint", "seq": 0}

        router._client = SimpleNamespace(
            ws_connect=_Replay().ws_connect, list_pending_approvals=supervisor.list_pending_approvals,
            get_approval=supervisor.get_approval, get_task_origin=supervisor.get_task_origin,
        )

        async def exercise() -> None:
            router._running = True
            with pytest.raises(ConnectionError):
                await router._run_ws()
            task = router._approval_reconcile_task
            assert task is not None
            await task

        asyncio.run(exercise())

        assert supervisor.listed == 1


class TestDecisionReceipts:
    @staticmethod
    def _client(handler) -> ClonothClient:
        client = ClonothClient("http://supervisor.test", admin_token="token")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer token"})
        return client

    @pytest.mark.parametrize("decision,status,effective", [
        ("allow", "allowed", True), ("deny", "denied", True),
        ("allow", "denied", False), ("deny", "allowed", False), ("allow", "pending", False),
    ])
    def test_a_decision_is_effective_only_when_the_status_matches(self, decision: str, status: str, effective: bool) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/approvals/a-1"
            return httpx.Response(200, json={"approval_id": "a-1", "status": status, "comment": "c"})

        client = self._client(handler)

        async def exercise() -> tuple[ApprovalDecision, bool]:
            try:
                return await client.decide_approval("a-1", decision=decision), await client.approve("a-1", decision=decision)
            finally:
                await client.close()

        result, approved = asyncio.run(exercise())

        assert result.status == status and result.comment == "c"
        assert result.effective is effective
        assert approved is effective

    def test_an_unknown_approval_is_reported_as_missing(self) -> None:
        client = self._client(lambda request: httpx.Response(404, json={"detail": "not found"}))

        async def exercise() -> ApprovalDecision:
            try:
                return await client.decide_approval("gone", decision="allow")
            finally:
                await client.close()

        result = asyncio.run(exercise())

        assert result.status == APPROVAL_MISSING and result.effective is False

    def test_other_http_errors_are_raised_not_swallowed(self) -> None:
        client = self._client(lambda request: httpx.Response(500))

        async def exercise() -> None:
            try:
                await client.decide_approval("a-1", decision="allow")
            finally:
                await client.close()

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(exercise())

    def test_unsafe_ids_never_reach_the_url(self) -> None:
        client = self._client(lambda request: httpx.Response(200, json={}))

        async def exercise() -> None:
            try:
                await client.decide_approval("../admin/state", decision="allow")
            finally:
                await client.close()

        with pytest.raises(ValueError):
            asyncio.run(exercise())


class TestQQBook:
    @pytest.fixture()
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        module = load_runtime(monkeypatch, tmp_path)
        set_live_config(module, admin_users=frozenset({10001}))
        return module

    @pytest.mark.parametrize("status,comment,decision", [
        (APPROVAL_ALLOWED, "", approval_book.DECISION_ALLOW),
        (APPROVAL_DENIED, "denied in console", approval_book.DECISION_DENY),
        (APPROVAL_DENIED, _TIMEOUT_COMMENT, approval_book.DECISION_EXPIRED),
        (APPROVAL_EXPIRED, "任务已取消", approval_book.DECISION_EXPIRED),
        (APPROVAL_MISSING, "", approval_book.DECISION_EXPIRED),
    ])
    def test_closing_from_the_supervisor_settles_the_book(self, runtime: Any, status: str, comment: str, decision: str) -> None:
        book = runtime.state._adapter.approvals
        book.pending["a1"] = {"operation": "run_command", "created_at": time.time()}

        asyncio.run(runtime.callbacks.TangQiuCallbacks().close_approval_ui("a1", status=status, comment=comment))

        assert "a1" not in book.pending
        assert book.settled["a1"]["decision"] == decision

    def test_a_local_decision_keeps_who_made_it(self, runtime: Any) -> None:
        book = runtime.state._adapter.approvals
        book.settle("a1", decision=approval_book.DECISION_ALLOW, admin_id=10001, operation="run_command")

        asyncio.run(runtime.callbacks.TangQiuCallbacks().close_approval_ui("a1", status=APPROVAL_ALLOWED))

        assert book.settled["a1"]["admin_id"] == "10001"

    @pytest.mark.parametrize("word", ["ok", "OK", "yes", "y", "no", "n", "好", "嗯"])
    def test_casual_words_are_not_approval_verbs(self, word: str) -> None:
        assert approval_book.parse_reply_verb(word) is None

    @pytest.mark.parametrize("word,decision", [("同意", "allow"), ("批准", "allow"), ("拒绝", "deny"), ("驳回", "deny")])
    def test_explicit_approval_words_still_work(self, word: str, decision: str) -> None:
        assert approval_book.parse_reply_verb(word) == decision
