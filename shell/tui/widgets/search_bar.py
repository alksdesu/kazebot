"""消息搜索栏。"""
from __future__ import annotations

from textual.widgets import Input
from textual.message import Message


class SearchQuery(Message):
    """搜索请求。"""
    def __init__(self, query: str) -> None:
        super().__init__()
        self.query = query


class SearchClose(Message):
    """关闭搜索栏。"""


class SearchBar(Input):
    """Ctrl+F 搜索栏。"""

    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        dock: top;
        display: none;
        border: solid $accent;
    }
    SearchBar.visible {
        display: block;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            id="search-bar",
            placeholder="搜索... (Enter 下一个, Esc 关闭)",
            **kwargs,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.value.strip():
            self.post_message(SearchQuery(self.value.strip()))

    def _on_key(self, event) -> None:
        if event.key == "escape":
            self.value = ""
            self.remove_class("visible")
            self.post_message(SearchClose())
            event.prevent_default()

    def show(self) -> None:
        self.add_class("visible")
        self.focus()

    def hide(self) -> None:
        self.value = ""
        self.remove_class("visible")
