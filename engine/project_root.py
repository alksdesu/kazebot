"""任务的项目根：文件工具、命令和项目说明按它找文件，实例数据和配置仍按工作区根找。

项目根由 supervisor 写进任务 input 的 PROJECT_ROOT_KEY；没写就是工作区根。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT_KEY = "_project_root"


def _directory_or_reason(raw: Any) -> tuple[Path | None, str]:
    """raw 解析到底的目录；不是存在的目录的绝对路径时目录为 None，第二项是原因。"""
    if not isinstance(raw, (str, PurePath)):
        return None, f"not a path string but {type(raw).__name__}"
    try:
        candidate = Path(str(raw).strip())
        if not candidate.is_absolute():
            return None, "not an absolute path"
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved, ""
        return None, "not a directory" if resolved.exists() else "no such directory"
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"unusable ({type(exc).__name__}: {exc})"


def task_project_root(task_input: Mapping[str, Any] | None, workspace_root: Path, *, task_id: str = "") -> Path:
    """任务 input 里写的项目根，解析到底。没写（或是空串）就是 workspace_root。

    写了却不是存在的目录的绝对路径时也退回 workspace_root，并记一条 warning。
    """
    raw = task_input.get(PROJECT_ROOT_KEY) if isinstance(task_input, Mapping) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return workspace_root
    directory, reason = _directory_or_reason(raw)
    if directory is not None:
        return directory
    logger.warning(
        "task %s: ignoring %s=%r (%s); file tools and commands use the workspace root %s",
        task_id or "?", PROJECT_ROOT_KEY, raw, reason, workspace_root,
    )
    return workspace_root
