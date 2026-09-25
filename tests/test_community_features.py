from __future__ import annotations

import asyncio
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from supervisor.community.commands import command
from supervisor.community.service import CommunityService
from supervisor.feature_auth import FeatureActor
from tests._onebot_harness import load_runtime, set_live_config


@pytest.fixture()
def service(tmp_path):
    return CommunityService(tmp_path)


@pytest.fixture()
def manager():
    return FeatureActor(scope="qq_group:one", owner="manager", role="admin", channel="qq_group", bot_scope="90000")


def member(manager, identity):
    return replace(manager, owner=str(identity), role="member")


def test_group_scope_and_roles_are_enforced(service, manager):
    with pytest.raises(HTTPException):
        service.create_activity(member(manager, 1), {"title": "test"})
    poll = service.create_activity(manager, {"title": "Dinner", "kind": "poll", "options": ["A", "B"]})
    other = replace(manager, scope="qq_group:two")
    with pytest.raises(ValueError):
        service.activity_action(other, poll["id"], "view", {})
    assert service.activities(other) == []


def test_votes_are_owned_and_replace_instead_of_accumulate(service, manager):
    poll = service.create_activity(manager, {"kind": "poll", "title": "Lunch", "options": ["A", "B"]})
    voter = member(manager, 1)
    service.activity_action(voter, poll["id"], "vote", {"choices": [0]})
    service.activity_action(voter, poll["id"], "vote", {"choices": [0]})
    result = service.activity_action(voter, poll["id"], "vote", {"choices": [1]})
    assert result["counts"] == [0, 1]
    with pytest.raises(ValueError):
        service.activity_action(voter, poll["id"], "vote", {"choices": [-1]})


def test_concurrent_signups_never_overbook_and_waiting_member_is_promoted(service, manager):
    activity = service.create_activity(manager, {"title": "Game", "capacity": 1})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: service.activity_action(member(manager, index), activity["id"], "join", {"display_name": f"user{index}"}), range(8)))
    state = service.activity_action(manager, activity["id"], "view", {})
    assert sum(item["status"] == "joined" for item in state["participants"]) == 1
    assert sum(item["status"] == "waiting" for item in state["participants"]) == 7
    occupied = next(index for index in range(8) if service.activity_action(member(manager, index), activity["id"], "view", {})["mine"]["status"] == "joined")
    state = service.activity_action(member(manager, occupied), activity["id"], "leave", {})
    assert sum(item["status"] == "joined" for item in state["participants"]) == 1
    assert state["remaining"] == 0


def test_creation_is_deduplicated_by_origin_message_and_survives_reload(service, manager, tmp_path):
    actor = replace(manager, message_id="m1")
    first = service.create_activity(actor, {"title": "Game", "capacity": 3})
    again = CommunityService(tmp_path).create_activity(actor, {"title": "Game", "capacity": 3})
    assert first["id"] == again["id"]
    assert len(service.activities(manager)) == 1


def test_reminders_recheck_current_attendance_and_cancellation(service, manager):
    activity = service.create_activity(manager, {"title": "Game"})
    person = member(manager, 1)
    service.activity_action(person, activity["id"], "join", {"display_name": "Alice"})
    service.activity_action(manager, activity["id"], "remind", {})
    assert "Alice" in service.notifications(manager)[0]["text"]
    service.activity_action(person, activity["id"], "confirm", {})
    assert service.notifications(manager) == []
    service.activity_action(replace(manager, message_id="r2"), activity["id"], "remind", {})
    service.activity_action(manager, activity["id"], "cancel", {})
    assert service.notifications(manager) == []


def test_guide_and_quiet_survive_restart_without_cross_group_effects(service, manager, tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("supervisor.community.service.time.time", lambda: now[0])
    service.update_guide(manager, {"rules": "Be kind", "faq": "Ask here"})
    service.set_quiet(manager, "silent", 30)
    restored = CommunityService(tmp_path)
    assert restored.state(manager)["quiet"]["mode"] == "silent"
    assert restored.state(replace(manager, scope="qq_group:two"))["quiet"] == {}
    assert command(restored, member(manager, 1), {"text": "/群规"})["text"] == "Be kind"
    now[0] += 31
    assert restored.state(manager)["quiet"] == {}


def test_notice_is_opt_in_deduplicated_and_obeys_quiet(service, manager, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("supervisor.community.service.time.time", lambda: now[0])
    notice = {"notice_type": "group_increase", "member_id": "12345", "timestamp": now[0]}
    assert not service.notice(manager, notice)["queued"]
    service.update_settings(manager, {"welcome_enabled": True})
    service.notice(manager, notice)
    service.notice(manager, notice)
    now[0] += 4
    service.set_quiet(manager, "listen", 30)
    assert service.notifications(manager) == []
    service.set_quiet(manager, "off")
    pending = service.notifications(manager)
    assert len(pending) == 1
    service.acknowledge_notification(manager, pending[0]["id"], "sent123")
    assert service.notifications(manager) == []


def test_explicit_reply_restores_correct_topic_and_scope(service, manager):
    service.update_settings(manager, {"topic_enabled": True})
    first = service.ingest(member(manager, 1), {"message_id": "1", "text": "A"})
    second = service.ingest(member(manager, 2), {"message_id": "2", "text": "B"})
    followup = service.ingest(member(manager, 3), {"message_id": "3", "reply_ref": "1", "text": "about A"})
    assert first["topic_id"] == followup["topic_id"] != second["topic_id"]
    assert [entry["ref"] for entry in followup["context"]] == ["1", "3"]
    other = replace(manager, scope="qq_group:two")
    service.update_settings(other, {"topic_enabled": True})
    foreign = service.ingest(other, {"message_id": "4", "reply_ref": "1", "text": "foreign"})
    assert foreign["topic_id"] != first["topic_id"]


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[12345], admin_users=[10001])
    return module


def event(user_id=10001, message_id=1, text="hello"):
    return SimpleNamespace(user_id=user_id, message_id=message_id, group_id=12345, time=1000,
                           sender=SimpleNamespace(card="", nickname="Alice", role="admin"),
                           get_message=lambda: [{"type": "text", "data": {"text": text}}])


def test_real_adapter_command_dispatch_uses_trusted_actor(runtime, monkeypatch):
    calls = []

    async def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/state"):
            return {"settings": {}, "quiet": {}}
        return {"result": {"text": "joined", "attachments": []}}

    runtime._client = SimpleNamespace(request_feature=request)
    result = asyncio.run(runtime._features.handle_command(SimpleNamespace(self_id="90000"), event(text="/报名 A1"), "/报名 A1"))
    assert result["text"] == "joined"
    actor = calls[-1][1]["actor"]
    assert actor["user_id"] == "10001" and actor["role"] == "admin"
    assert actor["scope"].startswith("qq_group:")


def test_quiet_defers_final_output_before_onebot_send(runtime, monkeypatch):
    import time
    policy = {"quiet": {"mode": "silent", "expires_at": time.time() + 100}, "settings": {}}
    runtime._client = SimpleNamespace(request_feature=AsyncMock(return_value=policy))
    bot = SimpleNamespace(self_id="90000", send_group_msg=AsyncMock(return_value={"message_id": 8}))
    from clonoth_sdk.state import TriggerInfo
    trigger = TriggerInfo(1, "qq_group:test", "session", False, platform_data={"bot": bot, "type": "group", "group_id": 12345})
    with pytest.raises(Exception) as caught:
        asyncio.run(runtime.TangQiuCallbacks().send_reply(trigger, "result", []))
    assert caught.value.deferred is True
    bot.send_group_msg.assert_not_called()


def test_short_message_aggregation_keeps_identity_and_order(runtime):
    coordinator = runtime._features.coordinator
    sent = []
    settings = {"merge_window_sec": 0.03, "merge_max_wait_sec": 0.1}

    def queued(text, sender=10001):
        return runtime.QueuedInbound(None, None, event(sender, text=text), "qq_group", "qq_group:12345", "scope", text, [], False, {}, text)

    async def submit(item):
        sent.append((item.event.user_id, item.user_text))
        return True

    async def exercise():
        first = asyncio.create_task(coordinator.submit(queued("one"), settings, (10001,), submit, runtime._merge_queued_inbound))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(coordinator.submit(queued("two"), settings, (10001,), submit, runtime._merge_queued_inbound))
        third = asyncio.create_task(coordinator.submit(queued("other", 10002), settings, (10002,), submit, runtime._merge_queued_inbound))
        assert await asyncio.gather(first, second, third) == [True, True, True]

    asyncio.run(exercise())
    assert sent == [(10001, "one\ntwo"), (10002, "other")]


def test_community_api_authorizes_actual_scope_and_runs_commands(tmp_path, monkeypatch):
    import json
    from supervisor.community.api import create_router

    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda request: None)
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(workspace_root=tmp_path)))
    manager_actor = {"scope": "qq_group:a", "user_id": "10001", "role": "admin", "channel": "qq_group"}
    headers = {"X-Clonoth-Adapter-Actor": json.dumps(manager_actor)}
    with TestClient(app) as client:
        response = client.post('/v1/community/commands', headers=headers, json={"text": "/活动创建 聚餐 | 2"})
        assert response.status_code == 200
        assert "聚餐" in response.json()["result"]["text"]
        assert client.get('/v1/community/activities?scope=qq_group:b', headers=headers).status_code == 403
        member_headers = {"X-Clonoth-Adapter-Actor": json.dumps({**manager_actor, "user_id": "10002", "role": "member"})}
        assert client.post('/v1/community/quiet', headers=member_headers, json={"mode": "silent"}).status_code == 403
        saved = client.patch('/v1/community/settings', headers=headers, json={"topic_enabled": True})
        assert saved.status_code == 200
        assert client.get('/v1/community/state', headers=headers).json()["settings"]["topic_enabled"] is True


def test_resuming_quiet_wakes_sdk_deferred_delivery(runtime):
    calls = []

    async def request(method, path, **kwargs):
        if path.endswith("/state"):
            return {"quiet": {"mode": "silent", "expires_at": 9999999999}, "settings": {}}
        return {"result": {"text": "已恢复", "attachments": []}}

    runtime._client = SimpleNamespace(request_feature=request)
    runtime._event_router = SimpleNamespace(resume_deferred=lambda scope: calls.append(scope) or 1)
    result = asyncio.run(runtime._features.handle_command(SimpleNamespace(self_id="90000"), event(text="/恢复"), "/恢复"))
    assert result["text"] == "已恢复"
    assert calls == [runtime._features.actor(SimpleNamespace(self_id="90000"), event())["scope"]]


def test_short_message_stage_survives_restart_and_keeps_topic(runtime, monkeypatch):
    import time

    bot = SimpleNamespace(self_id="90000", call_api=AsyncMock(return_value={"role": "member"}))
    original = event(message_id=7, text="pending")
    scope = runtime._features.actor(bot, original)["scope"]
    queued = runtime.QueuedInbound(None, bot, original, "qq_group", "qq_group:12345", scope,
                                   "pending", [{"type": "image", "path": "data/attachments/example.png"}], False,
                                   {"_route_hints": {"topic_id": "topic-1", "source_message_refs": ["7"]}}, "pending")
    runtime._features.stage_burst(queued)
    runtime._features.started_at = time.time() + 1
    recovered = []

    async def submit(item):
        recovered.append(item)
        return True

    monkeypatch.setattr(runtime, "_queue_or_submit_ready", submit)
    asyncio.run(runtime._features.recover_bursts(bot))
    assert len(recovered) == 1
    assert recovered[0].platform_updates["_route_hints"]["topic_id"] == "topic-1"
    assert recovered[0].platform_updates["_source_attachments"] == queued.attachments
    assert runtime._features.burst_store.recover("another-bot", time.time() + 2) == []


def test_quiet_deferral_is_not_wrapped_as_attachment_failure(runtime, monkeypatch, tmp_path):
    import time
    from importlib import import_module

    deferred = import_module(runtime.__name__ + '.feature_gateway').QuietDeliveryDeferred(time.time() + 60)
    image = tmp_path / "test.png"
    image.write_bytes(b"placeholder")
    monkeypatch.setattr(runtime, "_send_attachment_path", AsyncMock(side_effect=deferred))
    with pytest.raises(type(deferred)) as caught:
        asyncio.run(runtime._send_attachments_one_by_one(object(), {"type": "group", "group_id": 12345}, [{"type": "image", "path": str(image)}]))
    assert caught.value is deferred


def test_private_metadata_never_grants_group_role(runtime):
    private = event()
    del private.group_id
    private.sender.role = "owner"
    actor = runtime._features.actor(SimpleNamespace(self_id="90000"), private)
    assert actor["channel"] == "qq_private" and actor["role"] == "member"


def test_topic_intent_json_keeps_old_yes_no_contract():
    from supervisor.qq_intent import parse_intent_text

    assert parse_intent_text('yes|followup').agreed
    assert not parse_intent_text('no|other people').agreed
    decision = parse_intent_text('{"action":"short_reply","reason":"followup"}')
    assert decision.agreed and decision.action == "short_reply"
    assert parse_intent_text('{"action":"delete_everything"}').error == "unparsable"


def test_replayed_old_activity_messages_do_not_undo_later_actions(service, manager):
    poll = service.create_activity(manager, {"title": "Dinner", "kind": "poll", "options": ["A", "B"]})
    voter = replace(member(manager, 1), message_id="vote-old")
    first = service.activity_action(voter, poll["id"], "vote", {"choices": [0]})
    service.activity_action(replace(voter, message_id="vote-new"), poll["id"], "vote", {"choices": [1]})
    assert service.activity_action(voter, poll["id"], "vote", {"choices": [0]}) == first
    assert service.activity_action(voter, poll["id"], "view", {})["counts"] == [0, 1]
    activity = service.create_activity(manager, {"title": "Game", "capacity": 1})
    person = replace(member(manager, 1), message_id="join")
    service.activity_action(person, activity["id"], "join", {})
    service.activity_action(replace(person, message_id="leave"), activity["id"], "leave", {})
    service.activity_action(person, activity["id"], "join", {})
    assert service.activity_action(person, activity["id"], "view", {})["mine"]["status"] == "left"


def test_disabling_welcome_cancels_already_queued_notices(service, manager, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("supervisor.community.service.time.time", lambda: now[0])
    service.update_settings(manager, {"welcome_enabled": True})
    service.notice(manager, {"notice_type": "group_increase", "member_id": "12345", "timestamp": now[0]})
    service.update_settings(manager, {"welcome_enabled": False})
    now[0] += 4
    assert service.notifications(manager) == []
    service.update_settings(manager, {"welcome_enabled": True})
    assert service.notifications(manager) == []


@pytest.mark.parametrize("body", [{"capacity": True}, {"capacity": 1.5}, {"multiple": "false"}])
def test_activity_inputs_do_not_silently_coerce_invalid_types(service, manager, body):
    with pytest.raises(ValueError):
        service.create_activity(manager, {"title": "Game", **body})


def test_current_inbound_never_embeds_foreign_topic_as_user_content(runtime, monkeypatch):
    bot = SimpleNamespace(self_id="90000")
    current = event(message_id=9, text="follow up")
    runtime._client = SimpleNamespace(request_feature=AsyncMock())
    actor = runtime._features.actor(bot, current)
    runtime._features.coordinator.messages[(actor["scope"], "9")] = {
        "topic": {"topic_id": "books", "context": [{"ref": "8", "label": "Alice", "text": "book original"}]},
    }
    runtime._append_group_history(12345, "foreign unrelated question")
    monkeypatch.setattr(runtime, "_build_reply_context", AsyncMock(return_value=("", [])))
    text, watermark = asyncio.run(runtime._build_inbound_text(current, bot, "follow up", actor["scope"], []))
    assert "book original" in text and "foreign unrelated question" not in text
    assert watermark == -1


def test_reaction_rechecks_quiet_instead_of_using_cached_event_policy(runtime):
    import time
    set_live_config(runtime, enable_reactions=True)
    bot = SimpleNamespace(self_id="90000", call_api=AsyncMock())
    current = event()
    actor = runtime._features.actor(bot, current)
    runtime._features.coordinator.messages[(actor["scope"], actor["message_id"])] = {
        "actor": actor, "direct": True, "policy": {"quiet": {}},
    }
    runtime._client = SimpleNamespace(request_feature=AsyncMock(return_value={"quiet": {"mode": "silent", "expires_at": time.time() + 100}}))
    assert not asyncio.run(runtime._set_message_react(bot, current, "1", True))
    bot.call_api.assert_not_called()


def test_group_local_responses_and_ordinary_commands_respect_silent(runtime, monkeypatch):
    import time
    policy = {"quiet": {"mode": "silent", "expires_at": time.time() + 100}, "settings": {}}
    runtime._client = SimpleNamespace(request_feature=AsyncMock(return_value=policy))
    bot = SimpleNamespace(self_id="90000")
    matcher = SimpleNamespace(finish=AsyncMock())
    with pytest.raises(Exception) as caught:
        asyncio.run(runtime._finish_local_command(matcher, bot, event(), user_text="/命令", attachments=[], reply="ordinary"))
    assert caught.value.deferred
    matcher.finish.assert_not_called()
    result = asyncio.run(runtime._features.handle_command(bot, event(text="/活动创建 test | 2"), "/活动创建 test | 2"))
    assert not result["text"] and not result["control"]
    assert not any(call.args[1] == "/v1/community/commands" for call in runtime._client.request_feature.call_args_list)


def test_merged_burst_recovers_as_one_request_with_all_refs(runtime, monkeypatch):
    import time
    bot = SimpleNamespace(self_id="90000", call_api=AsyncMock(return_value={"role": "member"}))
    runtime._client = SimpleNamespace(request_feature=AsyncMock())
    def queued(ref, text):
        current = event(message_id=ref, text=text)
        scope = runtime._features.actor(bot, current)["scope"]
        return runtime.QueuedInbound(None, bot, current, "qq_group", "qq_group:12345", scope, text,
            [{"type": "image", "path": f"data/attachments/{ref}.png"}], False,
            {"_route_hints": {"topic_id": "books", "source_message_refs": [str(ref)]}}, text)
    first, second = queued(7, "one"), queued(8, "更正 two")
    runtime._features.stage_burst(first)
    runtime._features.stage_burst(second)
    runtime._merge_queued_inbound(first, second)
    staged = runtime._features.burst_store.recover("90000", time.time() + 1)
    assert len(staged) == 1 and staged[0]["refs"] == ["7", "8"]
    runtime._features.started_at = time.time() + 1
    recovered = []
    async def submit(item):
        recovered.append(item)
        return True
    monkeypatch.setattr(runtime, "_queue_or_submit_ready", submit)
    asyncio.run(runtime._features.recover_bursts(bot))
    assert len(recovered) == 1 and recovered[0].user_text == "one\n更正 two"
    assert len(recovered[0].attachments) == 2
    assert "以这条更正为准" in recovered[0].text


def test_burst_recovery_retries_after_unaccepted_submission(runtime, monkeypatch):
    import time
    bot = SimpleNamespace(self_id="90000", call_api=AsyncMock(return_value={"role": "member"}))
    original = event(message_id=7)
    scope = runtime._features.actor(bot, original)["scope"]
    runtime._features.stage_burst(runtime.QueuedInbound(None, bot, original, "qq_group", "qq_group:12345", scope,
        "pending", [], False, {"_route_hints": {"source_message_refs": ["7"]}}, "pending"))
    runtime._features.started_at = time.time() + 1
    submit = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(runtime, "_queue_or_submit_ready", submit)
    asyncio.run(runtime._features.recover_bursts(bot))
    assert "90000" not in runtime._features.recovered_accounts
    asyncio.run(runtime._features.recover_bursts(bot))
    assert submit.await_count == 2 and "90000" in runtime._features.recovered_accounts


def test_legacy_intent_decline_and_failed_decision_never_turn_into_task(runtime):
    from clonoth_sdk.types import IntentVerdict
    assert runtime._coordinated_response_action(IntentVerdict(agreed=False), {}) == "ignore"
    assert runtime._coordinated_response_action(IntentVerdict(error="timeout"), {}) == "ignore"
    assert runtime._coordinated_response_action(IntentVerdict(error="timeout"), {"direct": True}) == "task"
    set_live_config(runtime, enable_reactions=False)
    assert runtime._coordinated_response_action(IntentVerdict(agreed=True, decided=True, action="react", reaction_id="1"), {}) == "short_reply"


def test_burst_merge_retains_task_authority_and_direct_delivery(runtime):
    def queued(ref, action, purpose):
        return runtime.QueuedInbound(None, None, event(message_id=ref), "qq_group", "qq_group:12345", "scope", str(ref), [], False,
            {"_route_hints": {"source_message_refs": [str(ref)], "response_action": action}, "_response_purpose": purpose}, str(ref))
    first, followup = queued(1, "task", "direct"), queued(2, "short_reply", "ambient")
    runtime._merge_queued_inbound(first, followup)
    assert first.platform_updates["_route_hints"]["response_action"] == "task"
    assert first.platform_updates["_route_hints"]["delivery_purpose"] == "direct"
    assert first.platform_updates["_route_hints"]["source_message_refs"] == ["1", "2"]


def test_control_command_does_not_wait_for_pending_burst(runtime):
    coordinator = runtime._features.coordinator
    sent = []
    settings = {"merge_window_sec": 0.08, "merge_max_wait_sec": 0.1}
    def queued(text, ref):
        return runtime.QueuedInbound(None, None, event(message_id=ref, text=text), "qq_group", "qq_group:12345", "scope", text, [], False,
            {"_route_hints": {"source_message_refs": [str(ref)]}}, text)
    async def submit(item):
        sent.append(item.user_text)
        return True
    async def exercise():
        pending = asyncio.create_task(coordinator.submit(queued("one", 1), settings, (10001,), submit, runtime._merge_queued_inbound))
        await asyncio.sleep(0.005)
        await coordinator.submit(queued("/计划取消 P123456789abc", 2), settings, (10001,), submit, runtime._merge_queued_inbound)
        assert sent == ["/计划取消 P123456789abc"]
        assert coordinator.can_continue({"scope": "scope", "user_id": "10001"}, "1")
        await pending
    asyncio.run(exercise())
    assert sent == ["/计划取消 P123456789abc", "one"]


@pytest.mark.parametrize("path", ["text", "file", "forward"])
def test_silent_blocks_actual_platform_send_families(runtime, tmp_path, path):
    import time
    runtime._client = SimpleNamespace(request_feature=AsyncMock(return_value={"quiet": {"mode": "silent", "expires_at": time.time() + 60}}))
    bot = SimpleNamespace(self_id="90000", send_group_msg=AsyncMock(), call_api=AsyncMock())
    target = {"type": "group", "group_id": 12345}
    sample = tmp_path / "file.txt"
    sample.write_text("sample")
    if path == "text":
        operation = runtime._send_qq_message(bot, target, "result")
    elif path == "file":
        operation = runtime._send_attachment_path(bot, target, sample)
    else:
        operation = runtime._send_forward_nodes(bot, runtime.ProactiveTarget(target_type="group", target_id="12345", label="group"), [])
    with pytest.raises(Exception) as caught:
        asyncio.run(operation)
    assert caught.value.deferred
    bot.send_group_msg.assert_not_called()
    bot.call_api.assert_not_called()


def test_restart_fallback_preserves_platform_reference_and_topic(runtime, tmp_path):
    from clonoth_sdk.types import DeliveryContext
    service = CommunityService(tmp_path)
    bot = SimpleNamespace(self_id="90000", send_group_msg=AsyncMock(return_value={"message_id": 501}), call_api=AsyncMock(return_value=[]))
    scope = runtime._features.actor(bot, event())["scope"]
    admin = FeatureActor(scope=scope, owner="bot", is_admin=True, role="admin", channel="qq_group")
    service.update_settings(admin, {"topic_enabled": True})
    service.ingest(admin, {"message_id": "400", "text": "unrelated bot reply", "topic_id": "other"})
    calls = []
    async def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/state"):
            return service.state(admin)
        if path == "/v1/community/messages":
            return service.ingest(admin, kwargs["body"])
        return {"recorded": True}
    runtime._client = SimpleNamespace(request_feature=request)
    runtime._last_bot = bot
    runtime._real_conversation_keys[scope] = "qq_group:12345"
    context = DeliveryContext(event_id="final-recovered", conversation_key=scope, topic_id="books", reply_message_id="99", purpose="direct")
    asyncio.run(runtime.TangQiuCallbacks().send_to_channel(scope, "restored answer", [], delivery_context=context))
    bot.send_group_msg.assert_awaited_once()
    topic_call = next(kwargs for path, kwargs in calls if path == "/v1/community/messages")
    assert topic_call["body"]["reply_ref"] == "99" and topic_call["body"]["topic_id"] == "books"
    followup = service.ingest(replace(admin, is_admin=False, owner="reader"), {"message_id": "502", "reply_ref": "501", "text": "follow up"})
    assert followup["topic_id"] == "books"
    assert all(row["text"] != "unrelated bot reply" for row in followup["context"])


def test_member_cannot_reassign_message_to_arbitrary_topic(service, manager):
    service.update_settings(manager, {"topic_enabled": True})
    first = service.ingest(member(manager, 1), {"message_id": "1", "text": "A"})
    second = service.ingest(member(manager, 2), {"message_id": "2", "text": "B", "topic_id": first["topic_id"]})
    assert first["topic_id"] != second["topic_id"]


def test_context_clear_removes_only_topic_state(service, manager):
    service.update_settings(manager, {"topic_enabled": True, "welcome_enabled": True})
    service.update_guide(manager, {"rules": "published rules"})
    service.set_quiet(manager, "silent", 30)
    service.create_activity(manager, {"title": "Keep activity"})
    service.ingest(member(manager, 1), {"message_id": "1", "text": "old context"})
    service.record_decision(replace(manager, message_id="1"), {"action": "task"})
    service.clear_context(manager.scope)
    assert service.decisions(manager) == []
    assert service.state(manager)["quiet"]["mode"] == "silent"
    assert service.state(manager)["guide"]["rules"] == "published rules"
    assert len(service.activities(manager)) == 1
    after = service.ingest(member(manager, 1), {"message_id": "2", "text": "new context", "reply_ref": "1"})
    assert [row["text"] for row in after["context"]] == ["new context"]


def test_adapter_clear_discards_unsubmitted_burst_and_durable_copy(runtime):
    import time
    bot = SimpleNamespace(self_id="90000")
    original = event(message_id=7)
    scope = runtime._features.actor(bot, original)["scope"]
    item = runtime.QueuedInbound(None, bot, original, "qq_group", "qq_group:12345", scope, "old context", [], False,
        {"_route_hints": {"source_message_refs": ["7"]}}, "old context")
    runtime._features.stage_burst(item)
    runtime._features.coordinator.messages[(scope, "7")] = {"topic": {"topic_id": "old"}}
    sent = AsyncMock(return_value=True)
    async def exercise():
        pending = asyncio.create_task(runtime._features.coordinator.submit(item, {"merge_window_sec": 0.1}, (10001,), sent, runtime._merge_queued_inbound))
        await asyncio.sleep(0.005)
        runtime._purge_conversation_side_state(scope, {"type": "group", "group_id": 12345})
        assert await pending is False
        await asyncio.sleep(0.01)
    asyncio.run(exercise())
    sent.assert_not_called()
    assert (scope, "7") not in runtime._features.coordinator.messages
    assert runtime._features.burst_store.recover("90000", time.time() + 1) == []
