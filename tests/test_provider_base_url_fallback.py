"""节点渠道的 base_url 归属。

控制台把地址填进 config.yaml 的渠道块，engine 每个任务都会重新拉这份配置；
主渠道的地址只在请求格式对得上时才能借，借错了得到的是一个跟地址无关的 404。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from engine import runner
from engine.model import ResolvedProvider
from engine.node import Node
from engine.runner import ProviderKeyMissing, _provider_init_kwargs, _rooted_provider_name

RELAY = "https://relay.example.com/g/tok"
MAIN_KEY = "sk-main"
_PROVIDER_ENVS = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("OPENAI", "OPENAI_RESPONSES", "GEMINI", "ANTHROPIC", "DEEPSEEK")
    for suffix in ("API_KEY", "BASE_URL")
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 共享密钥文件跟着工作区走：钉到临时目录，开发机上真实的 .env、config/secrets.env 混不进来。
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT", raising=False)
    for name in _PROVIDER_ENVS:
        monkeypatch.delenv(name, raising=False)


def kwargs(rp, *, active, base_url=RELAY, api_key=MAIN_KEY, main_channel=""):
    return _provider_init_kwargs(
        rp,
        provider_name=rp.provider_type,
        llm_http=httpx.AsyncClient(),
        api_key=api_key,
        base_url=base_url,
        provider_options={},
        active_provider=active,
        main_channel=main_channel,
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


class TestTheMainChannelBeatsTheProviderEnv:
    """<PROVIDER>_* 只该在节点和渠道块都没给值时才用：.env 里留着的旧值不能盖过控制台刚填的。"""

    def _following(self) -> ResolvedProvider:
        return ResolvedProvider(model="m", provider_type="gemini", channel_name="Relay-A")

    def test_the_main_block_wins_for_the_same_channel(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BASE_URL", "https://stale.example.com")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-stale")

        built = kwargs(self._following(), active="gemini", main_channel="Relay-A")

        assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)

    def test_the_env_still_fills_what_the_main_block_left_empty(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-env")

        built = kwargs(self._following(), active="gemini", base_url="", api_key="", main_channel="Relay-A")

        assert (built["base_url"], built["api_key"]) == ("https://from-env.example.com", "sk-env")

    def test_the_node_own_values_still_win(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-env")
        rp = ResolvedProvider(
            model="m", provider_type="gemini", channel_name="Relay-A",
            api_key="sk-node", base_url="https://node.example.com",
        )

        built = kwargs(rp, active="gemini", main_channel="Relay-A")

        assert (built["base_url"], built["api_key"]) == ("https://node.example.com", "sk-node")

    def test_a_bare_wire_name_still_reads_the_env_first(self, monkeypatch):
        # 节点裸写 provider: gemini、没有渠道块：那是另一个渠道，先按环境变量取。
        monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-env")
        rp = ResolvedProvider(model="m", provider_type="gemini", channel_name="gemini")

        built = kwargs(rp, active="gemini", main_channel="Relay-A")

        assert (built["base_url"], built["api_key"]) == ("https://from-env.example.com", "sk-env")

    def test_another_channel_falls_back_to_the_main_channel_last(self):
        rp = ResolvedProvider(model="m", provider_type="gemini", channel_name="gemini")

        built = kwargs(rp, active="gemini", main_channel="Relay-A")

        assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)

    def test_callers_that_name_no_main_channel_keep_the_env_first(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")

        assert kwargs(self._following(), active="gemini")["base_url"] == "https://from-env.example.com"

    def test_the_openai_family_never_reads_openai_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env.example.com")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        rp = ResolvedProvider(model="m", provider_type="openai", channel_name="openai")

        for main_channel in ("", "openai", "Relay-A"):
            built = kwargs(rp, active="openai", main_channel=main_channel)
            assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)


class TestAMissingKeyIsJudgedOnTheSameCandidates:
    """取值顺序怎么排都按同一组来源判缺不缺 key：rp、主渠道、<PROVIDER>_* 都没有才算缺。"""

    def _create(self, monkeypatch, rp, *, api_key):
        created = []

        class _Provider:
            def __init__(self, **kwargs):
                created.append(kwargs)

        monkeypatch.setattr(runner.provider_registry, "get", lambda _name: _Provider)
        runner._create_provider_from_registry(
            rp, llm_http=None, api_key=api_key, base_url=RELAY, provider_options={},
            active_provider="gemini", main_channel="Relay-A",
        )
        return created[0]

    @pytest.mark.parametrize("channel", ["Relay-A", "gemini"])
    def test_nothing_anywhere_is_still_missing(self, monkeypatch, channel):
        rp = ResolvedProvider(model="m", provider_type="gemini", channel_name=channel)

        with pytest.raises(ProviderKeyMissing) as missing:
            self._create(monkeypatch, rp, api_key="")
        assert missing.value.channel == channel

    @pytest.mark.parametrize("channel", ["Relay-A", "gemini"])
    def test_an_env_key_alone_is_still_enough(self, monkeypatch, channel):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-env")
        rp = ResolvedProvider(model="m", provider_type="gemini", channel_name=channel)

        assert self._create(monkeypatch, rp, api_key="")["api_key"] == "sk-env"

    @pytest.mark.parametrize("channel", ["Relay-A", "gemini"])
    def test_a_main_key_alone_is_still_enough(self, monkeypatch, channel):
        rp = ResolvedProvider(model="m", provider_type="gemini", channel_name=channel)

        assert self._create(monkeypatch, rp, api_key=MAIN_KEY)["api_key"] == MAIN_KEY


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

    @pytest.mark.parametrize("wire", ["openai", "openai-responses"])
    def test_an_openai_address_in_the_env_does_not_count(self, monkeypatch, wire):
        # 构造 OpenAI 族时从不读 OPENAI_BASE_URL：这里认了它，就会拿主渠道的 key 去打 api.openai.com。
        monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env.example.com")
        monkeypatch.setenv("OPENAI_RESPONSES_BASE_URL", "https://from-env.example.com")
        rp = ResolvedProvider(model="m", provider_type=wire)

        assert _rooted_provider_name(rp, active_provider="gemini") == "gemini"


class TestTheRealPath:
    """_build_node_provider 一整条：渠道解析、无根判定、取值顺序。"""

    MAIN = {
        "api_key": MAIN_KEY, "base_url": RELAY, "default_model": "main-model",
        "default_provider": "gemini", "default_channel": "Relay-A",
    }
    RUNTIME = {"providers": {
        "gemini": {"options": {"safety_threshold": "off"}},
        "openai": {"options": {"temperature": 0.9}},
        "anthropic": {"options": {"max_tokens": 2048}},
    }}

    def _build(self, root: Path, monkeypatch, node: Node, models: str = "") -> tuple[object, str, dict]:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "models.yaml").write_text(models, encoding="utf-8")
        created: list[dict] = []
        requested: list[str] = []

        class _Recorder:
            def __init__(self, **kwargs):
                created.append(kwargs)

        def get(name):
            requested.append(name)
            return _Recorder

        monkeypatch.setattr(runner.provider_registry, "get", get)
        provider = asyncio.run(runner._build_node_provider(
            ws_root=root, node=node, http=None, llm_http=object(), sup_url="http://sup", route_session_id="",
            runtime_cfg=self.RUNTIME, default_options={"thinking_mode": "on"}, **self.MAIN,
        ))
        return provider, requested[-1], created[-1]

    def test_stale_env_does_not_beat_the_main_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_BASE_URL", "https://stale.example.com")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-stale")

        provider, wire, built = self._build(tmp_path, monkeypatch, Node(id="n", type="ai"))

        assert (wire, provider.plugin_channel) == ("gemini", "Relay-A")
        assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)
        assert built["provider_options"] == {"safety_threshold": "off", "thinking_mode": "on"}

    def test_a_bare_provider_still_reads_its_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_BASE_URL", "https://from-env.example.com")
        monkeypatch.setenv("GEMINI_API_KEY", "sk-env")

        provider, wire, built = self._build(tmp_path, monkeypatch, Node(id="n", type="ai", provider="gemini"))

        assert (wire, provider.plugin_channel) == ("gemini", "gemini")
        assert (built["base_url"], built["api_key"]) == ("https://from-env.example.com", "sk-env")
        # 另一个渠道：不带主渠道块的 options。
        assert built["provider_options"] == {"safety_threshold": "off"}

    def test_an_unrooted_declaration_goes_out_entirely_on_the_main_channel(self, tmp_path, monkeypatch, caplog):
        # 模板 .env 里就留着 OPENAI_BASE_URL；以它为根，会把 gemini 主渠道的 key 发到 api.openai.com。
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

        with caplog.at_level(logging.WARNING, logger=runner.logger.name):
            provider, wire, built = self._build(tmp_path, monkeypatch, Node(id="chat.vision", type="ai", provider="openai"))

        assert (wire, provider.plugin_channel) == ("gemini", "Relay-A")
        assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)
        assert built["provider_options"] == {"safety_threshold": "off", "thinking_mode": "on"}
        assert any("chat.vision" in record.getMessage() for record in caplog.records if record.name == runner.logger.name)

    def test_an_unrooted_block_key_never_reaches_the_main_address(self, tmp_path, monkeypatch):
        # 有块没地址（${ANTHROPIC_BASE_URL} 没设）也算无根、改用主渠道，块里的 key 得一起清掉。
        models = "anthropic:\n  base_url: ${ANTHROPIC_BASE_URL}\n  api_key: sk-ant\n  options:\n    thinking: true\n"

        provider, wire, built = self._build(tmp_path, monkeypatch, Node(id="n", type="ai", provider="anthropic"), models)

        assert (wire, provider.plugin_channel) == ("gemini", "Relay-A")
        assert (built["base_url"], built["api_key"]) == (RELAY, MAIN_KEY)
        assert built["provider_options"] == {"safety_threshold": "off", "thinking_mode": "on"}

    def test_a_rooted_declaration_keeps_its_own_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        models = "anthropic:\n  base_url: ${ANTHROPIC_BASE_URL}\n  api_key: sk-ant\n  options:\n    thinking: true\n"

        provider, wire, built = self._build(tmp_path, monkeypatch, Node(id="n", type="ai", provider="anthropic"), models)

        assert (wire, provider.plugin_channel) == ("anthropic", "anthropic")
        assert (built["base_url"], built["api_key"]) == ("https://api.anthropic.com", "sk-ant")
        assert built["provider_options"] == {"max_tokens": 2048, "thinking": True}

    def test_node_options_ride_along_while_the_wire_stays(self, tmp_path, monkeypatch):
        node = Node(id="n", type="ai", provider_options={"thinking_mode": "off", "top_k": 4})

        _provider, wire, built = self._build(tmp_path, monkeypatch, node)

        assert wire == "gemini"
        assert built["provider_options"] == {"safety_threshold": "off", "thinking_mode": "off", "top_k": 4}

    def test_node_options_stay_behind_when_the_request_is_rerouted(self, tmp_path, monkeypatch):
        # 节点 options 是照它声明的 anthropic 写的，改用 gemini 主渠道发时键名对不上，和备用渠道一样不带
        node = Node(id="n", type="ai", provider="anthropic", provider_options={"thinking": {"budget_tokens": 512}})

        _provider, wire, built = self._build(tmp_path, monkeypatch, node)

        assert wire == "gemini"
        assert built["provider_options"] == {"safety_threshold": "off", "thinking_mode": "on"}

    def test_node_options_stay_behind_when_a_slot_switches_the_wire(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
        for suffix in ("MODEL", "BASE_URL", "API_KEY", "PROVIDER"):
            monkeypatch.delenv(f"CLONOTH_INTENT_{suffix}", raising=False)
        models = (
            "claude-relay:\n  type: anthropic\n  base_url: https://claude.example.com\n  api_key: sk-claude\n"
            "system_models:\n  intent:\n    provider: claude-relay\n"
        )
        node = Node(
            id="judge", type="ai", provider_options={"thinking_mode": "off"},
            extra={"system_model_slot": "intent"},
        )

        provider, wire, built = self._build(tmp_path, monkeypatch, node, models)

        assert (wire, provider.plugin_channel) == ("anthropic", "claude-relay")
        assert built["provider_options"] == {"max_tokens": 2048}
