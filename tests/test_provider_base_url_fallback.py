"""非 openai 渠道的 base_url 兜底。

控制台把地址填进 config.yaml 的渠道块，engine 每个任务都会重新拉这份配置；
少了这层兜底，地址会被丢掉、provider 静默回落自己的官方域名，报错还跟地址无关。
"""
from __future__ import annotations

import httpx
import pytest

from engine.model import ResolvedProvider
from engine.runner import _provider_init_kwargs

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


def test_openai_channel_behaviour_is_untouched():
    rp = ResolvedProvider(model="m", provider_type="openai")

    assert kwargs(rp, active="gemini")["base_url"] == RELAY


@pytest.mark.parametrize("active", ["GEMINI", " gemini ", "Gemini"])
def test_active_provider_comparison_ignores_case_and_padding(monkeypatch, active):
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    rp = ResolvedProvider(model="m", provider_type="gemini")

    assert kwargs(rp, active=active)["base_url"] == RELAY
