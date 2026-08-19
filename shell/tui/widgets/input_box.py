"""多行输入框 + 斜杠命令补全。"""
from __future__ import annotations

from textual.widgets import TextArea, OptionList
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual.containers import Vertical
from textual import events

from ..models import SlashCommand, UserSubmit

_SLASH_COMMANDS = {
    "/clear": "清空上下文",
    "/cancel": "取消任务",
    "/restart": "重启程序",
    "/help": "帮助",
    "/auto": "自动审批",
    "/normal": "手动审批",
    "/exit": "退出",
    "/approve": "审批通过 <id>",
    "/deny": "审批拒绝 <id>",
}


class _CompletionList(OptionList):
    DEFAULT_CSS = """
    _CompletionList {
        height: auto;
        max-height: 8;
        border: solid $primary;
        background: $surface;
        display: none;
        layer: overlay;
        dock: bottom;
        margin-bottom: 3;
        margin-left: 1;
        margin-right: 1;
    }
    _CompletionList.visible {
        display: block;
    }
    """


class InputBox(TextArea):
    """Enter 发送，/ 触发补全。"""

    DEFAULT_CSS = """
    InputBox {
        height: auto;
        min-height: 3;
        max-height: 10;
        border: solid $primary;
        margin: 0 1;
    }
    InputBox:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("enter", "submit", "发送", show=False),
        Binding("escape", "close_completion", "关闭补全", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(
            id="input-box",
            language=None,
            **kwargs,
        )
        self._completion: _CompletionList | None = None
        self._history: list[str] = []
        self._history_idx: int = -1
        self._history_max: int = 50

    def on_mount(self) -> None:
        # Textual 8.x 没有 placeholder 参数，用 tooltip 代替提示
        self.tooltip = "输入消息，按 Enter 发送。/ 开头查看命令。"

    def action_submit(self) -> None:
        if self._completion and self._completion.has_class("visible"):
            idx = self._completion.highlighted
            if idx is not None:
                self._accept_completion(idx)
                return

        text = self.text.strip()
        if not text:
            return

        if not self._history or self._history[-1] != text:
            self._history.append(text)
            if len(self._history) > self._history_max:
                self._history.pop(0)
        self._history_idx = -1

        if text.startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            if cmd in _SLASH_COMMANDS:
                self.post_message(SlashCommand(cmd, args))
                self.clear()
                self._hide_completion()
                return

        self.post_message(UserSubmit(text))
        self.clear()
        self._hide_completion()

    def action_close_completion(self) -> None:
        self._hide_completion()

    def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            self.action_submit()
            return

        if event.key == "up":
            if self._completion and self._completion.has_class("visible"):
                self._completion.action_cursor_up()
                event.prevent_default()
                return
            self._history_up()
            event.prevent_default()
            return

        if event.key == "down":
            if self._completion and self._completion.has_class("visible"):
                self._completion.action_cursor_down()
                event.prevent_default()
                return
            self._history_down()
            event.prevent_default()
            return

        if event.key == "tab":
            if self._completion and self._completion.has_class("visible"):
                idx = self._completion.highlighted
                if idx is None:
                    # 没有高亮项时，选中第一项
                    if self._completion.option_count > 0:
                        idx = 0
                    else:
                        idx = None
                if idx is not None:
                    self._accept_completion(idx)
                event.prevent_default()
                return

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and "\n" not in text:
            self._show_completion(text)
        else:
            self._hide_completion()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """点击补全列表中的选项时触发。"""
        cmd = str(event.option.id) if event.option.id else ""
        if cmd:
            self.clear()
            self.insert(cmd + " ")
        self._hide_completion()
        self.focus()

    def _show_completion(self, prefix: str) -> None:
        matches = [
            (cmd, desc)
            for cmd, desc in _SLASH_COMMANDS.items()
            if cmd.startswith(prefix.lower().split()[0] if prefix.split() else prefix.lower())
        ]
        if not matches:
            self._hide_completion()
            return
        if self._completion is None:
            self._completion = _CompletionList()
            self.screen.mount(self._completion)
        self._completion.clear_options()
        for cmd, desc in matches:
            self._completion.add_option(Option(f"{cmd}  {desc}", id=cmd))
        self._completion.add_class("visible")

    def _hide_completion(self) -> None:
        if self._completion:
            self._completion.remove_class("visible")

    def _accept_completion(self, idx: int) -> None:
        if self._completion is None:
            return
        option = self._completion.get_option_at_index(idx)
        cmd = str(option.id) if option.id else ""
        if cmd:
            self.clear()
            self.insert(cmd + " ")
        self._hide_completion()

    def _history_up(self) -> None:
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self.clear()
        self.insert(self._history[self._history_idx])

    def _history_down(self) -> None:
        if self._history_idx == -1:
            return
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self.clear()
            self.insert(self._history[self._history_idx])
        else:
            self._history_idx = -1
            self.clear()
