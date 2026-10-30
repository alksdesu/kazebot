"""supervisor 的令牌门：除了两个有意匿名的端点，全部 HTTP 路由和 WebSocket 都要管理员令牌。

收在一处 app 级依赖 require_admin 上，以后新加的端点漏了门，这里的全量扫描就会红。
"""
from __future__ import annotations

import asyncio
import sys
from collections import OrderedDict
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
from clonoth_runtime import session_event_token  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "api-auth-gate-token"
# 门放行但端点自己验的，和匿名的，不进「没令牌必 401」的扫描
_ANONYMOUS = {"/v1/health", "/v1/instance-directory", "/"}
_SELF_AUTH = {"/v1/files/{rel:path}", "/v1/admin/auth/check"}


@pytest.fixture
def app_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="gate"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )
    return app, state


def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_run())


def _concrete(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_http_route_without_a_token_is_rejected(app_state) -> None:
    app, _ = app_state
    leaks: list[tuple[str, str, int]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in _ANONYMOUS or route.path in _SELF_AUTH:
            continue
        method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
        resp = _request(app, method, _concrete(route.path))
        if resp.status_code != 401:
            leaks.append((method, route.path, resp.status_code))
    assert leaks == [], f"these routes answered without a token: {leaks}"


def test_a_valid_bearer_token_passes_the_gate(app_state) -> None:
    app, state = app_state
    sid = state.get_or_create_session(channel="web", conversation_key="web:gate")
    resp = _request(app, "GET", f"/v1/sessions/{sid}/events", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200


def test_a_query_token_passes_the_gate(app_state) -> None:
    app, _ = app_state
    resp = _request(app, "GET", "/v1/events?token=" + _TOKEN)
    assert resp.status_code == 200


def test_health_is_anonymous_but_minimal(app_state) -> None:
    app, _ = app_state
    anon = _request(app, "GET", "/v1/health")
    assert anon.status_code == 200
    body = anon.json()
    assert body["status"] == "ok" and "uptime_seconds" in body
    # 存活字段以外的只回给带了令牌的
    assert "workspace_root" not in body and "run_id" not in body and "started_at" not in body


def test_health_with_a_token_carries_the_full_fields(app_state) -> None:
    app, _ = app_state
    full = _request(app, "GET", "/v1/health", headers={"Authorization": f"Bearer {_TOKEN}"}).json()
    assert full["run_id"] and full["workspace_root"] and full["started_at"]


def test_health_with_a_wrong_token_is_counted(app_state) -> None:
    app, _ = app_state
    # 出示了错令牌照常计退避，不走匿名
    resp = _request(app, "GET", "/v1/health", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert admin_api._auth_failures


def test_instance_directory_is_anonymous(app_state) -> None:
    app, _ = app_state
    resp = _request(app, "GET", "/v1/instance-directory")
    assert resp.status_code == 200
    body = resp.json()
    # 登录前列实例要用的字段，别的都不回
    assert set(body) == {"current_path", "instances"}
    for row in body["instances"]:
        assert set(row) == {"label", "path"}


def test_the_api_docs_are_disabled(app_state) -> None:
    app, _ = app_state
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert _request(app, "GET", path).status_code == 404


def test_the_admin_auth_check_maps_backoff_to_401(app_state) -> None:
    app, _ = app_state
    for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 1):
        _request(app, "GET", "/v1/admin/auth/check", headers={"Authorization": "Bearer nope"})
    # 退避期间登录页拿到的也是 401，不是 429
    assert _request(app, "GET", "/v1/admin/auth/check", headers={"Authorization": f"Bearer {_TOKEN}"}).status_code == 401


def test_a_files_refusal_without_a_token_keeps_the_security_headers(app_state) -> None:
    app, _ = app_state
    resp = _request(app, "GET", "/v1/files/data/attachments/x/none.png")
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "sandbox"


def test_reading_an_approval_without_a_token_is_not_a_wait(app_state) -> None:
    # 没令牌读审批被挡在门外，不该算「有人在等这条审批」
    app, state = app_state
    resp = _request(app, "GET", "/v1/approvals/whatever")
    assert resp.status_code == 401


class TestScriptEventCredential:
    def _post(self, app, sid: str, body: dict, token: str) -> httpx.Response:
        return _request(
            app, "POST", f"/v1/sessions/{sid}/events",
            json=body, headers={"X-Clonoth-Event-Token": token},
        )

    def test_a_session_event_token_posts_only_intermediate_replies(self, app_state) -> None:
        app, state = app_state
        sid = state.get_or_create_session(channel="web", conversation_key="web:evt")
        token = session_event_token(_TOKEN, sid)

        ok = self._post(app, sid, {"type": "intermediate_reply", "payload": {"text": "hi"}}, token)
        assert ok.status_code == 200

    def test_it_refuses_other_event_types(self, app_state) -> None:
        app, state = app_state
        sid = state.get_or_create_session(channel="web", conversation_key="web:evt")
        token = session_event_token(_TOKEN, sid)

        bad = self._post(app, sid, {"type": "outbound_message", "payload": {"text": "x"}}, token)
        assert bad.status_code == 403

    def test_it_is_scoped_to_its_own_session(self, app_state) -> None:
        app, state = app_state
        sid = state.get_or_create_session(channel="web", conversation_key="web:evt")
        other = state.get_or_create_session(channel="web", conversation_key="web:other")
        token = session_event_token(_TOKEN, sid)

        wrong = self._post(app, other, {"type": "intermediate_reply", "payload": {"text": "x"}}, token)
        assert wrong.status_code == 401

    def test_it_does_not_open_the_admin_surface(self, app_state) -> None:
        app, state = app_state
        sid = state.get_or_create_session(channel="web", conversation_key="web:evt")
        token = session_event_token(_TOKEN, sid)
        # 事件凭据不是管理员令牌：拿它当 Bearer 读事件流会被挡
        resp = _request(app, "GET", "/v1/events", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
