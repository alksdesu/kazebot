"""System management: request_restart (with git snapshot helpers)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar

from clonoth_runtime import get_int, load_runtime_config

from ..context import ToolContext
from .._common import request_guard, safe_subprocess_env

_T = TypeVar("_T")

# git 在线程池里跑，同时来的两次重启会并发 git add/commit 抢同一个 index.lock。
_git_lock = threading.Lock()
_HEAD_SHOWN_CHARS = 12


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # 字段在顶层也留一份：判取消等场合有调用方只看顶层。
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _run_capture(*, args: list[str], cwd: Path, timeout_sec: float = 10.0) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=safe_subprocess_env(),
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out.strip()
    except Exception as e:
        return 999, str(e)


def _cleanup_old_artifacts(*, workspace_root: Path, current_run_id: str) -> None:
    """artifact 用后即焚：只留当前 run_id 的目录。"""
    artifacts_root = workspace_root / "data" / "artifacts"
    if not artifacts_root.exists():
        return
    for d in artifacts_root.iterdir():
        if d.is_dir() and d.name != current_run_id:
            try:
                shutil.rmtree(d)
            except Exception:
                pass


def _write_text_artifact(*, ctx: ToolContext, filename: str, text: str) -> str:
    _cleanup_old_artifacts(workspace_root=ctx.workspace_root, current_run_id=ctx.run_id)
    artifacts_dir = ctx.workspace_root / "data" / "artifacts" / ctx.run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    p = artifacts_dir / filename
    p.write_text(text or "", encoding="utf-8", errors="ignore")
    return p.relative_to(ctx.workspace_root).as_posix()


def _git_snapshot(ctx: ToolContext) -> dict[str, Any]:
    root = ctx.workspace_root

    runtime_cfg = load_runtime_config(root)
    diff_max_chars = get_int(runtime_cfg, "meta.git.diff_max_chars", 600_000, min_value=10_000, max_value=2_000_000)

    rc, inside = _run_capture(args=["git", "rev-parse", "--is-inside-work-tree"], cwd=root)
    if rc != 0 or inside.strip().lower() != "true":
        return {"git_ok": False, "git_error": "not a git repo"}

    head_rc, head = _run_capture(args=["git", "rev-parse", "HEAD"], cwd=root)
    status_rc, status = _run_capture(args=["git", "status", "--porcelain"], cwd=root)
    stat_rc, diff_stat = _run_capture(args=["git", "diff", "--stat"], cwd=root, timeout_sec=20)

    diff_ref = ""
    if stat_rc == 0 and diff_stat.strip():
        diff_rc, diff_text = _run_capture(args=["git", "diff"], cwd=root, timeout_sec=30)
        if diff_rc == 0:
            if len(diff_text) > diff_max_chars:
                diff_text = diff_text[:diff_max_chars] + "\n...<truncated>"
            diff_ref = _write_text_artifact(
                ctx=ctx,
                filename=f"git_diff_{uuid.uuid4().hex}.diff",
                text=diff_text,
            )

    return {
        "git_ok": True,
        "git_head": head.strip() if head_rc == 0 else "",
        "git_dirty": bool(status.strip()) if status_rc == 0 else None,
        "git_status_porcelain": status,
        "git_diff_stat": diff_stat,
        "git_diff_ref": diff_ref,
    }


def _git_ensure_identity(*, root: Path) -> None:
    rc_name, name = _run_capture(args=["git", "config", "user.name"], cwd=root)
    rc_email, email = _run_capture(args=["git", "config", "user.email"], cwd=root)
    if rc_name == 0 and name.strip() and rc_email == 0 and email.strip():
        return
    _run_capture(args=["git", "config", "user.name", "clonoth"], cwd=root)
    _run_capture(args=["git", "config", "user.email", "clonoth@local"], cwd=root)


def _git_commit_all(*, ctx: ToolContext, message: str) -> dict[str, Any]:
    root = ctx.workspace_root

    snap_before = _git_snapshot(ctx)
    if not snap_before.get("git_ok"):
        return {"ok": False, "error": snap_before.get("git_error")}

    before_head = str(snap_before.get("git_head") or "")

    if not snap_before.get("git_dirty"):
        return {"ok": True, "committed": False, "before_head": before_head, "after_head": before_head}

    _git_ensure_identity(root=root)

    rc_add, out_add = _run_capture(args=["git", "add", "-A"], cwd=root, timeout_sec=30)
    if rc_add != 0:
        return {"ok": False, "error": "git add failed", "output": out_add, "before_head": before_head}

    rc_commit, out_commit = _run_capture(
        args=["git", "commit", "-m", message],
        cwd=root,
        timeout_sec=60,
    )

    snap_after = _git_snapshot(ctx)
    after_head = str(snap_after.get("git_head") or "") if snap_after.get("git_ok") else before_head

    if rc_commit != 0:
        return {
            "ok": False,
            "error": "git commit failed",
            "output": out_commit,
            "before_head": before_head,
            "after_head": after_head,
        }

    return {
        "ok": True,
        "committed": True,
        "output": out_commit,
        "before_head": before_head,
        "after_head": after_head,
    }


def _locked_git(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    with _git_lock:
        return func(*args, **kwargs)


async def _in_git_thread(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    # git 是同步子进程，大仓库一次 diff 能跑几十秒，在事件循环里跑会把同进程的其他任务一起卡住。
    return await asyncio.to_thread(_locked_git, func, *args, **kwargs)


def _checkpoint_note(commit_res: Any) -> str:
    """告诉用户重启前那个提交能拿来做什么：重启流程本身不回退版本，新版本有问题要手动回退到它。"""
    result = commit_res if isinstance(commit_res, dict) else {}
    if not result.get("ok"):
        reason = str(result.get("error") or "原因不明")
        return f"（重启前没能留下 checkpoint 提交：{reason}。新版本有问题时没有现成的提交可退，只能手动排查）"
    head = str(result.get("after_head") or result.get("before_head") or "").strip()[:_HEAD_SHOWN_CHARS]
    commit = f" {head}" if head else ""
    if result.get("committed"):
        return f"（重启前已把工作区改动提交为 checkpoint{commit}；新版本有问题时可以手动回退到这个提交，重启不会自己回退）"
    return (
        f"（工作区没有未提交的改动，重启前的版本就是当前提交{commit}；"
        "新版本有问题时可以手动回退到这个提交，重启不会自己回退）"
    )


async def request_restart(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = str(args.get("target", "engine"))
    reason = str(args.get("reason", ""))

    git_info = await _in_git_thread(_git_snapshot, ctx)

    op, err = await request_guard(
        ctx,
        "restart",
        {
            "target": target,
            "reason": reason,
            "git": git_info,
            "will_git_commit_before_restart": True,
        },
    )
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    approval_id: str | None = op.get("approval_id") if op.get("safety_level") == "approval_required" else None

    commit_res = await _in_git_thread(
        _git_commit_all,
        ctx=ctx,
        message=f"clonoth: checkpoint before restart (handoff={ctx.run_id}, target={target})",
    )

    if target in {"engine", "all"}:
        final_text = f"已请求重启：{target}。"
        if reason.strip():
            final_text += f"\n原因：{reason.strip()}"
        final_text += f"\n{_checkpoint_note(commit_res)}"

        try:
            await ctx.emit_event("intermediate_reply", {"text": final_text})
        except Exception:
            pass

    # 工具可能跑在临时分支上，重启通知要回到用户看得见的那个会话。
    r = await ctx.http.post(
        f"{ctx.supervisor_url}/v1/admin/restart",
        json={"target": target, "reason": reason, "approval_id": approval_id, "session_id": ctx.route_session_id()},
    )
    if r.status_code >= 400:
        return _err(r.text, git=git_info, git_commit=commit_res)
    try:
        outcome = r.json()
    except ValueError:
        outcome = None
    if not isinstance(outcome, dict) or not outcome.get("scheduled"):
        # 200 也可能没排上（上一次重启还没收尾）：照实告诉模型，别让它跟用户说已经在重启了。
        details = outcome if isinstance(outcome, dict) else {}
        refusal = details.get("reason") or "unknown"
        blocker = details.get("in_progress")
        if not isinstance(blocker, dict):
            return _err(f"restart not scheduled: {refusal}", git=git_info, git_commit=commit_res)
        return _err(
            f"restart not scheduled: {refusal} ({blocker.get('target')} restart since {blocker.get('since')})",
            in_progress=blocker,
            git=git_info,
            git_commit=commit_res,
        )

    return _ok(
        f"Restart requested: {target}",
        target=target,
        scheduled=True,
        git=git_info,
        git_commit=commit_res,
    )
