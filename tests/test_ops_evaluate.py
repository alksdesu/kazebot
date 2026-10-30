"""POST /v1/ops/evaluate：按任务身份做和 /v1/ops/request 同一套判定，但不创建审批、不记审计事件，只回结论和理由。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _approval_grant_fixture import (  # noqa: E402
    SENSITIVE_WRITE,
    Clock,
    grant_for,
    logged_events,
    make_state,
    qq_auth,
    start_task,
)
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_TOKEN = "test-ops-evaluate-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE_URL = "http://supervisor.test"


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("supervisor.state._now", clock)
    return clock


@pytest.fixture()
def state(tmp_path: Path, clock: Clock) -> SupervisorState:
    return make_state(tmp_path)


@pytest.fixture()
def app(tmp_path: Path, state: SupervisorState, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )


@pytest.fixture()
def api(app):
    def call(method: str, url: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http:
                return await http.request(method, url, headers=_AUTH if auth else {}, **kwargs)

        return asyncio.run(go())

    return call


Case = Callable[[SupervisorState], tuple[str, str | None, str, dict[str, Any]]]


def _internal(op: str, parameters: dict[str, Any]) -> Case:
    def build(state: SupervisorState) -> tuple[str, str | None, str, dict[str, Any]]:
        sid, task = start_task(state, auth=None, key="cli:default", channel="cli")
        return sid, task.task_id, op, dict(parameters)

    return build


def _qq(op: str, parameters: dict[str, Any], *, admin: bool = True, key: str | None = None, channel: str = "qq_private") -> Case:
    def build(state: SupervisorState) -> tuple[str, str | None, str, dict[str, Any]]:
        extra = {"key": key} if key else {}
        sid, task = start_task(state, auth=qq_auth(is_admin=admin), channel=channel, **extra)
        return sid, task.task_id, op, dict(parameters)

    return build


def _self_reported(op: str, parameters: dict[str, Any]) -> Case:
    def build(state: SupervisorState) -> tuple[str, str | None, str, dict[str, Any]]:
        sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")
        return sid, None, op, {**parameters, "platform_auth": qq_auth()}

    return build


CASES: dict[str, tuple[Case, SafetyLevel]] = {
    "policy auto": (_internal("read_file", {"path": "notes.txt"}), SafetyLevel.auto),
    "policy deny": (_internal("write_file", {"path": "config/policy.yaml"}), SafetyLevel.deny),
    "policy approval": (_internal("write_file", SENSITIVE_WRITE), SafetyLevel.approval_required),
    "read approval": (_internal("read_file", {"path": "data/notes.txt"}), SafetyLevel.approval_required),
    "external non-admin": (_qq("read_file", {"path": "notes.txt"}, admin=False), SafetyLevel.deny),
    "external admin plain command": (_qq("execute_command", {"command": "ls"}), SafetyLevel.auto),
    "external admin sensitive write": (_qq("write_file", SENSITIVE_WRITE), SafetyLevel.approval_required),
    "scheduler sensitive": (
        _qq("write_file", SENSITIVE_WRITE, key="scheduler:nightly", channel="internal"), SafetyLevel.deny,
    ),
    "scheduler plain": (
        _qq("execute_command", {"command": "ls"}, key="scheduler:nightly", channel="internal"), SafetyLevel.auto,
    ),
    "self-reported identity": (_self_reported("execute_command", {"command": "ls"}), SafetyLevel.auto),
}


class TestSameVerdictAsRequest:
    @pytest.mark.parametrize("name", list(CASES))
    def test_evaluate_answers_what_request_would(self, state: SupervisorState, name: str) -> None:
        build, expected = CASES[name]
        sid, task_id, op, parameters = build(state)
        before = len(state.approvals)

        evaluated = state.evaluate_operation(session_id=sid, op=op, parameters=dict(parameters), task_id=task_id)
        assert len(state.approvals) == before

        requested = state.request_operation(session_id=sid, op=op, parameters=dict(parameters), task_id=task_id)

        assert evaluated.safety_level == requested.safety_level == expected
        assert evaluated.reason == requested.reason
        assert evaluated.approval_id is None
        if expected == SafetyLevel.approval_required:
            assert requested.approval_id is not None and len(state.approvals) == before + 1
        else:
            assert requested.approval_id is None and len(state.approvals) == before

    def test_an_active_grant_is_reported_but_its_use_is_not_logged(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state)
        sid, task = start_task(state, auth=qq_auth())

        evaluated = state.evaluate_operation(session_id=sid, op="write_file", parameters=dict(SENSITIVE_WRITE), task_id=task.task_id)

        assert evaluated.safety_level == SafetyLevel.auto
        assert evaluated.reason.startswith(f"auto-approved: approval grant {grant.grant_id}")
        assert logged_events(tmp_path, "approval_grant_used") == []

        requested = state.request_operation(session_id=sid, op="write_file", parameters=dict(SENSITIVE_WRITE), task_id=task.task_id)

        assert requested.reason == evaluated.reason
        assert len(logged_events(tmp_path, "approval_grant_used")) == 1

    def test_a_scheduler_refusal_is_only_logged_when_requested(self, state: SupervisorState, tmp_path: Path) -> None:
        sid, task = start_task(state, auth=qq_auth(), key="scheduler:nightly", channel="internal")

        state.evaluate_operation(session_id=sid, op="write_file", parameters=dict(SENSITIVE_WRITE), task_id=task.task_id)
        assert logged_events(tmp_path, "scheduler_sensitive_op_denied") == []

        state.request_operation(session_id=sid, op="write_file", parameters=dict(SENSITIVE_WRITE), task_id=task.task_id)
        assert len(logged_events(tmp_path, "scheduler_sensitive_op_denied")) == 1

    def test_evaluating_twice_never_piles_up_approvals(self, state: SupervisorState) -> None:
        sid, task = start_task(state, auth=None, key="cli:default", channel="cli")

        for _ in range(3):
            out = state.evaluate_operation(session_id=sid, op="write_file", parameters=dict(SENSITIVE_WRITE), task_id=task.task_id)
            assert out.safety_level == SafetyLevel.approval_required

        assert state.approvals == {}
        assert logged_events(Path(state.workspace_root), "approval_requested") == []


class TestTheEndpoint:
    def test_it_answers_with_the_level_and_reason_only(self, api: Any, state: SupervisorState) -> None:
        sid, task = start_task(state, auth=None, key="cli:default", channel="cli")

        response = api("POST", "/v1/ops/evaluate", json={
            "session_id": sid, "op": "write_file", "parameters": dict(SENSITIVE_WRITE), "task_id": task.task_id,
        })

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"safety_level", "reason"}
        assert body["safety_level"] == "approval_required"
        assert state.approvals == {}

    def test_an_auto_answer(self, api: Any, state: SupervisorState) -> None:
        sid, task = start_task(state, auth=None, key="cli:default", channel="cli")

        response = api("POST", "/v1/ops/evaluate", json={
            "session_id": sid, "op": "read_file", "parameters": {"path": "notes.txt"}, "task_id": task.task_id,
        })

        assert response.json() == {"safety_level": "auto", "reason": "default policy"}

    def test_an_unknown_operation_is_rejected_like_request(self, api: Any, state: SupervisorState) -> None:
        sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")
        body = {"session_id": sid, "op": "format_disk", "parameters": {}}

        assert api("POST", "/v1/ops/evaluate", json=body).status_code == 422
        assert api("POST", "/v1/ops/request", json=body).status_code == 422

    def test_an_unauthenticated_caller_is_rejected(self, api: Any, state: SupervisorState, tmp_path: Path) -> None:
        sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")
        body = {"session_id": sid, "op": "read_file", "parameters": {"path": "notes.txt"}}

        # 观察期结束：这两个入口和别的端点一样走令牌门，没带令牌当场 401、不记旁路事件。
        assert api("POST", "/v1/ops/evaluate", json=body, auth=False).status_code == 401
        assert api("POST", "/v1/ops/request", json=body, auth=False).status_code == 401

        assert logged_events(tmp_path, "ops_request_unauthenticated") == []


class TestTheToolContext:
    @staticmethod
    def _capture(status: int = 200) -> tuple[list[httpx.Request], httpx.MockTransport]:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(status, json={"safety_level": "auto", "reason": "ok", "approval_id": None})

        return seen, httpx.MockTransport(handle)

    @staticmethod
    def _context(http: httpx.AsyncClient) -> ToolContext:
        return ToolContext(
            supervisor_url=_BASE_URL, session_id="branch-1", parent_session_id="main-1", run_id="run-1",
            worker_id="worker-1", workspace_root=Path("."), http=http, registry=None, task_id="task-1",
            tool_call_id="call-7", node_id="bootstrap.coder", platform_auth={"platform": "qq", "user_id": "1"},
        )

    def test_it_posts_the_same_body_as_request_op_to_the_evaluate_endpoint(self) -> None:
        seen, transport = self._capture()

        async def go() -> dict[str, Any]:
            async with httpx.AsyncClient(transport=transport) as http:
                ctx = self._context(http)
                evaluated = await ctx.evaluate_op("read_file", {"path": "a.txt"})
                await ctx.request_op("read_file", {"path": "a.txt"})
                return evaluated

        evaluated = asyncio.run(go())

        assert evaluated["safety_level"] == "auto"
        assert [request.url.path for request in seen] == ["/v1/ops/evaluate", "/v1/ops/request"]
        evaluate_body, request_body = (json.loads(request.content) for request in seen)
        assert evaluate_body == request_body == {
            "session_id": "main-1",
            "op": "read_file",
            "parameters": {"path": "a.txt", "platform_auth": {"platform": "qq", "user_id": "1"}},
            "tool_call_id": "call-7",
            "node_id": "bootstrap.coder",
            "task_id": "task-1",
        }

    def test_a_server_error_raises(self) -> None:
        _seen, transport = self._capture(status=500)

        async def go() -> None:
            async with httpx.AsyncClient(transport=transport) as http:
                await self._context(http).evaluate_op("read_file", {"path": "a.txt"})

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(go())


class TestEditEchoAgainstTheRealSupervisor:
    """apply_diff 对没读过的文件：读策略 auto 的回显原文，要审批的只给行号，而且一张审批卡都不弹。"""

    @staticmethod
    def _diff(app: Any, state: SupervisorState, workspace: Path, path: str, search: str, replace: str) -> dict[str, Any]:
        sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")

        async def go() -> dict[str, Any]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL, headers=_AUTH) as http:
                ctx = ToolContext(
                    supervisor_url=_BASE_URL, session_id=sid, run_id="run-1", worker_id="worker-1",
                    workspace_root=workspace, http=http, registry=None,
                )
                return await apply_diff({"path": path, "diffs": [{"search": search, "replace": replace}]}, ctx)

        return asyncio.run(go())

    def test_a_file_readable_without_approval_is_echoed(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

        result = self._diff(app, state, tmp_path, "notes.txt", "beta", "BETA")

        assert result["ok"] is True, result
        assert result["data"]["echoed"] is True
        assert "BETA" in result["data"]["result"]
        assert state.approvals == {}

    def test_a_file_whose_read_needs_approval_is_not_echoed_and_raises_no_approval(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        # SupervisorState 起来时已经建了 data/
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "notes.txt").write_text("token=abc\nother=1\n", encoding="utf-8")

        result = self._diff(app, state, tmp_path, "data/notes.txt", "other=1", "other=2")

        assert result["ok"] is True, result
        assert result["data"]["echoed"] is False
        assert "token=abc" not in result["data"]["result"]
        assert state.approvals == {}
        assert (tmp_path / "data" / "notes.txt").read_text(encoding="utf-8") == "token=abc\nother=2\n"
