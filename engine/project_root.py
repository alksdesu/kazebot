"""任务的项目根：文件工具、命令和项目说明按它找文件，实例数据和配置仍按工作区根找。

项目根由 supervisor 写进任务 input 的 PROJECT_ROOT_KEY；没写就是工作区根。
隔离任务（带 WORKTREE_KEY）的项目根是它的 git worktree，PROJECT_ORIGIN_KEY 是 worktree 对应的原仓库根。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

import config_layout
from clonoth_runtime import get_str, load_runtime_config
from real_paths import real_path

logger = logging.getLogger(__name__)

PROJECT_ROOT_KEY = "_project_root"
PROJECT_ORIGIN_KEY = "_project_origin"
# 隔离副本的登记号（就是要求隔离的那个子任务的 id）；继承同一个副本的后代任务写的也是它
WORKTREE_KEY = "_worktree"
# task_context 里的隔离说明：{mode, worktree, path, origin, repo, branch, base, inherited}，提示词按它告诉模型在副本里
ISOLATION_CONTEXT_KEY = "isolation"
# 异步委派结果回调入站时带的副本结果，前缀里按 engine.worktree_report 写成文字
INBOUND_CHILD_WORKTREE_KEY = "inbound_child_worktree"
WORKTREE_ROOT_CONFIG_KEY = "engine.worktree.root"
# 多开时数据根下放隔离副本的目录；单实例放在工作区旁边、目录名加这个后缀
WORKTREES_DIR = "worktrees"
_SINGLE_INSTANCE_SUFFIX = "-worktrees"


def _directory_or_reason(raw: Any) -> tuple[Path | None, str]:
    """raw 解析到底的目录；不是存在的目录的绝对路径时目录为 None，第二项是原因。"""
    if not isinstance(raw, (str, PurePath)):
        return None, f"not a path string but {type(raw).__name__}"
    try:
        candidate = Path(str(raw).strip())
        if not candidate.is_absolute():
            return None, "not an absolute path"
        resolved = real_path(candidate)
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


def task_worktree(task_input: Mapping[str, Any] | None) -> str:
    """任务所在隔离副本的登记号；不是隔离任务给空串。"""
    raw = task_input.get(WORKTREE_KEY) if isinstance(task_input, Mapping) else None
    return raw.strip() if isinstance(raw, str) else ""


def task_project_origin(task_input: Mapping[str, Any] | None) -> Path | None:
    """隔离任务的 worktree 对应的原仓库根；不是隔离任务、或写的不是存在的目录的绝对路径时给 None。"""
    if not task_worktree(task_input):
        return None
    directory, _reason = _directory_or_reason(task_input.get(PROJECT_ORIGIN_KEY))
    return directory


def _missing_or_reason(raw: Any) -> tuple[Path | None, str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "还没有登记"
    return _directory_or_reason(raw)


def isolation_error(task_input: Mapping[str, Any] | None) -> str:
    """隔离任务的 worktree 或原仓库用不了时的原因；不是隔离任务、或两样都能用时给空串。

    隔离任务不能像普通任务那样退回工作区根：那样就直接在原仓库里改文件、跑命令了。
    """
    worktree = task_worktree(task_input)
    if not worktree:
        return ""
    root, reason = _missing_or_reason(task_input.get(PROJECT_ROOT_KEY))
    if root is None:
        return f"隔离副本 {worktree} 的目录用不了（{reason}），这个任务不能退回原仓库里做"
    origin, reason = _missing_or_reason(task_input.get(PROJECT_ORIGIN_KEY))
    if origin is None:
        return f"隔离副本 {worktree} 对应的原仓库用不了（{reason}），这个任务不能退回原仓库里做"
    return ""


def _resolved(path: Path | str) -> Path:
    candidate = Path(path)
    try:
        return real_path(candidate)
    except (OSError, RuntimeError):
        return candidate.absolute()


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def worktrees_root(workspace_root: Path | str, runtime_cfg: dict[str, Any] | None = None) -> Path:
    """隔离副本放在哪，解析到底。

    配了 engine.worktree.root 就用它（相对路径按工作区根算）；没配时多开放数据根下的 worktrees/，
    单实例放工作区旁边的 <工作区目录名>-worktrees/。
    """
    workspace = _resolved(workspace_root)
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else load_runtime_config(workspace)
    configured = get_str(cfg, WORKTREE_ROOT_CONFIG_KEY, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
    else:
        data = config_layout.data_root()
        if data is not None:
            candidate = data / WORKTREES_DIR
        else:
            candidate = workspace.parent / f"{workspace.name}{_SINGLE_INSTANCE_SUFFIX}"
    return _resolved(candidate)


def worktrees_root_problem(root: Path, workspace_root: Path | str) -> str:
    """隔离副本的根和工作区重叠时的原因：根在工作区里，或者工作区在根里。不重叠给空串。"""
    workspace = _resolved(workspace_root)
    if _inside(root, workspace):
        return f"engine.worktree.root（{root.as_posix()}）在工作区 {workspace.as_posix()} 里面"
    if _inside(workspace, root):
        return f"工作区 {workspace.as_posix()} 在 engine.worktree.root（{root.as_posix()}）里面"
    return ""


def foreign_worktrees_root(workspace_root: Path | str, runtime_cfg: dict[str, Any] | None = None) -> Path | None:
    """判路径用的隔离副本根：它下面不属于本任务的路径一律按外部算。

    和工作区重叠时不用它：否则整个工作区都成了别人的副本。
    """
    root = worktrees_root(workspace_root, runtime_cfg)
    return None if worktrees_root_problem(root, workspace_root) else root
