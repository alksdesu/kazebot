"""异步委派的结果台账和 await_dispatches 汇合，结果留给某一轮、合成一轮交回，list_dispatches / cancel_dispatch，
wait 委派单项超时的到点处理，以及认出带着同一个投递 id 重发的入站。

都只在内存里：supervisor 重启后任务本身也不在了。只有认重发的入站在内存里没记着时，再查事件日志的入站回执。
带 _locked 的函数要求调用方持有 state._lock。
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
from engine.dispatch_batch import (
    AWAIT_ANY,
    AWAITING_INPUT_KEY,
    INBOUND_CONFLICT_STATUS,
    AwaitRequest,
    DispatchTimeouts,
)
from engine.dispatch_handle import format_handle, parse_handle
from engine.dispatch_results import INBOUND_RESULTS_KEY
from engine.run_budget import lifetime_limit_seconds, lifetime_origin, parse_timestamp

from . import child_retention
from ._helpers import _now
from .eventlog import INBOUND_DELIVERY
from .task_ending import TaskEnding
from .task_store import CancelEffect
from .types import RouteStatus, Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .session_reuse import InboundRefusal
    from .state import SupervisorState

log = logging.getLogger(__name__)

# wait 委派单项超时：supervisor 写进子任务 input
ITEM_TIMEOUT_SEC_KEY = "_wait_timeout_sec"
ITEM_DEADLINE_KEY = "_wait_deadline"
ITEM_TIMED_OUT_KEY = "_wait_timed_out"
# 派它的那一层最晚等到什么时候：它自己挂起等结果时也不能等过这个时刻
ITEM_LIFETIME_CAP_KEY = "_wait_lifetime_cap"
# 这一项的截止时间来自调用方的总寿命，不是它自己写的 timeout_sec
ITEM_LIFETIME_CAPPED_KEY = "_wait_lifetime_capped"

DELIVERED_INBOUND = "inbound"
DELIVERED_JOIN = "join"
DELIVERED_DROPPED = "dropped"
# 结果真正交到过模型手里的去处；随被取消的任务一起丢掉的不算
_DELIVERED_PLACES = frozenset({DELIVERED_INBOUND, DELIVERED_JOIN})
STATUS_RUNNING = "running"
STATUS_UNAVAILABLE = "unavailable"
# 结果为什么留着：await 过它的任务还活着，或者落点节点那一轮还没做完
HOLD_AWAIT = "await"
HOLD_ROUND = "round"
# 和 task_router 里压缩 dispatch 挂起时写的标记是同一个键
COMPACT_PENDING_KEY = "_compact_dispatch_pending"
# 结果被 await 收走后撤掉的那一轮：它不是一次委派，不进台账，也不在 list_dispatches 里出现
_WITHDRAWN_KEY = "_result_withdrawn"

LEDGER_LIMIT = 512
LEDGER_TTL = timedelta(hours=24)
# 调用方给的投递 id 加上这个前缀再写进入站事件，和内部那些 delivery_id 分开，外面的调用方占不了内部的 id
_INBOUND_DELIVERY_PREFIX = "inbound:"
# engine 带同一个 id 重发只在几十秒之内，按这段时间里收得到的入站留足
RECEIVED_LIMIT = 4096
LIST_ACTIVE_LIMIT = 50
LIST_SETTLED_LIMIT = 20
_PREVIEW_CHARS = 160
# 等待截止留出这段余量，挂起的任务先带着已有结果恢复，再被总寿命回收
_LIFETIME_MARGIN = timedelta(minutes=10)
# 定时器早醒一点就会错过刚好到点的那一项
_TIMER_SLACK_SEC = 0.05

_NOT_FOUND = "找不到这个委派：句柄不对、不属于这段对话，或者它结束得太早、记录已经清掉了（那样的话结果早先已经作为消息交回过）。"
_INVALID_HANDLE = "句柄格式不对：要照抄委派结果里给的句柄（task_id@session_id）。"
_SYNC_HANDLE = "这是 wait=true 同步委派的句柄，它的结果已经随那一批结果交回，不用再等。"
_SYNC_HANDLE_RUNNING = "这是 wait=true 同步委派的句柄：它的结果只交给挂起等这一批的那个任务，await_dispatches 收不到。"
_SYNC_CANCEL_RUNNING = (
    "这是 wait=true 同步委派的句柄，不能单独取消：它跟着挂起等它的那个任务走，"
    "要停它就取消或停下那个任务，它会一起停下。"
)
_SYNC_CANCEL_FINISHED = "这是 wait=true 同步委派的句柄，它已经结束，结果已随那一批结果交回调用方，不用取消。"
_SYNC_REASONS = frozenset({_SYNC_HANDLE, _SYNC_HANDLE_RUNNING, _SYNC_CANCEL_RUNNING, _SYNC_CANCEL_FINISHED})
PURPOSE_AWAIT = "await"
PURPOSE_CANCEL = "cancel"
_LIFETIME_CAPPED_ERROR = (
    "调用方的总运行时长快到上限，没等它做完就先收了，它已经停下（不是这一项自己写的 timeout_sec 到点）。"
)
WAIT_CAP_PASSED = (
    f"没有派出：本任务离总运行时长上限已不到 {int(_LIFETIME_MARGIN.total_seconds() // 60)} 分钟，"
    "再派需要等结果的子任务会来不及收尾；先把已有的结果整理交回。"
)
_SELF_HANDLE = "不能等自己，也不能等派出自己的那个任务。"
_DUPLICATE_HANDLE = "和前面的句柄是同一个委派。"
_SYSTEM_TASK = "系统任务不能挂起等待异步委派的结果。"
_VANISHED = "这个委派的任务记录已经不在了，拿不到它的结果。"


@dataclass
class SettledDispatch:
    """一个已经结束的异步委派。delivered 是结果第一次交到了哪里；held_for 非空时结果先留给那个任务，还没交出去，
    hold 说明为什么留（await 过它，或落点节点那一轮还没做完）。

    callback_task_id 是作为新消息交回时接它的那一轮；refs_session 是它引用的超长输出眼下复制到了哪个会话。
    """

    task: Task
    status: str
    family_root: str
    settled_at: datetime
    fallback_result: dict[str, Any] | None = None
    delivered: str = ""
    held_for: str = ""
    hold: str = ""
    callback_task_id: str = ""
    refs_session: str = ""


@dataclass(frozen=True)
class DispatchDelivery:
    """一次作为新消息交回：建出的那一轮（全部早先已经交回过时为 None），和这次算作已交回的子任务。"""

    callback: Task | None
    delivered: tuple[str, ...]


@dataclass(frozen=True)
class InboundReceipt:
    """收下过的一条入站：带着同一个投递 id 重发时照原样回的应答。"""

    session_id: str
    inbound_seq: int
    task_id: str


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
        # 任务 id -> 它的过渡段最晚到什么时候：到点还没等来路由或回报，按结束处理，在等它的批次就此收齐
        self.transit_checks: dict[str, datetime] = {}
        # (会话键, 投递 id) -> 第一次收下时的应答，最近收下的排在后面
        self.received: OrderedDict[tuple[str, str], InboundReceipt] = OrderedDict()
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


def inbound_delivery_key(raw: Any) -> str:
    """入站带的投递 id 写进事件时的样子；没带给空串。"""
    text = str(raw or "").strip()
    return f"{_INBOUND_DELIVERY_PREFIX}{text}" if text else ""


def received_locked(state: "SupervisorState", conversation_key: str, key: str) -> InboundReceipt | None:
    """这段对话里这个投递 id 收下过就回那次的应答；没收过、或 key 为空给 None。

    内存里只记着最近收下的；没记着的（supervisor 重启过、或早被挤出去了）再按事件日志的入站回执认。
    """
    if not key:
        return None
    registry = _existing_registry(state)
    receipt = registry.received.get((conversation_key, key)) if registry is not None else None
    return receipt if receipt is not None else _logged_receipt_locked(state, conversation_key, key)


def _logged_receipt_locked(state: "SupervisorState", conversation_key: str, key: str) -> InboundReceipt | None:
    """事件日志里这个投递 id 的入站回执，得落在这段对话现在的会话里：会话重置过、或别的对话用过这个 id 的都不算。"""
    logged = state.eventlog.find_receipt(INBOUND_DELIVERY, key)
    session_id = state.conversation_map.get(conversation_key)
    if logged is None or not session_id or logged["session_id"] != session_id or session_id not in state.sessions:
        return None
    inbound_seq = int(logged["seq"])
    return InboundReceipt(session_id=session_id, inbound_seq=inbound_seq, task_id=_entry_task_id_locked(state, inbound_seq))


def _entry_task_id_locked(state: "SupervisorState", inbound_seq: int) -> str:
    """这条入站建出的入口任务；已不在内存里给空串。从它派生的子任务也带着同一个 seq，但都有 caller。"""
    for task in state.tasks.values():
        if task.source_inbound_seq == inbound_seq and not task.caller_task_id:
            return task.task_id
    return ""


def remember_received_locked(
    state: "SupervisorState", conversation_key: str, key: str, receipt: InboundReceipt,
) -> None:
    """记下一条已经收下的入站。和建任务在同一次持锁里记，重发进锁时就认得出。"""
    if not key:
        return
    received = registry_locked(state).received
    slot = (conversation_key, key)
    received[slot] = receipt
    received.move_to_end(slot)
    while len(received) > RECEIVED_LIMIT:
        received.popitem(last=False)


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
    """还会接着跑、还收得了结果：刚交回 dispatch、正在路由去挂起的也算。"""
    return state._task_ending_locked(task, now).continues


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
    """异步委派到了终点：记进台账；有汇合在等就交给汇合，留给某个还活着的、await 过它的任务就先留着。

    返回 True 表示结果已经有了去处，调用方不要再把它作为新消息交回。
    """
    if task.input.get(_WITHDRAWN_KEY):
        return True
    if not is_async_dispatch(task):
        return False
    registry = registry_locked(state)
    now = _now()
    record = _record_locked(state, registry, task, fallback_result=fallback_result, now=now)
    if record.delivered or record.held_for:
        # 同一次完成的路由重推：第一次已经交出去或留下了
        return True
    if not record.refs_session:
        # 完成路由在这之前已经按同一个读者复制过它引用的超长输出
        record.refs_session = state._dispatch_result_reader_session_locked(task)
    waiter_id = registry.reserved.get(task.task_id, "")
    waiter = state.tasks.get(waiter_id) if waiter_id else None
    if waiter is None or not _alive(state, waiter, now):
        registry.reserved.pop(task.task_id, None)
        return False
    join = registry.joins.get(waiter_id)
    if join is not None and task.task_id in join.pending:
        _claim_settled(state, registry, join, record)
        log.info(
            "async dispatch %s settled into the join of task %s (status=%s)",
            task.task_id[:12], waiter_id[:12], record.status,
        )
        if join.satisfied():
            _resume_join_locked(state, registry, join, timed_out=False)
        return True
    record.held_for = waiter_id
    record.hold = HOLD_AWAIT
    log.info("async dispatch %s settled while task %s holds it; result kept for it", task.task_id[:12], waiter_id[:12])
    return True


def hold_for_round_locked(state: "SupervisorState", task: Task, holder: Task) -> bool:
    """落点节点那一轮还没做完：结果先留给它，它能用 await_dispatches 收走，没收的等它结束或挂起等待时合成一轮交回。

    结果不在台账里（不是异步委派）、已经交出去或留下了时返回 False，由调用方照常交回。
    """
    registry = _existing_registry(state)
    record = registry.ledger.get(task.task_id) if registry is not None else None
    if record is None or record.delivered or record.held_for:
        return False
    record.held_for = holder.task_id
    record.hold = HOLD_ROUND
    log.info(
        "result of async dispatch %s held for round %s (node=%s) until it ends",
        task.task_id[:12], holder.task_id[:12], holder.node_id,
    )
    return True


def round_open_locked(state: "SupervisorState", task: Task, now: datetime | None = None) -> bool:
    """这一轮还算没做完、新回来的结果该留给它：排队、在跑、为压缩挂起，或刚交回 dispatch 还没路由完。

    wait、await 挂起的不算，免得结果被一个长时间挂起的任务压住；已被要求停下的也不算。
    """
    if state._task_cancel_pending_locked(task):
        return False
    if task.status == TaskStatus.suspended:
        return bool(task.input.get(COMPACT_PENDING_KEY))
    return _alive(state, task, now or _now())


def release_round_holds_locked(state: "SupervisorState", task: Task) -> None:
    """一轮挂起等子任务或 await_dispatches：留给它的结果不再压着，合成一轮交回；为压缩挂起的照旧留着。不会抛异常。"""
    if task.input.get(COMPACT_PENDING_KEY):
        return
    registry = _existing_registry(state)
    if registry is None:
        return
    try:
        held = [
            record for record in registry.ledger.values()
            if record.held_for == task.task_id and record.hold == HOLD_ROUND
        ]
        if held:
            _hand_back_locked(state, held, drop=False)
    except Exception:
        log.exception("handing back the results held for round %s failed", getattr(task, "task_id", "?"))


def mark_delivered_locked(state: "SupervisorState", task: Task, delivery: DispatchDelivery) -> None:
    """结果已经作为新消息交回落点会话：记下接它的是哪一轮，await 收走时还能从那一轮撤下。"""
    registry = _existing_registry(state)
    record = registry.ledger.get(task.task_id) if registry is not None else None
    if record is not None:
        _mark_inbound(record, delivery)


def _mark_inbound(record: SettledDispatch, delivery: DispatchDelivery) -> None:
    if record.task.task_id not in delivery.delivered:
        return
    if not record.delivered:
        record.delivered = DELIVERED_INBOUND
    record.callback_task_id = delivery.callback.task_id if delivery.callback is not None else ""


def _hand_back_locked(state: "SupervisorState", records: list[SettledDispatch], *, drop: bool) -> None:
    """留着或收进汇合却没交出去的结果作为新消息交回：按落点分组，同一落点的合成一轮。取消、打断的异步委派本来就不交回。

    drop 为真时持有它们的任务是被取消的，结果不再交回，免得任务停下之后又开出新任务；台账里还在，
    list_dispatches 看得到，await_dispatches 还能收。
    """
    for record in records:
        record.held_for = ""
        record.hold = ""
    if drop:
        for record in records:
            record.delivered = DELIVERED_DROPPED
            log.info("result of async dispatch %s dropped: the task holding it was cancelled", record.task.task_id[:12])
        return
    groups: dict[tuple[str, str, str, str], list[tuple[SettledDispatch, Any]]] = {}
    for record in sorted(records, key=lambda item: item.settled_at):
        if record.fallback_result is None:
            continue
        home = state._dispatch_result_home_locked(record.task)
        if home is None:
            log.warning(
                "result of async dispatch %s dropped: no session along its dispatch chain can take it",
                record.task.task_id[:12],
            )
            continue
        groups.setdefault(state._result_group_key_locked(home), []).append((record, home))
    for members in groups.values():
        for record, home in members:
            _ensure_refs_locked(state, record, home.session_id)
        delivery = state._deliver_dispatch_results_locked(
            [(home, record.task, record.fallback_result or {}) for record, home in members],
        )
        for record, _home in members:
            _mark_inbound(record, delivery)


def release_if_ended_locked(state: "SupervisorState", task: Task) -> None:
    """任务结束了：它的汇合作废，留给它的句柄放开，收进汇合没交出去的和留给它的结果合成新消息交回
    （任务是被取消的就丢掉）。不会抛异常。
    """
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
    returned: list[SettledDispatch] = []
    if join is not None:
        log.info("join of task %s dropped: the task ended or can no longer resume", waiter_id[:12])
        returned.extend(_unreturned_join_results(registry, join))
    for child_id, holder in list(registry.reserved.items()):
        if holder == waiter_id:
            registry.reserved.pop(child_id, None)
    returned.extend(record for record in registry.ledger.values() if record.held_for == waiter_id)
    if returned:
        _hand_back_locked(state, returned, drop=drop)


def _unreturned_join_results(registry: AsyncDispatchRegistry, join: Join) -> list[SettledDispatch]:
    """汇合没能交到调用方手里时收进它的结果；早先已经交回过的不再交。"""
    returned: list[SettledDispatch] = []
    for child_id in join.settled:
        record = registry.ledger.get(child_id)
        if child_id in join.earlier or record is None or record.delivered != DELIVERED_JOIN:
            continue
        record.delivered = ""
        returned.append(record)
    return returned


def reserved_waiter_locked(state: "SupervisorState", task: Task) -> Task | None:
    """await 过这个委派、还活着的任务：结果交给它，它就是读结果的一方。"""
    registry = _existing_registry(state)
    waiter_id = registry.reserved.get(task.task_id, "") if registry is not None else ""
    waiter = state.tasks.get(waiter_id) if waiter_id else None
    return waiter if waiter is not None and _alive(state, waiter, _now()) else None


def chains_to_keep_locked(state: "SupervisorState") -> set[str]:
    """留着、或收进汇合还没交出去的结果，交回时要沿派发链重新找落点：这些链上的任务不能被清出内存。"""
    registry = _existing_registry(state)
    if registry is None:
        return set()
    in_joins = {child_id for join in registry.joins.values() for child_id in join.settled}
    keep: set[str] = set()
    for child_id, record in registry.ledger.items():
        if record.held_for or child_id in in_joins:
            keep |= _dispatch_chain_ids(state, record.task)
    return keep


def _ensure_refs_locked(state: "SupervisorState", record: SettledDispatch, target_session_id: str) -> None:
    """结果引用的超长输出要在读它的一方的会话名下：读者变了在锁里补一次复制，这种情况很少。"""
    if not target_session_id or target_session_id == record.refs_session:
        return
    refs = child_retention.result_refs(record.task)
    source = record.refs_session or child_retention.archive_session(record.task)
    if refs and source and source != target_session_id:
        child_retention.adopt(
            state.workspace_root, child_retention.RefAdoption(source, target_session_id, refs),
        )
    record.refs_session = target_session_id


def _withdraw_from_callback_locked(state: "SupervisorState", record: SettledDispatch) -> bool:
    """结果已作为新消息交回、接它的那一轮还没被领走：从那一轮撤下这条结果，改由收走它的 await 交出。

    那一轮只有这一条结果就整轮撤掉；那一轮已经领走过、开跑过的撤不了，返回 False。
    """
    callback = state.tasks.get(record.callback_task_id) if record.callback_task_id else None
    if (
        callback is None
        or callback.status != TaskStatus.pending
        or callback.claim_id
        or state._task_cancel_pending_locked(callback)
    ):
        return False
    child_id = record.task.task_id
    items = callback.input.get(INBOUND_RESULTS_KEY)
    if isinstance(items, list) and len(items) > 1:
        kept = [
            item for item in items
            if isinstance(item, dict) and str(item.get("inbound_child_task_id") or "") != child_id
        ]
        if not kept or len(kept) == len(items):
            return False
        _keep_callback_results(callback, kept)
        log.info("result of async dispatch %s taken out of round %s", child_id[:12], callback.task_id[:12])
        return True
    if str(callback.input.get("inbound_child_task_id") or "") != child_id:
        return False
    callback.input[_WITHDRAWN_KEY] = True
    # 撤掉不是用户取消：它排队时留给它的结果照常交回，不能随取消丢掉
    registry = registry_locked(state)
    held = [entry for entry in registry.ledger.values() if entry.held_for == callback.task_id]
    for entry in held:
        entry.held_for = ""
        entry.hold = ""
    state._cancel_task_locked(callback, _now(), reason="这条结果已被 await_dispatches 收走，这一轮不再需要")
    log.info("round %s withdrawn: its only result %s was collected by an await", callback.task_id[:12], child_id[:12])
    if held:
        _hand_back_locked(state, held, drop=False)
    return True


def _keep_callback_results(callback: Task, kept: list[dict[str, Any]]) -> None:
    """合并的那一轮撤下几条以后重写它的输入：顶层 inbound_* 照剩下的第一条，附件是剩下几条没外发的并集；只剩一条时退回单条格式。"""
    for key in [key for key in callback.input if key.startswith("inbound_") and key != "inbound_message_type"]:
        callback.input.pop(key, None)
    first = kept[0]
    callback.input.update({key: value for key, value in first.items() if key.startswith("inbound_")})
    callback.input["instruction"] = str(first.get("text") or "")
    callback.input["attachments"] = [
        attachment
        for item in kept
        if isinstance(item.get("attachments"), list)
        for attachment in item["attachments"]
    ]
    if any(item.get("inbound_attachments_outbound_sent") for item in kept):
        callback.input["inbound_attachments_outbound_sent"] = True
    else:
        callback.input.pop("inbound_attachments_outbound_sent", None)
    if len(kept) > 1:
        callback.input[INBOUND_RESULTS_KEY] = kept


def dispatcher_refusal_locked(state: "SupervisorState", payload: dict[str, Any]) -> "InboundRefusal | None":
    """异步委派的入站：派出它的任务已经结束、或已被要求停下时拒收；派发方的记录不在内存里（旧回调链、已清理）照收。"""
    origin = payload.get("dispatch_origin")
    if not isinstance(origin, dict) or not origin:
        return None
    dispatcher_id = str(origin.get("caller_task_id") or "").strip()
    dispatcher = state.tasks.get(dispatcher_id) if dispatcher_id else None
    if dispatcher is None:
        return None
    # session_reuse 要在模块级引用这里的检查，反过来只能用到时再导入
    from .session_reuse import InboundRefusal

    if not _alive(state, dispatcher, _now()):
        outcome = state._task_outcome_action(dispatcher) or dispatcher.status.value
        return InboundRefusal(
            INBOUND_CONFLICT_STATUS,
            f"没有派出：发起这次委派的任务已经结束（{outcome}），不再派出新的异步委派。",
        )
    if state._task_cancel_pending_locked(dispatcher):
        return InboundRefusal(
            INBOUND_CONFLICT_STATUS,
            "没有派出：发起这次委派的任务已被要求停下，不再派出新的异步委派。",
        )
    return None


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
    state: "SupervisorState", registry: AsyncDispatchRegistry, caller: Task, text: str, *, purpose: str,
) -> tuple[Task | None, str]:
    """句柄对应的异步委派任务，以及认不出时给模型看的原因。

    先核会话和家族，家族外的一律和不存在一样；家族里的同步委派按 purpose（await 或 cancel）和它结束没有照实说明。
    """
    handle = parse_handle(text)
    if handle is None:
        return None, _INVALID_HANDLE
    record = registry.ledger.get(handle.task_id)
    task = state.tasks.get(handle.task_id) or (record.task if record is not None else None)
    if task is None:
        return None, _NOT_FOUND
    if _storage_session_id(state, task) != handle.session_id:
        return None, _NOT_FOUND
    caller_root = state._task_family_root_locked(caller)
    task_root = record.family_root if record is not None else state._task_family_root_locked(task)
    if not caller_root or caller_root != task_root:
        return None, _NOT_FOUND
    if not is_async_dispatch(task):
        if not (task.caller_task_id or "").strip():
            return None, _NOT_FOUND
        return None, _sync_handle_reason(state, task, purpose)
    return task, ""


def _sync_handle_reason(state: "SupervisorState", task: Task, purpose: str) -> str:
    running = state._task_ending_locked(task).result_open
    if purpose == PURPOSE_CANCEL:
        return _SYNC_CANCEL_RUNNING if running else _SYNC_CANCEL_FINISHED
    return _SYNC_HANDLE_RUNNING if running else _SYNC_HANDLE


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
    cap = wait_cap(waiter)
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
            task, reason = _task_for_handle_locked(state, registry, waiter, text, purpose=PURPOSE_AWAIT)
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
        _claim_settled(state, registry, join, record)
        return ""
    holder = registry.reserved.get(child_id, "")
    if holder and holder != join.waiter_id and _holder_alive_locked(state, holder, now):
        return f"另一个任务（{_short_id(holder)}）已经在等这个委派，结果会交给它。"
    registry.reserved[child_id] = join.waiter_id
    join.pending.add(child_id)
    return ""


def _record_holder(registry: AsyncDispatchRegistry, join: Join, record: SettledDispatch) -> str:
    """结果留给了别的任务的 await，或者已经收进别的任务还没交出去的汇合：那个任务的 id。留给某一轮的谁都能收。"""
    if record.held_for and record.held_for != join.waiter_id and record.hold != HOLD_ROUND:
        return record.held_for
    child_id = record.task.task_id
    return next(
        (other.waiter_id for other in registry.joins.values() if other is not join and child_id in other.settled),
        "",
    )


def _claim_settled(
    state: "SupervisorState", registry: AsyncDispatchRegistry, join: Join, record: SettledDispatch,
) -> None:
    """已经结束的委派算进汇合；结果早先交到过别处的，记下是哪里，交回时注明。

    作为新消息交回、接它的那一轮还没被领走的，从那一轮撤下，算作这次才收到。
    """
    child_id = record.task.task_id
    join.pending.discard(child_id)
    join.settled.add(child_id)
    registry.reserved.pop(child_id, None)
    if record.delivered == DELIVERED_INBOUND and _withdraw_from_callback_locked(state, record):
        record.delivered = ""
        record.callback_task_id = ""
    if record.delivered in _DELIVERED_PLACES:
        join.earlier[child_id] = record.delivered
    else:
        record.delivered = DELIVERED_JOIN
    record.held_for = ""
    record.hold = ""
    waiter = state.tasks.get(join.waiter_id)
    if waiter is not None:
        _ensure_refs_locked(state, record, child_retention.archive_session(waiter))


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
        _claim_settled(state, registry, join, record)


def wait_cap(caller: Task) -> datetime:
    """caller 挂起等结果最晚等到什么时候：它自己的总寿命和派它的那一层给的封顶取早的，再留出收尾的余量。"""
    end = _lifetime_end(caller)
    inherited = _inherited_cap(caller)
    if inherited is not None and inherited < end:
        end = inherited
    return end - _LIFETIME_MARGIN


def _inherited_cap(task: Task) -> datetime | None:
    raw = str(task.input.get(ITEM_LIFETIME_CAP_KEY) or "").strip()
    if not raw:
        return None
    cap = parse_timestamp(raw)
    if cap is None:
        # 派它的那一层到点照样会按自己记的截止时间收它，这里只退回按它自己的寿命算
        log.warning("task %s has an unreadable wait cap %r", task.task_id[:12], raw)
    return cap


def item_deadline_inputs(seconds: int, *, cap: datetime, now: datetime | None = None) -> dict[str, Any]:
    """建批次子任务时一并写进 input 的截止字段：写了 timeout_sec 的按它算，调用方的寿命封顶更早时按封顶。

    封顶本身也记下：子任务自己再挂起等结果时，不能等过这个时刻。
    """
    explicit = (now or _now()) + timedelta(seconds=int(seconds)) if seconds else None
    capped = explicit is None or cap < explicit
    due = cap if capped else explicit
    fields: dict[str, Any] = {ITEM_LIFETIME_CAP_KEY: cap.isoformat(), ITEM_DEADLINE_KEY: due.isoformat()}
    if seconds:
        fields[ITEM_TIMEOUT_SEC_KEY] = int(seconds)
    if capped:
        fields[ITEM_LIFETIME_CAPPED_KEY] = True
    return fields


def watch_item_locked(state: "SupervisorState", child: Task) -> None:
    """登记批次子任务的截止时刻（读它 input 里的字段），到点还没结束就按失败收掉。"""
    raw = str(child.input.get(ITEM_DEADLINE_KEY) or "").strip()
    if not raw or not state._task_ending_locked(child).continues:
        return
    due = parse_timestamp(raw)
    if due is None:
        log.warning("dispatch item %s has an unreadable deadline %r", child.task_id[:12], raw)
        return
    registry = registry_locked(state)
    registry.item_deadlines[child.task_id] = due
    _arm_locked(state, registry)


def item_timeout_error(child: Task) -> str:
    """单项到点被停下的原因：调用方寿命封顶的，和它自己写的 timeout_sec 到点的分开说。"""
    if child.input.get(ITEM_LIFETIME_CAPPED_KEY):
        return _LIFETIME_CAPPED_ERROR
    seconds = int(child.input.get(ITEM_TIMEOUT_SEC_KEY) or 0)
    return f"等待超时：{seconds} 秒内没有做完，已被停下。"


def _expire_item_locked(state: "SupervisorState", child_id: str, now: datetime) -> None:
    child = state.tasks.get(child_id)
    if child is None:
        return
    ending = state._task_ending_locked(child, now)
    # 结局已经定了（结束、结局还在路由、旧 worker 还在停手），不用再计时
    if not ending.continues:
        return
    child.input[ITEM_TIMED_OUT_KEY] = True
    log.warning(
        "dispatch item %s (node=%s) of task %s timed out (%s)",
        child.task_id[:12], child.node_id, (child.caller_task_id or "")[:12], ending.value,
    )
    if ending is not TaskEnding.live:
        # 交回的委派还在路由：路由进锁时按超时改判失败，不会再派出子任务
        return
    # 按失败收掉：会话照失败的规矩保留，调用方能用句柄 continue_from 接着做
    state._fail_task_locked(child, error=item_timeout_error(child), now=now)


def watch_transit_locked(state: "SupervisorState", task_id: str, due: datetime) -> None:
    """登记一个任务过渡段的最晚时刻：到点还没等来路由或 worker 回报，由 state._transit_check_due_locked 按结束处理。"""
    registry = registry_locked(state)
    registry.transit_checks[task_id] = due
    _arm_locked(state, registry)


def expire_due_locked(state: "SupervisorState", *, now: datetime | None = None) -> None:
    """到点的汇合带着已有结果恢复调用方，到点的批次项按失败收掉，过渡段到点的任务按结束处理，顺带清理任务已经结束的汇合、保留和批次项截止时间。"""
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
        child = state.tasks.get(child_id)
        # 每项都带寿命封顶，几小时后才到点：已经结束的在这里清掉，定时器不为它们空醒
        if child is None or not state._task_ending_locked(child, now).continues:
            registry.item_deadlines.pop(child_id, None)
            continue
        if due > now:
            continue
        registry.item_deadlines.pop(child_id, None)
        try:
            _expire_item_locked(state, child_id, now)
        except Exception:
            log.exception("timing out dispatch item %s failed", child_id[:12])
    for task_id, due in list(registry.transit_checks.items()):
        if due > now:
            continue
        registry.transit_checks.pop(task_id, None)
        try:
            state._transit_check_due_locked(task_id, now)
        except Exception:
            log.exception("settling task %s at the end of its transit failed", task_id[:12])
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
    dues = [
        join.deadline for join in registry.joins.values()
    ] + list(registry.item_deadlines.values()) + list(registry.transit_checks.values())
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
    ending = state._task_ending_locked(task, now)
    if ending is TaskEnding.dispatching:
        # 交回了 dispatch、正在路由：接下来就是挂起等它派出去的子任务
        status = "waiting"
    elif ending is not TaskEnding.live:
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
    if record.held_for and record.hold == HOLD_ROUND:
        mine = record.held_for == caller.task_id
        item["delivered"] = "held_for_your_round" if mine else "held_for_round"
        item["held_note"] = (
            "结果先留着：你这一轮还没做完，可以用 await_dispatches 收走；没收的等这一轮结束后，和其他留着的结果合成一条消息交回。"
            if mine else
            f"结果先留着，等任务 {_short_id(record.held_for)} 那一轮做完后，和其他留着的结果合成一条消息交回；"
            "也可以用 await_dispatches 收走。"
        )
    elif record.held_for:
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
        and not task.input.get(_WITHDRAWN_KEY)
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
    task, reason = _task_for_handle_locked(state, registry, caller, str(raw_handle or ""), purpose=PURPOSE_CANCEL)
    if task is None:
        if reason == _INVALID_HANDLE:
            return _cancel_refusal("invalid_handle", reason)
        return _cancel_refusal("sync_dispatch" if reason in _SYNC_REASONS else "not_found", reason)
    if task.task_id in _dispatch_chain_ids(state, caller):
        return _cancel_refusal("self", "不能取消自己，也不能取消派出自己的那个任务。")
    now = _now()
    handle = handle_of_locked(state, task)
    already_finished = not state._task_ending_locked(task, now).continues
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
    elif not state._task_ending_locked(task, now).continues:
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
