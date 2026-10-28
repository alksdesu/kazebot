"""隔离子任务收尾时交回父节点的副本说明：改了哪些文件、diff、补丁在哪、怎么合回或放弃。

supervisor 算出结构化的结果，同步批次、单个委派的恢复消息和异步结果的前缀都按这里写成文字。
"""
from __future__ import annotations

from typing import Any

# 列出的文件最多这么多行，其余的看补丁
MAX_LISTED_FILES = 40
MERGE_TOOL = "merge_worktree"
_STATUS_NAMES = {"A": "新增", "M": "修改", "D": "删除", "T": "改类型"}


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def file_line(item: Any) -> str:
    """一个改动文件写成一行：状态、路径、增删行数；不是字典或没有路径时给空串。"""
    if not isinstance(item, dict):
        return ""
    path = str(item.get("path") or "").strip()
    if not path:
        return ""
    status = str(item.get("status") or "").strip()
    label = _STATUS_NAMES.get(status, status or "改动")
    if item.get("binary"):
        return f"  {status or '?'} {path}（{label}，二进制）"
    added, deleted = _count(item.get("added")), _count(item.get("deleted"))
    if added is None and deleted is None:
        return f"  {status or '?'} {path}（{label}）"
    return f"  {status or '?'} {path}（{label}，+{added or 0} -{deleted or 0}）"


def _handle(view: dict[str, Any]) -> str:
    return str(view.get("handle") or view.get("id") or "").strip()


def _merge_hint(view: dict[str, Any], *, can_merge: bool) -> str:
    handle = _handle(view)
    patch = str(view.get("patch") or "").strip()
    if view.get("merge_allowed") is False:
        why = str(view.get("merge_refusal") or "").strip() or "本实例不能合回这个仓库"
        where = f"补丁在 {patch}" if patch else "没有补丁文件"
        if not can_merge:
            return f"{why}，{where}；这个节点也不能调用 {MERGE_TOOL}，副本到了保留期会被清理。"
        return f"{why}，{where}；不要了就调用 {MERGE_TOOL}，handle 填 {handle}，带上 abandon=true 放弃。"
    if not can_merge:
        # 同一会话家族里有合回工具的上级拿这个句柄就能合回
        return (
            f"这个节点不能调用 {MERGE_TOOL}：审过之后把 handle {handle} 连同你的结论交给上级或用户，由他们合回或放弃；"
            "不要自己把改动手抄进原仓库。副本到了保留期会被清理。"
        )
    return (
        f"审过之后要合回原仓库就调用 {MERGE_TOOL}，handle 填 {handle}；"
        f"不要了就同样调用它并带上 abandon=true 放弃。合回前副本一直留着，到了保留期会被清理。"
    )


_BRIEF_KEYS = ("id", "state", "repo", "branch", "base", "path", "patch", "file_count", "added", "deleted", "error")


def worktree_brief(view: Any) -> dict[str, Any]:
    """存进对话记录 meta 的精简版：不带 diff 正文和文件清单，界面拿它标出这条结果来自隔离副本。"""
    if not isinstance(view, dict) or not str(view.get("id") or "").strip():
        return {}
    return {key: view[key] for key in _BRIEF_KEYS if view.get(key) not in (None, "")}


def worktree_lines(view: Any, *, can_merge: bool = True) -> list[str]:
    """把一份副本结果写成几行；不是字典或没有登记号时什么都不写。

    can_merge=False 是收到结果的节点工具表里没有合回工具：不叫它调用，让它把句柄交给上级或用户。
    """
    if not isinstance(view, dict):
        return []
    worktree_id = str(view.get("id") or "").strip()
    if not worktree_id:
        return []
    repo = str(view.get("repo") or "").strip()
    branch = str(view.get("branch") or "").strip()
    base = str(view.get("base") or "").strip()
    path = str(view.get("path") or "").strip()
    patch = str(view.get("patch") or "").strip()
    head = f"隔离副本 {worktree_id}"
    facts = [part for part in (
        f"原仓库 {repo}" if repo else "",
        f"分支 {branch}" if branch else "",
        f"基于提交 {base[:12]}" if base else "",
    ) if part]
    if facts:
        head += f"（{'，'.join(facts)}）"
    state = str(view.get("state") or "").strip()
    note = str(view.get("note") or "").strip()
    if state == "abandoned":
        lines = [f"{head}：已按放弃处理{f'（{note}）' if note else ''}，会被清理。"]
        if patch:
            lines.append(f"清理前副本里的改动会存成补丁：{patch}（没有改动就不留）")
        return lines
    error = str(view.get("error") or "").strip()
    if error:
        lines = [f"{head}：收尾时没算出改动（{error}）。"]
        if path:
            who = "" if can_merge else "上级或用户"
            lines.append(f"副本还在 {path}；{who}调用 {MERGE_TOOL} 时会重新算一遍。")
        return lines
    files = view.get("files") if isinstance(view.get("files"), list) else []
    total = _count(view.get("file_count"))
    total = len(files) if total is None else total
    lines = [f"{head}，副本目录 {path}。" if path else f"{head}。"]
    if total <= 0:
        lines.append("副本里没有改动，不需要合回。")
        if can_merge:
            lines.append(f"不要了就调用 {MERGE_TOOL}，handle 填 {_handle(view)}，带上 abandon=true 放弃。")
        else:
            lines.append("副本到了保留期会被清理。")
        return lines
    added, deleted = _count(view.get("added")) or 0, _count(view.get("deleted")) or 0
    lines.append(f"相对基准提交改了 {total} 个文件（+{added} -{deleted}，含未提交和未跟踪的文件）：")
    shown = [line for line in (file_line(item) for item in files[:MAX_LISTED_FILES]) if line]
    lines.extend(shown)
    if total > len(shown):
        lines.append(f"  …还有 {total - len(shown)} 个文件没有列出，见完整补丁。")
    diff = str(view.get("diff") or "")
    if diff.strip():
        lines.append("diff：")
        lines.append(diff.rstrip("\n"))
        if view.get("diff_truncated"):
            lines.append(f"（diff 太长，上面只列了开头；完整补丁在 {patch}）" if patch else "（diff 太长，上面只列了开头）")
        elif patch:
            lines.append(f"完整补丁：{patch}")
    elif patch:
        lines.append(f"完整补丁：{patch}")
    lines.append(_merge_hint(view, can_merge=can_merge))
    return lines
