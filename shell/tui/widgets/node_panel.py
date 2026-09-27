"""节点执行面板。"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

_SUMMARY_PREVIEW_CHARS = 40
_FAILED_OUTCOME = "fail"


class NodePanel(Vertical):
    """右侧节点执行面板：节点每启动一次追加一行，完成时就地改成结果。

    方法不叫 on_node_started：Textual 会把 on_<消息名> 当成消息处理器调用。
    """

    DEFAULT_CSS = """
    NodePanel {
        width: 30;
        height: 1fr;
        border-left: solid $primary;
        padding: 0 1;
    }
    .node-entry {
        height: auto;
    }
    .node-running {
        color: $warning;
    }
    .node-completed {
        color: $success;
    }
    .node-failed {
        color: $error;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="node-panel", **kwargs)
        # 不能叫 _nodes：那是 Textual 存子组件的 NodeList，覆盖掉整棵组件树就挂不上东西了
        self._entries: dict[str, Static] = {}

    def mark_started(self, name: str, node_id: str) -> None:
        entry = Static(f"▶ {name}", classes="node-entry node-running")
        self._entries[node_id or name] = entry
        self.mount(entry)

    def mark_completed(self, name: str, node_id: str, outcome: str, summary: str) -> None:
        failed = outcome == _FAILED_OUTCOME
        text = f"{'✗' if failed else '✓'} {name} → {outcome}: {summary[:_SUMMARY_PREVIEW_CHARS]}"
        state_class = "node-failed" if failed else "node-completed"
        key = node_id or name
        entry = self._entries.get(key)
        if entry is None:
            entry = Static(text, classes=f"node-entry {state_class}")
            self._entries[key] = entry
            self.mount(entry)
            return
        entry.update(text)
        entry.remove_class("node-running")
        entry.add_class(state_class)

    def clear_all(self) -> None:
        for entry in self._entries.values():
            entry.remove()
        self._entries.clear()
