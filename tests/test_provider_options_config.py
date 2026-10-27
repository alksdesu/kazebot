"""Regression tests for global provider_options configuration.

[AutoC 2026-06-01] These tests are written before the implementation because the
runtime already supports node-level provider_options, but provider-wide defaults
were not passed through. The tests pin the intended merge behavior so future
provider adapters can rely on one combined options dictionary.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

# Why: the project is tested directly from a source checkout. How: put the
# repository root on sys.path before importing local modules. Purpose: exercise
# the edited runtime and runner modules without requiring package installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_runtime import DEFAULT_RUNTIME_CONFIG  # noqa: E402
from engine import runner  # noqa: E402
from engine.model import layered_provider_options, merge_provider_options  # noqa: E402
from engine.attachments import contains_image_blocks  # noqa: E402
from engine.builtin import fallback_provider as fallback_module  # noqa: E402
from engine.context import ServingProvider  # noqa: E402
from engine.inference import llm_call  # noqa: E402
from engine.node import Node  # noqa: E402


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

    merged = merge_provider_options(global_options, node_options)

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


def test_layered_options_go_from_the_runtime_wire_up_to_the_narrowest_layer() -> None:
    runtime_cfg = {"providers": {
        "gemini": {"options": {"thinking_mode": "auto", "safety_threshold": "off", "nested": {"a": 1, "b": 1}}},
        "openai": {"options": {"temperature": 0.9}},
    }}

    merged = layered_provider_options(
        runtime_cfg, "gemini", {"thinking_mode": "on", "nested": {"b": 2}}, None, "不是映射", {"nested": {"c": 3}},
    )

    assert merged == {"thinking_mode": "on", "safety_threshold": "off", "nested": {"a": 1, "b": 2, "c": 3}}
    assert runtime_cfg["providers"]["gemini"]["options"]["nested"] == {"a": 1, "b": 1}


@pytest.mark.parametrize("runtime_cfg", [None, {}, {"providers": None}, {"providers": {"gemini": "x"}},
                                         {"providers": {"gemini": {"options": ["x"]}}}])
def test_a_runtime_layer_that_is_missing_or_malformed_counts_as_empty(runtime_cfg) -> None:
    assert layered_provider_options(runtime_cfg, "gemini", {"thinking_mode": "on"}) == {"thinking_mode": "on"}


class TestTheMainChannelOptionsOnTheRealPath:
    """跟随主渠道的节点：运行参数[主渠道线格式] < 主渠道块的 options < 节点写的，和点名渠道时一样分层。"""

    MAIN = {
        "api_key": "sk-main", "base_url": "https://api.deepseek.com", "default_model": "deepseek-v4",
        "default_provider": "deepseek", "default_channel": "deepseek",
    }
    RUNTIME = {"providers": {
        "deepseek": {"options": {"reasoning_effort": "max", "timeout_sec": 300}},
        "gemini": {"options": {"safety_threshold": "off"}},
    }}

    def _build(self, tmp_path, monkeypatch, node, *, models="", default_options=None):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "models.yaml").write_text(models, encoding="utf-8")
        created = []

        class _Recorder:
            def __init__(self, **kwargs):
                created.append(kwargs)

        monkeypatch.setattr(runner.provider_registry, "get", lambda _name: _Recorder)
        provider = asyncio.run(runner._build_node_provider(
            ws_root=tmp_path, node=node, http=None, llm_http=object(), sup_url="http://sup", route_session_id="",
            runtime_cfg=self.RUNTIME, default_options=default_options, **self.MAIN,
        ))
        return provider, created[-1]["provider_options"]

    def test_a_node_following_the_main_channel_gets_its_block_options(self, tmp_path, monkeypatch) -> None:
        _, options = self._build(
            tmp_path, monkeypatch, Node(id="n", type="ai", provider_options={"reasoning_effort": "default"}),
            default_options={"thinking": True, "reasoning_effort": "high"},
        )

        assert options == {"timeout_sec": 300, "thinking": True, "reasoning_effort": "default"}

    def test_without_main_options_only_the_runtime_and_node_layers_apply(self, tmp_path, monkeypatch) -> None:
        _, options = self._build(tmp_path, monkeypatch, Node(id="n", type="ai"))

        assert options == {"reasoning_effort": "max", "timeout_sec": 300}

    def test_a_node_naming_another_channel_keeps_to_that_channel(self, tmp_path, monkeypatch) -> None:
        provider, options = self._build(
            tmp_path, monkeypatch, Node(id="n", type="ai", provider="relay"),
            models="relay:\n  type: gemini\n  base_url: https://relay.example/v1\n  api_key: sk-relay\n"
                   "  options:\n    thinking_mode: \"on\"\n",
            default_options={"thinking": True},
        )

        assert provider.plugin_channel == "relay"
        assert options == {"safety_threshold": "off", "thinking_mode": "on"}


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
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
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
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(
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
        llm_call,
        "_build_messages_for_provider",
        lambda messages, formatter, provider: messages,
    )
    monkeypatch.setattr(
        llm_call,
        "prepare_messages_for_llm",
        lambda messages, workspace_root: messages,
    )

    ctx = _fallback_ctx(tmp_path)
    asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx))

    assert ctx.response.ok is True
    assert contains_image_blocks(captured["messages"])


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


_RELAY_CFG = {
    "relay": {
        "type": "openai", "base_url": "https://relay.test/v1", "api_key": "k",
        "options": {"temperature": 0.2, "top_p": 0.8, "reasoning": {"effort": "high", "summary": "auto"}},
    },
}
_RUNTIME_OPENAI = {"providers": {"openai": {"options": {"max_completion_tokens": 321, "temperature": 1.0}}}}


def test_a_fallback_on_the_primary_wire_layers_runtime_block_node_then_entry() -> None:
    entry = fallback_module._resolve_fallback_entry(
        {"provider": "relay", "options": {"temperature": 0.7}}, _RELAY_CFG,
        runtime_cfg=_RUNTIME_OPENAI, node_options={"top_p": 0.5, "max_completion_tokens": 999}, primary_wire="openai",
    )

    assert entry["options"] == {
        "max_completion_tokens": 999, "temperature": 0.7, "top_p": 0.5,
        "reasoning": {"effort": "high", "summary": "auto"},
    }


def test_node_options_stay_behind_when_the_fallback_speaks_another_wire() -> None:
    # 节点的 provider_options 是照主请求那家写的，换了一家键名不通用。
    entry = fallback_module._resolve_fallback_entry(
        {"provider": "relay"}, _RELAY_CFG,
        runtime_cfg=_RUNTIME_OPENAI, node_options={"top_p": 0.5}, primary_wire="deepseek",
    )

    assert entry["options"] == {
        "max_completion_tokens": 321, "temperature": 0.2, "top_p": 0.8,
        "reasoning": {"effort": "high", "summary": "auto"},
    }


def test_an_unknown_primary_wire_carries_no_node_options() -> None:
    entry = fallback_module._resolve_fallback_entry({"provider": "relay"}, _RELAY_CFG, node_options={"top_p": 0.5})

    assert entry["options"]["top_p"] == 0.8


def test_nested_entry_options_merge_into_the_block() -> None:
    entry = fallback_module._resolve_fallback_entry(
        {"provider": "relay", "options": {"reasoning": {"effort": "low"}}}, _RELAY_CFG,
    )

    assert entry["options"]["reasoning"] == {"effort": "low", "summary": "auto"}


def test_the_fallback_goes_out_like_the_primary_path(tmp_path, monkeypatch) -> None:
    """共用 engine 的模型请求客户端，运行参数、渠道块、节点、条目四层 options 都到请求里，用完不关共用的客户端。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text(yaml.safe_dump({
        "relay": {"type": "openai", "base_url": "https://relay.test/v1", "api_key": "sk-relay", "model": "backup",
                  "options": {"temperature": 0.2}},
        "fallbacks": [{"provider": "relay", "options": {"temperature": 0.7}}],
    }), encoding="utf-8")
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(llm_call, "_build_messages_for_provider", lambda messages, formatter, provider: messages)
    monkeypatch.setattr(llm_call, "prepare_messages_for_llm", lambda messages, workspace_root: messages)

    async def run() -> SimpleNamespace:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as shared:
            ctx = SimpleNamespace(
                response=SimpleNamespace(ok=False, status_code=502, error="primary failed"),
                rctx=SimpleNamespace(workspace_root=tmp_path, llm_http=shared, total_usage={}, serving_provider=None),
                node=SimpleNamespace(id="entry", provider_options={"top_p": 0.5}),
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                provider=SimpleNamespace(model="primary", provider_name="openai"),
                extra={"loop_state": SimpleNamespace(
                    formatter=None, runtime_cfg={"providers": {"openai": {"options": {"max_completion_tokens": 321}}}},
                )},
            )
            await fallback_module.FallbackProviderHandler().handle(ctx)
            assert not shared.is_closed
            return ctx

    ctx = asyncio.run(run())

    assert ctx.response.ok
    assert len(seen) == 1
    assert str(seen[0].url) == "https://relay.test/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-relay"
    body = json.loads(seen[0].content)
    assert (body["model"], body["temperature"], body["top_p"], body["max_completion_tokens"]) == ("backup", 0.7, 0.5, 321)
    assert ctx.rctx.serving_provider == ServingProvider(model="backup", provider="openai", channel="relay")
