"""continue_from：核对委派时带的句柄，找出能接着做的那个子 agent 会话。

句柄要指向调用方会话家族里一个还留着的子会话；目标节点、同步还是异步都得和当初一致，且此刻没有任务在用它。
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from engine.child_session_retention import retention_hold
from engine.dispatch_handle import DispatchHandle, parse_handle

from . import child_retention
from .types import Task

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

ENTRY_BRANCH_NODE = "__entry_branch__"

_REFUSALS = {
    "invalid_handle": "continue_from 的句柄格式不对：要照抄委派结果里给的句柄（task_id@session_id）。",
    "caller_not_found": "找不到发起这次委派的任务，没法核对 continue_from 的句柄。",
    "session_not_found": "continue_from 指向的子会话已经不在了（保留期过了或已被清理），没法接着做；需要的话重新委派。",
    "not_continuable": "continue_from 指向的不是能接着做的子 agent 会话；需要的话重新委派。",
    "not_retained": "continue_from 指向的子 agent 已经正常做完，它的会话不为接着做保留；需要的话重新委派。",
    "handle_mismatch": "continue_from 的句柄对不上：里面的任务不是在这个子会话里跑的，要照抄委派结果里给的句柄。",
    "not_in_family": "continue_from 指向的子会话不属于当前这段对话，不能接着做。",
    "target_mismatch": "continue_from 指向的子 agent 是 {node}，要接着它做就得委派给同一个节点。",
    "busy": "continue_from 指向的子 agent 此刻还有任务在跑，等它结束后再接着派。",
    "mode_wait_required": "这个子 agent 当初是同步委派（wait=true）的，接着做也要带 wait=true。",
    "mode_async_required": "这个子 agent 当初是异步委派的，接着做也要异步派（去掉 wait=true）。",
}


def refusal(reason: str, **details: Any) -> dict[str, Any]:
    message = _REFUSALS.get(reason, _REFUSALS["not_continuable"])
    if details:
        message = message.format(**details)
    return {"ok": False, "reason": reason, "message": message}


def target_ids(workspace_root: Any, target: str) -> frozenset[str]:
    """目标节点认的全部 id：节点改过名时，旧 id 留下的会话也算同一个节点。要读节点文件，放在锁外。"""
    from engine.node import known_node_ids

    name = str(target or "").strip()
    try:
        known = known_node_ids(workspace_root, name)
    except Exception:
        log.warning("reading the ids of node %s failed", name, exc_info=True)
        known = ()
    return frozenset(item for item in (name, *known) if item)


def _task_storage_id(state: "SupervisorState", task: Task) -> str:
    storage = child_retention.storage_locked(state, task)
    return storage.session_id if storage is not None else ""


def resolve(
    state: "SupervisorState", caller_task_id: str, raw_handle: str, target: str, *, wait: bool,
) -> dict[str, Any]:
    """核对 continue_from。能接着做时给出子会话的信息（ok 为真），不能时给出原因。"""
    handle = parse_handle(raw_handle)
    if handle is None:
        return refusal("invalid_handle")
    targets = target_ids(state.workspace_root, target)
    with state._lock:
        return resolve_locked(state, caller_task_id, handle, targets, wait=wait)


def resolve_locked(
    state: "SupervisorState",
    caller_task_id: str,
    handle: DispatchHandle,
    targets: Collection[str],
    *,
    wait: bool,
    require_live_caller: bool = True,
) -> dict[str, Any]:
    """require_live_caller 为假用于路由委派时复核：那时调用方这一程的完成刚受理，还没挂起。"""
    caller = state.tasks.get(str(caller_task_id or "").strip())
    if caller is None or (require_live_caller and state._task_terminal(caller)):
        return refusal("caller_not_found")
    session_id = handle.session_id
    entry = state._session_store.get_entry(session_id)
    if entry is None or entry.get("reset"):
        return refusal("session_not_found")
    hold = retention_hold(entry) or {}
    if entry.get("is_child"):
        if str(entry.get("node_id") or "") == ENTRY_BRANCH_NODE or session_id.startswith("branch_"):
            return refusal("not_continuable")
        kind = child_retention.KIND_CHILD
    else:
        info = state.sessions.get(session_id)
        if info is None or not str(info.conversation_key or "").startswith(child_retention.AGENT_KEY_PREFIX):
            return refusal("not_continuable")
        kind = child_retention.KIND_AGENT

    produced = state.tasks.get(handle.task_id)
    if produced is not None and _task_storage_id(state, produced) != session_id:
        return refusal("handle_mismatch")

    caller_root = state._task_family_root_locked(caller)
    if produced is not None:
        session_root = state._task_family_root_locked(produced)
    elif hold.get("family_root"):
        session_root = str(hold.get("family_root") or "")
    elif kind == child_retention.KIND_CHILD:
        session_root = state._session_family_root_locked(session_id)
    else:
        session_root = ""
    if not caller_root or caller_root != session_root:
        return refusal("not_in_family")

    node_id = str(
        hold.get("node_id")
        or (entry.get("node_id") if kind == child_retention.KIND_CHILD else "")
        or (produced.node_id if produced is not None else "")
        or ""
    ).strip()
    if node_id not in targets:
        return refusal("target_mismatch", node=node_id or "另一个节点")
    if kind == child_retention.KIND_CHILD and not wait:
        return refusal("mode_wait_required")
    if kind == child_retention.KIND_AGENT and wait:
        return refusal("mode_async_required")
    if child_retention.session_busy_locked(state, session_id):
        return refusal("busy")

    context_mode = str(
        (entry.get("context_mode") if kind == child_retention.KIND_CHILD else "")
        or hold.get("context_mode")
        or _produced_mode(produced)
        or "fresh"
    ).strip()
    # 正常做完的 fresh / fork 子会话收尾时就会删，接上去的子任务可能写进一个正在被删的会话
    if not hold and context_mode != "accumulate":
        return refusal("not_retained")
    found: dict[str, Any] = {
        "ok": True,
        "session_id": session_id,
        "task_id": handle.task_id,
        "kind": kind,
        "node_id": node_id,
        "context_mode": context_mode,
    }
    if kind == child_retention.KIND_AGENT:
        found["conversation_key"] = state.sessions[session_id].conversation_key
    return found


def _produced_mode(task: Task | None) -> str:
    if task is None:
        return ""
    origin = task.input.get("_dispatch_origin")
    return str(
        task.input.get("context_mode")
        or (origin.get("context_mode") if isinstance(origin, dict) else "")
        or task.input.get("dispatch_context_mode")
        or ""
    ).strip()
