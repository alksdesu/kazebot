"""provider 声明的请求格式族与官方域名。控制台照它提示「地址和渠道对不上」。

这两项一个要继承、一个不能继承，写反了界面就会指鹿为马 —— 所以两边都钉住。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers import registry  # noqa: E402
from providers.base import BaseProvider  # noqa: E402


class TestWireFormat:
    def test_every_registered_provider_declares_one(self) -> None:
        formats = registry.wire_formats()
        assert set(formats) == set(registry.list())
        assert all(value for value in formats.values())

    def test_it_is_inherited_because_deepseek_speaks_openai(self) -> None:
        # 换地址不换格式：DeepSeek 继承 OpenAIProvider 就是这个意思。
        assert registry.wire_formats()["deepseek"] == "openai"

    def test_a_provider_with_its_own_body_is_its_own_family(self) -> None:
        formats = registry.wire_formats()
        assert formats["anthropic"] == "anthropic"
        assert formats["gemini"] == "gemini"
        # 同域名不同请求体：Responses API 收不了 chat/completions 的 body。
        assert formats["openai-responses"] == "openai-responses"


class TestHostProfiles:
    def test_an_official_host_maps_to_the_families_that_accept_it(self) -> None:
        profiles = registry.host_profiles()
        assert profiles["api.anthropic.com"] == ["anthropic"]
        assert profiles["generativelanguage.googleapis.com"] == ["gemini"]

    def test_one_host_can_serve_two_families(self) -> None:
        assert registry.host_profiles()["api.openai.com"] == ["openai", "openai-responses"]

    def test_deepseek_declares_its_own_host(self) -> None:
        assert registry.host_profiles()["api.deepseek.com"] == ["openai"]

    def test_a_subclass_declaring_nothing_inherits_no_host(self) -> None:
        # 继承来的域名会让界面把 api.openai.com 认成这个中转。
        # 格式要继承（它确实说 OpenAI 话），域名不能继承 —— 这一对不对称就靠这条钉住。
        from providers import ProviderRegistry
        from providers.openai import OpenAIProvider

        class QuietRelay(OpenAIProvider):
            provider_name = "quiet-relay"

        scratch = ProviderRegistry()
        scratch.register("quiet-relay", QuietRelay)

        assert scratch.wire_formats() == {"quiet-relay": "openai"}
        assert scratch.host_profiles() == {}

    def test_only_self_declared_hosts_count(self) -> None:
        for name in registry.list():
            cls = registry.get(name)
            assert cls is not None
            declared = cls.__dict__.get("official_hosts", ())
            assert isinstance(declared, tuple)
            for host in declared:
                assert "/" not in host and ":" not in host, f"{name} 的 official_hosts 要写裸域名"

    def test_a_relay_domain_is_not_guessed_at(self) -> None:
        # 中转站看不出出身，认不出就闭嘴 —— 拦错比放过更烦人。
        profiles = registry.host_profiles()
        assert "api.oneapi.example" not in profiles
        assert all(host.count(".") >= 1 for host in profiles)


def test_the_base_class_leaves_both_empty() -> None:
    # 默认值非空的话，新写的 provider 不声明就会顶着别人的身份。
    assert BaseProvider.wire_format == ""
    assert BaseProvider.official_hosts == ()
