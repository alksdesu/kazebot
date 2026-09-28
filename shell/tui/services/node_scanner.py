"""扫描 engine/system_nodes/、config/nodes/ 和仓库默认的 defaults/nodes/ 构建节点图，找出根节点（无上游）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from engine.node import load_node, node_files


@dataclass
class NodeInfo:
    """节点摘要信息。"""
    id: str
    name: str
    description: str
    node_type: str  # ai | tool
    delegate_targets: list[str] = field(default_factory=list)


def scan_nodes(workspace_root: Path) -> list[NodeInfo]:
    """列出能派发到的节点，和 engine 加载节点同一套规则：系统节点优先，模板和示例不算，委派目标用现在的 id。"""
    result: list[NodeInfo] = []
    seen_ids: set[str] = set()
    for path in node_files(workspace_root):
        node = load_node(workspace_root, path.stem)
        if node is None or node.id in seen_ids:
            continue
        # internal 节点回的是给程序看的判定值，选它当入口只会收到 "no|群友之间在对话" 这种话
        if (node.extra or {}).get("internal"):
            continue
        seen_ids.add(node.id)
        result.append(NodeInfo(
            id=node.id, name=node.name, description=node.description,
            node_type=node.type, delegate_targets=list(node.delegate_targets),
        ))
    return result


def find_root_nodes(nodes: list[NodeInfo]) -> list[NodeInfo]:
    """找出无上游的根节点（不被任何其他节点 delegate_targets 引用）。"""
    all_targets: set[str] = set()
    for n in nodes:
        all_targets.update(n.delegate_targets)
    roots = [n for n in nodes if n.id not in all_targets]
    # 如果所有节点都被引用，返回全部作为备选
    return roots if roots else nodes
