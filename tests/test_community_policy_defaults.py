import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from supervisor.community.api import create_router
from supervisor.community.commands import command
from supervisor.community.service import CommunityService, POLICY_DEFAULTS, POLICY_FIELDS, SettingsConflict
from supervisor.feature_auth import FeatureActor


@pytest.fixture()
def admin():
    return FeatureActor(owner="console:admin", is_admin=True, role="admin", channel="web")


@pytest.fixture()
def group(admin):
    return replace(admin, scope="qq_group:synthetic")


@pytest.fixture()
def service(tmp_path):
    return CommunityService(tmp_path)


def snapshot(service):
    with sqlite3.connect(service.path) as db:
        return tuple(db.iterdump())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    def verify(request):
        if request.headers.get("Authorization") != "Bearer synthetic-admin":
            raise HTTPException(401, "Unauthorized")
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", verify)
    state = SimpleNamespace(workspace_root=tmp_path, _lock=RLock(), tasks={}, _task_terminal=lambda task: False)
    app = FastAPI()
    app.include_router(create_router(state))
    with TestClient(app, headers={"Authorization": "Bearer synthetic-admin"}) as client:
        yield client, state


def test_defaults_resource_is_independent_from_console_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda request: None)
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(workspace_root=tmp_path)))
    with TestClient(app) as client:
        response = client.get("/v1/community/settings/defaults")
        assert response.status_code == 200
        assert response.json()["values"] == {}
        assert response.json()["settings"]["topic_enabled"] is False


def test_state_describes_inheritance_without_migrating_console(service, admin, group):
    service.update_settings(replace(admin, scope="web:console"), {"topic_enabled": True})
    state = service.state(group)
    assert state["defaults"]["topic_enabled"] is False
    assert state["overrides"] == {}
    assert "topic_enabled" in state["inherited_fields"]


def test_instance_default_applies_until_false_override_is_cleared(service, admin, group):
    service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
    state = service.state(group)
    assert state["settings"]["topic_enabled"] is True
    changed = service.update_overrides(group, {"values": {"topic_enabled": False}, "expected_revision": 0, "expected_defaults_revision": 1})
    assert changed["overrides"] == {"topic_enabled": False}
    assert changed["settings"]["topic_enabled"] is False
    reset = service.update_overrides(group, {"reset_fields": ["topic_enabled"], "expected_revision": changed["revision"], "expected_defaults_revision": 1})
    assert reset["overrides"] == {}
    assert reset["settings"]["topic_enabled"] is True


def test_global_merge_pair_checks_existing_partial_override(service, admin, group):
    service.update_settings(group, {"merge_max_wait_sec": 1})
    with pytest.raises(ValueError, match="覆盖"):
        service.update_defaults(admin, {"values": {"merge_window_sec": 2}, "expected_revision": 0})
    assert service.defaults(admin)["revision"] == 0


def test_group_features_reject_console_but_private_quiet_remains(service, admin):
    console = replace(admin, scope="web:console")
    with pytest.raises(ValueError, match="QQ 群"):
        service.create_activity(console, {"title": "synthetic"})
    private = replace(admin, scope="qq_private:synthetic")
    assert service.set_quiet(private, "listen", 60)["quiet"]["mode"] == "listen"


def test_global_defaults_never_apply_to_or_get_blocked_by_old_web_scopes(service, admin, group):
    for scope in ("web:console", "web:legacy", "legacy-web", "qq_group:", "qq_private:"):
        service.update_settings(replace(admin, scope=scope), {"merge_max_wait_sec": 0, "topic_enabled": True})
    before = service.state(replace(admin, scope="web:console"))
    service.update_defaults(admin, {"values": {"merge_window_sec": 2, "topic_enabled": True}, "expected_revision": 0})
    assert service.state(group)["settings"]["merge_window_sec"] == 2
    assert service.state(replace(admin, scope="web:console")) == before
    assert service.state(replace(admin, scope="web:new"))["settings"]["topic_enabled"] is False


def test_private_conversation_inherits_same_instance_defaults(service, admin):
    service.update_defaults(admin, {"values": {"topic_enabled": True, "response_policy_enabled": True}, "expected_revision": 0})
    private = replace(admin, scope="qq_private:synthetic")
    assert service.state(private)["settings"]["topic_enabled"] is True
    assert service.state(private)["defaults_revision"] == 1


def test_instances_and_restart_have_independent_persistent_defaults(tmp_path, admin, group):
    first, second = CommunityService(tmp_path / "a"), CommunityService(tmp_path / "b")
    first.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
    assert second.defaults(admin)["settings"]["topic_enabled"] is False
    assert CommunityService(tmp_path / "a").state(group)["settings"]["topic_enabled"] is True
    assert CommunityService(tmp_path / "b").state(group)["settings"]["topic_enabled"] is False


def test_old_database_keeps_all_explicit_values_and_does_not_lower_future_version(tmp_path, admin, group):
    path = tmp_path / "data" / "community.sqlite3"
    path.parent.mkdir()
    stored = {"response_policy_enabled": False, "merge_window_sec": 0, "welcome_enabled": True}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE groups(scope TEXT PRIMARY KEY,settings TEXT NOT NULL DEFAULT '{}',guide TEXT NOT NULL DEFAULT '{}',quiet TEXT NOT NULL DEFAULT '{}',revision INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO groups(scope,settings,revision) VALUES (?,?,?)", (group.scope, json.dumps(stored), 7))
        db.execute("PRAGMA user_version=1")
    service = CommunityService(tmp_path)
    service.update_defaults(admin, {"values": {"response_policy_enabled": True, "merge_window_sec": 2}, "expected_revision": 0})
    state = service.state(group)
    assert state["revision"] == 7
    assert state["overrides"] == {"response_policy_enabled": False, "merge_window_sec": 0}
    assert state["settings"]["welcome_enabled"] is True
    assert "welcome_enabled" not in state["defaults"]
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT settings FROM groups WHERE scope=?", (group.scope,)).fetchone()[0]) == stored
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        db.execute("PRAGMA user_version=99")
    CommunityService(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_reset_all_overrides_preserves_group_guide_quiet_welcome_and_activities(service, admin, group):
    service.update_settings(group, {**POLICY_DEFAULTS, "welcome_enabled": True})
    service.update_guide(group, {"rules": "synthetic rules"})
    service.set_quiet(group, "listen", 60)
    service.create_activity(group, {"title": "synthetic activity"})
    before = service.state(group)
    result = service.update_overrides(group, {"reset_fields": list(POLICY_FIELDS), "expected_revision": before["revision"], "expected_defaults_revision": 0})
    assert result["overrides"] == {}
    assert set(result["inherited_fields"]) == set(POLICY_FIELDS)
    assert result["settings"]["welcome_enabled"] is True
    assert result["guide"] == before["guide"] and result["quiet"] == before["quiet"]
    assert len(service.activities(group)) == 1


def test_default_reset_uses_builtin_values_and_revision_tracks_sparse_map(service, admin):
    current = service.update_defaults(admin, {"values": {"topic_enabled": False, "merge_window_sec": 0}, "expected_revision": 0})
    assert current["revision"] == 1
    assert current["values"] == {"topic_enabled": False, "merge_window_sec": 0}
    unchanged = service.update_defaults(admin, {"values": current["values"], "expected_revision": 1})
    assert unchanged == current
    reset = service.update_defaults(admin, {"reset_fields": list(POLICY_FIELDS), "expected_revision": 1})
    assert reset == {"values": {}, "settings": POLICY_DEFAULTS, "revision": 2}
    assert service.update_defaults(admin, {"reset_fields": list(POLICY_FIELDS), "expected_revision": 2}) == reset


def test_single_field_reset_validates_effective_pair_atomically(service, admin, group):
    service.update_defaults(admin, {"values": {"merge_window_sec": 3, "merge_max_wait_sec": 4}, "expected_revision": 0})
    service.update_overrides(group, {"values": {"merge_window_sec": 0, "merge_max_wait_sec": 1}, "expected_revision": 0, "expected_defaults_revision": 1})
    before = snapshot(service)
    with pytest.raises(ValueError, match="最长等待"):
        service.update_overrides(group, {"reset_fields": ["merge_window_sec"], "expected_revision": 1, "expected_defaults_revision": 1})
    assert snapshot(service) == before
    reset = service.update_overrides(group, {"reset_fields": ["merge_window_sec", "merge_max_wait_sec"], "expected_revision": 1, "expected_defaults_revision": 1})
    assert reset["settings"]["merge_window_sec"] == 3
    assert reset["settings"]["merge_max_wait_sec"] == 4


def test_global_pair_conflict_rolls_back_all_values_without_clamping(service, admin, group):
    service.update_settings(group, {"merge_window_sec": 3})
    before = snapshot(service)
    with pytest.raises(ValueError, match="覆盖冲突"):
        service.update_defaults(admin, {"values": {"merge_max_wait_sec": 2, "topic_enabled": True}, "expected_revision": 0})
    assert snapshot(service) == before
    assert service.state(group)["settings"]["merge_window_sec"] == 3


def test_legacy_flat_settings_use_inherited_pair_and_accept_legacy_numeric_strings(service, admin, group):
    service.update_defaults(admin, {"values": {"merge_window_sec": 2, "merge_max_wait_sec": 6}, "expected_revision": 0})
    with pytest.raises(ValueError, match="最长等待"):
        service.update_settings(group, {"merge_max_wait_sec": "1.5"})
    state = service.update_settings(group, {"merge_window_sec": "1.5", "merge_max_wait_sec": "2.5"})
    assert state["overrides"] == {"merge_window_sec": 1.5, "merge_max_wait_sec": 2.5}


@pytest.mark.parametrize("scope", ["web:console", "qq_private:x", "qq_group:"])
def test_legacy_false_welcome_is_noop_outside_groups_but_true_is_rejected(service, admin, scope):
    actor = replace(admin, scope=scope)
    before = snapshot(service)
    assert service.update_settings(actor, {"welcome_enabled": False})["settings"]["welcome_enabled"] is False
    assert snapshot(service) == before
    with pytest.raises(ValueError, match="QQ 群"):
        service.update_settings(actor, {"welcome_enabled": True})
    assert snapshot(service) == before


@pytest.mark.parametrize("values", [
    {"welcome_enabled": True}, {"unexpected": True}, {"topic_enabled": 1}, {"response_policy_enabled": "true"},
    {"reply_budget_per_minute": True}, {"reply_budget_per_minute": 0}, {"reply_budget_per_minute": 61}, {"reply_budget_per_minute": 1.5},
    {"merge_window_sec": True}, {"merge_window_sec": "1.5"}, {"merge_window_sec": float("nan")},
    {"merge_window_sec": float("inf")}, {"merge_max_wait_sec": -1}, {"merge_max_wait_sec": 11},
    {"merge_window_sec": None}, {"merge_max_wait_sec": 10 ** 1000},
])
def test_new_policy_values_are_strict_and_failure_is_atomic(service, admin, group, values):
    before = snapshot(service)
    with pytest.raises(ValueError):
        service.update_defaults(admin, {"values": values, "expected_revision": 0})
    with pytest.raises(ValueError):
        service.update_overrides(group, {"values": values, "expected_revision": 0, "expected_defaults_revision": 0})
    assert snapshot(service) == before


@pytest.mark.parametrize("body", [
    [], {"unknown": True, "expected_revision": 0}, {"values": [], "expected_revision": 0},
    {"expected_revision": True}, {"expected_revision": "0"}, {"expected_revision": -1}, {},
    {"reset_fields": "topic_enabled", "expected_revision": 0},
    {"reset_fields": ["topic_enabled", "topic_enabled"], "expected_revision": 0},
    {"reset_fields": ["welcome_enabled"], "expected_revision": 0},
    {"reset_fields": [None], "expected_revision": 0},
    {"values": {"topic_enabled": False}, "reset_fields": ["topic_enabled"], "expected_revision": 0},
])
def test_patch_shape_is_strict_and_does_not_write(service, admin, body):
    before = snapshot(service)
    with pytest.raises(ValueError):
        service.update_defaults(admin, body)
    assert snapshot(service) == before


def test_both_local_and_global_revisions_are_checked(service, admin, group):
    service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
    service.update_settings(group, {"topic_enabled": False})
    before = snapshot(service)
    for local, global_revision in [(0, 1), (1, 0)]:
        with pytest.raises(SettingsConflict):
            service.update_overrides(group, {"values": {"topic_enabled": True}, "expected_revision": local, "expected_defaults_revision": global_revision})
    with pytest.raises(ValueError):
        service.update_overrides(group, {"expected_revision": 1, "expected_defaults_revision": True})
    assert snapshot(service) == before


def test_concurrent_default_writers_have_one_winner(service, admin):
    def write(value):
        try:
            return service.update_defaults(admin, {"values": {"reply_budget_per_minute": value}, "expected_revision": 0})
        except SettingsConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [2, 3]))
    assert results.count("conflict") == 1
    assert service.defaults(admin)["revision"] == 1
    assert service.defaults(admin)["settings"]["reply_budget_per_minute"] in {2, 3}


def test_state_uses_one_read_snapshot_for_defaults_and_local_revision(service, tmp_path, admin, group, monkeypatch):
    writer = CommunityService(tmp_path)
    original = service._defaults_in
    def read_and_write(db):
        result = original(db)
        writer.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
        writer.update_overrides(group, {"values": {"reply_budget_per_minute": 2}, "expected_revision": 0, "expected_defaults_revision": 1})
        return result
    monkeypatch.setattr(service, "_defaults_in", read_and_write)
    state = service.state(group)
    assert state["defaults_revision"] == 0 and state["revision"] == 0
    assert state["settings"]["topic_enabled"] is False
    assert state["settings"]["reply_budget_per_minute"] == 6
    assert state["overrides"] == {}


@pytest.mark.parametrize("raw", ["{broken", "[]", '{"topic_enabled":"false"}', '{"merge_window_sec":"1.0"}'])
def test_damaged_saved_qq_settings_are_not_silently_broadcast_or_overwritten(service, admin, group, raw):
    with service._db(True) as db:
        db.execute("INSERT INTO groups(scope,settings) VALUES (?,?)", (group.scope, raw))
    before = snapshot(service)
    with pytest.raises(ValueError):
        service.state(group)
    with pytest.raises(ValueError):
        service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
    assert snapshot(service) == before


@pytest.mark.parametrize("location", ["local", "defaults"])
@pytest.mark.parametrize("action", ["quiet", "guide"])
def test_failed_state_readback_rolls_back_quiet_and_guide(service, group, location, action):
    with service._db(True) as db:
        if location == "local":
            db.execute("INSERT INTO groups(scope,settings) VALUES (?,?)", (group.scope, '{"topic_enabled":"false"}'))
        else:
            db.execute("UPDATE conversation_defaults SET settings='[]' WHERE id=1")
    before = snapshot(service)
    with pytest.raises(ValueError):
        if action == "quiet":
            service.set_quiet(group, "silent", 60)
        else:
            service.update_guide(group, {"rules": "synthetic rule"})
    assert snapshot(service) == before


def test_legacy_false_welcome_clears_a_historical_private_flag_without_creating_an_override(service, admin):
    actor = replace(admin, scope="qq_private:legacy")
    with service._db(True) as db:
        db.execute("INSERT INTO groups(scope,settings) VALUES (?,?)", (actor.scope, '{"welcome_enabled":true}'))
    state = service.update_settings(actor, {"welcome_enabled": False})
    assert state["settings"]["welcome_enabled"] is False
    assert state["overrides"] == {}
    with service._db() as db:
        assert json.loads(db.execute("SELECT settings FROM groups WHERE scope=?", (actor.scope,)).fetchone()[0]) == {}


@pytest.mark.parametrize("actor_changes", [
    {"channel": "qq_group"}, {"is_admin": False}, {"interactive": False}, {"task_id": "model-task"}, {"owner": "adapter:user"},
])
def test_defaults_service_requires_interactive_console_admin(service, admin, actor_changes):
    actor = replace(admin, **actor_changes)
    with pytest.raises(HTTPException) as error:
        service.defaults(actor)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        service.update_defaults(actor, {"values": {"topic_enabled": True}, "expected_revision": 0})


def test_defaults_api_and_overrides_conflicts_are_explicit(client):
    client, _ = client
    assert client.get("/v1/community/settings/defaults", headers={"Authorization": "bad"}).status_code == 401
    saved = client.patch("/v1/community/settings/defaults", json={"values": {"topic_enabled": True}, "expected_revision": 0})
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert client.patch("/v1/community/settings/defaults", json={"values": {"topic_enabled": False}, "expected_revision": 0}).status_code == 409
    wrong = client.patch("/v1/community/settings/overrides?scope=qq_group:x", json={"values": {"topic_enabled": False}, "expected_revision": 0, "expected_defaults_revision": 0})
    assert wrong.status_code == 409
    valid = client.patch("/v1/community/settings/overrides?scope=qq_group:x", json={"values": {"topic_enabled": False}, "expected_revision": 0, "expected_defaults_revision": 1})
    assert valid.status_code == 200 and valid.json()["overrides"] == {"topic_enabled": False}


@pytest.mark.parametrize("channel", ["qq_group", "qq_private", "web"])
def test_adapter_cannot_impersonate_web_admin_to_manage_defaults(client, channel):
    client, _ = client
    headers = {"X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:x", "user_id": "12345", "channel": channel, "role": "admin", "is_admin": True})}
    assert client.get("/v1/community/settings/defaults", headers=headers).status_code == 403
    assert client.patch("/v1/community/settings/defaults", headers=headers, json={"values": {"topic_enabled": True}, "expected_revision": 0}).status_code == 403
    assert client.get("/v1/community/settings/defaults").json()["revision"] == 0


def test_model_task_cannot_use_defaults_api_even_when_bound_to_admin(client):
    client, state = client
    state.tasks["task"] = SimpleNamespace(cancel_requested=False, input={"task_context": {"conversation_key": "qq_group:x", "channel": "web", "platform_auth": {"is_admin": True}}})
    headers = {"X-Clonoth-Task-Id": "task"}
    assert client.get("/v1/community/settings/defaults", headers=headers).status_code == 403
    assert client.patch("/v1/community/settings/defaults", headers=headers, json={"values": {"topic_enabled": True}, "expected_revision": 0}).status_code == 403


def test_group_manager_can_override_only_own_scope_and_member_cannot(client):
    client, _ = client
    raw = {"scope": "qq_group:one", "user_id": "12345", "channel": "qq_group", "role": "admin"}
    headers = {"X-Clonoth-Adapter-Actor": json.dumps(raw)}
    body = {"values": {"topic_enabled": True}, "expected_revision": 0, "expected_defaults_revision": 0}
    assert client.patch("/v1/community/settings/overrides?scope=qq_group:two", headers=headers, json=body).status_code == 403
    assert client.patch("/v1/community/settings/overrides", headers=headers, json=body).status_code == 200
    headers["X-Clonoth-Adapter-Actor"] = json.dumps({**raw, "role": "member", "user_id": "12346"})
    assert client.patch("/v1/community/settings/overrides", headers=headers, json={**body, "expected_revision": 1}).status_code == 403


@pytest.mark.parametrize("scope", ["web:console", "qq_private:x", "qq_group:"])
def test_all_group_only_service_paths_reject_non_groups(service, admin, scope):
    actor = replace(admin, scope=scope)
    actions = [
        lambda: service.guide(actor), lambda: service.update_guide(actor, {"rules": "x"}),
        lambda: service.activities(actor), lambda: service.create_activity(actor, {"title": "x"}),
        lambda: service.activity_action(actor, "synthetic", "view", {}),
        lambda: service.notice(actor, {"notice_type": "group_increase"}),
        lambda: service.notifications(actor), lambda: service.acknowledge_notification(actor, "synthetic", "1"),
    ]
    for action in actions:
        with pytest.raises(ValueError, match="QQ 群"):
            action()
    for text in ("/群规", "/群资料", "/常见问题", "/活动列表", "/投票创建 x | a | b"):
        with pytest.raises(ValueError, match="QQ 群"):
            command(service, actor, {"text": text})


def test_private_state_quiet_messages_and_decisions_remain_available(service, admin):
    actor = replace(admin, scope="qq_private:x", message_id="1")
    service.update_settings(actor, {"topic_enabled": True, "welcome_enabled": False})
    assert service.state(actor)["settings"]["topic_enabled"] is True
    assert command(service, actor, {"text": "/旁听 1分钟"})["control"] is True
    assert service.ingest(actor, {"message_id": "1", "text": "synthetic"})["topic_id"]
    service.record_decision(actor, {"action": "reply"})
    assert service.decisions(actor)[0]["action"] == "reply"


def test_guide_api_matches_state_for_group_but_rejects_console(client):
    client, _ = client
    group = client.get("/v1/community/guide?scope=qq_group:x")
    assert group.status_code == 200
    assert group.json() == client.get("/v1/community/state?scope=qq_group:x").json()
    assert client.get("/v1/community/guide?scope=web:console").status_code == 422
    assert client.get("/v1/community/state?scope=qq_private:x").status_code == 200


def test_tool_guide_uses_group_specific_endpoint(monkeypatch):
    from toolbox.builtins import community as module
    request = AsyncMock(return_value={"guide": {}, "settings": {}})
    monkeypatch.setattr(module, "request", request)
    context = object()
    asyncio.run(module.community({"operation": "guide"}, context))
    request.assert_awaited_once_with(context, "GET", "/v1/community/guide")


def test_orphaned_policy_scope_remains_discoverable_after_conversation_removal(client):
    client, state = client
    scope = "qq_group:synthetic-orphan"
    actor = FeatureActor(scope=scope, owner="console:admin", is_admin=True, role="admin", channel="web")
    state.community.update_settings(actor, {"merge_max_wait_sec": 1})
    directory = state.workspace_root / "data" / "conversations"
    directory.mkdir()
    conversation = directory / "synthetic.jsonl"
    conversation.write_text('{"synthetic":"message"}\n', encoding="utf-8")
    state.community.clear_context(scope)
    conversation.unlink()
    assert list(directory.iterdir()) == []
    response = client.get("/v1/community/settings/scopes")
    assert response.status_code == 200
    assert [item["scope"] for item in response.json()["items"]] == [scope]
    blocked = client.patch("/v1/community/settings/defaults", json={"values": {"merge_window_sec": 2}, "expected_revision": 0})
    assert blocked.status_code == 422
    local = client.get("/v1/community/state", params={"scope": scope}).json()
    reset = client.patch("/v1/community/settings/overrides", params={"scope": scope}, json={"reset_fields": ["merge_max_wait_sec"], "expected_revision": local["revision"], "expected_defaults_revision": local["defaults_revision"]})
    assert reset.status_code == 200
    assert client.get("/v1/community/settings/scopes").json()["items"] == []
    assert client.patch("/v1/community/settings/defaults", json={"values": {"merge_window_sec": 2}, "expected_revision": 0}).status_code == 200


def test_policy_scope_directory_lists_only_explicit_qq_policy_keys(service, admin):
    for scope, settings in {
        "qq_group:false": {"topic_enabled": False},
        "qq_private:zero": {"merge_window_sec": 0},
        "qq_group:welcome": {"welcome_enabled": True},
        "web:console": {"topic_enabled": True},
        "web:another": {"merge_window_sec": 0},
    }.items():
        service.update_settings(replace(admin, scope=scope), settings)
    service.set_quiet(replace(admin, scope="qq_private:quiet"), "listen", 60)
    service.update_guide(replace(admin, scope="qq_group:guide"), {"rules": "synthetic"})
    with service._db(True) as db:
        db.execute("INSERT INTO groups(scope,settings) VALUES ('web:broken', '{broken')")
    before = snapshot(service)
    assert service.settings_scopes(admin) == ["qq_group:false", "qq_private:zero"]
    assert snapshot(service) == before


def test_policy_scope_directory_is_workspace_local(tmp_path, admin):
    first, second = CommunityService(tmp_path / "first"), CommunityService(tmp_path / "second")
    first.update_settings(replace(admin, scope="qq_group:first"), {"topic_enabled": False})
    second.update_settings(replace(admin, scope="qq_private:second"), {"merge_window_sec": 0})
    assert first.settings_scopes(admin) == ["qq_group:first"]
    assert second.settings_scopes(admin) == ["qq_private:second"]


def test_policy_scope_names_do_not_read_message_files_and_retain_history_flags(client, monkeypatch):
    client, state = client
    group, private, unknown = "qq_group:" + "a" * 24, "qq_private:" + "b" * 24, "qq_group:" + "c" * 24
    actor = FeatureActor(owner="console:admin", is_admin=True, role="admin", channel="web")
    for scope in (group, private, unknown):
        state.community.update_settings(replace(actor, scope=scope), {"topic_enabled": False})
    fixtures = {
        "onebot_plugin_state.json": {"real_conversation_keys": {group: "qq_group:101", private: "qq_private:202"}},
        "onebot_anon_map.json": {"groups": {"101": {"alias": "GroupA"}}, "users": {"202": {"alias": "UserA"}}},
        "qq_live_state.json": {"group_names": {"101": "合成群名"}, "scope_conversation_keys": [group]},
        "memory_subjects.json": {"subjects": {"UserA": {"display_name": "合成联系人"}}},
    }
    for name, value in fixtures.items():
        (state.workspace_root / "data" / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    original = Path.read_text
    reads = []
    def read_text(path, *args, **kwargs):
        assert path.suffix != ".jsonl"
        reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    response = client.get("/v1/community/settings/scopes")
    assert response.status_code == 200
    rows = {item["scope"]: item for item in response.json()["items"]}
    assert rows[group]["owner"]["label"] == "合成群名" and rows[group]["owner"]["name_available"] is True
    assert rows[group]["current_account"] is True
    assert rows[private]["owner"]["label"] == "合成联系人" and rows[private]["current_account"] is False
    assert rows[unknown]["owner"]["label"] == "群聊（名称暂不可用）"
    assert rows[unknown]["owner"]["name_available"] is False and rows[unknown]["current_account"] is False
    assert "sessions.json" in reads
    assert all(name.endswith(".json") for name in reads)


def test_policy_scope_missing_names_and_unknown_account_get_explicit_fallback(client):
    client, state = client
    actor = FeatureActor(scope="qq_private:synthetic", owner="console:admin", is_admin=True, role="admin", channel="web")
    state.community.update_settings(actor, {"merge_window_sec": 0})
    item = client.get("/v1/community/settings/scopes").json()["items"][0]
    assert item["scope"] == actor.scope
    assert item["current_account"] is True
    assert item["owner"]["name_available"] is False
    assert item["owner"]["label"] == "私聊（名称暂不可用）"


def test_policy_scope_directory_denies_adapter_task_and_unauthenticated_reads(client, monkeypatch):
    client, state = client
    def no_names(*args, **kwargs):
        raise AssertionError("unauthorized request reached display metadata")
    monkeypatch.setattr("supervisor.community.api.describe_namespaces", no_names)
    endpoint = "/v1/community/settings/scopes"
    assert client.get(endpoint, headers={"Authorization": "bad"}).status_code == 401
    for channel in ("web", "qq_group", "qq_private"):
        headers = {"X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:synthetic", "user_id": "12345", "channel": channel, "is_admin": True})}
        assert client.get(endpoint, headers=headers).status_code == 403
    state.tasks["task"] = SimpleNamespace(cancel_requested=False, input={"task_context": {"conversation_key": "qq_group:x", "channel": "web", "platform_auth": {"is_admin": True}}})
    assert client.get(endpoint, headers={"X-Clonoth-Task-Id": "task"}).status_code == 403


def test_policy_scope_directory_rejects_malformed_qq_json_instead_of_claiming_empty(service, admin):
    with service._db(True) as db:
        db.execute("INSERT INTO groups(scope,settings) VALUES ('qq_group:broken', '{broken')")
    before = snapshot(service)
    with pytest.raises(ValueError, match="损坏"):
        service.settings_scopes(admin)
    assert snapshot(service) == before
