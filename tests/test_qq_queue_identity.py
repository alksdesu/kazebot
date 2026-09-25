from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clonoth_sdk.state import SessionState, TriggerInfo
from clonoth_sdk.types import DeliveryContext, Event
from tests._onebot_harness import load_runtime, set_live_config


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    module._session_state = SessionState()
    set_live_config(module, enable_queue=True, queue_workers=2, queue_interval=0, enable_preempt=False)
    monkeypatch.setattr(module, "_remember_route_state", AsyncMock())
    return module


def item(runtime, user_id=111, message_id=1, group_id=1, text="hello", role="member", at=()):
    event = SimpleNamespace(
        user_id=user_id, message_id=message_id, group_id=group_id,
        sender=SimpleNamespace(role=role, nickname=str(user_id), card=""),
        get_message=lambda: [{"type": "at", "data": {"qq": str(uid)}} for uid in at],
    )
    return runtime.QueuedInbound(
        matcher=None, bot=SimpleNamespace(self_id="90000"), event=event, channel="qq_group",
        real_conversation_key=f"qq_group:{group_id}", stable_conversation_key=f"group-{group_id}",
        text=text, user_text=text, attachments=[], is_dm=False,
        platform_updates={"type": "group", "group_id": group_id, "event": event, "user_id": user_id,
                          "conversation_key": f"group-{group_id}"},
    )


async def settle():
    for _ in range(25):
        await asyncio.sleep(0)


async def stop(tasks):
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_other_senders_never_supply_permissions_for_queued_text(runtime, monkeypatch):
    set_live_config(runtime, admin_users=[10002])
    submitted = []

    async def submit(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(session_id="session", accepted=True, inbound_seq=len(submitted))

    runtime._client = SimpleNamespace(submit_inbound=submit)

    async def exercise():
        await runtime._enqueue_or_submit_inbound(item(runtime, 10001, text="first request"))
        await runtime._enqueue_or_submit_inbound(item(runtime, 10002, text="admin greeting"))
        worker = asyncio.create_task(runtime._qq_queue_worker_forever(0))
        try:
            await settle()
        finally:
            await stop([worker])

    asyncio.run(exercise())
    assert [request["text"] for request in submitted] == ["first request", "admin greeting"]
    assert [{key: request["platform_auth"][key] for key in ("platform", "user_id", "is_admin")} for request in submitted] == [
        {"platform": "qq", "user_id": "10001", "is_admin": False},
        {"platform": "qq", "user_id": "10002", "is_admin": True},
    ]
    assert submitted[0]["memory_hints"]["subjects"][0] == runtime._anonymize_user_id(10001)
    assert submitted[1]["memory_hints"]["subjects"][0] == runtime._anonymize_user_id(10002)


def test_sender_switch_preserves_arrival_order(runtime):
    async def exercise():
        for user_id, text in [(111, "a1"), (222, "b"), (111, "a2")]:
            await runtime._enqueue_or_submit_inbound(item(runtime, user_id, text=text))

    asyncio.run(exercise())
    assert [queued.user_text for queued in runtime._qq_queue] == ["a1", "b", "a2"]


def test_same_sender_keeps_all_memory_mentions_when_merged(runtime):
    async def exercise():
        await runtime._enqueue_or_submit_inbound(item(runtime, text="first", at=[10003]))
        await runtime._enqueue_or_submit_inbound(item(runtime, text="second"))

    asyncio.run(exercise())
    assert len(runtime._qq_queue) == 1
    merged = runtime._qq_queue[0]
    assert merged.user_text == "first\nsecond"
    subjects = runtime._collect_memory_subjects(
        merged.event, merged.user_text, merged.stable_conversation_key,
        mentioned_user_ids=merged.memory_user_ids,
    )
    assert runtime._anonymize_user_id(10003) in subjects


@pytest.mark.parametrize("change", ["admin", "role", "entry", "anonymous", "unknown"])
def test_changed_or_unverified_identity_cannot_merge(runtime, change):
    first = item(runtime)
    second = item(runtime)
    if change == "role":
        second.event.sender.role = "admin"
    elif change == "entry":
        second.entry_node_id = "draw.image_gen"
    elif change == "anonymous":
        first.event.anonymous = second.event.anonymous = SimpleNamespace(id=5)
    elif change == "unknown":
        first.event.user_id = second.event.user_id = None

    async def exercise():
        await runtime._enqueue_or_submit_inbound(first)
        if change == "admin":
            set_live_config(runtime, admin_users=[111])
        await runtime._enqueue_or_submit_inbound(second)

    asyncio.run(exercise())
    assert len(runtime._qq_queue) == 2


def test_waiters_serialize_each_group_without_blocking_other_groups(runtime, monkeypatch):
    set_live_config(runtime, queue_wait_for_reply=True)
    submitted = []

    async def submit(**kwargs):
        seq = kwargs["event"].message_id
        submitted.append(seq)
        runtime._bind_qq_reply_waiter(kwargs["stable_conversation_key"], seq)
        return True

    monkeypatch.setattr(runtime, "_submit_or_preempt_inbound", submit)
    monkeypatch.setattr(runtime, "_send_split_text", AsyncMock(return_value=True))
    monkeypatch.setattr(runtime, "_send_text_and_attachments", AsyncMock())
    first = item(runtime, 111, 11)

    async def exercise():
        for queued in [first, item(runtime, 222, 12), item(runtime, 333, 21, group_id=2)]:
            await runtime._enqueue_or_submit_inbound(queued)
        tasks = [asyncio.create_task(runtime._qq_queue_worker_forever(i)) for i in range(2)]
        try:
            await settle()
            assert submitted == [11, 21]
            trigger = TriggerInfo(11, "group-1", "session", False, platform_data={**first.platform_updates, "bot": first.bot})
            callbacks = runtime.TangQiuCallbacks()
            await callbacks.send_intermediate_reply(trigger, "working")
            await settle()
            assert submitted == [11, 21]
            await callbacks.send_reply(trigger, "done", [])
            await settle()
            assert submitted == [11, 21, 12]
            runtime._mark_qq_reply_finished(11)
            assert not runtime._qq_waiting_replies["group-1"].event.is_set()
            runtime._mark_qq_reply_finished(12)
            runtime._mark_qq_reply_finished(21)
            await settle()
            assert not runtime._qq_waiting_replies
            assert not runtime._qq_active_conversations
        finally:
            await stop(tasks)

    asyncio.run(exercise())


def test_early_final_reply_and_rejected_submit_release_workers(runtime, monkeypatch):
    set_live_config(runtime, queue_wait_for_reply=True)
    submitted = []

    async def submit(**kwargs):
        seq = kwargs["event"].message_id
        submitted.append(seq)
        if seq == 1:
            return False
        runtime._mark_qq_reply_finished(seq)
        runtime._bind_qq_reply_waiter(kwargs["stable_conversation_key"], seq)
        return True

    monkeypatch.setattr(runtime, "_submit_or_preempt_inbound", submit)

    async def exercise():
        for seq in range(1, 4):
            await runtime._enqueue_or_submit_inbound(item(runtime, seq + 100, seq))
        worker = asyncio.create_task(runtime._qq_queue_worker_forever(0))
        try:
            await settle()
            assert submitted == [1, 2, 3]
            assert not runtime._qq_waiting_replies
        finally:
            await stop([worker])

    asyncio.run(exercise())


def test_final_delivery_failure_does_not_finish_waiter(runtime, monkeypatch):
    queued = item(runtime)
    trigger = TriggerInfo(7, "group-1", "session", False, platform_data={**queued.platform_updates, "bot": queued.bot})
    waiter = runtime.QueueReplyWaiter(asyncio.Event(), 7)
    runtime._qq_waiting_replies["group-1"] = waiter
    monkeypatch.setattr(runtime, "_send_text_and_attachments", AsyncMock(side_effect=OSError("send failed")))
    with pytest.raises(OSError):
        asyncio.run(runtime.TangQiuCallbacks().send_reply(trigger, "done", []))
    assert not waiter.event.is_set()


def test_fallback_final_reply_wakes_only_its_request(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_get_fallback_bot", lambda: object())
    monkeypatch.setattr(runtime, "_send_text_and_attachments", AsyncMock())
    waiter = runtime.QueueReplyWaiter(asyncio.Event(), 8)
    runtime._qq_waiting_replies["group-1"] = waiter

    async def exercise():
        callbacks = runtime.TangQiuCallbacks()
        await callbacks.send_to_channel("qq_group:1", "old", [], delivery_context=DeliveryContext(source_inbound_seq=7))
        assert not waiter.event.is_set()
        await callbacks.send_to_channel("qq_group:1", "working", [], is_final=False, delivery_context=DeliveryContext(source_inbound_seq=8))
        assert not waiter.event.is_set()
        await callbacks.send_to_channel("qq_group:1", "current", [], delivery_context=DeliveryContext(source_inbound_seq=8))
        assert waiter.event.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("has_trigger", [False, True])
@pytest.mark.parametrize("attachments", [[], [{"type": "image", "path": "example.png"}]])
def test_router_intermediate_deliveries_never_release_waiter(runtime, monkeypatch, tmp_path, has_trigger, attachments):
    monkeypatch.setattr(runtime, "_get_fallback_bot", lambda: object())
    monkeypatch.setattr(runtime, "_send_text_and_attachments", AsyncMock())
    monkeypatch.setattr(runtime, "_send_split_text", AsyncMock(return_value=True))
    queued = item(runtime)
    if has_trigger:
        runtime._session_state.register_trigger(TriggerInfo(
            7, "qq_group:1", "session", False, platform_data={**queued.platform_updates, "bot": queued.bot},
        ))
    waiter = runtime.QueueReplyWaiter(asyncio.Event(), 7)
    runtime._qq_waiting_replies["group-1"] = waiter
    router = runtime.EventRouter(
        SimpleNamespace(), runtime._session_state, runtime.TangQiuCallbacks(),
        runtime.BotConfig(base_url="http://localhost:0", entry_node_id="qq.orchestrator",
                          conversation_key_prefix="qq_group", workspace_root=tmp_path),
    )
    event = Event(
        seq=1, event_id="reply-1", ts="", run_id="run", session_id="session", component="engine",
        type="intermediate_reply", payload={"source_inbound_seq": 7, "node_id": "qq.orchestrator",
                                           "text": "working", "attachments": attachments},
    )

    async def exercise():
        await router._deliver_intermediate_reply(event, {"conversation_key": "qq_group:1"})
        assert not waiter.event.is_set()
        event.type = "outbound_message"
        await router._deliver_outbound_message(event, {"conversation_key": "qq_group:1"})
        assert waiter.event.is_set()

    try:
        asyncio.run(exercise())
    finally:
        router._outbound_store.close()


@pytest.mark.parametrize("preempt_accepted", [False, True])
def test_preempt_collects_hints_once_after_sender_verification(runtime, monkeypatch, preempt_accepted):
    set_live_config(runtime, enable_preempt=True)
    queued = item(runtime, 111)
    runtime._session_state.register_session("group-1", "session")
    runtime._session_state.register_trigger(TriggerInfo(10, "group-1", "session", False, platform_data=queued.platform_updates))
    hints = {"subjects": ["UserA", "UserB"]}
    collector = Mock(return_value=hints["subjects"])
    monkeypatch.setattr(runtime, "_collect_memory_subjects", collector)
    preempt_calls, inbound_calls = [], []

    async def running(session_id):
        return [SimpleNamespace(task_id="task", is_user_entry=True, source_inbound_seq=10)]

    async def preempt(task_id, **kwargs):
        preempt_calls.append(kwargs)
        return preempt_accepted

    async def submit(**kwargs):
        inbound_calls.append(kwargs)
        return SimpleNamespace(session_id="session", accepted=True, inbound_seq=11)

    runtime._client = SimpleNamespace(get_running_tasks=running, preempt_task=preempt, submit_inbound=submit)
    set_live_config(runtime, enable_queue=False)
    asyncio.run(runtime._enqueue_or_submit_inbound(queued))
    assert preempt_calls[0]["memory_hints"] == hints
    if preempt_accepted:
        assert not inbound_calls
    else:
        assert inbound_calls[0]["memory_hints"] == hints
    assert collector.call_count == 1


def test_different_sender_is_checked_before_preempt_memory_collection(runtime):
    queued = item(runtime, 111)
    previous = item(runtime, 222)
    runtime._session_state.register_session("group-1", "session")
    runtime._session_state.register_trigger(TriggerInfo(10, "group-1", "session", False, platform_data=previous.platform_updates))
    collector = Mock(return_value={"subjects": ["UserA"]})
    preempt = AsyncMock(return_value=True)
    runtime._client = SimpleNamespace(
        get_running_tasks=AsyncMock(return_value=[SimpleNamespace(task_id="task", is_user_entry=True, source_inbound_seq=10)]),
        preempt_task=preempt,
    )
    accepted = asyncio.run(runtime._try_preempt_running_task(
        bot=queued.bot, event=queued.event, conversation_key="group-1", inbound_text="hello",
        attachments=None, is_dm=False, platform_updates=queued.platform_updates, get_memory_hints=collector,
    ))
    assert not accepted
    collector.assert_not_called()
    preempt.assert_not_called()


def test_timed_out_reply_cannot_release_the_next_request(runtime, monkeypatch):
    set_live_config(runtime, queue_wait_for_reply=True, queue_reply_timeout=1)
    second_started = asyncio.Event()

    async def submit(**kwargs):
        seq = kwargs["event"].message_id
        runtime._bind_qq_reply_waiter(kwargs["stable_conversation_key"], seq)
        if seq == 12:
            second_started.set()
        return True

    monkeypatch.setattr(runtime, "_submit_or_preempt_inbound", submit)

    async def exercise():
        await runtime._enqueue_or_submit_inbound(item(runtime, 111, 11))
        await runtime._enqueue_or_submit_inbound(item(runtime, 222, 12))
        worker = asyncio.create_task(runtime._qq_queue_worker_forever(0))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=2)
            runtime._mark_qq_reply_finished(11)
            assert not runtime._qq_waiting_replies["group-1"].event.is_set()
            runtime._mark_qq_reply_finished(12)
            await settle()
            assert not runtime._qq_waiting_replies
        finally:
            await stop([worker])

    asyncio.run(exercise())
