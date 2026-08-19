"""Tests for the admin active-task summary endpoint.

[AutoC 2026-06-04] These tests are written before the implementation because the
System dashboard needs a small task-summary API rather than the full Task payloads.
They pin authentication, field omission, status filtering, and newest-first order.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Why: tests run from a checkout that is not installed as a package. How: prepend
# the repository root. Purpose: exercise the edited supervisor modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    """Create a temporary SupervisorState for admin API tests."""
    # Why: the endpoint reads the real in-memory task registry. How: instantiate
    # SupervisorState with temporary persistence. Purpose: avoid touching live data.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-active-tasks")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _task(
    task_id: str,
    *,
    status: TaskStatus,
    kind: TaskKind = TaskKind.node,
    created_at: datetime,
    updated_at: datetime | None = None,
    worker_id: str | None = None,
    caller_task_id: str | None = None,
    input_payload: dict[str, object] | None = None,
    cancel_requested: bool = False,
) -> Task:
    """Build a Task containing large fields that the summary API must omit."""
    # Why: the dashboard only needs identifying metadata. How: populate input,
    # continuation, and result with sentinel values. Purpose: prove the API does
    # not return heavy or sensitive task payloads to the task-monitor modal.
    return Task(
        task_id=task_id,
        session_id=f"session-{task_id}",
        kind=kind,
        node_id=f"node-{task_id}",
        # [AutoC 2026-06-04] Why: the active-task modal may show a short input
        # preview but must never receive the full task input. How: allow each
        # fixture to provide text or instruction while preserving heavy default
        # payloads for omission checks. Purpose: the test describes the exact
        # summary-only contract before the API implementation changes.
        input=input_payload or {"large_prompt": "x" * 512},
        continuation={"history": ["hidden"]},
        result={"secret": "must-not-leak"},
        status=status,
        cancel_requested=cancel_requested,
        worker_id=worker_id,
        caller_task_id=caller_task_id,
        created_at=created_at,
        updated_at=updated_at or created_at,
    )


def test_admin_active_tasks_returns_summaries_sorted_and_requires_auth(tmp_path: Path, monkeypatch) -> None:
    """The active-task endpoint should expose only running, pending, and suspended summaries."""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(admin_api, "_admin_token", "")

    state = _make_state(tmp_path)
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    now = datetime(2026, 6, 4, 2, 58, tzinfo=timezone.utc)
    with state._lock:
        state.tasks = {
            "running-old": _task(
                "running-old",
                status=TaskStatus.running,
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(seconds=5),
                worker_id="worker-running-123456",
                input_payload={"text": "run " + "x" * 260, "secret_input": "must-not-leak-input"},
            ),
            "pending-new": _task(
                "pending-new",
                status=TaskStatus.pending,
                created_at=now - timedelta(minutes=1),
                caller_task_id="caller-1",
                input_payload={"instruction": "prepare deployment window"},
                cancel_requested=True,
            ),
            "suspended-mid": _task(
                "suspended-mid",
                status=TaskStatus.suspended,
                kind=TaskKind.tool,
                created_at=now - timedelta(minutes=3),
                input_payload={"text": "inspect paused tool"},
            ),
            "completed-hidden": _task(
                "completed-hidden",
                status=TaskStatus.completed,
                created_at=now,
                input_payload={"text": "must-not-leak-completed-input"},
            ),
        }

    async def _exercise_api() -> tuple[httpx.Response, httpx.Response]:
        # Why: Starlette TestClient is not used in this project. How: drive the
        # FastAPI app through httpx ASGITransport. Purpose: test real routing,
        # authentication, and serialization without opening a network port.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauth = await client.get("/v1/admin/tasks/active")
            authed = await client.get(
                "/v1/admin/tasks/active",
                headers={"Authorization": "Bearer test-admin-token"},
            )
            return unauth, authed

    unauth, authed = asyncio.run(_exercise_api())

    assert unauth.status_code == 401
    assert authed.status_code == 200
    payload = authed.json()
    assert [item["task_id"] for item in payload] == ["pending-new", "suspended-mid", "running-old"]
    assert {item["status"] for item in payload} == {"pending", "suspended", "running"}
    assert payload[1]["kind"] == "tool"
    assert payload[2]["worker_id"] == "worker-running-123456"
    assert payload[0]["caller_task_id"] == "caller-1"
    # [AutoC 2026-06-04] Why: operators need enough context to identify work
    # without downloading the full Task.input. How: assert text/instruction summaries
    # and cancellation flags only. Purpose: future endpoint changes cannot leak
    # complete inputs or drop the cancel state used by the modal.
    assert payload[0]["input_summary"] == "prepare deployment window"
    assert payload[0]["cancel_requested"] is True
    assert payload[1]["input_summary"] == "inspect paused tool"
    assert payload[1]["cancel_requested"] is False
    assert payload[2]["input_summary"] == ("run " + "x" * 260)[:200]
    assert len(payload[2]["input_summary"]) == 200
    for item in payload:
        assert set(item) == {
            "task_id",
            "session_id",
            "node_id",
            "status",
            "kind",
            "created_at",
            "updated_at",
            "worker_id",
            "caller_task_id",
            "input_summary",
            "cancel_requested",
            # [AutoC 2026-07-09] active-task 摘要新增实时阶段展示字段，
            # 由 event WS 推导写入 Task.current_phase/current_detail。
            "current_phase",
            "current_detail",
        }
        assert "large_prompt" not in str(item)
        assert "secret_input" not in str(item)
        assert "must-not-leak" not in str(item)


def test_generated_admin_token_is_never_printed(monkeypatch, capsys) -> None:
    """Auto-generated credentials must not be exposed through startup stdout."""
    secret = "generated-admin-token-must-stay-secret"
    monkeypatch.delenv("CLONOTH_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api.secrets, "token_urlsafe", lambda _length: secret)

    assert admin_api.get_admin_token() == secret

    output = capsys.readouterr().out
    assert secret not in output
    assert "内容不输出" in output


def test_create_app_writes_but_does_not_print_admin_token(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Startup may persist the token for workers, but must never log its value."""
    secret = "configured-admin-token-must-stay-secret"
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", secret)
    monkeypatch.setattr(admin_api, "_admin_token", "")

    state = _make_state(tmp_path)
    create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )

    output = capsys.readouterr().out
    assert secret not in output
    assert "管理 Token 已初始化（内容不输出）" in output
    assert (tmp_path / "data" / ".admin_token").read_text(encoding="utf-8") == secret
