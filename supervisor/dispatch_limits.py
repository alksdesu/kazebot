"""委派上限：从用户那一轮往下的层数，一次用户请求派生的委派里同时没结束的个数。

建任务时 supervisor 把层数和所属的任务树写进 input，派发前按上限核对；超出的那一次当场拒，原因交回给模型。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clonoth_runtime import load_runtime_config
from engine.dispatch_batch import INBOUND_BAD_MODE_STATUS, INBOUND_CONFLICT_STATUS, DispatchLimits
from engine.dispatch_results import INBOUND_CONTINUES_TASK_KEY
from engine.run_budget import AUTO_CONTINUE_KEY

from .session_reuse import InboundRefusal
from .task_traits import is_internal_task
from .types import Task, TaskKind

if TYPE_CHECKING:
    from .state import SupervisorState

DISPATCH_DEPTH_KEY = "_dispatch_depth"
DISPATCH_TREE_KEY = "_dispatch_tree"
# 回调入站的 input 记着产出结果的任务；这次回调接着做的那一轮记在 INBOUND_CONTINUES_TASK_KEY
CALLBACK_PRODUCER_KEY = "inbound_child_task_id"

_CHILD = "child"
_CALLBACK = "callback"
_CONTINUATION = "continuation"
_ROOT = "root"
# 发起链异常地长或绕成环时停下
_MAX_LINEAGE_HOPS = 64


@dataclass(frozen=True)
class TreePlace:
    """任务在任务树里的位置：第几层（用户那一轮是 0），属于哪棵树（树根任务的 id）。"""

    depth: int
    tree: str


@dataclass(frozen=True)
class Lineage:
    """新任务接着谁：子任务比来源深一层，回调落在接着做的那一轮那一层，续跑和上一段同层，root 是一棵新树。"""

    kind: str
    source_id: str = ""
    continues_id: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def lineage_of(task_input: Mapping[str, Any], caller_task_id: str | None) -> Lineage:
    """按 input 认出新任务的来源。带着产出结果的任务就是回调，哪怕它也带着原来的 dispatch_origin。"""
    producer = _text(task_input.get(CALLBACK_PRODUCER_KEY))
    if producer:
        return Lineage(_CALLBACK, producer, _text(task_input.get(INBOUND_CONTINUES_TASK_KEY)))
    caller = _text(caller_task_id)
    if caller:
        return Lineage(_CHILD, caller)
    origin = task_input.get("_dispatch_origin")
    if isinstance(origin, dict) and origin:
        return Lineage(_CHILD, _text(origin.get("caller_task_id")))
    chain = task_input.get(AUTO_CONTINUE_KEY)
    previous = _text(chain.get("previous_task_id")) if isinstance(chain, dict) else ""
    if previous:
        return Lineage(_CONTINUATION, previous)
    return Lineage(_ROOT)


def _stored_place(task: Task) -> TreePlace | None:
    task_input = task.input if isinstance(task.input, dict) else {}
    depth = task_input.get(DISPATCH_DEPTH_KEY)
    tree = task_input.get(DISPATCH_TREE_KEY)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        return None
    if not isinstance(tree, str) or not tree.strip():
        return None
    return TreePlace(depth, tree.strip())


def _route_session_locked(state: "SupervisorState", task: Task) -> str:
    return _text(state._route_session_id_for_task_locked(task)) or _text(task.session_id)


def task_place_locked(state: "SupervisorState", task: Task) -> TreePlace:
    """任务的层数和任务树：建任务时记下的那份；没有记的旧任务按同样的规则沿链补算。"""
    return _task_place_locked(state, task, hops=0)


def _task_place_locked(state: "SupervisorState", task: Task, *, hops: int) -> TreePlace:
    stored = _stored_place(task)
    if stored is not None:
        return stored
    if hops >= _MAX_LINEAGE_HOPS:
        return TreePlace(0, task.task_id)
    task_input = task.input if isinstance(task.input, dict) else {}
    return _place_locked(
        state,
        lineage_of(task_input, task.caller_task_id),
        own_id=task.task_id,
        landing=_route_session_locked(state, task),
        hops=hops + 1,
    )


def _place_locked(
    state: "SupervisorState", lineage: Lineage, *, own_id: str, landing: str, hops: int,
) -> TreePlace:
    if lineage.kind == _ROOT:
        return TreePlace(0, own_id)
    source = state.tasks.get(lineage.source_id) if lineage.source_id else None
    if lineage.kind == _CHILD:
        if source is None:
            # 来源已不在：它当作一棵新树的根，同一来源派出的几个仍算在一起
            return TreePlace(1, lineage.source_id or own_id)
        parent = _task_place_locked(state, source, hops=hops)
        return TreePlace(parent.depth + 1, parent.tree)
    if lineage.kind == _CONTINUATION:
        if source is None:
            return TreePlace(0, lineage.source_id)
        return _task_place_locked(state, source, hops=hops)
    return _callback_place_locked(state, lineage, source, own_id=own_id, landing=landing, hops=hops)


def _callback_place_locked(
    state: "SupervisorState",
    lineage: Lineage,
    producer: Task | None,
    *,
    own_id: str,
    landing: str,
    hops: int,
) -> TreePlace:
    """回调落在接着做的那一轮那一层；那一轮认不出时，在产出结果的任务往上找落点，再不行按它往上一层算。"""
    continues = state.tasks.get(lineage.continues_id) if lineage.continues_id else None
    if continues is not None:
        return _task_place_locked(state, continues, hops=hops)
    if producer is None:
        return TreePlace(0, lineage.source_id or own_id)
    produced = _task_place_locked(state, producer, hops=hops)
    home = _landing_chain_top_locked(state, producer, landing)
    if home is not None:
        return TreePlace(_task_place_locked(state, home, hops=hops).depth, produced.tree)
    return TreePlace(max(produced.depth - 1, 0), produced.tree)


def _landing_chain_top_locked(state: "SupervisorState", producer: Task, landing: str) -> Task | None:
    """从产出结果的任务往上，找路由会话就是回调落点的那个同步链顶任务。

    同步子任务的路由会话就是用户会话，只能拿每条同步链最上面、没有 caller 的那个任务比对；链顶不是落点就沿它的发起任务接着找。
    """
    if not landing:
        return None
    current = producer
    seen: set[str] = set()
    for _ in range(_MAX_LINEAGE_HOPS):
        if current.task_id in seen:
            return None
        seen.add(current.task_id)
        caller_id = _text(current.caller_task_id)
        if caller_id:
            caller = state.tasks.get(caller_id)
            if caller is None:
                return None
            current = caller
            continue
        if _route_session_locked(state, current) == landing:
            return current
        origin = current.input.get("_dispatch_origin") if isinstance(current.input, dict) else None
        dispatcher_id = _text(origin.get("caller_task_id")) if isinstance(origin, dict) else ""
        dispatcher = state.tasks.get(dispatcher_id) if dispatcher_id else None
        if dispatcher is None:
            return None
        current = dispatcher
    return None


def stamp_new_task_locked(
    state: "SupervisorState",
    task_input: dict[str, Any],
    *,
    task_id: str,
    caller_task_id: str | None,
    session_id: str,
) -> TreePlace:
    """建任务前把层数和任务树写进 input。每次都按来源重算、覆盖原有的值：这两项只认 supervisor 算出来的。"""
    landing = _text(task_input.get("parent_session_id")) or _text(session_id)
    place = _place_locked(state, lineage_of(task_input, caller_task_id), own_id=task_id, landing=landing, hops=0)
    task_input[DISPATCH_DEPTH_KEY] = place.depth
    task_input[DISPATCH_TREE_KEY] = place.tree
    return place


def _counts_as_live_delegation(state: "SupervisorState", task: Task) -> bool:
    # 交回的委派还在路由、随后转成挂起的也还没结束
    if not state._task_ending_locked(task).continues:
        return False
    if task.kind != TaskKind.node or not _text(task.node_id) or _text(task.node_id).startswith("system."):
        return False
    if is_internal_task(task):
        return False
    if _text(task.caller_task_id):
        return True
    origin = task.input.get("_dispatch_origin") if isinstance(task.input, dict) else None
    return isinstance(origin, dict) and bool(origin)


def live_delegations_locked(state: "SupervisorState", tree: str) -> int:
    """这棵任务树里还没结束的委派：挂起等结果的和派发后正转挂起的都算，工具任务和系统任务不算。"""
    if not tree:
        return 0
    return sum(
        1 for task in state.tasks.values()
        if _counts_as_live_delegation(state, task) and task_place_locked(state, task).tree == tree
    )


def depth_refusal(target: str, depth: int, max_depth: int) -> str:
    return (
        f"没有派出：{target or '目标节点'} 会排在委派链第 {depth} 层，超过了上限 {max_depth} 层（用户那一轮算第 0 层）。"
        "这件事自己做完，或者把需要的结果交回给派你出来的一方，由它决定要不要另派。"
    )


def live_refusal(live: int, max_live: int) -> str:
    return (
        f"没有派出：这次用户请求派生的委派里，同时没结束的已有 {live} 个（挂起等结果的也算），上限是 {max_live} 个。"
        "先等已派出的结束（可以用 await_dispatches 收结果），或者自己做。"
    )


@dataclass
class DispatchBudget:
    """一个 wait 批次核对上限用的账：子任务排在第几层，这棵树里已有几个没结束的委派，本批又放行了几个。"""

    limits: DispatchLimits
    child_depth: int
    live: int
    admitted: int = 0

    def refusal(self, target: str) -> str:
        """这一项不能派时给出原因，能派给空串。先看层数（同一批的节点项层数都一样），再看个数。"""
        if self.limits.depth_exceeded(self.child_depth):
            return depth_refusal(target, self.child_depth, self.limits.max_depth)
        if self.limits.live_full(self.live + self.admitted):
            return live_refusal(self.live + self.admitted, self.limits.max_live)
        return ""

    def admit(self) -> None:
        self.admitted += 1


def batch_budget_locked(state: "SupervisorState", caller: Task) -> DispatchBudget:
    """调用方这次 wait 批次的上限账。没结束的委派每批只数一次，调用方自己正转挂起，也算在里面。"""
    limits = DispatchLimits.from_config(load_runtime_config(state.workspace_root))
    place = task_place_locked(state, caller)
    live = live_delegations_locked(state, place.tree) if limits.max_live > 0 else 0
    return DispatchBudget(limits=limits, child_depth=place.depth + 1, live=live)


def inbound_refusal_locked(state: "SupervisorState", payload: Mapping[str, Any]) -> InboundRefusal | None:
    """异步委派的入站超出上限时拒收；不是委派、或两项上限都不限时一律收。认不出发起任务就核对不了，也不收。"""
    origin = payload.get("dispatch_origin")
    if not isinstance(origin, dict) or not origin:
        return None
    limits = DispatchLimits.from_config(load_runtime_config(state.workspace_root))
    if limits.max_depth <= 0 and limits.max_live <= 0:
        return None
    dispatcher_id = _text(origin.get("caller_task_id"))
    if not dispatcher_id:
        return InboundRefusal(
            INBOUND_BAD_MODE_STATUS,
            "没有派出：dispatch_origin 缺少 caller_task_id，认不出是哪个任务发起的委派，核对不了委派上限。",
        )
    dispatcher = state.tasks.get(dispatcher_id)
    if dispatcher is None:
        return InboundRefusal(
            INBOUND_CONFLICT_STATUS,
            f"没有派出：找不到发起这次委派的任务 {dispatcher_id[:12]}，核对不了委派上限。",
        )
    target = _text(payload.get("entry_node_id"))
    place = task_place_locked(state, dispatcher)
    if limits.depth_exceeded(place.depth + 1):
        return InboundRefusal(INBOUND_CONFLICT_STATUS, depth_refusal(target, place.depth + 1, limits.max_depth))
    if limits.max_live > 0:
        live = live_delegations_locked(state, place.tree)
        if limits.live_full(live):
            return InboundRefusal(INBOUND_CONFLICT_STATUS, live_refusal(live, limits.max_live))
    return None
