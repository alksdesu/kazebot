"""节点渠道的 base_url 归属。

控制台把地址填进 config.yaml 的渠道块，engine 每个任务都会重新拉这份配置；
主渠道的地址只在请求格式对得上时才能借，借错了得到的是一个跟地址无关的 404。
"""
from __future__ import annotations

import httpx
import pytest

from engine.model import ResolvedProvider
from engine.runner import _provider_init_kwargs, _rooted_provider_name

RELAY = "https://relay.example.com/g/tok"
MAIN_KEY = "sk-main"


def kwargs(rp, *, active, base_url=RELAY, api_key=MAIN_KEY):
    return _provider_init_kwargs(
        rp,
        provider_name=rp.provider_type,
        llm_http=httpx.AsyncClient(),
        api_key=api_key,
        base_url=base_url,
        provider_options={},
        active_provider=active,
    )


def test_active_channel_uses_the_base_url_from_config(monkeypatch):
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    rp = ResolvedProvider(model="m", provider_type="gemini")

    assert kwargs(rp, active="gemini")["base_url"] == RELAY


def test_a_node_switching_provider_does_not_borrow_the_main_address(monkeypatch):
    """节点单独换 provider 时套用主渠道地址，会把请求连同密钥发给错误的家。"""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    rp = ResolvedProvider(model="m", provider_type="anthropic")

    assert kwargs(rp, active="gemini")["base_url"] is None


def test_node_level_base_url_still_wins(monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")
    rp = ResolvedProvider(model="m", provider_type="gemini", base_url="https://from-node.example.com")

    assert kwargs(rp, active="gemini")["base_url"] == "https://from-node.example.com"


def test_env_overrides_config_but_config_is_the_last_resort(monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")
    rp = ResolvedProvider(model="m", provider_type="gemini")

    assert kwargs(rp, active="gemini")["base_url"] == "https://from-env.example.com"

    monkeypatch.delenv("GEMINI_BASE_URL")
    assert kwargs(rp, active="gemini")["base_url"] == RELAY


def test_openai_node_borrows_the_address_of_an_openai_main_channel():
    rp = ResolvedProvider(model="m", provider_type="openai")

    assert kwargs(rp, active="openai")["base_url"] == RELAY


def test_the_two_openai_variants_count_as_one_family():
    """同一个兼容网关同时提供 chat/completions 和 responses，地址是通用的。"""
    rp = ResolvedProvider(model="m", provider_type="openai-responses")

    assert kwargs(rp, active="openai")["base_url"] == RELAY


def test_an_openai_node_does_not_borrow_a_native_gemini_address():
    """拿 Gemini 反代地址发 /chat/completions 只会 404，报错还跟地址毫无关系。"""
    rp = ResolvedProvider(model="m", provider_type="openai")

    assert kwargs(rp, active="gemini")["base_url"] is None


@pytest.mark.parametrize("active", ["GEMINI", " gemini ", "Gemini"])
def test_active_provider_comparison_ignores_case_and_padding(monkeypatch, active):
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    rp = ResolvedProvider(model="m", provider_type="gemini")

    assert kwargs(rp, active=active)["base_url"] == RELAY


class TestRootedProviderName:
    """节点只写 provider、地址交给空掉的 $ENV{} 时，那份声明没有根。"""

    def test_a_declaration_without_an_address_follows_the_main_channel(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        rp = ResolvedProvider(model="m", provider_type="openai")

        assert _rooted_provider_name(rp, active_provider="gemini") == "gemini"

    def test_its_own_base_url_keeps_the_declaration(self):
        rp = ResolvedProvider(model="m", provider_type="openai", base_url="https://gw.example.com/v1")

        assert _rooted_provider_name(rp, active_provider="gemini") == "openai"

    def test_an_env_supplied_address_keeps_it_too(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://from-env.example.com")
        rp = ResolvedProvider(model="m", provider_type="anthropic")

        assert _rooted_provider_name(rp, active_provider="gemini") == "anthropic"

    def test_matching_the_main_channel_needs_no_address(self, monkeypatch):
        monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
        rp = ResolvedProvider(model="m", provider_type="gemini")

        assert _rooted_provider_name(rp, active_provider="gemini") == "gemini"

    def test_an_unknown_main_channel_leaves_the_declaration_alone(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        rp = ResolvedProvider(model="m", provider_type="openai")

        assert _rooted_provider_name(rp, active_provider="") == "openai"
