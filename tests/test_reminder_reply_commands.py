from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import supervisor.admin_api as admin_api
from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import DeliveryContext, Event
from supervisor.community.api import create_router as community_router
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.reminders import ReminderService, create_router
from supervisor.state import SupervisorState
from tests._onebot_harness import load_runtime, set_live_config


def incoming(text="hello", *, message_id=1, reply_ref="", user_id=42, group_id=12345, role="member"):
    segments = ([{"type": "reply", "data": {"id": str(reply_ref)}}] if reply_ref else []) + [{"type": "text", "data": {"text": text}}]
    return SimpleNamespace(user_id=user_id, message_id=message_id, group_id=group_id, time=1000,
                           sender=SimpleNamespace(card="", nickname="Alice", role=role),
                           get_message=lambda: segments)


class Platform:
    self_id = "90000"

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, **kwargs):
        message_id = str(9001 + len(self.sent))
        self.sent.append({"message_id": message_id, **kwargs})
        return {"message_id": message_id}


@pytest_asyncio.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "reply-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    state = SupervisorState(workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", run_id="reply"), policy=PolicyEngine(workspace_root=tmp_path))
    state.reminders = ReminderService(state)
    app = FastAPI()
    app.include_router(create_router(state))
    app.include_router(community_router(state))
    context = SimpleNamespace(state=state, fail_bindings=0, requests=[])

    @app.middleware("http")
    async def fault(request, call_next):
        context.requests.append((request.method, request.url.path))
        if request.url.path == "/v1/reminders/delivery-messages" and context.fail_bindings:
            context.fail_bindings -= 1
            return JSONResponse({"detail": "synthetic binding outage"}, status_code=503)
        return await call_next(request)

    client = ClonothClient("http://test", admin_token="reply-test")
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer reply-test"})
    runtime = load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, allowed_groups=[12345, 23456], admin_users=[])
    monkeypatch.setattr(runtime, "refresh_live_config", lambda: None)
    runtime._client = client
    runtime._features.record_inbound = AsyncMock(return_value={"recorded": False})
    runtime._features.record_outbound = AsyncMock()
    runtime._feature_matcher.finish = AsyncMock()
    bot = Platform()
    monkeypatch.setattr(runtime, "_get_fallback_bot", lambda: bot)
    actor = runtime._features.actor(bot, incoming())
    runtime._conversation_bots[actor["scope"]] = bot
    config = BotConfig(base_url="http://test", workspace_root=tmp_path, conversation_key_prefix="qq_group", outbound_retry_initial=0.05)
    router = EventRouter(client, SessionState(), runtime.TangQiuCallbacks(), config)
    runtime._event_router = router
    context.__dict__.update(client=client, runtime=runtime, actor=actor, bot=bot, router=router, config=config)
    try:
        yield context
    finally:
        context.router._outbound_store.close()
        runtime._outbound_idempotency.close()
        await client.close()


async def create(context, *, message_id="1", text="提交报告"):
    return await context.client.request_feature("POST", "/v1/reminders", actor={**context.actor, "message_id": message_id}, body={"text": text, "due_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})


async def deliver(context, reminder):
    context.state.reminders.tick(datetime.fromisoformat(reminder["due_at"]) + timedelta(seconds=1))
    event = next(event for event in reversed(context.state.eventlog.events) if event["type"] == "outbound_message" and event["payload"].get("reminder_id") == reminder["id"])
    await context.router._dispatch_durable_outbound(Event.from_dict(event))
    return event


async def reminder(context, identity):
    return await context.client.request_feature("GET", f"/v1/reminders/{identity}", actor=context.actor)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,status", [("已完成", "completed"), ("取消提醒", "cancelled")])
async def test_real_sender_binding_and_natural_reference_action(context, text, status):
    created = await create(context)
    outbound = await deliver(context, created)
    assert "引用此消息回复“已完成”“十分钟后”或“取消提醒”" in outbound["payload"]["text"]
    current = await reminder(context, created["id"])
    assert current["delivery_status"] == "delivered" and current["status"] == "open"
    message_id = context.bot.sent[0]["message_id"]
    row = context.runtime._outbound_idempotency._db.execute("SELECT state,platform_message_id FROM claims").fetchone()
    assert tuple(row) == ("sent", message_id)
    metadata = await context.client.request_feature("GET", "/v1/reminders/reply-context", actor=context.actor, params={"message_id": message_id})
    assert metadata["matched"] and metadata["authorized"] and metadata["revision"] == 1
    event = incoming(text, message_id=2, reply_ref=message_id)
    assert await context.runtime._feature_command_rule(context.bot, event)
    await context.runtime._handle_feature_command(context.bot, event)
    updated = await reminder(context, created["id"])
    assert updated["status"] == status and updated["revision"] == 2
    assert "引用实际到期提醒消息" in str(context.bot.sent[-1]["message"])
    await context.runtime._handle_feature_command(context.bot, event)
    assert (await reminder(context, created["id"]))["revision"] == 2
    assert len(context.bot.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text,minutes", [("十分钟后", 10), ("10分钟后", 10), ("半小时后", 30), ("延后一小时三十分钟", 90)])
async def test_natural_snooze_binds_original_revision_and_keeps_duplicates_idempotent(context, text, minutes):
    created = await create(context)
    await deliver(context, created)
    message_id = context.bot.sent[0]["message_id"]
    event = incoming(text, message_id=2, reply_ref=message_id)
    before = datetime.now(timezone.utc)
    assert await context.runtime._feature_command_rule(context.bot, event)
    response = await context.runtime._features.handle_command(context.bot, event, text)
    assert response["control"] is True
    changed = await reminder(context, created["id"])
    assert changed["revision"] == 2 and changed["delivery_status"] == "scheduled"
    assert abs((datetime.fromisoformat(changed["due_at"]) - before).total_seconds() - minutes * 60) < 3
    await context.runtime._features.handle_command(context.bot, event, text)
    assert (await reminder(context, created["id"]))["revision"] == 2
    stale = incoming("已完成", message_id=3, reply_ref=message_id)
    response = await context.runtime._features.handle_command(context.bot, stale, "已完成")
    assert "已更新" in response["text"]
    assert (await reminder(context, created["id"]))["status"] == "open"
    await deliver(context, changed)
    latest_message = context.bot.sent[-1]["message_id"]
    response = await context.runtime._features.handle_command(context.bot, incoming("已完成", message_id=4, reply_ref=latest_message), "已完成")
    assert "已完成" in response["text"]
    assert (await reminder(context, created["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_binding_failure_restarts_only_registration_without_resending_platform_message(context):
    created = await create(context)
    context.fail_bindings = 1
    await deliver(context, created)
    assert len(context.bot.sent) == 1
    assert (await reminder(context, created["id"]))["delivery_status"] != "delivered"
    assert len(context.router._outbound_store.pending()) == 1
    store = context.runtime._outbound_idempotency
    path = store.path
    store.close()
    context.runtime._outbound_idempotency = type(store)(path)
    context.state.reminders = ReminderService(context.state)
    context.router._outbound_store.close()
    context.router = EventRouter(context.client, SessionState(), context.runtime.TangQiuCallbacks(), context.config)
    context.runtime._event_router = context.router
    await asyncio.sleep(0.06)
    assert await context.router._deliver_outbound_record(context.router._outbound_store.pending()[0])
    assert len(context.bot.sent) == 1
    assert (await reminder(context, created["id"]))["delivery_status"] == "delivered"
    message_id = context.bot.sent[0]["message_id"]
    event = incoming("已完成", message_id=2, reply_ref=message_id)
    assert await context.runtime._feature_command_rule(context.bot, event)
    await context.runtime._handle_feature_command(context.bot, event)
    assert (await reminder(context, created["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_recording_real_message_reference_does_not_mark_delivery_complete(context):
    created = await create(context)
    context.state.reminders.tick(datetime.fromisoformat(created["due_at"]) + timedelta(seconds=1))
    current = await reminder(context, created["id"])
    send_context = DeliveryContext(idempotency_key="separate-binding", feature_delivery_id=current["delivery_id"], conversation_key=context.actor["scope"], purpose="ambient")
    await context.runtime.TangQiuCallbacks().send_to_channel(context.actor["scope"], "待确认送达的提醒", [], delivery_context=send_context)
    assert (await reminder(context, created["id"]))["delivery_status"] == "queued"
    reply = await context.client.request_feature("GET", "/v1/reminders/reply-context", actor=context.actor, params={"message_id": context.bot.sent[0]["message_id"]})
    assert reply["matched"] and reply["authorized"]


@pytest.mark.asyncio
@pytest.mark.parametrize("part", [0, 1])
async def test_every_real_split_message_keeps_the_same_reminder_binding(context, part):
    created = await create(context, text="第一部分[SPLIT]第二部分")
    await deliver(context, created)
    assert len(context.bot.sent) == 2
    event = incoming("已完成", message_id=2, reply_ref=context.bot.sent[part]["message_id"])
    assert await context.runtime._feature_command_rule(context.bot, event)
    response = await context.runtime._features.handle_command(context.bot, event, "已完成")
    assert "已完成" in response["text"]
    assert (await reminder(context, created["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_natural_reference_scope_bot_owner_and_missing_target_are_not_guessed(context):
    created = await create(context)
    await deliver(context, created)
    ref = context.bot.sent[0]["message_id"]
    for overrides, expected in [({"user_id": "43", "is_admin": True}, 403), ({"scope": "qq_group:other"}, 422), ({"bot_scope": "different-bot"}, 422)]:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await context.client.request_feature("POST", "/v1/reminders/reply-actions", actor={**context.actor, **overrides}, body={"message_id": ref, "action": "complete"})
        assert failure.value.response.status_code == expected
    for text, reply_ref in [("已完成", ""), ("十分钟后", ""), ("已完成", "999999"), ("我刚才已完成了一半", ref)]:
        event = incoming(text, message_id=50, reply_ref=reply_ref)
        context.runtime._features.coordinator.messages.clear()
        assert not await context.runtime._feature_command_rule(context.bot, event)
    assert (await reminder(context, created["id"]))["status"] == "open"


@pytest.mark.asyncio
async def test_multiple_open_reminders_use_the_quoted_message_not_the_latest(context):
    first = await create(context)
    await deliver(context, first)
    first_ref = context.bot.sent[0]["message_id"]
    second = await create(context, message_id="second-reminder", text="另一个事项")
    await deliver(context, second)
    event = incoming("已完成", message_id=3, reply_ref=first_ref)
    assert await context.runtime._feature_command_rule(context.bot, event)
    await context.runtime._features.handle_command(context.bot, event, "已完成")
    assert (await reminder(context, first["id"]))["status"] == "completed"
    assert (await reminder(context, second["id"]))["status"] == "open"


@pytest.mark.asyncio
async def test_binding_is_transport_only_and_never_overwrites_another_reminder(context):
    first = await create(context)
    await deliver(context, first)
    current = await reminder(context, first["id"])
    body = {"delivery_id": current["delivery_id"], "scope": context.actor["scope"], "bot_scope": context.bot.self_id, "message_id": "9001"}
    assert (await context.client.request_feature("POST", "/v1/reminders/delivery-messages", body=body))["accepted"]
    for override in ({"scope": "qq_group:other"}, {"bot_scope": "wrong-bot"}):
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await context.client.request_feature("POST", "/v1/reminders/delivery-messages", body={**body, **override})
        assert failure.value.response.status_code == 403
    for actor, headers in [(context.actor, {}), (None, {"X-Clonoth-Task-Id": "fake-model-task"})]:
        if headers:
            response = await context.client._http().post("http://test/v1/reminders/delivery-messages", json=body, headers=headers)
            assert response.status_code == 403
        else:
            with pytest.raises(httpx.HTTPStatusError) as failure:
                await context.client.request_feature("POST", "/v1/reminders/delivery-messages", actor=actor, body=body)
            assert failure.value.response.status_code == 403
    second = await create(context, message_id="different-request")
    context.state.reminders.tick(datetime.fromisoformat(second["due_at"]) + timedelta(seconds=1))
    second = await reminder(context, second["id"])
    with pytest.raises(httpx.HTTPStatusError) as failure:
        await context.client.request_feature("POST", "/v1/reminders/delivery-messages", body={**body, "delivery_id": second["delivery_id"]})
    assert failure.value.response.status_code == 409


@pytest.mark.asyncio
async def test_natural_reminder_control_remains_available_while_group_is_silent(context):
    created = await create(context)
    await deliver(context, created)
    manager = {**context.actor, "role": "admin"}
    await context.client.request_feature("POST", "/v1/community/quiet", actor=manager, body={"mode": "silent", "duration_sec": 600})
    event = incoming("已完成", message_id=2, reply_ref=context.bot.sent[0]["message_id"])
    assert await context.runtime._feature_command_rule(context.bot, event)
    await context.runtime._handle_feature_command(context.bot, event)
    assert (await reminder(context, created["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_natural_quiet_restore_uses_same_canonical_command_and_authorization(context):
    manager = {**context.actor, "role": "admin"}
    await context.client.request_feature("POST", "/v1/community/quiet", actor=manager, body={"mode": "silent", "duration_sec": 600})
    resume = Mock()
    context.runtime._event_router = SimpleNamespace(resume_deferred=resume)
    member = incoming("恢复", message_id=2)
    assert await context.runtime._feature_command_rule(context.bot, member)
    denied = await context.runtime._features.handle_command(context.bot, member, "恢复")
    assert "管理" in denied["text"]
    assert (await context.client.request_feature("GET", "/v1/community/state", actor=manager))["quiet"]
    resume.assert_not_called()
    event = incoming("恢复", message_id=3, role="admin")
    assert await context.runtime._feature_command_rule(context.bot, event)
    allowed = await context.runtime._features.handle_command(context.bot, event, "恢复")
    assert allowed["control"] is True
    assert not (await context.client.request_feature("GET", "/v1/community/state", actor=manager))["quiet"]
    resume.assert_called_once_with(context.actor["scope"])
    for text in ["恢复文件", "我恢复了"]:
        assert not await context.runtime._feature_command_rule(context.bot, incoming(text, message_id=4))
