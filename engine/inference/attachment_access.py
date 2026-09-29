"""模型在伪工具参数里点名的附件路径，放行前按 read_file 的策略判一遍。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config_layout
from toolbox._common import request_guard

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


def _workspace_relative(workspace_root: Path, path: str) -> str | None:
    """指向工作区内现成文件时，返回解析后的工作区相对路径（经受信链接的按链接名写）；否则 None。"""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    relative = config_layout.workspace_logical_path(workspace_root, resolved)
    return relative if relative is not None and resolved.is_file() else None


def _collected_paths(ls: _LoopState, workspace_root: Path) -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    resolved: set[str] = set()
    for att in ls.collected_attachments:
        if not isinstance(att, dict):
            continue
        att_path = str(att.get("path") or "").strip()
        if not att_path:
            continue
        exact.add(att_path)
        relative = _workspace_relative(workspace_root, att_path)
        if relative:
            resolved.add(relative)
    return exact, resolved


async def screen_model_attachment_paths(
    ls: _LoopState,
    pseudo_call: Any,
    paths: Any,
    *,
    data_dir_only: bool = False,
) -> tuple[list[str], list[RefusedAttachment]]:
    """返回 (放行的路径, 被拒的路径)，顺序同入参，重复的只算一次。

    本任务已收到的附件直接放行，其余和模型调 read_file 同等待遇。策略按解析后的
    真实位置判，换种写法或经符号链接指过去都绕不开规则。
    """
    pending = _unique_paths(paths)
    if not pending:
        return [], []
    workspace_root = Path(ls.rctx.workspace_root)
    collected_exact, collected_resolved = _collected_paths(ls, workspace_root)
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
        relative = _workspace_relative(workspace_root, path)
        if relative is None:
            refused.append(RefusedAttachment(path, "no such file in the workspace"))
            continue
        if relative in collected_resolved:
            kept.append(path)
            continue
        if ctx is None:
            ctx = build_tool_context(ls, tool_call_id=str(getattr(pseudo_call, "id", "") or ""))
        try:
            _op, err = await request_guard(ctx, "read_file", {
                "path": relative,
                "tool_name": tool_name,
                "reason": f"{tool_name} attaching {relative}",
            })
        except Exception as exc:
            # 策略查不成就按拒绝处理，宁可少发
            err = {"error": f"policy check failed: {exc}"}
        if err is None:
            kept.append(path)
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
