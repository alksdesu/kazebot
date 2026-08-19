"""QQ 群触发配置回归测试。

默认值一改就全局失效（前缀分隔符曾误用全角逗号导致 / 前缀永久失效），
且 / 参与剥离会让 /draw 命令正则失配、群里贴的绝对路径被静默改写。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_live_config, write_live_config  # noqa: E402

_TRIGGER_ENV = ("ONEBOT_TRIGGER_PREFIXES", "ONEBOT_GROUP_TRIGGER")


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str):
    for key in _TRIGGER_ENV:
        monkeypatch.delenv(key, raising=False)
    return load_live_config(monkeypatch, tmp_path, **env)


def _strip_trigger_prefix(module: Any, text: str) -> str:
    """复刻 __init__.py 的剥离逻辑，避免为此拉起 nonebot 依赖。"""
    value = (text or "").strip()
    for prefix in module.live.trigger_prefixes_strippable:
        if value.startswith(prefix):
            return value[len(prefix):].strip() or value
    return value


def test_default_prefixes_cover_both_slash_widths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    # ／ 少一个，全角斜杠写的命令就只能在私聊里用。
    assert module.live.trigger_prefixes == ("!", "！", "/", "／")


@pytest.mark.parametrize("text", ["/生图 猫", "／生图 猫", "!生图 猫", "！生图 猫"])
def test_default_prefixes_trigger_slash_and_bang(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    module = _load(monkeypatch, tmp_path)

    assert text.strip().startswith(module.live.trigger_prefixes)


def test_plain_text_does_not_trigger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert not "普通聊天".startswith(module.live.trigger_prefixes)


def test_slash_is_not_strippable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert "/" not in module.live.trigger_prefixes_strippable
    assert "／" not in module.live.trigger_prefixes_strippable


def test_slash_command_survives_prefix_strip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert _strip_trigger_prefix(module, "/draw cat") == "/draw cat"


def test_absolute_path_is_not_rewritten(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert _strip_trigger_prefix(module, "/www/wwwroot/Clonoth 看一下") == "/www/wwwroot/Clonoth 看一下"


def test_bang_prefix_is_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert _strip_trigger_prefix(module, "!生图 猫") == "生图 猫"


def test_prefix_list_trims_and_dedupes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path, ONEBOT_TRIGGER_PREFIXES=" ! , ! , # ")

    assert module.live.trigger_prefixes == ("!", "#")


def test_full_width_separator_stays_detectable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path, ONEBOT_TRIGGER_PREFIXES="!,！,/，/")

    assert [p for p in module.live.trigger_prefixes if "，" in p] == ["/，/"]


def test_known_trigger_modes_cover_documented_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert module.live.group_trigger == "mention_only"
    assert module.live.group_trigger_is_known
    assert module.GROUP_TRIGGER_MODES == {
        "mention_only", "at", "at_only",
        "prefix", "prefix_or_mention", "mention_or_prefix",
        "all", "always",
    }


def test_unknown_trigger_mode_is_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path, ONEBOT_GROUP_TRIGGER="whatever")

    assert not module.live.group_trigger_is_known


def test_removed_dead_toggle_stays_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path)

    assert not hasattr(module.live, "image_prefer_same_sender")


# --- 热载：yaml 里的触发配置盖掉 env，且改完立刻生效 ---


def test_yaml_prefixes_override_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path, ONEBOT_TRIGGER_PREFIXES="!")

    write_live_config(module, trigger_prefixes=["#", "/"])

    assert module.live.trigger_prefixes == ("#", "/")
    assert module.live.trigger_prefixes_strippable == ("#",)


def test_yaml_trigger_mode_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load(monkeypatch, tmp_path, ONEBOT_GROUP_TRIGGER="all")

    write_live_config(module, group_trigger="prefix")

    assert module.live.group_trigger == "prefix"
    assert module.live.group_trigger_is_known


def test_derived_strippable_follows_a_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """派生值必须跟着重解析一起重算，否则改前缀后剥离规则会停在旧值上。"""
    module = _load(monkeypatch, tmp_path)
    assert module.live.trigger_prefixes_strippable == ("!", "！")

    write_live_config(module, trigger_prefixes=["@@", "／"])

    assert module.live.trigger_prefixes_strippable == ("@@",)
