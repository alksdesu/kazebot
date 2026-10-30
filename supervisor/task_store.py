"""Task 生命周期 mixin —— 创建、领取、完成、取消。"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from clonoth_runtime import default_entry_node_id, load_runtime_config, main_session_enabled, parse_bool
from engine.dispatch_batch import AWAITING_INPUT_KEY, CONTINUE_FROM_KEY
from engine.dispatch_handle import parse_handle
from engine.dispatch_results import (
    CONTINUES_TASK_PAYLOAD_KEY,
    DELIVERY_NOTE_PAYLOAD_KEY,
    DISPATCH_RESULTS_PAYLOAD_KEY,
    INBOUND_CONTINUES_TASK_KEY,
    INBOUND_DELIVERY_NOTE_KEY,
    INBOUND_RESULTS_KEY,
    RESULT_FALLBACK_KEY,
)
from engine.node import known_node_ids
from engine.output_guide import OUTPUT_GUIDE_KEY
from engine.project_root import INBOUND_CHILD_WORKTREE_KEY
from engine.run_budget import AUTO_CONTINUE_KEY, STEPS_USED_KEY
from engine.tool_outputs import bounded_for_event
from platforms.registry import find_platform

from . import child_retention, dispatch_fork, stopped_batch
from ._helpers import _now
from .auto_continue import chain_input
from .context_calibration import calibrated_chars_per_token
from .dispatch_limits import DISPATCH_DEPTH_KEY, DISPATCH_TREE_KEY, stamp_new_task_locked
from .task_ending import TaskEnding, clear_stopping, mark_stopping, task_ending
from .task_lanes import CLAIM_ORDER, LaneLimits, TaskLane, derive_lane, parse_lane
from .task_traits import REQUEUE_MARK
from .types import (
    Approval,
    ApprovalStatus,
    OutputGuide,
    RouteStatus,
    Task,
    TaskKind,
    TaskStatus,
    output_guide_payload,
)
from .worktrees import service_of as worktree_service_of
from .worktrees.keys import (
    CALLER_WORKTREE_KEY,
    CHILD_WORKTREE_PAYLOAD_KEY,
    SNAPSHOT_INPUT_KEYS as WORKTREE_SNAPSHOT_INPUT_KEYS,
    WORKTREE_FROM_KEY,
    WORKTREE_PENDING_KEY,
)


logger = logging.getLogger(__name__)

# 恢复运行时会从 input 里拿掉的键；task_resumed 快照只存增量，回放要靠这份清单把它们删掉。
_RESUME_REMOVABLE_INPUT_KEYS = (
    "_compact_dispatch_pending", "_compact_plan", "_compact_failures", AWAITING_INPUT_KEY,
)
# 异步委派结果回调入站带的子任务结局，只认这几种
_INBOUND_CHILD_STATUSES = frozenset({"finish", "ask", "fail"})
# 任务进入终态时发的事件：失败也发 task_completed
_TASK_END_EVENT_TYPES = frozenset({"task_completed", "task_cancelled"})


def _inbound_result_fields(source: dict[str, Any]) -> dict[str, Any]:
    """入站里一条结果的字段换成任务 input 的 inbound_* 键：单条交回读入站顶层，合并交回逐条读。"""
    fields: dict[str, Any] = {}
    summary = str(source.get("summary") or "").strip()
    if summary:
        fields["inbound_summary"] = summary
    child_session_id = str(source.get("child_session_id") or "").strip()
    if child_session_id:
        fields["inbound_child_session_id"] = child_session_id
    child_task_id = str(source.get("child_task_id") or source.get("task_id") or "").strip()
    if child_task_id:
        fields["inbound_child_task_id"] = child_task_id
    child_node_id = str(source.get("child_node_id") or source.get("node_id") or "").strip()
    if child_node_id:
        fields["inbound_child_node_id"] = child_node_id
    caller_node_id = str(source.get("caller_node_id") or "").strip()
    if caller_node_id:
        fields["inbound_caller_node_id"] = caller_node_id
    if bool(source.get("attachments_outbound_sent")):
        fields["inbound_attachments_outbound_sent"] = True
    child_status = str(source.get("child_status") or "").strip()
    if child_status in _INBOUND_CHILD_STATUSES:
        fields["inbound_child_status"] = child_status
    child_handle = str(source.get("child_handle") or "").strip()
    if child_handle:
        fields["inbound_child_handle"] = child_handle
        if source.get("child_continuable") is True:
            fields["inbound_child_continuable"] = True
        child_wrapup = str(source.get("child_wrapup") or "").strip()
        if child_wrapup:
            fields["inbound_child_wrapup"] = child_wrapup
    child_worktree = source.get(CHILD_WORKTREE_PAYLOAD_KEY)
    if isinstance(child_worktree, dict) and child_worktree.get("id"):
        fields[INBOUND_CHILD_WORKTREE_KEY] = dict(child_worktree)
    continues_task_id = str(source.get(CONTINUES_TASK_PAYLOAD_KEY) or "").strip()
    if continues_task_id:
        fields[INBOUND_CONTINUES_TASK_KEY] = continues_task_id
    delivery_note = str(source.get(DELIVERY_NOTE_PAYLOAD_KEY) or "").strip()
    if delivery_note:
        fields[INBOUND_DELIVERY_NOTE_KEY] = delivery_note
    return fields


def _inbound_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """合并交回的几条结果，每条带上自己的正文和没外发的附件；不是合并交回给空列表。"""
    raw = payload.get(DISPATCH_RESULTS_PAYLOAD_KEY)
    if not isinstance(raw, list):
        return []
    results: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachments = item.get("attachments")
        results.append({
            **_inbound_result_fields(item),
            "text": str(item.get("text") or "").strip(),
            "attachments": list(attachments) if isinstance(attachments, list) else [],
        })
    return results if len(results) > 1 else []


class CompletionOutcome(str, Enum):
    accepted = "accepted"
    duplicate = "duplicate"
    stale = "stale"


@dataclass(frozen=True)
class TaskCompletion:
    task: Task
    outcome: CompletionOutcome


@dataclass(frozen=True)
class LeaseRenewal:
    ok: bool
    reason: str = ""


class CancelEffect(str, Enum):
    none = ""
    requested = "requested"
    cancelled = "cancelled"


@dataclass(frozen=True)
class TaskOrigin:
    task_id: str
    origin_task_id: str
    conversation_key: str
    channel: str
    platform_auth: dict[str, Any] = field(default_factory=dict)


class TaskStoreMixin:
    """提供 Task 创建 / 领取 / 完成 / 取消等方法。

    运行时 self 是 SupervisorState 实例。
    """

    # ---- 基础工具 ----

    def _default_entry_node(self) -> str:
        return default_entry_node_id(self.workspace_root)

    def _entry_node_pinned(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("entry_node_pinned")) and bool(str(payload.get("entry_node_id") or "").strip())

    def _vision_node_is_configured(self, node_id: str) -> bool:
        """视觉入口节点有没有自己的模型或地址。

        没有就别绕：engine/model.py 里空值一律回落全局，这个节点解析出来就是主渠道那个
        看不了图的模型，只是顺带把工具和委派也砍光了 —— 比不绕更糟。
        节点照 engine 的方式加载（先认文件名，再认旧 id），加载不到时按「配了」算，维持原来的绕。
        """
        from engine.node import load_node

        node = load_node(self.workspace_root, node_id)
        if node is None:
            return True
        return bool(node.model or node.base_url)

    def _warn_vision_node_unconfigured(self, node_id: str) -> None:
        seen = getattr(self, "_vision_unconfigured_warned", None)
        if seen is None:
            seen = set()
            self._vision_unconfigured_warned = seen
        if node_id in seen:
            return
        seen.add(node_id)
        logger.warning(
            "vision routing: %s has no model/base_url of its own, keeping images on the main channel. "
            "Set CLONOTH_VISION_MODEL / CLONOTH_VISION_API_KEY / CLONOTH_VISION_BASE_URL "
            "(QQ_VISION_* still read) to make the detour meaningful.",
            node_id,
        )

    def _vision_routing_applies(self, setting: Any, node_id: str = "") -> bool:
        """带图消息要不要绕去视觉入口节点。

        auto：主渠道自己收得下图就别绕 —— 绕过去等于把工具和委派全砍掉，
        换来的还常常是同一个模型。读不到配置时按绕算，那是这里的历史行为。
        显式 true 不看节点配没配：那是「把图挡在工具外面」，隔离本身就是目的。
        """
        explicit = parse_bool(setting)
        if explicit is not None:
            return explicit
        store = getattr(self, "config_store", None)
        if store is None:
            return True
        try:
            if store.supports_vision():
                return False
        except Exception:
            logger.warning("vision routing: cannot read provider config, routing as before", exc_info=True)
            return True
        if node_id and not self._vision_node_is_configured(node_id):
            self._warn_vision_node_unconfigured(node_id)
            return False
        return True

    def _route_entry_node_for_capabilities(
        self,
        *,
        cfg: dict[str, Any],
        payload: dict[str, Any],
        selected_entry_node: str,
        default_node: str,
        session_override: str,
    ) -> str:
        """Route multimodal inputs to a configured vision-capable entry node."""
        # A pinned entry node comes from an explicit user command, so image capability routing must not move it.
        if payload.get("dispatch_origin") or session_override or self._entry_node_pinned(payload):
            return selected_entry_node
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        has_image_attachment = any(
            isinstance(a, dict) and str(a.get("type") or "") == "image"
            for a in attachments
        )
        hints = payload.get("route_hints") if isinstance(payload.get("route_hints"), dict) else {}
        has_image_hint = bool(hints.get("has_image"))
        if not (has_image_attachment or has_image_hint):
            return selected_entry_node

        routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
        vision = routing.get("vision") if isinstance(routing.get("vision"), dict) else {}
        channel = str(payload.get("channel") or hints.get("channel") or "").strip()
        registered = find_platform(channel)
        platform = str(hints.get("platform") or "").strip() or (registered.id if registered is not None else "")
        channels = vision.get("channels") if isinstance(vision.get("channels"), dict) else {}
        channel_cfg = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
        platform_cfg = channels.get(platform) if isinstance(channels.get(platform), dict) else {}
        # 目标节点要先算出来：auto 判定看的就是这个节点配没配。
        node_id = str(
            channel_cfg.get("entry_node_id")
            or platform_cfg.get("entry_node_id")
            or vision.get("entry_node_id")
            or ""
        ).strip()
        if not self._vision_routing_applies(vision.get("enabled", "auto"), node_id):
            return selected_entry_node
        return node_id or selected_entry_node or default_node

    def _task_terminal(self, task: Task) -> bool:
        return task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}

    def _task_ending_locked(self, task: Task, now: datetime | None = None) -> TaskEnding:
        """终态之后还在不在过渡段：问「结束了没有」的都用它，再按自己的问题取 continues / still_writing / result_open。"""
        return task_ending(task, now or _now())

    def _to_positive_int(self, value: Any) -> int | None:
        try:
            v = int(value)
            return v if v > 0 else None
        except Exception:
            return None

    def _reset_task_route_state_locked(self, task: Task) -> None:
        """Persist a stable intent identity for a newly produced completion phase."""
        task.route_schema_version = 1
        task.route_generation = max(0, int(task.route_generation or 0)) + 1
        task.delivery_id = f"task:{task.task_id}:completion:{task.route_generation}:v1"
        task.route_status = RouteStatus.pending
        task.routed_at = None
        task.route_error = ""
        task.post_work_schema_version = 0
        task.post_work_id = ""
        task.post_work_status = ""
        task.post_work_error = ""
        task.post_work_phases = []
        raw_input = task.input if isinstance(task.input, dict) else {}
        task_context = raw_input.get("task_context")
        try:
            base_count = int(raw_input.get("base_count") or 0)
        except (TypeError, ValueError):
            base_count = 0
        task.route_context = {
            "runtime_session_id": task.session_id,
            "route_session_id": str(
                raw_input.get("_route_session_id")
                or raw_input.get("parent_session_id")
                or task.session_id
                or ""
            ),
            "parent_session_id": str(raw_input.get("parent_session_id") or ""),
            "branch_session_id": str(raw_input.get("branch_session_id") or ""),
            "base_count": base_count,
            "conversation_key": str(
                task_context.get("conversation_key")
                if isinstance(task_context, dict)
                else ""
            ),
        }

    # ---- 内部 task 创建 ----

    def _event_task_snapshot(
        self, event_type: str, task: Task, *, component: str = "supervisor",
    ) -> dict[str, Any]:
        event = self.eventlog.append(
            session_id=task.session_id,
            component=component,
            type_=event_type,
            payload=self._event_payload_for_task(task, event_type=event_type),
        )
        if event_type in _TASK_END_EVENT_TYPES:
            # 每条终态路径都从这里发事件，任务名下没人再等的审批在这里一并收掉，各条路径不用各记一遍
            self._close_ended_task_approvals_locked(task)
        return event

    # [2026-07-16] 只在 task_created 快照保留完整 input；后续生命周期
    # 事件（started/completed/suspended/resumed 等）的 input 与 created 一致。
    _INPUT_FULL_EVENT_TYPES = frozenset({"task_created"})

    def _event_payload_for_task(
        self, task: Task, *, event_type: str = "",
    ) -> dict[str, Any]:
        """Build a task snapshot payload safe for long-term event storage."""
        payload = task.model_dump(mode="json")
        result = payload.get("result")
        if isinstance(result, dict) and "dispatch_input" in result:
            # Why: dispatch_input can contain a full child prompt or compacted
            # conversation text, and the same stale result used to be repeated in
            # task_started/task_suspended snapshots. How: copy the serialized
            # result and remove only dispatch_input from the event payload while
            # leaving the live Task object untouched. Purpose: reduce future
            # events.jsonl growth and replay memory without changing routing,
            # which reads task.result before or outside event serialization.
            slim_result = dict(result)
            slim_result.pop("dispatch_input", None)
            slim_result["dispatch_input_omitted"] = True
            payload["result"] = slim_result
        if payload.get("kind") == TaskKind.tool.value and isinstance(payload.get("result"), dict):
            # 工具任务结果带着整份输出（可达几 MB），快照里只存摘录：路由读活 Task 对象，只有重启回放才拿到摘录
            bounded_result, clipped = bounded_for_event(payload["result"])
            if clipped:
                payload["result"] = {**bounded_result, "_event_text_clipped": True}
        # [2026-07-16] Why: task_started/task_completed/task_suspended/
        # task_resumed 等事件重复写入与 task_created 相同的 input（实测每条
        # ~1KB）How: 除 task_created 外，把 input 替换为只
        # 保留路由/回放必需轻量字段的瘦身版本并标记 _input_slimmed。
        # Purpose: 回放仍可从 task_created 拿完整 input，后续事件体积大幅缩减，
        # 不影响活 Task 对象与路由。
        if event_type and event_type not in self._INPUT_FULL_EVENT_TYPES:
            raw_input = payload.get("input")
            if isinstance(raw_input, dict) and raw_input:
                slim_input: dict[str, Any] = {"_input_slimmed": True}
                for _k in (
                    "task_context", "_dispatch_origin", "dispatch_context_mode",
                    "parent_session_id", "branch_session_id", "base_count", "base_last_id",
                    "_route_session_id", "_branch_finalized", "child_session_id",
                    "_compact_dispatch_pending", "_system_task", "_session_reset",
                    "_compact_plan", "_compact_target_session_id", "_compact_keep_recent",
                    "_compact_keep_recent_tokens", "_compact_threshold_tokens", "_compact_failures",
                    "context_ref", STEPS_USED_KEY, REQUEUE_MARK, AWAITING_INPUT_KEY,
                    DISPATCH_DEPTH_KEY, DISPATCH_TREE_KEY,
                    *WORKTREE_SNAPSHOT_INPUT_KEYS,
                ):
                    if _k in raw_input:
                        slim_input[_k] = raw_input[_k]
                context = slim_input.get("task_context")
                if isinstance(context, dict) and OUTPUT_GUIDE_KEY in context:
                    # 输出说明只给那一轮用，task_created 里已经存过完整的一份，后面的快照不再重复写。
                    slim_input["task_context"] = {key: value for key, value in context.items() if key != OUTPUT_GUIDE_KEY}
                if event_type == "task_resumed":
                    slim_input["_removed_input_keys"] = [
                        key for key in _RESUME_REMOVABLE_INPUT_KEYS if key not in raw_input
                    ]
                    resume_data = raw_input.get("resume_data")
                    if isinstance(resume_data, dict) and resume_data.get("type") == "compact_done":
                        slim_input["resume_data"] = resume_data
                payload["input"] = slim_input
        return payload

    @staticmethod
    def _merge_slim_task_input(previous: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any]:
        merged = {
            **previous,
            **{key: value for key, value in updated.items() if key not in {"_input_slimmed", "_removed_input_keys"}},
        }
        removed = updated.get("_removed_input_keys")
        if isinstance(removed, list):
            for key in _RESUME_REMOVABLE_INPUT_KEYS:
                if key in removed:
                    merged.pop(key, None)
        old_context = previous.get("task_context")
        new_context = merged.get("task_context")
        if (
            isinstance(old_context, dict) and isinstance(new_context, dict)
            and OUTPUT_GUIDE_KEY in old_context and OUTPUT_GUIDE_KEY not in new_context
        ):
            merged["task_context"] = {**new_context, OUTPUT_GUIDE_KEY: old_context[OUTPUT_GUIDE_KEY]}
        return merged

    def _apply_task_snapshot(self, payload: dict[str, Any]) -> None:
        # 后续快照只保存输入增量，回放时仍需保留创建事件中的完整指令。
        try:
            _new_input = payload.get("input")
            if isinstance(_new_input, dict) and _new_input.get("_input_slimmed"):
                _tid = payload.get("task_id")
                _prev = self.tasks.get(_tid) if _tid else None
                _prev_input = getattr(_prev, "input", None) if _prev else None
                if isinstance(_prev_input, dict) and _prev_input:
                    payload = dict(payload)
                    payload["input"] = self._merge_slim_task_input(_prev_input, _new_input)
            t = Task.model_validate(payload)
        except Exception:
            return
        existed = t.task_id in self.tasks
        self.tasks[t.task_id] = t
        if not existed:
            self._task_order.append(t.task_id)
        cur = self.session_generations.get(t.session_id, 0)
        if t.session_generation > cur:
            self.session_generations[t.session_id] = t.session_generation

    def _apply_cancel_requested(self, session_id: str, payload: dict[str, Any]) -> None:
        sid = str(session_id or payload.get("session_id") or "")
        if not sid:
            return
        self._cancelled_sessions.add(sid)
        gen = self._to_positive_int(payload.get("session_generation"))
        if gen is not None and gen > self.session_generations.get(sid, 0):
            self.session_generations[sid] = gen

    def _create_task_locked(
        self,
        *,
        session_id: str,
        session_generation: int,
        kind: TaskKind,
        node_id: str | None = None,
        tool_name: str | None = None,
        input_data: dict[str, Any] | None = None,
        continuation: dict[str, Any] | None = None,
        source_inbound_seq: int | None = None,
        caller_task_id: str | None = None,
        batch_id: str | None = None,
        batch_index: int = 0,
        lane: TaskLane | str | None = None,
    ) -> Task:
        """建任务并记 task_created。lane 不给（或给的认不出）就按 derive_lane 定。"""
        now = _now()
        task_input = dict(input_data or {})
        # [2026-05-29 方案C第一步] 为什么：task_created 事件在 Task 创建时立即
        # 持久化，调用方如果在 _create_task_locked 返回后才补 dispatch 元数据，SDK
        # 无法从事件 payload.input.task_context 中读取结构化路由字段。怎么改：在
        # 创建 Task 之前就把 _dispatch_origin、dispatch_context_mode、
        # parent_conversation_key、route_conversation_key 复制进 task_context。
        # 目的：下游 EventRouter 可直接从 task_created 事件判断子 session 属于哪个
        # 父频道，不再反解析 agent: 字符串。
        dispatch_origin = task_input.get("_dispatch_origin")
        if isinstance(dispatch_origin, dict) and dispatch_origin:
            task_context = task_input.get("task_context")
            if not isinstance(task_context, dict):
                task_context = {}
                task_input["task_context"] = task_context
            dispatch_mode = str(
                task_input.get("dispatch_context_mode")
                or dispatch_origin.get("context_mode")
                or task_context.get("dispatch_context_mode")
                or ""
            ).strip()
            parent_conv_key = str(
                dispatch_origin.get("parent_conversation_key")
                or task_context.get("parent_conversation_key")
                or task_context.get("route_conversation_key")
                or ""
            ).strip()
            if dispatch_mode:
                task_input.setdefault("dispatch_context_mode", dispatch_mode)
                dispatch_origin.setdefault("context_mode", dispatch_mode)
                task_context.setdefault("dispatch_context_mode", dispatch_mode)
            if parent_conv_key:
                dispatch_origin.setdefault("parent_conversation_key", parent_conv_key)
                task_context.setdefault("parent_conversation_key", parent_conv_key)
                task_context.setdefault("route_conversation_key", parent_conv_key)

        task_lane = parse_lane(lane) if lane is not None else None
        if task_lane is None:
            task_lane = derive_lane(task_input, inherited=self._origin_lane_locked(caller_task_id, task_input))
        task_id = str(uuid.uuid4())
        stamp_new_task_locked(
            self, task_input, task_id=task_id, caller_task_id=caller_task_id, session_id=session_id,
        )
        task = Task(
            task_id=task_id,
            session_id=session_id,
            session_generation=session_generation,
            kind=kind,
            node_id=node_id,
            tool_name=tool_name,
            lane=task_lane,
            input=task_input,
            continuation=dict(continuation or {}),
            source_inbound_seq=source_inbound_seq,
            caller_task_id=caller_task_id,
            batch_id=batch_id,
            batch_index=batch_index,
            status=TaskStatus.pending,
            cancel_requested=False,
            worker_id=None,
            created_at=now,
            updated_at=now,
            queued_at=now,
            lease_expires_at=None,
            result={},
        )
        self.tasks[task.task_id] = task
        self._task_order.append(task.task_id)
        self._event_task_snapshot("task_created", task)
        return task

    def _origin_lane_locked(self, caller_task_id: str | None, task_input: dict[str, Any]) -> TaskLane | None:
        """新任务替之干活的那个任务排哪一道：同步派发看 caller，异步派发看 dispatch_origin 里的发起任务。"""
        origin = task_input.get("_dispatch_origin")
        source_id = str(
            caller_task_id
            or (origin.get("caller_task_id") if isinstance(origin, dict) else "")
            or ""
        ).strip()
        source = self.tasks.get(source_id) if source_id else None
        return source.lane if source is not None else None

    # ---- 取消、恢复、失败：所有路径共用的状态迁移 ----

    def _cancel_task_locked(
        self,
        task: Task,
        now: datetime,
        *,
        force: bool = False,
        finalize_branch: bool = True,
        reason: str = "任务已取消",
    ) -> CancelEffect:
        """取消一个任务：running 且租约还在时只记取消请求，等 worker 停下回报；其余直接终结并结算。

        force 用于 worker 已经不在的场合（引擎重启、分支拆除），不等回报直接终结。
        交回了 dispatch、路由还没跑完的也只记取消请求：路由进锁时按取消收掉，不再派出子任务。
        """
        if self._task_terminal(task):
            if task.cancel_requested or not self._task_ending_locked(task, now).continues:
                return CancelEffect.none
            task.cancel_requested = True
            self._event_task_snapshot("task_cancel_requested", task)
            return CancelEffect.requested
        newly_requested = not task.cancel_requested
        task.cancel_requested = True
        lease_alive = task.lease_expires_at is not None and task.lease_expires_at >= now
        if task.status == TaskStatus.running and lease_alive and not force:
            if not newly_requested:
                return CancelEffect.none
            task.updated_at = now
            self._event_task_snapshot("task_cancel_requested", task)
            return CancelEffect.requested
        self._terminate_cancelled_locked(task, now, finalize_branch=finalize_branch, reason=reason)
        return CancelEffect.cancelled

    def _terminate_cancelled_locked(
        self, task: Task, now: datetime, *, finalize_branch: bool = True, reason: str = "任务已取消",
    ) -> None:
        """直接把任务终结为取消并结算。在等一批子任务、会话又会留下来时，先记下那一批的现状。"""
        captured = finalize_branch and stopped_batch.capture_locked(
            self, task, reason=reason, now=now, batch_id=stopped_batch.waiting_batch_id(task), kept_only=True,
        )
        teardown_started = bool(task.input.get("_branch_finalize_worker_started"))
        task.status = TaskStatus.cancelled
        task.updated_at = now
        task.lease_expires_at = None
        task.waiting_for_task_id = None
        task.waiting_for_batch_id = None
        task.result = {"action": "cancelled", "node_id": task.node_id or task.tool_name or "", "error": reason}
        self._event_task_snapshot("task_cancelled", task)
        self._settle_cancelled_task_locked(task, finalize_branch=finalize_branch)
        if captured and (teardown_started or not task.input.get("_branch_finalize_worker_started")):
            # 那一批由取消后的收尾线程写进会话；这次没起收尾线程，就不能让它的会话一直算忙
            stopped_batch.drop_locked(task)

    def _live_descendants_locked(self, root_task_id: str) -> list[Task]:
        """root 同步派出去、还会接着跑的全部后代（交回的委派还在路由的也算），父在子前。"""
        children: dict[str, list[Task]] = {}
        for candidate in self.tasks.values():
            caller_id = (candidate.caller_task_id or "").strip()
            if caller_id and self._task_ending_locked(candidate).continues:
                children.setdefault(caller_id, []).append(candidate)
        found: list[Task] = []
        seen = {root_task_id}
        queue = [root_task_id]
        while queue:
            for child in children.get(queue.pop(0), []):
                if child.task_id not in seen:
                    seen.add(child.task_id)
                    found.append(child)
                    queue.append(child.task_id)
        return found

    def _dispatched_descendants_locked(self, root_task_id: str) -> list[Task]:
        """root 派出去的全部后代里还会接着跑的（交回的委派还在路由的也算），同步派的（caller）、异步派的（dispatch_origin 记的发起任务）、
        结果回调接着它做的那一轮（inbound_continues_task_id）都算，父在子前。

        异步委派派完就结束的中间任务也要穿过去：它结束了，它派出去的、接着它做的还可能在跑。
        """
        children: dict[str, list[Task]] = {}
        for candidate in self.tasks.values():
            parent_ids = {
                child_retention.parent_task_id(candidate),
                str(candidate.input.get(INBOUND_CONTINUES_TASK_KEY) or "").strip(),
            }
            for parent_id in parent_ids - {"", candidate.task_id}:
                children.setdefault(parent_id, []).append(candidate)
        found: list[Task] = []
        seen = {root_task_id}
        queue = [root_task_id]
        while queue:
            for child in children.get(queue.pop(0), []):
                if child.task_id in seen:
                    continue
                seen.add(child.task_id)
                queue.append(child.task_id)
                if self._task_ending_locked(child).continues:
                    found.append(child)
        return found

    def _resume_suspended_task_locked(
        self,
        task: Task,
        resume_data: dict[str, Any],
        *,
        action_type: str = "",
        context_ref: str | None = None,
        drop_input_keys: tuple[str, ...] = (),
    ) -> None:
        """把挂起的任务放回队列，带上恢复数据。重新排队从这一刻起算排队时长。"""
        now = _now()
        task.status = TaskStatus.pending
        task.waiting_for_task_id = None
        task.waiting_for_batch_id = None
        task.worker_id = None
        task.lease_expires_at = None
        task.updated_at = now
        task.queued_at = now
        task.input["resume_data"] = resume_data
        if action_type:
            task.input["_last_child_action_type"] = str(action_type).strip()
        if context_ref is None:
            context_ref = str((task.result or {}).get("context_ref") or task.input.get("context_ref") or "")
        task.input["context_ref"] = context_ref
        for key in drop_input_keys:
            task.input.pop(key, None)
        self._event_task_snapshot("task_resumed", task)

    def _apply_failure_locked(self, task: Task, error: str, *, now: datetime | None = None) -> None:
        """把任务置为失败。cancel_requested 一并置上：还在跑的 worker 靠它和续租结果停手。"""
        now = now or _now()
        task.cancel_requested = True
        task.status = TaskStatus.failed
        task.updated_at = now
        task.lease_expires_at = None
        task.waiting_for_task_id = None
        task.waiting_for_batch_id = None
        task.result = {"action": "fail", "node_id": task.node_id or task.tool_name or "", "error": error}

    def _fail_task_locked(
        self, task: Task, *, error: str, now: datetime | None = None, worker_gone: bool = False,
    ) -> None:
        """由 supervisor 判定失败的任务：取消它派出去的后代，再按一次新的完成走正常路由。

        还在跑、租约也有效的，旧 worker 要到下次续租或查取消才停手：记上停手记号，路由等它回报（最晚 STOP_GRACE）
        再跑，diff、分支合并和交给调用方的那一项都在它停手之后。确定 worker 已经不在（引擎重启）时传 worker_gone=True。
        """
        now = now or _now()
        stopping_claim = "" if worker_gone else self._live_claim_locked(task, now)
        stopped_batch_id = stopped_batch.waiting_batch_id(task)
        self._apply_failure_locked(task, error, now=now)
        self._reset_task_route_state_locked(task)
        if stopping_claim:
            mark_stopping(task, claim_id=stopping_claim, now=now)
        self._event_task_snapshot("task_completed", task, component="supervisor")
        for descendant in self._live_descendants_locked(task.task_id):
            self._cancel_task_locked(descendant, now, reason="上级任务已失败")
        if stopped_batch_id:
            stopped_batch.capture_locked(self, task, reason=error, now=now, batch_id=stopped_batch_id)
        if stopping_claim:
            self._watch_transit_locked(task)
            return
        # 分支任务失败也要合并回父会话、给用户发错误，和正常完成走同一条路
        self._route_completed_task_locked(task)

    def _live_claim_locked(self, task: Task, now: datetime) -> str:
        """还在跑、租约也有效的那次领取；不是的给空串。"""
        lease_alive = task.lease_expires_at is not None and task.lease_expires_at >= now
        if task.status != TaskStatus.running or not lease_alive:
            return ""
        return str(task.claim_id or "").strip()

    def _worker_reported_stop_locked(self, task: Task, claim_id: str) -> None:
        """判失败时还在跑的那个 worker 回报了，说明它已经停手；推迟的路由由这次回报接着推。"""
        if clear_stopping(task, claim_id):
            logger.info("task %s: the worker stopped after the failure, routing it now", task.task_id[:12])

    def _cancel_session_tasks_locked(self, session_id: str) -> None:
        now = _now()
        for task in list(self.tasks.values()):
            if task.session_id == session_id:
                self._cancel_task_locked(task, now)

    def _find_last_context_ref_locked(self, session_id: str, node_id: str, *, context_key: str | None = None) -> str:
        """查找同一 session 中指定 node_id 最近一次完成的 task 的 context_ref。

        如果提供了 context_key，则按 (session_id, node_id, context_key) 精确匹配，
        用于区分同一 node_id 的多个并发实例。否则按 (session_id, node_id) 匹配（默认行为）。

        DEPRECATED — Child Session 隔离（Phase D）
        新 dispatch 路径使用 get_or_create_child_session() 替代本方法。
        当前仍被 task_router 的兼容期 fallback 代码调用，待 child session
        稳定运行后可删除。
        """
        for tid in reversed(self._task_order):
            t = self.tasks.get(tid)
            if (t is not None
                and t.session_id == session_id
                and t.node_id == node_id
                and t.status == TaskStatus.completed
                and t.result.get("context_ref")):
                if context_key:
                    if t.input.get("_context_key") == context_key:
                        return str(t.result["context_ref"])
                else:
                    return str(t.result["context_ref"])
        return ""

    def _create_entry_task_for_inbound_locked(self, *, inbound_seq: int, session_id: str, payload: dict[str, Any]) -> Task | None:
        text = str(payload.get("text") or "").strip()
        has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
        # 委派结果正文和摘要都空也要建：派发方得知道它做完了，只是没交回内容
        is_dispatch_result = str(payload.get("message_type") or "").strip() == "dispatch_result"
        if not text and not has_attachments and not is_dispatch_result:
            return None
        # 优先级: session 覆盖（AI switch） > 前端指定 > session 记录的 > 全局默认
        default_node = self._default_entry_node()
        session_override = self.session_entry_overrides.get(session_id, "").strip()
        session_info = self.sessions.get(session_id)
        # Why: after a supervisor restart, session_entry_overrides is deliberately
        # cleared. How: read the persisted SessionInfo.entry_node_id before using
        # the global runtime default. Purpose: callbacks keep the node binding
        # selected when the session was first routed or switched.
        session_recorded = (session_info.entry_node_id if session_info else "").strip() if session_info else ""
        payload_entry = str(payload.get("entry_node_id") or "").strip()
        # [2026-05-28] dispatch 场景下跳过 session_override，用 payload 指定的 entry_node。
        # 为什么：dispatch inbound 的 entry_node_id 由调用方精确指定，不应被
        #   session 级覆盖（如 AI switch_node）干扰。
        # 怎么改：检查 dispatch_origin 存在时走简化路径。
        # 目的：确保 dispatch 的目标节点始终准确。
        if payload.get("dispatch_origin"):
            entry_node = payload_entry or default_node
        elif self._entry_node_pinned(payload):
            entry_node = payload_entry
        else:
            entry_node = (
                session_override
                or payload_entry
                or session_recorded
                or default_node
            )
        cfg = load_runtime_config(self.workspace_root)
        entry_node = self._route_entry_node_for_capabilities(
            cfg=cfg,
            payload=payload,
            selected_entry_node=entry_node,
            default_node=default_node,
            session_override=session_override,
        )
        if session_info and not session_info.entry_node_id:
            session_info.entry_node_id = entry_node
            self._session_store.update_entry_node(session_id, entry_node)
        # Record the actual entry node used for this session (for getActiveNode API)
        self.session_last_entry_node[session_id] = entry_node
        generation = self._current_session_generation_locked(session_id) or 1
        if not self.session_generations.get(session_id):
            self.session_generations[session_id] = generation
        self._cancelled_sessions.discard(session_id)
        # fork 模式的子节点要看到调用方的对话：经 /v1/inbound 来的已在锁外复制好装进来，这里只补进程里直接建的
        _fork_from = dispatch_fork.fork_source(payload)
        _fork_error = dispatch_fork.copy_if_empty_locked(self, _fork_from, session_id) if _fork_from else ""
        # [Fork/Merge 2026-05-12] 每条 inbound 都创建独立入口分支。
        branch_session_id, fork_meta = self._create_entry_branch_locked(session_id, inbound_seq)
        branch_base_count = int(fork_meta.get("base_count") or 0)
        self._cancelled_sessions.discard(branch_session_id)
        # 收集当前活跃 task 摘要，注入给入口节点 AI 判断
        active_tasks_summary = self._active_tasks_summary_locked(session_id)
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
        use_context = bool(payload.get("use_context", True))
        platform_auth = dict(payload.get("platform_auth") or {}) if isinstance(payload.get("platform_auth"), dict) else {}
        route_hints = dict(payload.get("route_hints") or {}) if isinstance(payload.get("route_hints"), dict) else {}
        memory_hints = dict(payload.get("memory_hints") or {}) if isinstance(payload.get("memory_hints"), dict) else {}
        output_guide = output_guide_payload(payload.get(OUTPUT_GUIDE_KEY))
        session_ratio = calibrated_chars_per_token(self.session_calibration(session_id))
        # Step 2（2026-04-16）：主节点切 ConversationStore。
        # flag 开启时不再注入 context_ref，engine 侧 runner.py 会从 data/conversations/{session_id}.jsonl
        # 加载 history，不再依赖 node_contexts snapshot。
        # flag 关闭时走旧路径，调用 _find_last_context_ref_locked 注入 snapshot ref。
        # _find_last_context_ref_locked 本体保留（兼容期回退路径仍需要）。
        main_use_conv = main_session_enabled(cfg)
        if main_use_conv:
            last_ctx_ref = ""
        else:
            # 查找入口节点上一轮的 context_ref，使对话上下文（含工具调用）跨轮次连续
            last_ctx_ref = self._find_last_context_ref_locked(session_id, entry_node)
        _dispatch_origin = payload.get("dispatch_origin")
        if not isinstance(_dispatch_origin, dict):
            _dispatch_origin = {}
        else:
            _dispatch_origin = dict(_dispatch_origin)
        _dispatch_parent = str(_dispatch_origin.get("parent_session_id") or "").strip()
        if _dispatch_parent and "parent_session_generation" not in _dispatch_origin:
            _dispatch_origin["parent_session_generation"] = (
                self._current_session_generation_locked(_dispatch_parent) or 1
            )
        _dispatch_context_mode = str(payload.get("dispatch_context_mode") or "").strip()
        _dispatch_parent_conv_key = str(_dispatch_origin.get("parent_conversation_key") or "").strip()

        inbound_metadata: dict[str, Any] = {}
        _inbound_message_type = str(payload.get("message_type") or "").strip()
        if _inbound_message_type:
            inbound_metadata["inbound_message_type"] = _inbound_message_type
        # 合并交回时顶层照第一条：委派深度、任务树都按第一条结果的子任务和它接着做的那一轮算
        inbound_metadata.update(_inbound_result_fields(payload))
        _merged_results = _inbound_results(payload)
        if _merged_results:
            inbound_metadata[INBOUND_RESULTS_KEY] = _merged_results
        if _dispatch_origin:
            _result_fallback = self._dispatch_result_fallback_locked(_dispatch_origin)
            if _result_fallback is not None:
                inbound_metadata[RESULT_FALLBACK_KEY] = _result_fallback
        # 自动续跑的那一轮带着续跑链：第几轮、链从哪一刻起算寿命、上一段留下的待办
        _auto_continue = chain_input(payload.get("auto_continue"))
        _worktrees = worktree_service_of(self)
        # 新任务落在哪个隔离副本：异步委派按派发项和派发方算；结果回到原会话、自动续跑接着用上一段的副本
        _worktree_source = payload.get(WORKTREE_FROM_KEY) if isinstance(payload.get(WORKTREE_FROM_KEY), dict) else None
        if _worktree_source is None and _auto_continue is not None:
            _worktree_source = _worktrees.caller_record_locked(self.tasks.get(_auto_continue["previous_task_id"]))
        _dispatcher_id = str(_dispatch_origin.get("caller_task_id") or "").strip() if _dispatch_origin else ""
        _dispatcher = self.tasks.get(_dispatcher_id) if _dispatcher_id else None
        _continued_ref = parse_handle(_dispatch_origin.get(CONTINUE_FROM_KEY)) if _dispatch_origin else None
        _worktree_plan = _worktrees.plan_child_locked(
            caller=_dispatcher,
            request=_dispatch_origin or None,
            continued_task_id=_continued_ref.task_id if _continued_ref is not None else "",
            source=_worktree_source,
            requested_by=_dispatcher_id,
            session_id=session_id if _dispatch_origin else "",
        )
        _dispatcher_worktree = _worktrees.caller_record_locked(_dispatcher)

        # task_created 快照一产生就包含 SDK 路由所需字段。
        if _dispatch_origin:
            if _dispatch_context_mode:
                _dispatch_origin.setdefault("context_mode", _dispatch_context_mode)
            if _dispatch_parent_conv_key:
                _dispatch_origin.setdefault("parent_conversation_key", _dispatch_parent_conv_key)

        _entry_input: dict[str, Any] = {
            "instruction": text,
            "context_ref": last_ctx_ref,
            "switched_from": default_node if session_override else "",
            "resume_data": {},
            "use_context": use_context,
            "_system_task": bool(payload.get("_system_task", False)),
            "schedule_id": str(payload.get("schedule_id") or ""),
            "active_tasks_summary": active_tasks_summary,
            "attachments": attachments or [],
            "parent_session_id": session_id,
            "branch_session_id": branch_session_id,
            "base_count": branch_base_count,
            "base_last_id": str(fork_meta.get("base_last_id") or ""),
            "fork_copied": int(fork_meta.get("copied") or 0),
            "task_context": {
                "conversation_key": str(payload.get("conversation_key") or ""),
                "channel": str(payload.get("channel") or ""),
                "message_id": str(payload.get("message_id") or ""),
                "entry_node_id": entry_node,
                "session_id": branch_session_id,
                "parent_session_id": session_id,
                "branch_session_id": branch_session_id,
                "base_count": branch_base_count,
                "session_generation": generation,
                "is_system_task": bool(payload.get("_system_task", False)),
                "switched_from": default_node if session_override else "",
                "use_context": use_context,
                "platform_auth": platform_auth,
                "route_hints": route_hints,
                # knowledge_inject 从 rctx.task_context 读它，决定这轮要不要
                # 按人加载某些人的跨会话记忆档案。
                "memory_hints": memory_hints,
                # 平台输出说明，引擎渲染进提示词的缓存前缀和动态区，不写进对话历史。
                **({OUTPUT_GUIDE_KEY: output_guide} if output_guide else {}),
                # 本任务第一次请求拿到实测比值之前，engine 按这段对话上一次的实测估 token
                **({"chars_per_token": round(session_ratio, 4)} if session_ratio is not None else {}),
                # [2026-05-29 方案C第一步] 为什么：SDK 只读取 task_created
                # 事件的 payload.input.task_context，不能依赖后续对 Task 的补写。
                # 怎么改：把 dispatch 上下文模式和父频道 route key 写入
                # task_context。目的：子 session 映射和审批归属判断使用结构化字段。
                "dispatch_context_mode": _dispatch_context_mode,
                "parent_conversation_key": _dispatch_parent_conv_key,
                "route_conversation_key": _dispatch_parent_conv_key,
            },
            "dispatch_context_mode": _dispatch_context_mode,
            "_dispatch_origin": _dispatch_origin,
            **inbound_metadata,
            **({AUTO_CONTINUE_KEY: _auto_continue} if _auto_continue is not None else {}),
        }
        _worktrees.apply_plan(_entry_input, _worktree_plan)
        if _dispatcher_worktree is not None:
            _entry_input[CALLER_WORKTREE_KEY] = _dispatcher_worktree
        task = self._create_task_locked(
            session_id=branch_session_id,
            session_generation=generation,
            kind=TaskKind.node,
            node_id=entry_node,
            input_data=_entry_input,
            continuation={},
            source_inbound_seq=inbound_seq,
            caller_task_id=None,
        )
        _continued = parse_handle(_dispatch_origin.get(CONTINUE_FROM_KEY)) if _dispatch_origin else None
        if _continued is not None and _continued.session_id == session_id:
            # 接着上一个子 agent 做：会话不再为接着做留着，这一程留不留看新任务自己的结局
            child_retention.release_locked(self, session_id, task_id=_continued.task_id)
        # [2026-05-28/2026-05-29] dispatch_origin 透传：异步 dispatch 走 inbound 时
        # 将回调元数据写入 task.input。为什么：task 完成后 task_router 需要知道结果
        # 应发回哪个 session，同时 task_created 事件也必须携带 route 元数据。怎么改：
        # _create_task_locked 前已写入 _dispatch_origin，这里只补 caller_node_id 并保持
        # 兼容。目的：回调注入与 SDK 路由读取同一份结构化元数据。
        if isinstance(_dispatch_origin, dict) and _dispatch_origin:
            task.input["_caller_node_id"] = str(_dispatch_origin.get("caller_node_id") or "")
        if _worktree_plan.refused:
            # repo 不能建副本、或要接着用的副本正被别的任务用着：任务照常建出来再判失败，结果按异步委派的路子交回派发方
            self._fail_task_locked(task, error=_worktree_plan.error)
        elif _fork_error:
            # 调用方的历史复制不了：同样建出来再判失败，不让它从空历史开始
            self._fail_task_locked(task, error=_fork_error)
        else:
            _worktrees.register_child_locked(task, _worktree_plan)
        # Emit transient inbound_accepted so Bot can advance watermark
        self.eventlog.append(
            session_id=session_id,
            component="supervisor",
            type_="inbound_accepted",
            payload={
                "inbound_seq": inbound_seq,
                "task_id": task.task_id,
                "session_id": session_id,
                "branch_session_id": branch_session_id,
                "conversation_key": str(payload.get("conversation_key") or ""),
            },
            transient=True,
        )
        return task

    # ---- 发起来源 ----

    @staticmethod
    def _origin_parent_task_id(task: Task) -> str:
        """身份往上追一级：同步派发看 caller，异步派发看发起任务，结果回调入站看产出结果的那个任务。"""
        origin = task.input.get("_dispatch_origin")
        return str(
            task.caller_task_id
            or (origin.get("caller_task_id") if isinstance(origin, dict) else "")
            or task.input.get("inbound_child_task_id")
            or ""
        ).strip()

    def _platform_auth_origin_locked(self, task: Task) -> tuple[Task, dict[str, Any]]:
        """任务自身 task_context 里的平台身份；没有就沿派发链回溯，返回 (身份所在的任务, 身份)。"""
        current = task
        seen: set[str] = set()
        while current.task_id not in seen:
            seen.add(current.task_id)
            context = current.input.get("task_context")
            auth = context.get("platform_auth") if isinstance(context, dict) else None
            if isinstance(auth, dict) and auth:
                return current, dict(auth)
            parent = self.tasks.get(self._origin_parent_task_id(current))
            if parent is None:
                break
            current = parent
        return current, {}

    def _authorizing_origin_locked(self, session_id: str, task: Task | None) -> tuple[str, str]:
        """(conversation_key, channel) whose permissions govern this operation.

        dispatch 出去的子任务跑在自己的 `agent:...` 会话里，按运行期 session 判权就等于：
        任何群成员只要能让 bot 派一个子节点，「外部平台非管理员禁止写文件 / 禁止敏感命令」
        那几道硬拒就整段跳过。发起会话的 key 由 dispatch 一路透传，嵌套派发也保留最初那个。
        """
        channel = ""
        if task is not None:
            raw_ctx = task.input.get("task_context")
            ctx = raw_ctx if isinstance(raw_ctx, dict) else {}
            channel = str(ctx.get("channel") or "").strip()
            routed = str(
                ctx.get("route_conversation_key") or ctx.get("parent_conversation_key") or ""
            ).strip()
            if routed:
                return routed, channel
            session = self.sessions.get(task.session_id or session_id)
            if session is not None:
                return (
                    str(getattr(session, "conversation_key", "") or ""),
                    channel or str(getattr(session, "channel", "") or ""),
                )
        session = self.sessions.get(session_id)
        if session is None:
            return "", channel
        return (
            str(getattr(session, "conversation_key", "") or ""),
            channel or str(getattr(session, "channel", "") or ""),
        )

    def task_origin(self, task_id: str) -> TaskOrigin | None:
        with self._lock:
            return self._task_origin_locked(task_id)

    def _task_origin_locked(self, task_id: str) -> TaskOrigin | None:
        task = self.tasks.get((task_id or "").strip())
        if task is None:
            return None
        conversation_key, channel = self._authorizing_origin_locked(task.session_id, task)
        origin_task, platform_auth = self._platform_auth_origin_locked(task)
        return TaskOrigin(
            task_id=task.task_id,
            origin_task_id=origin_task.task_id,
            conversation_key=conversation_key,
            channel=channel,
            platform_auth=platform_auth,
        )

    # ---- 内存回收 ----

    def _prune_finished_tasks_locked(self, keep: int) -> int:
        """内存里只留最近 keep 个已了结的任务，返回删掉几个。还会被用到的不删，也不占名额。"""
        pinned = self._tasks_still_needed_locked()
        disposable = [
            task_id for task_id in self._task_order
            if task_id in self.tasks and task_id not in pinned and self._task_settled_locked(self.tasks[task_id])
        ]
        doomed = set(disposable[:max(len(disposable) - max(keep, 0), 0)])
        if not doomed:
            return 0
        for task_id in doomed:
            self.tasks.pop(task_id, None)
        self._task_order = [task_id for task_id in self._task_order if task_id in self.tasks]
        return len(doomed)

    def _task_settled_locked(self, task: Task) -> bool:
        """终态之后的路由、分支收束、后置工作都做完了。"""
        if not self._task_terminal(task) or task.route_status != RouteStatus.routed:
            return False
        # 旧 worker 还没停手的不能清出内存：会话忙不忙要靠它判
        if self._task_ending_locked(task).still_writing:
            return False
        if task.input.get("branch_session_id") and not task.input.get("_branch_finalized"):
            return False
        self._ensure_post_completion_state_locked()
        post_work_id = task.post_work_id
        return not post_work_id or (
            post_work_id not in self._post_work_enqueued and post_work_id not in self._post_work_running
        )

    def _tasks_still_needed_locked(self) -> set[str]:
        """活着的任务还要读的已了结任务：派发链上的祖先（认身份），以及批次里先结束的兄弟（等最后一个结束时一起打包）。"""
        now = _now()
        live = {task_id for task_id, task in self.tasks.items() if self._task_ending_locked(task, now).continues}
        needed: set[str] = set()
        for task_id in live:
            current = self.tasks[task_id]
            while (parent_id := self._origin_parent_task_id(current)) and parent_id not in needed:
                needed.add(parent_id)
                parent = self.tasks.get(parent_id)
                if parent is None:
                    break
                current = parent
        for task in self.tasks.values():
            if task.batch_id and task.caller_task_id in live:
                needed.add(task.task_id)
        needed |= self._async_result_chains_needed_locked()
        return needed

    def _active_tasks_summary_locked(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session 中活跃（pending/running）task 的摘要列表。"""
        result: list[dict[str, Any]] = []
        session_ids = {session_id, *self._entry_branch_ids_for_parent_locked(session_id)}
        for task in self.tasks.values():
            if task.session_id not in session_ids:
                continue
            if self._task_terminal(task):
                continue
            result.append({
                "task_id": task.task_id,
                "kind": task.kind.value,
                "node_id": task.node_id or "",
                "tool_name": task.tool_name or "",
                "status": task.status.value,
                "instruction": str(task.input.get("instruction") or "")[:200],
                "created_at": task.created_at.isoformat() if task.created_at else "",
                # [Fork/Merge 2026-05-12] 摘要暴露分支来源。
                # 原因：主 session 可能同时有多个 branch 任务，入口节点需要区分它们。
                # 做法：优先读取 task.input.branch_session_id，否则用 task.session_id。
                # 目的：保留旧摘要字段的同时支持并发分支可观测性。
                "branch_session_id": str(task.input.get("branch_session_id") or (task.session_id if task.session_id != session_id else "")),
                "parent_session_id": str(task.input.get("parent_session_id") or session_id),
                "source_inbound_seq": task.source_inbound_seq,
            })
        return result

    # [2026-05-28] 新增可选 node_id 参数：只取消指定节点的活跃任务。
    # 为什么：原本 cancel_active_tasks 会取消 session 内所有任务，粒度太粗。
    # 怎么改：传入 node_id 时，跳过不匹配的任务。
    # 目的：允许调用方按节点粒度管理任务生命周期。
    def cancel_active_tasks(self, session_id: str, *, exclude_task_id: str | None = None, node_id: str | None = None) -> dict[str, Any]:
        sid = (session_id or "").strip()
        if not sid:
            return {"ok": False}
        _filter_node = (node_id or "").strip() or None
        # 用旧 id 建的任务（会话存的入口、定时任务、还没回来的回调）也算这个节点的
        _filter_ids = frozenset(known_node_ids(self.workspace_root, _filter_node)) if _filter_node else frozenset()
        with self._lock:
            keep_ids: set[str] = set()
            tid = (exclude_task_id or "").strip()
            while tid and tid not in keep_ids:
                keep_ids.add(tid)
                t = self.tasks.get(tid)
                if t is None:
                    break
                # 沿整条派发链往上：从异步委派里调用时，派它出来、正等着它的任务也不能取消
                tid = child_retention.parent_task_id(t)

            now = _now()
            count = 0
            session_ids = {sid, *self._entry_branch_ids_for_parent_locked(sid)}
            info = self.sessions.get(sid)
            if info is not None:
                # 异步委派跑在 agent: 会话的入口分支上，不在这段对话自己的入口分支里，要顺着会话树找
                session_ids |= self._session_tree_locked(sid, info.conversation_key)
            for task in list(self.tasks.values()):
                if task.session_id not in session_ids or task.task_id in keep_ids:
                    continue
                # node_id 过滤：仅当指定了 node_id 且不匹配时跳过
                if _filter_node and task.node_id not in _filter_ids:
                    continue
                targets = [task]
                if _filter_node and self._task_ending_locked(task).continues:
                    # 只取消某个节点时，它派出去的后代（节点不同）一并取消，和 cancel_single_task 一致
                    targets.extend(
                        descendant for descendant in self._dispatched_descendants_locked(task.task_id)
                        if descendant.task_id not in keep_ids
                    )
                for target in targets:
                    if self._cancel_task_locked(target, now) is not CancelEffect.none:
                        count += 1
            return {"ok": True, "cancelled_count": count}

    def cancel_single_task(self, task_id: str) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链，异步委派出去的也算。"""
        tid = (task_id or "").strip()
        if not tid:
            return {"ok": False, "error": "task_id required"}
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return {"ok": False, "error": "task not found"}
            # 交回的委派还在路由的不算结束：取消会把那次派发叫停
            if not self._task_ending_locked(task).continues:
                return {"ok": True, "cancelled_count": 0, "already_terminal": True}
            now = _now()
            count = 0
            # 父在子前：子任务结算时父已是终态，不会被先唤醒再取消
            for target in (task, *self._dispatched_descendants_locked(tid)):
                if self._cancel_task_locked(target, now) is not CancelEffect.none:
                    count += 1
            return {"ok": True, "cancelled_count": count}

    # ---- orphaned task cleanup ----

    def settle_orphaned_tasks(self, *, claimed_before: datetime | None = None) -> int:
        """engine 整个重启之后收拾旧进程手里的任务，返回重排、判失败和取消的个数之和。

        只动 claimed_before 之前领走、还在 running 的：排队的和挂起等子任务的留给新进程接着办，新进程已经领走的不碰。
        声明了 auto_continue、已经有进展的节点任务重排一次；其余判失败（内部流程和群里的插话照旧取消），
        同步后代连带取消。
        """
        from .task_requeue import (
            ENGINE_RESTART,
            INTERRUPTED_CANCEL_REASON,
            collect_orphans_locked,
            settle_orphans_locked,
            stored_steps,
        )

        with self._lock:
            orphans = collect_orphans_locked(self, claimed_before=claimed_before)
        if not orphans:
            return 0
        # 查进展要读对话文件，放在锁外；回到锁里时状态变了的任务不动
        progress = stored_steps(self.workspace_root, orphans)
        with self._lock:
            outcome = settle_orphans_locked(
                self, orphans, progress, reason=ENGINE_RESTART, cancel_reason=INTERRUPTED_CANCEL_REASON,
            )
        if outcome.total:
            logger.warning(
                "engine restart: requeued %d, failed %d and cancelled %d task(s) left by the old engine",
                outcome.requeued, outcome.failed, outcome.cancelled,
            )
        return outcome.total

    # ---- preempt ----

    def preempt_task(
        self, task_id: str, message: str = "", attachments: list | None = None,
        memory_hints: dict[str, Any] | None = None,
        output_guide: OutputGuide | dict[str, Any] | None = None,
        *,
        caller_task_id: str | None = None,
    ) -> bool:
        """标记单个 task 为 preempt_requested。不影响 session 状态。

        带 caller_task_id 的是任务里的模型经 engine 发起的：目标不在调用方的会话家族里，或不是能插话的节点任务
        （工具任务、系统任务），都和不存在一样返回 False。适配器不带，它按自己会话的 running_tasks 选目标。
        """
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in (TaskStatus.running, TaskStatus.pending):
                return False
            if caller_task_id is not None and not self._family_target_locked(caller_task_id, task):
                return False
            task.preempt_requested = True
            task.preempt_message = message
            task.preempt_attachments = list(attachments or [])
            task.preempt_revision += 1
            task.preempt_memory_hints = None
            if memory_hints is not None:
                from engine.memory_subjects import resolve_load_subjects

                task.preempt_memory_hints = {
                    "subjects": resolve_load_subjects(
                        self.workspace_root, memory_hints.get("subjects"),
                    ),
                }
            task.preempt_output_guide = None
            if output_guide is not None:
                task.preempt_output_guide = output_guide_payload(output_guide) or OutputGuide().task_payload()
            # [Fork/Merge 2026-05-17] Why: a task can be running inside a
            # temporary entry branch, but session event consumers watch the
            # durable parent session. How: use the task route helper when the
            # supervisor includes it, and keep the runtime branch id in the
            # payload for diagnostics. Purpose: accepted preempt requests remain
            # visible without changing the task-local preempt flag semantics.
            route_for_task = getattr(self, "_route_session_id_for_task_locked", None)
            route_session_id = (
                str(route_for_task(task) or "").strip()
                if callable(route_for_task)
                else ""
            ) or task.session_id
            payload = {"task_id": task_id, "session_id": route_session_id, "has_message": bool(message)}
            if route_session_id != task.session_id:
                payload["runtime_session_id"] = task.session_id
            self.eventlog.append(
                session_id=route_session_id,
                component="supervisor",
                type_="preempt_requested",
                payload=payload,
                transient=True,
            )
            return True

    def consume_preempt_message(self, task_id: str, expected_revision: int | None = None) -> dict:
        """只清除已确认的补充消息，避免旧请求覆盖刚到的新补充。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"consumed": False, "message": "", "attachments": [], "revision": 0}
            if expected_revision is not None and expected_revision != task.preempt_revision:
                return {"consumed": False, "revision": task.preempt_revision}
            payload = {
                "consumed": True,
                "message": task.preempt_message,
                "attachments": list(task.preempt_attachments),
                "revision": task.preempt_revision,
            }
            if task.preempt_memory_hints is not None:
                hints = {"subjects": list(task.preempt_memory_hints.get("subjects") or [])}
                payload["memory_hints"] = hints
                self._task_context_locked(task)["memory_hints"] = hints
            if task.preempt_output_guide is not None:
                guide = dict(task.preempt_output_guide)
                payload[OUTPUT_GUIDE_KEY] = guide
                # 写回任务上：同一个任务之后再恢复运行，用的还是这次的说明。
                if guide.get("sections"):
                    self._task_context_locked(task)[OUTPUT_GUIDE_KEY] = guide
                else:
                    self._task_context_locked(task).pop(OUTPUT_GUIDE_KEY, None)
            task.preempt_message = ""
            task.preempt_attachments = []
            task.preempt_memory_hints = None
            task.preempt_output_guide = None
            task.preempt_requested = False
            return payload

    def is_task_preempted(self, task_id: str) -> dict:
        """查询 task 的 preempt 状态，包含 message。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"preempted": False, "message": "", "attachments": [], "revision": 0}
            payload = {
                "preempted": task.preempt_requested,
                "message": task.preempt_message,
                "attachments": list(task.preempt_attachments),
                "revision": task.preempt_revision,
            }
            if task.preempt_memory_hints is not None:
                payload["memory_hints"] = {
                    "subjects": list(task.preempt_memory_hints.get("subjects") or []),
                }
            if task.preempt_output_guide is not None:
                payload[OUTPUT_GUIDE_KEY] = dict(task.preempt_output_guide)
            return payload

    @staticmethod
    def _task_context_locked(task: Task) -> dict[str, Any]:
        task_context = task.input.get("task_context")
        if not isinstance(task_context, dict):
            task_context = {}
            task.input["task_context"] = task_context
        return task_context

    def _task_cancel_pending_locked(self, task: Task) -> bool:
        current_gen = self._current_session_generation_locked(task.session_id)
        return bool(task.cancel_requested or (current_gen and task.session_generation != current_gen))

    def renew_lease(
        self, task_id: str, worker_id: str, lease_sec: float = 120.0, *, claim_id: str | None = None,
    ) -> LeaseRenewal:
        """续租。ok=False 时 worker 必须停下这个任务：它已被回收、取消，或已经交给了别的领取。"""
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        claim = (claim_id or "").strip()
        with self._lock:
            if wid:
                # 名额占满的 worker 不来领任务，只续租：回收线程靠这一笔分清 worker 是忙着还是不在了
                self.mark_engine_seen(worker_id=wid)
            task = self.tasks.get(tid) if tid else None
            if task is None:
                return LeaseRenewal(False, "not_found")
            if task.status != TaskStatus.running:
                return LeaseRenewal(False, "not_running")
            if claim:
                if claim != task.claim_id:
                    return LeaseRenewal(False, "stale_claim")
            elif not wid or task.worker_id != wid:
                return LeaseRenewal(False, "stale_claim")
            if self._task_cancel_pending_locked(task):
                return LeaseRenewal(False, "cancelled")
            now = _now()
            task.lease_expires_at = now + timedelta(seconds=max(10.0, min(lease_sec, 600.0)))
            # 工具执行期间没有流式事件刷新 updated_at，靠心跳顺带刷新，回收线程才分得清活着的 worker
            task.updated_at = now
            return LeaseRenewal(True)

    # ---- task 队列（公开方法） ----

    def assign_next_task(
        self,
        *,
        worker_id: str,
        lease_sec: float = 120.0,
        free_slots: int | None = None,
        capacity: int | None = None,
    ) -> dict[str, Any] | None:
        """给 worker 领一个任务：快道先于交互道，交互道先于后台道，同一道里按排队先后。

        free_slots、capacity 是 worker 报来的空位数和名额（等审批的任务不占名额）：只剩保留给交互道的空位时
        不给后台道的任务，报了 0 个空位就什么都不给。不报的老 engine 只受后台道的全局上限约束。
        """
        wid = (worker_id or "").strip()
        if not wid or (free_slots is not None and free_slots <= 0):
            return None
        limits = LaneLimits.from_config(load_runtime_config(self.workspace_root))
        with self._lock:
            now = _now()
            lease_val = max(10.0, min(lease_sec, 600.0))
            task = self._next_claimable_task_locked(now, limits=limits, free_slots=free_slots, capacity=capacity)
            if task is None:
                return None

            if task.worker_id and task.worker_id != wid:
                # lease expired – 重新排队
                task.worker_id = None
                task.lease_expires_at = None
                self._event_task_snapshot("task_requeued", task)

            task.status = TaskStatus.running
            task.worker_id = wid
            task.claim_id = uuid.uuid4().hex
            task.claimed_at = now
            task.updated_at = now
            task.lease_expires_at = now + timedelta(seconds=lease_val)
            self._event_task_snapshot("task_started", task)
            return task.model_dump(mode="json")

    def _next_claimable_task_locked(
        self,
        now: datetime,
        *,
        limits: LaneLimits,
        free_slots: int | None,
        capacity: int | None,
    ) -> Task | None:
        """各道排在最前、现在就能领的那个，再按道的先后挑。路过的待取消任务顺手取消掉。"""
        heads: dict[TaskLane, Task] = {}
        for tid in list(self._task_order):
            task = self.tasks.get(tid)
            if task is None or task.status != TaskStatus.pending:
                continue
            if self._task_cancel_pending_locked(task):
                self._cancel_task_locked(task, now)
                continue
            if task.lease_expires_at and task.lease_expires_at > now:
                continue
            # 隔离副本还没建好：不能领走，否则就在原仓库里跑了；也不占它那一道的排头
            if isinstance(task.input.get(WORKTREE_PENDING_KEY), dict):
                continue
            if task.lane in heads:
                continue
            heads[task.lane] = task
            if task.lane is CLAIM_ORDER[0]:
                break
        for lane in CLAIM_ORDER:
            head = heads.get(lane)
            if head is None:
                continue
            if lane is TaskLane.background and not self._background_claimable_locked(
                limits, free_slots=free_slots, capacity=capacity,
            ):
                continue
            return head
        return None

    def _background_claimable_locked(
        self, limits: LaneLimits, *, free_slots: int | None, capacity: int | None,
    ) -> bool:
        if not limits.worker_takes_background(free_slots, capacity):
            return False
        if limits.background_max_running <= 0:
            return True
        return limits.background_has_room(self._background_running_locked())

    def _background_running_locked(self) -> int:
        """所有 worker 合计正在跑的后台道任务。等人工审批的不算，和 engine 那边等审批不占名额是一个道理。"""
        running = {
            task.task_id for task in self.tasks.values()
            if task.status == TaskStatus.running and task.lane is TaskLane.background
        }
        if not running:
            return 0
        return len(running - self._tasks_awaiting_approval_locked())

    def _tasks_awaiting_approval_locked(self) -> set[str]:
        """正在跑、这一次领取之后提了审批还没定案的任务。

        没带 task_id 的审批认不出是谁的，不算；重排之前那次领取留下的审批已经没人在等，也不算。
        """
        waiting: set[str] = set()
        for approval in self.approvals.values():
            task_id = str(approval.task_id or "").strip()
            if approval.status != ApprovalStatus.pending or not task_id:
                continue
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.running:
                continue
            if self._asked_in_current_claim(task, approval):
                waiting.add(task_id)
        return waiting

    @staticmethod
    def _asked_in_current_claim(task: Task, approval: Approval) -> bool:
        if task.claimed_at is None:
            return True
        try:
            return approval.requested_at >= task.claimed_at
        except TypeError:
            # 旧数据里不带时区的时间没法和带时区的比，按还在等算
            return True

    def _completion_outcome_locked(self, task: Task, *, worker_id: str, claim_id: str) -> CompletionOutcome:
        if claim_id:
            if task.status == TaskStatus.running and claim_id == task.claim_id:
                return CompletionOutcome.accepted
            if claim_id == task.completed_claim_id:
                return CompletionOutcome.duplicate
            return CompletionOutcome.stale
        # 没带领取代次的旧 worker：保留原来的 worker / 租约校验，但只有 running 才接受回报
        if task.status != TaskStatus.running:
            return CompletionOutcome.stale
        if task.worker_id and task.worker_id != worker_id and task.lease_expires_at and task.lease_expires_at > _now():
            return CompletionOutcome.stale
        return CompletionOutcome.accepted

    def complete_task(
        self, *, task_id: str, worker_id: str, result: dict[str, Any], claim_id: str | None = None,
    ) -> TaskCompletion | None:
        """受理 worker 的完成回报；任务不存在时返回 None。

        只有当前领取的回报会改状态，同一领取的重发原样应答，旧领取一律作废。终态但
        路由没跑完的任务，任何一次回报都会把那次路由重新推一遍（按 delivery_id 幂等）。
        """
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        claim = (claim_id or "").strip()
        if not tid or not wid:
            return None
        from .task_requeue import requeue_after_shutdown_locked, shutdown_failure

        should_route = False
        cancelled_while_running = False
        requeued = False
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            reset_cleanup = bool(task.input.get("_session_reset"))
            outcome = self._completion_outcome_locked(task, worker_id=wid, claim_id=claim)
            if claim:
                self._worker_reported_stop_locked(task, claim)
            if reset_cleanup:
                pass
            elif outcome is CompletionOutcome.accepted:
                now = _now()
                task.completed_claim_id = task.claim_id
                task.result = dict(result or {})
                task.updated_at = now
                task.lease_expires_at = None
                if self._task_cancel_pending_locked(task):
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.route_status = RouteStatus.routed
                    task.routed_at = now
                    task.route_error = ""
                    self._event_task_snapshot("task_cancelled", task, component="engine")
                    self._settle_cancelled_task_locked(task, finalize_branch=False)
                    cancelled_while_running = True
                elif requeue_after_shutdown_locked(self, task, task.result):
                    requeued = True
                else:
                    failure = shutdown_failure(task, task.result)
                    if failure is not None:
                        task.result = failure
                    act = str(task.result.get("action") or "").strip()
                    if act == "cancelled":
                        task.status = TaskStatus.cancelled
                    elif act == "fail":
                        task.status = TaskStatus.failed
                    else:
                        task.status = TaskStatus.completed
                    self._reset_task_route_state_locked(task)
                    self._event_task_snapshot("task_completed", task, component="engine")
                    should_route = True
            elif self._task_terminal(task):
                should_route = task.route_status != RouteStatus.routed
                if not should_route:
                    logger.info(
                        "task completion already routed; skipping duplicate route",
                        extra=self._task_route_log_extra_locked(task, event="task_route_skipped"),
                    )
                elif self._task_ending_locked(task) is TaskEnding.stopping:
                    # 判失败时还在跑的那个 worker 回报停手之前不路由，别的领取的回报不算
                    should_route = False
                    logger.info(
                        "task %s: a report from claim %s while its failed worker has not stopped; route stays deferred",
                        task.task_id[:12], claim[:12],
                    )
            elif outcome is CompletionOutcome.stale:
                logger.info(
                    "stale task completion ignored: task=%s status=%s claim=%s current=%s",
                    task.task_id[:12], task.status.value, claim[:12], (task.claim_id or "")[:12],
                )

        if reset_cleanup:
            session_ids = {
                task.session_id,
                str(task.input.get("branch_session_id") or ""),
                str(task.input.get("child_session_id") or ""),
            }
            for session_id in session_ids - {""}:
                with self._branch_parent_write_lock(session_id):
                    self.purge_session_files(session_id)
        elif cancelled_while_running:
            self._finish_cancelled_task(task)
        elif should_route:
            self._route_completed_task(task)
        execution = getattr(self, "execution", None)
        # 重排回队列的任务还没有结局，执行计划那一步等它真正结束再记
        if execution is not None and not requeued:
            execution.on_task_completed(task)
        return TaskCompletion(task, outcome)
