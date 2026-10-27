"""隔离副本的配置：engine.worktree 下各项的读法和默认值。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from clonoth_runtime import get_int, load_runtime_config
from engine.child_session_retention import retain_grace_hours
from engine.project_root import worktrees_root, worktrees_root_problem

MERGE_INSTANCES_KEY = "engine.worktree.merge_instances"
LINK_DIRS_KEY = "engine.worktree.link_dirs"

DEFAULT_INLINE_DIFF_CHARS = 6000
MAX_INLINE_DIFF_CHARS = 200_000
DEFAULT_TOMBSTONE_DAYS = 7


def _dotted(cfg: Any, dotted: str) -> Any:
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _names(raw: Any) -> tuple[str, ...]:
    items = raw if isinstance(raw, list) else []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip() if isinstance(item, (str, int)) and not isinstance(item, bool) else ""
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _link_dirs(raw: Any) -> tuple[str, ...]:
    """只收仓库根下的相对目录：绝对路径、带 .. 的、.git 本身都不要。"""
    out: list[str] = []
    for item in _names(raw):
        text = item.replace("\\", "/").strip("/")
        if not text or ":" in text or item.startswith(("/", "\\")):
            continue
        parts = PurePosixPath(text).parts
        if not parts or any(part in ("", ".", "..") for part in parts) or parts[0] == ".git":
            continue
        clean = PurePosixPath(*parts).as_posix()
        if clean not in out:
            out.append(clean)
    return tuple(out)


@dataclass(frozen=True)
class WorktreeSettings:
    root: Path
    root_problem: str
    retain_grace: timedelta
    merge_instances: tuple[str, ...]
    link_dirs: tuple[str, ...]
    inline_diff_chars: int
    tombstone_days: int

    @classmethod
    def load(cls, workspace_root: Path | str) -> "WorktreeSettings":
        cfg = load_runtime_config(Path(workspace_root))
        root = worktrees_root(workspace_root, cfg)
        hours = get_int(cfg, "engine.worktree.retain_grace_hours", 0, min_value=0)
        if hours <= 0:
            hours = retain_grace_hours(cfg)
        return cls(
            root=root,
            root_problem=worktrees_root_problem(root, workspace_root),
            retain_grace=timedelta(hours=hours),
            merge_instances=_names(_dotted(cfg, MERGE_INSTANCES_KEY)),
            link_dirs=_link_dirs(_dotted(cfg, LINK_DIRS_KEY)),
            inline_diff_chars=get_int(
                cfg, "engine.worktree.inline_diff_chars", DEFAULT_INLINE_DIFF_CHARS,
                min_value=0, max_value=MAX_INLINE_DIFF_CHARS,
            ),
            tombstone_days=get_int(cfg, "engine.worktree.tombstone_days", DEFAULT_TOMBSTONE_DAYS, min_value=1, max_value=365),
        )
