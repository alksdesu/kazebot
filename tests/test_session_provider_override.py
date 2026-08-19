"""Regression tests for session-level provider overrides.

[AutoC 2026-06-01] These tests are written before the implementation because
session routing already has durable per-session metadata, while provider
selection still only used global and node-level values. The tests pin storage,
admin API behavior, and provider resolution priority.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Why: these tests run from a checkout that is not installed as a package. How:
# prepend the repository root. Purpose: import and exercise local edited modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from engine.model import resolve_provider  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.runner import _fetch_session_provider_override  # noqa: E402
from supervisor._helpers import SessionInfo  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.session_store import SessionStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    """Create a supervisor state backed by temporary persistence files."""
    # Why: provider_override must survive the same file-backed path used in
    # production. How: instantiate the real SupervisorState with tmp_path data.
    # Purpose: cover SessionInfo, SessionStore, and API state methods together.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-provider-override")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def test_session_store_persists_and_restores_provider_override(tmp_path: Path) -> None:
    """SessionStore should load old rows safely and restore new provider overrides."""
    store_path = tmp_path / "data" / "sessions.json"
    created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = SessionStore(store_path)

    store.on_session_created(
        SessionInfo(
            session_id="session-new",
            channel="discord",
            conversation_key="discord:new",
            created_at=created_at,
            updated_at=created_at,
            provider_override={"provider": "anthropic", "model": "claude-test"},
        )
    )
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert raw["session-new"]["provider_override"] == {
        "provider": "anthropic",
        "model": "claude-test",
    }

    raw["session-old"] = {
        "session_id": "session-old",
        "channel": "discord",
        "conversation_key": "discord:old",
        "created_at": created_at.isoformat(),
        "reset": False,
    }
    store_path.write_text(json.dumps(raw), encoding="utf-8")

    restored, conv_map, child_map, parent_children = SessionStore(store_path).load()

    # Why: existing deployments have sessions.json rows without provider_override.
    # How: missing or non-dict values become an empty dict. Purpose: the schema
    # extension does not break restart recovery for old sessions.
    assert restored["session-new"].provider_override == {
        "provider": "anthropic",
        "model": "claude-test",
    }
    assert restored["session-old"].provider_override == {}
    assert conv_map["discord:new"] == "session-new"
    assert child_map == {}
    assert parent_children == {}


def test_resolve_provider_applies_session_override_above_node_values() -> None:
    """Session provider_override should have higher priority than node and global values."""
    node = Node(
        id="node-1",
        type="ai",
        provider="anthropic",
        model="node-model",
        api_key="node-key",
        base_url="https://node.example/v1",
    )

    resolved = resolve_provider(
        Path("/tmp/workspace"),
        node,
        "global-model",
        session_override={
            "provider": "gemini",
            "model": "session-model",
            "api_key": "session-key",
            "base_url": "https://session.example/v1",
        },
    )

    # Why: users may switch a whole conversation to another provider without
    # editing node YAML. How: resolve_provider overlays session_override last.
    # Purpose: the active session controls provider, model, and credentials.
    assert resolved.provider_type == "gemini"
    assert resolved.model == "session-model"
    assert resolved.api_key == "session-key"
    assert resolved.base_url == "https://session.example/v1"

    partial = resolve_provider(
        Path("/tmp/workspace"),
        node,
        "global-model",
        session_override={"model": "session-only-model"},
    )
    assert partial.provider_type == "anthropic"
    assert partial.model == "session-only-model"
    assert partial.api_key == "node-key"
    assert partial.base_url == "https://node.example/v1"


def test_provider_override_admin_api_requires_auth_and_persists(tmp_path: Path, monkeypatch) -> None:
    """Admin endpoints should get, set, and clear provider_override for a session."""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(admin_api, "_admin_token", "")

    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="discord", conversation_key="discord:provider")
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )

    async def _exercise_api() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        # Why: the installed Starlette TestClient is not used elsewhere in this
        # project. How: drive the FastAPI app with httpx ASGITransport. Purpose:
        # test real routing and auth without opening a network port.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauth = await client.get(f"/v1/sessions/{session_id}/provider_override")
            headers = {"Authorization": "Bearer test-admin-token"}
            put_resp = await client.put(
                f"/v1/sessions/{session_id}/provider_override",
                json={"provider": "anthropic", "model": "claude-test", "api_key": "secret-test-key"},
                headers=headers,
            )
            get_resp = await client.get(f"/v1/sessions/{session_id}/provider_override", headers=headers)
            delete_resp = await client.delete(f"/v1/sessions/{session_id}/provider_override", headers=headers)
            return unauth, put_resp, get_resp, delete_resp

    unauth, put_resp, get_resp, delete_resp = asyncio.run(_exercise_api())

    assert unauth.status_code == 401
    assert put_resp.status_code == 200
    assert put_resp.json() == {"provider": "anthropic", "model": "claude-test", "api_key": "secret-test-key"}
    assert get_resp.json() == {"provider": "anthropic", "model": "claude-test", "api_key": "secret-test-key"}
    assert delete_resp.json() == {}

    raw = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))
    assert raw[session_id]["provider_override"] == {}
    assert state.sessions[session_id].provider_override == {}
    events_text = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    # Why: provider_override can contain credentials. How: the endpoint returns
    # and persists the exact dict, but the audit event stores only safe metadata.
    # Purpose: API keys do not leak into append-only event logs.
    assert "secret-test-key" not in events_text


def test_runner_fetch_session_provider_override_uses_supervisor_endpoint() -> None:
    """The runner helper should read a session override dict and tolerate misses."""
    seen_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/missing/provider_override"):
            return httpx.Response(404, json={"detail": "session not found"})
        return httpx.Response(200, json={"provider": "gemini", "model": "gemini-test"})

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://supervisor") as client:
            found = await _fetch_session_provider_override(client, "http://supervisor", "session-1")
            missing = await _fetch_session_provider_override(client, "http://supervisor", "missing")
            return found, missing

    found, missing = asyncio.run(_exercise())

    assert found == {"provider": "gemini", "model": "gemini-test"}
    assert missing == {}
    assert seen_paths == [
        "/v1/sessions/session-1/provider_override",
        "/v1/sessions/missing/provider_override",
    ]
