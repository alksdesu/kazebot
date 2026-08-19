"""Regression tests for global provider_options configuration.

[AutoC 2026-06-01] These tests are written before the implementation because the
runtime already supports node-level provider_options, but provider-wide defaults
were not passed through. The tests pin the intended merge behavior so future
provider adapters can rely on one combined options dictionary.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

# Why: the project is tested directly from a source checkout. How: put the
# repository root on sys.path before importing local modules. Purpose: exercise
# the edited runtime and runner modules without requiring package installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_runtime import DEFAULT_RUNTIME_CONFIG  # noqa: E402
from engine.runner import _merge_provider_options  # noqa: E402
from engine.builtin import fallback_provider as fallback_module  # noqa: E402


def test_default_openai_provider_declares_options_dict() -> None:
    """The default runtime config should expose a provider-wide options dict."""
    # Why: downstream runner code reads providers.openai.options unconditionally
    # when building the provider_options payload. How: assert the default schema
    # contains an empty dict. Purpose: old configs remain valid and new configs
    # have a documented place to put provider-wide options.
    assert DEFAULT_RUNTIME_CONFIG["providers"]["openai"]["options"] == {}


def test_provider_options_merge_keeps_global_defaults_and_node_overrides() -> None:
    """Node provider_options should override matching global provider options."""
    global_options = {
        "reasoning": {"effort": "medium", "summary": "auto"},
        "text": {"verbosity": "low"},
        "shared": "global",
    }
    node_options = {
        "reasoning": {"effort": "high"},
        "shared": "node",
        "node_only": True,
    }

    merged = _merge_provider_options(global_options, node_options)

    # Why: global provider options provide defaults, while node YAML must still be
    # able to specialize a single nested value. How: merge dictionaries recursively
    # and let node values win on conflicts. Purpose: avoid losing sibling options
    # such as reasoning.summary when a node changes only reasoning.effort.
    assert merged == {
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {"verbosity": "low"},
        "shared": "node",
        "node_only": True,
    }
    assert global_options["reasoning"]["effort"] == "medium"
    assert node_options["reasoning"] == {"effort": "high"}


_IMAGE_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }
]


def test_explicit_empty_node_fallback_disables_global_chain() -> None:
    cfg = {"fallbacks": [{"provider": "openai"}], "node_fallbacks": {"qq.vision": []}}
    assert fallback_module._select_fallbacks_for_node(cfg, "qq.vision") == []


def test_fallback_supports_vision_override_and_inheritance() -> None:
    cfg = {
        "openai": {
            "base_url": "https://example.test/v1",
            "api_key": "test-key",
            "model": "vision-model",
            "supports_vision": True,
        }
    }
    inherited = fallback_module._resolve_fallback_entry({"provider": "openai"}, cfg)
    overridden = fallback_module._resolve_fallback_entry(
        {"provider": "openai", "supports_vision": "false"}, cfg
    )
    assert inherited["supports_vision"] is True
    assert overridden["supports_vision"] is False


def _fallback_ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(ok=False, status_code=500, error="primary failed"),
        rctx=SimpleNamespace(workspace_root=tmp_path),
        node=SimpleNamespace(id="qq.vision"),
        messages=_IMAGE_MESSAGES,
        tools=[],
        provider=SimpleNamespace(model="primary", timeout=10.0),
        extra={"loop_state": SimpleNamespace(formatter=None)},
    )


def test_text_only_fallback_is_skipped_for_image_request(tmp_path, monkeypatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.yaml").write_text(
        yaml.safe_dump({
            "fallbacks": [{
                "provider": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "model": "text-only",
            }]
        }),
        encoding="utf-8",
    )
    called = False

    def _unexpected_create(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("text-only vision fallback must not be created")

    monkeypatch.setattr(fallback_module, "_create_fallback_provider", _unexpected_create)
    ctx = _fallback_ctx(tmp_path)
    asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx))

    assert called is False
    assert ctx.response.ok is False
    assert "vision unsupported" in ctx.response.error


def test_vision_fallback_preserves_image_blocks(tmp_path, monkeypatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.yaml").write_text(
        yaml.safe_dump({
            "fallbacks": [{
                "provider": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "model": "vision-backup",
                "supports_vision": True,
            }]
        }),
        encoding="utf-8",
    )
    captured = {}

    class _Provider:
        model = "vision-backup"

        async def chat(self, *, messages, tools):
            captured["messages"] = messages
            return SimpleNamespace(ok=True, status_code=200, error="")

    monkeypatch.setattr(fallback_module, "_create_fallback_provider", lambda **kwargs: _Provider())
    monkeypatch.setattr(
        fallback_module,
        "_build_messages_for_provider",
        lambda messages, formatter, provider: messages,
    )
    monkeypatch.setattr(
        fallback_module,
        "prepare_messages_for_llm",
        lambda messages, workspace_root: messages,
    )

    ctx = _fallback_ctx(tmp_path)
    asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx))

    assert ctx.response.ok is True
    assert fallback_module._messages_contain_images(captured["messages"])


def test_fallback_entry_inherits_options_from_its_provider_block() -> None:
    """备选渠道也得能配参数，否则主渠道开了思考、切过去就悄悄变回默认。"""
    cfg = {
        "deepseek": {
            "base_url": "https://example.test",
            "api_key": "k",
            "model": "deepseek-v4-pro",
            "options": {"thinking": False, "reasoning_effort": "high"},
        }
    }
    inherited = fallback_module._resolve_fallback_entry({"provider": "deepseek"}, cfg)
    assert inherited["options"] == {"thinking": False, "reasoning_effort": "high"}

    # 条目自己写的盖住块里的，其余兄弟项保留。
    overridden = fallback_module._resolve_fallback_entry(
        {"provider": "deepseek", "options": {"thinking": True}}, cfg
    )
    assert overridden["options"] == {"thinking": True, "reasoning_effort": "high"}


def test_fallback_options_reach_the_provider(monkeypatch, tmp_path) -> None:
    seen = {}

    class _Provider:
        model = "backup"

        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def chat(self, *, messages, tools):
            return SimpleNamespace(ok=True, status_code=200, error="")

    class _Registry:
        @staticmethod
        def get(_name):
            return _Provider

        @staticmethod
        def list():
            return ["openai"]

    monkeypatch.setitem(sys.modules, "providers", SimpleNamespace(registry=_Registry()))
    fallback_module._create_fallback_provider(
        provider_type="openai",
        base_url="https://example.test",
        api_key="k",
        model="backup",
        provider_options={"reasoning_effort": "high"},
    )
    assert seen.get("provider_options") == {"reasoning_effort": "high"}


def test_borrowed_http_client_is_returned() -> None:
    """每次 fallback 建一个连接池不还，长跑下来就是慢性泄漏。"""
    closed = []

    class _Client:
        async def aclose(self):
            closed.append(True)

    provider = SimpleNamespace(_fallback_owned_http=_Client())
    asyncio.run(fallback_module._release_fallback_provider(provider))
    assert closed == [True]


def test_shared_http_client_is_not_closed() -> None:
    """runner 传进来的是进程级共享连接池，关掉它整个引擎就废了。"""
    asyncio.run(fallback_module._release_fallback_provider(SimpleNamespace()))
    asyncio.run(fallback_module._release_fallback_provider(None))
