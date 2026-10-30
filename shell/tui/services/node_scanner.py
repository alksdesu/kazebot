"""扫描 engine/system_nodes/、config/nodes/ 和仓库默认的 defaults/nodes/，列出能当入口的节点。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from engine.node import entry_node_problem, entry_roots, load_node, node_files


@dataclass
class NodeInfo:
    """节点摘要信息。"""
    id: str
    name: str
    description: str
    node_type: str  # ai | tool
    delegate_targets: list[str] = field(default_factory=list)
    selectable: bool | None = None


def scan_nodes(workspace_root: Path) -> list[NodeInfo]:
    """列出能当入口的节点，和 engine 的 switch_node 枚举同一套规则：系统目录优先，模板、示例和不接对话的不算，委派目标用现在的 id。"""
    result: list[NodeInfo] = []
    seen_ids: set[str] = set()
    for path in node_files(workspace_root):
        node = load_node(workspace_root, path.stem)
        if node is None or node.id in seen_ids:
            continue
        # 不接对话的不列：internal 节点选它当入口只会收到 "no|群友之间在对话" 这种话，系统节点没人委派会个个算成根节点
        if entry_node_problem(node):
            continue
        seen_ids.add(node.id)
        result.append(NodeInfo(
            id=node.id, name=node.name, description=node.description,
            node_type=node.type, delegate_targets=list(node.delegate_targets), selectable=node.selectable,
        ))
    return result


def find_root_nodes(nodes: list[NodeInfo]) -> list[NodeInfo]:
    """入口列表列哪些节点，和 engine 的 switch_node 枚举是同一条规则（entry_roots）。"""
    return entry_roots(nodes)
