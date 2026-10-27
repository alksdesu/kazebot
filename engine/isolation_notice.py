"""隔离在 git worktree 里的任务看到的说明：supervisor 写进 task_context 的隔离信息，渲染成动态上下文里固定的一段。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from toolbox._common import ORIGIN_ROOT_ENV, ORIGIN_VENV_ENV, origin_venv

from .dispatch_batch import ISOLATION_WORKTREE
from .project_root import ISOLATION_CONTEXT_KEY

ISOLATION_OPEN = "[ISOLATION]"
ISOLATION_CLOSE = "[/ISOLATION]"
_SHORT_COMMIT = 12
# 原来的副本为什么用不了：登记里的状态，取值和 supervisor 的副本登记一致
_PREVIOUS_STATES = {
    "merged": "已经合回",
    "abandoned": "已经放弃",
    "expired": "过了保留期",
    "removed": "已经清理",
    "failed": "没建成",
    "merging": "正在合回",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def task_isolation(task_context: Any) -> dict[str, Any]:
    """task_context 里的隔离说明；不在隔离副本里给空表。继承副本的孙任务、接着用副本的任务也带着它。"""
    raw = task_context.get(ISOLATION_CONTEXT_KEY) if isinstance(task_context, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) and raw else {}


def nested_isolation_refusal(isolation: Mapping[str, Any]) -> str:
    """已在隔离副本里的任务又要求隔离时的拒绝原因，和 supervisor 当场判失败时的说法一致。"""
    worktree = _text(isolation.get("worktree"))
    where = f"隔离副本 {worktree} " if worktree else "隔离副本"
    return f"没有派出：你已经在{where}里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派。"


def isolation_parts(raw: Any, *, workspace_root: Path | str) -> list[str]:
    """并进动态区的隔离说明，最多一段；不在 worktree 隔离里时给空列表，提示词和不隔离的任务一字不差。"""
    if not isinstance(raw, Mapping) or _text(raw.get("mode")).lower() != ISOLATION_WORKTREE:
        return []
    return ["\n".join([ISOLATION_OPEN, *_notice_lines(raw, Path(workspace_root)), ISOLATION_CLOSE])]


def _notice_lines(info: Mapping[str, Any], workspace_root: Path) -> list[str]:
    inherited = info.get("inherited") is True
    worktree = _text(info.get("worktree"))
    lines = [_headline(worktree, inherited=inherited)]
    if info.get("reused") is True:
        lines.append("- 这是你上次用的那个副本，之前的改动都还在。")
    # 继承来的说明里带着的是副本主人当初的经历，对孙任务没有意义
    previous = "" if inherited else _previous_line(info.get("previous"))
    location = _location_line(info, worktree)
    origin_line = _origin_line(info)
    lines += [line for line in (previous, location, origin_line) if line]
    lines += [
        "- 文件工具的相对路径和命令的工作目录都在副本里；不要用绝对路径去改原仓库里的文件，"
        "那样改的不在副本里，也不会算进交回的 diff。",
        "- 副本从基准提交检出：原仓库里还没提交的改动不在这里，.venv、node_modules 和被 gitignore 的文件也没有。",
        _environment_line(info),
    ]
    raw_links = info.get("links")
    links = [_text(item) for item in raw_links if _text(item)] if isinstance(raw_links, list) else []
    if links:
        lines.append(
            f"- 副本里的 {'、'.join(links)} 链到原仓库的同名目录，可以直接用；不要往里装包或改文件，那会改到原仓库。"
        )
    lines += [
        _instance_line(workspace_root),
        _finish_line(inherited),
        "- 你再派出的子任务也在这个副本里做，不能再要求隔离。",
    ]
    return lines


def _headline(worktree: str, *, inherited: bool) -> str:
    if inherited:
        owner = f"（登记号 {worktree}）" if worktree else ""
        return f"你在隔离副本里工作：这个副本属于派你来的任务{owner}，你在里面接着做，改的不是原仓库。"
    return "你在隔离副本里工作：改的是原仓库的一份 git worktree，不是原仓库本身。"


def _previous_line(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    previous = _text(raw.get("worktree"))
    if not previous:
        return ""
    state = _text(raw.get("state"))
    label = _PREVIOUS_STATES.get(state, "用不了了")
    return (
        f"- 原来的副本 {previous} {label}，这是按原仓库现在的提交另建的；"
        "旧副本里的改动，除非已经合回并提交到原仓库，否则不在这里。"
    )


def _location_line(info: Mapping[str, Any], worktree: str) -> str:
    path = _text(info.get("path"))
    root = _text(info.get("worktree_root"))
    branch = _text(info.get("branch"))
    base = _text(info.get("base"))
    facts = [part for part in (
        f"副本根目录 {root}" if root and root != path else "",
        f"登记号 {worktree}" if worktree else "",
        f"分支 {branch}" if branch else "",
        f"基于提交 {base[:_SHORT_COMMIT]}" if base else "",
    ) if part]
    suffix = f"（{'，'.join(facts)}）" if facts else ""
    if path:
        return f"- 副本里的项目根：{path}{suffix}"
    return f"- 副本{suffix}" if suffix else ""


def _origin_line(info: Mapping[str, Any]) -> str:
    repo = _text(info.get("repo"))
    origin = _text(info.get("origin"))
    if not repo:
        return f"- 你的项目根对应原仓库里的 {origin}" if origin else ""
    if origin and origin != repo:
        return f"- 原仓库：{repo}；你的项目根对应原仓库里的 {origin}"
    return f"- 原仓库：{repo}"


def _environment_line(info: Mapping[str, Any]) -> str:
    env = info.get("env") if isinstance(info.get("env"), Mapping) else {}
    root_var = _text(env.get("origin_root")) or ORIGIN_ROOT_ENV
    venv_var = _text(env.get("origin_venv")) or ORIGIN_VENV_ENV
    origin_note = f"命令的环境变量 {root_var} 是原仓库里对应项目根的目录，只读不改。"
    origin = _text(info.get("origin")) or _text(info.get("repo"))
    venv = origin_venv(origin) if origin else None
    if venv is None:
        return f"- 原仓库里没找到虚拟环境，命令的环境变量里没有 {venv_var}，要用 Python 依赖时先确认装在哪；{origin_note}"
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    shown = f"（解释器 {interpreter}）" if interpreter.is_file() else ""
    return f"- Python 用原仓库的虚拟环境 {venv}{shown}，命令的环境变量 {venv_var} 也指向它；{origin_note}"


def _instance_line(workspace_root: Path) -> str:
    try:
        root = workspace_root.resolve()
    except (OSError, RuntimeError):
        root = workspace_root.absolute()
    attachments = root / "data" / "attachments"
    return (
        f"- config/、data/ 在实例根 {root}，不在副本里；交付给用户的文件写到 {attachments} 下，"
        "用绝对路径，不要写进副本。"
    )


def _finish_line(inherited: bool) -> str:
    if inherited:
        return (
            "- 不要切分支、不要 reset、不要 push，也不用自己提交：你的改动留在这个副本里，"
            "由派你来的任务收尾时一起算出 diff 交回。"
        )
    return (
        "- 不要切分支、不要 reset、不要 push，也不用自己提交：结束时会按基准提交算出副本里的全部改动"
        "（含未提交和未跟踪的文件），连同结果交给委派方，审过再合回。"
    )
