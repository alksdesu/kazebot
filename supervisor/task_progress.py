"""按发起人查询任务进度，记录发送回执并核对查询版本。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from clonoth_sdk.task_status import MAX_DELIVERY_RECORDS, MAX_STATUS_TASKS

from ._helpers import _now
from .active_tasks import request_preview
from .task_traits import is_ambient_task, is_internal_task
from .task_owners import same_requester
from .types import Task, TaskKind, TaskStatus

QUERY_KEY = "_task_status_queries"
MAX_QUERIES = 4
MAX_SHOWN_DELIVERIES = 6


def visible(state: Any, caller: Task, task: Task) -> bool:
    root = state._task_family_root_locked(caller)
    return (
        bool(root) and task.task_id != caller.task_id and task.kind == TaskKind.node
        and not is_internal_task(task) and not is_ambient_task(task)
        and root == state._task_family_root_locked(task)
        and state._caller_may_touch_locked(caller, task)
    )


def activity(task: Task, event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "model_request_started":
        task.current_phase, task.current_detail = "model_request", ""
    elif event_type == "model_request_ended":
        task.current_phase, task.current_detail = "", ""
    elif event_type == "tool_call_end":
        task.last_tool = {
            "name": str(payload.get("tool_name") or "")[:80],
            "status": str(payload.get("status") or "unknown")[:40],
            "completed_at": _now().isoformat(),
        }
    call_id = str(payload.get("tool_call_id") or payload.get("tool_name") or "")[:128]
    if event_type == "tool_call_start" and call_id:
        task.active_tool_calls[call_id] = str(payload.get("tool_name") or "")[:80]
    elif event_type == "tool_call_end":
        task.active_tool_calls.pop(call_id, None)
    if task.active_tool_calls:
        task.current_phase = "tool_call"
        task.current_detail = ", ".join(task.active_tool_calls.values())[:80]


def record_delivery_locked(state: Any, task: Task, receipt: dict[str, Any]) -> None:
    key = receipt["receipt_id"]
    previous = task.delivery_receipts.get(key)
    # 已确认的发送不会被迟到的 pending 或失败通知抹掉。
    if previous and previous["status"] == "sent":
        return
    if previous and all(previous.get(k) == v for k, v in receipt.items()):
        return
    row = {**receipt, "observed_at": _now().isoformat()}
    state.eventlog.append(
        session_id=state._route_session_id_for_task_locked(task), component="adapter",
        type_="task_delivery_receipt", payload={"task_id": task.task_id, **row},
    )
    task.delivery_receipts[key] = row
    while len(task.delivery_receipts) > MAX_DELIVERY_RECORDS:
        del task.delivery_receipts[next(iter(task.delivery_receipts))]
        task.delivery_records_omitted = True


def item_of(state: Any, task: Task, now: Any) -> dict[str, Any]:
    ending = state._task_ending_locked(task, now)
    status = task.status.value
    if status in ("completed", "failed", "cancelled") and ending.continues:
        status = "finishing"
    owner = state._platform_auth_origin_locked(task)[1]
    requester_scope = hashlib.sha256(f"qq:private:{owner.get('user_id', '')}".encode()).hexdigest()
    delivery = [{**row, "to_requester": owner.get("platform") == "qq" and bool(owner.get("user_id")) and row["destination_scope"] == requester_scope}
                for row in task.delivery_receipts.values()]
    counts = {kind: {status: 0 for status in ("pending", "sent", "failed", "unknown")} for kind in ("file", "message")}
    for row in delivery:
        counts[row["kind"]][row["status"]] += row["count"]
    return {
        "task_id": task.task_id, "node_id": task.node_id or "", "status": status,
        "instruction": request_preview(task.input),
        "phase": task.current_phase if task.status == TaskStatus.running else "",
        "detail": task.current_detail[:80] if task.status == TaskStatus.running else "",
        "age_sec": max(0, int((now - task.created_at).total_seconds())),
        "steerable": state._family_preemptable(task),
        "parent_task_id": state._origin_parent_task_id(task),
        "last_tool": dict(task.last_tool),
        "delivery": {
            "records": delivery[-MAX_SHOWN_DELIVERIES:], "counts": counts, "unknown": not delivery,
            "omitted": task.delivery_records_omitted or len(delivery) > MAX_SHOWN_DELIVERIES,
        },
    }


def collect_locked(state: Any, caller: Task, ids: list[str] | None) -> tuple[list[dict[str, Any]], int]:
    now = _now()
    candidates = [task for task in state.tasks.values() if visible(state, caller, task)]
    viewer = state._platform_auth_origin_locked(caller)[1]
    def item(task: Task) -> dict[str, Any]:
        return {**item_of(state, task, now), "mine": same_requester(viewer, state._platform_auth_origin_locked(task)[1])}
    if ids:
        by_id = {task.task_id: task for task in candidates}
        return [item(by_id[tid]) if tid in by_id else {
            "task_id": tid, "status": "unavailable",
        } for tid in ids], 0
    candidates = [t for t in candidates if not state._task_terminal(t) or t.updated_at >= now - timedelta(hours=24)]
    candidates.sort(key=lambda t: (state._task_terminal(t), -t.created_at.timestamp()))
    return [item(t) for t in candidates[:MAX_STATUS_TASKS]], max(0, len(candidates) - MAX_STATUS_TASKS)


def version_of(items: list[dict[str, Any]]) -> str:
    stable = [{k: v for k, v in item.items() if k != "age_sec"} for item in items]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def snapshot_locked(state: Any, caller: Task, ids: list[str] | None) -> dict[str, Any]:
    items, omitted = collect_locked(state, caller, ids)
    result = {
        "query_id": uuid.uuid4().hex, "observed_at": _now().isoformat(),
        "version": version_of(items), "tasks": items, "omitted": omitted,
    }
    queries = caller.input.setdefault(QUERY_KEY, {})
    queries[result["query_id"]] = {"snapshot": result, "ids": ids}
    while len(queries) > MAX_QUERIES:
        del queries[next(iter(queries))]
    return result


def check_locked(state: Any, caller: Task, query_id: str) -> dict[str, Any] | None:
    saved = caller.input.get(QUERY_KEY, {}).get(query_id)
    if not isinstance(saved, dict):
        return None
    items, _ = collect_locked(state, caller, saved["ids"])
    stale = version_of(items) != saved["snapshot"]["version"]
    return {"changed": stale, "snapshot": snapshot_locked(state, caller, saved["ids"]) if stale else saved["snapshot"]}
