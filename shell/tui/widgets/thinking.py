"""Thinking 流式指示器。"""
from __future__ import annotations

from textual.widgets import Static


class ThinkingIndicator(Static):
    """显示 LLM thinking 内容，灰色斜体，流结束时隐藏。"""

    DEFAULT_CSS = """
    ThinkingIndicator {
        height: auto;
        max-height: 3;
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
        display: none;
    }
    ThinkingIndicator.visible {
        display: block;
    }
    """

    MAX_CHARS = 200

    def __init__(self, **kwargs) -> None:
        super().__init__(id="thinking", **kwargs)
        self._text = ""

    def show_thinking(self, content: str) -> None:
        """追加 thinking 文字并显示。"""
        self._text += content
        display = self._text[:self.MAX_CHARS]
        if len(self._text) > self.MAX_CHARS:
            display += "..."
        self.update(f"💭 {display}")
        self.add_class("visible")

    def hide(self) -> None:
        """隐藏并清空。"""
        self._text = ""
        self.update("")
        self.remove_class("visible")
