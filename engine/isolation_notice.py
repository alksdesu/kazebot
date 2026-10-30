"""隔离在 git worktree 里的任务看到的说明：supervisor 写进 task_context 的隔离信息，渲染成动态上下文里固定的一段。

接着一段隔离过的对话、这次却不隔离的任务也有一段，提醒它历史里的副本不是这次的工作目录。
"""
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
# task_context 的隔离说明里 mode 取这个：不在副本里，只带着这段对话上一次用过的副本
NOT_ISOLATED_MODE = "none"
# 原来的副本在登记里已经查不到（登记文件坏了、或过了保留期被清掉）
PREVIOUS_MISSING = "missing"
# 合回过的文件带没带进新副本：带进来了、在原仓库里已经提交或撤销了（不用带）、没带成
CARRY_CARRIED = "carried"
CARRY_COMMITTED = "committed"
CARRY_FAILED = "failed"
_SHORT_COMMIT = 12
# 原来的副本现在怎样：登记里的状态，取值和 supervisor 的副本登记一致
_PREVIOUS_STATES = {
    "creating": "还在建",
    "ready": "还有任务在用",
    "finished": "还留着等合回",
    "merged": "已经合回",
    "abandoned": "已经放弃",
    "expired": "过了保留期",
    "removed": "已经清理",
    "failed": "没建成",
    "merging": "正在合回",
    PREVIOUS_MISSING: "在登记里已经查不到",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def task_isolation(task_context: Any) -> dict[str, Any]:
    """task_context 里的隔离说明；不在隔离副本里给空表。继承副本的孙任务、接着用副本的任务也带着它。

    只带着上一次副本提醒的（mode 是 none）不算在副本里。
    """
    raw = task_context.get(ISOLATION_CONTEXT_KEY) if isinstance(task_context, Mapping) else None
    if not isinstance(raw, Mapping) or _text(raw.get("mode")).lower() != ISOLATION_WORKTREE:
        return {}
    return dict(raw)


def nested_isolation_refusal(isolation: Mapping[str, Any]) -> str:
    """已在隔离副本里的任务又要求隔离时的拒绝原因，和 supervisor 当场判失败时的说法一致。"""
    worktree = _text(isolation.get("worktree"))
    where = f"隔离副本 {worktree} " if worktree else "隔离副本"
    return f"没有派出：你已经在{where}里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派。"


def isolation_parts(raw: Any, *, workspace_root: Path | str) -> list[str]:
    """并进动态区的隔离说明，最多一段；不在 worktree 隔离里、也没有上一次副本要提醒时给空列表，提示词和不隔离的任务一字不差。"""
    if not isinstance(raw, Mapping):
        return []
    mode = _text(raw.get("mode")).lower()
    if mode == ISOLATION_WORKTREE:
        lines = _notice_lines(raw, Path(workspace_root))
    elif mode == NOT_ISOLATED_MODE and isinstance(raw.get("previous"), Mapping):
        lines = _reminder_lines(raw["previous"])
    else:
        return []
    if not lines:
        return []
    return ["\n".join([ISOLATION_OPEN, *lines, ISOLATION_CLOSE])]


def _notice_lines(info: Mapping[str, Any], workspace_root: Path) -> list[str]:
    inherited = info.get("inherited") is True
    worktree = _text(info.get("worktree"))
    lines = [_headline(worktree, inherited=inherited)]
    if info.get("reused") is True:
        lines.append("- 这是你上次用的那个副本，之前的改动都还在。")
    # 继承来的说明里带着的是副本主人当初的经历，对孙任务没有意义
    previous_info = None if inherited else info.get("previous")
    previous = _previous_line(previous_info)
    location = _location_line(info, worktree)
    origin_line = _origin_line(info)
    lines += [line for line in (previous, location, origin_line) if line]
    carried = _carry_status(previous_info) == CARRY_CARRIED
    lines += [
        "- 文件工具的相对路径和命令的工作目录都在副本里；不要用绝对路径去改原仓库里的文件，"
        "那样改的不在副本里，也不会算进交回的 diff。",
        (
            "- 副本从基准提交检出：除了上面说的带进来的文件，原仓库里还没提交的改动不在这里，"
            ".venv、node_modules 和被 gitignore 的文件也没有。"
        ) if carried else (
            "- 副本从基准提交检出：原仓库里还没提交的改动不在这里，.venv、node_modules 和被 gitignore 的文件也没有。"
        ),
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


def _carry_status(raw: Any) -> str:
    carry = raw.get("carry") if isinstance(raw, Mapping) else None
    return _text(carry.get("status")) if isinstance(carry, Mapping) else ""


def _previous_line(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    previous = _text(raw.get("worktree"))
    if not previous:
        return ""
    state = _text(raw.get("state"))
    label = _PREVIOUS_STATES.get(state, "用不了了")
    head = f"- 原来的副本 {previous} {label}，这是另建的"
    carry = raw.get("carry") if isinstance(raw.get("carry"), Mapping) else {}
    status = _text(carry.get("status"))
    count = carry.get("count")
    files = f" {count} 个" if isinstance(count, int) and not isinstance(count, bool) and count > 0 else ""
    if status == CARRY_CARRIED:
        return (
            f"{head}：合回只改原仓库的工作区、不提交，所以它合回过的{files}文件按原仓库工作区里现在的内容"
            "带进了基准提交，你在这之上接着做；没合回的改动不在这里。"
        )
    if status == CARRY_COMMITTED:
        return (
            f"{head}，基于原仓库现在的提交：它合回过的{files}文件在原仓库里已经提交（或已撤回），"
            "不用另外带；没合回的改动不在这里。"
        )
    if status == CARRY_FAILED:
        error = _text(carry.get("error"))
        why = f"（{error}）" if error else ""
        return (
            f"{head}，基于原仓库现在的提交：它合回过的{files}文件没能带进来{why}，这些改动在原仓库的工作区里、不在这里；"
            "要接着改它们，先核对这里的内容，需要的话重做，或者请上级先把原仓库里的改动提交再派。"
        )
    if state == "merged":
        return f"{head}，基于原仓库现在的提交：合回只改原仓库的工作区、不提交，它合回的改动不在这里。"
    return f"{head}，基于原仓库现在的提交：旧副本里没合回的改动不在这里。"


def _reminder_lines(previous: Mapping[str, Any]) -> list[str]:
    """这次不隔离、这段对话之前却在隔离副本里做过：说清历史里的副本路径不是这次的工作目录。"""
    worktree = _text(previous.get("worktree"))
    if not worktree:
        return []
    label = _PREVIOUS_STATES.get(_text(previous.get("state")), "现在用不了")
    how = "跟着派你来的任务" if previous.get("inherited") is True else ""
    return [
        "你这次不在隔离副本里：文件工具和命令直接作用于原仓库。",
        f"- 这段对话之前{how}在隔离副本 {worktree} 里做过（那个副本{label}）：历史里的副本路径、"
        "在副本里改过的文件都指那个副本，不是这次的工作目录。",
        "- 那个副本里没合回的改动不在原仓库里，不要凭历史以为它们已经在了；要接着那份改动，"
        "请委派方带 isolation: worktree 再派，不要在原仓库里凭记忆重做。",
    ]


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
