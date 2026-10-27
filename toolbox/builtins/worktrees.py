"""merge_worktree：把隔离子任务副本里的改动合回原仓库，或者放弃它。改动由 supervisor 套到原仓库上，审批按 write_file 的规则逐个文件判。"""
from __future__ import annotations

import logging
from typing import Any

from clonoth_runtime import SwitchValueError, switch_arg
from engine.worktree_report import MERGE_TOOL, file_line

from .._common import request_guard

log = logging.getLogger(__name__)

_HTTP_TIMEOUT_SEC = 30.0
# 预览和合回都要在 supervisor 里重新算一遍 diff，合回还要 git apply、记改动日志：大仓库要多等一会儿
_MERGE_TIMEOUT_SEC = 600.0
# 审批卡上最多列这么多个文件，结果里多列一些
_CARD_FILES = 30
_RESULT_FILES = 60
_CONFLICT_LINES = 30


class _MergeToolError(Exception):
    pass


def _failure(message: str, **data: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}", **data}}


def _detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])
    return f"supervisor 回了 HTTP {response.status_code}"


async def _call(ctx: Any, path: str, body: dict[str, Any], *, timeout: float = _HTTP_TIMEOUT_SEC) -> dict[str, Any]:
    try:
        response = await ctx.http.request(
            "POST", f"{ctx.supervisor_url.rstrip('/')}{path}", json=body, timeout=timeout,
        )
    except Exception as exc:
        raise _MergeToolError(f"连不上 supervisor：{type(exc).__name__}: {exc}") from None
    if response.status_code >= 400:
        raise _MergeToolError(_detail(response))
    data = response.json()
    if not isinstance(data, dict):
        raise _MergeToolError("supervisor 回的不是 JSON 对象")
    return data


def _file_rows(files: Any, limit: int) -> list[str]:
    items = [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []
    rows = [line for line in (file_line(item) for item in items[:limit]) if line]
    if len(items) > limit:
        rows.append(f"  …还有 {len(items) - limit} 个文件")
    return rows


def _scale(listing: dict[str, Any]) -> str:
    return f"{listing.get('file_count') or 0} 个文件（+{listing.get('added') or 0} -{listing.get('deleted') or 0}）"


def _origin_of(listing: dict[str, Any]) -> str:
    facts = [part for part in (
        f"分支 {listing.get('branch')}" if listing.get("branch") else "",
        f"基于提交 {str(listing.get('base'))[:12]}" if listing.get("base") else "",
    ) if part]
    suffix = f"（{'，'.join(facts)}）" if facts else ""
    return f"隔离副本 {listing.get('worktree')}{suffix}"


def _card_reason(listing: dict[str, Any]) -> str:
    head = f"{MERGE_TOOL}：把{_origin_of(listing)}的改动合回 {listing.get('repo')}，改 {_scale(listing)}："
    return "\n".join([head, *_file_rows(listing.get("files"), _CARD_FILES)])


def _conflict_rows(conflicts: Any) -> list[str]:
    items = [item for item in conflicts if isinstance(item, dict)] if isinstance(conflicts, list) else []
    rows = []
    for item in items[:_CONFLICT_LINES]:
        path = str(item.get("path") or "").strip() or "（认不出是哪个文件）"
        where = f" 第 {item['line']} 行起" if isinstance(item.get("line"), int) else ""
        rows.append(f"  {path}{where}：{item.get('reason') or '套不上'}")
    if len(items) > _CONFLICT_LINES:
        rows.append(f"  …还有 {len(items) - _CONFLICT_LINES} 处")
    return rows


def _refused(reply: dict[str, Any]) -> dict[str, Any]:
    reason = str(reply.get("reason") or "refused")
    message = str(reply.get("message") or "supervisor 没有说明原因")
    data: dict[str, Any] = {"reason": reason}
    if reply.get("patch"):
        data["patch"] = str(reply["patch"])
    if reason == "conflict":
        rows = _conflict_rows(reply.get("conflicts"))
        text = "\n".join([
            message,
            *(["冲突的位置："] + rows if rows else []),
            "冲突要在原仓库里手工处理，或者带上 abandon=true 放弃这份改动。",
        ])
        data["conflicts"] = reply.get("conflicts") or []
        return {"ok": False, "error": message, "data": {"result": f"ERROR: {text}", **data}}
    return _failure(message, **data)


def _merged_text(merged: dict[str, Any]) -> str:
    if not merged.get("merged"):
        return str(merged.get("message") or f"{_origin_of(merged)}里没有改动，不用合回；副本会被清理。")
    lines = [f"已把{_origin_of(merged)}的改动合回 {merged.get('repo')}：{_scale(merged)}。"]
    lines.extend(_file_rows(merged.get("files"), _RESULT_FILES))
    lines.append(f"副本目录和分支 {merged.get('branch') or '?'} 会被清理。")
    undo_task = str(merged.get("undo_task_id") or "").strip()
    if undo_task:
        lines.append(f"要撤销这次合回：调用 undo_edits，task_id 填 {undo_task}。")
    if merged.get("undoable") is False:
        notes = [str(item) for item in merged.get("undo_notes") or [] if str(item or "").strip()]
        detail = f"（{'；'.join(notes)}）" if notes else ""
        lines.append(f"注意：有的文件改之前没能记下原样{detail}，这部分撤销不了。")
    return "\n".join(lines)


async def _abandon(ctx: Any, handle: str) -> dict[str, Any]:
    reply = await _call(ctx, "/v1/worktrees/abandon", {"task_id": ctx.task_id, "handle": handle})
    if not reply.get("ok"):
        return _refused(reply)
    message = str(reply.get("message") or "已放弃")
    return {"ok": True, "data": {
        "result": message,
        "worktree": reply.get("worktree") or "",
        "abandoned": not reply.get("already"),
    }}


async def merge_worktree(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    handle = str(args.get("handle") or "").strip()
    if not handle:
        return _failure("handle 不能为空：填子任务结果里给的句柄")
    if not str(getattr(ctx, "task_id", "") or "").strip():
        return _failure("没有任务身份，不能合回或放弃隔离副本")
    # 写错的 abandon 照默认值读就成了合回，审批卡上是一份本想丢掉的改动
    try:
        abandon = switch_arg(args, "abandon", False)
    except SwitchValueError as exc:
        return _failure(str(exc))
    try:
        if abandon:
            return await _abandon(ctx, handle)
        preview = await _call(
            ctx, "/v1/worktrees/preview", {"task_id": ctx.task_id, "handle": handle}, timeout=_MERGE_TIMEOUT_SEC,
        )
        if not preview.get("ok"):
            return _refused(preview)
        files = [item for item in preview.get("files") or [] if isinstance(item, dict)]
        targets = [str(item.get("target") or "") for item in files]
        if files:
            if not all(targets):
                return _failure("supervisor 给的改动清单里有文件没有目标路径，没有合回")
            _op, err = await request_guard(ctx, "write_file", {
                "path": targets[0],
                "paths": targets,
                "tool_name": MERGE_TOOL,
                "reason": _card_reason(preview),
                "worktree": str(preview.get("worktree") or ""),
                "patch": str(preview.get("patch") or ""),
            })
            if err is not None:
                failed = _failure(str(err.get("error") or "denied"))
                if err.get("cancelled"):
                    failed["cancelled"] = True
                    failed["data"]["cancelled"] = True
                return failed
        merged = await _call(
            ctx, "/v1/worktrees/merge",
            {"task_id": ctx.task_id, "handle": handle, "digest": str(preview.get("digest") or "")},
            timeout=_MERGE_TIMEOUT_SEC,
        )
    except _MergeToolError as exc:
        return _failure(str(exc))
    if not merged.get("ok"):
        return _refused(merged)
    return {"ok": True, "data": {
        "result": _merged_text(merged),
        "worktree": merged.get("worktree") or "",
        "repo": merged.get("repo") or "",
        "merged": merged.get("merged") or [],
        "undo_task_id": merged.get("undo_task_id") or "",
        "undoable": merged.get("undoable", True),
    }}


def register_tools(registry: Any) -> None:
    registry.register_builtin_tool(
        MERGE_TOOL,
        "Merge the changes a sub-agent made in its isolated git worktree (a dispatch with isolation: worktree) back into "
        "the original repository, or discard them with abandon=true. handle is the handle the sub-agent's result gave. "
        "The diff is recomputed against the worktree's base commit, uncommitted and untracked files included; every "
        "changed file goes through the same approval as write_file and the approval card lists them. Files another "
        "unfinished task is editing make it refuse and name that task. If the original repository changed meanwhile "
        "so the diff does not apply, nothing is changed, the conflicting files and lines are reported and the worktree "
        "is kept. A merge is recorded like an edit and can be undone with undo_edits (the result gives the task_id). "
        "After merging or abandoning, the worktree and its branch are removed. A task that itself runs in a worktree "
        "cannot merge.",
        {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "The sub-agent handle from its result (task_id@session), or the worktree id.",
                },
                "abandon": {
                    "type": "boolean",
                    "description": "Discard the worktree's changes instead of merging them. Default false.",
                },
            },
            "required": ["handle"],
        },
        merge_worktree,
    )
