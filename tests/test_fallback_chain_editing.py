"""备选链在界面上改一次，不能把界面看不见的东西一起改没。

编辑器读到的是脱敏后的白名单视图：api_key 只有一个星号串，手写进 yaml 的键它
压根看不见。所以保存必须是按条目合并，不是整条链覆盖 —— 后者会让「保存一次」
等于「清空密钥、关掉视觉支持、抹掉请求参数」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.config_store import ConfigStore  # noqa: E402

_CONFIG = """version: 1
provider: openai
openai:
  base_url: https://api.openai.com/v1
  api_key: sk-main
  model: gpt-4o-mini
deepseek:
  base_url: https://api.deepseek.com
  api_key: sk-block
  model: deepseek-v4-pro
fallbacks:
  - provider: deepseek
  - provider: openai
    base_url: https://relay.example.test/v1
    api_key: sk-inline-secret
    model: backup
    supports_vision: true
    options:
      reasoning_effort: high
      handwritten_knob: keep-me
node_fallbacks:
  system.compactor:
    - provider: deepseek
      model: deepseek-v4-flash
  qq.vision: []
"""


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    return ConfigStore(path=path)


def _saved(store: ConfigStore) -> dict:
    return yaml.safe_load(store.path.read_text(encoding="utf-8"))


def _echo(public: dict) -> list[dict]:
    """模拟编辑器：把读到的公开视图原样提交回去，只补上原始位置。"""
    return [{**entry, "_origin": index} for index, entry in enumerate(public["fallbacks"])]


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def test_inline_api_key_never_reaches_the_browser(store: ConfigStore) -> None:
    public = store.get_providers_public()
    assert "sk-inline-secret" not in str(public)
    assert public["fallbacks"][1]["api_key_present"] is True


def test_editor_can_see_the_fields_it_must_edit(store: ConfigStore) -> None:
    entry = store.get_providers_public()["fallbacks"][1]
    assert entry["provider"] == "openai"
    assert entry["model"] == "backup"
    assert entry["supports_vision"] is True
    assert entry["options"]["reasoning_effort"] == "high"


def test_undeclared_options_stay_out_of_the_public_view(store: ConfigStore) -> None:
    """界面只认 provider 声明过的参数，手写的杂项不摆出来。"""
    entry = store.get_providers_public()["fallbacks"][1]
    assert "handwritten_knob" not in entry["options"]


def test_node_chains_are_published_including_the_empty_one(store: ConfigStore) -> None:
    chains = store.get_providers_public()["node_fallbacks"]
    assert chains["system.compactor"][0]["model"] == "deepseek-v4-flash"
    # 空列表表示「这个节点禁用 fallback」，丢掉它就变成了「跟随全局」。
    assert chains["qq.vision"] == []


# ---------------------------------------------------------------------------
# 写：原样存回不该改变任何东西
# ---------------------------------------------------------------------------

def test_saving_untouched_keeps_the_inline_api_key(store: ConfigStore) -> None:
    store.update_fallbacks(_echo(store.get_providers_public()))
    assert _saved(store)["fallbacks"][1]["api_key"] == "sk-inline-secret"


def test_saving_untouched_keeps_supports_vision(store: ConfigStore) -> None:
    store.update_fallbacks(_echo(store.get_providers_public()))
    assert _saved(store)["fallbacks"][1]["supports_vision"] is True


def test_saving_untouched_keeps_handwritten_options(store: ConfigStore) -> None:
    store.update_fallbacks(_echo(store.get_providers_public()))
    assert _saved(store)["fallbacks"][1]["options"]["handwritten_knob"] == "keep-me"


def test_saving_untouched_leaves_the_bare_entry_bare(store: ConfigStore) -> None:
    """只写了 provider 的条目靠继承同名块取值，不该被撑出一堆空字段。"""
    store.update_fallbacks(_echo(store.get_providers_public()))
    first = _saved(store)["fallbacks"][0]
    assert first["provider"] == "deepseek"
    assert not first.get("api_key")


# ---------------------------------------------------------------------------
# 写：改动要生效
# ---------------------------------------------------------------------------

def test_editing_a_field_takes_effect(store: ConfigStore) -> None:
    payload = _echo(store.get_providers_public())
    payload[1]["model"] = "backup-v2"
    payload[1]["supports_vision"] = False
    store.update_fallbacks(payload)
    saved = _saved(store)["fallbacks"][1]
    assert saved["model"] == "backup-v2"
    assert saved["supports_vision"] is False
    assert saved["api_key"] == "sk-inline-secret"


def test_changing_an_option_keeps_its_siblings(store: ConfigStore) -> None:
    payload = _echo(store.get_providers_public())
    payload[1]["options"] = {"reasoning_effort": "low"}
    store.update_fallbacks(payload)
    options = _saved(store)["fallbacks"][1]["options"]
    assert options["reasoning_effort"] == "low"
    assert options["handwritten_knob"] == "keep-me"


def test_explicit_null_removes_a_key(store: ConfigStore) -> None:
    payload = _echo(store.get_providers_public())
    payload[1]["options"] = {"reasoning_effort": None}
    payload[1]["base_url"] = None
    store.update_fallbacks(payload)
    saved = _saved(store)["fallbacks"][1]
    assert "reasoning_effort" not in saved["options"]
    assert "base_url" not in saved
    assert saved["options"]["handwritten_knob"] == "keep-me"


def test_reordering_carries_the_secret_along(store: ConfigStore) -> None:
    """靠 _origin 认原条目，所以换了位置密钥也跟着走。"""
    payload = list(reversed(_echo(store.get_providers_public())))
    store.update_fallbacks(payload)
    saved = _saved(store)["fallbacks"]
    assert saved[0]["model"] == "backup"
    assert saved[0]["api_key"] == "sk-inline-secret"
    assert saved[1]["provider"] == "deepseek"


def test_a_new_entry_starts_clean(store: ConfigStore) -> None:
    payload = _echo(store.get_providers_public())
    payload.append({"provider": "deepseek", "model": "fresh"})
    store.update_fallbacks(payload)
    added = _saved(store)["fallbacks"][2]
    assert added == {"provider": "deepseek", "model": "fresh"}


def test_deleting_an_entry_drops_only_that_one(store: ConfigStore) -> None:
    payload = _echo(store.get_providers_public())
    store.update_fallbacks([payload[1]])
    saved = _saved(store)["fallbacks"]
    assert len(saved) == 1
    assert saved[0]["api_key"] == "sk-inline-secret"


def test_the_transport_only_key_never_lands_in_yaml(store: ConfigStore) -> None:
    store.update_fallbacks(_echo(store.get_providers_public()))
    text = store.path.read_text(encoding="utf-8")
    assert "_origin" not in text
    for derived in ("model_raw", "base_url_raw", "api_key_present", "api_key_redacted"):
        assert derived not in text


# ---------------------------------------------------------------------------
# 按节点的链
# ---------------------------------------------------------------------------

def test_node_chain_can_be_edited(store: ConfigStore) -> None:
    store.update_node_fallbacks("system.compactor", [{"provider": "deepseek", "model": "cheap"}])
    assert _saved(store)["node_fallbacks"]["system.compactor"][0]["model"] == "cheap"


def test_empty_node_chain_means_disabled_not_absent(store: ConfigStore) -> None:
    store.update_node_fallbacks("system.compactor", [])
    chains = _saved(store)["node_fallbacks"]
    assert chains["system.compactor"] == []
    assert "system.compactor" in chains


def test_deleting_a_node_chain_falls_back_to_the_global_one(store: ConfigStore) -> None:
    store.update_node_fallbacks("qq.vision", None)
    assert "qq.vision" not in _saved(store)["node_fallbacks"]


def test_last_node_chain_removal_drops_the_whole_key(store: ConfigStore) -> None:
    store.update_node_fallbacks("qq.vision", None)
    store.update_node_fallbacks("system.compactor", None)
    assert "node_fallbacks" not in _saved(store)


def test_node_chain_merge_keeps_secrets_too(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprovider: openai\n"
        "openai:\n  model: m\n  api_key: sk-x\n"
        "node_fallbacks:\n  system.compactor:\n"
        "    - provider: openai\n      api_key: sk-node-secret\n      model: cheap\n",
        encoding="utf-8",
    )
    store = ConfigStore(path=path)
    public = store.get_providers_public()["node_fallbacks"]["system.compactor"]
    assert "sk-node-secret" not in str(public)

    store.update_node_fallbacks(
        "system.compactor", [{**public[0], "_origin": 0, "model": "cheaper"}],
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["node_fallbacks"]["system.compactor"][0]
    assert saved["model"] == "cheaper"
    assert saved["api_key"] == "sk-node-secret"


def test_empty_node_id_is_rejected(store: ConfigStore) -> None:
    with pytest.raises(ValueError):
        store.update_node_fallbacks("  ", [])
