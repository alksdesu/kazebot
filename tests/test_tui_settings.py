"""shell.tui 一节是 TUI 启动时唯一读的配置来源。

theme 允许写 dark / light 这种顺手的别名，也允许直接写 Textual 内置主题名；认不出的名字
不该在启动时炸，留给 App 提示后回落默认。max_message_height 是 CSS 变量的值，越界会让
整张样式表解析失败，所以在这里就夹住。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shell.tui.settings import (  # noqa: E402
    DEFAULT_THEME,
    MAX_MESSAGE_HEIGHT_LIMIT,
    TuiSettings,
    load_tui_settings,
)


def _cfg(**tui: object) -> dict:
    return {"shell": {"tui": dict(tui)}}


def test_an_empty_config_falls_back_to_the_dataclass_defaults() -> None:
    settings = TuiSettings.from_runtime_config({})

    assert settings == TuiSettings()
    assert settings.theme == DEFAULT_THEME
    assert settings.show_thinking is True
    assert settings.auto_scroll is True
    assert settings.node_panel is False
    assert settings.max_message_height == 50


@pytest.mark.parametrize("raw, expected", [
    ("dark", "textual-dark"),
    ("light", "textual-light"),
    ("Dark", "textual-dark"),
    ("LIGHT", "textual-light"),
    ("  dark  ", "textual-dark"),
    ("nord", "nord"),
    ("gruvbox", "gruvbox"),
    ("", DEFAULT_THEME),
    ("   ", DEFAULT_THEME),
])
def test_theme_aliases_and_passthrough(raw: str, expected: str) -> None:
    assert TuiSettings.from_runtime_config(_cfg(theme=raw)).theme == expected


@pytest.mark.parametrize("raw, expected", [
    (0, 0),
    (1, 1),
    (50, 50),
    (-5, 0),
    (MAX_MESSAGE_HEIGHT_LIMIT, MAX_MESSAGE_HEIGHT_LIMIT),
    (MAX_MESSAGE_HEIGHT_LIMIT + 1, MAX_MESSAGE_HEIGHT_LIMIT),
    (99999, MAX_MESSAGE_HEIGHT_LIMIT),
])
def test_max_message_height_is_clamped(raw: int, expected: int) -> None:
    assert TuiSettings.from_runtime_config(_cfg(max_message_height=raw)).max_message_height == expected


def test_a_nonsense_height_falls_back_to_the_default() -> None:
    assert TuiSettings.from_runtime_config(_cfg(max_message_height="abc")).max_message_height == 50


@pytest.mark.parametrize("height, caps", [(0, False), (1, True), (50, True)])
def test_caps_message_height_tracks_the_configured_height(height: int, caps: bool) -> None:
    assert TuiSettings(max_message_height=height).caps_message_height is caps


@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False), ("yes", True), ("off", False), ("1", True), ("0", False),
])
def test_boolean_switches_accept_yaml_and_string_spellings(raw: object, expected: bool) -> None:
    settings = TuiSettings.from_runtime_config(
        _cfg(show_thinking=raw, auto_scroll=raw, node_panel=raw),
    )

    assert (settings.show_thinking, settings.auto_scroll, settings.node_panel) == (
        expected, expected, expected,
    )


def test_settings_are_immutable() -> None:
    with pytest.raises(Exception):
        TuiSettings().theme = "nord"  # type: ignore[misc]


def test_load_tui_settings_reads_the_workspace_runtime_yaml(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "runtime.yaml").write_text(
        "shell:\n"
        "  tui:\n"
        "    theme: light\n"
        "    show_thinking: false\n"
        "    auto_scroll: false\n"
        "    node_panel: true\n"
        "    max_message_height: 12\n",
        encoding="utf-8",
    )

    settings = load_tui_settings(tmp_path)

    assert settings == TuiSettings(
        theme="textual-light",
        show_thinking=False,
        auto_scroll=False,
        node_panel=True,
        max_message_height=12,
    )


def test_load_tui_settings_on_a_bare_workspace_uses_defaults(tmp_path: Path) -> None:
    assert load_tui_settings(tmp_path) == TuiSettings()


def test_the_shipped_runtime_yaml_matches_the_code_defaults() -> None:
    """配置文件里写的就是代码默认值，改一边不改另一边会让「默认是什么」有两个答案。"""
    cfg = yaml.safe_load((_ROOT / "defaults" / "runtime.yaml").read_text(encoding="utf-8"))

    assert TuiSettings.from_runtime_config(cfg) == TuiSettings()
