"""continue_from：核对委派时带的句柄，找出能接着做的那个子 agent 会话。

句柄要指向调用方会话家族里一个还留着的子会话；目标节点、同步还是异步都得和当初一致，此刻没有任务在用它，也不是调用方上级在写的会话。
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from engine.child_session_retention import retention_hold
from engine.dispatch_batch import CONTINUE_FROM_KEY, INBOUND_BAD_MODE_STATUS, INBOUND_CONFLICT_STATUS
from engine.dispatch_handle import DispatchHandle, parse_handle

from . import child_retention, session_reuse
from .types import Task

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

ENTRY_BRANCH_NODE = "__entry_branch__"
NOT_RETAINED = "not_retained"

_REFUSALS = {
    "invalid_handle": "continue_from 的句柄格式不对：要照抄委派结果里给的句柄（task_id@session_id）。",
    "caller_not_found": "找不到发起这次委派的任务，没法核对 continue_from 的句柄。",
    "session_not_found": "continue_from 指向的子会话已经不在了（保留期过了或已被清理），没法接着做；需要的话重新委派。",
    "not_continuable": "continue_from 指向的不是能接着做的子 agent 会话；需要的话重新委派。",
    "handle_mismatch": "continue_from 的句柄对不上：里面的任务不是在这个子会话里跑的，要照抄委派结果里给的句柄。",
    "not_in_family": "continue_from 指向的子会话不属于当前这段对话，不能接着做。",
    "lineage_unknown": (
        "continue_from 指向的子 agent 会话认不出属于哪段对话：产出这个句柄的任务已经不在记录里，会话也没记下来历。"
        "要接着它的历史，就用 accumulate、同一个 context_key 再异步委派；否则重新委派。"
    ),
    "target_mismatch": "continue_from 指向的子 agent 是 {node}，要接着它做就得委派给同一个节点。",
    "mode_wait_required": "这个子 agent 当初是同步委派（wait=true）的，接着做也要带 wait=true。",
    "mode_async_required": "这个子 agent 当初是异步委派的，接着做也要异步派（去掉 wait=true）。",
    "conversation_mismatch": "continue_from 指向的子会话和这次投递的会话对不上，没有派出；要照抄委派结果里给的句柄。",
}

# fresh / fork 子会话没有保留记录时为什么接不上：按产出这个句柄的那一程的结局说
_NOT_RETAINED_REASONS = {
    "finish": "continue_from 指向的子 agent 已经正常做完，它的会话不为接着做保留；需要的话重新委派。",
    "cancelled": "continue_from 指向的子 agent 已被取消，它的会话不为接着做保留；需要的话重新委派。",
    "handed_on": (
        "continue_from 指向的子 agent 这一程之后又被接着派过，会话现在不为接着做保留；"
        "要接着最近那一程，用它的委派结果里给的句柄，否则重新委派。"
    ),
    "": (
        "continue_from 指向的子 agent 这一程的结局已经查不到（任务记录清掉了），它的会话也没有为接着做留着；"
        "需要的话重新委派。"
    ),
}


def refusal(reason: str, **details: Any) -> dict[str, Any]:
    # busy、lineage 和 accumulate 撞会话是同一件事，原因由 session_reuse 统一给
    message = (
        session_reuse.refusal_message(reason, continuing=True)
        or _REFUSALS.get(reason, _REFUSALS["not_continuable"])
    )
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


def _not_retained(produced: Task | None) -> dict[str, Any]:
    if produced is None:
        outcome = ""
    elif child_retention.retention_reason(produced):
        # 没做完就交回的会话本该留着；没有保留记录，是之后又被接着派、那一程做完了
        outcome = "handed_on"
    else:
        outcome = child_retention.outcome_of(produced)
    message = _NOT_RETAINED_REASONS.get(outcome, _NOT_RETAINED_REASONS[""])
    return {"ok": False, "reason": NOT_RETAINED, "message": message}


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
    # 产出句柄的任务清出内存后，accumulate 的 agent: 会话靠那一行记的来历核对
    lineage = child_retention.lineage_of(entry) if kind == child_retention.KIND_AGENT else {}

    caller_root = state._task_family_root_locked(caller)
    if produced is not None:
        session_root = state._task_family_root_locked(produced)
    elif hold.get("family_root"):
        session_root = str(hold.get("family_root") or "")
    elif lineage.get("family_root"):
        session_root = lineage["family_root"]
    elif kind == child_retention.KIND_CHILD:
        session_root = state._session_family_root_locked(session_id)
    else:
        return refusal("lineage_unknown")
    if not caller_root or caller_root != session_root:
        return refusal("not_in_family")

    node_id = str(
        hold.get("node_id")
        or (entry.get("node_id") if kind == child_retention.KIND_CHILD else "")
        or (produced.node_id if produced is not None else "")
        or lineage.get("node_id")
        or (entry.get("entry_node_id") if kind == child_retention.KIND_AGENT else "")
        or ""
    ).strip()
    if node_id not in targets:
        return refusal("target_mismatch", node=node_id or "另一个节点")
    if kind == child_retention.KIND_CHILD and not wait:
        return refusal("mode_wait_required")
    if kind == child_retention.KIND_AGENT and wait:
        return refusal("mode_async_required")
    conflict = session_reuse.conflict_locked(state, session_id, session_reuse.lineage_sessions_locked(state, caller))
    if conflict:
        return refusal(conflict)

    context_mode = str(
        (entry.get("context_mode") if kind == child_retention.KIND_CHILD else "")
        or hold.get("context_mode")
        or _produced_mode(produced)
        or lineage.get("context_mode")
        or "fresh"
    ).strip()
    # 正常做完的 fresh / fork 子会话收尾时就会删，接上去的子任务可能写进一个正在被删的会话
    if not hold and context_mode != child_retention.ACCUMULATE:
        return _not_retained(produced)
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


def _requested(raw: Any) -> tuple[bool, DispatchHandle | None]:
    """continue_from 写了没有、写的是不是句柄。写了却认不出的不能当成没写，否则就成了一次普通委派。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False, None
    return True, parse_handle(raw)


def batch_item_locked(
    state: "SupervisorState", caller: Task, item: Any, target: str,
) -> tuple[DispatchHandle | None, dict[str, Any]]:
    """同步批次一项带的 continue_from：(句柄, 核对结果)。没带给 (None, {})；认不出或此刻接不上时核对结果的 ok 为假。

    engine 派出前核对过；这期间子会话可能过了保留期或被别的任务接走，按当下的状态再核一遍。
    """
    written, handle = _requested(item.get(CONTINUE_FROM_KEY) if isinstance(item, dict) else None)
    if not written:
        return None, {}
    if handle is None:
        return None, refusal("invalid_handle")
    return handle, resolve_locked(
        state, caller.task_id, handle, target_ids(state.workspace_root, target),
        wait=True, require_live_caller=False,
    )


def inbound_refusal_locked(
    state: "SupervisorState", payload: dict[str, Any],
) -> "session_reuse.InboundRefusal | None":
    """带 continue_from 的异步委派入站时在锁里按句柄重核：engine 核过之后，会话可能过了保留期被清掉，或被别的任务接走。

    接不上就拒收，不能让投递按会话键另建一个空会话。
    """
    origin = payload.get("dispatch_origin")
    if not isinstance(origin, dict):
        return None
    written, handle = _requested(origin.get(CONTINUE_FROM_KEY))
    if not written:
        return None
    if handle is None:
        return session_reuse.InboundRefusal(INBOUND_BAD_MODE_STATUS, refusal("invalid_handle")["message"])
    found = resolve_locked(
        state,
        str(origin.get("caller_task_id") or "").strip(),
        handle,
        target_ids(state.workspace_root, str(payload.get("entry_node_id") or "")),
        wait=False,
    )
    if not found.get("ok"):
        return session_reuse.InboundRefusal(
            INBOUND_CONFLICT_STATUS, str(found.get("message") or _REFUSALS["not_continuable"]),
        )
    conversation_key = str(payload.get("conversation_key") or "").strip()
    if (
        conversation_key != str(found.get("conversation_key") or "")
        or state.conversation_map.get(conversation_key) != found.get("session_id")
    ):
        return session_reuse.InboundRefusal(INBOUND_CONFLICT_STATUS, refusal("conversation_mismatch")["message"])
    return None
