"""系统槽位盖到主渠道之上的那一层，以及 session 覆盖的入口校验。

只换 url/key 不换 provider，就是拿 A 家的请求格式去打 B 家的地址。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import SystemModel  # noqa: E402
from engine import runner  # noqa: E402
from engine.model import ResolvedProvider  # noqa: E402
from engine.node import load_node  # noqa: E402
from engine.runner import _apply_system_slot  # noqa: E402

_RELAY_BLOCK = (
    "gemini-中转A:\n"
    "  type: gemini\n"
    "  base_url: https://relay.example/v1\n"
    "  api_key: sk-relay\n"
    "  model: gemini-3-pro\n"
    "  options:\n"
    "    thinking_mode: \"on\"\n"
)


def _main() -> ResolvedProvider:
    return ResolvedProvider(
        model="main-model", provider_type="openai",
        api_key="sk-main", base_url="https://main.example/v1",
    )


def _workspace(tmp_path: Path, config: str = "") -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "models.yaml").write_text(config, encoding="utf-8")
    return tmp_path


class TestApplySystemSlot:
    def test_an_empty_slot_changes_nothing(self, tmp_path) -> None:
        rp = _main()
        _apply_system_slot(_workspace(tmp_path), rp, SystemModel(), "compact")
        assert (rp.model, rp.provider_type, rp.api_key, rp.base_url) == (
            "main-model", "openai", "sk-main", "https://main.example/v1",
        )

    def test_the_slot_provider_wins(self, tmp_path) -> None:
        rp = _main()
        _apply_system_slot(
            _workspace(tmp_path), rp,
            SystemModel(model="claude-haiku-4-5", base_url="https://api.anthropic.com",
                        api_key="sk-ant", provider="anthropic"),
            "compact",
        )
        assert rp.provider_type == "anthropic"
        assert rp.base_url == "https://api.anthropic.com"
        assert rp.api_key == "sk-ant"
        assert rp.model == "claude-haiku-4-5"

    def test_model_only_slot_keeps_the_main_channel_format(self, tmp_path) -> None:
        # 最常见的用法：只换个便宜模型，url/key/格式都跟着主渠道。
        rp = _main()
        _apply_system_slot(_workspace(tmp_path), rp, SystemModel(model="cheap-mini"), "summary")
        assert rp.model == "cheap-mini"
        assert rp.provider_type == "openai"
        assert rp.base_url == "https://main.example/v1"

    def test_a_new_base_url_without_provider_is_reported(self, tmp_path, caplog) -> None:
        rp = _main()
        with caplog.at_level(logging.WARNING, logger=runner.logger.name):
            _apply_system_slot(
                _workspace(tmp_path), rp, SystemModel(base_url="https://api.anthropic.com"), "intent",
            )
        assert rp.provider_type == "openai"
        assert rp.base_url == "https://api.anthropic.com"
        assert any("intent" in r.getMessage() and "provider" in r.getMessage() for r in caplog.records)

    def test_switching_provider_alone_is_not_reported(self, tmp_path, caplog) -> None:
        rp = _main()
        with caplog.at_level(logging.WARNING, logger=runner.logger.name):
            _apply_system_slot(_workspace(tmp_path), rp, SystemModel(provider="deepseek"), "compact")
        assert rp.provider_type == "deepseek"
        assert [r for r in caplog.records if r.name == runner.logger.name] == []

    def test_a_slot_pointing_at_a_named_channel_borrows_everything(self, tmp_path) -> None:
        # 槽位只填渠道名就该整块继承，否则得在两个地方各填一遍地址密钥。
        # rp 照真实路径的样子带着节点解析出的模型：resolve_provider 从不给空模型。
        root = _workspace(tmp_path, _RELAY_BLOCK)
        rp = ResolvedProvider(
            model="main-model", provider_type="openai", api_key=None, base_url=None,
            channel_options={"temperature": 0.3}, channel_name="openai",
        )
        _apply_system_slot(root, rp, SystemModel(provider="gemini-中转A"), "compact")
        assert (rp.provider_type, rp.channel_name) == ("gemini", "gemini-中转A")
        assert rp.base_url == "https://relay.example/v1"
        assert rp.api_key == "sk-relay"
        assert rp.model == "gemini-3-pro"
        assert rp.channel_options == {"thinking_mode": "on"}

    def test_the_slot_own_fields_beat_the_channel_block(self, tmp_path) -> None:
        root = _workspace(tmp_path, _RELAY_BLOCK)
        rp = _main()
        _apply_system_slot(
            root, rp,
            SystemModel(model="cheap-mini", base_url="https://slot.example/v1",
                        provider="gemini-中转A"),
            "compact",
        )
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://slot.example/v1"
        assert rp.model == "cheap-mini"
        # 槽位没写密钥就用渠道块的，主渠道的 sk-main 不能被发到这个地址。
        assert rp.api_key == "sk-relay"

    def test_the_slot_key_beats_the_block_key(self, tmp_path) -> None:
        rp = _main()
        _apply_system_slot(
            _workspace(tmp_path, _RELAY_BLOCK), rp, SystemModel(api_key="sk-slot", provider="gemini-中转A"), "compact",
        )
        assert (rp.api_key, rp.base_url) == ("sk-slot", "https://relay.example/v1")

    def test_a_block_without_a_model_keeps_the_node_model(self, tmp_path) -> None:
        root = _workspace(tmp_path, "relay:\n  type: gemini\n  base_url: https://relay.example/v1\n  api_key: sk-relay\n")
        rp = _main()
        _apply_system_slot(root, rp, SystemModel(provider="relay"), "summary")
        assert rp.model == "main-model"

    def test_a_bare_wire_name_drops_the_node_address_and_key(self, tmp_path) -> None:
        # 没有块的线格式名：地址密钥留空，交给后面的 <PROVIDER>_* 或借主渠道去补，不能留着别家的。
        rp = ResolvedProvider(
            model="main-model", provider_type="openai", api_key="sk-node", base_url="https://node.example/v1",
            channel_options={"temperature": 0.3}, channel_name="openai",
        )
        _apply_system_slot(_workspace(tmp_path), rp, SystemModel(provider="anthropic"), "compact")
        assert (rp.provider_type, rp.channel_name) == ("anthropic", "anthropic")
        assert (rp.base_url, rp.api_key) == (None, None)
        assert rp.channel_options == {}
        assert rp.model == "main-model"

    def test_without_provider_the_options_stay_with_the_node_channel(self, tmp_path) -> None:
        rp = ResolvedProvider(
            model="main-model", provider_type="deepseek", channel_options={"thinking": True}, channel_name="ds",
        )
        _apply_system_slot(_workspace(tmp_path), rp, SystemModel(model="cheap-mini"), "summary")
        assert (rp.model, rp.channel_name, rp.channel_options) == ("cheap-mini", "ds", {"thinking": True})


class TestTheIntentNodeOnTheRealPath:
    """仓库里的 chat.intent 走 _build_node_provider：槽位解析、渠道继承、options 分层一起验。"""

    _MAIN = {
        "api_key": "sk-main", "base_url": "https://main.example/v1", "default_model": "main-model",
        "default_provider": "deepseek", "default_channel": "deepseek",
    }
    _RUNTIME = {"providers": {
        "deepseek": {"options": {"reasoning_effort": "max"}},
        "gemini": {"options": {"thinking_mode": "auto", "safety_threshold": "off"}},
    }}

    @pytest.fixture
    def root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # 共享密钥文件跟着工作区走：钉到临时目录，开发机上真实的 config/secrets.env 混不进来。
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT", raising=False)
        for suffix in ("MODEL", "BASE_URL", "API_KEY", "PROVIDER"):
            monkeypatch.delenv(f"CLONOTH_INTENT_{suffix}", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
        return tmp_path

    def _build(self, root: Path, monkeypatch: pytest.MonkeyPatch, models: str) -> tuple[object, str, dict]:
        _workspace(root, models)
        created: list[dict] = []
        requested: list[str] = []

        class _Recorder:
            def __init__(self, **kwargs: object) -> None:
                created.append(kwargs)

        def get(name: str) -> type:
            requested.append(name)
            return _Recorder

        monkeypatch.setattr(runner.provider_registry, "get", get)
        node = load_node(root, "chat.intent")
        assert node is not None
        provider = asyncio.run(runner._build_node_provider(
            ws_root=root, node=node, http=None, llm_http=object(), sup_url="http://sup", route_session_id="",
            runtime_cfg=self._RUNTIME, default_options={"thinking": True}, **self._MAIN,
        ))
        return provider, requested[-1], created[-1]

    def test_a_slot_naming_a_channel_takes_its_model_key_and_options(self, root, monkeypatch) -> None:
        provider, wire, kwargs = self._build(
            root, monkeypatch, _RELAY_BLOCK + "system_models:\n  intent:\n    provider: gemini-中转A\n",
        )
        assert wire == "gemini"
        assert provider.plugin_channel == "gemini-中转A"
        assert kwargs["model"] == "gemini-3-pro"
        assert (kwargs["base_url"], kwargs["api_key"]) == ("https://relay.example/v1", "sk-relay")
        # 运行参数按渠道的线格式取，渠道块盖在上面；主渠道的 options 不跟过来。
        assert kwargs["provider_options"] == {"thinking_mode": "on", "safety_threshold": "off"}

    def test_the_slot_env_model_beats_the_channel_block(self, root, monkeypatch) -> None:
        monkeypatch.setenv("CLONOTH_INTENT_MODEL", "intent-mini")
        _, wire, kwargs = self._build(
            root, monkeypatch, _RELAY_BLOCK + "system_models:\n  intent:\n    provider: gemini-中转A\n",
        )
        assert (wire, kwargs["model"]) == ("gemini", "intent-mini")

    def test_the_slot_file_model_beats_the_channel_block(self, root, monkeypatch) -> None:
        _, _, kwargs = self._build(
            root, monkeypatch,
            _RELAY_BLOCK + "system_models:\n  intent:\n    provider: gemini-中转A\n    model: flash-lite\n",
        )
        assert kwargs["model"] == "flash-lite"

    def test_a_model_only_slot_still_follows_the_main_channel(self, root, monkeypatch) -> None:
        provider, wire, kwargs = self._build(
            root, monkeypatch, "system_models:\n  intent:\n    model: cheap-mini\n",
        )
        assert (wire, provider.plugin_channel) == ("deepseek", "deepseek")
        assert kwargs["model"] == "cheap-mini"
        assert (kwargs["base_url"], kwargs["api_key"]) == ("https://main.example/v1", "sk-main")
        # 跟随主渠道的系统节点也带上主渠道块的 options，分层和点名渠道时一样。
        assert kwargs["provider_options"] == {"reasoning_effort": "max", "thinking": True}

    def test_an_empty_slot_changes_nothing(self, root, monkeypatch) -> None:
        provider, wire, kwargs = self._build(root, monkeypatch, "")
        assert (wire, provider.plugin_channel, kwargs["model"]) == ("deepseek", "deepseek", "main-model")
        assert kwargs["api_key"] == "sk-main"


class TestSessionOverrideValidation:
    """拼错的 provider 会一路走到 engine 静默回退成 openai 格式。"""

    def _app(self, tmp_path, monkeypatch, config: str = ""):
        import supervisor.admin_api as admin_api
        from supervisor.api import create_app
        from supervisor.config_store import ConfigStore
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState

        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "tok")
        monkeypatch.setattr(admin_api, "_admin_token", "")
        if config:
            (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-override"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )
        app = create_app(
            state=state, process_manager=None,
            config_store=ConfigStore(path=tmp_path / "config.yaml"),
        )
        return app, state

    def _put(self, app, session_id, body):
        import asyncio

        import httpx

        async def _go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.put(
                    "/v1/sessions/" + session_id + "/provider_override",
                    headers={"Authorization": "Bearer tok"}, json=body,
                )

        return asyncio.run(_go())

    def test_an_unknown_provider_is_refused(self, tmp_path, monkeypatch) -> None:
        app, _ = self._app(tmp_path, monkeypatch)
        resp = self._put(app, "no-such-session", {"provider": "antropic"})
        # 会话不存在是 404；名字先被拦下来，所以这里必须是 400。
        assert resp.status_code == 400
        assert "antropic" in resp.text

    def test_a_known_provider_passes_the_name_check(self, tmp_path, monkeypatch) -> None:
        app, _ = self._app(tmp_path, monkeypatch)
        assert self._put(app, "no-such-session", {"provider": "anthropic"}).status_code == 404

    def test_omitting_provider_is_still_allowed(self, tmp_path, monkeypatch) -> None:
        # 只换中转地址、格式不变是正常用法，不能一刀切要求填 provider。
        app, _ = self._app(tmp_path, monkeypatch)
        resp = self._put(app, "no-such-session", {"base_url": "https://relay.example/v1"})
        assert resp.status_code == 404

    def test_the_alias_field_is_checked_too(self, tmp_path, monkeypatch) -> None:
        app, _ = self._app(tmp_path, monkeypatch)
        assert self._put(app, "no-such-session", {"provider_type": "nope"}).status_code == 400

    def test_a_named_channel_passes(self, tmp_path, monkeypatch) -> None:
        app, _ = self._app(tmp_path, monkeypatch, (
            "provider: openai\n"
            "openai:\n  base_url: https://main.example/v1\n  api_key: sk-main\n"
            "gemini-中转A:\n  type: gemini\n  base_url: https://relay.example/v1\n"
        ))
        assert self._put(app, "no-such-session", {"provider": "gemini-中转A"}).status_code == 404

    def test_the_channel_name_is_case_sensitive(self, tmp_path, monkeypatch) -> None:
        # 块名原样存进 config.yaml，转小写就查不到那个块了。
        app, _ = self._app(tmp_path, monkeypatch, (
            "provider: openai\n"
            "openai:\n  base_url: https://main.example/v1\n  api_key: sk-main\n"
            "Relay-A:\n  type: openai\n  base_url: https://relay.example/v1\n"
        ))
        assert self._put(app, "no-such-session", {"provider": "Relay-A"}).status_code == 404
        assert self._put(app, "no-such-session", {"provider": "relay-a"}).status_code == 400
