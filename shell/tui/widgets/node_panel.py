"""节点/任务状态面板。"""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static


class NodePanel(Vertical):
    """右侧节点状态面板，动态挂载，不使用 display:none。"""

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
        color: yellow;
    }
    .node-completed {
        color: green;
    }
    .node-failed {
        color: red;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="node-panel", **kwargs)
        self._nodes: dict[str, Static] = {}

    def on_node_started(self, name: str, node_id: str) -> None:
        key = node_id or name
        w = Static(f"▶ {name}", classes="node-entry node-running")
        self._nodes[key] = w
        self.mount(w)

    def on_node_completed(self, name: str, node_id: str, outcome: str, summary: str) -> None:
        key = node_id or name
        short = summary[:40] if summary else ""
        if key in self._nodes:
            w = self._nodes[key]
            w.update(f"✓ {name} → {outcome}: {short}")
            w.remove_class("node-running")
            w.add_class("node-completed")
        else:
            w = Static(f"✓ {name} → {outcome}: {short}", classes="node-entry node-completed")
            self._nodes[key] = w
            self.mount(w)

    def clear_all(self) -> None:
        for w in self._nodes.values():
            w.remove()
        self._nodes.clear()
