"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config, main_session_enabled
from file_lock import exclusive_lock

from ._helpers import _now
from .branch_finalize_store import BranchClaim
from .compact_breaker import MAX_TASK_CONSECUTIVE_FAILURES
from .reply_intent import result_from_task as reply_intent_result_from_task
from engine.compact import COMPACT_SUMMARY_PREFIX
from engine.dispatch_batch import MAX_DISPATCH_BATCH_ITEMS, batch_item_attachments
from engine.output_guide import OUTPUT_GUIDE_KEY

from .types import RouteStatus, Task, TaskKind, TaskStatus


log = logging.getLogger(__name__)

# 让任务就此结束的动作。dispatch 不在其中：派出去的子任务回来后同一个任务还会接着跑。
_TASK_END_ACTIONS = frozenset({"finish", "ask", "fail", "cancelled", "preempted"})
# 和 engine/inference/pseudo_tools.py 的 NO_REPLY_MARKER 一致；在这里导入 engine.inference 会把整个推理循环拉进来
OUTBOUND_NO_REPLY = "no_reply"


class BranchFinalizeDeferred(RuntimeError):
    """Another live owner is finalizing this branch identity."""


@dataclass(frozen=True)
class CompactPlanRequest:
    """一次同步压缩 dispatch 要算计划所需的全部输入，锁外算计划时与锁内路由用同一份。"""

    target_session_id: str
    context_ref: str
    keep_recent: int
    keep_recent_tokens: int
    threshold_tokens: int

    def sizing_inputs(self) -> dict[str, int]:
        return {
            "_compact_keep_recent": self.keep_recent,
            "_compact_keep_recent_tokens": self.keep_recent_tokens,
            "_compact_threshold_tokens": self.threshold_tokens,
        }


@dataclass(frozen=True)
class PreparedCompactPlan:
    request: CompactPlanRequest
    plan: dict[str, Any]
    instruction: str


@dataclass(frozen=True)
class CompactApplyJob:
    """把压缩摘要落盘所需的输入，锁内取、锁外执行。"""

    target_session_id: str
    summary: str
    plan: dict[str, Any]
    storage: str
    context_ref: str
    sizing: dict[str, int]


@dataclass(frozen=True)
class CompactApplyOutcome:
    counts: dict[str, Any] | None
    success: bool


@dataclass(frozen=True)
class PreparedCompactApply:
    job: CompactApplyJob
    outcome: CompactApplyOutcome


@dataclass(frozen=True)
class BranchSync:
    """父会话的改写同步到活跃入口分支的结果。branches 是前缀已经对齐的分支 → (前缀条数, 前缀最后一条 id)。"""

    synchronized: bool
    branches: dict[str, tuple[int, str]] = field(default_factory=dict)


@dataclass
class _RouteSlot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


@dataclass
class PostCompletionWork:
    """Frozen payload for non-critical work after a task completion is routed.

    Why: outbound_message must become visible as soon as the critical route has
    merged branch history and appended the user reply. How: copy the completed
    task and its task-scoped message snapshot before the supervisor lock is
    released, then let a background worker run hooks and turn-summary creation.
    Purpose: preserve memory extraction and turn summaries without keeping bot
    event polling blocked behind their I/O and preprocessing.
    """

    task: Task
    route_session_id: str
    session_generation: int
    post_work_id: str = ""
    schema_version: int = 2
    phases: list[dict[str, Any]] = field(default_factory=list)
    task_messages: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class BranchFinalizePlan:
    identity: str
    task_id: str
    parent_session_id: str
    branch_session_id: str
    base_count: int
    merge: bool
    cleanup_session_ids: tuple[str, ...]


def _message_to_turn_summary_dict(message: Any) -> dict[str, Any]:
    """Convert a task message into the history shape used for summary input.

    Why: post-completion work now receives a frozen task-message snapshot from
    the completion critical section instead of reloading ConversationStore later.
    How: accept both Message objects and serialized dictionaries, then copy role,
    content, metadata, tool calls, and native tool-result pairing fields into the
    same shape runner uses. Purpose: one sanitizer protects LLM replay and
    system.turn_summarizer input while the summary path stays I/O-free.
    """
    if isinstance(message, dict):
        d: dict[str, Any] = {
            "role": str(message.get("role") or "unknown"),
            "content": message.get("content") or "",
        }
        meta: dict[str, Any] = {}
        raw_meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else message.get("meta")
        if isinstance(raw_meta, dict):
            meta.update(raw_meta)
        source_task_id = str(message.get("source_task_id") or "").strip()
        if source_task_id:
            meta.setdefault("source_task_id", source_task_id)
        if meta:
            d["_meta"] = meta
        message_type = str(message.get("message_type") or "").strip()
        if message_type:
            d["message_type"] = message_type
        if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
            d["tool_calls"] = copy.deepcopy(message.get("tool_calls"))
        if message.get("tool_call_id"):
            d["tool_call_id"] = str(message.get("tool_call_id") or "")
        if message.get("name"):
            d["name"] = str(message.get("name") or "")
        return d

    d = {"role": getattr(message, "role", "unknown"), "content": getattr(message, "content", "") or ""}
    meta = dict(message.meta) if isinstance(getattr(message, "meta", None), dict) else {}
    source_task_id = str(getattr(message, "source_task_id", "") or "").strip()
    if source_task_id:
        meta.setdefault("source_task_id", source_task_id)
    if meta:
        d["_meta"] = meta
    message_type = str(getattr(message, "message_type", "") or "").strip()
    if message_type:
        d["message_type"] = message_type
    if getattr(message, "tool_calls", None):
        d["tool_calls"] = list(message.tool_calls)
    if getattr(message, "tool_call_id", ""):
        d["tool_call_id"] = str(message.tool_call_id)
    if getattr(message, "name", ""):
        d["name"] = str(message.name)
    return d


def _format_task_messages_for_turn_summary(messages: list[Any]) -> str:
    """Format task messages for system.turn_summarizer without control-tool plumbing."""
    from engine.inference.tool_format import sanitize_control_tool_history

    # [2026-05-07] 轮摘要输入必须跳过 finish 控制流工具历史。
    # 原因：system.turn_summarizer 从 ConversationStore 重新读取任务消息，旧数据中可能已有 finish tool_call/tool_result。
    # 做法：把 Message 转成与 LLM 历史一致的 dict 后复用控制流清洗，再拼接摘要输入。
    # 目的：摘要子任务只看到已交付文本和普通工具结果，不继续传播 finish 协议占位内容。
    cleaned = sanitize_control_tool_history([
        _message_to_turn_summary_dict(message) for message in messages
    ])

    parts: list[str] = []
    for msg in cleaned:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            call_lines: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(call.get("name") or function.get("name") or "").strip()
                raw_args = call.get("arguments") if "arguments" in call else function.get("arguments", {})
                args_text = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False, default=str)
                call_lines.append(f"[tool_call] {name} {args_text}".strip())
            if call_lines:
                # [2026-05-07] 轮摘要输入显式渲染 assistant.tool_calls。
                # 原因：finish 保持为真实工具轮后，assistant.content 常为空；只拼 content 会丢失最终交付参数。
                # 做法：把工具调用名称和参数追加到摘要文本，不改动原始消息结构。
                # 目的：turn_summarizer 能读到 finish.text，同时保留完整工具配对历史。
                content = "\n".join(part for part in [content, *call_lines] if part)
        if len(content) > 5000:
            content = content[:5000] + "\n...[truncated]"
        parts.append(f"<log_{msg.get('role', 'unknown')}>\n{content}\n</log_{msg.get('role', 'unknown')}>")
    body = "\n\n".join(parts)
    return f"=== COMPLETED TASK LOG (read-only, do NOT continue) ===\n\n{body}\n\n=== END OF LOG ==="


class TaskRouterMixin:
    """提供 _route_completed_task_locked 及其子路由方法。

    运行时 self 是 SupervisorState 实例，可以访问
    self.tasks / self._event_task_snapshot / self.append_outbound_message 等。
    """

    # ------------------------------------------------------------------ #
    #  后置工作队列
    # ------------------------------------------------------------------ #

    def _ensure_post_completion_state_locked(self) -> None:
        if not hasattr(self, "_post_completion_queue"):
            self._post_completion_queue = queue.Queue()
        if not hasattr(self, "_post_work_states"):
            self._post_work_states: dict[str, str] = {}
        if not hasattr(self, "_post_work_enqueued"):
            self._post_work_enqueued: set[str] = set()
        if not hasattr(self, "_post_work_running"):
            self._post_work_running: set[str] = set()
        if not hasattr(self, "_post_phase_states"):
            self._post_phase_states: dict[str, str] = {}
        if not hasattr(self, "_post_effect_states"):
            self._post_effect_states: dict[str, str] = {}
        if not hasattr(self, "_recovered_post_works"):
            self._recovered_post_works: dict[str, PostCompletionWork] = {}

    def _ensure_post_completion_worker_locked(self) -> None:
        """Start one daemon worker while holding only the short state lock."""
        self._ensure_post_completion_state_locked()
        if getattr(self, "_post_completion_worker_started", False):
            return
        worker = threading.Thread(
            target=self._post_completion_worker_loop,
            daemon=True,
            name="post-completion-worker",
        )
        self._post_completion_worker_started = True
        self._post_completion_worker = worker
        worker.start()

    def _enqueue_post_completion_work(self, work: PostCompletionWork | None) -> bool:
        """Idempotently queue durable pending work."""
        if work is None or not work.post_work_id:
            return False
        with self._lock:
            self._ensure_post_completion_state_locked()
            state = self._post_work_states.get(work.post_work_id, "pending")
            self._recovered_post_works[work.post_work_id] = work
            if getattr(self, "_defer_post_work_enqueue", False):
                self._post_work_states[work.post_work_id] = state
                self._recovered_post_works[work.post_work_id] = work
                return True
            self._ensure_post_completion_worker_locked()
            if state in {"completed", "failed"}:
                return False
            if work.post_work_id in self._post_work_enqueued or work.post_work_id in self._post_work_running:
                return False
            self._post_work_states[work.post_work_id] = state
            self._post_work_enqueued.add(work.post_work_id)
            self._post_completion_queue.put(work)
            return True

    def _post_completion_worker_loop(self) -> None:
        """Execute hooks and summary without holding Supervisor's global lock."""
        while True:
            work = self._post_completion_queue.get()
            try:
                with self._lock:
                    self._ensure_post_completion_state_locked()
                    self._post_work_enqueued.discard(work.post_work_id)
                    state = self._post_work_states.get(work.post_work_id, "pending")
                    if state in {"completed", "failed"}:
                        continue
                    self._post_work_running.add(work.post_work_id)
                    self._post_work_states[work.post_work_id] = "started"
                    self.eventlog.append(
                        session_id=work.route_session_id or work.task.session_id,
                        component="supervisor",
                        type_="task_post_work_started",
                        payload=self._post_work_status_payload(work, status="started"),
                    )
                self._post_completion_work(work)
            except Exception as exc:
                log.exception(
                    "post completion worker crashed",
                    extra={"event": "task_post_work_worker_crashed", "post_work_id": work.post_work_id},
                )
                with self._lock:
                    self._post_work_running.discard(work.post_work_id)
                    errors = [{"phase": "worker", "error": str(exc)[:4000]}]
                    try:
                        self.eventlog.append(
                            session_id=work.route_session_id or work.task.session_id,
                            component="supervisor",
                            type_="task_post_work_started",
                            payload=self._post_work_status_payload(work, status="started", errors=errors),
                        )
                    except Exception:
                        log.exception("failed to persist post-work interruption")
                    # Never terminalize the aggregate on an infrastructure crash;
                    # per-phase recovery decides retry vs ambiguous safely.
                    self._post_work_states[work.post_work_id] = "started"
            finally:
                self._post_completion_queue.task_done()

    def _task_needs_post_completion_work_locked(self, task: Task) -> bool:
        """Return whether hook/summary phases apply (cleanup is always planned)."""
        if task.batch_id or self._is_compact_dispatch_result(task) or self._is_turn_summary_result(task):
            return False
        # 意愿判定每条不命中的群消息跑一次。记忆提取和轮摘要各自都会因 _system_task
        # 自行短路，但 phase 本身要写两条 eventlog —— 活跃群一天几千条就全是这个。
        if task.input.get("_reply_intent"):
            return False
        act = str((task.result or {}).get("action") or "").strip()
        return act not in {"preempted", "dispatch"}

    def _build_post_work_phases_locked(self, task: Task, post_work_id: str) -> list[dict[str, Any]]:
        phases: list[dict[str, Any]] = []
        if self._task_needs_post_completion_work_locked(task):
            for handler in self.hook_registry.handlers_for("on_entry_task_complete"):
                name = str(handler.get("name") or "unknown").strip() or "unknown"
                phases.append({
                    "phase_id": f"{post_work_id}:hook:{name}",
                    "kind": "hook",
                    "name": name,
                    "retry_policy": "idempotent" if handler.get("post_work_idempotent") else "ambiguous",
                })
            phases.append({
                "phase_id": f"{post_work_id}:turn_summary",
                "kind": "turn_summary",
                "name": "turn_summary",
                "retry_policy": "retry",
            })
        phases.append({
            "phase_id": f"{post_work_id}:cleanup",
            "kind": "cleanup",
            "name": "cleanup",
            "retry_policy": "retry",
        })
        return phases

    def _post_work_status_payload(
        self, work: PostCompletionWork, *, status: str, errors: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "post_work_schema_version": work.schema_version,
            "post_work_id": work.post_work_id,
            "delivery_id": str(work.task.delivery_id or ""),
            "task_id": work.task.task_id,
            "source_inbound_seq": work.task.source_inbound_seq,
            "conversation_key": str((work.task.route_context or {}).get("conversation_key") or ""),
            "session_id": work.route_session_id,
            "runtime_session_id": work.task.session_id,
            "status": status,
            "errors": list(errors or []),
        }

    def _build_post_completion_hook_ctx(
        self, work: PostCompletionWork, phase: dict[str, Any],
    ) -> dict[str, Any]:
        """Build callbacks that acquire the Supervisor lock only per state access."""
        def create_task(**kwargs: Any) -> Task:
            with self._lock:
                return self._create_task_from_hook_locked(**kwargs)

        def current_generation(session_id: str) -> int:
            with self._lock:
                return self._current_session_generation_locked(session_id)

        def session_count() -> int:
            with self._lock:
                return len(self.sessions)

        def task_snapshots(task_ids: list[str]) -> dict[str, dict[str, str]]:
            with self._lock:
                return self._task_snapshots_from_hook_locked(task_ids)

        phase_id = str(phase.get("phase_id") or "")

        def claim_phase_effect(effect_id: str) -> bool:
            key = str(effect_id or "").strip()
            if not key:
                return False
            with self._lock:
                self._ensure_post_completion_state_locked()
                if self._post_effect_states.get(key) in {"started", "completed"}:
                    return False
                self.eventlog.append(
                    session_id=work.route_session_id or work.task.session_id,
                    component="supervisor",
                    type_="task_post_effect_started",
                    payload=self._post_phase_payload(work, phase, status="started", effect_id=key),
                )
                self._post_effect_states[key] = "started"
                return True

        def complete_phase_effect(effect_id: str) -> None:
            key = str(effect_id or "").strip()
            if not key:
                return
            with self._lock:
                if self._post_effect_states.get(key) == "completed":
                    return
                self.eventlog.append(
                    session_id=work.route_session_id or work.task.session_id,
                    component="supervisor",
                    type_="task_post_effect_completed",
                    payload=self._post_phase_payload(work, phase, status="completed", effect_id=key),
                )
                self._post_effect_states[key] = "completed"

        ctx = self._build_supervisor_hook_ctx(
            task=work.task,
            session_id=work.route_session_id,
            post_work_id=work.post_work_id,
            post_phase_id=phase_id,
            post_phase_name=str(phase.get("name") or ""),
        )
        ctx.update({
            "create_task": create_task,
            "current_session_generation": current_generation,
            "session_count": session_count,
            "task_snapshots": task_snapshots,
            "claim_phase_effect": claim_phase_effect,
            "complete_phase_effect": complete_phase_effect,
        })
        return ctx

    def _post_phase_payload(
        self,
        work: PostCompletionWork,
        phase: dict[str, Any],
        *,
        status: str,
        error: str = "",
        effect_id: str = "",
    ) -> dict[str, Any]:
        payload = self._post_work_status_payload(work, status=status)
        payload.update({
            "phase_id": str(phase.get("phase_id") or ""),
            "phase_kind": str(phase.get("kind") or ""),
            "phase_name": str(phase.get("name") or ""),
            "retry_policy": str(phase.get("retry_policy") or "ambiguous"),
            "error": str(error or "")[:4000],
        })
        if effect_id:
            payload["effect_id"] = effect_id
        return payload

    def _persist_post_phase_status_locked(
        self, work: PostCompletionWork, phase: dict[str, Any], *, status: str, error: str = "",
    ) -> None:
        phase_id = str(phase.get("phase_id") or "")
        self.eventlog.append(
            session_id=work.route_session_id or work.task.session_id,
            component="supervisor",
            type_=f"task_post_phase_{status}",
            payload=self._post_phase_payload(work, phase, status=status, error=error),
        )
        self._post_phase_states[phase_id] = status

    def _execute_post_phase(self, work: PostCompletionWork, phase: dict[str, Any]) -> None:
        kind = str(phase.get("kind") or "")
        if kind == "hook":
            name = str(phase.get("name") or "")
            descriptor = next(
                (item for item in self.hook_registry.handlers_for("on_entry_task_complete") if item.get("name") == name),
                None,
            )
            if descriptor is None or not callable(descriptor.get("callback")):
                raise RuntimeError(f"post-work hook unavailable: {name}")
            descriptor["callback"](self._build_post_completion_hook_ctx(work, phase))
            return
        if kind == "turn_summary":
            self._maybe_trigger_turn_summary_locked(
                work.task, phase_id=str(phase.get("phase_id") or ""),
            )
            return
        if kind == "cleanup":
            self._execute_post_cleanup(work, phase)
            return
        raise RuntimeError(f"unknown post-work phase kind: {kind}")

    def _post_completion_work(self, work: PostCompletionWork) -> None:
        """Execute each durable phase independently; one failure never blocks later phases."""
        errors: list[dict[str, str]] = []
        for phase in work.phases:
            phase_id = str(phase.get("phase_id") or "")
            policy = str(phase.get("retry_policy") or "ambiguous")
            with self._lock:
                state = self._post_phase_states.get(phase_id, "pending")
                if state in {"completed", "ambiguous"}:
                    continue
                if state == "failed" and policy not in {"retry", "idempotent"}:
                    continue
                if state == "started" and policy == "ambiguous":
                    self._persist_post_phase_status_locked(
                        work, phase, status="ambiguous",
                        error="started without completed; unsafe third-party side effect not replayed",
                    )
                    errors.append({"phase": phase_id, "error": "ambiguous"})
                    continue
                self._persist_post_phase_status_locked(work, phase, status="started")
            try:
                self._execute_post_phase(work, phase)
            except Exception as exc:
                errors.append({"phase": phase_id, "error": str(exc)[:4000]})
                with self._lock:
                    self._persist_post_phase_status_locked(
                        work, phase, status="failed", error=str(exc),
                    )
                log.exception(
                    "post-work phase failed",
                    extra={"event": "task_post_phase_failed", "post_work_id": work.post_work_id, "phase_id": phase_id},
                )
                continue
            with self._lock:
                self._persist_post_phase_status_locked(work, phase, status="completed")

        with self._lock:
            retryable_failed = any(
                self._post_phase_states.get(str(phase.get("phase_id") or "")) == "failed"
                and str(phase.get("retry_policy") or "") in {"retry", "idempotent"}
                for phase in work.phases
            )
            status = "partial" if retryable_failed else "completed"
            event_type = "task_post_work_failed" if errors else "task_post_work_completed"
            self.eventlog.append(
                session_id=work.route_session_id or work.task.session_id,
                component="supervisor",
                type_=event_type,
                payload=self._post_work_status_payload(work, status=status, errors=errors),
            )
            self._post_work_states[work.post_work_id] = status
            self._post_work_running.discard(work.post_work_id)
            live_task = self.tasks.get(work.task.task_id)
            if live_task is not None and live_task.post_work_id == work.post_work_id:
                live_task.post_work_status = status
                live_task.post_work_error = json.dumps(errors, ensure_ascii=False)[:4000] if errors else ""

    def _execute_post_cleanup(
        self, work: PostCompletionWork, phase: dict[str, Any],
    ) -> None:
        """Delete files lock-free, then commit only minimal in-memory index changes."""
        task = work.task
        origin = task.input.get("_dispatch_origin")
        context_mode = str(
            (origin.get("context_mode") if isinstance(origin, dict) else "")
            or task.input.get("dispatch_context_mode")
            or ""
        ).strip()
        dispatch_sid = str(task.input.get("parent_session_id") or "").strip()
        branch_sid = str(task.input.get("branch_session_id") or "").strip()
        child_sid = str(task.input.get("child_session_id") or "").strip()
        # 只拆真正结束了的任务：同步派发后挂起的任务恢复后还要接着用这些会话
        task_ended = self._task_terminal(task)
        with self._lock:
            dispatch_info = self.sessions.get(dispatch_sid)
            cleanup_dispatch = bool(
                task_ended
                and context_mode in {"fresh", "fork"}
                and dispatch_info is not None
                and dispatch_info.conversation_key.startswith("agent:")
            )
            dispatch_conv = dispatch_info.conversation_key if cleanup_dispatch else ""
            child_entry = self._session_store.get_entry(child_sid)
            child_mode = str(child_entry.get("context_mode") or "").strip() if isinstance(child_entry, dict) else ""
            cleanup_child = bool(
                child_sid and task_ended
                and (child_mode or str(task.input.get("context_mode") or "").strip()) != "accumulate"
            )

        # All potentially slow filesystem work is outside Supervisor._lock.
        file_ids = set()
        if cleanup_dispatch:
            file_ids.update({dispatch_sid, branch_sid})
        if cleanup_child:
            file_ids.add(child_sid)
        for session_id in filter(None, file_ids):
            self.purge_session_files(session_id)

        registry_snapshot = None
        with self._lock:
            registry_remove_ids: set[str] = set()
            if cleanup_dispatch:
                self.conversation_map.pop(dispatch_conv, None)
                self.sessions.pop(dispatch_sid, None)
                self.session_generations.pop(dispatch_sid, None)
                self._cancelled_sessions.discard(dispatch_sid)
                self._session_context_usage.pop(dispatch_sid, None)
                registry_remove_ids.add(dispatch_sid)
            if cleanup_child:
                self._remove_child_mapping_for_session_locked(child_sid)
                self.sessions.pop(child_sid, None)
                self.session_generations.pop(child_sid, None)
                self._cancelled_sessions.discard(child_sid)
                self._session_context_usage.pop(child_sid, None)
                registry_remove_ids.add(child_sid)
            if registry_remove_ids:
                registry_snapshot = self._session_store.remove_sessions(
                    registry_remove_ids, flush=False,
                )
        if registry_snapshot is not None:
            # SessionStore serialization is file I/O and intentionally lock-free
            # with respect to Supervisor; its atomic replace preserves durability.
            if not self._session_store.flush_snapshot(registry_snapshot):
                raise RuntimeError("cleanup sessions registry flush failed")

    # ------------------------------------------------------------------ #
    #  统一路由入口
    # ------------------------------------------------------------------ #

    def _route_session_id_for_task_locked(self, task: Task) -> str:
        """Return the session that user-visible routing should use for a task."""
        # [Fork/Merge 2026-05-12] branch task 的运行 session 与用户会话 session 分离。
        # 原因：entry task 在 branch 上运行，但 outbound、hook 和 adapter 查询仍应落到主 session。
        # 做法：优先读取 finalize 写入的 _route_session_id，其次读取 parent_session_id。
        # 目的：没有 branch_session_id 的旧 task 保持原 session_id 行为。
        route_context = task.route_context if isinstance(task.route_context, dict) else {}
        return str(
            task.input.get("_route_session_id")
            or route_context.get("route_session_id")
            or task.input.get("parent_session_id")
            or route_context.get("parent_session_id")
            or task.session_id
            or ""
        )

    def _merge_branch_transcript_locked(self, parent_session_id: str, branch_session_id: str) -> int:
        """Append branch transcript records into parent transcript, rewriting session_id.

        Returns number of records merged.
        """
        from pathlib import Path
        import json
        transcript_dir = Path(self.workspace_root) / "data" / "transcripts"
        branch_path = transcript_dir / f"{branch_session_id}.jsonl"
        if not branch_path.exists():
            return 0
        parent_path = transcript_dir / f"{parent_session_id}.jsonl"
        count = 0
        try:
            with open(branch_path, "r", encoding="utf-8") as bf:
                lines = bf.readlines()
            with open(parent_path, "a", encoding="utf-8") as pf:
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        # Rewrite session_id so snip_history and summary write-back
                        # can find the record under the parent session.
                        rec["session_id"] = parent_session_id
                        pf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        count += 1
                    except json.JSONDecodeError:
                        continue
            if count:
                log.info("Merged %d transcript records from %s → %s", count, branch_session_id, parent_session_id[:12])
                # [2026-05-25] 用后即焚：合并成功后删除 branch transcript 源文件
                try:
                    branch_path.unlink()
                except Exception:
                    pass
        except Exception as e:
            log.warning("Failed to merge branch transcript %s: %s", branch_session_id, e)
        return count

    def _branch_claim_payload(
        self, plan: BranchFinalizePlan, claim: BranchClaim, *, status: str, error: str = "",
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "identity": plan.identity,
            "task_id": plan.task_id,
            "parent_session_id": plan.parent_session_id,
            "branch_session_id": plan.branch_session_id,
            "owner_id": claim.owner_id,
            "owner_run_id": claim.owner_run_id,
            "owner_pid": claim.owner_pid,
            "token": claim.token,
            "fencing_generation": claim.fencing_generation,
            "status": status,
            "lease_expires_at": datetime.fromtimestamp(claim.lease_expires_at, tz=_now().tzinfo).isoformat() if claim.lease_expires_at else "",
            "error": str(error or "")[:4000],
        }

    def _claim_branch_finalize(self, plan: BranchFinalizePlan) -> BranchClaim:
        # Never wait for SQLite while holding Supervisor._lock: fenced actions
        # intentionally acquire the SQLite writer transaction before taking the
        # short Supervisor lock, so the inverse order would deadlock.
        claim = self._branch_finalize_store.claim(
            plan.identity,
            owner_run_id=self.eventlog.run_id,
            owner_pid=os.getpid(),
            lease_seconds=float(self._branch_finalize_lease_seconds),
        )
        with self._lock:
            self._branch_finalize_claims[plan.identity] = dict(claim.__dict__)
            if claim.status == "owned":
                claimed = self._branch_claim_payload(plan, claim, status="claimed")
                started = self._branch_claim_payload(plan, claim, status="started")
                self.eventlog.append(
                    session_id=plan.parent_session_id, component="supervisor",
                    type_="task_branch_finalize_claimed", payload=claimed,
                )
                self.eventlog.append(
                    session_id=plan.parent_session_id, component="supervisor",
                    type_="task_branch_finalize_started", payload=started,
                )
        return claim

    def _claim_branch_finalize_locked(self, plan: BranchFinalizePlan) -> BranchClaim:
        """Compatibility helper; production orchestration uses lock-safe claim."""
        return self._claim_branch_finalize(plan)

    def _heartbeat_branch_finalize(self, plan: BranchFinalizePlan, claim: BranchClaim) -> BranchClaim | None:
        renewed = self._branch_finalize_store.heartbeat(
            claim, float(self._branch_finalize_lease_seconds),
        )
        if renewed is None:
            return None
        with self._lock:
            payload = self._branch_claim_payload(plan, renewed, status="started")
            self.eventlog.append(
                session_id=plan.parent_session_id, component="supervisor",
                type_="task_branch_finalize_heartbeat", payload=payload,
            )
            self._branch_finalize_claims[plan.identity] = dict(renewed.__dict__)
        return renewed

    def _assert_branch_finalize_owner_locked(self, plan: BranchFinalizePlan, claim: BranchClaim) -> None:
        if not self._branch_finalize_store.is_owner(claim):
            raise RuntimeError("branch finalize ownership lost")

    def _record_branch_finalize_terminal_locked(
        self, plan: BranchFinalizePlan, claim: BranchClaim, *, status: str, error: str = "",
    ) -> None:
        payload = self._branch_claim_payload(plan, claim, status=status, error=error)
        self.eventlog.append(
            session_id=plan.parent_session_id,
            component="supervisor",
            type_="task_branch_finalize_completed" if status == "completed" else "task_branch_finalize_failed",
            payload=payload,
        )
        self._branch_finalize_claims[plan.identity] = {**claim.__dict__, "status": status}

    def _branch_parent_write_lock(self, parent_session_id: str) -> AbstractContextManager[None]:
        digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()[:24]
        return exclusive_lock(self.workspace_root / "data" / "locks" / f"branch-merge-{digest}.lock")

    def _prepare_branch_finalize_locked(
        self, task: Task, *, merge: bool,
    ) -> BranchFinalizePlan | None:
        route_context = task.route_context if isinstance(task.route_context, dict) else {}
        parent = str(
            task.input.get("parent_session_id")
            or route_context.get("parent_session_id")
            or route_context.get("route_session_id")
            or ""
        ).strip()
        branch = str(
            task.input.get("branch_session_id")
            or route_context.get("branch_session_id")
            or ""
        ).strip()
        if not parent or not branch:
            return None
        task.input["_route_session_id"] = parent
        if task.input.get("_branch_finalized"):
            return None
        try:
            base_count = int(task.input.get("base_count") or route_context.get("base_count") or 0)
        except Exception:
            base_count = 0
        cleanup_ids: list[str] = []
        stack = [branch]
        seen: set[str] = set()
        while stack:
            session_id = stack.pop()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            cleanup_ids.append(session_id)
            stack.extend(sorted(self.parent_children.get(session_id, set())))
        identity = f"{task.delivery_id or task.task_id}:branch-finalize:v1"
        plan = BranchFinalizePlan(
            identity=identity,
            task_id=task.task_id,
            parent_session_id=parent,
            branch_session_id=branch,
            base_count=base_count,
            merge=merge,
            cleanup_session_ids=tuple(cleanup_ids),
        )
        self.eventlog.append(
            session_id=parent,
            component="supervisor",
            type_="task_branch_finalize_intent",
            payload={
                "schema_version": 1,
                "identity": identity,
                "task_id": task.task_id,
                "parent_session_id": parent,
                "branch_session_id": branch,
                "base_count": base_count,
                "merge": merge,
                "cleanup_session_ids": cleanup_ids,
            },
        )
        return plan

    def _execute_branch_finalize_io(self, plan: BranchFinalizePlan) -> int:
        """Serialize parent JSONL writes across threads/processes."""
        with self._branch_parent_write_lock(plan.parent_session_id):
            return self._execute_branch_finalize_io_body(plan)

    def _execute_branch_finalize_io_body(self, plan: BranchFinalizePlan) -> int:
        """Idempotently merge and delete branch files without Supervisor._lock."""
        from engine.branch_history import branch_base_count
        from engine.conversation_store import ConversationStore

        merged_count = 0
        store = ConversationStore(self.workspace_root / "data" / "conversations")
        task = self.tasks.get(plan.task_id)
        parent_entry = self._session_store.get_entry(plan.parent_session_id)
        session_reset = bool(task and task.input.get("_session_reset")) or bool(
            parent_entry and parent_entry.get("reset")
        )
        if plan.merge and not session_reset:
            branch_messages = list(store.load(plan.branch_session_id))
            base_count = branch_base_count(branch_messages, plan.branch_session_id, plan.base_count)
            tail = branch_messages[base_count:]
            parent_ids = {str(getattr(message, "id", "") or "") for message in store.load(plan.parent_session_id)}
            missing = [
                message for message in tail
                if not str(getattr(message, "id", "") or "")
                or str(getattr(message, "id", "") or "") not in parent_ids
            ]
            if missing:
                store.append_batch(plan.parent_session_id, missing)
                merged_count = len(missing)

            transcript_dir = self.workspace_root / "data" / "transcripts"
            branch_path = transcript_dir / f"{plan.branch_session_id}.jsonl"
            parent_path = transcript_dir / f"{plan.parent_session_id}.jsonl"
            if branch_path.exists():
                existing: set[str] = set()
                if parent_path.exists():
                    existing = set(parent_path.read_text(encoding="utf-8").splitlines())
                additions: list[str] = []
                for line in branch_path.read_text(encoding="utf-8").splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    record["session_id"] = plan.parent_session_id
                    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    if encoded not in existing:
                        existing.add(encoded)
                        additions.append(encoded)
                if additions:
                    parent_path.parent.mkdir(parents=True, exist_ok=True)
                    with parent_path.open("a", encoding="utf-8") as stream:
                        for line in additions:
                            stream.write(line + "\n")

        for session_id in plan.cleanup_session_ids:
            self.purge_session_files(session_id)
        return merged_count

    def _commit_branch_finalize_indexes_locked(self, plan: BranchFinalizePlan):
        now = _now()
        cleanup_ids = set(plan.cleanup_session_ids)
        for candidate in list(self.tasks.values()):
            if candidate.session_id in cleanup_ids:
                # 会话文件已经删掉，不等 worker 回报；分支收束就是本次 finalize
                self._cancel_task_locked(
                    candidate, now, force=True, finalize_branch=False, reason="所在分支已收束",
                )
        for session_id in cleanup_ids:
            self.sessions.pop(session_id, None)
            self.session_generations.pop(session_id, None)
            self._cancelled_sessions.discard(session_id)
            self._session_context_usage.pop(session_id, None)
            self.entry_branch_parents.pop(session_id, None)
            self.parent_entry_branches.pop(session_id, None)
            self.parent_children.pop(session_id, None)
            self._remove_child_mapping_for_session_locked(session_id)
        branches = self.parent_entry_branches.get(plan.parent_session_id)
        if branches is not None:
            branches.discard(plan.branch_session_id)
            if not branches:
                self.parent_entry_branches.pop(plan.parent_session_id, None)
        children = self.parent_children.get(plan.parent_session_id)
        if children is not None:
            children.discard(plan.branch_session_id)
            if not children:
                self.parent_children.pop(plan.parent_session_id, None)
        return self._session_store.remove_sessions(cleanup_ids, flush=False)

    def _finalize_branch_task(
        self, task: Task, *, merge: bool, hold_claim: bool = False,
    ) -> str | None:
        with self._lock:
            plan = self._prepare_branch_finalize_locked(task, merge=merge)
            if plan is None:
                return self._route_session_id_for_task_locked(task)
            if not hasattr(self, "_branch_finalize_runtime"):
                self._branch_finalize_runtime = {}
        claim = self._claim_branch_finalize(plan)
        if claim.status == "completed":
            return plan.parent_session_id
        if claim.status == "deferred":
            return None

        heartbeat_stop = threading.Event()
        claim_holder = [claim]

        def heartbeat_loop() -> None:
            lease_seconds = float(self._branch_finalize_lease_seconds)
            interval = max(0.02, min(1.0, lease_seconds / 3.0))
            while not heartbeat_stop.wait(interval):
                try:
                    renewed = self._heartbeat_branch_finalize(plan, claim_holder[0])
                    if renewed is None:
                        return
                    claim_holder[0] = renewed
                except Exception:
                    log.exception("branch finalize heartbeat failed", extra={"identity": plan.identity})

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            daemon=True,
            name=f"branch-heartbeat-{task.task_id[:8]}",
        )
        heartbeat.start()
        try:
            if not self._branch_finalize_store.is_owner(claim_holder[0]):
                raise RuntimeError("branch finalize ownership lost before I/O")
            merged_count = self._execute_branch_finalize_io(plan)
            if not self._branch_finalize_store.is_owner(claim_holder[0]):
                raise RuntimeError("branch finalize ownership lost during I/O")

            def commit_indexes_and_registry() -> None:
                with self._lock:
                    registry_snapshot = self._commit_branch_finalize_indexes_locked(plan)
                # Keep the shared SQLite fence while committing the generation
                # snapshot, but never hold Supervisor._lock during file I/O.
                if (
                    registry_snapshot is not None
                    and not self._session_store.flush_snapshot(registry_snapshot)
                ):
                    raise RuntimeError("branch finalize sessions registry flush failed")

            self._branch_finalize_store.fenced_action(
                claim_holder[0], commit_indexes_and_registry, complete=False,
            )

            def persist_finalize_receipt() -> None:
                with self._lock:
                    task.input["_route_session_id"] = plan.parent_session_id
                    task.input["_branch_finalized"] = True
                    task.input["_branch_merged_count"] = merged_count
                    self._event_task_snapshot("task_branch_finalized", task)

            self._branch_finalize_store.fenced_action(
                claim_holder[0], persist_finalize_receipt, complete=False,
            )
            with self._lock:
                if hold_claim:
                    self._branch_finalize_runtime[task.task_id] = {
                        "plan": plan,
                        "claim_holder": claim_holder,
                        "heartbeat_stop": heartbeat_stop,
                        "heartbeat": heartbeat,
                    }
            if not hold_claim:
                if not self._branch_finalize_store.complete(claim_holder[0]):
                    raise RuntimeError("branch finalize fenced completion lost")
                with self._lock:
                    self._record_branch_finalize_terminal_locked(
                        plan, claim_holder[0], status="completed",
                    )
            return plan.parent_session_id
        except Exception as exc:
            failed = self._branch_finalize_store.fail(claim_holder[0], str(exc))
            if failed:
                with self._lock:
                    self._record_branch_finalize_terminal_locked(
                        plan, claim_holder[0], status="failed", error=str(exc),
                    )
                    task.route_status = RouteStatus.failed
                    task.route_error = f"branch finalize failed: {exc}"[:4000]
                    self._event_task_snapshot("task_route_failed", task)
            raise
        finally:
            if not hold_claim or task.task_id not in getattr(self, "_branch_finalize_runtime", {}):
                heartbeat_stop.set()
                heartbeat.join(timeout=1.0)

    def _finalize_branch_task_locked(self, task: Task, *, merge: bool) -> str:
        """Lock-only compatibility: branch I/O must be prepared by the orchestrator."""
        route_session_id = self._route_session_id_for_task_locked(task)
        if task.input.get("branch_session_id") and not task.input.get("_branch_finalized"):
            task.input["_branch_finalize_deferred"] = True
            if not task.input.get("_branch_finalize_worker_started"):
                task.input["_branch_finalize_worker_started"] = True
                threading.Thread(
                    target=self._finalize_branch_task,
                    kwargs={"task": task, "merge": merge},
                    daemon=True,
                    name=f"branch-finalize-{task.task_id[:8]}",
                ).start()
        return route_session_id

    def _route_completed_task(self, task: Task) -> None:
        """路由一次完成。压缩计划和压缩落盘这类大块磁盘 I/O 先在锁外做完，锁内只提交结果。"""
        with self._task_route_slot(task.task_id):
            self._prepare_route_io(task)
            try:
                self._route_completed_task_with_branch(task)
            finally:
                with self._lock:
                    self._prepared_route_io().pop(task.task_id, None)

    @contextmanager
    def _task_route_slot(self, task_id: str) -> Iterator[None]:
        """同一任务的路由一次只跑一个：重发的 complete 要等前一次路由结束，不能拿它锁外算到一半的结果。

        不能在持有 self._lock 时进入，否则与正在路由的线程互等。
        """
        with self._lock:
            slots = getattr(self, "_task_route_slots", None)
            if slots is None:
                slots = self._task_route_slots = {}
            slot = slots.get(task_id)
            if slot is None:
                slot = slots[task_id] = _RouteSlot()
            slot.users += 1
        try:
            with slot.lock:
                yield
        finally:
            with self._lock:
                slot.users -= 1
                if not slot.users:
                    slots.pop(task_id, None)

    def _route_completed_task_with_branch(self, task: Task) -> None:
        action = str((task.result or {}).get("action") or "").strip()
        runtime = None
        if action in _TASK_END_ACTIONS:
            route_session = self._finalize_branch_task(task, merge=True, hold_claim=True)
            if route_session is None:
                return
            runtime = getattr(self, "_branch_finalize_runtime", {}).get(task.task_id)
        if runtime is None:
            with self._lock:
                self._route_completed_task_locked(task)
            return
        plan = runtime["plan"]
        claim = runtime["claim_holder"][0]
        try:
            def fenced_route() -> None:
                with self._lock:
                    self._route_completed_task_locked(task)
            self._branch_finalize_store.fenced_action(claim, fenced_route)
            with self._lock:
                self._record_branch_finalize_terminal_locked(
                    plan, claim, status="completed",
                )
        except Exception as exc:
            failed = self._branch_finalize_store.fail(claim, str(exc))
            if failed:
                with self._lock:
                    self._record_branch_finalize_terminal_locked(
                        plan, claim, status="failed", error=str(exc),
                    )
            raise
        finally:
            runtime["heartbeat_stop"].set()
            runtime["heartbeat"].join(timeout=1.0)
            with self._lock:
                self._branch_finalize_runtime.pop(task.task_id, None)

    def _task_route_log_extra_locked(self, task: Task, *, event: str) -> dict[str, Any]:
        """Return stable structured fields available at Supervisor route time."""
        route_session_id = self._route_session_id_for_task_locked(task)
        task_context = task.input.get("task_context")
        context_conversation = (
            str(task_context.get("conversation_key") or "").strip()
            if isinstance(task_context, dict)
            else ""
        )
        session_info = self.sessions.get(route_session_id)
        conversation_key = context_conversation or str(
            getattr(session_info, "conversation_key", "") or ""
        ).strip()
        route_status = (
            task.route_status.value
            if isinstance(task.route_status, RouteStatus)
            else str(task.route_status or "")
        )
        return {
            "event": event,
            "task": task.task_id,
            "task_id": task.task_id,
            "source_seq": task.source_inbound_seq,
            "source_inbound_seq": task.source_inbound_seq,
            "conversation": conversation_key,
            "conversation_key": conversation_key,
            "session": route_session_id,
            "session_id": route_session_id,
            "runtime_session_id": task.session_id,
            "route_status": route_status,
            "delivery_id": str(task.delivery_id or ""),
            "route_generation": int(task.route_generation or 0),
        }

    def _route_completed_task_locked(self, task: Task) -> None:
        """Route one completion once, persisting success or retryable failure."""
        action = str((task.result or {}).get("action") or "").strip()
        if (
            action in _TASK_END_ACTIONS
            and task.input.get("branch_session_id")
            and not task.input.get("_branch_finalized")
        ):
            if not task.input.get("_branch_route_worker_started"):
                task.input["_branch_route_worker_started"] = True
                threading.Thread(
                    target=self._route_completed_task,
                    args=(task,),
                    daemon=True,
                    name=f"branch-route-{task.task_id[:8]}",
                ).start()
            return
        if task.route_status == RouteStatus.routed:
            log.info(
                "task completion already routed; skipping duplicate route",
                extra=self._task_route_log_extra_locked(
                    task, event="task_route_skipped",
                ),
            )
            return

        log.info(
            "routing completed task",
            extra=self._task_route_log_extra_locked(
                task, event="task_route_started",
            ),
        )
        try:
            self._route_completed_task_inner_locked(task)
        except Exception as exc:
            task.route_status = RouteStatus.failed
            task.routed_at = None
            task.route_error = str(exc)[:4000]
            task.updated_at = _now()
            try:
                self._event_task_snapshot("task_route_failed", task)
            except Exception:
                log.exception(
                    "failed to persist task route failure snapshot",
                    extra=self._task_route_log_extra_locked(
                        task, event="task_route_failure_persist_failed",
                    ),
                )
            log.exception(
                "completed task routing failed",
                extra=self._task_route_log_extra_locked(
                    task, event="task_route_failed",
                ),
            )
            raise

        needs_post_work = bool(task.delivery_id and int(task.route_schema_version or 0) >= 1)
        if needs_post_work:
            task.post_work_schema_version = 2
            task.post_work_id = f"{task.delivery_id}:post-work:v2"
            task.post_work_status = "pending"
            task.post_work_error = ""
            task.post_work_phases = self._build_post_work_phases_locked(task, task.post_work_id)
        task.route_status = RouteStatus.routed
        task.routed_at = _now()
        task.route_error = ""
        task.updated_at = task.routed_at
        try:
            routed_event = self._event_task_snapshot("task_routed", task)
        except Exception as exc:
            # The outbound may already be durable. Keep this phase retryable; the
            # stable delivery_id makes the next route a no-op at the append step.
            task.route_status = RouteStatus.failed
            task.routed_at = None
            task.route_error = f"task_routed persistence failed: {exc}"[:4000]
            task.updated_at = _now()
            log.exception(
                "failed to persist task_routed snapshot",
                extra=self._task_route_log_extra_locked(
                    task, event="task_routed_persist_failed",
                ),
            )
            raise
        routed_log_extra = self._task_route_log_extra_locked(task, event="task_routed")
        routed_event_data = routed_event if isinstance(routed_event, dict) else {}
        routed_log_extra.update({
            "event_id": str(routed_event_data.get("event_id") or ""),
            "event_seq": int(routed_event_data.get("seq", 0) or 0),
        })
        log.info("completed task routed", extra=routed_log_extra)
        post_work = PostCompletionWork(
            task=task.model_copy(deep=True),
            route_session_id=self._route_session_id_for_task_locked(task),
            session_generation=task.session_generation,
            post_work_id=task.post_work_id,
            schema_version=int(task.post_work_schema_version or 2),
            phases=copy.deepcopy(task.post_work_phases),
        )
        if needs_post_work:
            try:
                self.eventlog.append(
                    session_id=post_work.route_session_id or post_work.task.session_id,
                    component="supervisor",
                    type_="task_post_work_pending",
                    payload=self._post_work_status_payload(post_work, status="pending"),
                )
                for phase in post_work.phases:
                    self.eventlog.append(
                        session_id=post_work.route_session_id or post_work.task.session_id,
                        component="supervisor",
                        type_="task_post_phase_intent",
                        payload=self._post_phase_payload(post_work, phase, status="pending"),
                    )
                    self._post_phase_states.setdefault(str(phase.get("phase_id") or ""), "pending")
            except Exception:
                # task_routed already contains the full durable intent, so this
                # observability event is optional and must not roll back delivery.
                log.exception(
                    "failed to append post-work pending status",
                    extra={"event": "task_post_work_pending_persist_failed", "post_work_id": post_work.post_work_id},
                )
            with self._lock:
                self._ensure_post_completion_state_locked()
                self._post_work_states.setdefault(post_work.post_work_id, "pending")
            self._enqueue_post_completion_work(post_work)

    def _route_completed_task_inner_locked(self, task: Task) -> None:
        """执行 task 完成后的实际路由逻辑。"""
        # ---- 后台压缩结果：无 caller，压缩期间用户那一轮照常在跑 ----
        if task.input.get("_compact_async"):
            self._apply_async_compact_result_locked(task)
            return

        # ---- 接话意愿判定：结果交回 HTTP 里等着的那个请求，不产生任何用户可见输出 ----
        if task.input.get("_reply_intent"):
            self.reply_intent.settle(task.task_id, reply_intent_result_from_task(task))
            return

        # ---- 压缩 dispatch 结果：compactor 子 task 完成 ----
        if self._is_compact_dispatch_result(task):
            self._apply_compact_result_locked(task)
            return

        # ---- 轮摘要 dispatch 结果：summarizer 子 task 完成 ----
        if self._is_turn_summary_result(task):
            self._apply_turn_summary_result_locked(task)
            return

        action = task.result or {}
        act = str(action.get("action") or "").strip()
        if act == "preempted":
            self._finalize_branch_task_locked(task, merge=True)
            self._route_preempted_locked(task, action)
            return
        if act == "dispatch":
            self._route_dispatch_locked(task, action)
            if task.status != TaskStatus.failed:
                return
            # 同一任务的压缩连续失败到上限，这次 dispatch 已改判为失败，按失败继续路由
            action = task.result
            act = "fail"
        if task.batch_id:
            if act in _TASK_END_ACTIONS:
                self._collect_batch_locked(task)
            return
        if act in ("finish", "ask"):
            # 先把分支合并回主历史，再在主 session 产生用户可见的回复
            self._finalize_branch_task_locked(task, merge=True)
            self._route_finish_locked(task, action)
        elif act == "fail":
            self._finalize_branch_task_locked(task, merge=True)
            self._route_fail_locked(task, action)
        elif act == "cancelled":
            self._finalize_branch_task_locked(task, merge=True)
            if (task.caller_task_id or "").strip():
                self._settle_child_locked(
                    task, self._child_cancelled_resume_data(task), action_type="cancelled",
                )


    # ------------------------------------------------------------------ #
    #  dispatch
    # ------------------------------------------------------------------ #

    def _node_is_persistent(self, node_id: str) -> bool:
        """读取 dispatch 目标节点的 persistent 声明。

        [AutoC 2026-07-09] Why: 当上游未显式传 context_mode 时，需要依据
        节点的 persistent 声明推导默认（persistent→accumulate，否则 fresh）。
        How: 复用 engine.node.load_node（system_nodes、config/nodes、defaults/nodes 的搜索顺序），
        失败时保守返回 False。Purpose: engine 侧与 supervisor 侧行为一致。
        """
        nid = str(node_id or "").strip()
        if not nid:
            return False
        try:
            from pathlib import Path
            from engine.node import load_node
            _n = load_node(Path(self.workspace_root), nid)
            return bool(_n is not None and getattr(_n, "persistent", False))
        except Exception:
            return False

    def _resolve_dispatch_context_mode(self, node_id: str, raw_mode: Any) -> str:
        """解析 dispatch 的有效 context_mode。

        [AutoC 2026-07-09] 显式传参优先；未传时按 target 节点 persistent 推导默认。
        """
        _m = str(raw_mode or "").strip()
        if _m:
            return _m
        return "accumulate" if self._node_is_persistent(node_id) else "fresh"

    def _dispatch_batch_refusal_locked(
        self, task: Task, batch: list[Any], *, has_single_target: bool = False,
    ) -> str:
        """同步批次能不能派：系统任务不许挂起等子任务，一次的项数有上限。返回拒绝原因，能派时为空串。"""
        context = task.input.get("task_context")
        if task.input.get("_system_task") or (isinstance(context, dict) and context.get("is_system_task")):
            return "同步委派被拒绝：系统任务不能挂起等待子任务的结果。"
        if len(batch) > MAX_DISPATCH_BATCH_ITEMS:
            return f"同步委派被拒绝：一次最多派出 {MAX_DISPATCH_BATCH_ITEMS} 个子任务，这次有 {len(batch)} 个。"
        if not has_single_target and not any(
            isinstance(item, dict) and str(item.get("target") or "").strip() for item in batch
        ):
            # 一个子任务都没建出来的话 caller 会停在 completed，既不挂起也不路由
            return "同步委派被拒绝：批次里没有一项写了 target，没有可派出的子任务。"
        return ""

    def _batch_child_task_context_locked(
        self,
        caller: Task,
        *,
        target: str,
        context_mode: str,
        context_key: str,
        child_session_id: str,
    ) -> dict[str, Any]:
        """批次子节点的 task_context：身份、渠道、路由提示和根会话的键照异步委派那样从发起方带过来。

        不带的话子节点没有 conversation_key，记忆读写会落到无归属的公共目录，功能接口也认不出发起人。
        """
        raw_context = caller.input.get("task_context")
        caller_context = raw_context if isinstance(raw_context, dict) else {}
        _origin, platform_auth = self._platform_auth_origin_locked(caller)
        route_hints = caller_context.get("route_hints")
        session_info = self.sessions.get(self._route_session_id_for_task_locked(caller))
        context: dict[str, Any] = {
            "channel": str(caller_context.get("channel") or getattr(session_info, "channel", "") or "").strip(),
            "platform_auth": platform_auth,
            "route_hints": dict(route_hints) if isinstance(route_hints, dict) else {},
            "session_generation": caller.session_generation,
            "is_system_task": False,
            "dispatch_context_mode": context_mode,
        }
        root_key = str(
            caller_context.get("route_conversation_key")
            or caller_context.get("conversation_key")
            or getattr(session_info, "conversation_key", "")
            or ""
        ).strip()
        if not root_key:
            return context
        if context_mode in ("fresh", "fork"):
            conversation_key = f"agent:{target}:{root_key}:{child_session_id}"
        elif context_key:
            conversation_key = f"agent:{target}:{context_key}:{root_key}"
        else:
            conversation_key = f"agent:{target}:{root_key}"
        context.update({
            "conversation_key": conversation_key,
            "parent_conversation_key": root_key,
            "route_conversation_key": root_key,
        })
        return context

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        dispatch_batch = action.get("dispatch_batch")

        # 标记父 task 正在等待压缩 dispatch
        compact_target_session_id = ""
        compact_request = self._compact_dispatch_request(task, action)
        if compact_request is not None:
            compact_target_session_id = compact_request.target_session_id
            task.input["_compact_dispatch_pending"] = True
            # 三个尺寸参数都要搬到 caller.input：_apply_compact_result_locked 只读 caller，
            # 少搬一个就等于 supervisor 用默认值收缩，与 engine 的触发阈值脱钩。
            task.input.update(compact_request.sizing_inputs())
            if compact_target_session_id:
                task.input["_compact_target_session_id"] = compact_target_session_id
            if self.compact_breaker.is_open(compact_target_session_id):
                self._fail_compact_round_locked(
                    task, compact_request.context_ref, reason="压缩熔断冷却中", in_route=True,
                )
                return
            plan, instruction = self._take_compact_plan_locked(task, compact_request)
            if not plan:
                self.compact_breaker.record_failure(compact_target_session_id)
                self._fail_compact_round_locked(
                    task, compact_request.context_ref, reason="没有可压缩的完整历史段", in_route=True,
                )
                return
            task.input["_compact_plan"] = plan
            dispatch_input["instruction"] = instruction

        created_child_ids: list[str] = []
        batch_id: str | None = None
        delivery_key = str(task.delivery_id or "").strip()
        existing_children = self._existing_dispatch_children_locked(task, delivery_key)
        # [Fork/Merge 2026-05-17] Why: descendants of an entry branch should keep
        # branch-local storage but still route user-visible events through the
        # parent session. How: compute the parent route once and propagate it in
        # child task input while leaving task.session_id unchanged. Purpose: child
        # nodes/tools do not call supervisor APIs with a soon-to-be-deleted branch.
        route_parent_session_id = self._route_session_id_for_task_locked(task)
        route_parent_for_child = route_parent_session_id if route_parent_session_id != task.session_id else ""

        # ---- 批量委派（node 或 tool 均可） ----
        if isinstance(dispatch_batch, list) and dispatch_batch:
            refusal = self._dispatch_batch_refusal_locked(task, dispatch_batch, has_single_target=bool(target))
            if refusal:
                log.warning("dispatch batch refused for task %s: %s", task.task_id[:12], refusal)
                # 只改判这次完成的结果，外层接着按失败路由：有 caller 的交给 caller，否则把错误发给用户
                self._apply_failure_locked(task, refusal)
                self._event_task_snapshot("task_completed", task, component="supervisor")
                return
            batch_id = next(
                (child.batch_id for child in existing_children.values() if child.batch_id), None,
            ) or str(uuid.uuid4())
            for idx, item in enumerate(dispatch_batch):
                if not isinstance(item, dict):
                    continue
                item_kind = str(item.get("kind") or "node").strip()
                item_target = str(item.get("target") or "").strip()
                if not item_target:
                    continue
                reused = existing_children.get(idx)
                if reused is not None and reused.batch_id == batch_id:
                    created_child_ids.append(reused.task_id)
                    continue

                if item_kind == "tool":
                    _tool_input = {
                        "arguments": item.get("arguments", {}),
                        "call_id": str(item.get("call_id") or ""),
                        # 独立 tool task 不走 ai_step 那道白名单，engine 侧要能查出
                        # 是谁派的才复核得了；缺这一项就没有可依据的授权表。
                        "origin_node_id": str(task.node_id or ""),
                        "_context_archive_session_id": str(task.input.get("child_session_id") or route_parent_session_id or task.session_id),
                    }
                    if route_parent_for_child:
                        # [Fork/Merge 2026-05-17] Why: batch tool tasks inherit
                        # the caller branch as runtime session. How: carry the
                        # parent route in input for engine.runner ToolContext.
                        # Purpose: guarded tools and progress route to parent.
                        _tool_input["parent_session_id"] = route_parent_for_child
                    if delivery_key:
                        _tool_input["_dispatch_delivery_id"] = delivery_key
                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.tool,
                        tool_name=item_target,
                        input_data=_tool_input,
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                else:
                    _batch_ctx_mode = self._resolve_dispatch_context_mode(
                        item_target, item.get("context_mode")
                    )
                    _batch_ctx_key = str(item.get("context_key") or "").strip() or None
                    _batch_input: dict[str, Any] = {
                        "instruction": str(item.get("instruction") or "").strip(),
                    }
                    if route_parent_for_child:
                        # [Fork/Merge 2026-05-17] Why: batch child nodes can run
                        # under a branch session while emitting progress or tool
                        # approvals. How: pass the durable parent as route metadata.
                        # Purpose: RunContext.emit_event targets the user session.
                        _batch_input["parent_session_id"] = route_parent_for_child
                    if _batch_ctx_key:
                        _batch_input["_context_key"] = _batch_ctx_key

                    # Child Session 隔离（Phase B）：批量委派也走 child session
                    _b_child_sid, _b_is_new = self.get_or_create_child_session(
                        task.session_id, item_target, _batch_ctx_key or "", _batch_ctx_mode,
                    )
                    _batch_input["child_session_id"] = _b_child_sid
                    _batch_input["context_mode"] = _batch_ctx_mode
                    _batch_input["use_context"] = False
                    if _batch_ctx_mode == "fork":
                        # 批次里的子节点再派批次时 session_id 还是祖先的，它自己的对话在它的 child session 里
                        _batch_input["fork_from_session_id"] = str(
                            task.input.get("child_session_id") or task.session_id
                        )
                    _batch_attachments = batch_item_attachments(item.get("attachments"))
                    if _batch_attachments:
                        _batch_input["attachments"] = _batch_attachments
                    _batch_call_id = str(item.get("call_id") or "").strip()
                    if _batch_call_id:
                        _batch_input["_dispatch_call_id"] = _batch_call_id
                    _batch_input["task_context"] = self._batch_child_task_context_locked(
                        task,
                        target=item_target,
                        context_mode=_batch_ctx_mode,
                        context_key=_batch_ctx_key or "",
                        child_session_id=_b_child_sid,
                    )
                    if delivery_key:
                        _batch_input["_dispatch_delivery_id"] = delivery_key

                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.node,
                        node_id=item_target,
                        input_data=_batch_input,
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                created_child_ids.append(child.task_id)
            if not created_child_ids:
                batch_id = None

        # ---- 单节点委派 ----
        reused_single = existing_children.get(-1)
        if not created_child_ids and target and reused_single is not None:
            created_child_ids.append(reused_single.task_id)
        elif not created_child_ids and target:
            # [AutoC 2026-07-09] Why: compact dispatch 等系统任务已显式传 context_mode；
            # 其余单节点委派未传时按 target persistent 推导（persistent→accumulate，
            # 否则 fresh），避免普通节点默认持久化。
            _ctx_mode = self._resolve_dispatch_context_mode(
                target, dispatch_input.get("context_mode")
            )
            _ctx_key = str(dispatch_input.get("context_key") or "").strip() or None
            _single_input: dict[str, Any] = {
                "instruction": str(dispatch_input.get("instruction") or "").strip(),
            }
            if route_parent_for_child:
                # [Fork/Merge 2026-05-17] Why: a synchronous child task should be
                # branch-local for merge semantics but parent-routed for events and
                # session tools. How: pass parent_session_id without changing the
                # child task's runtime session. Purpose: avoid hidden branch events.
                _single_input["parent_session_id"] = route_parent_for_child
            if _ctx_key:
                _single_input["_context_key"] = _ctx_key

            # Child Session 隔离（Phase B）：用 child session 替代 context_ref
            # [AutoC 2026-05-13] Why: system.compactor is a child node, but its
            # output must target the parent session when the caller is a branch.
            # How: for compact dispatch only, key the child session and child task
            # under compact_target_session_id. Purpose: compactor bookkeeping and
            # result application stay attached to the durable parent session.
            _dispatch_session_id = compact_target_session_id or task.session_id
            _child_sid, _is_new = self.get_or_create_child_session(
                _dispatch_session_id, target, _ctx_key or "", _ctx_mode,
            )
            _single_input["child_session_id"] = _child_sid
            _single_input["context_mode"] = _ctx_mode
            _single_input["use_context"] = False  # child session 模式不走 _fetch_history
            if _ctx_mode == "fork":
                # fork 模式需要告诉 engine 从哪个 session 复制历史
                _single_input["fork_from_session_id"] = _dispatch_session_id
            if delivery_key:
                _single_input["_dispatch_delivery_id"] = delivery_key

            child = self._create_task_locked(
                session_id=_dispatch_session_id,
                session_generation=task.session_generation,
                kind=TaskKind.node,
                node_id=target,
                input_data=_single_input,
                continuation={},
                source_inbound_seq=task.source_inbound_seq,
                caller_task_id=task.task_id,
            )
            created_child_ids.append(child.task_id)

        # ---- 挂起当前 task ----
        if created_child_ids:
            task.status = TaskStatus.suspended
            task.waiting_for_batch_id = batch_id
            task.waiting_for_task_id = None if batch_id else created_child_ids[0]
            task.updated_at = _now()
            self._event_task_snapshot("task_suspended", task)

    def _existing_dispatch_children_locked(self, task: Task, delivery_key: str) -> dict[int, Task]:
        """同一次完成已经派出的子任务，按批次序号索引（单节点委派记 -1）。

        路由在派出部分子任务后抛异常时，重推这次完成要接着用它们，不能再派一遍。
        """
        if not delivery_key:
            return {}
        return {
            (child.batch_index if child.batch_id else -1): child
            for child in self.tasks.values()
            if child.caller_task_id == task.task_id
            and child.input.get("_dispatch_delivery_id") == delivery_key
        }

    # ------------------------------------------------------------------ #
    #  preempted
    # ------------------------------------------------------------------ #

    def _route_preempted_locked(self, task: Task, action: dict[str, Any]) -> None:
        """处理 preempted 结果：保存 context_ref，发射事件，恢复 caller（如有）。"""
        context_ref = str(action.get("context_ref") or "")
        task.preempted_context_ref = context_ref

        # [Fork/Merge 2026-05-17] Why: preempt notifications are user-visible,
        # while task.session_id can be a temporary branch. How: route the event to
        # the parent session when task metadata provides one. Purpose: adapters
        # polling the durable session can observe the preempted task.
        route_session_id = self._route_session_id_for_task_locked(task)
        self.eventlog.append(
            session_id=route_session_id,
            component="supervisor",
            type_="task_preempted",
            payload={
                "task_id": task.task_id,
                "node_id": task.node_id,
                "context_ref": context_ref,
                "session_id": route_session_id,
                "runtime_session_id": task.session_id,
            },
            transient=True,
        )

        # 子节点被 preempt 也是它这一程的终点：单独派出的直接唤醒 caller，批次里的等兄弟都结束
        if (task.caller_task_id or "").strip():
            self._settle_child_locked(task, {
                "type": "child_preempted",
                "child_node_id": str(task.node_id or ""),
                "context_ref": context_ref,
            }, action_type="preempted")

        # 不输出给用户，不注入 inbound

    # ------------------------------------------------------------------ #
    #  finish / ask / fail
    # ------------------------------------------------------------------ #

    def _route_finish_locked(self, task: Task, action: dict[str, Any]) -> None:
        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        action_type = str(action.get("action") or "finish").strip() or "finish"
        self._resume_caller_or_output_locked(task, {
            "type": "child_result",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
            # [AutoC 2026-05-31] Why: Phase 0 routes ask like finish, but the
            # downstream resume payload must retain the original terminal action
            # for Phase 1 topology routing. How: add action_type metadata while
            # leaving the result payload unchanged. Purpose: callers can tell ask
            # from finish without a separate route implementation yet.
            "action_type": action_type,
        }, result, action_type=action_type)


    def _route_fail_locked(self, task: Task, action: dict[str, Any]) -> None:
        error = str(action.get("error") or "未知错误").strip()
        self._resume_caller_or_output_locked(task, {
            "type": "child_failed",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "error": error,
        }, {"text": f"[错误] {error}"}, action_type="fail")

    # ------------------------------------------------------------------ #
    #  子任务结算：唤醒在等它的调用方
    # ------------------------------------------------------------------ #

    @staticmethod
    def _task_outcome_action(task: Task) -> str:
        """任务这一程的结局。被取消的任务 result 里可能还是 worker 回报的原动作，以状态为准。"""
        if task.status == TaskStatus.cancelled:
            return "cancelled"
        return str((task.result or {}).get("action") or "").strip()

    @staticmethod
    def _child_cancelled_resume_data(task: Task) -> dict[str, Any]:
        return {
            "type": "child_cancelled",
            "child_task_id": task.task_id,
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "error": "child task cancelled",
        }

    def _caller_can_resume_locked(self, caller: Task) -> bool:
        if caller.status != TaskStatus.suspended or caller.cancel_requested:
            return False
        route_session_id = self._route_session_id_for_task_locked(caller)
        if route_session_id in self._cancelled_sessions:
            return False
        current_generation = self._current_session_generation_locked(route_session_id)
        return not current_generation or caller.session_generation == current_generation

    def _caller_waits_for_child_locked(self, caller: Task | None, child: Task) -> bool:
        return (
            caller is not None
            and self._caller_can_resume_locked(caller)
            and caller.waiting_for_task_id == child.task_id
        )

    def _caller_waits_for_batch_locked(self, caller: Task | None, child: Task) -> bool:
        if caller is None or not self._caller_can_resume_locked(caller):
            return False
        if caller.waiting_for_batch_id:
            return caller.waiting_for_batch_id == child.batch_id
        # 旧快照没有批次等待字段：多项批次挂起时两个等待字段都为空，单项批次记在 waiting_for_task_id 上
        return not caller.waiting_for_task_id or caller.waiting_for_task_id == child.task_id

    def _log_dropped_child_result_locked(self, child: Task, caller: Task | None) -> None:
        log.warning(
            "child result dropped: caller %s is not waiting for task %s "
            "(caller status=%s, waiting_for=%s, waiting_for_batch=%s, child outcome=%s)",
            (child.caller_task_id or "")[:12],
            child.task_id[:12],
            caller.status.value if caller is not None else "missing",
            ((caller.waiting_for_task_id or "") if caller is not None else "")[:12],
            ((caller.waiting_for_batch_id or "") if caller is not None else "")[:12],
            self._task_outcome_action(child),
        )

    def _settle_child_locked(
        self, child: Task, resume_data: dict[str, Any], *, action_type: str = "",
    ) -> None:
        """子任务到了终态：单独派出的直接唤醒 caller，批次里的等兄弟都结束再一起唤醒。

        caller 不在等它（已被回收、取消，或正在等别的子任务）时只记日志，结果绝不改投给用户。
        """
        if child.batch_id:
            self._collect_batch_locked(child)
            return
        caller = self.tasks.get((child.caller_task_id or "").strip())
        if not self._caller_waits_for_child_locked(caller, child):
            self._log_dropped_child_result_locked(child, caller)
            return
        self._resume_suspended_task_locked(caller, resume_data, action_type=action_type)

    def _collect_batch_locked(self, child: Task) -> None:
        """批次里有一个子任务到了终态：兄弟全部结束后打包结果恢复 caller，不区分 tool/node。"""
        caller = self.tasks.get((child.caller_task_id or "").strip())
        if not self._caller_waits_for_batch_locked(caller, child):
            self._log_dropped_child_result_locked(child, caller)
            return
        siblings = sorted(
            (
                sibling for sibling in self.tasks.values()
                if sibling.batch_id == child.batch_id and sibling.caller_task_id == caller.task_id
            ),
            key=lambda sibling: sibling.batch_index,
        )
        if any(not self._task_terminal(sibling) for sibling in siblings):
            return
        self._resume_suspended_task_locked(caller, self._batch_resume_data(siblings))

    def _batch_resume_data(self, siblings: list[Task]) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for t in siblings:
            tr = t.result.get("result") if isinstance(t.result.get("result"), dict) else {}
            act = self._task_outcome_action(t)

            entry: dict[str, Any] = {
                "kind": t.kind.value,
                "status": act,
                "summary": str(t.result.get("summary") or tr.get("summary") or ""),
            }

            if t.kind == TaskKind.node:
                entry["node_id"] = str(t.node_id or "")
                entry["instruction"] = str(t.input.get("instruction") or "")
                entry["text"] = str(tr.get("text") or "")
                entry["child_task_id"] = t.task_id
                for source_key, entry_key in (("child_session_id", "child_session_id"), ("_dispatch_call_id", "call_id")):
                    value = str(t.input.get(source_key) or "").strip()
                    if value:
                        entry[entry_key] = value
            elif t.kind == TaskKind.tool:
                entry["name"] = str(t.tool_name or tr.get("tool_name") or "")
                entry["args"] = dict(tr.get("arguments") or t.input.get("arguments") or {})
                entry["format"] = str(tr.get("raw_format") or tr.get("format") or "json")
                entry["raw_inline"] = str(tr.get("raw_inline") or tr.get("text") or "")
                entry["truncated"] = bool(tr.get("truncated", False))
                entry["ref"] = str(tr.get("ref") or "")

            if act in ("fail", "cancelled"):
                entry["error"] = str(t.result.get("error") or "")

            atts = tr.get("attachments")
            if isinstance(atts, list):
                entry["attachments"] = atts

            entries.append(entry)
        return {"type": "batch_results", "entries": entries}

    def _settle_cancelled_task_locked(self, task: Task, *, finalize_branch: bool = True) -> None:
        """supervisor 直接终结的取消：收束分支、拆临时会话，并像正常完成一样结算调用方。"""
        if finalize_branch:
            self._schedule_cancelled_task_teardown_locked(task)
        if not (task.caller_task_id or "").strip():
            return
        if self._is_compact_dispatch_result(task):
            self._apply_compact_result_locked(task)
            return
        self._settle_child_locked(
            task, self._child_cancelled_resume_data(task), action_type="cancelled",
        )

    def _schedule_cancelled_task_teardown_locked(self, task: Task) -> None:
        has_branch = bool(task.input.get("branch_session_id")) and not task.input.get("_branch_finalized")
        has_sessions = bool(task.input.get("_dispatch_origin") or task.input.get("child_session_id"))
        if not (has_branch or has_sessions) or task.input.get("_branch_finalize_worker_started"):
            return
        task.input["_branch_finalize_worker_started"] = True
        threading.Thread(
            target=self._finish_cancelled_task,
            args=(task,),
            daemon=True,
            name=f"cancel-teardown-{task.task_id[:8]}",
        ).start()

    def _finish_cancelled_task(self, task: Task) -> None:
        """取消终结之后的收尾：先把分支合并回去，再拆这个任务独占的 fresh/fork 会话。"""
        try:
            if task.input.get("branch_session_id") and not task.input.get("_branch_finalized"):
                if self._finalize_branch_task(task, merge=True) is None:
                    return
            with self._lock:
                work = PostCompletionWork(
                    task=task.model_copy(deep=True),
                    route_session_id=self._route_session_id_for_task_locked(task),
                    session_generation=task.session_generation,
                )
            self._execute_post_cleanup(work, {"phase_id": f"task:{task.task_id}:cancel-cleanup", "kind": "cleanup"})
        except Exception:
            log.exception("cancelled task teardown failed", extra={"task_id": task.task_id})

    # ------------------------------------------------------------------ #
    #  公共：恢复 caller 或输出给用户
    # ------------------------------------------------------------------ #

    def _resume_caller_or_output_locked(
        self,
        task: Task,
        resume_data: dict[str, Any],
        fallback_result: dict[str, Any],
        *,
        action_type: str = "",
    ) -> None:
        """有 caller 的子任务交给 caller 结算；没有 caller 的把 fallback_result 输出给用户。"""
        if (task.caller_task_id or "").strip():
            self._settle_child_locked(task, resume_data, action_type=action_type)
            return

        if task.input.get("_dispatch_origin"):
            self._inject_dispatch_result_via_origin_locked(task, fallback_result)
            return

        # 系统内部任务（如记忆提取）静默完成，不输出给用户
        if task.input.get("_system_task"):
            return
        if not isinstance(fallback_result, dict):
            fallback_result = {}
        text = str(fallback_result.get("text") or fallback_result.get("summary") or "").strip()
        atts = fallback_result.get("attachments") if isinstance(fallback_result.get("attachments"), list) else None
        # [Fix] finish(text="") 空文本时也产出 outbound_message 事件。
        # 原先 `if text or atts:` 导致空文本 finish 不发事件，
        # Bot 侧 trigger 和 status_msg（含 stream_delta 累积的原始标记预览）永远无法清理。
        # 去掉守卫后，Bot 侧 send_reply 会跳过平台发送但正常执行清理收尾。
        # [fix 2026-07-17] 路由竞态防护：任务完成到这里时，目标 session 可能已被
        # 合并/清理（例如 branch 已 merge 回主 session、或 fresh/fork dispatch session
        # 已删除），此时 append_outbound_message 会抛 KeyError('session not found')并
        # 中断整个 task_complete 路由。做法：投递前先判断 session 是否存在，不存在
        # 则记日志并跳过，避免一条已无处可送的回复拖垮后续完成处理。
        _route_sid = self._route_session_id_for_task_locked(task)
        if _route_sid not in self.sessions:
            log.warning(
                "skip outbound for task %s: route session %s no longer exists "
                "(likely merged/cleaned up)", task.task_id, _route_sid,
            )
            return
        self.append_outbound_message(
            session_id=_route_sid, text=text,
            attachments=atts, source_inbound_seq=task.source_inbound_seq,
            node_id=task.node_id,
            # engine 的不发言结果只有这个标记，出站消息照它标出来，适配层据此什么也不发
            action_type=OUTBOUND_NO_REPLY if fallback_result.get(OUTBOUND_NO_REPLY) is True else action_type,
            llm_request_id=str(task.result.get("llm_request_id") or "").strip(),
            delivery_id=task.delivery_id,
            route_hints=(task.input.get("task_context") or {}).get("route_hints"),
        )


    def _emit_dispatch_attachment_outbound_locked(
        self,
        *,
        session_id: str,
        conversation_key: str,
        task: Task,
        attachments: list[Any] | None,
    ) -> bool:
        """Emit child-generated attachments as a standard outbound_message.

        """
        if not session_id or not attachments:
            return False
        if session_id not in self.sessions:
            return False
        node_id = str(task.node_id or task.tool_name or "").strip()
        attachment_delivery_id = (
            f"{task.delivery_id}:dispatch-attachment" if task.delivery_id else ""
        )
        if attachment_delivery_id and self.eventlog.find_outbound_delivery(attachment_delivery_id):
            return True
        payload: dict[str, Any] = {
            "text": "",
            "attachments": list(attachments),
            "message_type": "dispatch_attachment",
            "action_type": "dispatch_attachment",
            "child_task_id": task.task_id,
            "child_node_id": node_id,
        }
        hints = (task.input.get("task_context") or {}).get("route_hints") or {}
        for key in ("delivery_purpose", "reply_message_id"):
            if key in hints:
                payload[key] = hints[key]
        if attachment_delivery_id:
            payload["delivery_id"] = attachment_delivery_id
        child_session_id = str(task.input.get("child_session_id") or "").strip()
        if child_session_id:
            payload["child_session_id"] = child_session_id
        if node_id:
            payload["node_id"] = node_id
        if conversation_key:
            # Helpful for adapter restarts or state loss; SDK still falls back to
            # session_id → conversation_key mapping for older outbound events.
            payload["conversation_key"] = conversation_key
        evt = self.eventlog.append(
            session_id=session_id,
            component="supervisor",
            type_="outbound_message",
            payload=payload,
        )
        try:
            self._apply_outbound_message(
                seq=int(evt.get("seq", 0) or 0),
                session_id=session_id,
                payload=payload,
            )
        except Exception as exc:
            log.warning("dispatch attachment outbound apply failed: %s", exc)
        return True

    # ------------------------------------------------------------------ #
    #  异步委派结果回调
    # ------------------------------------------------------------------ #

    def _inject_dispatch_result_via_origin_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """异步委派的子任务完成后，把结果作为新 inbound 注入 dispatch_origin 记的发起方会话。

        回调目标是 dispatch_origin.parent_session_id，不是子任务自己的 route session。
        此方法在 self._lock 内调用。
        """
        origin = task.input.get("_dispatch_origin") or {}
        target_session_id = str(origin.get("parent_session_id") or "").strip()
        caller_node = str(origin.get("caller_node_id") or "").strip()
        child_session_id = str(task.input.get("child_session_id") or "").strip()

        if not target_session_id:
            log.warning(
                "dispatch_origin callback skipped: no parent_session_id in task %s",
                task.task_id[:12],
            )
            return

        session_info = self.sessions.get(target_session_id)
        if not session_info:
            log.warning(
                "dispatch_origin callback skipped: session %s not found for task %s",
                target_session_id[:12], task.task_id[:12],
            )
            return

        # 旧任务没有父代次快照，继续沿用原来的保守校验。
        parent_generation = self._to_positive_int(origin.get("parent_session_generation"))
        if parent_generation is None:
            parent_generation = task.session_generation
        current_gen = self._current_session_generation_locked(target_session_id)
        if parent_generation and current_gen and parent_generation != current_gen:
            log.warning(
                "dispatch_origin callback skipped: session %s generation %s != %s for task %s",
                target_session_id[:12], parent_generation, current_gen, task.task_id[:12],
            )
            return
        if target_session_id in self._cancelled_sessions:
            log.warning(
                "dispatch_origin callback skipped: session %s cancelled for task %s",
                target_session_id[:12], task.task_id[:12],
            )
            return

        result_text = str(fallback_result.get("text") or "").strip()
        result_summary = str(fallback_result.get("summary") or "").strip()

        result_atts = (
            fallback_result.get("attachments")
            if isinstance(fallback_result.get("attachments"), list)
            else None
        )

        # 构造 inbound payload 并注入目标 session
        conv_key = session_info.conversation_key
        channel = session_info.channel
        msg_id = f"dispatch_origin:{task.task_id}"
        attachments_outbound_sent = self._emit_dispatch_attachment_outbound_locked(
            session_id=target_session_id,
            conversation_key=conv_key,
            task=task,
            attachments=result_atts,
        ) if result_atts else False

        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conv_key,
            "message_id": msg_id,
            "text": result_text,
            "summary": result_summary,
            "message_type": "dispatch_result",
            "caller_node_id": caller_node,
            "child_node_id": task.node_id,
            "child_task_id": task.task_id,
            "child_session_id": child_session_id,
        }
        # [2026-07-15] Why: 异步 dispatch 结果回调以新 inbound 注入时，若不指定
        # entry_node_id，_create_entry_task_for_inbound_locked 会回退到 session 最后绑定
        # 的入口节点（如上一次被 dispatch 目标写入的 chat.web_search），导致回调被错误
        # 路由到不具备推送能力的节点，异步结果无法发出。How: 回调明确指定
        # entry_node_id 为发起 dispatch 的 caller 节点（orchestrator）。Purpose: 异步
        # 结果回到原编排节点，由它决定如何向用户推送。
        if caller_node:
            payload["entry_node_id"] = caller_node
        output_guide = self._inherited_output_guide_locked(origin.get("caller_task_id"))
        if output_guide:
            payload[OUTPUT_GUIDE_KEY] = output_guide
        if attachments_outbound_sent:
            payload["attachments_outbound_sent"] = True
        elif result_atts:
            payload["attachments"] = result_atts

        callback_delivery_id = (
            f"{task.delivery_id}:caller-resume" if task.delivery_id else ""
        )
        if callback_delivery_id:
            payload["delivery_id"] = callback_delivery_id
            if self.eventlog.find_delivery_event(
                callback_delivery_id, event_type="inbound_message",
            ):
                return

        evt = self.eventlog.append(
            session_id=target_session_id,
            component="supervisor",
            type_="inbound_message",
            payload=payload,
        )
        seq = int(evt.get("seq", 0))
        self._apply_inbound_message(seq=seq, session_id=target_session_id, payload=payload)
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=target_session_id, payload=payload,
        )

    def _async_result_origin_guard_locked(
        self, session_id: str, task_id: str,
    ) -> dict[str, Any] | None:
        """Drop late async results before they can create a new inbound task."""
        if session_id in self._cancelled_sessions:
            return {"ok": True, "strategy": "dropped", "reason": "session_cancelled"}
        if not task_id:
            return None

        origin = self.tasks.get(task_id)
        if origin is None:
            return {"ok": True, "strategy": "dropped", "reason": "origin_not_found"}
        if origin.cancel_requested or origin.status == TaskStatus.cancelled:
            return {"ok": True, "strategy": "dropped", "reason": "origin_cancelled"}

        route_session_id = self._route_session_id_for_task_locked(origin) or session_id
        if route_session_id != session_id:
            return {"ok": True, "strategy": "dropped", "reason": "route_mismatch"}
        if route_session_id in self._cancelled_sessions:
            return {"ok": True, "strategy": "dropped", "reason": "route_session_cancelled"}
        current_generation = self._current_session_generation_locked(route_session_id)
        if (
            origin.session_generation
            and current_generation
            and origin.session_generation != current_generation
        ):
            return {"ok": True, "strategy": "dropped", "reason": "stale_generation"}
        return None

    def inject_async_result(
        self,
        session_id: str,
        text: str,
        attachments: list | None = None,
        *,
        node_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        success: bool | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """注入异步工具结果到 session，并创建新的 inbound 分支。

        [Fork/Merge 2026-05-12] 旧的 running/suspended 自动 preempt 回退被移除。
        原因：新架构要求只有 adapter 显式 preempt API 才能抢占任务。
        做法：异步工具结果统一创建 inbound。目的：保持入口分支并发运行。
        """
        with self._lock:
            session_info = self.sessions.get(session_id)
            if not session_info:
                return {"ok": False, "error": "session not found"}

            result_atts = list(attachments or [])
            event_node_id = str(node_id or "").strip()
            event_task_id = str(task_id or "").strip()
            guard_result = self._async_result_origin_guard_locked(session_id, event_task_id)
            if guard_result is not None:
                log.warning(
                    "async tool result dropped: session=%s task=%s reason=%s",
                    session_id[:12], event_task_id[:12], guard_result["reason"],
                )
                return guard_result

            # Successful media tools keep the existing direct-outbound fast path.
            # A failed result must deliver its failure text/metadata instead of
            # silently sending any incidental attachment and dropping the error.
            if result_atts and success is not False:
                payload: dict[str, Any] = {
                    "text": "",
                    "attachments": [{"path": p} for p in result_atts],
                    "message_type": "async_tool_attachment",
                    "action_type": "async_tool_attachment",
                }
                if event_node_id:
                    payload["node_id"] = event_node_id
                if event_task_id:
                    payload["task_id"] = event_task_id
                    origin = self.tasks.get(event_task_id)
                    hints = ((origin.input.get("task_context") or {}).get("route_hints") or {}) if origin else {}
                    for key in ("delivery_purpose", "reply_message_id"):
                        if key in hints:
                            payload[key] = hints[key]
                tool = str(tool_name or "").strip()
                if tool:
                    payload["tool_name"] = tool
                conv_key = session_info.conversation_key
                if conv_key:
                    payload["conversation_key"] = conv_key
                evt = self.eventlog.append(
                    session_id=session_id,
                    component="supervisor",
                    type_="outbound_message",
                    payload=payload,
                )
                try:
                    self._apply_outbound_message(
                        seq=int(evt.get("seq", 0) or 0),
                        session_id=session_id,
                        payload=payload,
                    )
                except Exception as exc:
                    log.warning("async tool attachment outbound apply failed: %s", exc)
                return {"ok": True, "strategy": "outbound_attachment"}

            # [Fork/Merge 2026-05-12] 外部异步结果注入不再自动 preempt。
            # 原因：preempt 语义收窄为 adapter 显式请求；内部注入应像普通 inbound 一样
            # fork 新分支。做法：删除 running/suspended 自动抢占路径。目的：避免新 inbound
            # 破坏正在运行的入口分支。

            # ---- 创建 inbound → 新 branch task ----
            conv_key = session_info.conversation_key
            channel = session_info.channel
            msg_id = f"async_tool_result:{session_id}"

            payload = {
                "channel": channel,
                "conversation_key": conv_key,
                "message_id": msg_id,
                "text": text,
            }
            if event_node_id:
                payload["node_id"] = event_node_id
            if event_task_id:
                payload["task_id"] = event_task_id
            tool = str(tool_name or "").strip()
            if tool:
                payload["tool_name"] = tool
            if success is not None:
                payload["route_hints"] = {
                    "async_tool_success": success,
                    "async_tool_failed": not success,
                    "async_tool_name": tool,
                    "async_tool_error": str(error)[:1000] if error else "",
                }
            output_guide = self._inherited_output_guide_locked(event_task_id)
            if output_guide:
                payload[OUTPUT_GUIDE_KEY] = output_guide

            evt = self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="inbound_message",
                payload=payload,
            )
            seq = int(evt.get("seq", 0))
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._create_entry_task_for_inbound_locked(
                inbound_seq=seq, session_id=session_id, payload=payload,
            )
            return {"ok": True, "strategy": "inbound"}

    def _inherited_output_guide_locked(self, task_id: Any) -> dict[str, Any] | None:
        """转述结果的那一轮沿用发起任务的输出说明：回的还是同一个平台上的同一个会话。"""
        origin = self.tasks.get(str(task_id or "").strip())
        context = origin.input.get("task_context") if origin is not None else None
        guide = context.get(OUTPUT_GUIDE_KEY) if isinstance(context, dict) else None
        return dict(guide) if isinstance(guide, dict) else None

    @staticmethod
    def _is_user_entry_task(task: Task) -> bool:
        """用户消息直接建出来的入口任务。

        派出去的子任务不算（同步委派有 caller，异步委派的执行体带 dispatch_origin），系统任务和定时任务也不算。
        running_tasks 的 is_user_entry 用的也是这一份，适配器据它决定新消息抢占哪个任务。
        """
        task_input = task.input if isinstance(task.input, dict) else {}
        return not (
            (task.caller_task_id or "").strip()
            or task_input.get("_dispatch_origin")
            or task_input.get("_system_task")
            or task_input.get("schedule_id")
        )

    def _find_entry_task_by_status_locked(self, session_id: str, statuses: set) -> Task | None:
        """在 session 中查找指定状态的入口 task，口径同 _is_user_entry_task。

        若存在多个符合条件的 task（理论上不应发生），返回最早创建的。
        """
        candidate: Task | None = None
        for t in self.tasks.values():
            if t.session_id != session_id or t.status not in statuses or not self._is_user_entry_task(t):
                continue
            if candidate is None or t.created_at < candidate.created_at:
                candidate = t
        return candidate

    def _find_running_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 running 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.running})

    def _find_suspended_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 suspended 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.suspended})

    # ------------------------------------------------------------------ #
    #  压缩 dispatch 结果处理
    # ------------------------------------------------------------------ #

    def _is_compact_dispatch_result(self, task: Task) -> bool:
        """判断是否是 compactor 子 task 的完成事件。"""
        caller_id = (task.caller_task_id or "").strip()
        if not caller_id:
            return False
        caller = self.tasks.get(caller_id)
        if not caller:
            return False
        return bool(caller.input.get("_compact_dispatch_pending"))

    @staticmethod
    def _compact_sizing(source: dict[str, Any]) -> dict[str, int]:
        """Read the sizing parameters an engine compact dispatch carried over.

        threshold 缺失时回落 256000 是历史兜底：engine 现在会显式发这个字段，
        六处调用共用这一份读取，避免再次出现「engine 按 A 触发、supervisor 按 B 收缩」。
        """
        return {
            "keep_recent": int(source.get("_compact_keep_recent", 6) or 6),
            "keep_recent_tokens": int(source.get("_compact_keep_recent_tokens", 0) or 0),
            "threshold_tokens": int(source.get("_compact_threshold_tokens", 0) or 256000),
        }

    def _session_chars_per_token(self, session_id: str, messages: list[Any]) -> float:
        """本会话的字符/token 比，用这段对话自己那次请求的真实 prompt_tokens 校准。

        少了这一步就按 3 字符/token 算，中文会把 keep_recent_tokens 的预算放大两三倍，
        压完仍超阈值，下一轮又压一次。engine 上报 usage 时会带同一批消息的字符数；
        拿不到时退而用 store 里的历史字符数比 —— 它少了 system 段，比例偏小、偏保守。
        """
        from engine.compact import DEFAULT_CHARS_PER_TOKEN
        from .context_calibration import calibrated_chars_per_token

        ratio = calibrated_chars_per_token(self.session_calibration(session_id), messages)
        return DEFAULT_CHARS_PER_TOKEN if ratio is None else ratio

    def _session_counts_reasoning(self, session_id: str) -> bool:
        """校准那次请求把历史思维链发回去了：按这个比例估历史时思维链也得算进字数。"""
        from .context_calibration import counts_reasoning

        return counts_reasoning(self.session_calibration(session_id))

    def _apply_snip_via_conv_store(self, session_id: str, task_ids: set[str]) -> dict[str, Any]:
        """用 TaskRecord 摘要替换已结束任务的原文。不要求持锁，会话文件按 ConversationChangedError 乐观重试。"""
        from engine.compaction_plan import record_fingerprint, record_task_id
        from engine.context_archive import ContextArchiveStore
        from engine.conversation_store import ConversationChangedError, ConversationStore
        from engine.task_record import load_task_records, safe_snip_task_ids, snip_store

        records = load_task_records(self.workspace_root, session_id)
        summarized_ids = {
            record.task_id for record in records
            if isinstance(record.summary, str) and record.summary.strip()
        }
        for _ in range(3):
            store = ConversationStore(self.workspace_root / "data" / "conversations")
            messages = store.load(session_id)
            already_snipped = {
                record_task_id(message) for message in messages
                if message.message_type == "summary" or (
                    isinstance(message.content, str) and message.content.startswith("[Task summary — original messages snipped]")
                )
            }
            eligible = (task_ids & summarized_ids & {record_task_id(message) for message in messages}) - already_snipped
            eligible = safe_snip_task_ids(messages, eligible)
            if not eligible:
                return {"applied": False, "reason": "no eligible task summaries"}
            selected = [
                {"index": index, "fingerprint": record_fingerprint(message)}
                for index, message in enumerate(messages) if record_task_id(message) in eligible
            ]
            plan = {
                "version": 1, "source_length": len(messages), "indexed_records": selected,
                "fingerprints": [item["fingerprint"] for item in selected],
            }
            compressed = snip_store(messages, records, eligible)
            ContextArchiveStore(self.workspace_root).archive(session_id, messages, reason="task_snip")
            try:
                store.replace_all(session_id, compressed)
            except ConversationChangedError:
                continue
            summaries = {
                record_task_id(message): message for message in compressed
                if record_task_id(message) in eligible and message.message_type == "summary"
            }
            synchronized = self._rewrite_branch_prefixes(
                session_id,
                functools.partial(self._snip_branch_prefix, plan=plan, summaries=summaries),
                reason="branch_task_snip",
            )
            return {
                "applied": synchronized, "before": len(messages), "after": len(compressed),
                "snipped_tasks": len(eligible),
                "snipped_task_ids": sorted(eligible),
            }
        return {"applied": False, "reason": "conversation changed during task compaction"}

    @staticmethod
    def _snip_branch_prefix(
        prefix: list[Any], *, plan: dict[str, Any], summaries: dict[str, Any],
    ) -> list[Any] | None:
        from engine.compaction_plan import covered_indices, record_task_id

        indices = covered_indices(prefix, plan)
        if indices is None:
            return None
        selected = set(indices)
        replaced: list[Any] = []
        seen: set[str] = set()
        for index, message in enumerate(prefix):
            if index not in selected:
                replaced.append(message)
                continue
            task_id = record_task_id(message)
            if task_id not in seen:
                replaced.append(summaries[task_id])
                seen.add(task_id)
        return replaced

    def _apply_view_marks_via_conv_store(
        self,
        target_session_id: str,
        marks: dict[str, tuple[str, dict[str, Any]]],
        *,
        branch_session_id: str = "",
        reason: str = "view_marks",
    ) -> dict[str, Any]:
        """把旧工具结果的截短、让位、清空标记合进父会话，再同步到各活跃入口分支的父历史前缀。

        不要求持锁，会话文件按 ConversationChangedError 乐观重试。只改 meta、原文还在，不归档。
        marks 是这些 id 合并后生效的标记；branch_synchronized 说的是 branch_session_id 那个分支
        （没给就是全部活跃分支）的副本有没有补上同一批标记。
        """
        from engine.conversation_store import ConversationStore
        from engine.view_marks import apply_view_marks_to_store, mark_counts, marked_prefix

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        effective = apply_view_marks_to_store(store, target_session_id, marks)
        if effective is None:
            return {
                "applied": False, "synchronized": False, "branch_synchronized": False, "marks": {},
                "reason": "conversation changed during view marking",
            }
        synced = {
            message_id: (marks[message_id][0], mark)
            for message_id, mark in effective.items() if message_id in marks
        }
        if not synced:
            return {"applied": False, "synchronized": True, "branch_synchronized": True, "marks": {}}
        sync = self._sync_branch_prefixes(
            target_session_id,
            functools.partial(marked_prefix, marks=synced),
            reason="branch_view_marks",
            archive=False,
        )
        branch = str(branch_session_id or "").strip()
        branch_synchronized = branch in sync.branches if branch else sync.synchronized
        counts = mark_counts(effective.values())
        log.info(
            "view marks (%s) on %s: truncated=%d superseded=%d cleared=%d, branches synchronized=%s",
            reason, target_session_id, counts["truncated"], counts["superseded"], counts["cleared"],
            sync.synchronized,
        )
        return {
            "applied": True, "synchronized": sync.synchronized,
            "branch_synchronized": branch_synchronized, "marks": effective,
        }

    def _active_task_ids_for_session_locked(self, session_id: str) -> set[str]:
        """还没结束、历史落在这个会话上的任务：直接跑在上面的、跑在它入口分支上的、把它当子会话的。"""
        active: set[str] = set()
        for task in self.tasks.values():
            if self._task_terminal(task):
                continue
            homes = (
                task.session_id, task.input.get("parent_session_id"),
                task.input.get("branch_session_id"), task.input.get("child_session_id"),
            )
            if session_id in homes:
                active.add(task.task_id)
        return active

    def _mark_compacted_history(self, target_session_id: str, *, threshold_tokens: int) -> None:
        """LLM 压缩刚换掉父会话的前缀，缓存本来就整段不命中：顺带把留下的旧工具结果的标记推到底。

        尽力而为，出错只记日志；还没结束的任务自己的消息不打标记。
        """
        from engine.compact import is_tool_result_message
        from engine.conversation_store import ConversationStore
        from engine.view_marks import keyed_marks, mark_settings, plan_view_marks

        try:
            store = ConversationStore(self.workspace_root / "data" / "conversations")
            records = [message.to_dict() for message in store.load(target_session_id)]
            if not any(is_tool_result_message(record) for record in records):
                return
            with self._lock:
                active = self._active_task_ids_for_session_locked(target_session_id)
            settings = mark_settings(
                load_runtime_config(self.workspace_root),
                compact_threshold=threshold_tokens,
                chars_per_token=self._session_chars_per_token(target_session_id, records),
                workspace_root=self.workspace_root,
            )
            marks = keyed_marks(plan_view_marks(records, settings=settings, exclude_task_ids=active, force=True))
            if marks:
                self._apply_view_marks_via_conv_store(target_session_id, marks, reason="llm_compaction")
        except Exception:
            log.exception("view marks after compaction failed: %s", target_session_id)

    def _active_branch_bases_locked(self, target_session_id: str) -> dict[str, int]:
        """target 下还在跑的入口分支 → 登记的父历史长度（分支文件里没有边界标记时兜底用）。"""
        bases: dict[str, int] = {}
        for task in self.tasks.values():
            if self._task_terminal(task) or task.input.get("parent_session_id") != target_session_id:
                continue
            branch_id = str(task.input.get("branch_session_id") or "")
            if branch_id:
                bases[branch_id] = max(bases.get(branch_id, 0), int(task.input.get("base_count") or 0))
        return bases

    def _rewrite_branch_prefixes(
        self,
        target_session_id: str,
        rewrite: Callable[[list[Any]], list[Any] | None],
        *,
        reason: str,
    ) -> bool:
        """父会话上做过的改写同步到各活跃入口分支的父历史前缀，全部同步成功才返回 True。"""
        return self._sync_branch_prefixes(target_session_id, rewrite, reason=reason).synchronized

    def _sync_branch_prefixes(
        self,
        target_session_id: str,
        rewrite: Callable[[list[Any]], list[Any] | None],
        *,
        reason: str,
        archive: bool = True,
    ) -> BranchSync:
        """把父会话上做过的改写同步到各活跃入口分支的父历史前缀。

        分支文件在锁外读写，只有取分支清单和回写 base_count 时短暂持锁。rewrite 对不上前缀时返回 None；
        archive 为 False 用在只改 meta 的改写上，原文还在，不另存归档。
        """
        with self._lock:
            branches = self._active_branch_bases_locked(target_session_id)
        synchronized = True
        rewritten: dict[str, tuple[int, str]] = {}
        for branch_id, fallback_base in branches.items():
            try:
                result = self._rewrite_branch_prefix(
                    target_session_id, branch_id, fallback_base, rewrite, reason=reason, archive=archive,
                )
            except Exception:
                synchronized = False
                log.exception("branch history sync failed (%s): %s", reason, branch_id)
                continue
            if result is None:
                synchronized = False
            else:
                rewritten[branch_id] = result
        if rewritten:
            with self._lock:
                if not self._commit_branch_bases_locked(target_session_id, rewritten):
                    synchronized = False
        return BranchSync(synchronized=synchronized, branches=rewritten)

    def _rewrite_branch_prefix(
        self,
        target_session_id: str,
        branch_id: str,
        fallback_base: int,
        rewrite: Callable[[list[Any]], list[Any] | None],
        *,
        reason: str,
        archive: bool = True,
    ) -> tuple[int, str] | None:
        from engine.branch_history import branch_base_count, mark_branch_boundary, without_branch_boundary
        from engine.context_archive import ContextArchiveStore
        from engine.conversation_store import ConversationChangedError, ConversationStore

        for attempt in range(3):
            store = ConversationStore(self.workspace_root / "data" / "conversations")
            messages = store.load(branch_id)
            base_count = branch_base_count(messages, branch_id, fallback_base)
            if base_count <= 0 or base_count > len(messages):
                return None
            original = without_branch_boundary(messages[:base_count], branch_id)
            prefix = rewrite(original)
            if prefix is None:
                return None
            if len(prefix) == len(original) and all(new is old for new, old in zip(prefix, original)):
                return len(prefix), (prefix[-1].id if prefix else "")
            if archive:
                ContextArchiveStore(self.workspace_root).archive(target_session_id, messages, reason=reason)
            try:
                store.replace_all(
                    branch_id, mark_branch_boundary(prefix + messages[base_count:], branch_id, len(prefix)),
                )
            except ConversationChangedError:
                if attempt == 2:
                    raise
                continue
            return len(prefix), (prefix[-1].id if prefix else "")
        return None

    def _commit_branch_bases_locked(
        self, target_session_id: str, rewritten: dict[str, tuple[int, str]],
    ) -> bool:
        persisted = True
        now = _now()
        for task in list(self.tasks.values()):
            if self._task_terminal(task) or task.input.get("parent_session_id") != target_session_id:
                continue
            branch_id = str(task.input.get("branch_session_id") or "")
            if branch_id not in rewritten:
                continue
            base = rewritten[branch_id]
            if (int(task.input.get("base_count") or 0), str(task.input.get("base_last_id") or "")) == base:
                continue
            task.input["base_count"], task.input["base_last_id"] = base
            task.updated_at = now
            try:
                self._event_task_snapshot("task_context_compacted", task)
            except Exception:
                persisted = False
                log.exception("persisting compacted branch base failed: %s", branch_id)
        return persisted

    def _prepare_compact_input(
        self, session_id: str, source: dict[str, Any], *, context_ref: str = "",
    ) -> tuple[dict[str, Any], str]:
        """算压缩覆盖计划和发给 compactor 的输入。不要求持锁。"""
        from clonoth_runtime import load_runtime_config
        from engine.compaction_plan import build_compaction_plan
        from engine.context_store import load_context_snapshot
        from engine.conversation_store import ConversationStore

        config = load_runtime_config(self.workspace_root)
        use_store = main_session_enabled(config)
        if use_store or not context_ref:
            messages = ConversationStore(self.workspace_root / "data" / "conversations").load(session_id)
            storage = "conversation"
        else:
            messages = (load_context_snapshot(self.workspace_root, context_ref) or {}).get("messages") or []
            storage = "snapshot"
        plan, instruction = build_compaction_plan(
            messages, chars_per_token=self._session_chars_per_token(session_id, messages),
            include_reasoning=self._session_counts_reasoning(session_id),
            **self._compact_sizing(source),
        )
        if plan:
            plan.update(session_id=session_id, storage=storage, context_ref=context_ref)
        return plan, instruction

    def _prepared_route_io(self) -> dict[str, Any]:
        """路由前在锁外算好的 I/O 结果，按 task_id 存放，由同一次路由取走。调用方持锁。"""
        prepared = getattr(self, "_route_io_prepared", None)
        if prepared is None:
            prepared = self._route_io_prepared = {}
        return prepared

    def _prepare_route_io(self, task: Task) -> None:
        """在锁外先做这次完成要用的压缩 I/O：同步压缩 dispatch 的覆盖计划，或 compactor 摘要的落盘。

        算不出来就什么也不留，路由时退回锁内现算，结果一致。
        """
        try:
            with self._lock:
                if task.route_status == RouteStatus.routed:
                    return
                result = task.result or {}
                plan_request = None
                if str(result.get("action") or "").strip() == "dispatch":
                    plan_request = self._compact_dispatch_request(task, result)
                    if plan_request is not None and self.compact_breaker.is_open(plan_request.target_session_id):
                        return
                apply_job = self._compact_apply_job_locked(task) if plan_request is None else None
            if plan_request is not None:
                plan, instruction = self._prepare_compact_input(
                    plan_request.target_session_id,
                    plan_request.sizing_inputs(),
                    context_ref=plan_request.context_ref,
                )
                prepared: Any = PreparedCompactPlan(plan_request, plan, instruction)
            elif apply_job is not None:
                prepared = PreparedCompactApply(apply_job, self._run_compact_apply_job(apply_job))
            else:
                return
            with self._lock:
                self._prepared_route_io()[task.task_id] = prepared
        except Exception:
            log.exception(
                "route I/O preparation failed; routing computes it under the lock",
                extra={"task_id": task.task_id},
            )

    @staticmethod
    def _compact_dispatch_request(task: Task, action: dict[str, Any]) -> CompactPlanRequest | None:
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        if not dispatch_input.get("_compact_dispatch"):
            return None
        return CompactPlanRequest(
            # caller 可能跑在入口分支上，压缩要写回持久的父会话，所以 engine 给的目标优先
            target_session_id=str(
                dispatch_input.get("target_session_id")
                or task.input.get("parent_session_id")
                or task.session_id
                or ""
            ).strip(),
            context_ref=str(action.get("context_ref") or task.input.get("context_ref") or ""),
            keep_recent=int(dispatch_input.get("_compact_keep_recent", 6)),
            keep_recent_tokens=int(dispatch_input.get("_compact_keep_recent_tokens", 0) or 0),
            threshold_tokens=int(dispatch_input.get("_compact_threshold_tokens", 0) or 0),
        )

    def _take_compact_plan_locked(
        self, task: Task, request: CompactPlanRequest,
    ) -> tuple[dict[str, Any], str]:
        prepared = self._prepared_route_io().pop(task.task_id, None)
        if isinstance(prepared, PreparedCompactPlan) and prepared.request == request:
            return prepared.plan, prepared.instruction
        return self._prepare_compact_input(
            request.target_session_id, request.sizing_inputs(), context_ref=request.context_ref,
        )

    @staticmethod
    def _compact_summary_of(task: Any) -> str:
        """compactor 产出的可用摘要；没有正常 finish（包括被取消）或格式不对时为空。"""
        from engine.compact import _format_compact_summary

        result = getattr(task, "result", None) or {}
        if getattr(task, "status", None) == TaskStatus.cancelled or result.get("action") != "finish":
            return ""
        return _format_compact_summary(str((result.get("result") or {}).get("text") or ""))

    def _async_compact_job(self, task: Any) -> CompactApplyJob | None:
        target = str(task.input.get("_compact_target_session_id") or "")
        plan = task.input.get("_compact_plan") or {}
        summary = self._compact_summary_of(task)
        if not target or not summary or not plan:
            return None
        return CompactApplyJob(
            target_session_id=target, summary=summary, plan=plan, storage="conversation",
            context_ref="", sizing=self._compact_sizing(task.input),
        )

    def _dispatch_compact_job_locked(self, task: Task) -> CompactApplyJob | None:
        caller = self.tasks.get((task.caller_task_id or "").strip())
        if not self._caller_waits_for_child_locked(caller, task):
            return None
        plan = caller.input.get("_compact_plan") or {}
        storage = str(plan.get("storage") or "")
        context_ref = str(caller.result.get("context_ref") or caller.input.get("context_ref") or "")
        summary = self._compact_summary_of(task)
        if not summary or storage not in ("conversation", "snapshot") or (storage == "snapshot" and not context_ref):
            return None
        return CompactApplyJob(
            target_session_id=str(
                plan.get("session_id") or caller.input.get("_compact_target_session_id") or caller.session_id
            ),
            summary=summary, plan=plan, storage=storage, context_ref=context_ref,
            sizing=self._compact_sizing(caller.input),
        )

    def _compact_apply_job_locked(self, task: Task) -> CompactApplyJob | None:
        if task.input.get("_compact_async"):
            return self._async_compact_job(task)
        if self._is_compact_dispatch_result(task):
            return self._dispatch_compact_job_locked(task)
        return None

    def _run_compact_apply_job(self, job: CompactApplyJob) -> CompactApplyOutcome:
        """把摘要写进会话历史或旧式快照。不要求持锁；出异常按没落盘算。"""
        try:
            if job.storage == "snapshot":
                counts = self._apply_compact_to_snapshot(job)
            else:
                counts = self._apply_compact_via_conv_store_locked(
                    job.target_session_id, job.summary, plan=job.plan, **job.sizing,
                )
        except Exception:
            log.exception("compact apply failed: %s", job.target_session_id)
            return CompactApplyOutcome(counts=None, success=False)
        success = bool(counts and counts.get("applied"))
        if success and job.storage != "snapshot":
            self._mark_compacted_history(
                job.target_session_id, threshold_tokens=int(job.sizing.get("threshold_tokens") or 0),
            )
        return CompactApplyOutcome(counts=counts, success=success)

    def _compact_apply_outcome_locked(self, task: Any, job: CompactApplyJob) -> CompactApplyOutcome:
        prepared = self._prepared_route_io().pop(getattr(task, "task_id", ""), None)
        if isinstance(prepared, PreparedCompactApply) and prepared.job == job:
            return prepared.outcome
        return self._run_compact_apply_job(job)

    def _apply_compact_to_snapshot(self, job: CompactApplyJob) -> dict[str, Any] | None:
        from engine.compact import total_content_chars
        from engine.compaction_plan import archive_hint, covered_indices, fully_covered_task_ids, replace_covered
        from engine.context_archive import ContextArchiveStore
        from engine.context_store import load_context_snapshot, write_context_snapshot

        snapshot = load_context_snapshot(self.workspace_root, job.context_ref)
        messages = (snapshot or {}).get("messages") or []
        indices = covered_indices(messages, job.plan)
        if not indices:
            return None
        archive = ContextArchiveStore(self.workspace_root).archive(
            job.target_session_id, messages, reason="snapshot_compaction",
        )
        summary_message = {
            "role": "user",
            "content": COMPACT_SUMMARY_PREFIX + job.summary + archive_hint(archive["archive_id"]),
            "_meta": {
                "source_task_id": "compact_summary", "message_type": "summary",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "compressed_task_ids": fully_covered_task_ids(messages, indices),
                "archive_id": archive["archive_id"],
            },
        }
        compressed = replace_covered(messages, job.plan, summary_message)
        if compressed is None or (
            len(compressed) >= len(messages)
            and total_content_chars(compressed) >= total_content_chars(messages)
        ):
            return None
        snapshot["messages"] = compressed
        write_context_snapshot(self.workspace_root, job.context_ref, snapshot)
        return {"before": len(messages), "after": len(compressed), "applied": True}

    def _apply_async_compact_result_locked(self, task: Task) -> None:
        target = str(task.input.get("_compact_target_session_id") or "")
        job = self._async_compact_job(task)
        outcome = self._compact_apply_outcome_locked(task, job) if job is not None else None
        if not target or getattr(task, "status", None) == TaskStatus.cancelled:
            return
        if outcome is not None and outcome.success:
            self.compact_breaker.record_success(target)
        else:
            self.compact_breaker.record_failure(target)

    def _apply_compact_result_locked(self, task: Task) -> None:
        caller = self.tasks.get((task.caller_task_id or "").strip())
        if not self._caller_waits_for_child_locked(caller, task):
            self._log_dropped_child_result_locked(task, caller)
            return
        context_ref = str(caller.result.get("context_ref") or caller.input.get("context_ref") or "")
        plan = caller.input.get("_compact_plan") or {}
        target = str(plan.get("session_id") or caller.input.get("_compact_target_session_id") or caller.session_id)
        job = self._dispatch_compact_job_locked(task)
        outcome = self._compact_apply_outcome_locked(task, job) if job is not None else None
        if outcome is not None and outcome.success:
            self.compact_breaker.record_success(target)
            self._resume_compact_parent_locked(
                caller, context_ref, compact_result=outcome.counts, success=True,
            )
            return
        cancelled = task.status == TaskStatus.cancelled
        if cancelled:
            reason = "压缩子任务已取消"
        elif str((task.result or {}).get("action") or "").strip() != "finish":
            reason = "压缩子任务失败"
        elif job is None:
            reason = "压缩摘要为空、格式不对或缺少压缩计划"
        else:
            reason = "压缩摘要没能落盘"
        if not cancelled:
            self.compact_breaker.record_failure(target)
        self._fail_compact_round_locked(
            caller, context_ref, reason=reason, in_route=False,
            compact_result=outcome.counts if outcome is not None else None,
        )

    def _fail_compact_round_locked(
        self,
        task: Task,
        context_ref: str,
        *,
        reason: str,
        in_route: bool,
        compact_result: dict[str, Any] | None = None,
    ) -> None:
        """一轮同步压缩没成：恢复 task 让它继续；同一任务连续失败到上限就按失败结束。

        in_route 时 task 正在路由它这次的压缩 dispatch，只改判结果，由外层接着按失败路由。
        """
        failures = int(task.input.get("_compact_failures") or 0) + 1
        task.input["_compact_failures"] = failures
        if failures < MAX_TASK_CONSECUTIVE_FAILURES:
            self._resume_compact_parent_locked(
                task, context_ref, compact_result=compact_result, success=False,
            )
            return
        log.warning(
            "compaction failed %d times in a row, failing task %s: %s",
            failures, task.task_id[:12], reason,
        )
        error = f"上下文压缩连续失败 {failures} 次（{reason}），任务已停止"
        for key in ("_compact_dispatch_pending", "_compact_plan"):
            task.input.pop(key, None)
        if in_route:
            self._apply_failure_locked(task, error)
            self._event_task_snapshot("task_completed", task, component="supervisor")
        else:
            self._fail_task_locked(task, error=error)

    def _apply_compact_via_conv_store_locked(
        self, target_session_id: str, summary: str, *, keep_recent: int,
        threshold_tokens: int = 256000, keep_recent_tokens: int = 0,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """按覆盖计划把摘要写进会话历史并同步到分支。不要求持锁，会话被并发改动时重试。"""
        from engine.conversation_store import ConversationChangedError, ConversationStore

        for _ in range(3):
            try:
                return self._apply_compact_via_conv_store_once(target_session_id, summary, plan=plan)
            except ConversationChangedError:
                continue
        count = ConversationStore(self.workspace_root / "data" / "conversations").message_count(target_session_id)
        return {"before": count, "after": count, "total_segments": 0, "kept_segments": 0, "compressed_segments": 0, "applied": False}

    def _apply_compact_via_conv_store_once(
        self, target_session_id: str, summary: str, *, plan: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        from uuid import uuid4
        from engine.compaction_plan import archive_hint, fully_covered_task_ids, covered_indices, replace_covered
        from engine.compact import total_content_chars
        from engine.context_archive import ContextArchiveStore
        from engine.conversation_store import ConversationStore, Message, MessageType

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        messages = store.load(target_session_id)
        before = len(messages)
        plan = plan or {}
        counts = {
            "before": before, "after": before,
            "total_segments": int(plan.get("total_segments", 0)),
            "kept_segments": int(plan.get("total_segments", 0)),
            "compressed_segments": 0,
            "applied": False,
        }
        if plan.get("session_id", target_session_id) != target_session_id or not summary:
            return counts
        indices = covered_indices(messages, plan)
        if not indices:
            return counts
        archive = ContextArchiveStore(self.workspace_root).archive(target_session_id, messages, reason="llm_compaction")
        summary_message = Message(
            id=str(uuid4()), role="user",
            content=COMPACT_SUMMARY_PREFIX + summary + archive_hint(archive["archive_id"]),
            message_type=MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(), source_task_id="compact_summary",
            meta={
                "compressed_task_ids": fully_covered_task_ids(messages, indices),
                "archive_id": archive["archive_id"],
            },
        )
        compressed = replace_covered(messages, plan, summary_message)
        if compressed is None or (
            len(compressed) >= before and total_content_chars(compressed) >= total_content_chars(messages)
        ):
            return counts
        store.replace_all(target_session_id, compressed)
        counts.update(
            after=len(compressed),
            compressed_segments=int(plan.get("compressed_segments", 0)),
            kept_segments=int(plan.get("total_segments", 0)) - int(plan.get("compressed_segments", 0)),
            applied=True,
        )
        self._sync_compact_to_branches(target_session_id, summary_message, plan)
        return counts

    def _sync_compact_to_branches(
        self, target_session_id: str, summary_message: Any, plan: dict[str, Any],
    ) -> None:
        from engine.compaction_plan import replace_covered

        # 入口分支和父会话共享历史；独立子节点只压缩自己的记录。
        self._rewrite_branch_prefixes(
            target_session_id,
            functools.partial(replace_covered, plan=plan, summary=summary_message),
            reason="branch_compaction",
        )

    def _resume_compact_parent_locked(
        self, caller: Task, context_ref: str, *,
        compact_result: dict[str, int] | None = None,
        before: int = 0, after: int = 0, success: bool = True,
    ) -> None:
        """恢复等待压缩的父 task。"""
        if success:
            caller.input.pop("_compact_failures", None)
        resume_data = {
            "type": "compact_done", "success": success,
            **(compact_result or {"before": before, "after": after}),
        }
        self._resume_suspended_task_locked(
            caller, resume_data, context_ref=context_ref,
            drop_input_keys=("_compact_dispatch_pending", "_compact_plan"),
        )


    # ------------------------------------------------------------------ #
    #  Turn Summary — 非阻塞轮摘要节点化
    #  原先在 engine/runner.py 中做阻塞式 LLM 调用生成轮摘要，
    #  改为 supervisor 在 task 完成后按需触发独立的 system.turn_summarizer 节点。
    #  Created: 2026-04-25
    # ------------------------------------------------------------------ #

    def _is_turn_summary_result(self, task: Task) -> bool:
        """判断是否是 turn_summarizer 子 task 的完成事件。"""
        return bool(task.input.get("_turn_summary_dispatch"))

    def _apply_turn_summary_result_locked(self, task: Task) -> None:
        """summarizer 完成后，将摘要回写到 TaskRecord。

        write_task_record 是 append 模式（追加到 JSONL），所以回写 summary 时
        会追加一条新记录。同一 task_id 出现多次时，后者覆盖前者——
        snip_history 遍历全部 records 取最后一条有 summary 的。
        """
        act = str((task.result or {}).get("action") or "").strip()
        if act != "finish":
            return

        result = (task.result or {}).get("result") or {}
        summary = str(result.get("text") or "").strip()
        if not summary or len(summary) < 50:
            return

        target_task_id = str(task.input.get("_target_task_id") or "").strip()
        target_session_id = str(task.input.get("_target_session_id") or "").strip()
        if not target_task_id or not target_session_id:
            return

        # 回写 TaskRecord：追加一条 updated record
        try:
            from engine.task_record import TaskRecord, write_task_record, load_task_records
            from pathlib import Path
            records = load_task_records(Path(self.workspace_root), target_session_id)
            for r in records:
                if r.task_id == target_task_id:
                    r.summary = summary
                    write_task_record(Path(self.workspace_root), r)
                    log.info("Turn summary written for task %s: %d chars", target_task_id[:12], len(summary))
                    break
        except Exception as e:
            log.warning("Failed to write turn summary for task %s: %s", target_task_id[:12], e)

    def _maybe_trigger_turn_summary_locked(
        self, task: Task, *, phase_id: str = "",
    ) -> None:
        """Task 完成后，检查是否需要创建轮摘要后置任务。

        门控条件：
        - 仅对 finish/fail 的 node 类型 task 触发
        - 不对系统内部任务触发（避免递归）
        - tool_call_count >= 3 或 total_tokens >= 4000
        - runtime.yaml 中 engine.turn_summary.enabled 为 true
        """
        act = str((task.result or {}).get("action") or "").strip()
        if act not in ("finish", "fail") or task.kind != TaskKind.node:
            return
        # 不对系统内部任务触发
        if task.input.get("_system_task"):
            return
        # [AutoC 2026-05-30] Why: persistent 子节点（accumulate 模式）需要轮摘要来支持 snip compact。
        # How: 仅跳过 fresh/fork 模式的一次性子节点，保留 accumulate 模式的。
        # Purpose: scout/smith 等持久节点能正常触发轮摘要和压缩。
        if task.caller_task_id:
            _child_ctx_mode = str(task.input.get("context_mode") or "").strip()
            if _child_ctx_mode in ("fresh", "fork") or not _child_ctx_mode:
                return

        runtime_cfg = load_runtime_config(self.workspace_root)
        if not get_bool(runtime_cfg, "engine.turn_summary.enabled", True):
            return

        # 门控：仅针对长工具链，短任务不触发
        min_calls = get_int(runtime_cfg, "engine.turn_summary.min_tool_calls", 3)
        _tool_call_count = (task.result or {}).get("_tool_call_count", 0)
        if _tool_call_count < min_calls:
            return

        # 格式化 task 消息，作为 summarizer 的输入
        _instruction = ""
        # [Fork/Merge 2026-05-12] 入口分支完成后，摘要任务必须回到主 session。
        # 原因：branch JSONL 已经在 finalize 中 merge 并 cleanup，继续读取 task.session_id
        # 会访问已删除的分支。做法：无 child_session_id 时读取 route_session_id。
        # 目的：轮摘要仍能基于合并后的主 ConversationStore 生成。
        _route_sid = self._route_session_id_for_task_locked(task)
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            _store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _child_sid = task.input.get("child_session_id") or ""
            _load_sid = _child_sid if _child_sid else _route_sid
            _all_msgs = _store.load(_load_sid)
            _task_msgs = [m for m in _all_msgs if m.source_task_id == task.task_id]
            if not _task_msgs:
                return
            _instruction = _format_task_messages_for_turn_summary(_task_msgs)
        except Exception as e:
            log.warning("Failed to format task messages for turn summary: %s", e)
            return

        if not _instruction.strip():
            return

        # Strip tool call blocks to prevent summarizer from mimicking tool calls.
        # The <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>> markers cause flash models to
        # hallucinate tool execution instead of summarizing.
        import re
        _instruction = re.sub(
            r'<<<TOOL_CALL>>>.*?<<<END_TOOL_CALL>>>',
            '[tool call omitted]',
            _instruction,
            flags=re.DOTALL,
        )

        # 截断（保持在 ~30K chars，与原 turn_summary.py 一致）
        if len(_instruction) > 30000:
            _instruction = _instruction[:30000] + "\n...[truncated]"

        summarizer_node = get_str(runtime_cfg, "engine.turn_summary.node_id", "system.turn_summarizer").strip()

        # [2026-05-07] Give turn_summarizer its own child session so its messages
        # don't pollute the parent conversation JSONL.
        # Why: previously used task.session_id directly, causing all summarizer
        # tool_call/tool_result messages to be written into the parent's JSONL,
        # creating orphan tool pairs that Anthropic/Gemini reject with 400.
        # A task_created event is the durable side-effect receipt. If a process
        # crashed after creation but before phase completion, recovery observes this
        # identity and does not create a second summarizer task.
        if phase_id:
            for event in self.eventlog.iter_persisted_events():
                if event.get("type") != "task_created":
                    continue
                payload = event.get("payload")
                task_input = payload.get("input") if isinstance(payload, dict) else None
                if isinstance(task_input, dict) and str(task_input.get("_post_phase_id") or "") == phase_id:
                    return

        # Only the in-memory session/task mutation needs the Supervisor lock. Config
        # loading, ConversationStore reads and formatting above remain lock-free.
        with self._lock:
            if phase_id and any(
                str((existing.input or {}).get("_post_phase_id") or "") == phase_id
                for existing in self.tasks.values()
            ):
                return
            _sum_child_sid, _ = self.get_or_create_child_session(
                _route_sid, summarizer_node, "turn_summary", "fresh",
            )
            _summary_generation = self._current_session_generation_locked(_route_sid) or task.session_generation
            self._create_task_locked(
                session_id=_route_sid,
                session_generation=_summary_generation,
                kind=TaskKind.node,
                node_id=summarizer_node,
                input_data={
                    "instruction": _instruction,
                    "_system_task": True,
                    "_turn_summary_dispatch": True,
                    "_target_task_id": task.task_id,
                    "_post_phase_id": phase_id,
                    "_post_delivery_id": phase_id,
                    # [AutoC 2026-05-30] Why: persistent 子节点的 TaskRecord 存在 child_session_id 下，
                    # 轮摘要回写时需要从正确的 transcript JSONL 找到记录。
                    # How: 优先使用 child_session_id. Purpose: snip compact 能找到摘要。
                    "_target_session_id": str(task.input.get("child_session_id") or "").strip() or _route_sid,
                    "child_session_id": _sum_child_sid,
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
