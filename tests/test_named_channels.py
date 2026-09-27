"""渠道命名与同家族多实例。

块名从此只是名字，块内的 type 才决定请求按谁的格式发。没写 type 的老配置必须
原封不动照旧工作 —— 这条如果破了，线上那份配置一重载就会拿错格式去打上游。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

import supervisor.admin_api as admin_api
from clonoth_runtime import CONFIG_META_KEYS
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

CONFIG = """\
version: 1
provider: gemini-relay
gemini-relay:
  type: gemini
  label: "Gemini 中转（便宜）"
  base_url: https://relay-a.example
  api_key: sk-relay-aaaaaaaa
  model: gemini-3-pro
gemini-official:
  type: gemini
  base_url: https://generativelanguage.googleapis.com
  api_key: sk-official-bbbbbbbb
  model: gemini-3-flash
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-legacy-cccccccc
  model: gpt-4o-mini
"""


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    path = tmp_path / "data" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-named-channels"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(go())

    return call


class TestTheWireComesFromTypeNotTheBlockName:
    def test_an_explicit_type_wins(self, store):
        assert store.wire_of("gemini-relay") == "gemini"
        assert store.wire_of("gemini-official") == "gemini"

    def test_a_block_without_type_falls_back_to_its_name(self, store):
        # 老配置没有 type 键，块名本来就是家族名。
        assert store.wire_of("openai") == "openai"

    def test_the_engine_is_handed_the_wire_not_the_channel_name(self, store):
        # engine 拿这个字段去 registry 取实现类，给渠道名会查不到、静默回落成 openai。
        secret = store.get_openai_secret()

        assert secret.provider == "gemini"
        assert secret.base_url == "https://relay-a.example"
        assert secret.model == "gemini-3-pro"

    def test_two_channels_can_share_one_wire(self, store):
        providers = store.get_providers_public()["providers"]

        assert providers["gemini-relay"]["type"] == "gemini"
        assert providers["gemini-official"]["type"] == "gemini"
        assert providers["gemini-relay"]["base_url"] != providers["gemini-official"]["base_url"]


class TestThePublicShape:
    def test_a_named_channel_reports_its_label(self, store):
        providers = store.get_providers_public()["providers"]

        assert providers["gemini-relay"]["label"] == "Gemini 中转（便宜）"
        assert providers["gemini-official"]["label"] == ""

    def test_an_inferred_type_is_marked_as_not_explicit(self, store):
        # 界面不该把「没写过 type」显示成「选过 openai」。
        providers = store.get_providers_public()["providers"]

        assert providers["openai"]["type"] == "openai"
        assert providers["openai"]["type_explicit"] is False
        assert providers["gemini-relay"]["type_explicit"] is True

    def test_the_active_channel_is_reported_by_name(self, store):
        assert store.get_providers_public()["active_provider"] == "gemini-relay"

    def test_a_named_channel_can_be_active(self, store):
        # _active_name 以前只认注册过的家族名，自定义名会被回落成 openai。
        assert store.get_public().provider == "gemini-relay"


class TestVisionDefaultsFollowTheWire:
    def test_the_default_is_looked_up_by_wire(self, store):
        # 默认值表按家族登记，自定义渠道名在里面查不到。
        assert store.supports_vision("gemini-relay") is True

    def test_an_explicit_flag_still_wins(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG + "  supports_vision: false\n", encoding="utf-8")

        assert ConfigStore(path=path).supports_vision("openai") is False


class TestWriting:
    def test_creating_a_second_channel_of_the_same_family(self, store):
        store.upsert_provider(
            "gemini-backup", wire="gemini", label="备用",
            base_url="https://relay-b.example", api_key="sk-b", model="gemini-3-pro",
        )

        saved = yaml.safe_load(store.path.read_text(encoding="utf-8"))
        assert saved["gemini-backup"]["type"] == "gemini"
        assert saved["gemini-backup"]["label"] == "备用"
        assert store.wire_of("gemini-backup") == "gemini"

    def test_clearing_the_type_returns_to_guessing_from_the_name(self, store):
        store.upsert_provider("gemini-relay", wire="")

        saved = yaml.safe_load(store.path.read_text(encoding="utf-8"))
        assert "type" not in saved["gemini-relay"]

    def test_untouched_fields_survive_a_partial_update(self, store):
        store.upsert_provider("gemini-relay", model="gemini-3-flash")

        saved = yaml.safe_load(store.path.read_text(encoding="utf-8"))
        assert saved["gemini-relay"]["type"] == "gemini"
        assert saved["gemini-relay"]["label"] == "Gemini 中转（便宜）"
        assert saved["gemini-relay"]["api_key"] == "sk-relay-aaaaaaaa"

    def test_switching_the_active_channel_by_name(self, store):
        store.set_active_provider("gemini-official")

        assert store.get_openai_secret().base_url == "https://generativelanguage.googleapis.com"
        assert store.get_openai_secret().provider == "gemini"


class TestDeletingCleansUpReferences:
    def test_the_active_channel_cannot_be_deleted(self, store):
        with pytest.raises(ValueError):
            store.delete_provider("gemini-relay")

    def test_node_chains_lose_the_deleted_channel_too(self, tmp_path):
        # 只清全局链会在节点链里留下悬空引用：它继承到一份空 url+key，
        # 直到真的降级那一刻才炸。
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG + """\
fallbacks:
  - provider: gemini-official
node_fallbacks:
  qq.orchestrator:
    - provider: gemini-official
    - provider: openai
""", encoding="utf-8")
        store = ConfigStore(path=path)

        store.delete_provider("gemini-official")

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["fallbacks"] == []
        assert saved["node_fallbacks"]["qq.orchestrator"] == [{"provider": "openai"}]


class TestTheAdminApiValidatesTheWireNotTheName:
    _URL = "/v1/config/providers/"

    def test_a_brand_new_name_is_accepted_when_it_declares_a_wire(self, api):
        # 这一条以前是 400：名字必须等于某个注册家族，同家族开第二个渠道无路可走。
        response = api("PUT", self._URL + "gemini-third", headers=_AUTH, json={
            "type": "gemini", "label": "第三个", "base_url": "https://relay-c.example",
        })

        assert response.status_code == 200, response.text
        assert response.json()["providers"]["gemini-third"]["type"] == "gemini"

    def test_an_unknown_wire_is_still_refused(self, api):
        response = api("PUT", self._URL + "whatever", headers=_AUTH, json={"type": "not-a-wire"})

        assert response.status_code == 400
        assert "not-a-wire" in response.text

    def test_an_existing_channel_keeps_its_stored_wire_when_none_is_sent(self, api):
        response = api("PUT", self._URL + "gemini-relay", headers=_AUTH, json={"model": "gemini-3-flash"})

        assert response.status_code == 200, response.text
        assert response.json()["providers"]["gemini-relay"]["type"] == "gemini"

    def test_a_new_name_with_no_wire_at_all_is_refused(self, api):
        # 名字猜不出家族又没人说，放进去只会在发请求时才炸。
        response = api("PUT", self._URL + "my-relay", headers=_AUTH, json={
            "base_url": "https://relay-d.example",
        })

        assert response.status_code == 400
        assert "my-relay" in response.text


RESERVED_SECTIONS = """\
fallbacks:
  - provider: gemini-official
node_fallbacks:
  qq.orchestrator:
    - provider: gemini-official
system_models:
  compact:
    model: gemini-3-flash
image_gen:
  default_channel: gemini-official
platforms:
  qq:
    forward_bridge_token: keep-me
"""


class TestReservedSectionNamesAreNotChannels:
    """渠道块和这几段同在 data/config.yaml 顶层：拿它们当渠道名，保存就改坏那一段，删除就整段没了。"""

    @pytest.mark.parametrize("name", sorted(CONFIG_META_KEYS))
    def test_the_store_refuses_every_write_under_one(self, tmp_path, name):
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG + RESERVED_SECTIONS, encoding="utf-8")
        store = ConfigStore(path=path)
        before = yaml.safe_load(path.read_text(encoding="utf-8"))

        with pytest.raises(ValueError, match="保留段名"):
            store.upsert_provider(name, base_url="https://relay-x.example", wire="openai")
        with pytest.raises(ValueError, match="保留段名"):
            store.delete_provider(name)
        with pytest.raises(ValueError, match="保留段名"):
            store.set_active_provider(name)

        assert yaml.safe_load(path.read_text(encoding="utf-8")) == before

    def test_the_listing_hands_them_to_the_console(self, store):
        assert store.get_providers_public()["reserved_names"] == sorted(CONFIG_META_KEYS)

    def test_the_platforms_section_is_never_listed_as_a_channel(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG + RESERVED_SECTIONS, encoding="utf-8")
        store = ConfigStore(path=path)

        assert "platforms" not in store.get_providers_public()["providers"]
        assert "platforms" not in store.channel_names()

    def test_saving_a_channel_keeps_the_platforms_section(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG + RESERVED_SECTIONS, encoding="utf-8")
        store = ConfigStore(path=path)

        store.upsert_provider("gemini-official", model="gemini-3.5-flash")
        store.set_active_provider("gemini-official")

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["platforms"] == {"qq": {"forward_bridge_token": "keep-me"}}

    def test_the_api_answers_400_and_leaves_the_section_alone(self, api, tmp_path):
        path = tmp_path / "data" / "config.yaml"
        path.write_text(CONFIG + RESERVED_SECTIONS, encoding="utf-8")

        written = api("PUT", "/v1/config/providers/platforms", headers=_AUTH, json={
            "type": "openai", "base_url": "https://relay-x.example",
        })
        removed = api("DELETE", "/v1/config/providers/platforms", headers=_AUTH)
        activated = api("PUT", "/v1/config/active-provider", headers=_AUTH, json={"provider": "platforms"})

        assert (written.status_code, removed.status_code, activated.status_code) == (400, 400, 400)
        assert "保留段名" in written.text
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["platforms"] == {"qq": {"forward_bridge_token": "keep-me"}}


class TestEveryProvidersResponseIsComplete:
    """控制台拿写接口的返回整份替换本地数据：少了 registered，添加渠道那一栏保存一次就消失了。"""

    @pytest.mark.parametrize("method,url,body", [
        ("GET", "/v1/config/providers", None),
        ("PUT", "/v1/config/providers/gemini-official", {"model": "gemini-3.5-flash"}),
        ("DELETE", "/v1/config/providers/openai", None),
        ("PUT", "/v1/config/active-provider", {"provider": "gemini-official"}),
        ("PUT", "/v1/config/fallbacks", {"fallbacks": [{"provider": "gemini-official"}]}),
        ("PUT", "/v1/config/node-fallbacks/qq.orchestrator", {"fallbacks": [{"provider": "gemini-official"}]}),
        ("DELETE", "/v1/config/node-fallbacks/qq.orchestrator", None),
    ])
    def test_it_carries_the_wires_and_the_reserved_names(self, api, method, url, body):
        kwargs: dict[str, Any] = {"headers": _AUTH}
        if body is not None:
            kwargs["json"] = body

        response = api(method, url, **kwargs)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert "gemini" in payload["registered"]
        assert payload["reserved_names"] == sorted(CONFIG_META_KEYS)
