from __future__ import annotations

import asyncio
import json
import importlib.util
import sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import supervisor.admin_api as admin_api
from supervisor.eventlog import EventLog
from supervisor.feature_auth import FeatureActor
from supervisor.policy import PolicyEngine
from supervisor.reminders import ReminderService, create_router
from supervisor.reminders.service import ReminderConflict
from supervisor.state import SupervisorState
from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import Event


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "reminder-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    state = SupervisorState(workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="reminders"), policy=PolicyEngine(workspace_root=tmp_path))
    state.reminders = ReminderService(state)
    yield state
    state.reminders.close()


@pytest.fixture
def actor():
    return FeatureActor(scope="qq_private:test", owner="alice", bot_scope="bot", channel="qq_private")


def create(state, actor):
    return state.reminders.create(actor, "交材料", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())


def fire(state, reminder):
    state.reminders.tick(datetime.fromisoformat(reminder["due_at"]) + timedelta(seconds=1))
    return state.reminders.get(FeatureActor(is_admin=True), reminder["id"])


def adapter_module(name):
    spec = importlib.util.spec_from_file_location(f"_reminder_test_{name}", Path(__file__).resolve().parents[1] / "adapters/onebot" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_delivery_complete_and_snooze_are_distinct_persisted_states(state, actor):
    reminder = create(state, actor)
    current = fire(state, reminder)
    assert current["status"] == "open" and current["delivery_status"] == "queued"
    delivery = current["delivery_id"]
    assert state.reminders.delivery_status(delivery)["deliver"]
    state.reminders.receipt(delivery, "delivered", message_ids=["message-1"])
    assert state.reminders.get(actor, reminder["id"])["status"] == "open"
    assert not state.reminders.delivery_status(delivery)["deliver"]
    snoozed = state.reminders.act(actor, reminder["id"], "snooze", 1, minutes=10, action_id="snooze")
    assert snoozed["revision"] == 2 and snoozed["delivery_status"] == "scheduled"
    assert not state.reminders.receipt(delivery, "delivered")["accepted"]
    state.reminders = ReminderService(state)
    repeated = fire(state, snoozed)
    assert repeated["delivery_id"] != delivery
    completed = state.reminders.act(actor, reminder["id"], "complete", 2)
    assert completed["status"] == "completed"
    assert not state.reminders.delivery_status(repeated["delivery_id"])["deliver"]


def test_tick_restart_dedupes_and_failed_vs_unknown_delivery(state, actor):
    reminder = create(state, actor)
    current = fire(state, reminder)
    state.reminders = ReminderService(state)
    fire(state, reminder)
    assert len([event for event in state.eventlog.events if event["type"] == "outbound_message"]) == 1
    state.reminders.receipt(current["delivery_id"], "failed")
    assert state.reminders.delivery_status(current["delivery_id"])["deliver"]
    state.reminders.receipt(current["delivery_id"], "outcome_unknown")
    assert not state.reminders.delivery_status(current["delivery_id"])["deliver"]
    assert state.reminders.receipt(current["delivery_id"], "delivered")["accepted"]
    assert state.reminders.get(actor, reminder["id"])["delivery_status"] == "delivered"


def test_message_identity_and_action_identity_are_idempotent(state, actor):
    actor = FeatureActor(**{**actor.__dict__, "message_id": "m1"})
    first, duplicate = create(state, actor), create(state, actor)
    assert first["id"] == duplicate["id"]
    action_actor = FeatureActor(**{**actor.__dict__, "message_id": "m2"})
    first_action = state.reminders.act(action_actor, first["id"], "snooze", 1)
    second_action = state.reminders.act(action_actor, first["id"], "snooze", 1)
    assert first_action == second_action


def test_complete_and_snooze_race_does_not_revive_completed_reminder(state, actor):
    reminder = create(state, actor)
    def action(name):
        try:
            return state.reminders.act(actor, reminder["id"], name, 1)
        except ReminderConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, ["complete", "snooze"]))
    assert sum(item is not None for item in results) == 1
    assert state.reminders.get(actor, reminder["id"])["revision"] == 2


def test_ownership_scope_and_timezone(state, actor):
    local = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    reminder = state.reminders.create(actor, "local", local, "Asia/Shanghai")
    assert datetime.fromisoformat(reminder["due_at"]).hour == 1
    for stranger in (FeatureActor(scope=actor.scope, owner="bob", bot_scope="bot"), FeatureActor(scope="other", owner=actor.owner, bot_scope="bot")):
        with pytest.raises(HTTPException):
            state.reminders.act(stranger, reminder["id"], "complete", 1)
    assert state.reminders.list(FeatureActor(scope=actor.scope, owner="bob", bot_scope="bot")) == []


def test_owner_listing_and_quota_are_not_hidden_by_other_reminders(state, actor):
    reminder = create(state, actor)
    with state.reminders.connection() as db:
        rows = []
        for index in range(501):
            other = {**reminder, "id": f"R{index:012x}", "owner": "other"}
            rows.append((other["id"], other["scope"], other["owner"], None, json.dumps(other)))
        db.executemany("INSERT INTO reminders VALUES (?,?,?,?,?)", rows)
    assert [item["id"] for item in state.reminders.list(actor)] == [reminder["id"]]
    state.reminders.max_open = 1
    with pytest.raises(ValueError, match="最多"):
        create(state, actor)
    different_scope = FeatureActor(**{**actor.__dict__, "scope": "qq_group:elsewhere"})
    with pytest.raises(ValueError, match="最多"):
        create(state, different_scope)


def test_open_reminder_remains_visible_after_many_completed_items(state, actor):
    reminder = create(state, actor)
    with state.reminders.connection() as db:
        rows = []
        for index in range(501):
            closed = {**reminder, "id": f"R{index:012x}", "status": "completed"}
            rows.append((closed["id"], closed["scope"], closed["owner"], None, json.dumps(closed)))
        db.executemany("INSERT INTO reminders VALUES (?,?,?,?,?)", rows)
    assert state.reminders.list(actor)[0]["id"] == reminder["id"]


def test_completed_receipt_cannot_downgrade_to_failed_and_cancel_invalidates_delivery(state, actor):
    reminder = create(state, actor)
    current = fire(state, reminder)
    state.reminders.receipt(current["delivery_id"], "delivered", message_ids=["real-id"])
    state.reminders.receipt(current["delivery_id"], "failed")
    delivered = state.reminders.get(actor, reminder["id"])
    assert delivered["delivery_status"] == "delivered"
    assert delivered["platform_message_ids"] == ["real-id"]
    cancelled = state.reminders.act(actor, reminder["id"], "cancel", 1)
    assert cancelled["status"] == "cancelled"
    assert state.reminders.delivery_status(current["delivery_id"])["deliver"] is False


@pytest.mark.asyncio
async def test_reminder_api_auth_and_receipt_cannot_be_spoofed_by_model_or_adapter(state):
    app = FastAPI()
    app.include_router(create_router(state))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/reminders")).status_code == 401
        actor_header = {"Authorization": "Bearer reminder-test", "X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:1", "user_id": "alice"})}
        response = await client.post("/v1/reminders", headers=actor_header, json={"text": "hello", "due_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        assert response.status_code == 200
        assert (await client.post("/v1/reminders/receipts", headers=actor_header, json={"delivery_id": "fake", "status": "delivered"})).status_code == 403


@pytest.mark.asyncio
async def test_qq_command_service_sdk_receipt_and_completion_roundtrip(state):
    app = FastAPI()
    app.include_router(create_router(state))
    client = ClonothClient("http://test", admin_token="reminder-test")
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer reminder-test"})
    commands = adapter_module("reminder_commands")
    actor = {"scope": "qq_private:42", "user_id": "42", "channel": "qq_private", "bot_scope": "bot", "message_id": "100"}
    sent = []
    async def send(scope, text, attachments, **kwargs):
        sent.append((scope, text, kwargs["delivery_context"]))
    router = EventRouter(client, SessionState(), SimpleNamespace(send_to_channel=send), BotConfig(base_url="http://test", workspace_root=state.workspace_root, conversation_key_prefix="qq_private"))
    try:
        response = await commands.handle(client, actor, "/提醒 1分钟后 交材料")
        assert "未完成" in response["text"]
        reminder = (await client.request_feature("GET", "/v1/reminders", actor=actor))["reminders"][0]
        current = fire(state, reminder)
        event = next(item for item in state.eventlog.events if item["type"] == "outbound_message")
        await router._dispatch_durable_outbound(Event.from_dict(event))
        assert len(sent) == 1 and sent[0][0] == actor["scope"]
        assert sent[0][2].purpose == "ambient"
        delivered = await client.request_feature("GET", f"/v1/reminders/{reminder['id']}", actor=actor)
        assert delivered["delivery_status"] == "delivered" and delivered["status"] == "open"
        response = await commands.handle(client, {**actor, "message_id": "101"}, f"/提醒完成 {reminder['id']}")
        assert "已完成" in response["text"]
        assert state.reminders.delivery_status(current["delivery_id"])["deliver"] is False
    finally:
        router._outbound_store.close()
        await client.close()


@pytest.mark.asyncio
async def test_failed_receipt_and_restart_reuse_durable_onebot_send_claim(state, actor):
    from fastapi.responses import JSONResponse

    app = FastAPI()
    app.include_router(create_router(state))
    reject_receipt = True
    @app.middleware("http")
    async def fail_first_receipt(request, call_next):
        nonlocal reject_receipt
        if request.url.path == "/v1/reminders/receipts" and reject_receipt:
            reject_receipt = False
            return JSONResponse({"detail": "synthetic receipt outage"}, status_code=503)
        return await call_next(request)

    module = adapter_module("send_contract")
    claims_path = state.workspace_root / "adapter-claims.sqlite3"
    claims = module.TwoPhaseIdempotencyStore(claims_path)
    client = ClonothClient("http://test", admin_token="reminder-test")
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer reminder-test"})
    sends = []
    async def send(scope, text, attachments, *, delivery_context, **kwargs):
        claim = await claims.begin(delivery_context.idempotency_key)
        if claim.state == "sent":
            return
        async def platform_send():
            sends.append(text)
            return "platform-id"
        await module.protected_claim_send(claims, claim, platform_send)
    config = BotConfig(base_url="http://test", workspace_root=state.workspace_root, conversation_key_prefix="qq_private", outbound_retry_initial=0.05)
    router = EventRouter(client, SessionState(), SimpleNamespace(send_to_channel=send), config)
    try:
        reminder = fire(state, create(state, actor))
        event = next(item for item in state.eventlog.events if item["type"] == "outbound_message")
        await router._dispatch_durable_outbound(Event.from_dict(event))
        assert len(sends) == 1 and len(router._outbound_store.pending()) == 1
        assert state.reminders.get(actor, reminder["id"])["delivery_status"] == "queued"
        router._outbound_store.close()
        claims.close()
        state.reminders = ReminderService(state)
        claims = module.TwoPhaseIdempotencyStore(claims_path)
        router = EventRouter(client, SessionState(), SimpleNamespace(send_to_channel=send), config)
        pending = router._outbound_store.pending()[0]
        await asyncio.sleep(0.06)
        assert await router._deliver_outbound_record(pending)
        assert len(sends) == 1
        assert not router._outbound_store.pending()
        assert state.reminders.get(actor, reminder["id"])["delivery_status"] == "delivered"
    finally:
        router._outbound_store.close()
        claims.close()
        await client.close()


@pytest.mark.asyncio
async def test_superseded_delivery_is_discarded_by_real_service_preflight(state, actor):
    app = FastAPI()
    app.include_router(create_router(state))
    client = ClonothClient("http://test", admin_token="reminder-test")
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer reminder-test"})
    sends = []
    async def send(*args, **kwargs):
        sends.append(args)
    router = EventRouter(client, SessionState(), SimpleNamespace(send_to_channel=send), BotConfig(base_url="http://test", workspace_root=state.workspace_root, conversation_key_prefix="qq_private"))
    try:
        current = fire(state, create(state, actor))
        old_event = next(item for item in state.eventlog.events if item["type"] == "outbound_message")
        snoozed = state.reminders.act(actor, current["id"], "snooze", 1)
        await router._dispatch_durable_outbound(Event.from_dict(old_event))
        assert sends == [] and not router._outbound_store.pending()
        fire(state, snoozed)
        new_event = [item for item in state.eventlog.events if item["type"] == "outbound_message"][-1]
        await router._dispatch_durable_outbound(Event.from_dict(new_event))
        assert len(sends) == 1
        assert state.reminders.get(actor, current["id"])["revision"] == 2
        assert state.reminders.get(actor, current["id"])["delivery_status"] == "delivered"
    finally:
        router._outbound_store.close()
        await client.close()
