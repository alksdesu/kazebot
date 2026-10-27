"""活跃渠道解析。provider 这个键以前没人读，现在决定 engine 用哪套请求格式。"""
from __future__ import annotations

import asyncio

import pytest

from clonoth_runtime import normalize_openai_secret
from engine.model import resolve_provider
from engine.node import Node
from supervisor.config_store import ConfigStore
from supervisor.types import OpenAIConfigUpdateIn

CONFIG = """version: 1
provider: openai
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-openai-key
  model: gpt-5.4
anthropic:
  base_url: https://api.anthropic.com
  api_key: sk-ant-key
  model: claude-sonnet-4-6
bare:
  note: 这不是渠道块
"""


OPTIONS_CONFIG = """version: 1
provider: deepseek
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-openai-key
  model: gpt-5.4
  options:
    temperature: 0.3
deepseek:
  base_url: https://api.deepseek.com
  api_key: sk-ds-key
  model: deepseek-v4
  options:
    thinking: true
    reasoning_effort: high
    extra_body:
      tag: ${DS_TAG}
anthropic:
  base_url: https://api.anthropic.com
  api_key: sk-ant-key
  options: 不是映射
"""


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


@pytest.fixture
def options_store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(OPTIONS_CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


class TestActiveBlockSelection:
    def test_default_active_still_reads_the_openai_block(self, store):
        secret = store.get_openai_secret()
        assert (secret.provider, secret.model) == ("openai", "gpt-5.4")
        assert secret.api_key == "sk-openai-key"

    def test_switching_active_switches_the_whole_block(self, store):
        store.set_active_provider("anthropic")
        secret = store.get_openai_secret()
        assert secret.provider == "anthropic"
        assert secret.base_url == "https://api.anthropic.com"
        assert secret.api_key == "sk-ant-key"
        assert secret.model == "claude-sonnet-4-6"

    def test_missing_block_falls_back_without_lying_about_the_name(self, store, tmp_path):
        # 报了 anthropic 却给 openai 的地址，engine 会拿错的请求格式去打。
        (tmp_path / "config.yaml").write_text(
            CONFIG.replace("provider: openai", "provider: ghost"), encoding="utf-8"
        )
        store.reload()
        secret = store.get_openai_secret()
        assert secret.provider == "openai"
        assert secret.base_url == "https://api.openai.com/v1"

    def test_non_provider_block_is_not_selectable(self, store, tmp_path):
        (tmp_path / "config.yaml").write_text(
            CONFIG.replace("provider: openai", "provider: bare"), encoding="utf-8"
        )
        store.reload()
        assert store.get_openai_secret().provider == "openai"

    def test_secret_never_leaks_into_the_public_shape(self, store):
        store.set_active_provider("anthropic")
        assert "sk-ant-key" not in str(store.get_providers_public())


class TestUpdateOpenAiKeepsActive:
    def test_changing_model_no_longer_resets_the_active_provider(self, store):
        # QQ 里的「/切换模型」走这条；以前它会把切好的活跃渠道悄悄改回 openai。
        store.set_active_provider("anthropic")
        store.update_openai(OpenAIConfigUpdateIn(model="gpt-4o-mini"))
        assert store.get_providers_public()["active_provider"] == "anthropic"

    def test_the_openai_block_is_still_what_gets_written(self, store):
        store.update_openai(OpenAIConfigUpdateIn(model="written-here"))
        assert store.get_providers_public()["providers"]["openai"]["model"] == "written-here"

    def test_it_writes_to_whichever_block_is_active(self, store):
        # 读走 active、写落 openai 的话，「/切换模型」会改一个没人用的块，改完毫无反应。
        store.set_active_provider("anthropic")
        store.update_openai(OpenAIConfigUpdateIn(model="claude-opus-4-2"))

        blocks = store.get_providers_public()["providers"]
        assert blocks["anthropic"]["model"] == "claude-opus-4-2"
        assert blocks["openai"]["model"] == "gpt-5.4"
        assert store.get_openai_secret().model == "claude-opus-4-2"

    def test_the_public_shape_reports_the_active_block(self, store):
        store.set_active_provider("anthropic")
        public = store.get_openai_public()
        assert public.model == "claude-sonnet-4-6"
        assert public.base_url == "https://api.anthropic.com"
        assert public.api_key_present is True
        assert "api_key" not in public.model_dump()

    def test_get_public_reports_the_name_actually_in_use(self, store, tmp_path):
        (tmp_path / "config.yaml").write_text(
            CONFIG.replace("provider: openai", "provider: ghost"), encoding="utf-8"
        )
        store.reload()
        assert store.get_public().provider == "openai"


class TestTheActiveBlockOptions:
    """跟随主渠道的节点要吃到主渠道块的 options，而且和地址、密钥出自同一份配置。"""

    def test_they_come_with_the_address_and_key(self, options_store):
        secret = options_store.get_openai_secret()
        assert (secret.provider, secret.base_url) == ("deepseek", "https://api.deepseek.com")
        # ${VAR} 原样给：和 engine 读点名渠道的 options 一个读法。
        assert secret.options == {"thinking": True, "reasoning_effort": "high", "extra_body": {"tag": "${DS_TAG}"}}

    def test_switching_the_active_channel_switches_them(self, options_store):
        options_store.set_active_provider("openai")
        assert options_store.get_openai_secret().options == {"temperature": 0.3}

    def test_a_block_without_them_gives_an_empty_mapping(self, store):
        store.set_active_provider("anthropic")
        assert store.get_openai_secret().options == {}

    def test_a_value_that_is_not_a_mapping_counts_as_unwritten(self, options_store):
        options_store.set_active_provider("anthropic")
        assert options_store.get_openai_secret().options == {}

    def test_a_missing_active_block_takes_the_openai_block_options(self, options_store, tmp_path):
        (tmp_path / "config.yaml").write_text(
            OPTIONS_CONFIG.replace("provider: deepseek", "provider: ghost"), encoding="utf-8",
        )
        options_store.reload()
        secret = options_store.get_openai_secret()
        assert (secret.provider, secret.options) == ("openai", {"temperature": 0.3})

    def test_a_broken_rewrite_keeps_them_with_the_address(self, options_store, tmp_path):
        (tmp_path / "config.yaml").write_text("deepseek: [oops\n", encoding="utf-8")
        options_store.reload()
        secret = options_store.get_openai_secret()
        assert options_store.load_error
        assert secret.base_url == "https://api.deepseek.com"
        assert secret.options["reasoning_effort"] == "high"

    def test_callers_cannot_change_the_stored_copy(self, options_store):
        options_store.get_openai_secret().options["thinking"] = False
        assert options_store.get_openai_secret().options["thinking"] is True


class TestWireFormat:
    def test_old_supervisor_response_degrades_to_openai(self):
        # 字段是这次新加的，engine 比 supervisor 新时读不到，不能因此崩。
        main = normalize_openai_secret({"api_key": "k", "base_url": "u", "model": "m"})
        assert main.provider == "openai"

    def test_provider_is_normalised(self):
        assert normalize_openai_secret({"provider": "  AnThRoPiC "}).provider == "anthropic"

    def test_channel_name_keeps_case_independent_of_wire(self):
        main = normalize_openai_secret({"provider": "gemini", "channel_name": " Relay-A "})
        assert main.provider == "gemini"
        assert main.channel_name == "Relay-A"

    def test_garbage_payload_yields_usable_defaults(self):
        assert normalize_openai_secret(None).provider == "openai"

    def test_the_main_options_come_through_untouched(self):
        main = normalize_openai_secret({"provider": "deepseek", "options": {"thinking": True, "tag": "${DS_TAG}"}})
        assert main.options == {"thinking": True, "tag": "${DS_TAG}"}

    def test_an_old_supervisor_without_options_means_none_written(self):
        assert normalize_openai_secret({"api_key": "k", "base_url": "u", "model": "m"}).options == {}

    @pytest.mark.parametrize("raw", [None, "thinking", ["thinking"], 3])
    def test_options_that_are_not_a_mapping_are_dropped(self, raw):
        assert normalize_openai_secret({"options": raw}).options == {}


class TestResolveProvider:
    def _node(self, **kw):
        return Node(id="n", type="agent", **kw)

    def test_node_without_provider_follows_the_active_one(self):
        rp = resolve_provider(None, self._node(), "m", provider_default_type="deepseek")
        assert rp.provider_type == "deepseek"

    def test_node_provider_still_wins(self):
        rp = resolve_provider(None, self._node(provider="openai"), "m", provider_default_type="deepseek")
        assert rp.provider_type == "openai"

    def test_both_empty_falls_back_to_openai(self):
        assert resolve_provider(None, self._node(), "m").provider_type == "openai"

    def test_plugin_scope_distinguishes_active_and_session_channels(self):
        inherited = resolve_provider(None, self._node(), "m", provider_default_type="gemini", provider_default_channel="Relay-A")
        overridden = resolve_provider(None, self._node(), "m", provider_default_type="gemini", provider_default_channel="Relay-A", session_override={"provider": "Relay-B"})
        assert inherited.channel_name == "Relay-A"
        assert overridden.channel_name == "Relay-B"

    def test_session_override_still_outranks_everything(self):
        rp = resolve_provider(
            None, self._node(provider="openai"), "m",
            provider_default_type="deepseek",
            session_override={"provider": "gemini"},
        )
        assert rp.provider_type == "gemini"

    def test_following_the_main_channel_brings_its_options(self):
        main_options = {"thinking": True}
        rp = resolve_provider(
            None, self._node(), "m",
            provider_default_type="deepseek", provider_default_channel="ds",
            provider_default_options=main_options,
        )
        assert (rp.channel_name, rp.channel_options) == ("ds", {"thinking": True})
        rp.channel_options["thinking"] = False
        assert main_options == {"thinking": True}

    def test_a_session_override_that_only_swaps_the_model_keeps_them(self):
        rp = resolve_provider(
            None, self._node(), "m",
            provider_default_type="deepseek", provider_default_options={"thinking": True},
            session_override={"model": "deepseek-chat"},
        )
        assert (rp.model, rp.channel_options) == ("deepseek-chat", {"thinking": True})

    def test_naming_another_provider_leaves_them_behind(self):
        # 换了一家，键名不通用，主渠道的 options 不能跟过去。
        rp = resolve_provider(
            None, self._node(provider="anthropic"), "m",
            provider_default_type="deepseek", provider_default_options={"thinking": True},
        )
        assert rp.channel_options == {}

    def test_a_session_override_channel_leaves_them_behind(self):
        rp = resolve_provider(
            None, self._node(), "m",
            provider_default_type="deepseek", provider_default_options={"thinking": True},
            session_override={"provider": "gemini"},
        )
        assert rp.channel_options == {}

    def test_old_callers_without_main_options_get_none(self):
        assert resolve_provider(None, self._node(), "m", provider_default_type="deepseek").channel_options == {}


class TestTheEngineHandsTheMainOptionsOn:
    def test_the_node_task_receives_what_the_supervisor_sent(self, tmp_path, monkeypatch):
        from engine import runner

        seen = {}

        async def secret(_http, _url):
            return {
                "provider": "deepseek", "channel_name": "ds", "api_key": "k", "base_url": "u", "model": "m",
                "options": {"thinking": True},
            }

        async def node_task(**kwargs):
            seen.update(kwargs)
            return {"action": "finish", "node_id": "entry"}

        async def idle_heartbeat(*_args, **_kwargs):
            await asyncio.Event().wait()

        class _Answer:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        class _Http:
            async def post(self, _url, **_kwargs):
                return _Answer()

        monkeypatch.setattr(runner, "fetch_openai_secret", secret)
        monkeypatch.setattr(runner, "_run_node_task", node_task)
        monkeypatch.setattr(runner, "_heartbeat", idle_heartbeat)
        task = {"task_id": "t-1", "kind": "node", "session_id": "s", "session_generation": 1, "claim_id": "c", "node_id": "entry"}

        asyncio.run(runner._handle_task(_Http(), _Http(), "http://sup", tmp_path, None, task, "worker-1"))

        assert (seen["default_provider"], seen["default_channel"]) == ("deepseek", "ds")
        assert seen["default_options"] == {"thinking": True}


class TestSecretEndpointWireShape:
    """response_model 漏掉 provider 的话 FastAPI 会静默把它过滤掉，engine 永远收不到。"""

    def _app(self, tmp_path, monkeypatch, config=None):
        import supervisor.admin_api as admin_api
        from supervisor.api import create_app
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState

        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "tok")
        monkeypatch.setattr(admin_api, "_admin_token", "")
        path = tmp_path / "config.yaml"
        path.write_text(config or CONFIG.replace("provider: openai", "provider: anthropic"), encoding="utf-8")
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-active-provider"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )
        return create_app(state=state, process_manager=None, config_store=ConfigStore(path=path))

    def _get(self, app, url, token="tok"):
        import asyncio

        import httpx

        async def _go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.get(url, headers={"Authorization": f"Bearer {token}"})

        return asyncio.run(_go())

    def test_provider_survives_serialisation(self, tmp_path, monkeypatch):
        app = self._app(tmp_path, monkeypatch)
        body = self._get(app, "/v1/config/openai/secret").json()
        assert body["provider"] == "anthropic"
        assert body["model"] == "claude-sonnet-4-6"
        assert body["options"] == {}

    def test_the_main_options_survive_serialisation(self, tmp_path, monkeypatch):
        app = self._app(tmp_path, monkeypatch, OPTIONS_CONFIG)
        body = self._get(app, "/v1/config/openai/secret").json()
        assert body["provider"] == "deepseek"
        assert body["options"] == {"thinking": True, "reasoning_effort": "high", "extra_body": {"tag": "${DS_TAG}"}}
        assert normalize_openai_secret(body).options == body["options"]

    def test_the_tokenless_endpoints_do_not_carry_the_options(self, tmp_path, monkeypatch):
        app = self._app(tmp_path, monkeypatch, OPTIONS_CONFIG)
        for url in ("/v1/config/openai", "/v1/config"):
            assert "reasoning_effort" not in str(self._get(app, url, token="wrong").json())

    def test_secret_endpoint_still_needs_the_token(self, tmp_path, monkeypatch):
        app = self._app(tmp_path, monkeypatch)
        assert self._get(app, "/v1/config/openai/secret", token="wrong").status_code == 401

    def test_the_tokenless_public_endpoint_gained_no_key(self, tmp_path, monkeypatch):
        # /v1/config/openai 没有令牌门，形状变宽等于全网可读。
        app = self._app(tmp_path, monkeypatch)
        body = self._get(app, "/v1/config/openai", token="wrong").json()
        assert "api_key" not in body
        assert "sk-ant-key" not in str(body)
