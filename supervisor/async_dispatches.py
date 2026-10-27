"""异步委派的结果台账和 await_dispatches 汇合，list_dispatches / cancel_dispatch，以及 wait 委派单项超时的到点处理。

都只在内存里：supervisor 重启后任务本身也不在了。带 _locked 的函数要求调用方持有 state._lock。
"""
from __future__ import annotations

import logging
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from clonoth_runtime import load_runtime_config, task_max_running_seconds
from engine.dispatch_batch import AWAIT_ANY, AWAITING_INPUT_KEY, AwaitRequest, DispatchTimeouts
from engine.dispatch_handle import format_handle, parse_handle
from engine.run_budget import lifetime_limit_seconds, lifetime_origin

from . import child_retention
from ._helpers import _now
from .task_store import CancelEffect
from .types import RouteStatus, Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

# wait 委派单项超时：supervisor 写进子任务 input
ITEM_TIMEOUT_SEC_KEY = "_wait_timeout_sec"
ITEM_DEADLINE_KEY = "_wait_deadline"
ITEM_TIMED_OUT_KEY = "_wait_timed_out"

DELIVERED_INBOUND = "inbound"
DELIVERED_JOIN = "join"
DELIVERED_DROPPED = "dropped"
# 结果真正交到过模型手里的去处；随被取消的任务一起丢掉的不算
_DELIVERED_PLACES = frozenset({DELIVERED_INBOUND, DELIVERED_JOIN})
STATUS_RUNNING = "running"
STATUS_UNAVAILABLE = "unavailable"

LEDGER_LIMIT = 512
LEDGER_TTL = timedelta(hours=24)
LIST_ACTIVE_LIMIT = 50
LIST_SETTLED_LIMIT = 20
_PREVIEW_CHARS = 160
# 等待截止留出这段余量，挂起的任务先带着已有结果恢复，再被总寿命回收
_LIFETIME_MARGIN = timedelta(minutes=10)
# 刚交回 dispatch、路由还没跑完的任务算活着；路由一直没跑完的过了这段时间就不算了
_TRANSIT_GRACE = timedelta(minutes=10)
# 定时器早醒一点就会错过刚好到点的那一项
_TIMER_SLACK_SEC = 0.05

_NOT_FOUND = "找不到这个委派：句柄不对、不属于这段对话，或者它结束得太早、记录已经清掉了（那样的话结果早先已作为单独的消息交回过）。"
_INVALID_HANDLE = "句柄格式不对：要照抄委派结果里给的句柄（task_id@session_id）。"
_SYNC_HANDLE = "这是 wait=true 同步委派的句柄，它的结果已经随那一批结果交回，不用再等。"
_SELF_HANDLE = "不能等自己，也不能等派出自己的那个任务。"
_DUPLICATE_HANDLE = "和前面的句柄是同一个委派。"
_SYSTEM_TASK = "系统任务不能挂起等待异步委派的结果。"
_VANISHED = "这个委派的任务记录已经不在了，拿不到它的结果。"


@dataclass
class SettledDispatch:
    """一个已经结束的异步委派。delivered 是结果第一次交到了哪里；held_for 非空时结果先留给那个任务，还没交出去。"""

    task: Task
    status: str
    family_root: str
    settled_at: datetime
    fallback_result: dict[str, Any] | None = None
    delivered: str = ""
    held_for: str = ""


@dataclass
class Join:
    """一次 await_dispatches：调用方挂起，等 order 里这些句柄。"""

    waiter_id: str
    mode: str
    timeout_sec: int
    started_at: datetime
    deadline: datetime
    call_id: str = ""
    lifetime_capped: bool = False
    order: list[tuple[str, str]] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    settled: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    earlier: dict[str, str] = field(default_factory=dict)

    def satisfied(self) -> bool:
        if not self.pending:
            return True
        return self.mode == AWAIT_ANY and bool(self.settled)


class AsyncDispatchRegistry:
    def __init__(self) -> None:
        self.ledger: OrderedDict[str, SettledDispatch] = OrderedDict()
        self.joins: dict[str, Join] = {}
        # 子任务 id -> 留给哪个任务：await 过它的任务活着时，结果不另开任务
        self.reserved: dict[str, str] = {}
        self.item_deadlines: dict[str, datetime] = {}
        self.timer: threading.Timer | None = None
        self.timer_due: datetime | None = None
        self.timer_token: object | None = None


class DispatchCancelIn(BaseModel):
    handle: str = Field(min_length=1, max_length=320)


def registry_locked(state: "SupervisorState") -> AsyncDispatchRegistry:
    registry = getattr(state, "_async_dispatches", None)
    if not isinstance(registry, AsyncDispatchRegistry):
        registry = AsyncDispatchRegistry()
        state._async_dispatches = registry
    return registry


def _existing_registry(state: "SupervisorState") -> AsyncDispatchRegistry | None:
    registry = getattr(state, "_async_dispatches", None)
    return registry if isinstance(registry, AsyncDispatchRegistry) else None


def is_async_dispatch(task: Task) -> bool:
    """异步委派出去的节点任务：带 dispatch_origin、没有 caller。"""
    origin = task.input.get("_dispatch_origin")
    return (
        task.kind == TaskKind.node
        and isinstance(origin, dict)
        and bool(origin)
        and not (task.caller_task_id or "").strip()
    )


def _origin_caller_id(task: Task) -> str:
    origin = task.input.get("_dispatch_origin")
    return str(origin.get("caller_task_id") or "").strip() if isinstance(origin, dict) else ""


def _storage_session_id(state: "SupervisorState", task: Task) -> str:
    """异步委派的对话所在的 agent: 会话；会话已经拆掉时按任务自己记的那个。"""
    storage = child_retention.storage_locked(state, task)
    if storage is not None:
        return storage.session_id
    return str(task.input.get("parent_session_id") or "").strip()


def handle_of_locked(state: "SupervisorState", task: Task) -> str:
    return format_handle(task.task_id, _storage_session_id(state, task))


def _alive(state: "SupervisorState", task: Task, now: datetime) -> bool:
    if not state._task_terminal(task):
        return True
    action = str((task.result or {}).get("action") or "").strip()
    return (
        task.status == TaskStatus.completed
        and action == "dispatch"
        and task.route_status != RouteStatus.routed
        and now - task.updated_at < _TRANSIT_GRACE
    )


def _preview(text: Any, limit: int = _PREVIEW_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _short_id(task_id: str) -> str:
    return str(task_id or "")[:8]


def _record_locked(
    state: "SupervisorState",
    registry: AsyncDispatchRegistry,
    task: Task,
    *,
    fallback_result: dict[str, Any] | None,
    now: datetime,
) -> SettledDispatch:
    status = state._task_outcome_action(task) or "fail"
    record = registry.ledger.get(task.task_id)
    if record is None:
        record = SettledDispatch(
            task=task,
            status=status,
            family_root=state._task_family_root_locked(task),
            settled_at=now,
            fallback_result=dict(fallback_result) if isinstance(fallback_result, dict) else None,
        )
        registry.ledger[task.task_id] = record
        _trim_ledger_locked(registry, now)
        return record
    record.task = task
    record.status = status
    if isinstance(fallback_result, dict):
        record.fallback_result = dict(fallback_result)
    return record


def _trim_ledger_locked(registry: AsyncDispatchRegistry, now: datetime) -> None:
    """超出条数或放得太久的记录删掉；结果还留着没交出去的、有汇合在等的不删。"""
    waited = {child_id for join in registry.joins.values() for child_id in join.settled | join.pending}
    overflow = len(registry.ledger) - LEDGER_LIMIT
    for child_id, record in list(registry.ledger.items()):
        if record.held_for or child_id in waited:
            continue
        if overflow > 0 or now - record.settled_at > LEDGER_TTL:
            registry.ledger.pop(child_id, None)
            overflow -= 1


def _settled_without_hook_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, task: Task, now: datetime,
) -> SettledDispatch | None:
    """终态、路由也跑完了却没进台账的（会话重置时直接改的终态）：照任务记录补一笔。"""
    if not state._task_terminal(task) or task.route_status != RouteStatus.routed:
        return None
    record = _record_locked(state, registry, task, fallback_result=None, now=now)
    if not record.delivered and task.delivery_id and state.eventlog.find_delivery_event(
        f"{task.delivery_id}:caller-resume", event_type="inbound_message",
    ):
        record.delivered = DELIVERED_INBOUND
    return record


def settle_locked(
    state: "SupervisorState", task: Task, *, fallback_result: dict[str, Any] | None = None,
) -> bool:
    """异步委派到了终点：记进台账；有汇合在等就交给汇合，留给某个还活着的任务就先留着。

    返回 True 表示结果已经有了去处，调用方不要再把它作为新消息交回。
    """
    if not is_async_dispatch(task):
        return False
    registry = registry_locked(state)
    now = _now()
    record = _record_locked(state, registry, task, fallback_result=fallback_result, now=now)
    if record.delivered or record.held_for:
        # 同一次完成的路由重推：第一次已经交出去或留下了
        return True
    waiter_id = registry.reserved.get(task.task_id, "")
    waiter = state.tasks.get(waiter_id) if waiter_id else None
    if waiter is None or not _alive(state, waiter, now):
        registry.reserved.pop(task.task_id, None)
        return False
    join = registry.joins.get(waiter_id)
    if join is not None and task.task_id in join.pending:
        _claim_settled(registry, join, record)
        log.info(
            "async dispatch %s settled into the join of task %s (status=%s)",
            task.task_id[:12], waiter_id[:12], record.status,
        )
        if join.satisfied():
            _resume_join_locked(state, registry, join, timed_out=False)
        return True
    record.held_for = waiter_id
    log.info("async dispatch %s settled while task %s holds it; result kept for it", task.task_id[:12], waiter_id[:12])
    return True


def mark_delivered_locked(state: "SupervisorState", task: Task) -> None:
    """结果已经作为新消息交回发起方会话。"""
    registry = _existing_registry(state)
    record = registry.ledger.get(task.task_id) if registry is not None else None
    if record is not None and not record.delivered:
        record.delivered = DELIVERED_INBOUND


def _deliver_held_locked(state: "SupervisorState", record: SettledDispatch, *, drop: bool = False) -> None:
    """留着的结果没人收了：照没有汇合时那样作为新消息交回。取消、打断的异步委派本来就不交回。

    drop 为真时收它的任务是被取消的，结果不再交回，免得任务停下之后又开出新任务。
    """
    record.held_for = ""
    if drop:
        record.delivered = DELIVERED_DROPPED
        log.info("result of async dispatch %s dropped: the task holding it was cancelled", record.task.task_id[:12])
        return
    if record.fallback_result is None:
        return
    if state._deliver_dispatch_result_inbound_locked(record.task, record.fallback_result):
        record.delivered = DELIVERED_INBOUND


def release_if_ended_locked(state: "SupervisorState", task: Task) -> None:
    """任务结束了：它的汇合作废，留给它的句柄放开，留着没收的结果作为新消息交回（任务是被取消的就丢掉）。不会抛异常。"""
    registry = _existing_registry(state)
    if registry is None:
        return
    try:
        now = _now()
        if _alive(state, task, now):
            return
        _release_waiter_locked(state, registry, task.task_id)
        _arm_locked(state, registry)
    except Exception:
        log.exception("releasing the dispatches held for task %s failed", getattr(task, "task_id", "?"))


def _release_waiter_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, waiter_id: str, *, join: Join | None = None,
) -> None:
    join = registry.joins.pop(waiter_id, None) or join
    waiter = state.tasks.get(waiter_id)
    drop = waiter is not None and waiter.status == TaskStatus.cancelled
    if join is not None:
        log.info("join of task %s dropped: the task ended or can no longer resume", waiter_id[:12])
        _return_join_results_locked(state, registry, join, drop=drop)
    for child_id, holder in list(registry.reserved.items()):
        if holder == waiter_id:
            registry.reserved.pop(child_id, None)
    for record in list(registry.ledger.values()):
        if record.held_for == waiter_id:
            _deliver_held_locked(state, record, drop=drop)


def _return_join_results_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, join: Join, *, drop: bool,
) -> None:
    """汇合没能交到调用方手里：收进汇合的结果照没有汇合时那样作为新消息交回，早先已经交回过的不再交。"""
    for child_id in join.settled:
        record = registry.ledger.get(child_id)
        if child_id in join.earlier or record is None or record.delivered != DELIVERED_JOIN:
            continue
        record.delivered = ""
        _deliver_held_locked(state, record, drop=drop)


def _lifetime_end(task: Task) -> datetime:
    return lifetime_origin(task.created_at, task.input) + timedelta(
        seconds=lifetime_limit_seconds(task_max_running_seconds()),
    )


def _is_system_task(task: Task) -> bool:
    context = task.input.get("task_context")
    return bool(task.input.get("_system_task") or (isinstance(context, dict) and context.get("is_system_task")))


def _dispatch_chain_ids(state: "SupervisorState", task: Task) -> set[str]:
    """任务自己，和沿委派链（同步的 caller、异步的发起任务）往上的全部任务。"""
    found = {task.task_id}
    current = task
    while True:
        parent = state.tasks.get(child_retention.parent_task_id(current))
        if parent is None or parent.task_id in found:
            return found
        found.add(parent.task_id)
        current = parent


def _task_for_handle_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, caller: Task, text: str,
) -> tuple[Task | None, str]:
    """句柄对应的异步委派任务，以及认不出时给模型看的原因。家族外的和不存在一样。"""
    handle = parse_handle(text)
    if handle is None:
        return None, _INVALID_HANDLE
    record = registry.ledger.get(handle.task_id)
    task = state.tasks.get(handle.task_id) or (record.task if record is not None else None)
    if task is None:
        return None, _NOT_FOUND
    if not is_async_dispatch(task):
        return None, _SYNC_HANDLE if (task.caller_task_id or "").strip() else _NOT_FOUND
    if _storage_session_id(state, task) != handle.session_id:
        return None, _NOT_FOUND
    caller_root = state._task_family_root_locked(caller)
    task_root = record.family_root if record is not None else state._task_family_root_locked(task)
    if not caller_root or caller_root != task_root:
        return None, _NOT_FOUND
    return task, ""


def _holder_alive_locked(state: "SupervisorState", holder_id: str, now: datetime) -> bool:
    holder = state.tasks.get(holder_id)
    return holder is not None and _alive(state, holder, now)


def route_await_locked(state: "SupervisorState", waiter: Task, request: AwaitRequest) -> None:
    """await_dispatches：挂起调用方，等齐（all）或等到第一个（any）结果、或到点时带着结果恢复它。

    在这之前已经结束的委派直接算进来；家族外、认不出的句柄作为收不到结果的项一起交回，不让整次调用失败。
    """
    registry = registry_locked(state)
    now = _now()
    timeouts = DispatchTimeouts.from_config(load_runtime_config(state.workspace_root))
    timeout = timeouts.clamp(request.timeout_sec) if request.timeout_sec is not None else timeouts.await_default
    deadline = now + timedelta(seconds=timeout)
    cap = _lifetime_end(waiter) - _LIFETIME_MARGIN
    lifetime_capped = cap < deadline
    join = Join(
        waiter_id=waiter.task_id,
        mode=request.mode,
        timeout_sec=timeout,
        started_at=now,
        deadline=max(now, cap) if lifetime_capped else deadline,
        call_id=request.call_id,
        lifetime_capped=lifetime_capped,
    )
    refusal = _SYSTEM_TASK if _is_system_task(waiter) else ""
    own_chain = _dispatch_chain_ids(state, waiter)
    # 先登记：逐项认句柄时台账可能裁掉旧记录，登记了的汇合里的项不会被裁
    registry.joins[waiter.task_id] = join
    for text in request.handles:
        task: Task | None = None
        reason = refusal
        if not reason:
            task, reason = _task_for_handle_locked(state, registry, waiter, text)
        if task is not None and not reason:
            if task.task_id in own_chain:
                reason = _SELF_HANDLE
            elif any(child_id == task.task_id for _text, child_id in join.order):
                reason = _DUPLICATE_HANDLE
            else:
                reason = _admit_locked(state, registry, join, task, now)
        if task is None or reason:
            join.refused[len(join.order)] = reason or _NOT_FOUND
            join.order.append((text, ""))
        else:
            join.order.append((text, task.task_id))
    waiter.input[AWAITING_INPUT_KEY] = {
        "handles": [text for text, _child in join.order],
        "mode": join.mode,
        "timeout_sec": join.timeout_sec,
        "deadline": join.deadline.isoformat(),
    }
    state._suspend_caller_locked(waiter)
    log.info(
        "task %s awaits %d dispatch(es) (mode=%s, timeout=%ss, settled=%d, pending=%d, refused=%d)",
        waiter.task_id[:12], len(join.order), join.mode, join.timeout_sec,
        len(join.settled), len(join.pending), len(join.refused),
    )
    if join.satisfied():
        _resume_join_locked(state, registry, join, timed_out=False)
        return
    if join.deadline <= now:
        _resume_join_locked(state, registry, join, timed_out=True)
        return
    _arm_locked(state, registry)


def _admit_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, join: Join, task: Task, now: datetime,
) -> str:
    """把一个认出来的委派放进汇合：已经结束的算进 settled，还在跑的留给调用方。返回不能放进来的原因。"""
    child_id = task.task_id
    record = registry.ledger.get(child_id) or _settled_without_hook_locked(state, registry, task, now)
    if record is not None:
        holder = _record_holder(registry, join, record)
        if holder and _holder_alive_locked(state, holder, now):
            return f"另一个任务（{_short_id(holder)}）还在等这个结果，结果会交给它。"
        _claim_settled(registry, join, record)
        return ""
    holder = registry.reserved.get(child_id, "")
    if holder and holder != join.waiter_id and _holder_alive_locked(state, holder, now):
        return f"另一个任务（{_short_id(holder)}）已经在等这个委派，结果会交给它。"
    registry.reserved[child_id] = join.waiter_id
    join.pending.add(child_id)
    return ""


def _record_holder(registry: AsyncDispatchRegistry, join: Join, record: SettledDispatch) -> str:
    """结果留给了别的任务，或者已经收进别的任务还没交出去的汇合：那个任务的 id。"""
    if record.held_for and record.held_for != join.waiter_id:
        return record.held_for
    child_id = record.task.task_id
    return next(
        (other.waiter_id for other in registry.joins.values() if other is not join and child_id in other.settled),
        "",
    )


def _claim_settled(registry: AsyncDispatchRegistry, join: Join, record: SettledDispatch) -> None:
    """已经结束的委派算进汇合；结果早先交到过别处的，记下是哪里，交回时注明。"""
    child_id = record.task.task_id
    join.pending.discard(child_id)
    join.settled.add(child_id)
    registry.reserved.pop(child_id, None)
    if record.delivered in _DELIVERED_PLACES:
        join.earlier[child_id] = record.delivered
    else:
        record.delivered = DELIVERED_JOIN
    record.held_for = ""


def _entry_locked(
    state: "SupervisorState",
    registry: AsyncDispatchRegistry,
    join: Join,
    index: int,
    text: str,
    child_id: str,
    *,
    timed_out: bool,
) -> dict[str, Any]:
    """汇合交回调用方的一项：已经结束的照同步批次的样子给结果，还在跑的标 running，认不出的标 unavailable。"""
    reason = join.refused.get(index)
    if reason is not None:
        return {"kind": "node", "status": STATUS_UNAVAILABLE, "handle": text, "error": reason}
    if child_id in join.missing:
        return {"kind": "node", "status": STATUS_UNAVAILABLE, "handle": text, "error": _VANISHED}
    record = registry.ledger.get(child_id)
    task = record.task if record is not None else state.tasks.get(child_id)
    if task is None:
        return {"kind": "node", "status": STATUS_UNAVAILABLE, "handle": text, "error": _VANISHED}
    if child_id in join.settled:
        settled = state._dispatch_result_entry_locked(task)
        settled.setdefault("handle", handle_of_locked(state, task) or text)
        settled.setdefault("child_session_id", _storage_session_id(state, task))
        earlier = join.earlier.get(child_id, "")
        if earlier:
            settled["delivered_earlier"] = earlier
            # 早先那次交回时附件已经发给用户或交给了那一轮，这里再给就会发两遍
            settled.pop("attachments", None)
        return settled
    running: dict[str, Any] = {
        "kind": "node",
        "status": STATUS_RUNNING,
        "state": task.status.value,
        "node_id": str(task.node_id or ""),
        "instruction": str(task.input.get("instruction") or ""),
        "handle": handle_of_locked(state, task) or text,
        "child_task_id": task.task_id,
    }
    if timed_out:
        running["timed_out"] = True
        running["waited_sec"] = max(0, int((join.deadline - join.started_at).total_seconds()))
        if join.lifetime_capped:
            running["lifetime_capped"] = True
    return running


def _resume_join_locked(state: "SupervisorState", registry: AsyncDispatchRegistry, join: Join, *, timed_out: bool) -> None:
    registry.joins.pop(join.waiter_id, None)
    waiter = state.tasks.get(join.waiter_id)
    if waiter is None or not state._caller_can_resume_locked(waiter):
        log.warning("join of task %s dropped: the task can no longer resume", join.waiter_id[:12])
        _release_waiter_locked(state, registry, join.waiter_id, join=join)
        _arm_locked(state, registry)
        return
    entries = [
        _entry_locked(state, registry, join, index, text, child_id, timed_out=timed_out)
        for index, (text, child_id) in enumerate(join.order)
    ]
    resume_data = {
        "type": "batch_results",
        "entries": entries,
        "await": {
            "mode": join.mode,
            "timeout_sec": join.timeout_sec,
            "timed_out": bool(timed_out and join.pending),
            "call_id": join.call_id,
        },
    }
    log.info(
        "resuming task %s from await_dispatches (settled=%d, still running=%d, unavailable=%d, timed_out=%s)",
        join.waiter_id[:12], len(join.settled), len(join.pending), len(join.refused) + len(join.missing),
        bool(timed_out and join.pending),
    )
    state._resume_suspended_task_locked(waiter, resume_data, drop_input_keys=(AWAITING_INPUT_KEY,))
    _arm_locked(state, registry)


def _catch_up_join_locked(state: "SupervisorState", registry: AsyncDispatchRegistry, join: Join, now: datetime) -> None:
    """汇合里还在等的项若已经结束却没经过结算（会话重置时直接改的终态），或者任务记录没了，这里补上。"""
    for child_id in list(join.pending):
        task = state.tasks.get(child_id)
        if task is None:
            join.pending.discard(child_id)
            join.missing.add(child_id)
            registry.reserved.pop(child_id, None)
            continue
        record = registry.ledger.get(child_id) or _settled_without_hook_locked(state, registry, task, now)
        if record is None:
            continue
        _claim_settled(registry, join, record)


def item_deadline_inputs(seconds: int, *, now: datetime | None = None) -> dict[str, Any]:
    """建批次子任务时一并写进 input 的超时字段。"""
    due = (now or _now()) + timedelta(seconds=int(seconds))
    return {ITEM_TIMEOUT_SEC_KEY: int(seconds), ITEM_DEADLINE_KEY: due.isoformat()}


def watch_item_locked(state: "SupervisorState", child: Task) -> None:
    """登记批次子任务的截止时刻（读它 input 里的字段），到点还没结束就按失败收掉。"""
    raw = str(child.input.get(ITEM_DEADLINE_KEY) or "").strip()
    if not raw or state._task_terminal(child):
        return
    try:
        due = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("dispatch item %s has an unreadable deadline %r", child.task_id[:12], raw)
        return
    registry = registry_locked(state)
    registry.item_deadlines[child.task_id] = due
    _arm_locked(state, registry)


def _expire_item_locked(state: "SupervisorState", child_id: str, now: datetime) -> None:
    child = state.tasks.get(child_id)
    if child is None or state._task_terminal(child):
        return
    seconds = int(child.input.get(ITEM_TIMEOUT_SEC_KEY) or 0)
    child.input[ITEM_TIMED_OUT_KEY] = True
    log.warning(
        "dispatch item %s (node=%s) of task %s timed out after %ss",
        child.task_id[:12], child.node_id, (child.caller_task_id or "")[:12], seconds,
    )
    # 按失败收掉：会话照失败的规矩保留，调用方能用句柄 continue_from 接着做
    state._fail_task_locked(child, error=f"等待超时：{seconds} 秒内没有做完，已被停下。", now=now)


def expire_due_locked(state: "SupervisorState", *, now: datetime | None = None) -> None:
    """到点的汇合带着已有结果恢复调用方，到点的批次项按失败收掉，顺带清理任务已经结束的汇合和保留。"""
    registry = _existing_registry(state)
    if registry is None:
        return
    now = now or _now()
    for join in list(registry.joins.values()):
        if registry.joins.get(join.waiter_id) is not join:
            continue
        waiter = state.tasks.get(join.waiter_id)
        if waiter is None or not _alive(state, waiter, now):
            _release_waiter_locked(state, registry, join.waiter_id)
            continue
        _catch_up_join_locked(state, registry, join, now)
        if join.satisfied() or join.deadline <= now:
            try:
                _resume_join_locked(state, registry, join, timed_out=not join.satisfied())
            except Exception:
                log.exception("resuming task %s from its join failed", join.waiter_id[:12])
    for child_id, due in list(registry.item_deadlines.items()):
        if due > now:
            continue
        registry.item_deadlines.pop(child_id, None)
        try:
            _expire_item_locked(state, child_id, now)
        except Exception:
            log.exception("timing out dispatch item %s failed", child_id[:12])
    holders = set(registry.reserved.values()) | {record.held_for for record in registry.ledger.values() if record.held_for}
    for holder_id in holders:
        if not _holder_alive_locked(state, holder_id, now):
            _release_waiter_locked(state, registry, holder_id)
    _trim_ledger_locked(registry, now)
    _arm_locked(state, registry)


def wait_target_state_locked(state: "SupervisorState", task: Task) -> tuple[bool, bool, bool]:
    """挂起等异步委派的任务给回收看的 (全部结束, 找不到, 还在等)：汇合还登记着就算在等。"""
    registry = _existing_registry(state)
    if registry is not None and task.task_id in registry.joins:
        return False, False, True
    return False, True, False


def _next_due(registry: AsyncDispatchRegistry) -> datetime | None:
    dues = [join.deadline for join in registry.joins.values()] + list(registry.item_deadlines.values())
    return min(dues) if dues else None


def _arm_locked(state: "SupervisorState", registry: AsyncDispatchRegistry) -> None:
    """只留一个定时器，定在最早的截止时刻；回收线程每分钟也会兜底查一遍。"""
    due = _next_due(registry)
    if due is not None and due == registry.timer_due and registry.timer is not None:
        return
    if registry.timer is not None:
        registry.timer.cancel()
    registry.timer = None
    registry.timer_due = due
    registry.timer_token = None
    if due is None:
        return
    token = object()
    delay = max(0.0, (due - _now()).total_seconds()) + _TIMER_SLACK_SEC
    timer = threading.Timer(delay, _on_timer, args=(weakref.ref(state), token))
    timer.daemon = True
    timer.name = "dispatch-deadlines"
    registry.timer = timer
    registry.timer_token = token
    timer.start()


def _on_timer(state_ref: "weakref.ReferenceType[SupervisorState]", token: object) -> None:
    state = state_ref()
    if state is None:
        return
    try:
        with state._lock:
            registry = registry_locked(state)
            if registry.timer_token is token:
                registry.timer = None
                registry.timer_due = None
                registry.timer_token = None
            expire_due_locked(state)
    except Exception:
        log.exception("dispatch deadline check failed")


def _caller_locked(state: "SupervisorState", caller_task_id: str) -> Task | None:
    caller = state.tasks.get(str(caller_task_id or "").strip())
    if caller is None or state._task_terminal(caller):
        return None
    return caller


def _active_item_locked(
    state: "SupervisorState", registry: AsyncDispatchRegistry, caller: Task, task: Task, now: datetime,
) -> dict[str, Any]:
    if state._task_terminal(task):
        status = "finishing"
    elif task.status == TaskStatus.suspended:
        status = "waiting"
    elif task.status == TaskStatus.running:
        status = "running"
    else:
        status = "queued"
    origin_caller = _origin_caller_id(task)
    origin = task.input.get("_dispatch_origin")
    item: dict[str, Any] = {
        "handle": handle_of_locked(state, task),
        "node_id": str(task.node_id or ""),
        "status": status,
        "instruction": _preview(task.input.get("instruction")),
        "age_sec": max(0, int((now - task.created_at).total_seconds())),
        "dispatched_by": "you" if origin_caller == caller.task_id else (
            f"{(origin.get('caller_node_id') if isinstance(origin, dict) else '') or 'task'}({_short_id(origin_caller)})"
        ),
    }
    phase = " ".join(part for part in (task.current_phase, _preview(task.current_detail, 80)) if part)
    if phase:
        item["activity"] = phase
    holder = registry.reserved.get(task.task_id, "")
    if holder:
        item["awaited_by"] = "you" if holder == caller.task_id else _short_id(holder)
    return item


def _finished_item_locked(
    state: "SupervisorState", caller: Task, record: SettledDispatch, now: datetime,
) -> dict[str, Any]:
    task = record.task
    inner = (task.result or {}).get("result")
    inner = inner if isinstance(inner, dict) else {}
    item: dict[str, Any] = {
        "handle": handle_of_locked(state, task),
        "node_id": str(task.node_id or ""),
        "status": record.status,
        "instruction": _preview(task.input.get("instruction")),
        "finished_sec_ago": max(0, int((now - record.settled_at).total_seconds())),
    }
    if record.status in ("fail", "cancelled"):
        item["error"] = _preview((task.result or {}).get("error"))
    else:
        summary = _preview(inner.get("summary") or (task.result or {}).get("summary") or inner.get("text"))
        if summary:
            item["summary"] = summary
    if record.held_for:
        item["delivered"] = "held_for_you" if record.held_for == caller.task_id else "held"
    else:
        item["delivered"] = record.delivered or "not_delivered"
    continue_fields = child_retention.continue_fields_locked(state, task)
    if continue_fields.get("continuable"):
        item["continuable"] = True
    return item


def list_locked(state: "SupervisorState", caller_task_id: str) -> dict[str, Any] | None:
    """调用方会话家族里的异步委派：还没结束的全部（最早的在前），加上最近结束的几个。调用方不在或已结束给 None。"""
    caller = _caller_locked(state, caller_task_id)
    if caller is None:
        return None
    registry = registry_locked(state)
    now = _now()
    root = state._task_family_root_locked(caller)
    if not root:
        return {"ok": True, "active": [], "finished": [], "active_total": 0, "finished_total": 0}
    active = [
        task for task in state.tasks.values()
        if is_async_dispatch(task)
        and task.task_id not in registry.ledger
        and not (state._task_terminal(task) and task.route_status == RouteStatus.routed)
        and state._task_family_root_locked(task) == root
    ]
    active.sort(key=lambda task: task.created_at)
    finished = [record for record in reversed(registry.ledger.values()) if record.family_root == root]
    return {
        "ok": True,
        "active": [_active_item_locked(state, registry, caller, task, now) for task in active[:LIST_ACTIVE_LIMIT]],
        "finished": [_finished_item_locked(state, caller, record, now) for record in finished[:LIST_SETTLED_LIMIT]],
        "active_total": len(active),
        "finished_total": len(finished),
    }


def _cancel_refusal(reason: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message}


def cancel_locked(state: "SupervisorState", caller_task_id: str, raw_handle: str) -> dict[str, Any]:
    """取消调用方会话家族里的一个异步委派，连同它派出去的全部后代（同步派的和异步派的）。"""
    caller = _caller_locked(state, caller_task_id)
    if caller is None:
        return _cancel_refusal("caller_not_found", "找不到发起这次取消的任务。")
    registry = registry_locked(state)
    task, reason = _task_for_handle_locked(state, registry, caller, str(raw_handle or ""))
    if task is None:
        return _cancel_refusal("invalid_handle" if reason == _INVALID_HANDLE else "not_found", reason)
    if task.task_id in _dispatch_chain_ids(state, caller):
        return _cancel_refusal("self", "不能取消自己，也不能取消派出自己的那个任务。")
    now = _now()
    handle = handle_of_locked(state, task)
    already_finished = state._task_terminal(task)
    # 委派本身已经结束时，它派出去还在跑的后代照样取消
    targets = ([] if already_finished else [task]) + state._dispatched_descendants_locked(task.task_id)
    cancelled = 0
    requested = 0
    for target in targets:
        cause = "调用方取消了这个委派" if target.task_id == task.task_id else "上级委派已被取消"
        effect = state._cancel_task_locked(target, now, reason=cause)
        if effect is CancelEffect.cancelled:
            cancelled += 1
        elif effect is CancelEffect.requested:
            requested += 1
    log.info(
        "task %s cancelled dispatch %s: %d cancelled, %d asked to stop",
        caller.task_id[:12], task.task_id[:12], cancelled, requested,
    )
    result: dict[str, Any] = {
        "ok": True,
        "handle": handle,
        "node_id": str(task.node_id or ""),
        "cancelled_count": cancelled,
        "stopping_count": requested,
    }
    if already_finished:
        result["status"] = state._task_outcome_action(task) or "finished"
        result["already_finished"] = True
    elif state._task_terminal(task):
        result["status"] = "cancelled"
    else:
        result["status"] = "stopping"
    return result


def list_dispatches(state: "SupervisorState", caller_task_id: str) -> dict[str, Any] | None:
    with state._lock:
        return list_locked(state, caller_task_id)


def cancel_dispatch(state: "SupervisorState", caller_task_id: str, raw_handle: str) -> dict[str, Any]:
    with state._lock:
        return cancel_locked(state, caller_task_id, raw_handle)
