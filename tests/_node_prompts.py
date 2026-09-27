"""按引擎的方式拼出节点的系统提示词：规则大多写在 include 的片段里，只读节点文件断言会落空。"""
from __future__ import annotations

from pathlib import Path

from engine.node import load_node
from engine.prompt import assemble_prompt

ROOT = Path(__file__).resolve().parents[1]


def assembled_prompt(node_id: str, workspace_root: Path = ROOT) -> str:
    """模型看到的系统提示词全文，静态段和动态段按顺序拼在一起。"""
    node = load_node(workspace_root, node_id)
    assert node is not None, f"node not found: {node_id}"
    return "\n\n".join(
        message["content"] for message in assemble_prompt(workspace_root, node)
        if isinstance(message.get("content"), str)
    )
