"""向上游要模型列表。

控制台在渠道还没保存时就要用页面上的值去问，所以这条路不经过 provider 实例。
密钥会跟着请求走，因此这里同时钉住「它不许出现在 URL 里」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers import registry  # noqa: E402

_KEY = "sk-must-not-leak"


def _req(name: str, base: str = ""):
    cls = registry.get(name)
    assert cls is not None, name
    return cls.catalog_request(base_url=base, api_key=_KEY)


class TestTheEndpointIsBuiltPerFamily:
    @pytest.mark.parametrize("name,expected", [
        ("openai", "https://api.openai.com/v1/models"),
        ("deepseek", "https://api.deepseek.com/models"),
        ("anthropic", "https://api.anthropic.com/v1/models"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/models"),
        ("openai-responses", "https://api.openai.com/v1/models"),
    ])
    def test_the_default_address_is_this_vendors_own(self, name: str, expected: str) -> None:
        # deepseek 继承 OpenAIProvider，漏了这条就会跑去问 api.openai.com 要模型。
        assert _req(name)[0] == expected

    @pytest.mark.parametrize("name,base,expected", [
        ("openai", "https://relay.test/v1", "https://relay.test/v1/models"),
        ("deepseek", "https://relay.test/v1", "https://relay.test/v1/models"),
        ("anthropic", "https://relay.test", "https://relay.test/v1/models"),
        ("gemini", "https://relay.test", "https://relay.test/v1beta/models"),
    ])
    def test_a_relay_address_is_honoured(self, name: str, base: str, expected: str) -> None:
        assert _req(name, base)[0] == expected

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        assert "//models" not in _req("anthropic", "https://relay.test/")[0]


class TestTheKeyTravelsInHeadersOnly:
    @pytest.mark.parametrize("name", ["openai", "deepseek", "anthropic", "gemini", "openai-responses"])
    def test_it_never_lands_in_the_url(self, name: str) -> None:
        # URL 会进日志和报错文案，密钥一旦进去就跟着到处跑。
        assert _KEY not in _req(name)[0]

    @pytest.mark.parametrize("name", ["openai", "deepseek", "anthropic", "gemini", "openai-responses"])
    def test_it_is_actually_sent(self, name: str) -> None:
        assert _KEY in "".join(_req(name)[1].values())

    def test_the_official_hosts_use_their_own_scheme(self) -> None:
        # 认证方式必须和 _headers() 挑的那一套一致，否则拉得到列表却聊不了天。
        assert _req("anthropic")[1]["x-api-key"] == _KEY
        assert _req("gemini")[1]["x-goog-api-key"] == _KEY

    def test_a_relay_falls_back_to_bearer(self) -> None:
        assert _req("anthropic", "https://relay.test")[1]["Authorization"] == "Bearer " + _KEY
        assert _req("gemini", "https://relay.test")[1]["Authorization"] == "Bearer " + _KEY

    def test_anthropic_still_sends_its_version(self) -> None:
        assert _req("anthropic")[1]["anthropic-version"]


class TestParsing:
    def test_openai_shape(self) -> None:
        payload = {"data": [{"id": "gpt-4o"}, {"id": "o3"}]}
        assert registry.get("openai").parse_catalog(payload) == ["gpt-4o", "o3"]

    def test_gemini_strips_the_models_prefix(self) -> None:
        payload = {"models": [{"name": "models/gemini-2.0-flash"}]}
        assert registry.get("gemini").parse_catalog(payload) == ["gemini-2.0-flash"]

    def test_blank_entries_are_dropped(self) -> None:
        payload = {"data": [{"id": "  "}, {"id": "gpt-4o"}, {}, "junk"]}
        assert registry.get("openai").parse_catalog(payload) == ["gpt-4o"]

    @pytest.mark.parametrize("payload", [{}, [], None, {"data": "nope"}, {"models": 3}])
    def test_an_unrecognised_shape_yields_nothing(self, payload) -> None:
        # 认不出就说没有，别把半个响应当成模型名喂给界面。
        assert registry.get("openai").parse_catalog(payload) == []
        assert registry.get("gemini").parse_catalog(payload) == []


class TestTheDefaultIsOptOut:
    def test_a_provider_without_a_catalog_says_so(self) -> None:
        # 新接入的 provider 不实现这个也要能跑，界面据此把「拉取」按钮藏起来。
        from providers.base import BaseProvider

        assert BaseProvider.catalog_request(base_url="", api_key=_KEY) is None
        assert BaseProvider.parse_catalog({"data": [{"id": "x"}]}) == []

    def test_every_registered_provider_answers_the_question(self) -> None:
        for name in registry.list():
            cls = registry.get(name)
            result = cls.catalog_request(base_url="", api_key=_KEY)
            assert result is None or (isinstance(result, tuple) and len(result) == 2), name
