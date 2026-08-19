"""底部状态栏。"""
from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """底部状态栏。"""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="status-bar", **kwargs)
        self._connected = False
        self._mode = "normal"
        self._session_id = ""
        self._waiting = False
        self._node_name = ""  # 友好名称
        self._model = ""
        self._refresh()

    def update_status(
        self,
        *,
        connected: bool | None = None,
        mode: str | None = None,
        session_id: str | None = None,
        waiting: bool | None = None,
        node: str | None = None,
        node_name: str | None = None,
        model: str | None = None,
    ) -> None:
        if connected is not None:
            self._connected = connected
        if mode is not None:
            self._mode = mode
        if session_id is not None:
            self._session_id = session_id
        if waiting is not None:
            self._waiting = waiting
        if node_name is not None:
            self._node_name = node_name
        elif node is not None:
            # 兼容：如果只传了 node（ID），取最后一段作为显示名
            self._node_name = node.rsplit(".", 1)[-1] if "." in node else node
        if model is not None:
            self._model = model
        self._refresh()

    def _refresh(self) -> None:
        conn = "🟢" if self._connected else "🔴"
        mode_str = "⚡ 自动审批" if self._mode == "auto" else "🔒 手动审批"
        wait = "  ⏳ 等待回复" if self._waiting else ""
        node = f"  📌 {self._node_name}" if self._node_name else ""
        model = f"  🤖 {self._model}" if self._model else ""
        self.update(f" {conn} {mode_str}{node}{model}{wait}")
