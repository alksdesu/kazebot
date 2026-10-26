"""续跑、重排、子会话保留都要问的两件事：任务是不是系统内部流程，节点有没有声明 auto_continue。"""
from __future__ import annotations

import logging
from pathlib import Path

from clonoth_runtime import parse_bool

from .types import Task

log = logging.getLogger(__name__)

AUTO_CONTINUE_FIELD = "auto_continue"
# engine 中途退出后重排过的任务记一笔：同一个任务只重排一次，再中断就照旧取消
REQUEUE_MARK = "_crash_requeued"


def is_internal_task(task: Task) -> bool:
    """压缩、记忆提取、轮摘要、接话判定、会话重置收尾、执行计划这类内部流程，不是替用户干活的任务。"""
    task_input = task.input if isinstance(task.input, dict) else {}
    context = task_input.get("task_context")
    return bool(
        task_input.get("_system_task")
        or task_input.get("_reply_intent")
        or task_input.get("_compact_async")
        or task_input.get("_session_reset")
        or (isinstance(context, dict) and context.get("is_system_task"))
    )


def node_auto_continues(workspace_root: Path | str, node_id: str) -> bool:
    """节点 yaml 写了 auto_continue: true。读不到节点按没写算。"""
    from engine.node import load_node

    nid = str(node_id or "").strip()
    if not nid:
        return False
    try:
        node = load_node(Path(workspace_root), nid)
    except Exception:
        log.warning("loading node %s to read auto_continue failed", nid, exc_info=True)
        return False
    if node is None:
        return False
    extra = getattr(node, "extra", None) or {}
    return bool(parse_bool(extra.get(AUTO_CONTINUE_FIELD), False))
