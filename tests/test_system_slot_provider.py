"""系统槽位盖到主渠道之上的那一层，以及 session 覆盖的入口校验。

只换 url/key 不换 provider，就是拿 A 家的请求格式去打 B 家的地址。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import SystemModel  # noqa: E402
from engine.model import ResolvedProvider  # noqa: E402
from engine.runner import _apply_system_slot  # noqa: E402


def _main() -> ResolvedProvider:
    return ResolvedProvider(
        model="main-model", provider_type="openai",
        api_key="sk-main", base_url="https://main.example/v1",
    )


def _workspace(tmp_path: Path, config: str = "") -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text(config, encoding="utf-8")
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

    def test_a_new_base_url_without_provider_is_reported(self, tmp_path, capsys) -> None:
        rp = _main()
        _apply_system_slot(
            _workspace(tmp_path), rp, SystemModel(base_url="https://api.anthropic.com"), "intent",
        )
        assert rp.provider_type == "openai"
        out = capsys.readouterr().out
        assert "intent" in out
        assert "provider" in out

    def test_switching_provider_alone_is_not_reported(self, tmp_path, capsys) -> None:
        rp = _main()
        _apply_system_slot(_workspace(tmp_path), rp, SystemModel(provider="deepseek"), "compact")
        assert rp.provider_type == "deepseek"
        assert capsys.readouterr().out == ""

    def test_a_slot_pointing_at_a_named_channel_borrows_its_url_and_key(self, tmp_path) -> None:
        # 槽位只填渠道名就该整块继承，否则得在两个地方各填一遍地址密钥。
        root = _workspace(tmp_path, (
            "gemini-中转A:\n"
            "  type: gemini\n"
            "  base_url: https://relay.example/v1\n"
            "  api_key: sk-relay\n"
            "  model: gemini-3-pro\n"
        ))
        rp = ResolvedProvider(model="", provider_type="openai", api_key=None, base_url=None)
        _apply_system_slot(root, rp, SystemModel(provider="gemini-中转A"), "compact")
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://relay.example/v1"
        assert rp.api_key == "sk-relay"
        assert rp.model == "gemini-3-pro"

    def test_the_slot_own_fields_beat_the_channel_block(self, tmp_path) -> None:
        root = _workspace(tmp_path, (
            "gemini-中转A:\n"
            "  type: gemini\n"
            "  base_url: https://relay.example/v1\n"
            "  api_key: sk-relay\n"
        ))
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
