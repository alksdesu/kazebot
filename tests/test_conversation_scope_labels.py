from __future__ import annotations

import json
import hashlib
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import supervisor.admin_api as admin_api
import supervisor.materials_api as materials_api
from engine.materials import MaterialService
from supervisor.conversation_labels import describe_namespaces, memory_namespace
from supervisor.feature_auth import FeatureActor

GROUP = "qq_group:" + "a" * 24
PRIVATE = "qq_private:" + "b" * 24
UNKNOWN = "qq_group:" + "c" * 24
TOKEN = "scope-label-test-token"


def seed_names(root: Path, group_name: str = "测试群", person_name: str = "联系人甲") -> None:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    documents = {
        "onebot_plugin_state.json": {"real_conversation_keys": {GROUP: "qq_group:101", PRIVATE: "qq_private:202"}},
        "onebot_anon_map.json": {"groups": {"101": {"alias": "GroupA"}}, "users": {"202": {"alias": "UserA"}}},
        "qq_live_state.json": {"group_names": {"101": group_name}, "scope_conversation_keys": [GROUP]},
        "memory_subjects.json": {"subjects": {"UserA": {"display_name": person_name}}},
        "sessions.json": {"known": {"conversation_key": UNKNOWN}},
    }
    for name, value in documents.items():
        (data / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_unknown_group_has_an_explicit_missing_name_label(tmp_path: Path):
    seed_names(tmp_path)
    owner = describe_namespaces(tmp_path)[memory_namespace(UNKNOWN)]
    assert owner["label"] == "群聊（名称暂不可用）"
    assert owner["real_id"] == ""
    assert owner["conversation_key"] == UNKNOWN
    assert owner["name_available"] is False


def test_explicit_material_scope_without_session_keeps_original_identity(tmp_path: Path):
    key = "qq_private:" + "d" * 24
    owner = describe_namespaces(tmp_path, conversation_keys=[key])[memory_namespace(key)]
    assert owner["label"] == "私聊（名称暂不可用）"
    assert owner["conversation_key"] == key
    assert owner["name_available"] is False
    assert not (tmp_path / "data").exists()


def test_bad_alias_entry_does_not_break_known_group_name(tmp_path: Path):
    seed_names(tmp_path)
    (tmp_path / "data/onebot_anon_map.json").write_text(json.dumps({"groups": {"101": "old-invalid-entry"}}))
    assert describe_namespaces(tmp_path)[memory_namespace(GROUP)]["label"] == "测试群"


def test_workspace_name_resolution_does_not_share_names(tmp_path: Path):
    first, second = tmp_path / "first", tmp_path / "second"
    seed_names(first, "甲实例群", "甲实例联系人")
    seed_names(second, "乙实例群", "乙实例联系人")
    for root, group, person in [(first, "甲实例群", "甲实例联系人"), (second, "乙实例群", "乙实例联系人")]:
        labels = describe_namespaces(root)
        assert labels[memory_namespace(GROUP)]["label"] == group
        assert labels[memory_namespace(PRIVATE)]["label"] == person


@pytest.mark.parametrize("name", ["GroupA", "qq_group:兴趣小组"])
def test_real_names_are_distinguished_from_technical_fallbacks(tmp_path: Path, name: str):
    seed_names(tmp_path, name)
    owner = describe_namespaces(tmp_path)[memory_namespace(GROUP)]
    assert owner["label"] == name
    assert owner["name_available"] is True


def test_malformed_name_metadata_uses_known_aliases(tmp_path: Path):
    seed_names(tmp_path)
    (tmp_path / "data/qq_live_state.json").write_text(json.dumps({"group_names": {"101": {"wrong": "shape"}}}))
    (tmp_path / "data/memory_subjects.json").write_text(json.dumps({"subjects": {"UserA": {"display_name": ["wrong"]}}}))
    owners = describe_namespaces(tmp_path)
    assert owners[memory_namespace(GROUP)]["label"] == "GroupA"
    assert owners[memory_namespace(PRIVATE)]["label"] == "UserA"
    assert owners[memory_namespace(GROUP)]["name_available"] is False
    assert owners[memory_namespace(PRIVATE)]["name_available"] is False


@pytest.fixture
def material_apps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(admin_api, "_admin_token", TOKEN)
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    services = []

    def make(name: str = "main", group_name: str = "测试群"):
        root = tmp_path / name
        seed_names(root, group_name)
        service = MaterialService(root)
        services.append(service)
        state = SimpleNamespace(workspace_root=root, materials=service)
        for scope, bot in [(GROUP, "bot-a"), (GROUP, "bot-b"), (PRIVATE, "bot-a"), (UNKNOWN, "bot-a"), ("web:console", "")]:
            service.store.save_settings(FeatureActor(scope=scope, owner="admin", is_admin=True, bot_scope=bot), {})
        app = FastAPI()
        app.include_router(materials_api.create_router(state))
        return root, service, app

    yield make
    for service in services:
        service.close()


@pytest.mark.asyncio
async def test_material_scope_names_preserve_bot_partition_and_old_account_flag(material_apps):
    _, _, app = material_apps()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    rows = {(row["scope"], row["bot_scope"]): row for row in response.json()}
    assert len(rows) == 5
    for bot in ["bot-a", "bot-b"]:
        assert rows[GROUP, bot]["owner"]["label"] == "测试群"
        assert rows[GROUP, bot]["current_account"] is True
    assert rows[PRIVATE, "bot-a"]["owner"]["label"] == "联系人甲"
    assert rows[PRIVATE, "bot-a"]["current_account"] is False
    assert rows[UNKNOWN, "bot-a"]["owner"]["label"] == "群聊（名称暂不可用）"
    assert rows["web:console", ""]["owner"] is None
    assert rows["web:console", ""]["current_account"] is True


@pytest.mark.asyncio
async def test_material_scope_names_refresh_without_migrating_records(material_apps):
    root, service, app = material_apps()
    before = [tuple(row) for row in service.store.db.execute("SELECT * FROM settings ORDER BY scope,bot_scope")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})).json()
        seed_names(root, "修改后的群名")
        second = (await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})).json()
    assert next(row for row in first if row["scope"] == GROUP)["owner"]["label"] == "测试群"
    assert next(row for row in second if row["scope"] == GROUP)["owner"]["label"] == "修改后的群名"
    after = [tuple(row) for row in service.store.db.execute("SELECT * FROM settings ORDER BY scope,bot_scope")]
    assert after == before


@pytest.mark.asyncio
async def test_material_scope_names_stay_in_the_requested_workspace(material_apps):
    for name in ["甲", "乙"]:
        _, _, app = material_apps(name, name + "的群")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rows = (await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})).json()
        assert all(row["owner"]["label"] == name + "的群" for row in rows if row["scope"] == GROUP)


@pytest.mark.asyncio
async def test_material_scope_names_require_authentication(material_apps):
    _, _, app = material_apps()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/materials/scopes")
    assert response.status_code == 401
    assert "测试群" not in response.text
    assert "联系人甲" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin,task_id", [(False, ""), (True, "task-test")])
async def test_non_console_actors_do_not_receive_contact_metadata(material_apps, monkeypatch, is_admin, task_id):
    _, _, app = material_apps()
    monkeypatch.setattr(materials_api, "resolve_actor", lambda *_: FeatureActor(scope=GROUP, bot_scope="bot-a", owner="actor", is_admin=is_admin, task_id=task_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.json() == [{"scope": GROUP, "bot_scope": "bot-a"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,interactive", [("qq_group", True), ("qq_private", True), ("web", False)])
async def test_non_interactive_console_admins_keep_legacy_scope_rows(material_apps, monkeypatch, channel, interactive):
    _, _, app = material_apps()
    monkeypatch.setattr(materials_api, "resolve_actor", lambda *_: FeatureActor(scope=GROUP, bot_scope="bot-a", owner="actor", is_admin=True, channel=channel, interactive=interactive))

    def unexpected_metadata(*args, **kwargs):
        raise AssertionError("Contact metadata must not be loaded for this actor")

    monkeypatch.setattr(materials_api, "describe_namespaces", unexpected_metadata)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/materials/scopes", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert len(response.json()) == 5
    assert all(set(row) == {"scope", "bot_scope"} for row in response.json())


@pytest.mark.asyncio
async def test_explicit_empty_material_partition_differs_from_omitted_partition(material_apps):
    root, service, app = material_apps()
    default_bot = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:24]
    attachment = root / "data/attachments/test.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("Synthetic partition evidence", encoding="utf-8")
    for bot, name in [(default_bot, "默认分区"), ("", "旧空分区")]:
        service.register_source(FeatureActor(scope=GROUP, owner="admin", is_admin=True, bot_scope=bot), {"path": "data/attachments/test.txt", "name": name})
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        default = await client.get("/v1/materials/sources", params={"scope": GROUP}, headers=headers)
        empty = await client.get("/v1/materials/sources", params={"scope": GROUP, "bot_scope": ""}, headers=headers)
        assert default.status_code == empty.status_code == 200
        assert [row["name"] for row in default.json()] == ["默认分区"]
        assert [row["name"] for row in empty.json()] == ["旧空分区"]
        await client.put("/v1/materials/settings", params={"scope": GROUP, "bot_scope": ""}, headers=headers, json={"journal_enabled": True})
        assert (await client.get("/v1/materials/settings", params={"scope": GROUP, "bot_scope": ""}, headers=headers)).json()["journal_enabled"] is True
        assert (await client.get("/v1/materials/settings", params={"scope": GROUP}, headers=headers)).json()["journal_enabled"] is False
