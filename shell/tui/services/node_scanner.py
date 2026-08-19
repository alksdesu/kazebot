"""扫描 engine/system_nodes/ 和 config/nodes/ 构建节点图，找出根节点（无上游）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NodeInfo:
    """节点摘要信息。"""
    id: str
    name: str
    description: str
    node_type: str  # ai | tool
    delegate_targets: list[str] = field(default_factory=list)


def scan_nodes(workspace_root: Path) -> list[NodeInfo]:
    """扫描 engine/system_nodes/ 和 config/nodes/ 下所有 yaml 文件，返回节点列表。

    系统节点目录分离：engine 内建目录优先，同 id 节点只保留首次出现的。
    """
    # 两个扫描目录：内建系统节点优先于用户配置节点
    dirs = [
        workspace_root / "engine" / "system_nodes",
        workspace_root / "config" / "nodes",
    ]
    result: list[NodeInfo] = []
    try:
        import yaml
    except ImportError:
        return []

    seen_ids: set[str] = set()
    for nodes_dir in dirs:
        if not nodes_dir.is_dir():
            continue
        for f in sorted(nodes_dir.iterdir()):
            if f.suffix not in (".yaml", ".yml") or f.name.startswith("_"):
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            nid = str(data.get("id") or f.stem).strip()
            if nid in seen_ids:
                continue
            seen_ids.add(nid)
            ntype = str(data.get("type") or "ai").strip().lower()
            if ntype not in ("ai", "tool"):
                continue
            name = str(data.get("name") or nid).strip()
            desc = str(data.get("description") or "").strip()
            dt_raw = data.get("delegate_targets")
            delegates = [
                str(x).strip() for x in (dt_raw or []) if isinstance(x, str) and x.strip()
            ] if isinstance(dt_raw, list) else []
            result.append(NodeInfo(
                id=nid, name=name, description=desc,
                node_type=ntype, delegate_targets=delegates,
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
