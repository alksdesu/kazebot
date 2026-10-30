"""没做完就交回的子 agent 会话（失败、到点收尾、ask 要补充信息、被软打断）：留到发起它的任务结束后再过一段宽限期，期间父节点能用句柄接着派。

保留记录写在 sessions.json 那一行的 retain 字段；还登记着的子会话 engine 的 data_cleanup 一概不删，登记和文件由 supervisor 的定期清理按它收。
accumulate 的 agent: 会话本来就一直留着，那一行另记它属于哪段对话、哪个节点，产出它的任务清出内存后照样接得上。
子任务的结果交回去之前，结果里引用到的超长输出先复制到读结果的那一方的会话名下，子会话删了也读得到。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clonoth_runtime import load_runtime_config
from engine.child_session_retention import (
    RETAIN_FIELD,
    is_held_row,
    retain_grace_hours,
    retention_hold,
    retention_until,
)
from engine.dispatch_handle import format_handle
from engine.run_budget import wrapup_info
from engine.tool_outputs import ToolOutputStore, mentioned_refs

from ._helpers import _now
from .task_traits import is_internal_task
from .types import Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .session_store import RegistrySnapshot
    from .state import SupervisorState

log = logging.getLogger(__name__)

KIND_CHILD = "child"
KIND_AGENT = "agent"
REASON_FAILED = "failed"
REASON_WRAPUP = "wrapup"
REASON_ASK = "ask"
REASON_PREEMPTED = "preempted"
# 子任务这一程交回父节点的结局：都带句柄，没做完的会话按 retention_reason 留着
HANDED_BACK = frozenset({"finish", "ask", "fail", "preempted"})
_UNFINISHED_REASONS = {"fail": REASON_FAILED, "ask": REASON_ASK, "preempted": REASON_PREEMPTED}
AGENT_KEY_PREFIX = "agent:"
ACCUMULATE = "accumulate"
# accumulate 的 agent: 会话属于哪段对话、哪个节点：产出它的任务清出内存后，continue_from 靠它核对
LINEAGE_FIELD = "dispatch_lineage"
# accumulate 的 agent: 会话本来就一直留着；用后即删的只有这两种
_DISPOSABLE_AGENT_MODES = frozenset({"fresh", "fork"})


@dataclass(frozen=True)
class ChildStorage:
    """委派出去的节点任务把对话写在哪：同步委派是 child_* 子会话，异步委派是 agent: 会话。"""

    kind: str
    session_id: str
    context_mode: str


@dataclass(frozen=True)
class RefAdoption:
    """子会话名下要复制到读结果的那一方会话名下的超长输出。"""

    source_session_id: str
    target_session_id: str
    refs: tuple[str, ...]


def _outcome(task: Task) -> str:
    if task.status == TaskStatus.cancelled:
        return "cancelled"
    return str((task.result or {}).get("action") or "").strip()


def outcome_of(task: Task) -> str:
    """子任务这一程的结局：被取消的以状态为准，其余是 worker 交回的动作（finish、ask、fail、preempted）。"""
    return _outcome(task)


def _inner_result(task: Task) -> dict[str, Any]:
    inner = (task.result or {}).get("result")
    return inner if isinstance(inner, dict) else {}


def wrapup_reason(task: Task) -> str:
    """子任务是到步数（steps）还是运行时长（time）上限收尾的；不是收尾给空串。"""
    if _outcome(task) != "finish":
        return ""
    info = wrapup_info(_inner_result(task))
    return str(info.get("reason") or "") if info else ""


def retention_reason(task: Task) -> str:
    """子任务的会话为什么要为接着做留着：没做完就交回的（失败、ask、被软打断、到点收尾）给原因，做完或取消的给空串。"""
    outcome = _outcome(task)
    reason = _UNFINISHED_REASONS.get(outcome, "")
    if reason:
        return reason
    if outcome == "finish" and wrapup_reason(task):
        return REASON_WRAPUP
    return ""


def parent_task_id(task: Task) -> str:
    """派出这个子任务的任务：同步委派是 caller，异步委派是 dispatch_origin 记的发起任务。"""
    origin = task.input.get("_dispatch_origin")
    return str(
        task.caller_task_id
        or (origin.get("caller_task_id") if isinstance(origin, dict) else "")
        or ""
    ).strip()


def storage_locked(state: "SupervisorState", task: Task) -> ChildStorage | None:
    """委派出去的节点任务把对话写在哪。不是委派出去的节点任务，或是系统内部任务，给 None。"""
    if task.kind != TaskKind.node or not task.node_id or is_internal_task(task):
        return None
    if (task.caller_task_id or "").strip():
        child_sid = str(task.input.get("child_session_id") or "").strip()
        if not child_sid:
            return None
        entry = state._session_store.get_entry(child_sid)
        mode = str((entry or {}).get("context_mode") or task.input.get("context_mode") or "").strip()
        return ChildStorage(KIND_CHILD, child_sid, mode)
    origin = task.input.get("_dispatch_origin")
    if not isinstance(origin, dict) or not origin:
        return None
    agent_sid = str(task.input.get("parent_session_id") or "").strip()
    info = state.sessions.get(agent_sid)
    if info is None or not str(info.conversation_key or "").startswith(AGENT_KEY_PREFIX):
        return None
    mode = str(origin.get("context_mode") or task.input.get("dispatch_context_mode") or "").strip()
    return ChildStorage(KIND_AGENT, agent_sid, mode)


def mark_locked(state: "SupervisorState", task: Task) -> bool:
    """没做完就交回的子任务：给它的会话记一笔保留，返回记了没有。截止时刻等发起任务结束后由清理时补上。

    accumulate 的 agent: 会话不记保留，只记下它的来历。
    """
    storage = storage_locked(state, task)
    if storage is None:
        return False
    entry = state._session_store.get_entry(storage.session_id)
    if entry is None or entry.get("reset"):
        return False
    if storage.kind == KIND_AGENT and storage.context_mode not in _DISPOSABLE_AGENT_MODES:
        _record_lineage_locked(state, task, storage, entry)
        return False
    reason = retention_reason(task)
    if not reason:
        return False
    hold = {
        "task_id": task.task_id,
        "parent_task_id": parent_task_id(task),
        "node_id": str(task.node_id or ""),
        "kind": storage.kind,
        "context_mode": storage.context_mode,
        "reason": reason,
        "since": _now().isoformat(),
        "until": "",
        "family_root": state._task_family_root_locked(task),
    }
    if state._session_store.set_entry_field(storage.session_id, RETAIN_FIELD, hold) is None:
        return False
    log.info(
        "retaining %s session %s of task %s (node=%s, reason=%s)",
        storage.kind, storage.session_id, task.task_id[:12], task.node_id, reason,
    )
    return True


def _record_lineage_locked(
    state: "SupervisorState", task: Task, storage: ChildStorage, entry: dict[str, Any],
) -> None:
    if storage.context_mode != ACCUMULATE:
        return
    lineage = {
        "family_root": state._task_family_root_locked(task),
        "node_id": str(task.node_id or "").strip(),
        "context_mode": ACCUMULATE,
    }
    if not lineage["family_root"] or not lineage["node_id"] or entry.get(LINEAGE_FIELD) == lineage:
        return
    state._session_store.set_entry_field(storage.session_id, LINEAGE_FIELD, lineage)


def lineage_of(entry: dict[str, Any] | None) -> dict[str, str]:
    """accumulate 的 agent: 会话那一行记的来历；没记或写坏了给空表。"""
    raw = (entry or {}).get(LINEAGE_FIELD)
    if not isinstance(raw, dict):
        return {}
    return {key: str(raw.get(key) or "").strip() for key in ("family_root", "node_id", "context_mode")}


def hand_back_locked(state: "SupervisorState", task: Task) -> None:
    """子任务这一程交回父节点之前：没做完的会话记上保留，结果里引用的超长输出复制到父节点名下。"""
    if _outcome(task) not in HANDED_BACK:
        return
    mark_locked(state, task)
    adopt(state.workspace_root, adoption_locked(state, task))


def continue_fields_locked(state: "SupervisorState", task: Task) -> dict[str, Any]:
    """交还给父节点的结果里附带的几项：句柄，到点收尾的原因，以及会话是不是能用 continue_from 接着做。"""
    storage = storage_locked(state, task)
    if storage is None:
        return {}
    handle = format_handle(task.task_id, storage.session_id)
    if not handle:
        return {}
    fields: dict[str, Any] = {"handle": handle}
    reason = wrapup_reason(task)
    if reason:
        fields["wrapup"] = reason
    fields["continuable"] = _continuable_locked(state, task, storage)
    return fields


def _continuable_locked(state: "SupervisorState", task: Task, storage: ChildStorage) -> bool:
    entry = state._session_store.get_entry(storage.session_id)
    if entry is None or entry.get("reset"):
        return False
    # accumulate 的会话不靠保留记录，一直在：同步的跟着这次任务，异步的跟着这段对话
    if storage.context_mode == ACCUMULATE:
        return True
    hold = retention_hold(entry)
    return bool(hold and str(hold.get("task_id") or "") == task.task_id)


def is_held_locked(state: "SupervisorState", session_id: str, *, now: datetime | None = None) -> bool:
    return is_held_row(state._session_store.get_entry(session_id), now=now or _now())


def release_locked(state: "SupervisorState", session_id: str, *, task_id: str = "") -> bool:
    """会话被接着派出去了：清掉保留记录，这一程怎么收尾由新任务自己的结局决定。给了 task_id 时只清那个任务留下的。"""
    hold = retention_hold(state._session_store.get_entry(session_id))
    if hold is None:
        return False
    if task_id and str(hold.get("task_id") or "") != task_id:
        return False
    return state._session_store.set_entry_field(session_id, RETAIN_FIELD, None) is not None


def detach_locked(state: "SupervisorState", session_id: str) -> None:
    """从内存里的上下级索引摘掉这个会话。改挂过上级的子会话不在 child_session_map 里，按映射摘不干净。"""
    state._remove_child_mapping_for_session_locked(session_id)
    for parent in [parent for parent, children in state.parent_children.items() if session_id in children]:
        children = state.parent_children[parent]
        children.discard(session_id)
        if not children:
            state.parent_children.pop(parent, None)


def reparent_locked(
    state: "SupervisorState", session_id: str, parent_session_id: str, *, flush: bool,
) -> "RegistrySnapshot | None":
    """保留着的子会话改挂到另一个上级：原来那个分支收束了，或被新的调用方接着用。

    sessions.json 里的上级链要一直连到用户会话：会话家族和用量汇总都顺着它往上找。
    上级没变（同一次任务里 continue_from 接着派）时不摘 accumulate 的映射，之后同一个 context_key 还接得上。
    """
    entry = state._session_store.get_entry(session_id)
    if str((entry or {}).get("parent_session_id") or "") != parent_session_id:
        detach_locked(state, session_id)
    state.parent_children.setdefault(parent_session_id, set()).add(session_id)
    return state._session_store.set_entry_field(session_id, "parent_session_id", parent_session_id, flush=flush)


def grace_period(state: "SupervisorState") -> timedelta:
    return timedelta(hours=retain_grace_hours(load_runtime_config(state.workspace_root)))


def session_busy_locked(state: "SupervisorState", session_id: str) -> bool:
    """还有没结束的任务在用这个会话，或在用它下面还没收束的入口分支。"""
    if state._session_files_in_use_locked(session_id):
        return True
    return any(
        state._session_files_in_use_locked(branch)
        for branch in state._entry_branch_ids_for_parent_locked(session_id)
    )


def _parent_live_locked(state: "SupervisorState", hold: dict[str, Any]) -> bool:
    parent = state.tasks.get(str(hold.get("parent_task_id") or "").strip())
    return parent is not None and state._task_ending_locked(parent).continues


def sweep_hold_locked(
    state: "SupervisorState", session_id: str, hold: dict[str, Any], *, now: datetime, grace: timedelta,
) -> tuple[bool, bool]:
    """保留期往前走一格：发起任务结束了就从这一刻起算宽限期；过了期且没有任务在用就该删。

    返回 (该删, 保留记录改过)。改过的记录没有落盘，调用方统一写 sessions.json。
    """
    if session_busy_locked(state, session_id):
        return False, False
    until_text = str(hold.get("until") or "").strip()
    until = retention_until(hold) if until_text else None
    if until is None:
        # 截止时刻读不出来也重新起算，不让一行永远留着
        if not until_text and _parent_live_locked(state, hold):
            return False, False
        updated = {**hold, "until": (now + grace).isoformat()}
        state._session_store.set_entry_field(session_id, RETAIN_FIELD, updated, flush=False)
        return False, True
    return until <= now, False


def archive_session(task: Task) -> str:
    """任务的对话和超长输出记在哪个会话名下，和 engine 读 ref 的取法一致：子会话，其次它运行的那个会话。"""
    return str(
        task.input.get("child_session_id") or task.input.get("parent_session_id") or task.session_id or ""
    ).strip()


def result_refs(task: Task) -> tuple[str, ...]:
    """任务交回的结果里引用到的超长输出。"""
    inner = _inner_result(task)
    return tuple(mentioned_refs(
        inner.get("text"), inner.get("summary"), (task.result or {}).get("summary"), (task.result or {}).get("error"),
    ))


def adoption_locked(state: "SupervisorState", task: Task) -> RefAdoption | None:
    """子任务结果里引用到的超长输出该从哪个会话复制到哪个会话；用不着复制给 None。

    同步委派复制到 caller 名下；异步委派复制到真正读它的一方：在等它的 await 的那个任务名下，否则是结果的落点会话。
    """
    if _outcome(task) not in HANDED_BACK or storage_locked(state, task) is None:
        return None
    source = archive_session(task)
    origin = task.input.get("_dispatch_origin")
    if not (task.caller_task_id or "").strip() and isinstance(origin, dict) and origin:
        target = state._dispatch_result_reader_session_locked(task)
    else:
        caller = state.tasks.get(parent_task_id(task))
        target = archive_session(caller) if caller is not None else ""
    if not source or not target or source == target:
        return None
    refs = result_refs(task)
    if not refs:
        return None
    return RefAdoption(source_session_id=source, target_session_id=target, refs=refs)


def adopt(workspace_root: Path | str, adoption: RefAdoption | None) -> int:
    """把引用到的超长输出复制到读结果的那一方会话名下，返回那边现在能读到几份。有磁盘读写，尽量在锁外调用。"""
    if adoption is None:
        return 0
    store = ToolOutputStore(Path(workspace_root))
    adopted = 0
    for ref in adoption.refs:
        try:
            if store.adopt(adoption.source_session_id, adoption.target_session_id, ref):
                adopted += 1
        except OSError:
            log.warning(
                "copying tool output %s from %s to %s failed",
                ref, adoption.source_session_id, adoption.target_session_id, exc_info=True,
            )
    return adopted
