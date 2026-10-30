"""完成回报、续租、取消查询都按领取代次（claim_id）认人。

迟到或重试的 complete 不能再改写已经挂起、恢复或被别人重新领走的任务；同一次领取的
重发只原样应答，不再路由第二遍，也就不会派出第二批子任务。
"""
from __future__ import annotations

import asyncio
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_store import CompletionOutcome  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_TOKEN = "sup-claims-token"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-claims"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _queue(state: SupervisorState, session_id: str, **input_data: Any) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.node",
            input_data={"task_context": {"conversation_key": "test:claims"}, **input_data},
            continuation={},
            source_inbound_seq=None,
        )


def _claim(state: SupervisorState, task: Task, worker_id: str = "worker-1") -> str:
    assigned = state.assign_next_task(worker_id=worker_id)
    assert assigned is not None and assigned["task_id"] == task.task_id
    assert assigned["claim_id"] == task.claim_id != ""
    return str(assigned["claim_id"])


def _dispatch_result(target: str = "child.node") -> dict[str, Any]:
    return {"action": "dispatch", "target_node": target, "dispatch_input": {"instruction": "go", "context_mode": "fresh"}}


def _children(state: SupervisorState, task: Task) -> list[Task]:
    return [t for t in state.tasks.values() if t.caller_task_id == task.task_id]


_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _client_call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    # 没显式给 headers 就带上管理员令牌：task 接口现在都要令牌
    kwargs.setdefault("headers", _AUTH)

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(_run())


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


class TestClaimIdentity:
    def test_every_claim_gets_a_fresh_claim_id_and_claim_time(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        assert task.claim_id == "" and task.claimed_at is None and task.queued_at is not None

        first = _claim(state, task)
        assert task.claimed_at is not None
        state.complete_task(task_id=task.task_id, worker_id="worker-1", result=_dispatch_result(), claim_id=first)
        child = _children(state, task)[0]
        child.status = TaskStatus.running
        child.worker_id = "worker-2"
        state.complete_task(task_id=child.task_id, worker_id="worker-2", result={"action": "finish", "result": {"text": "ok"}})

        assert task.status == TaskStatus.pending
        second = _claim(state, task)
        assert second != first

    def test_a_legacy_snapshot_without_claim_fields_still_loads(self) -> None:
        legacy = Task.model_validate({
            "task_id": "legacy", "session_id": "s", "kind": "node", "status": "suspended",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            "waiting_for_task_id": None,
        })
        assert legacy.claim_id == "" and legacy.completed_claim_id == ""
        assert legacy.queued_at is None and legacy.claimed_at is None
        assert legacy.waiting_for_batch_id is None


class TestCompleteByClaim:
    def test_the_current_claim_is_accepted(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)

        completion = state.complete_task(
            task_id=task.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "done"}}, claim_id=claim,
        )

        assert completion is not None and completion.outcome is CompletionOutcome.accepted
        assert task.status == TaskStatus.completed
        assert task.completed_claim_id == claim

    def test_a_retried_dispatch_complete_does_not_dispatch_twice(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)

        state.complete_task(task_id=task.task_id, worker_id="worker-1", result=_dispatch_result(), claim_id=claim)
        retried = state.complete_task(task_id=task.task_id, worker_id="worker-1", result=_dispatch_result(), claim_id=claim)

        assert retried is not None and retried.outcome is CompletionOutcome.duplicate
        assert task.status == TaskStatus.suspended
        assert len(_children(state, task)) == 1

    def test_a_late_complete_from_an_old_claim_cannot_overwrite_the_new_one(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        old_claim = _claim(state, task)
        task.lease_expires_at = None
        task.status = TaskStatus.pending
        new_claim = _claim(state, task, worker_id="worker-2")

        late = state.complete_task(
            task_id=task.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "stale"}}, claim_id=old_claim,
        )

        assert late is not None and late.outcome is CompletionOutcome.stale
        assert task.status == TaskStatus.running and task.claim_id == new_claim
        assert task.result == {}

    def test_a_legacy_complete_without_claim_requires_a_running_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)

        completion = state.complete_task(
            task_id=task.task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "x"}},
        )

        assert completion is not None and completion.outcome is CompletionOutcome.stale
        assert task.status == TaskStatus.pending

    def test_http_complete_maps_outcomes_to_status_codes(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)
        body = {"worker_id": "worker-1", "result": {"action": "finish", "result": {"text": "done"}}, "claim_id": claim}

        first = _client_call(app, "POST", f"/v1/tasks/{task.task_id}/complete", json=body)
        again = _client_call(app, "POST", f"/v1/tasks/{task.task_id}/complete", json=body)
        stale = _client_call(app, "POST", f"/v1/tasks/{task.task_id}/complete", json={**body, "claim_id": "other"})
        missing = _client_call(app, "POST", "/v1/tasks/no-such-task/complete", json=body)

        assert first.status_code == 200 and first.json() == {"ok": True, "task_id": task.task_id, "status": "completed"}
        assert again.status_code == 200 and again.json()["duplicate"] is True
        assert stale.status_code == 409 and stale.json()["detail"] == "stale_claim"
        assert missing.status_code == 404


class TestLeaseAndCancelProbe:
    def test_renew_reports_why_the_worker_must_stop(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)

        assert state.renew_lease(task.task_id, "worker-1", 120.0, claim_id=claim).ok
        assert state.renew_lease(task.task_id, "worker-1", 120.0, claim_id="other").reason == "stale_claim"
        assert state.renew_lease("no-such-task", "worker-1", 120.0, claim_id=claim).reason == "not_found"
        task.cancel_requested = True
        assert state.renew_lease(task.task_id, "worker-1", 120.0, claim_id=claim).reason == "cancelled"
        task.status = TaskStatus.failed
        assert state.renew_lease(task.task_id, "worker-1", 120.0, claim_id=claim).reason == "not_running"

    def test_http_renew_is_always_200_with_a_reason(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)

        ok = _client_call(app, "POST", f"/v1/tasks/{task.task_id}/renew_lease", json={"worker_id": "worker-1", "claim_id": claim})
        stale = _client_call(app, "POST", f"/v1/tasks/{task.task_id}/renew_lease", json={"worker_id": "worker-1", "claim_id": "x"})

        assert ok.status_code == 200 and ok.json() == {"ok": True, "reason": ""}
        assert stale.status_code == 200 and stale.json() == {"ok": False, "reason": "stale_claim"}

    def test_cancel_probe_covers_failed_tasks_and_foreign_claims(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:claims")
        task = _queue(state, sid)
        claim = _claim(state, task)

        def probe(claim_id: str | None) -> bool:
            params = {"claim_id": claim_id} if claim_id is not None else {}
            response = _client_call(app, "GET", f"/v1/tasks/{task.task_id}/cancelled", params=params)
            assert response.status_code == 200
            return bool(response.json()["cancelled"])

        assert probe(claim) is False
        assert probe(None) is False
        assert probe("someone-else") is True
        task.status = TaskStatus.failed
        assert probe(claim) is True
        assert state.is_task_cancelled("no-such-task") is True
