"""/v1/config 与 /v1/config/openai 没有令牌门，形状里不能带密钥片段。

CLONOTH_HOST 默认 127.0.0.1，但改一个环境变量就是全网可读。
"""
from __future__ import annotations

import pytest

from supervisor.config_store import ConfigStore
from supervisor.types import OpenAIConfigUpdateIn

CONFIG = """\
version: 1
provider: openai
openai:
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-abcdefgh")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


def test_public_shape_carries_no_key_material(store):
    for public in (store.get_openai_public(), store.get_public().openai):
        dumped = public.model_dump()

        assert dumped["api_key_present"] is True
        # 尾 4 位也是密钥内容，<env:VAR> 还会连变量名一起说出去。
        assert "api_key" not in dumped
        assert "abcdefgh" not in str(dumped)
        assert "OPENAI_API_KEY" not in str(dumped)


def test_public_shape_still_reports_model_and_url(store):
    public = store.get_openai_public()

    assert public.model == "gpt-4o-mini"
    assert public.base_url == "https://relay.example.com/v1"


def test_present_is_false_when_the_reference_resolves_to_nothing(tmp_path, monkeypatch):
    # 引用了一个没设的变量 = 没有可用的钥匙，别报 True。
    monkeypatch.delenv("MISSING_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text('version: 1\nprovider: openai\nopenai:\n  api_key: "${MISSING_KEY}"\n', encoding="utf-8")

    assert ConfigStore(path=path).get_openai_public().api_key_present is False


def test_write_path_reports_present_the_same_way(tmp_path, monkeypatch):
    # 写完立刻回给调用方的那份，判定口径要和读接口一致。
    monkeypatch.delenv("MISSING_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nprovider: openai\nopenai:\n  model: gpt-4o-mini\n", encoding="utf-8")
    store = ConfigStore(path=path)

    out = store.update_openai(OpenAIConfigUpdateIn(api_key="${MISSING_KEY}"))

    assert out.openai.api_key_present is False
    assert "api_key" not in out.openai.model_dump()


def test_write_path_keeps_the_reference_in_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_KEY", "sk-live-12345678")
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nprovider: openai\nopenai:\n  model: gpt-4o-mini\n", encoding="utf-8")
    store = ConfigStore(path=path)

    out = store.update_openai(OpenAIConfigUpdateIn(api_key="${SOME_KEY}"))

    assert out.openai.api_key_present is True
    # 展开只发生在读的时候，写进 yaml 的还是那句引用。
    assert "${SOME_KEY}" in path.read_text(encoding="utf-8")


def test_admin_endpoint_still_redacts_rather_than_hiding(store):
    # /config/providers 有令牌门，那边保留尾 4 位是给管理员认渠道用的。
    openai = store.get_providers_public()["providers"]["openai"]

    assert openai["api_key_redacted"] == "****efgh"
    assert "api_key" not in openai
