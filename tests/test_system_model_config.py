"""系统槽位的读写。控制台照这份清单渲染，密钥只报有无。"""
from __future__ import annotations

import pytest
import yaml

from clonoth_runtime import SYSTEM_MODEL_SLOTS
from supervisor.config_store import ConfigStore

CONFIG = "\n".join([
    "version: 1",
    "provider: openai",
    "openai:",
    "  base_url: https://relay.example/v1",
    "  api_key: sk-main",
    "  model: gpt-5.4",
    "system_models:",
    "  compact:",
    '    model: "${COMPACT_MODEL}"',
    "    api_key: sk-compact-secret",
    "  image_gpt:",
    "    base_url: https://images.example/v1",
])


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPACT_MODEL", "gemini-3.5-flash")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


def _slot(store, key):
    return next(s for s in store.get_system_models_public()["slots"] if s["key"] == key)


class TestPublicShape:
    def test_every_registered_slot_is_listed_even_when_unconfigured(self, store):
        keys = [s["key"] for s in store.get_system_models_public()["slots"]]
        assert keys == [spec.key for spec in SYSTEM_MODEL_SLOTS]
        assert "image_gemini" in keys

    def test_secrets_never_leave_the_process(self, store):
        dumped = str(store.get_system_models_public())
        assert "sk-compact-secret" not in dumped
        assert _slot(store, "compact")["api_key_present"] is True
        assert _slot(store, "image")["api_key_present"] is False

    def test_both_the_raw_template_and_its_value_are_reported(self, store):
        compact = _slot(store, "compact")
        # 回填 raw，展示 resolved —— 回填展开值就把这层间接烧死了。
        assert compact["model_raw"] == "${COMPACT_MODEL}"
        assert compact["model"] == "gemini-3.5-flash"

    def test_tool_slots_do_not_offer_a_provider(self, store):
        # 工具子进程的请求格式写死在源码里，配了也不生效。
        assert _slot(store, "compact")["supports_provider"] is True
        assert _slot(store, "intent")["supports_provider"] is True
        for key in ("image", "image_gpt", "image_gemini"):
            assert _slot(store, key)["supports_provider"] is False

    def test_the_env_prefix_matches_what_the_tools_read(self, store):
        assert _slot(store, "image_gpt")["env_prefix"] == "CLONOTH_IMAGE_GPT"
        assert _slot(store, "image_gemini")["env_prefix"] == "CLONOTH_IMAGE_GEMINI"


class TestUpdate:
    def test_it_writes_the_slot(self, store):
        store.update_system_model("image_gemini", model="gemini-4-image", api_key="sk-img")
        assert _slot(store, "image_gemini")["model"] == "gemini-4-image"
        assert _slot(store, "image_gemini")["api_key_present"] is True

    def test_an_empty_string_removes_the_key_entirely(self, store, tmp_path):
        # 留个空串在 yaml 里会被当成「配了个空值」，跟随主渠道就失效了。
        store.update_system_model("image_gpt", base_url="")
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert "image_gpt" not in (raw.get("system_models") or {})

    def test_none_leaves_a_field_alone(self, store):
        store.update_system_model("compact", base_url="https://other.example/v1")
        assert _slot(store, "compact")["model_raw"] == "${COMPACT_MODEL}"
        assert _slot(store, "compact")["api_key_present"] is True

    def test_clearing_the_last_slot_drops_the_whole_section(self, store, tmp_path):
        store.update_system_model("compact", model="", api_key="")
        store.update_system_model("image_gpt", base_url="")
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert "system_models" not in raw

    def test_an_unknown_slot_is_refused(self, store):
        with pytest.raises(ValueError):
            store.update_system_model("image_dalle", model="x")

    def test_other_config_survives_the_write(self, store, tmp_path):
        store.update_system_model("summary", model="cheap")
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert raw["openai"]["api_key"] == "sk-main"
        assert raw["provider"] == "openai"

    def test_the_slot_is_not_mistaken_for_a_provider_block(self, store):
        store.update_system_model("summary", model="cheap", base_url="https://s.example/v1")
        assert "system_models" not in store.get_providers_public()["providers"]
