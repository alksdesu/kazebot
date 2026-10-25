"""同一份渠道地址，provider 和工具子进程必须解释成同一个根。

主渠道的 base_url 既喂给 provider 发对话，也被读图、联网搜索这些工具借去用；两边各补各的 /v1，
就会出现「聊天通、看图 404」或者反过来，而且报错里看不出是地址的事。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

import _wire as vw  # noqa: E402
from providers import registry  # noqa: E402
from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.gemini import GeminiProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402

_ADDRESSES = [
    "https://relay.example",
    "https://relay.example/",
    "relay.example",
    "https://relay.example/v1",
    "https://relay.example/v1beta",
    "https://relay.example/g/tok",
    "https://relay.example/g/tok/v1",
    "https://ark.cn-beijing.volces.com/api/v3",
    "https://open.bigmodel.cn/api/paas/v4",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://generativelanguage.googleapis.com/v1beta/openai",
    "http://localhost:11434",
]


@pytest.mark.parametrize("name", sorted(registry.list()))
@pytest.mark.parametrize("address", _ADDRESSES)
def test_provider_and_tools_agree_on_the_root(name: str, address: str) -> None:
    wire = registry.wire_formats()[name]
    assert registry.get(name).resolve_base_url(address) == vw.normalize_base_url(wire, address)


@pytest.mark.parametrize("name", sorted(registry.list()))
def test_an_empty_address_falls_back_to_the_vendor_default(name: str) -> None:
    cls = registry.get(name)
    assert cls.resolve_base_url("") == cls.resolve_base_url(cls.default_base_url)
    assert cls.resolve_base_url(None).startswith("https://")


class TestOpenAIFamily:
    def test_a_bare_host_gains_the_version_segment(self) -> None:
        assert vw.normalize_base_url("openai", "http://localhost:11434") == "http://localhost:11434/v1"

    @pytest.mark.parametrize("address", [
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://relay.example/openai",
    ])
    def test_a_written_out_path_is_used_as_is(self, address: str) -> None:
        # 这些网关的版本段不叫 /v1，再补一个只会 404。
        assert vw.normalize_base_url("openai", address) == address

    def test_the_provider_endpoint_follows(self) -> None:
        provider = OpenAIProvider(http=None, api_key="k", base_url="https://relay.example", model="m")  # type: ignore[arg-type]
        assert provider._base_url + "/chat/completions" == "https://relay.example/v1/chat/completions"


class TestNativeFamilies:
    @pytest.mark.parametrize("address", ["https://api.anthropic.com", "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/"])
    def test_anthropic_never_doubles_the_version(self, address: str) -> None:
        provider = AnthropicProvider(http=None, api_key="k", base_url=address, model="m")  # type: ignore[arg-type]
        assert provider._endpoint == "https://api.anthropic.com/v1/messages"
        url, _headers = AnthropicProvider.catalog_request(base_url=address, api_key="k")
        assert url == "https://api.anthropic.com/v1/models"

    @pytest.mark.parametrize("address", [
        "https://generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://generativelanguage.googleapis.com/v1",
    ])
    def test_gemini_never_doubles_the_version(self, address: str) -> None:
        provider = GeminiProvider(http=None, api_key="k", base_url=address, model="flash")  # type: ignore[arg-type]
        assert provider._endpoint() == "https://generativelanguage.googleapis.com/v1beta/models/flash:generateContent"


class TestImageSlots:
    """生图槽位拼的是原生端点，同一套归一化规则管着。"""

    @staticmethod
    def _resolve(tmp_path: Path, slot: str, base_url: str):
        import _channel

        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "models.yaml").write_text("\n".join([
            "version: 1",
            "provider: openai",
            "system_models:",
            f"  {slot}:",
            f"    base_url: {base_url}",
            "    api_key: k",
        ]), encoding="utf-8")
        return _channel.resolve_image_channel(slot, root=tmp_path)

    @pytest.mark.parametrize("address", ["https://relay.example/g", "https://relay.example/g/v1", "https://relay.example/g/v1beta"])
    def test_gemini_slot_strips_any_version_segment(self, tmp_path: Path, address: str) -> None:
        assert self._resolve(tmp_path, "image_gemini", address).base_url == "https://relay.example/g"

    def test_gpt_slot_follows_the_openai_rule(self, tmp_path: Path) -> None:
        assert self._resolve(tmp_path, "image_gpt", "https://relay.example").base_url == "https://relay.example/v1"
        assert self._resolve(tmp_path, "image_gpt", "https://relay.example/v1").base_url == "https://relay.example/v1"


class TestAuthHeaders:
    """官方域名收专用头、中转站收 Bearer：对话、列模型和工具子进程挑的必须是同一套。"""

    _AUTH_ADDRESSES = [
        "",
        "https://api.anthropic.com",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://relay.example",
        "https://relay.example/proxy/api.anthropic.com",
        "https://relay.example/proxy/generativelanguage.googleapis.com",
    ]

    @pytest.mark.parametrize("provider_cls", [AnthropicProvider, GeminiProvider])
    @pytest.mark.parametrize("address", _AUTH_ADDRESSES)
    def test_chat_catalog_and_tools_agree(self, provider_cls: type, address: str) -> None:
        wire = provider_cls.provider_name
        expected = vw.headers(wire, provider_cls.resolve_base_url(address), "sk-x")
        provider = provider_cls(http=None, api_key="sk-x", base_url=address or None, model="m")

        _url, catalog_headers = provider_cls.catalog_request(base_url=address, api_key="sk-x")

        assert catalog_headers == expected
        assert provider._headers() == expected

    @pytest.mark.parametrize("wire, address, official", [
        ("anthropic", "https://api.anthropic.com", "x-api-key"),
        ("gemini", "https://generativelanguage.googleapis.com", "x-goog-api-key"),
    ])
    def test_an_official_domain_in_a_relay_path_still_gets_bearer(self, wire: str, address: str, official: str) -> None:
        # 只认主机名：中转站把官方域名写进路径时，收的仍是 Bearer。
        assert official in vw.headers(wire, address, "sk-x")
        relayed = vw.headers(wire, "https://relay.example/proxy/" + address.split("://", 1)[1], "sk-x")
        assert official not in relayed
        assert relayed["Authorization"] == "Bearer sk-x"
