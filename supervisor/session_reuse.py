"""沿用会话的委派（accumulate、continue_from）落进会话之前的核对。

会话此刻有任务在用，两个任务同写会搅乱它的历史；会话是发起方委派链上某一级在写的，它还要收这一串交回的结果。两种都不派。
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from engine.dispatch_batch import (
    CONTINUE_FROM_KEY,
    INBOUND_BAD_MODE_STATUS,
    INBOUND_CONFLICT_STATUS,
    context_mode_refusal,
    parse_context_mode,
)

from . import child_retention
from .types import Task

if TYPE_CHECKING:
    from .state import SupervisorState

CONFLICT_LINEAGE = "lineage"
CONFLICT_BUSY = "busy"
# 委派链异常地长或绕成环时停下
_MAX_LINEAGE_HOPS = 64

_ACCUMULATE_REFUSALS = {
    CONFLICT_LINEAGE: (
        "没有派出：{target} 要沿用的会话是派你出来的上级在写的会话，它还要收这一串交回的结果，"
        "派进去两边会写进同一份历史；改用 fresh 或 fork 另开一个会话。"
    ),
    CONFLICT_BUSY: (
        "没有派出：{target} 要沿用的会话此刻还有任务在跑，两个任务同时写会搅乱它的历史；"
        "等那个任务结束后再派，或者改用 fresh、fork，或换一个 context_key。"
    ),
}
_CONTINUE_REFUSALS = {
    CONFLICT_LINEAGE: (
        "continue_from 指向的是派你出来的上级在写的会话，它还要收这一串交回的结果，"
        "接着派进去两边会写进同一份历史；需要的话改用 fresh 或 fork 重新委派。"
    ),
    CONFLICT_BUSY: "continue_from 指向的子 agent 此刻还有任务在跑，等它结束后再接着派。",
}


@dataclass(frozen=True)
class InboundRefusal:
    """拒收一条异步委派的入站：HTTP 状态码和给模型看的原因。"""

    status: int
    message: str


# 入站的一项核对：不收就给出原因，收给 None
InboundCheck = Callable[["SupervisorState", dict[str, Any]], "InboundRefusal | None"]


def lineage_sessions_locked(state: "SupervisorState", task: Task) -> set[str]:
    """task 和派出它的各级上级在写的会话：同步子任务的子会话，以及各自的路由会话（用户会话或 agent: 会话）。

    上一级同步派看 caller，异步派看 dispatch_origin 记的发起任务；发起任务的记录被清理掉时，从它记的发起会话补上那一级。
    """
    sessions: set[str] = set()
    current: Task | None = task
    seen: set[str] = set()
    while current is not None and current.task_id not in seen and len(seen) < _MAX_LINEAGE_HOPS:
        seen.add(current.task_id)
        origin = current.input.get("_dispatch_origin")
        for raw in (
            current.input.get("child_session_id"),
            state._route_session_id_for_task_locked(current),
            origin.get("parent_session_id") if isinstance(origin, dict) else "",
        ):
            sid = str(raw or "").strip()
            if sid:
                sessions.add(sid)
        current = state.tasks.get(child_retention.parent_task_id(current))
    return sessions


def conflict_locked(state: "SupervisorState", session_id: str, lineage: Collection[str]) -> str:
    """要沿用的会话能不能派：在委派链上给 CONFLICT_LINEAGE，此刻有任务在用给 CONFLICT_BUSY，能派给空串。"""
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    if sid in lineage:
        return CONFLICT_LINEAGE
    if child_retention.session_busy_locked(state, sid):
        return CONFLICT_BUSY
    return ""


def refusal_message(conflict: str, *, target: str = "", continuing: bool = False) -> str:
    """不能派时给模型看的原因：continuing 是 continue_from 接着派，否则是 accumulate 派给 target。能派给空串。"""
    if continuing:
        return _CONTINUE_REFUSALS.get(conflict, "")
    template = _ACCUMULATE_REFUSALS.get(conflict, "")
    return template.format(target=target or "目标节点") if template else ""


def child_refusal_locked(
    state: "SupervisorState", caller: Task, parent_session_id: str, target: str, context_key: str,
) -> str:
    """同步委派用 accumulate 时，它会沿用的子会话不能派就给出原因；还没有这个子会话或能派时给空串。"""
    reused = state._reusable_child_session_locked(parent_session_id, target, context_key)
    if not reused:
        return ""
    conflict = conflict_locked(state, reused, lineage_sessions_locked(state, caller))
    return refusal_message(conflict, target=target)


def inbound_refusal_locked(state: "SupervisorState", payload: dict[str, Any]) -> InboundRefusal | None:
    """一条入站能不能收：不是异步委派的一律收；是的话依次过每一项核对，第一项不收的给出原因。"""
    origin = payload.get("dispatch_origin")
    if not isinstance(origin, dict) or not origin:
        return None
    for check in _inbound_checks():
        refused = check(state, payload)
        if refused is not None:
            return refused
    return None


def _inbound_checks() -> tuple[InboundCheck, ...]:
    # 这几个模块在模块层引用本模块，只能在这里取
    from . import async_dispatches, continuation, dispatch_limits

    return (
        _context_mode_refusal,
        async_dispatches.dispatcher_refusal_locked,
        continuation.inbound_refusal_locked,
        dispatch_limits.inbound_refusal_locked,
        _reused_session_refusal_locked,
    )


def _context_mode_refusal(state: "SupervisorState", payload: dict[str, Any]) -> InboundRefusal | None:
    origin = payload.get("dispatch_origin") or {}
    for raw_mode in (payload.get("dispatch_context_mode"), origin.get("context_mode")):
        if parse_context_mode(raw_mode) is None:
            return InboundRefusal(INBOUND_BAD_MODE_STATUS, context_mode_refusal(raw_mode))
    return None


def _reused_session_refusal_locked(state: "SupervisorState", payload: dict[str, Any]) -> InboundRefusal | None:
    """要落进的会话已经在了（accumulate、continue_from）：此刻有任务在用、或是发起方委派链上在写的，不收。"""
    origin = payload.get("dispatch_origin") or {}
    session_id = state.conversation_map.get(str(payload.get("conversation_key") or ""))
    if not session_id or session_id not in state.sessions:
        return None
    caller = state.tasks.get(str(origin.get("caller_task_id") or "").strip())
    lineage = lineage_sessions_locked(state, caller) if caller is not None else set()
    parent = str(origin.get("parent_session_id") or "").strip()
    if parent:
        lineage.add(parent)
    conflict = conflict_locked(state, session_id, lineage)
    if not conflict:
        return None
    return InboundRefusal(INBOUND_CONFLICT_STATUS, refusal_message(
        conflict,
        target=str(payload.get("entry_node_id") or "").strip(),
        continuing=bool(str(origin.get(CONTINUE_FROM_KEY) or "").strip()),
    ))
