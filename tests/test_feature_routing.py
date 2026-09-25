from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.outbound_store import OutboundStore
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import Event
from clonoth_sdk.feature_paths import validate_feature_path
from engine.conversation_routing import concise_text, routing_meta, topic_history
from supervisor.feature_auth import resolve_actor


@pytest.mark.parametrize("path", ["/v1/materials/../../admin/config", "/v1/materials/%2e%2e/admin", "/v1/materials\\..\\admin", "/v1/materialsevil/status", "/v1/materials?path=/admin", "/v1/operations/status"])
def test_model_feature_paths_cannot_escape_authorized_domain(path):
    with pytest.raises(ValueError):
        validate_feature_path(path)


def test_feature_paths_accept_real_versioned_endpoints():
    validate_feature_path("/v1/materials/versions/v_123/preview/1")
    validate_feature_path("/v1/execution/plans/P123/resolve-step")
    validate_feature_path("/v1/operations/diagnostics", operations=True)
    validate_feature_path("/v1/operations/instances", operations=True)
    validate_feature_path("/v1/community/notifications/activity%3AA123%3Aconfirm/ack")


def test_session_reset_clears_topic_state_only_for_current_route(tmp_path):
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState
    state = SupervisorState(workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="test"), policy=PolicyEngine(workspace_root=tmp_path))
    cleared = []
    state.community = SimpleNamespace(clear_context=cleared.append)
    scope = "qq_group:one"
    old = state.get_or_create_session(channel="qq_group", conversation_key=scope)
    state.conversation_map[scope] = "newer-session"
    state.reset_session(session_id=old)
    assert cleared == []
    state.conversation_map.pop(scope)
    current = state.get_or_create_session(channel="qq_group", conversation_key=scope)
    state.reset_session(session_id=current)
    assert cleared == [scope]


def test_topic_history_keeps_complete_task_tool_pairs_and_shared_legacy():
    def message(role, task, topic, **extra):
        return {"role": role, "content": task, "_meta": {"source_task_id": task, "topic_id": topic}, **extra}
    history = [
        message("user", "a", "books"),
        message("assistant", "a", "books", tool_calls=[{"id": "call-a"}]),
        message("user", "b", "games"),
        message("tool", "a", "", tool_call_id="call-a"),
        message("assistant", "b", "games"),
        {"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"},
    ]
    selected = topic_history(history, "books")
    assert selected == [history[index] for index in (0, 1, 3, 5, 6)]
    assert topic_history(history, "") is history
    assert len(history) == 7


def test_topic_history_limits_legacy_by_whole_turn():
    history = [{"role": role, "content": str(index)} for index in range(8) for role in ("user", "assistant")]
    assert topic_history(history, "new") == history[-6:]
    assert len(concise_text("很长的回应" * 100)) <= 240
    assert routing_meta({"route_hints": {"topic_id": "books", "source_message_refs": ["123"], "untrusted": "ignored"}}) == {"topic_id": "books", "source_message_refs": ["123"]}


def request(headers):
    return Request({"type": "http", "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()], "query_string": b""})


def test_feature_actor_uses_active_task_scope_and_bound_group_role(tmp_path, monkeypatch):
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda _: None)
    context = {"conversation_key": "qq_group:one", "channel": "qq_group", "platform_auth": {"user_id": "42", "group_role": "admin", "group_scope": "qq_group:other"}}
    task = SimpleNamespace(input={"task_context": context}, cancel_requested=False)
    state = SimpleNamespace(workspace_root=tmp_path, tasks={"task": task}, _lock=threading.RLock(), _task_terminal=lambda _: False)
    actor = resolve_actor(request({"X-Clonoth-Task-Id": "task"}), state)
    assert actor.scope == "qq_group:one" and actor.role == "member" and not actor.interactive
    with pytest.raises(HTTPException):
        actor.require_interaction()
    with pytest.raises(HTTPException):
        resolve_actor(request({"X-Clonoth-Task-Id": "task", "X-Clonoth-Scope": "qq_group:other"}), state)
    context["platform_auth"]["group_scope"] = actor.scope
    assert resolve_actor(request({"X-Clonoth-Task-Id": "task"}), state).role == "admin"
    context["channel"] = "qq_private"
    assert resolve_actor(request({"X-Clonoth-Task-Id": "task"}), state).role == "member"


def outbound(seq=1, **payload):
    return Event(seq=seq, event_id=f"event-{seq}", ts="", run_id="run", session_id="session", component="supervisor", type="outbound_message", payload={"text": "提醒", "conversation_key": "qq_group:one", **payload})


def test_quiet_deferral_survives_restart_without_ack_or_failed_attempt(tmp_path):
    class Quiet(Exception):
        deferred = True
        retry_after = 3600

    async def exercise():
        callback = AsyncMock(side_effect=Quiet())
        config = BotConfig(base_url="http://local", workspace_root=tmp_path, conversation_key_prefix="qq_group")
        router = EventRouter(SimpleNamespace(), SessionState(), SimpleNamespace(send_to_channel=callback), config)
        event = outbound(delivery_purpose="ambient", topic_id="books", reply_message_id="42")
        await router._dispatch_durable_outbound(event)
        pending = router._outbound_store.pending()
        assert len(pending) == 1 and pending[0].attempt == 0
        assert router._outbound_store.processed_seq == 0
        reopened = OutboundStore(router._outbound_store.path)
        assert reopened.due() == []
        assert reopened.resume_deferred("qq_group:other") == 0
        assert router.resume_deferred("qq_group:one") == 1
        callback.side_effect = None
        await router._deliver_outbound_record(router._outbound_store.pending()[0])
        assert not router._outbound_store.pending()
        assert callback.call_args.kwargs["delivery_context"].purpose == "ambient"
        assert callback.call_args.kwargs["delivery_context"].topic_id == "books"
        assert callback.call_args.kwargs["delivery_context"].reply_message_id == "42"

    asyncio.run(exercise())


@pytest.mark.parametrize("deliver", [True, False])
def test_reminder_preflight_and_receipt_prevent_superseded_delivery(tmp_path, deliver):
    async def exercise():
        feature = AsyncMock(side_effect=[{"deliver": deliver}, {"ok": True}] if deliver else [{"deliver": False}])
        callback = AsyncMock()
        config = BotConfig(base_url="http://local", workspace_root=tmp_path, conversation_key_prefix="qq_group")
        router = EventRouter(SimpleNamespace(request_feature=feature), SessionState(), SimpleNamespace(send_to_channel=callback), config)
        await router._dispatch_durable_outbound(outbound(reminder_id="reminder", delivery_id="reminder:one:1"))
        assert callback.await_count == int(deliver)
        assert feature.await_count == (2 if deliver else 1)
        assert not router._outbound_store.pending()
        if deliver:
            assert callback.call_args.kwargs["delivery_context"].purpose == "ambient"
            assert feature.call_args.kwargs["body"]["status"] == "delivered"

    asyncio.run(exercise())
