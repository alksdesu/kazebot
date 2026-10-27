"""模型在伪工具参数里点名的附件路径，放行前按 read_file 的策略判一遍。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config_layout
from real_paths import real_path
from toolbox._common import request_guard
from toolbox.context import project_root_of

from ..attachments import is_allowed_attachment_path
from .loop_state import _LoopState, build_tool_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefusedAttachment:
    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


def _unique_paths(paths: Any) -> list[str]:
    if not isinstance(paths, list):
        return []
    unique: dict[str, None] = {}
    for raw in paths:
        if isinstance(raw, str) and raw.strip():
            unique.setdefault(raw.strip(), None)
    return list(unique)


@dataclass(frozen=True)
class _Located:
    """点名的路径指到的现成文件：解析到底的位置，和它在所属根里的相对路径（经受信链接的按链接名写）。"""

    resolved: Path
    logical: str


def _locate(root: Path, path: str) -> _Located | None:
    """path 指向 root 里的现成文件时给出它，相对路径按 root 算；否则 None。"""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = real_path(candidate)
    except (OSError, RuntimeError):
        return None
    logical = config_layout.workspace_logical_path(root, resolved)
    return _Located(resolved, logical) if logical is not None and resolved.is_file() else None


def _handoff_path(path: str, located: _Located, base: Path, workspace_root: Path) -> str:
    """下游按工作区根解析相对路径：在别的根里找到的文件交绝对路径，下游才不会读到工作区里同名的另一个。"""
    return path if base == workspace_root else located.resolved.as_posix()


def _collected_paths(ls: _LoopState, workspace_root: Path) -> tuple[set[str], set[Path]]:
    """已收附件的原写法，和它们在工作区里解析到底的现成文件。"""
    exact: set[str] = set()
    files: set[Path] = set()
    for att in ls.collected_attachments:
        if not isinstance(att, dict):
            continue
        att_path = str(att.get("path") or "").strip()
        if not att_path:
            continue
        exact.add(att_path)
        located = _locate(workspace_root, att_path)
        if located is not None:
            files.add(located.resolved)
    return exact, files


async def screen_model_attachment_paths(
    ls: _LoopState,
    pseudo_call: Any,
    paths: Any,
    *,
    data_dir_only: bool = False,
) -> tuple[list[str], list[RefusedAttachment]]:
    """返回 (放行的路径, 被拒的路径)，顺序同入参，重复的只算一次。

    本任务已收到的附件直接放行，其余和模型调 read_file 同等待遇：相对路径按项目根算，
    交给子节点的按工作区根算。策略按解析后的真实位置判，换种写法或经符号链接指过去都绕不开规则。
    """
    pending = _unique_paths(paths)
    if not pending:
        return [], []
    workspace_root = Path(ls.rctx.workspace_root)
    # 交给子节点的只能是 data/ 下的实例数据，下游也按工作区根读；reply / finish 点名的是项目里的文件
    base = workspace_root if data_dir_only else Path(project_root_of(ls.rctx))
    collected_exact, collected_files = _collected_paths(ls, workspace_root)
    tool_name = str(getattr(pseudo_call, "name", "") or "")

    kept: list[str] = []
    refused: list[RefusedAttachment] = []
    ctx = None
    for index, path in enumerate(pending):
        if path in collected_exact:
            kept.append(path)
            continue
        if data_dir_only and not is_allowed_attachment_path(path):
            refused.append(RefusedAttachment(path, "only files under data/ can be handed to a child node"))
            continue
        in_workspace = _locate(workspace_root, path)
        if in_workspace is not None and in_workspace.resolved in collected_files:
            kept.append(path)
            continue
        located = in_workspace if base == workspace_root else _locate(base, path)
        if located is None:
            refused.append(RefusedAttachment(path, "no such file in the workspace"))
            continue
        if ctx is None:
            ctx = build_tool_context(ls, tool_call_id=str(getattr(pseudo_call, "id", "") or ""))
        try:
            _op, err = await request_guard(ctx, "read_file", {
                "path": located.logical,
                "tool_name": tool_name,
                "reason": f"{tool_name} attaching {located.logical}",
            })
        except Exception as exc:
            # 策略查不成就按拒绝处理，宁可少发
            err = {"error": f"policy check failed: {exc}"}
        if err is None:
            kept.append(_handoff_path(path, located, base, workspace_root))
            continue
        refused.append(RefusedAttachment(path, str(err.get("error") or "denied")))
        if err.get("cancelled"):
            # 任务已取消，剩下的不必再去建审批
            refused.extend(RefusedAttachment(rest, "task cancelled") for rest in pending[index + 1:])
            break

    if refused:
        logger.warning(
            "node %s: %s dropped attachment paths %s",
            ls.node.id, tool_name, [item.as_dict() for item in refused],
        )
    return kept, refused
