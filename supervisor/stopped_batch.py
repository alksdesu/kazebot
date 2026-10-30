"""挂起等一批子任务（或交回了一批委派、还没派出去）时被停下的任务：把那一批的现状补进它自己的会话，
接着做它的任务读历史时知道那几次派发的下文。

锁内 capture_locked() 记下现状，锁外 write() 写进会话；写完之前它的会话算忙。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.conversation_store import ConversationStore, Message, MessageType

from . import child_retention
from .task_ending import TaskEnding
from .types import Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

STOPPED_BATCH_KEY = "_stopped_batch"
# 路由已经另起线程去写这一批的说明，同一次完成不再起第二个
WRITER_KEY = "_stopped_batch_writer"
MARK_KEY = "stopped_batch_of"
_KEPT_MODE = "accumulate"
_UNFINISHED_ERROR = "它当时还没交回结果，已被一并叫停。"
_UNSENT_ERROR = "这一项还没派出去。"


def waiting_batch_id(task: Task) -> str:
    """挂起等着的那一批；不在等批次的给空串。"""
    if task.status != TaskStatus.suspended:
        return ""
    return str(task.waiting_for_batch_id or "").strip()


def capture_locked(
    state: "SupervisorState",
    task: Task,
    *,
    reason: str,
    now: datetime,
    batch_id: str = "",
    kept_only: bool = False,
) -> bool:
    """记下 task 被停下时那一批的现状，返回记了没有。只管委派出去、有自己会话的节点任务。

    batch_id 为空时看它是不是交回了 dispatch、那一批还没派出去。kept_only 用于取消：会话随取消拆掉的就不记。
    """
    storage = child_retention.storage_locked(state, task)
    if storage is None or (kept_only and storage.context_mode != _KEPT_MODE):
        return False
    session_id = _storage_session(task)
    if not session_id:
        return False
    entries = _batch_entries_locked(state, task, batch_id, now) if batch_id else _unsent_entries(task)
    if not entries:
        return False
    task.input[STOPPED_BATCH_KEY] = {
        "session_id": session_id,
        "reason": str(reason or "").strip(),
        "at": now.isoformat(),
        "unsent": not batch_id,
        "entries": entries,
    }
    return True


def pending_locked(task: Task) -> bool:
    """记下了、还没写进会话。"""
    return isinstance(task.input.get(STOPPED_BATCH_KEY), dict)


def drop_locked(task: Task) -> None:
    """没有谁会来写了（会话随任务拆掉）：不再让它的会话因此算忙。"""
    task.input.pop(STOPPED_BATCH_KEY, None)


def write_soon_locked(state: "SupervisorState", task: Task) -> None:
    """路由已经过了先写再路由的那一步时用：另起线程写，写完之前它的会话照样算忙。"""
    threading.Thread(
        target=write, args=(state, task), daemon=True, name=f"stopped-batch-{task.task_id[:8]}",
    ).start()


def write(state: "SupervisorState", task: Task) -> None:
    """锁外把记下的那一批写进它的会话，没记或写过的直接返回。写不成只记日志，会话不能因此一直算忙。"""
    with state._lock:
        capture = task.input.get(STOPPED_BATCH_KEY)
        if not isinstance(capture, dict):
            return
        session_id = str(capture.get("session_id") or "")
        reason = str(capture.get("reason") or "")
        unsent = capture.get("unsent") is True
        entries = list(capture.get("entries") or [])
        node_id = str(task.node_id or "")
    try:
        _append(Path(state.workspace_root), session_id, task.task_id, node_id, entries, reason, unsent=unsent)
    except Exception:
        log.exception("writing the stopped batch of task %s into %s failed", task.task_id[:12], session_id)
    finally:
        with state._lock:
            task.input.pop(STOPPED_BATCH_KEY, None)


def _storage_session(task: Task) -> str:
    # task_requeue 导入了 task_store，在模块顶部导入会成环
    from .task_requeue import storage_session

    return storage_session(task)


def _batch_entries_locked(
    state: "SupervisorState", task: Task, batch_id: str, now: datetime,
) -> list[dict[str, Any]]:
    siblings = sorted(
        (
            child for child in state.tasks.values()
            if child.caller_task_id == task.task_id and child.batch_id == batch_id
        ),
        key=lambda child: child.batch_index,
    )
    entries: list[dict[str, Any]] = []
    for child in siblings:
        if state._task_ending_locked(child, now) in (TaskEnding.live, TaskEnding.dispatching):
            entries.append(_unfinished_entry(child))
        else:
            entries.append(state._dispatch_result_entry_locked(child))
    return entries


def _unfinished_entry(child: Task) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": child.kind.value, "status": "cancelled", "error": _UNFINISHED_ERROR}
    if child.kind == TaskKind.tool:
        entry["name"] = str(child.tool_name or "")
        return entry
    entry["node_id"] = str(child.node_id or "")
    entry["instruction"] = str(child.input.get("instruction") or "")
    entry["child_task_id"] = child.task_id
    call_id = str(child.input.get("_dispatch_call_id") or "").strip()
    if call_id:
        entry["call_id"] = call_id
    return entry


def _unsent_entries(task: Task) -> list[dict[str, Any]]:
    """交回了 dispatch、那一批还没派出去：按它要派的各项写成没有结果。"""
    result = task.result or {}
    if task.status != TaskStatus.completed or str(result.get("action") or "").strip() != "dispatch":
        return []
    batch = result.get("dispatch_batch")
    if not isinstance(batch, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in batch:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()
        if not target:
            continue
        if str(item.get("kind") or "node").strip() == "tool":
            entries.append({"kind": "tool", "name": target, "status": "cancelled", "error": _UNSENT_ERROR})
            continue
        entry: dict[str, Any] = {
            "kind": "node",
            "node_id": target,
            "instruction": str(item.get("instruction") or ""),
            "status": "cancelled",
            "error": _UNSENT_ERROR,
        }
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            entry["call_id"] = call_id
        entries.append(entry)
    return entries


def _append(
    workspace_root: Path, session_id: str, task_id: str, node_id: str,
    entries: list[dict[str, Any]], reason: str, *, unsent: bool,
) -> None:
    # resume_builder 在 engine.inference 里，模块顶部导入会把整个推理循环拉进 supervisor
    from engine.inference.resume_builder import stopped_batch_messages

    store = ConversationStore(workspace_root / "data" / "conversations")
    if any(
        isinstance(message.meta, dict) and message.meta.get(MARK_KEY) == task_id
        for message in store.load(session_id)
    ):
        return
    created_at = datetime.now(timezone.utc).isoformat()
    messages: list[Message] = []
    for rendered in stopped_batch_messages(entries, reason=reason, unsent=unsent):
        meta = dict(rendered.get("_meta") or {})
        meta[MARK_KEY] = task_id
        messages.append(Message(
            id=str(uuid.uuid4()),
            role=str(rendered.get("role") or "user"),
            content=rendered.get("content") or "",
            message_type=str(meta.get("message_type") or MessageType.USER_INPUT),
            created_at=created_at,
            meta=meta,
            source_node_id=node_id,
            source_task_id=task_id,
        ))
    if messages:
        store.append_batch(session_id, messages)
