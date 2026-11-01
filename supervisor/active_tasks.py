"""新消息进来时这段对话里还在跑的任务：入站时按发消息的人算好写进入口任务，engine 并进那一轮的动态区。

带 _locked 的函数要求调用方持有 state._lock。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from engine.concurrent_tasks import ACTIVE_TASKS_INPUT_KEY, ACTIVE_TASKS_OMITTED_INPUT_KEY
from engine.conversation_routing import routing_meta

from ._helpers import _now
from .task_owners import in_group, may_touch, same_requester
from .task_traits import AMBIENT_PURPOSE, is_ambient_task, is_internal_task
from .types import Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .state import SupervisorState

# 适配器把用户原话和上下文拼成 text 时另给的一份，入站存进入口任务，预览优先用它
REQUEST_TEXT_KEY = "request_text"
# 只拿来预览，存一段就够；入站本身不因为它太长被拒
REQUEST_TEXT_MAX_CHARS = 500
PREVIEW_CHARS = 120
MAX_LISTED_TASKS = 12
_DETAIL_CHARS = 80
_LIVE_STATUSES = frozenset({TaskStatus.pending, TaskStatus.running, TaskStatus.suspended})
# 不是用户发来的那几种轮次：正文是交回的结果或续跑说明，不标出来会被当成用户的要求
_ROUND_LABELS = {"dispatch_result": "交回的委派结果", "auto_continue": "自动续跑"}


def request_text_of(payload: Mapping[str, Any]) -> str:
    """入站另带的这一轮用户原话，裁到上限；没带是空串。"""
    value = payload.get(REQUEST_TEXT_KEY)
    return value.strip()[:REQUEST_TEXT_MAX_CHARS] if isinstance(value, str) else ""


def request_preview(task_input: Mapping[str, Any], limit: int = PREVIEW_CHARS) -> str:
    """一个任务在办什么：有用户原话用原话，没有用指令；空白折成一个空格，超长截断。交回结果、自动续跑的那几轮标出来。"""
    raw = " ".join(str(task_input.get(REQUEST_TEXT_KEY) or task_input.get("instruction") or "").split())
    label = _ROUND_LABELS.get(str(task_input.get("inbound_message_type") or "").strip(), "")
    text = f"{label}：{raw}" if label and raw else (label or raw)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def wants_summary(payload: Mapping[str, Any]) -> bool:
    """用户自己发来的一轮才要：系统流程、委派、交回的结果、续跑、定时任务和群里没人叫 bot 的插话都不要。"""
    if payload.get("_system_task") or payload.get("dispatch_origin") or payload.get("schedule_id"):
        return False
    if str(payload.get("message_type") or "").strip():
        return False
    hints = payload.get("route_hints")
    return routing_meta({"route_hints": hints} if isinstance(hints, dict) else {}).get("delivery_purpose") != AMBIENT_PURPOSE


def summary_input_locked(
    state: "SupervisorState", session_id: str, payload: Mapping[str, Any], viewer_auth: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """入口任务 input 里要带的摘要两项；这一轮不要、或者没有在跑的任务时是空字典。"""
    if not wants_summary(payload):
        return {}
    listed, omitted = summary_locked(state, session_id, viewer_auth)
    if not listed:
        return {}
    return {ACTIVE_TASKS_INPUT_KEY: listed, **({ACTIVE_TASKS_OMITTED_INPUT_KEY: omitted} if omitted else {})}


def summary_locked(
    state: "SupervisorState", session_id: str, viewer_auth: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], int]:
    """session_id 所在会话家族里还没结束、替用户干活的节点任务，返回 (列出的, 没列出的个数)。

    派出去的子任务和异步委派都算。群聊里普通成员只看得到自己发起的；管理员和控制台看得到全部，
    群里别人发起的标 mine=False。列不下时先留各轮自己（上级不在列表里的），再按先后补子任务，列出来的最早的在前。
    """
    root = state._session_family_root_locked(session_id)
    if not root:
        return [], 0
    owners: dict[str, dict[str, Any]] = {}
    visible: list[Task] = []
    for task in state.tasks.values():
        if task.kind != TaskKind.node or task.status not in _LIVE_STATUSES:
            continue
        if is_internal_task(task) or is_ambient_task(task) or state._task_family_root_locked(task) != root:
            continue
        owner = state._platform_auth_origin_locked(task)[1]
        if not may_touch(viewer_auth, owner):
            continue
        owners[task.task_id] = owner
        visible.append(task)
    ids = set(owners)
    chosen = sorted(visible, key=lambda task: (state._origin_parent_task_id(task) in ids, task.created_at))[:MAX_LISTED_TASKS]
    chosen.sort(key=lambda task: task.created_at)
    now = _now()
    grouped = in_group(viewer_auth)
    items: list[dict[str, Any]] = []
    for task in chosen:
        item: dict[str, Any] = {
            "task_id": task.task_id,
            "node_id": str(task.node_id or ""),
            "status": task.status.value,
            "steerable": state._family_preemptable(task),
            "age_sec": max(0, int((now - task.created_at).total_seconds())),
        }
        preview = request_preview(task.input)
        if preview:
            item["instruction"] = preview
        # 挂起时留下的是挂起前最后一步的活动，不算它现在在做的
        if task.status == TaskStatus.running and task.current_phase:
            item["phase"] = task.current_phase
            if task.current_detail:
                item["detail"] = task.current_detail[:_DETAIL_CHARS]
        parent = state._origin_parent_task_id(task)
        if parent in ids:
            item["parent_task_id"] = parent
        if grouped:
            item["mine"] = same_requester(viewer_auth, owners[task.task_id])
        items.append(item)
    return items, len(visible) - len(chosen)
