"""runtime.yaml 里 shell.tui 一节：TUI 启动时读一次。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

# 配置里写 dark / light 更顺手，Textual 的主题名是 textual-dark / textual-light
THEME_ALIASES = {"dark": "textual-dark", "light": "textual-light"}
DEFAULT_THEME = "textual-dark"
MAX_MESSAGE_HEIGHT_LIMIT = 1000


@dataclass(frozen=True)
class TuiSettings:
    theme: str = DEFAULT_THEME
    show_thinking: bool = True
    auto_scroll: bool = True
    node_panel: bool = False
    # 0 表示不限高
    max_message_height: int = 50

    @property
    def caps_message_height(self) -> bool:
        return self.max_message_height > 0

    @classmethod
    def from_runtime_config(cls, runtime_cfg: dict[str, Any]) -> TuiSettings:
        raw_theme = get_str(runtime_cfg, "shell.tui.theme", "").strip()
        return cls(
            theme=THEME_ALIASES.get(raw_theme.lower(), raw_theme) or DEFAULT_THEME,
            show_thinking=get_bool(runtime_cfg, "shell.tui.show_thinking", cls.show_thinking),
            auto_scroll=get_bool(runtime_cfg, "shell.tui.auto_scroll", cls.auto_scroll),
            node_panel=get_bool(runtime_cfg, "shell.tui.node_panel", cls.node_panel),
            max_message_height=get_int(
                runtime_cfg,
                "shell.tui.max_message_height",
                cls.max_message_height,
                min_value=0,
                max_value=MAX_MESSAGE_HEIGHT_LIMIT,
            ),
        )


def load_tui_settings(workspace_root: Path) -> TuiSettings:
    return TuiSettings.from_runtime_config(load_runtime_config(workspace_root))
