"""到了步数或运行时长上限收尾的入口任务，在同一会话自动接着做。

一条续跑链有轮数上限，并受任务总寿命约束：链上累计用掉的时间从第一个任务创建时算。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from clonoth_runtime import get_int, load_runtime_config, task_max_running_seconds
from engine.output_guide import OUTPUT_GUIDE_KEY
from engine.run_budget import (
    AUTO_CONTINUE_KEY,
    CHAIN_STARTED_AT_KEY,
    STEP_LIMIT_NOTICE,
    TIME_LIMIT_NOTICE,
    WRAPUP_STEPS,
    WRAPUP_TIME,
    lifetime_limit_seconds,
    lifetime_origin,
    parse_timestamp,
    wrapup_info,
    wrapup_reserve_seconds,
    wrapup_time_ratio,
)
from engine.todo_list import carried_todo_items, render_todo_lines, unfinished_items

from ._helpers import _now
from .task_traits import is_internal_task, node_auto_continues
from .types import Task, TaskKind

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

AUTO_CONTINUE_MESSAGE_TYPE = "auto_continue"
MAX_ROUNDS_KEY = "engine.auto_continue.max_rounds"
DEFAULT_MAX_ROUNDS = 3
_MAX_ROUNDS_CEILING = 20
# 收尾说明完整的一份已经在历史里，续跑那一轮的输入只引用开头
_WRAPUP_EXCERPT_CHARS = 2000
_ID_CHARS = 64
# 续跑任务带过去的路由提示：简短回应的要求不带，续跑要正常干活
_DROPPED_ROUTE_HINTS = frozenset({"response_action"})

_NOTE_GOING = "（这一段到了{limit}上限，接下来自动接着做：第 {round}/{rounds} 轮）"
_NOTE_ROUNDS_USED_UP = "（已经自动接着做了 {rounds} 轮，先停在这里。需要的话说「继续」接着做。）"
_NOTE_LIFETIME_USED_UP = "（这项任务累计运行的时间快到上限了，先停在这里。需要的话说「继续」接着做。）"


@dataclass(frozen=True)
class ContinuePlan:
    """收尾之后怎么办：go 为真就起续跑任务；note 补在这次交付给用户的文本末尾。"""

    go: bool
    note: str
    round: int = 0
    max_rounds: int = 0
    reason: str = WRAPUP_STEPS
    chain_started_at: datetime | None = None
    origin_task_id: str = ""
    todos: list[dict[str, str]] = field(default_factory=list)
    wrapup_text: str = ""


def max_rounds(runtime_cfg: Any) -> int:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return get_int(cfg, MAX_ROUNDS_KEY, DEFAULT_MAX_ROUNDS, min_value=0, max_value=_MAX_ROUNDS_CEILING)


def chain_input(raw: Any) -> dict[str, Any] | None:
    """续跑任务 input 里的续跑链记录：只收内部生成的那几项，格式不对整份不要。"""
    if not isinstance(raw, dict):
        return None
    try:
        round_no = int(raw.get("round") or 0)
        rounds = int(raw.get("max_rounds") or 0)
    except (TypeError, ValueError):
        return None
    started = parse_timestamp(raw.get(CHAIN_STARTED_AT_KEY))
    if round_no <= 0 or started is None:
        return None
    return {
        "round": round_no,
        "max_rounds": max(round_no, rounds),
        CHAIN_STARTED_AT_KEY: started.isoformat(),
        "origin_task_id": str(raw.get("origin_task_id") or "")[:_ID_CHARS],
        "previous_task_id": str(raw.get("previous_task_id") or "")[:_ID_CHARS],
        "reason": WRAPUP_TIME if raw.get("reason") == WRAPUP_TIME else WRAPUP_STEPS,
        "todos": carried_todo_items(raw.get("todos")),
    }


def _eligible(task: Task) -> bool:
    """用户会话里直接跑的节点任务才自己续跑；委派出去的子任务把收尾标记和句柄交还父节点，由它决定。"""
    if task.kind != TaskKind.node or not task.node_id:
        return False
    if (task.caller_task_id or "").strip() or task.input.get("_dispatch_origin"):
        return False
    return not is_internal_task(task)


def plan_locked(
    state: "SupervisorState", task: Task, result: Any, action_type: str,
) -> ContinuePlan | None:
    """入口任务收尾交付时决定要不要接着做：有没做完的待办或节点声明了 auto_continue 才续；不用管时给 None。"""
    if action_type != "finish" or not _eligible(task):
        return None
    info = wrapup_info(result)
    if info is None:
        return None
    cfg = load_runtime_config(state.workspace_root)
    rounds = max_rounds(cfg)
    if rounds <= 0:
        return None
    todos = carried_todo_items(info.get("todos"))
    if not unfinished_items(todos) and not node_auto_continues(state.workspace_root, str(task.node_id)):
        return None
    route_session_id = state._route_session_id_for_task_locked(task)
    if route_session_id not in state.sessions or route_session_id in state._cancelled_sessions:
        return None
    current = state._current_session_generation_locked(route_session_id)
    if current and task.session_generation != current:
        return None
    previous = chain_input(task.input.get(AUTO_CONTINUE_KEY))
    round_no = (previous["round"] if previous else 0) + 1
    if round_no > rounds:
        return ContinuePlan(go=False, note=_NOTE_ROUNDS_USED_UP.format(rounds=rounds))
    origin = lifetime_origin(task.created_at, task.input)
    max_running = task_max_running_seconds()
    life_end = origin + timedelta(seconds=lifetime_limit_seconds(max_running))
    # 剩下的时间不够一段跑出点东西再收尾，就不续了
    if (life_end - _now()).total_seconds() <= 2 * wrapup_reserve_seconds(max_running, wrapup_time_ratio(cfg)):
        return ContinuePlan(go=False, note=_NOTE_LIFETIME_USED_UP)
    reason = str(info.get("reason") or WRAPUP_STEPS)
    return ContinuePlan(
        go=True,
        note=_NOTE_GOING.format(limit=_limit_label(reason), round=round_no, rounds=rounds),
        round=round_no,
        max_rounds=rounds,
        reason=reason,
        chain_started_at=origin,
        origin_task_id=(previous or {}).get("origin_task_id") or task.task_id,
        todos=todos,
        wrapup_text=_without_stop_notice(str((result or {}).get("text") or (result or {}).get("summary") or "")),
    )


def _limit_label(reason: str) -> str:
    return "运行时长" if reason == WRAPUP_TIME else "步数"


def _without_stop_notice(text: str) -> str:
    """去掉 engine 收尾时补在末尾的「任务到此停止」。"""
    base = text.strip()
    for notice in (STEP_LIMIT_NOTICE, TIME_LIMIT_NOTICE):
        if base.endswith(notice):
            return base[: -len(notice)].rstrip()
    return base


def with_note(result: Any, note: str) -> dict[str, Any]:
    """交付给用户的那份结果：正文末尾的「任务到此停止」换成续跑说明，原结果不动。"""
    output = dict(result) if isinstance(result, dict) else {}
    if not note:
        return output
    base = _without_stop_notice(str(output.get("text") or output.get("summary") or ""))
    output["text"] = f"{base}\n\n{note}" if base else note
    return output


def continuation_text(plan: ContinuePlan) -> str:
    """续跑任务当轮的输入：交代为什么接着做、上一段做到哪、清单上还有什么。"""
    lines = [
        f"[自动续跑 第 {plan.round}/{plan.max_rounds} 轮] 上一段到了{_limit_label(plan.reason)}上限，"
        "先停了下来，现在接着做同一个任务。",
    ]
    excerpt = plan.wrapup_text
    if excerpt:
        if len(excerpt) > _WRAPUP_EXCERPT_CHARS:
            excerpt = excerpt[:_WRAPUP_EXCERPT_CHARS] + "…（完整的收尾说明见上一条回复）"
        lines += ["上一段最后交代的进度：", excerpt]
    if plan.todos:
        lines += ["待办清单：", *render_todo_lines(plan.todos)]
    lines.append(
        "先按上面的进度和待办核对实际做到了哪里，再接着做没完成的部分，做的过程中用 update_todos 更新清单。"
        "如果其实已经全部做完，直接用 finish 说明结果。"
    )
    return "\n".join(lines)


def _chain_record(task: Task, plan: ContinuePlan) -> dict[str, Any]:
    started = plan.chain_started_at or lifetime_origin(task.created_at, task.input)
    return {
        "round": plan.round,
        "max_rounds": plan.max_rounds,
        CHAIN_STARTED_AT_KEY: started.isoformat(),
        "origin_task_id": str(plan.origin_task_id or task.task_id)[:_ID_CHARS],
        "previous_task_id": task.task_id,
        "reason": plan.reason,
        "todos": [dict(item) for item in plan.todos],
    }


def start_locked(state: "SupervisorState", task: Task, plan: ContinuePlan) -> Task | None:
    """在同一会话投一条续跑消息、起续跑任务。走入站那条路：分支、身份、输出说明、字/token 比都照常带上。"""
    if not plan.go:
        return None
    route_session_id = state._route_session_id_for_task_locked(task)
    info = state.sessions.get(route_session_id)
    if info is None:
        return None
    delivery_id = f"{task.delivery_id}:auto-continue" if task.delivery_id else ""
    if delivery_id and state.eventlog.find_delivery_event(delivery_id, event_type="inbound_message"):
        return None
    context = task.input.get("task_context")
    context = context if isinstance(context, dict) else {}
    hints = context.get("route_hints") if isinstance(context.get("route_hints"), dict) else {}
    payload: dict[str, Any] = {
        "channel": info.channel,
        "conversation_key": info.conversation_key,
        "message_id": f"auto_continue:{task.task_id}",
        "text": continuation_text(plan),
        "message_type": AUTO_CONTINUE_MESSAGE_TYPE,
        "entry_node_id": str(task.node_id or ""),
        "entry_node_pinned": True,
        "use_context": True,
        "platform_auth": dict(context.get("platform_auth") or {}) if isinstance(context.get("platform_auth"), dict) else {},
        "route_hints": {key: value for key, value in hints.items() if key not in _DROPPED_ROUTE_HINTS},
        "memory_hints": dict(context.get("memory_hints") or {}) if isinstance(context.get("memory_hints"), dict) else {},
        "auto_continue": _chain_record(task, plan),
    }
    guide = context.get(OUTPUT_GUIDE_KEY)
    if isinstance(guide, dict) and guide:
        payload[OUTPUT_GUIDE_KEY] = dict(guide)
    schedule_id = str(task.input.get("schedule_id") or "").strip()
    if schedule_id:
        # 定时任务的续跑仍算定时任务：适配器不会把它当成用户消息起的任务去抢占
        payload["schedule_id"] = schedule_id
    if delivery_id:
        payload["delivery_id"] = delivery_id
    event = state.eventlog.append(
        session_id=route_session_id,
        component="supervisor",
        type_="inbound_message",
        payload=payload,
    )
    seq = int(event.get("seq", 0) or 0)
    state._apply_inbound_message(seq=seq, session_id=route_session_id, payload=payload)
    created = state._create_entry_task_for_inbound_locked(
        inbound_seq=seq, session_id=route_session_id, payload=payload,
    )
    if created is not None:
        log.info(
            "auto-continue round %d/%d for task %s started as %s (node=%s)",
            plan.round, plan.max_rounds, task.task_id[:12], created.task_id[:12], task.node_id,
        )
    return created
