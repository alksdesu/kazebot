"""GET /config/providers 的对外形状：密钥不出门，${VAR} 那层间接不能被编辑器烧掉。"""
from __future__ import annotations

import pytest
import yaml

from supervisor.config_store import ConfigStore

CONFIG_WITH_SECRETS = """\
version: 1
provider: openai
openai:
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"
deepseek:
  base_url: https://api.deepseek.com
  api_key: sk-plaintext-1234567890
  model: deepseek-v4-pro
fallbacks:
  - provider: deepseek
  - provider: relay
    model: "${RELAY_MODEL}"
    base_url: https://relay.example.com/v1
    api_key: sk-fallback-secret-9876
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-abcdefgh")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("RELAY_MODEL", "relay/qwen3-max")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_WITH_SECRETS, encoding="utf-8")
    return ConfigStore(path=path)


def test_provider_block_reports_both_resolved_and_raw(store):
    openai = store.get_providers_public()["providers"]["openai"]

    assert openai["model"] == "gpt-4o-mini"
    assert openai["base_url"] == "https://api.openai.com/v1"
    # raw 是编辑器要回填的那一份 —— 没有它，保存一次就把间接层写死了。
    assert openai["model_raw"] == "${OPENAI_MODEL}"
    assert openai["base_url_raw"] == "${OPENAI_BASE_URL}"


def test_api_key_has_no_raw_counterpart(store):
    openai = store.get_providers_public()["providers"]["openai"]
    plaintext = store.get_providers_public()["providers"]["deepseek"]

    assert "api_key_raw" not in openai
    assert "api_key" not in openai
    assert openai["api_key_present"] is True
    assert openai["api_key_redacted"] == "****efgh"
    # 明文写在 yaml 里的那份同样只留尾 4 位。
    assert plaintext["api_key_redacted"] == "****7890"


def test_fallback_entries_never_carry_their_api_key(store):
    fallbacks = store.get_providers_public()["fallbacks"]
    relay = next(fb for fb in fallbacks if fb["provider"] == "relay")

    # 备选条目允许写自己的 key，读接口一路原样透传过就是把它交给前端。
    assert "api_key" not in relay
    assert relay["api_key_present"] is True
    assert relay["api_key_redacted"] == "****9876"
    assert "sk-fallback-secret-9876" not in str(fallbacks)


def test_fallback_entries_expand_and_keep_raw_like_provider_blocks(store):
    relay = next(fb for fb in store.get_providers_public()["fallbacks"] if fb["provider"] == "relay")

    assert relay["model"] == "relay/qwen3-max"
    assert relay["model_raw"] == "${RELAY_MODEL}"
    assert relay["base_url"] == "https://relay.example.com/v1"


def test_fallback_entry_without_own_fields_stays_empty(store):
    # 只写 provider 名的条目由引擎去继承 provider 块，读接口不替它猜。
    bare = next(fb for fb in store.get_providers_public()["fallbacks"] if fb["provider"] == "deepseek")

    assert bare["model"] == ""
    assert bare["api_key_present"] is False


def test_unknown_fallback_keys_are_dropped(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprovider: openai\n"
        "openai:\n  model: gpt-4o-mini\n  api_key: sk-x\n"
        "fallbacks:\n  - provider: openai\n    private_token: sk-should-not-leak\n",
        encoding="utf-8",
    )

    fallbacks = ConfigStore(path=path).get_providers_public()["fallbacks"]

    assert "private_token" not in fallbacks[0]
    assert "sk-should-not-leak" not in str(fallbacks)


def test_saving_raw_value_back_keeps_the_env_indirection(store):
    # 编辑器把 model_raw 原样提交回来，config.yaml 里就仍然是 ${OPENAI_MODEL}。
    public = store.get_providers_public()["providers"]["openai"]
    store.upsert_provider("openai", model=public["model_raw"])

    assert "${OPENAI_MODEL}" in store.path.read_text(encoding="utf-8")
    assert store.get_providers_public()["providers"]["openai"]["model"] == "gpt-4o-mini"


def test_saving_the_resolved_value_would_burn_the_indirection(store):
    # 反过来记一笔：提交展开值就是把间接层写死。前端必须回填 raw，不是这一份。
    store.upsert_provider("openai", model=store.get_providers_public()["providers"]["openai"]["model"])

    assert "${OPENAI_MODEL}" not in store.path.read_text(encoding="utf-8")


def test_update_fallbacks_strips_readonly_fields(store):
    public = store.get_providers_public()["fallbacks"]
    store.update_fallbacks(public)

    written = store.path.read_text(encoding="utf-8")
    for key in ("model_raw", "base_url_raw", "api_key_present", "api_key_redacted"):
        assert key not in written


def test_editing_a_provider_keeps_its_options(tmp_path):
    """备选渠道的请求参数写在 provider 块里，改一下模型名不该把它带走。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprovider: openai\n"
        "openai:\n  model: gpt-4o-mini\n  api_key: sk-x\n"
        "deepseek:\n  model: deepseek-v4-pro\n  api_key: sk-d\n"
        "  options:\n    thinking: false\n    reasoning_effort: high\n",
        encoding="utf-8",
    )
    store = ConfigStore(path=path)

    store.upsert_provider("deepseek", model="deepseek-v4-flash")

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["deepseek"]["model"] == "deepseek-v4-flash"
    assert saved["deepseek"]["options"] == {"thinking": False, "reasoning_effort": "high"}


def test_the_openai_block_keeps_keys_the_model_does_not_declare(tmp_path):
    """openai 块走 Pydantic、别的块走自由 dict，只有它会在回写时掉字段。

    reload() 每次启动都按 model_dump 回写一遍。丢字段是静默的：手写的
    supports_vision / options 下次启动就没了，而 fallback 链在读它们。
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprovider: openai\n"
        "openai:\n  model: gpt-4o-mini\n  api_key: sk-x\n"
        "  supports_vision: false\n"
        "  options:\n    thinking: true\n",
        encoding="utf-8",
    )

    ConfigStore(path=path)

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["openai"]["supports_vision"] is False
    assert saved["openai"]["options"] == {"thinking": True}


def test_provider_options_are_not_published_to_the_browser(tmp_path):
    """公开视图按白名单挑字段。options 里可以出现缓存分组之类的自定义串，不外发。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprovider: openai\n"
        "openai:\n  model: gpt-4o-mini\n  api_key: sk-x\n"
        "  options:\n    prompt_cache_key: internal-tenant-42\n",
        encoding="utf-8",
    )

    public = ConfigStore(path=path).get_providers_public()

    assert "internal-tenant-42" not in str(public)
