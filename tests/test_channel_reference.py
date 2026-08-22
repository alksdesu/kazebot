"""节点、备选链、系统槽位指向一个命名渠道时的解析。

以前 provider 只能填线格式名，填渠道名会被静默清空或整条跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import resolve_channel  # noqa: E402
from engine.builtin.fallback_provider import _resolve_fallback_entry  # noqa: E402
from engine.model import resolve_provider  # noqa: E402
from engine.node import Node, load_node  # noqa: E402

CONFIG = """version: 1
provider: openai
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-openai
  model: gpt-5.4
gemini-中转A:
  type: gemini
  base_url: https://relay-a.example/v1
  api_key: sk-relay-a
  model: gemini-3-pro
gemini-官方:
  type: gemini
  base_url: https://generativelanguage.googleapis.com
  api_key: sk-official
  model: gemini-3-flash
deepseek:
  base_url: https://api.deepseek.com
  api_key: sk-deepseek
  model: deepseek-v4
带参数的:
  type: anthropic
  base_url: https://api.anthropic.com
  api_key: sk-ant
  options:
    thinking: true
不是渠道:
  note: 只是一段备注
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


class TestResolveChannel:
    def test_a_named_channel_reports_its_declared_wire(self, root: Path) -> None:
        channel = resolve_channel(root, "gemini-中转A")
        assert channel.known
        assert channel.wire == "gemini"
        assert channel.base_url == "https://relay-a.example/v1"
        assert channel.api_key == "sk-relay-a"
        assert channel.model == "gemini-3-pro"

    def test_a_block_without_type_falls_back_to_its_name(self, root: Path) -> None:
        # 老配置里块名本来就是家族名，没有 type 也得照旧能用。
        assert resolve_channel(root, "deepseek").wire == "deepseek"

    def test_a_bare_wire_name_is_not_a_channel(self, root: Path) -> None:
        channel = resolve_channel(root, "anthropic")
        assert not channel.known
        assert channel.wire == "anthropic"
        assert channel.base_url == ""

    def test_the_name_is_case_sensitive(self, root: Path) -> None:
        # 块名原样存盘，转小写就查不到了。
        assert not resolve_channel(root, "GEMINI-中转A").known

    def test_a_non_channel_block_is_not_mistaken_for_one(self, root: Path) -> None:
        assert not resolve_channel(root, "不是渠道").known

    def test_meta_keys_are_never_channels(self, root: Path) -> None:
        # provider / fallbacks 这些顶层键不是渠道，认成渠道会解析出一堆空值。
        assert not resolve_channel(root, "fallbacks").known
        assert not resolve_channel(root, "provider").known

    def test_an_empty_name_resolves_to_nothing(self, root: Path) -> None:
        channel = resolve_channel(root, "")
        assert not channel.known
        assert channel.wire == ""

    def test_env_refs_are_expanded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_KEY", "sk-from-env")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "config.yaml").write_text(
            "relay:\n  type: openai\n  api_key: $ENV{RELAY_KEY}\n", encoding="utf-8",
        )
        assert resolve_channel(tmp_path, "relay").api_key == "sk-from-env"


class TestNodeKeepsWhatWasWritten:
    def _node(self, root: Path, body: str) -> Node:
        (root / "config" / "nodes").mkdir(parents=True, exist_ok=True)
        (root / "config" / "nodes" / "n.yaml").write_text(body, encoding="utf-8")
        node = load_node(root, "n")
        assert node is not None
        return node

    def test_a_channel_name_survives_loading(self, root: Path) -> None:
        # 以前这里按线格式白名单过滤，渠道名会被静默换成空串。
        assert self._node(root, "id: n\ntype: ai\nprovider: gemini-中转A\n").provider == "gemini-中转A"

    def test_the_case_is_preserved(self, root: Path) -> None:
        assert self._node(root, "id: n\ntype: ai\nprovider: Relay-A\n").provider == "Relay-A"

    def test_an_absent_provider_is_still_empty(self, root: Path) -> None:
        assert self._node(root, "id: n\ntype: ai\n").provider == ""


class TestNodeProviderResolution:
    def _node(self, **kwargs) -> Node:
        return Node(id="n", name="n", type="ai", **kwargs)

    def test_pointing_at_a_named_channel_borrows_everything(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(provider="gemini-中转A"), "gpt-5.4")
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://relay-a.example/v1"
        assert rp.api_key == "sk-relay-a"
        assert rp.model == "gemini-3-pro"

    def test_the_node_own_fields_beat_the_channel_block(self, root: Path) -> None:
        rp = resolve_provider(
            root,
            self._node(provider="gemini-中转A", model="gemini-3-flash",
                       base_url="https://node.example/v1"),
            "gpt-5.4",
        )
        assert rp.provider_type == "gemini"
        assert rp.model == "gemini-3-flash"
        assert rp.base_url == "https://node.example/v1"
        # 节点没写 key，仍然从渠道块继承。
        assert rp.api_key == "sk-relay-a"

    def test_two_channels_of_the_same_family_stay_apart(self, root: Path) -> None:
        a = resolve_provider(root, self._node(provider="gemini-中转A"), "gpt-5.4")
        b = resolve_provider(root, self._node(provider="gemini-官方"), "gpt-5.4")
        assert a.provider_type == b.provider_type == "gemini"
        assert a.base_url != b.base_url
        assert a.api_key != b.api_key

    def test_a_bare_wire_name_still_works(self, root: Path) -> None:
        # 只想换请求格式、地址走环境变量的节点仍然这么写。
        rp = resolve_provider(root, self._node(provider="anthropic"), "gpt-5.4")
        assert rp.provider_type == "anthropic"
        assert rp.base_url is None
        assert rp.api_key is None

    def test_an_old_config_now_inherits_its_block(self, root: Path) -> None:
        # 行为变更：以前只取 wire，url/key 得靠 DEEPSEEK_API_KEY 之类的环境变量。
        rp = resolve_provider(root, self._node(provider="deepseek"), "gpt-5.4")
        assert rp.provider_type == "deepseek"
        assert rp.base_url == "https://api.deepseek.com"
        assert rp.api_key == "sk-deepseek"

    def test_no_provider_follows_the_active_channel(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(), "gpt-5.4", provider_default_type="openai")
        assert rp.provider_type == "openai"
        assert rp.model == "gpt-5.4"
        assert rp.base_url is None

    def test_the_channel_options_come_along(self, root: Path) -> None:
        rp = resolve_provider(root, self._node(provider="带参数的"), "gpt-5.4")
        assert rp.channel_options == {"thinking": True}

    def test_an_unknown_name_is_left_for_the_registry_to_reject(self, root: Path) -> None:
        # 拼错的名字不在这里吞掉：runner 拿它查 registry 落空时会打一行日志。
        rp = resolve_provider(root, self._node(provider="gemini-中转B"), "gpt-5.4")
        assert rp.provider_type == "gemini-中转b"
        assert rp.base_url is None


class TestSessionOverrideBeatsTheNode:
    def _node(self) -> Node:
        return Node(id="n", name="n", type="ai", provider="deepseek")

    def test_the_override_channel_replaces_the_node_one(self, root: Path) -> None:
        rp = resolve_provider(
            root, self._node(), "gpt-5.4",
            session_override={"provider": "gemini-中转A"},
        )
        assert rp.provider_type == "gemini"
        assert rp.base_url == "https://relay-a.example/v1"

    def test_an_inline_key_is_not_overwritten_by_the_block(self, root: Path) -> None:
        # 会话里临时贴一把 key 的用法，不能被渠道块顶掉。
        rp = resolve_provider(
            root, self._node(), "gpt-5.4",
            session_override={"provider": "gemini-中转A", "api_key": "sk-session"},
        )
        assert rp.api_key == "sk-session"
        assert rp.base_url == "https://relay-a.example/v1"


class TestFallbackEntries:
    def _cfg(self) -> dict:
        import yaml
        return yaml.safe_load(CONFIG)

    def test_a_named_channel_resolves_to_its_wire(self) -> None:
        # 以前这里把渠道名原样当线格式送进 registry，整条备选被静默跳过。
        entry = _resolve_fallback_entry({"provider": "gemini-中转A"}, self._cfg())
        assert entry["wire"] == "gemini"
        assert entry["provider"] == "gemini-中转A"
        assert entry["base_url"] == "https://relay-a.example/v1"
        assert entry["api_key"] == "sk-relay-a"
        assert entry["model"] == "gemini-3-pro"

    def test_a_family_named_block_still_works(self) -> None:
        entry = _resolve_fallback_entry({"provider": "deepseek"}, self._cfg())
        assert entry["wire"] == "deepseek"
        assert entry["base_url"] == "https://api.deepseek.com"

    def test_the_entry_overrides_the_block(self) -> None:
        entry = _resolve_fallback_entry(
            {"provider": "gemini-中转A", "model": "gemini-3-flash"}, self._cfg(),
        )
        assert entry["model"] == "gemini-3-flash"
        assert entry["api_key"] == "sk-relay-a"

    def test_block_options_are_inherited_then_overlaid(self) -> None:
        entry = _resolve_fallback_entry(
            {"provider": "带参数的", "options": {"max_tokens": 100}}, self._cfg(),
        )
        assert entry["options"] == {"thinking": True, "max_tokens": 100}

    def test_an_unconfigured_name_yields_no_credentials(self) -> None:
        entry = _resolve_fallback_entry({"provider": "gemini"}, self._cfg())
        assert entry["wire"] == "gemini"
        assert entry["base_url"] == ""
        assert entry["api_key"] == ""
