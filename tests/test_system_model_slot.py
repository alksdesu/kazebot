"""节点用哪个 system_models 槽位，由节点 yaml 自己说了算。

原来是 runner 里写死的 id→slot 映射。意愿判断节点的 id 由 qq.yaml 配置，运营者改个
节点名就会静默丢掉独立渠道、回落主渠道模型 —— 而它每条不命中的群消息都要跑一次。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import resolve_system_model  # noqa: E402
from engine.node import load_node  # noqa: E402
from engine.runner import _resolve_system_model_slot  # noqa: E402


def _node(node_id: str, **extra):
    return SimpleNamespace(id=node_id, extra=dict(extra))


class TestSlotResolution:
    def test_a_declared_slot_wins(self) -> None:
        assert _resolve_system_model_slot(_node("qq.intent", system_model_slot="intent")) == "intent"

    def test_a_renamed_node_keeps_its_slot(self) -> None:
        assert _resolve_system_model_slot(_node("qq.my_gate", system_model_slot="intent")) == "intent"

    def test_the_declaration_overrides_the_legacy_mapping(self) -> None:
        assert _resolve_system_model_slot(
            _node("system.compactor", system_model_slot="summary"),
        ) == "summary"

    def test_the_legacy_compactor_mapping_still_applies(self) -> None:
        assert _resolve_system_model_slot(_node("system.compactor")) == "compact"

    def test_the_legacy_summarizer_mapping_still_applies(self) -> None:
        assert _resolve_system_model_slot(_node("system.turn_summarizer")) == "summary"

    def test_an_ordinary_node_has_no_slot(self) -> None:
        assert _resolve_system_model_slot(_node("qq.orchestrator")) == ""

    def test_a_node_without_extra_does_not_explode(self) -> None:
        assert _resolve_system_model_slot(SimpleNamespace(id="qq.orchestrator")) == ""

    def test_the_declaration_is_case_insensitive(self) -> None:
        assert _resolve_system_model_slot(_node("x", system_model_slot=" Intent ")) == "intent"


class TestTheIntentNodeShipsWired:
    def test_the_shipped_node_declares_the_intent_slot(self) -> None:
        node = load_node(_ROOT, "qq.intent")

        assert node is not None
        assert _resolve_system_model_slot(node) == "intent"

    def test_the_shipped_node_calls_no_tools(self) -> None:
        # 判定节点拿到工具就可能真的去干活。它只该回一个 yes/no。
        node = load_node(_ROOT, "qq.intent")

        assert node.tool_access.mode == "none"

    def test_the_shipped_node_is_not_switchable(self) -> None:
        # 不排除的话，主对话节点的 switch_node 枚举里会多出一个「切过去就回
        # no|群友之间在对话」的目标。
        from engine.runner import _discover_switchable_nodes

        listed = {n["id"] for n in _discover_switchable_nodes(_ROOT, "qq.orchestrator")}

        assert "qq.intent" not in listed

    def test_ordinary_nodes_are_still_switchable(self) -> None:
        from engine.runner import _discover_switchable_nodes

        listed = {n["id"] for n in _discover_switchable_nodes(_ROOT, "qq.orchestrator")}

        assert "qq.vision" in listed

    def test_the_shipped_node_asks_for_finish(self) -> None:
        node = load_node(_ROOT, "qq.intent")

        assert "finish" in node.prompt
        assert "yes" in node.prompt and "no" in node.prompt


class TestTheSlotProvider:
    """槽位换 url/key 不换请求格式的话，等于拿 A 家的格式打 B 家的地址。"""

    def test_provider_comes_from_config_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "config.yaml").write_text(
            "\n".join([
                "system_models:",
                "  compact:",
                "    provider: anthropic",
                "    model: claude-haiku-4-5",
            ]),
            encoding="utf-8",
        )

        slot = resolve_system_model(tmp_path, "compact")

        assert slot.provider == "anthropic"
        assert slot.model == "claude-haiku-4-5"

    def test_provider_comes_from_the_slot_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_SUMMARY_PROVIDER", "  DeepSeek ")

        assert resolve_system_model(tmp_path, "summary").provider == "deepseek"

    def test_an_unset_provider_means_follow_the_main_channel(self, tmp_path: Path) -> None:
        # 空串是「跟随」的信号，调用方靠它决定要不要覆盖。
        assert resolve_system_model(tmp_path, "compact").provider == ""

    def test_the_caller_default_is_used_when_nothing_is_configured(self, tmp_path: Path) -> None:
        assert resolve_system_model(
            tmp_path, "compact", default_provider="gemini",
        ).provider == "gemini"

    def test_each_slot_keeps_its_own_provider(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_COMPACT_PROVIDER", "anthropic")

        assert resolve_system_model(tmp_path, "compact").provider == "anthropic"
        assert resolve_system_model(tmp_path, "intent").provider == ""


class TestTheIntentEnvPrefix:
    def test_the_intent_slot_reads_its_own_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_INTENT_MODEL", "cheap-mini")
        monkeypatch.setenv("CLONOTH_INTENT_BASE_URL", "https://intent.example/v1")

        slot = resolve_system_model(tmp_path, "intent", default_model="main-model")

        assert slot.model == "cheap-mini"
        assert slot.base_url == "https://intent.example/v1"

    def test_an_unset_intent_slot_follows_the_main_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("CLONOTH_INTENT_MODEL", "CLONOTH_INTENT_BASE_URL", "CLONOTH_INTENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)

        assert resolve_system_model(
            tmp_path, "intent", default_model="main-model",
        ).model == "main-model"

    def test_config_yaml_beats_the_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_INTENT_MODEL", "from-env")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "config.yaml").write_text(
            "system_models:\n  intent:\n    model: from-config\n", encoding="utf-8",
        )

        assert resolve_system_model(
            tmp_path, "intent", default_model="main-model",
        ).model == "from-config"

    def test_the_compact_slot_is_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_COMPACT_MODEL", "compact-mini")
        monkeypatch.setenv("CLONOTH_INTENT_MODEL", "intent-mini")

        assert resolve_system_model(tmp_path, "compact", default_model="main").model == "compact-mini"
