from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import config_layout

# workflow id 直接拼成文件名，只认一段名字，和节点 id 同一套。
_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class WorkflowUnreadable(ValueError):
    """workflow 文件在盘上，却读不出一份能用的 workflow。reason 只有位置和问题，不带文件内容。"""

    def __init__(self, rel_path: str, reason: str) -> None:
        super().__init__(f"{rel_path} 用不了：{reason}")
        self.rel_path = rel_path
        self.reason = reason


@dataclass
class Workflow:
    id: str
    name: str
    entry_node: str
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, str]] = field(default_factory=dict)  # node_id -> {outcome -> target_node_id}


def _read_workflow_file(workspace_root: Path, wid: str) -> dict[str, Any] | None:
    """config/workflows/<id>.yaml 的内容，文件不在是 None；在却读不出一份 workflow 就抛 WorkflowUnreadable。"""
    path = config_layout.config_dir(workspace_root) / "workflows" / f"{wid}.yaml"
    rel_path = config_layout.logical_path(workspace_root, path)
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowUnreadable(rel_path, f"读不了：{config_layout.config_error_text(exc)}") from exc
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        raise WorkflowUnreadable(rel_path, f"YAML 读不出来：{config_layout.yaml_error_text(exc)}") from exc
    if data is None:
        raise WorkflowUnreadable(rel_path, "文件是空的")
    if not isinstance(data, dict):
        raise WorkflowUnreadable(rel_path, "顶层不是 key: value 的映射")
    if str(data.get("kind") or "workflow").strip() != "workflow":
        raise WorkflowUnreadable(rel_path, "kind 写的不是 workflow")
    if not isinstance(data.get("nodes"), dict):
        raise WorkflowUnreadable(rel_path, "nodes 不是 key: value 的映射")
    return data


def load_workflow(workspace_root: Path, workflow_id: str) -> Workflow | None:
    """按 id 读 workflow：id 是空的或文件不在是 None。

    id 不是一段合法的名字抛 ValueError；文件在却用不了抛 WorkflowUnreadable，不当成没有这个 workflow。
    """
    wid = (workflow_id or "").strip()
    if not wid:
        return None
    if not _WORKFLOW_ID_RE.fullmatch(wid):
        raise ValueError("workflow id 只能用字母、数字和 _ . -，以字母或数字开头，最多 64 个字符")
    data = _read_workflow_file(workspace_root, wid)
    if data is None:
        return None
    raw_nodes = data["nodes"]
    edges: dict[str, dict[str, str]] = {}
    handoffs: dict[str, dict[str, str]] = {}
    for key, val in raw_nodes.items():
        if not isinstance(val, dict):
            continue
        nid = str(val.get("node_id") or key).strip()
        on = val.get("on") or val.get(True)  # YAML 1.1: bare `on` is parsed as True
        if isinstance(on, dict):
            edges[nid] = {
                str(k).strip(): str(v).strip()
                for k, v in on.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        ho = val.get("handoffs")
        if isinstance(ho, dict):
            handoffs[nid] = {
                str(k).strip(): str(v).strip()
                for k, v in ho.items()
                if isinstance(k, str) and isinstance(v, str)
            }
    entry = str(data.get("entry_node") or "").strip()
    if not entry:
        entry = next(iter(edges), "")
    return Workflow(
        id=str(data.get("id") or wid).strip(),
        name=str(data.get("name") or wid).strip(),
        entry_node=entry,
        edges=edges,
        handoffs=handoffs,
    )


def next_node(wf: Workflow, node_id: str, outcome: str) -> str:
    """根据 (node_id, outcome) 查 workflow flow edges，返回下一跳。空字符串表示终止。"""
    on = wf.edges.get(node_id, {})
    return on.get(outcome, "") or on.get("default", "")


def handoff_target(wf: Workflow, node_id: str, outcome: str) -> str:
    """根据 (node_id, outcome) 查 workflow handoffs，返回回调目标。空字符串表示无。"""
    ho = wf.handoffs.get(node_id, {})
    return ho.get(outcome, "")


def allowed_outcomes(wf: Workflow, node_id: str) -> list[str]:
    """返回 workflow 中此节点声明的全部 outcome 名称（flow + handoff）。"""
    on = wf.edges.get(node_id, {})
    ho = wf.handoffs.get(node_id, {})
    merged = {**on, **ho}
    return [k for k in merged if k != "default"]
