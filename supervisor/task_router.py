"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

from ._helpers import _now
from .branch_finalize_store import BranchClaim
from .qq_intent import result_from_task as qq_intent_result_from_task
from .types import RouteStatus, Task, TaskKind, TaskStatus


log = logging.getLogger(__name__)

_BRANCH_PROCESS_LOCKS_GUARD = threading.Lock()
_BRANCH_PROCESS_LOCKS: dict[str, threading.Lock] = {}


class BranchFinalizeDeferred(RuntimeError):
    """Another live owner is finalizing this branch identity."""


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
        if task.input.get("_qq_intent"):
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
        with self._lock:
            dispatch_info = self.sessions.get(dispatch_sid)
            cleanup_dispatch = bool(
                context_mode in {"fresh", "fork"}
                and dispatch_info is not None
                and dispatch_info.conversation_key.startswith("agent:")
            )
            dispatch_conv = dispatch_info.conversation_key if cleanup_dispatch else ""
            child_entry = self._session_store.get_entry(child_sid)
            child_mode = str(child_entry.get("context_mode") or "").strip() if isinstance(child_entry, dict) else ""
            cleanup_child = bool(
                child_sid and self._task_terminal(task)
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

    @contextmanager
    def _branch_parent_write_lock(self, parent_session_id: str):
        digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()[:24]
        lock_dir = self.workspace_root / "data" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"branch-merge-{digest}.lock"
        key = str(lock_path.resolve())
        with _BRANCH_PROCESS_LOCKS_GUARD:
            process_lock = _BRANCH_PROCESS_LOCKS.setdefault(key, threading.Lock())
        with process_lock:
            with lock_path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

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
        from engine.conversation_store import ConversationStore

        merged_count = 0
        store = ConversationStore(self.workspace_root / "data" / "conversations")
        if plan.merge:
            branch_messages = list(store.load(plan.branch_session_id))
            tail = branch_messages[plan.base_count:]
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
            store.delete(session_id)
            transcript_path = self.workspace_root / "data" / "transcripts" / f"{session_id}.jsonl"
            try:
                if transcript_path.exists():
                    transcript_path.unlink()
            except FileNotFoundError:
                pass
        return merged_count

    def _commit_branch_finalize_indexes_locked(self, plan: BranchFinalizePlan):
        now = _now()
        cleanup_ids = set(plan.cleanup_session_ids)
        for candidate in self.tasks.values():
            if candidate.session_id not in cleanup_ids or self._task_terminal(candidate):
                continue
            candidate.cancel_requested = True
            candidate.status = TaskStatus.cancelled
            candidate.updated_at = now
            candidate.lease_expires_at = None
            candidate.waiting_for_task_id = None
            self._event_task_snapshot("task_cancelled", candidate)
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
        action = str((task.result or {}).get("action") or "").strip()
        runtime = None
        if action in {"finish", "ask", "fail", "cancelled"}:
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
            action in {"finish", "ask", "fail", "cancelled"}
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
        # [AutoC 2026-05-30] Why: _route_completed_task_locked 需要统一 finally
        # 清理，但直接包裹原大方法会造成整体缩进变更。How: 保持原路由主体
        # 缩进不变并移动到 inner。Purpose: 降低重构噪音，便于审查行为差异。
        # ---- 批量 task：优先走统一批量收集 ----
        if task.batch_id:
            self._try_complete_batch_locked(task)
            # 后置记忆提取不对 batch 子 task 触发
            return

        # ---- 后台压缩结果：无 caller，压缩期间用户那一轮照常在跑 ----
        if task.input.get("_compact_async"):
            self._apply_async_compact_result_locked(task)
            return

        # ---- QQ 接话意愿判定：结果交回 HTTP 里等着的那个请求，不产生任何用户可见输出 ----
        if task.input.get("_qq_intent"):
            self.qq_intent.settle(task.task_id, qq_intent_result_from_task(task))
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
            self._route_preempted_locked(task, action)
            return  # preempted 不触发记忆提取，也不 merge branch
        elif act == "dispatch":
            self._route_dispatch_locked(task, action)
        elif act in ("finish", "ask"):
            # [Fork/Merge 2026-05-12] finish/ask 是入口分支的用户可见终态。
            # 原因：outbound 必须在主 session 产生，且应先把 branch JSONL 合并回主历史。
            # 做法：路由 finish 前执行幂等 finalize。目的：SDK 按主 conversation_key 收到回复。
            self._finalize_branch_task_locked(task, merge=True)
            self._route_finish_locked(task, action)
        elif act == "fail":
            # [Fork/Merge 2026-05-12] fail 也会结束入口分支。
            # 原因：错误回复应进入主 session，同时保留分支中已产生的上下文。
            # 做法：先 merge/cleanup，再走原 fail 输出。目的：失败路径与成功路径一致。
            self._finalize_branch_task_locked(task, merge=True)
            self._route_fail_locked(task, action)
        elif act == "cancelled":
            # [Fork/Merge 2026-05-12] engine 主动返回 cancelled 时不输出，但仍收束分支。
            # 原因：cancel 是终态，不应遗留 branch session。做法：只 finalize，不调用输出路由。
            # 目的：符合 finish/cancel 时 merge 回主 session 的架构约束。
            self._finalize_branch_task_locked(task, merge=True)
        # cancelled → 不做用户输出


    # ------------------------------------------------------------------ #
    #  dispatch
    # ------------------------------------------------------------------ #

    def _node_is_persistent(self, node_id: str) -> bool:
        """读取 dispatch 目标节点的 persistent 声明。

        [AutoC 2026-07-09] Why: 当上游未显式传 context_mode 时，需要依据
        节点的 persistent 声明推导默认（persistent→accumulate，否则 fresh）。
        How: 复用 engine.node.load_node（config/nodes 与 system_nodes 搜索顺序），
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

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        dispatch_batch = action.get("dispatch_batch")

        # 标记父 task 正在等待压缩 dispatch
        compact_target_session_id = ""
        if dispatch_input.get("_compact_dispatch"):
            task.input["_compact_dispatch_pending"] = True
            task.input["_compact_keep_recent"] = int(
                dispatch_input.get("_compact_keep_recent", 6)
            )
            # 这两个也必须搬到 caller.input：_apply_compact_result_locked 只读 caller，
            # 少搬一个就等于 supervisor 用默认值收缩，与 engine 的触发阈值脱钩。
            task.input["_compact_keep_recent_tokens"] = int(
                dispatch_input.get("_compact_keep_recent_tokens", 0) or 0
            )
            task.input["_compact_threshold_tokens"] = int(
                dispatch_input.get("_compact_threshold_tokens", 0) or 0
            )
            # [AutoC 2026-05-13] Why: compactor results are applied after the
            # caller task is suspended, and caller.session_id may be an entry
            # branch. How: store the explicit parent-first target from engine,
            # falling back to caller parent/session for older actions. Purpose:
            # _apply_compact_result_locked always rewrites the durable session.
            compact_target_session_id = str(
                dispatch_input.get("target_session_id")
                or task.input.get("parent_session_id")
                or task.session_id
                or ""
            ).strip()
            if compact_target_session_id:
                task.input["_compact_target_session_id"] = compact_target_session_id

        created_child_ids: list[str] = []
        # [Fork/Merge 2026-05-17] Why: descendants of an entry branch should keep
        # branch-local storage but still route user-visible events through the
        # parent session. How: compute the parent route once and propagate it in
        # child task input while leaving task.session_id unchanged. Purpose: child
        # nodes/tools do not call supervisor APIs with a soon-to-be-deleted branch.
        route_parent_session_id = self._route_session_id_for_task_locked(task)
        route_parent_for_child = route_parent_session_id if route_parent_session_id != task.session_id else ""

        # ---- 批量委派（node 或 tool 均可） ----
        if isinstance(dispatch_batch, list) and dispatch_batch:
            batch_id = str(uuid.uuid4())
            for idx, item in enumerate(dispatch_batch):
                item_kind = str(item.get("kind") or "node").strip()
                item_target = str(item.get("target") or "").strip()
                if not item_target:
                    continue

                if item_kind == "tool":
                    _tool_input = {
                        "arguments": item.get("arguments", {}),
                        "call_id": str(item.get("call_id") or ""),
                    }
                    if route_parent_for_child:
                        # [Fork/Merge 2026-05-17] Why: batch tool tasks inherit
                        # the caller branch as runtime session. How: carry the
                        # parent route in input for engine.runner ToolContext.
                        # Purpose: guarded tools and progress route to parent.
                        _tool_input["parent_session_id"] = route_parent_for_child
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
                        _batch_input["fork_from_session_id"] = task.session_id
                    # 审计报告 Step 1（2026-04-16）：删除此处对 _find_last_context_ref_locked
                    # 的 accumulate fallback 注入。engine/runner.py 在 child_session_id
                    # 非空时会无条件清空 context_ref（参见 runner.py:514），此注入永远
                    # 不会被消费。保留只是兼容期死代码，现移除。

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

        # ---- 单节点委派 ----
        if not created_child_ids and target:
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

            # 审计报告 Step 1（2026-04-16）：删除此处单节点 dispatch 的 accumulate/fresh
            # 兼容期 fallback。engine/runner.py:514 在 child_session_id 非空时会无条件
            # 清空 context_ref，accumulate/fresh 两个分支写的值永远不会被消费。

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
            task.waiting_for_task_id = created_child_ids[0] if len(created_child_ids) == 1 else None
            task.updated_at = _now()
            self._event_task_snapshot("task_suspended", task)

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

        # 子节点被 preempt：恢复 suspended 的 caller，注入 child_preempted
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                caller.input["resume_data"] = {
                    "type": "child_preempted",
                    "child_node_id": str(task.node_id or ""),
                    "context_ref": context_ref,
                }
                caller.input["context_ref"] = str(
                    caller.result.get("context_ref")
                    or caller.input.get("context_ref")
                    or ""
                )
                self._event_task_snapshot("task_resumed", caller)

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
    #  统一批量收集
    # ------------------------------------------------------------------ #

    def _try_complete_batch_locked(self, task: Task) -> None:
        """统一批量收集。不区分 tool/node，等同 batch_id 全部终态后打包恢复 caller。"""
        batch_id = task.batch_id
        if not batch_id:
            return

        # 找所有同 batch 的兄弟
        siblings: list[Task] = []
        for t in self.tasks.values():
            if t.batch_id == batch_id and t.session_id == task.session_id:
                siblings.append(t)
        if not siblings:
            return

        # 有任何一个还没结束，等着
        if any(not self._task_terminal(t) for t in siblings):
            return

        # 防止重复恢复（多个兄弟同时完成时可能多次进入这里）
        resume_key = f"batch:{batch_id}"
        for t in self.tasks.values():
            if t.session_id == task.session_id and str(t.input.get("_resume_key") or "") == resume_key:
                return

        # 按 batch_index 排序
        siblings.sort(key=lambda x: x.batch_index)

        # 收集结果
        entries: list[dict[str, Any]] = []
        for t in siblings:
            tr = t.result.get("result") if isinstance(t.result.get("result"), dict) else {}
            act = str(t.result.get("action") or "").strip()

            entry: dict[str, Any] = {
                "kind": t.kind.value,
                "status": act,
                "summary": str(t.result.get("summary") or tr.get("summary") or ""),
            }

            if t.kind == TaskKind.node:
                entry["node_id"] = str(t.node_id or "")
                entry["instruction"] = str(t.input.get("instruction") or "")
                entry["text"] = str(tr.get("text") or "")
            elif t.kind == TaskKind.tool:
                entry["name"] = str(t.tool_name or tr.get("tool_name") or "")
                entry["args"] = dict(tr.get("arguments") or t.input.get("arguments") or {})
                entry["format"] = str(tr.get("raw_format") or tr.get("format") or "json")
                entry["raw_inline"] = str(tr.get("raw_inline") or tr.get("text") or "")
                entry["truncated"] = bool(tr.get("truncated", False))
                entry["ref"] = str(tr.get("ref") or "")

            if act == "fail":
                entry["error"] = str(t.result.get("error") or "")

            atts = tr.get("attachments")
            if isinstance(atts, list):
                entry["attachments"] = atts

            entries.append(entry)

        # 构建摘要
        total = len(entries)
        fail_count = sum(1 for e in entries if e["status"] == "fail")
        summary_parts = [f"批量完成 ({total} 个)"]
        if fail_count:
            summary_parts.append(f"{fail_count} 个失败")
        summary_text = ", ".join(summary_parts)

        resume_data: dict[str, Any] = {
            "type": "batch_results",
            "entries": entries,
        }
        self._resume_caller_or_output_locked(
            task, resume_data, {"summary": summary_text},
        )

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
        """尝试恢复 caller 节点。如果没有 caller，把 fallback_result 输出给用户。"""
        # 优先尝试直接唤醒 suspended 的 caller
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                # 注入 resume_data 到 caller 的 input 中
                caller.input["resume_data"] = resume_data
                if action_type:
                    # [AutoC 2026-05-31] Why: ask and finish currently resume the
                    # caller through the same code path. How: copy action_type into
                    # resume_data for callers and also into input for cheap
                    # supervisor inspection. Purpose: preserve ask identity for
                    # upcoming topology routing without changing the payload shape.
                    caller.input["_last_child_action_type"] = str(action_type).strip()
                caller.input["context_ref"] = str(caller.result.get("context_ref") or caller.input.get("context_ref") or "")
                # 标记防重复恢复
                if task.batch_id:
                    caller.input["_resume_key"] = f"batch:{task.batch_id}"
                self._event_task_snapshot("task_resumed", caller)
                return


        if task.input.get("_dispatch_origin"):
            self._inject_dispatch_result_via_origin_locked(task, fallback_result)
            # [2026-05-28] fresh/fork session 清理：conversation_key 带 uuid 的
            # dispatch session 完成后删除，避免资源积累。
            _d_ctx_mode = str(task.input.get("_dispatch_origin", {}).get("context_mode") or "").strip()
            if not _d_ctx_mode:
                _d_ctx_mode = str(task.input.get("dispatch_context_mode") or "").strip()
            return

        # 旧路径（向后兼容）：异步 dispatch 子任务完成 → 注入 inbound 通知入口节点
        if task.input.get("_async_dispatch"):
            self._inject_async_dispatch_result_locked(task, fallback_result)
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
        # 去掉守卫后，Bot 侧 send_reply 会跳过 Discord 发送但正常执行清理收尾。
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
            action_type=action_type,
            llm_request_id=str(task.result.get("llm_request_id") or "").strip(),
            delivery_id=task.delivery_id,
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
    #  异步 dispatch 结果注入
    # ------------------------------------------------------------------ #

    def _inject_async_dispatch_result_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """异步 dispatch 子任务完成后，将结果作为新 inbound 注入 session。

        [Fork/Merge 2026-05-12] 不再自动 preempt running/suspended 入口 task。
        原因：preempt 只能由 adapter 显式 API 触发；内部结果注入应 fork 新分支并发处理。
        做法：始终走 inbound 创建路径。目的：避免内部事件破坏正在运行的入口分支。

        此方法在 self._lock 内调用，不可调用会再次获取 _lock 的公开方法。
        """

        result_text = str(fallback_result.get("text") or "").strip()
        result_summary = str(fallback_result.get("summary") or "").strip()
        caller_node = str(task.input.get("_caller_node_id") or "").strip()

        child_session_id = str(task.input.get("child_session_id") or "").strip()

        result_atts = (
            fallback_result.get("attachments")
            if isinstance(fallback_result.get("attachments"), list)
            else None
        )


        route_session_id = self._route_session_id_for_task_locked(task)
        session_info = self.sessions.get(route_session_id)
        if not session_info:
            log.warning(
                "async dispatch result dropped: session %s not found for task %s",
                route_session_id[:12], task.task_id[:12],
            )
            return

        # 等待期间用户又说了一句就会 bump generation，子节点结果随之丢弃；对用户表现为
        # 「说了稍等然后再无下文」，不留声就无从查起。
        current_gen = self._current_session_generation_locked(route_session_id)
        if task.session_generation and current_gen and task.session_generation != current_gen:
            log.warning(
                "async dispatch result dropped: session %s generation %s != %s for task %s",
                route_session_id[:12], task.session_generation, current_gen, task.task_id[:12],
            )
            return

        if route_session_id in self._cancelled_sessions:
            log.warning(
                "async dispatch result dropped: session %s cancelled for task %s",
                route_session_id[:12], task.task_id[:12],
            )
            return

        # [Fork/Merge 2026-05-12] 异步 dispatch 结果不再自动 preempt 入口 task。
        # 原因：新架构规定 preempt 只能由 adapter 显式调用 preempt API 触发，普通 inbound
        # 或内部注入都应通过 fork 分支并发处理。做法：移除 running/suspended 入口任务的
        # 自动 preempt 分支，直接创建 inbound。目的：同一 session 下多个入口分支可并行运行。

        # ---- 创建 inbound → 新 branch task ----
        conv_key = session_info.conversation_key
        channel = session_info.channel
        msg_id = f"async_dispatch:{task.task_id}"
        attachments_outbound_sent = self._emit_dispatch_attachment_outbound_locked(
            session_id=route_session_id,
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
        # [2026-07-15] 同 _inject_dispatch_result_via_origin_locked：回调明确回
        # 发起节点，避免回退到 session 上一次绑定的入口节点而错路。
        if caller_node:
            payload["entry_node_id"] = caller_node
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

        # eventlog 有独立锁，不会与 self._lock 死锁
        evt = self.eventlog.append(
            session_id=route_session_id,
            component="supervisor",
            type_="inbound_message",
            payload=payload,
        )
        seq = int(evt.get("seq", 0))
        self._apply_inbound_message(seq=seq, session_id=route_session_id, payload=payload)
        self._advance_inbound_cursor()
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=route_session_id, payload=payload,
        )

    # ------------------------------------------------------------------ #
    #  [2026-05-28] 新路径：通过 dispatch_origin 回调结果
    # ------------------------------------------------------------------ #

    def _inject_dispatch_result_via_origin_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """dispatch_origin 路径的回调注入。

        [2026-05-28] 异步 dispatch 统一走 inbound 后，子任务完成时通过
        task.input["_dispatch_origin"] 中记录的 parent_session_id 将结果
        作为新 inbound 注入回调用方 session。

        与 _inject_async_dispatch_result_locked 逻辑基本一致，区别在于：
        - 从 task.input["_dispatch_origin"] 读取回调目标
        - 目标是 dispatch_origin.parent_session_id（不是 task 自身的 route session）

        此方法在 self._lock 内调用。
        """
        origin = task.input.get("_dispatch_origin") or {}
        target_session_id = str(origin.get("parent_session_id") or "").strip()
        caller_node = str(origin.get("caller_node_id") or "").strip()
        # [AutoC 2026-06-03] Why: dispatch_origin callbacks use the same visible
        # callback card as legacy async dispatch. How: keep the child session id from
        # the completed child task input. Purpose: both callback paths support the
        # same structured web jump button.
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

        # 等待期间用户又说了一句就会 bump generation，子节点结果随之丢弃；对用户表现为
        # 「说了稍等然后再无下文」，不留声就无从查起。
        current_gen = self._current_session_generation_locked(target_session_id)
        if task.session_generation and current_gen and task.session_generation != current_gen:
            log.warning(
                "dispatch_origin callback skipped: session %s generation %s != %s for task %s",
                target_session_id[:12], task.session_generation, current_gen, task.task_id[:12],
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
        # 的入口节点（如上一次被 dispatch 目标写入的 qq.web_search），导致回调被错误
        # 路由到不具备推送能力的节点，异步结果无法发出。How: 回调明确指定
        # entry_node_id 为发起 dispatch 的 caller 节点（orchestrator）。Purpose: 异步
        # 结果回到原编排节点，由它决定如何向用户推送。
        if caller_node:
            payload["entry_node_id"] = caller_node
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
        self._advance_inbound_cursor()
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=target_session_id, payload=payload,
        )

    def _cleanup_dispatch_session_locked(self, task: Task) -> None:
        """清理 fresh/fork 模式 dispatch 创建的临时 session。

        [2026-05-28] fresh/fork dispatch 的 conversation_key 带 uuid，
        完成后不会被复用。清理对应的 session 和 JSONL，释放资源。
        """
        # task 运行在 entry branch 上，需要找到其 parent session
        _parent_sid = str(task.input.get("parent_session_id") or "").strip()
        _branch_sid = str(task.input.get("branch_session_id") or "").strip()
        # dispatch session 是 branch 的 parent
        _dispatch_sid = _parent_sid or ""
        if not _dispatch_sid:
            return
        _info = self.sessions.get(_dispatch_sid)
        if not _info:
            return
        # 只清理 agent: 前缀的 dispatch session
        if not _info.conversation_key.startswith("agent:"):
            return
        try:
            self.purge_session_files(_dispatch_sid)
            if _branch_sid:
                self.purge_session_files(_branch_sid)
            # 从内存中移除
            self.conversation_map.pop(_info.conversation_key, None)
            self.sessions.pop(_dispatch_sid, None)
            self.session_generations.pop(_dispatch_sid, None)
            self._cancelled_sessions.discard(_dispatch_sid)
            self._session_context_usage.pop(_dispatch_sid, None)

            # 阻止 agent:* 派生 session 持续堆积。
            self._session_store.remove_session(_dispatch_sid)
        except Exception as e:
            log.warning("cleanup dispatch session %s failed: %s", _dispatch_sid[:12], e)
            raise

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

            evt = self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="inbound_message",
                payload=payload,
            )
            seq = int(evt.get("seq", 0))
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            self._create_entry_task_for_inbound_locked(
                inbound_seq=seq, session_id=session_id, payload=payload,
            )
            return {"ok": True, "strategy": "inbound"}

    def _find_entry_task_by_status_locked(self, session_id: str, statuses: set) -> Task | None:
        """在 session 中查找指定状态的入口 task。

        入口 task 定义：无 caller、非异步 dispatch 子任务、非系统任务。
        若存在多个符合条件的 task（理论上不应发生），返回最早创建的。
        """
        candidate: Task | None = None
        for t in self.tasks.values():
            if t.session_id != session_id:
                continue
            if t.status not in statuses:
                continue
            # 排除：有 caller 的子任务
            if t.caller_task_id:
                continue
            # 排除：异步 dispatch 子任务自身（旧路径）
            if t.input.get("_async_dispatch"):
                continue
            # [2026-05-28] 排除：通过 inbound dispatch_origin 创建的子任务。
            # 为什么：这类任务是异步委派的执行体，不应被视为用户入口 task。
            if t.input.get("_dispatch_origin"):
                continue
            # 排除：系统内部任务
            if t.input.get("_system_task"):
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
    #  child session 清理
    # ------------------------------------------------------------------ #

    def _cleanup_task_child_session_if_needed(self, task: Task) -> None:
        """统一清理 task 关联的 child session（非 accumulate 模式）。

        [AutoC 2026-05-30] Why: child session 清理逻辑原先散落在 10+ 个
        return 路径中，每新增路径都要记得补清理，容易遗漏导致 sessions.json
        堆积。How: 在 _route_completed_task_locked 的 finally 中统一调用。
        Purpose: 单一出口，防止泄漏。
        """
        child_sid = str(task.input.get("child_session_id") or "").strip()
        if not child_sid:
            return

        # 避免 finally 在挂起路径误删仍活跃的 child session。
        if not self._task_terminal(task):
            return

        # 优先读取registry，缺失时才回退 task.input。避免误删 accumulate 会话。
        entry = self._session_store.get_entry(child_sid)
        registry_mode = str(entry.get("context_mode") or "").strip() if isinstance(entry, dict) else ""
        input_mode = str(task.input.get("context_mode") or "").strip()
        ctx_mode = registry_mode or input_mode

        if ctx_mode == "accumulate":
            return

        self._expire_child_session(child_sid)

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
        """本会话的字符/token 比，用 engine 上报的真实 prompt_tokens 校准。

        少了这一步就按 3 字符/token 算，中文会把 keep_recent_tokens 的预算放大两三倍，
        压完仍超阈值，下一轮又压一次。engine 上报 usage 时会带同一批消息的字符数；
        拿不到时退而用 store 里的历史字符数比 —— 它少了 system 段，比例偏小、偏保守。
        """
        from engine.compact import DEFAULT_CHARS_PER_TOKEN, observed_chars_per_token, total_content_chars

        usage = self._session_context_usage.get(session_id) or {}
        prompt_tokens = usage.get("prompt_tokens")
        if not isinstance(prompt_tokens, int):
            return DEFAULT_CHARS_PER_TOKEN
        prompt_chars = usage.get("prompt_chars")
        if not isinstance(prompt_chars, int) or prompt_chars <= 0:
            prompt_chars = total_content_chars(messages)
        return observed_chars_per_token(prompt_chars, prompt_tokens)

    def _emergency_compact_summary(self, target_session_id: str) -> str:
        """Build a replacement summary from per-task records when the compactor failed.

        压缩器失败时旧的做法是拿一句固定道歉替掉十几个任务段 —— 数据照删、摘要为零。
        TaskRecord 里已经存了每个任务的摘要，拼起来至少能留住发生过什么。
        """
        header = "[上下文压缩摘要生成失败，以下为各任务的既有摘要]"
        try:
            from engine.task_record import load_task_records

            records = load_task_records(self.workspace_root, target_session_id)
        except Exception as load_error:
            log.warning("emergency compact summary: task records unavailable: %s", load_error)
            records = []

        lines: list[str] = []
        for record in records:
            text = str(getattr(record, "summary", "") or "").strip()
            if text:
                lines.append(f"- {text}")
        if not lines:
            return "[上下文压缩摘要生成失败，旧消息已被裁剪以恢复服务]"
        return header + "\n\n" + "\n".join(lines[-40:])

    def _apply_async_compact_result_locked(self, task: Task) -> None:
        """Apply a background compaction result; there is no caller to resume.

        与同步路径的区别只有两点：没有 caller 需要 resume，也没有旧 snapshot 分支
        （后台压缩只在 ConversationStore 模式下发起）。摘要校验、应急兜底、熔断
        记账都复用同一套。
        """
        from engine.compact import _format_compact_summary

        target_session_id = str(task.input.get("_compact_target_session_id") or "").strip()
        if not target_session_id:
            log.warning("async compact result without target session: task=%s", task.task_id[:8])
            return

        sizing = self._compact_sizing(task.input)
        act = str((task.result or {}).get("action") or "").strip()
        summary = ""
        if act == "finish":
            raw_summary = str(((task.result or {}).get("result") or {}).get("text") or "").strip()
            summary = _format_compact_summary(raw_summary)

        emergency = not summary
        if emergency:
            self.compact_breaker.record_failure(target_session_id)
            summary = self._emergency_compact_summary(target_session_id)
            log.warning(
                "async compact fell back to task-record summary: task=%s action=%s",
                task.task_id[:8], act or "?",
            )

        try:
            cr = self._apply_compact_via_conv_store_locked(target_session_id, summary, **sizing)
        except Exception as apply_error:
            log.warning("async compact apply failed: %s", apply_error)
            return
        if not emergency and cr["after"] < cr["before"]:
            self.compact_breaker.record_success(target_session_id)

        log.info(
            "async compact applied session=%s before=%d after=%d kept_segments=%d",
            target_session_id, cr["before"], cr["after"], cr["kept_segments"],
        )

    def _apply_compact_result_locked(self, task: Task) -> None:
        """compactor 完成后，对父 task 的上下文执行压缩并恢复父 task。

        Step 2（2026-04-16）：支持两条路径。
        - ConversationStore 路径：caller 是主节点（flag 开启）或子节点（有 child_session_id），
          直接操作 data/conversations/{target}.jsonl，用 summary 消息替换中间部分。
        - 旧 snapshot 路径：flag 关闭时的主节点，沿用 caller.input.context_ref 读写。
        """
        from engine.compact import _format_compact_summary, apply_compact_summary
        from engine.context_store import load_context_snapshot, write_context_snapshot

        caller_id = task.caller_task_id
        caller = self.tasks.get(caller_id)
        if not caller or caller.status != TaskStatus.suspended:
            return

        act = str((task.result or {}).get("action") or "").strip()
        parent_ctx_ref = str(
            caller.result.get("context_ref")
            or caller.input.get("context_ref")
            or ""
        ).strip()

        # ---- 选择压缩路径 ----
        # 1. caller 提供 _compact_target_session_id / parent_session_id → ConversationStore（父 session）
        # 2. caller 无 parent 但 main_session_enabled → ConversationStore（当前 session）
        # 3. 否则 → 旧 snapshot 路径
        from clonoth_runtime import load_runtime_config
        _rc = load_runtime_config(self.workspace_root)
        _main_conv_enabled = bool(
            _rc.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
        )
 
        target_sid_for_conv = str(
            caller.input.get("_compact_target_session_id")
            or caller.input.get("target_session_id")
            or caller.input.get("parent_session_id")
            or ""
        ).strip()
        if not target_sid_for_conv and _main_conv_enabled:
            # Step 2 修复：main_session_enabled=true 时无条件走 ConvStore 路径。
            # 原条件 `and not parent_ctx_ref` 会被 _persist_ctx 写入的 context_ref 拦住，
            # 导致 compact 走旧 snapshot 路径，而 runner.py 已改为从 JSONL 读取——读写不一致。
            target_sid_for_conv = caller.session_id

        if act == "finish":
            result = (task.result or {}).get("result") or {}
            raw_summary = str(result.get("text") or "").strip()
            summary = _format_compact_summary(raw_summary)

            # --- Circuit breaker + emergency truncation ---
            # If summary was rejected (empty), record failure for circuit breaker
            # AND perform emergency segment truncation to break the loop.
            if not summary and target_sid_for_conv:
                self.compact_breaker.record_failure(target_sid_for_conv)
                # Emergency truncation: even without a valid summary, force-remove
                # old segments to break the compact loop.
                _emergency_summary = self._emergency_compact_summary(target_sid_for_conv)
                try:
                    cr = self._apply_compact_via_conv_store_locked(
                        target_sid_for_conv, _emergency_summary,
                        **self._compact_sizing(caller.input),
                    )
                    if cr["after"] < cr["before"]:
                        log.warning(
                            "compact: emergency truncation applied (summary rejected), "
                            "before=%d after=%d", cr["before"], cr["after"],
                        )
                        self._resume_compact_parent_locked(
                            caller, parent_ctx_ref,
                            compact_result=cr, success=True,
                        )
                        return
                except Exception as _et:
                    log.warning("compact: emergency truncation failed: %s", _et)
                # If emergency truncation didn't help, fall through to failure path

            if summary and target_sid_for_conv:
                # ---- ConversationStore 路径 ----
                cr = self._apply_compact_via_conv_store_locked(
                    target_sid_for_conv, summary, **self._compact_sizing(caller.input),
                )
                if cr["after"] < cr["before"]:
                    self.compact_breaker.record_success(target_sid_for_conv)
                    self._resume_compact_parent_locked(
                        caller, parent_ctx_ref,
                        compact_result=cr, success=True,
                    )
                    return
                # before == after：消息太少未压缩，静默恢复
                self._resume_compact_parent_locked(
                    caller, parent_ctx_ref,
                    compact_result=cr, success=False,
                )
                return

            if summary and parent_ctx_ref:
                # ---- 旧 snapshot 路径（flag 关闭时的主节点）----
                snapshot = load_context_snapshot(self.workspace_root, parent_ctx_ref)
                if snapshot and isinstance(snapshot.get("messages"), list):
                    old_messages = snapshot["messages"]
                    compressed = apply_compact_summary(
                        old_messages, summary,
                        chars_per_token=self._session_chars_per_token(caller.session_id, old_messages),
                        **self._compact_sizing(caller.input),
                    )
                    if len(compressed) < len(old_messages):
                        snapshot["messages"] = compressed
                        write_context_snapshot(
                            self.workspace_root, parent_ctx_ref, snapshot,
                        )
                        self.compact_breaker.record_success(caller.session_id)
                        self._resume_compact_parent_locked(
                            caller, parent_ctx_ref,
                            before=len(old_messages),
                            after=len(compressed),
                            success=True,
                        )
                        return

        # ---- 失败路径 ----
        # [2026-07-16] Why: compactor 子 task 以 action=fail 结束时（例如
        # 压缩模型 404 / 不可用），act != "finish"，会直接落到这里静默恢复父 task，
        # 但从未调用 record_compact_failure，导致熔断器计数永远为 0、is_compact_circuit_open
        # 恒为 False，父 task 恢复后上下文未变小又立即再次 dispatch 压缩，形成死循环
        # How: 在失败路径同样对 compact target session 记一次失败；累计到
        # _MAX_CONSECUTIVE_FAILURES 后 _check_and_compact 会跳过压缩。并复用
        # 与「finish 但 summary 为空」分支相同的应急截断，尽力打破死循环。
        # Purpose: 压缩模型偶发/持续失败时不再无限重试拖垮群聊主任务。
        if target_sid_for_conv:
            self.compact_breaker.record_failure(target_sid_for_conv)
            _emergency_summary = self._emergency_compact_summary(target_sid_for_conv)
            try:
                cr = self._apply_compact_via_conv_store_locked(
                    target_sid_for_conv, _emergency_summary,
                    **self._compact_sizing(caller.input),
                )
                if cr["after"] < cr["before"]:
                    log.warning(
                        "compact: emergency truncation applied (compactor action=%s), "
                        "before=%d after=%d", act or "?", cr["before"], cr["after"],
                    )
                    self._resume_compact_parent_locked(
                        caller, parent_ctx_ref,
                        compact_result=cr, success=True,
                    )
                    return
            except Exception as _et:
                log.warning("compact: emergency truncation failed on fail-path: %s", _et)

        # 静默恢复父 task
        self._resume_compact_parent_locked(
            caller, parent_ctx_ref, success=False,
        )

    def _apply_compact_via_conv_store_locked(
        self, target_session_id: str, summary: str, *, keep_recent: int,
        threshold_tokens: int = 256000, keep_recent_tokens: int = 0,
    ) -> dict[str, int]:
        """Step 2（2026-04-16）：在 ConversationStore 层面执行压缩。

        读取 target session 的 JSONL，用 summary 消息替换旧 task segment，保留
        最近 keep_recent 个完整 task segment。与 engine.compact.apply_compact_summary
        的策略一致，但直接操作 Message 对象而非 dict。

        注意 ConversationStore 里没有 system 消息（system prompt 每轮由 ai_step
        的 assemble_initial_messages 重建），所以无需处理 prefix/inner system。

        Returns: dict with keys: before, after, total_segments, kept_segments, compressed_segments
        """
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import ConversationStore, Message, MessageType

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        msgs = store.load(target_session_id)
        before = len(msgs)
        chars_per_token = self._session_chars_per_token(target_session_id, msgs)

        # [AutoC 2026-05-13] Why: ConversationStore stores Message objects, while
        # engine.compact.apply_compact_summary works on dict history. How: read
        # task ids through Message attributes and meta, then split only when the
        # consecutive source_task_id changes. Purpose: keep recent task segments
        # complete instead of slicing by raw message count.
        # [2026-05-17] Split messages into task segments, but treat old
        # compact_summary segments as "prefix" rather than a real task segment.
        # Old summaries should be replaced alongside other old segments, not
        # counted toward keep_recent (which would cause an infinite loop where
        # the old summary is deleted and recreated each cycle with no net change).
        prefix_msgs: list[Any] = []  # old compact_summary messages
        segments: list[list[Any]] = []
        cur_seg: list[Any] = []
        cur_tid: str = ""
        for msg in msgs:
            meta = msg.meta if isinstance(getattr(msg, "meta", None), dict) else {}
            tid = str(meta.get("source_task_id", "") or getattr(msg, "source_task_id", "") or "")
            if tid == "compact_summary":
                # [2026-05-17] Why: an old compact_summary is compressed prefix
                # material, not a task boundary. How: collect it for replacement
                # and keep the current real segment open. Purpose: the old summary
                # is fed into the next summary and never consumes keep_recent.
                prefix_msgs.append(msg)
                continue
            if tid != cur_tid and cur_seg:
                segments.append(cur_seg)
                cur_seg = []
            cur_tid = tid
            cur_seg.append(msg)
        if cur_seg:
            segments.append(cur_seg)

        # keep_recent applies to real task segments only (prefix excluded)
        # keep_recent_tokens > 0 时保留量按 token 算：从最后一段往前累加到预算为止。
        # 仍然切在段边界上，否则 assistant 的 tool_call 会和它的 tool_result 分家。
        if keep_recent_tokens > 0:
            from engine.compact import segments_within_token_budget

            keep_recent = segments_within_token_budget(segments, keep_recent_tokens, chars_per_token)
        keep_recent = max(keep_recent, 1)
        if len(segments) <= keep_recent:
            return {"before": before, "after": before, "total_segments": len(segments), "kept_segments": len(segments), "compressed_segments": 0}

        kept_segments = segments[-keep_recent:]
        to_remove_segments = segments[:-keep_recent]

        to_keep: list[Any] = []
        for seg in kept_segments:
            to_keep.extend(seg)

        to_remove: list[Any] = list(prefix_msgs)  # old summaries are always replaced
        for seg in to_remove_segments:
            to_remove.extend(seg)

        # 收集被压缩掉的消息所属的 source_task_id，
        # 存入 summary 消息的 meta 中。L2 snip_history 据此判断哪些 task 已被
        # LLM 压缩过，避免因 ID 丢失而反复 fall through 到 LLM compact。
        # [2026-04-26] 累积继承：旧 compact_summary 被再次压缩时，
        # 继承其 meta.compressed_task_ids，防止历史 ID 丢失。
        _ctid_set: set[str] = set()
        for _rm in to_remove:
            _rm_meta = _rm.meta if isinstance(getattr(_rm, "meta", None), dict) else {}
            _rm_tid = str(_rm_meta.get("source_task_id", "") or getattr(_rm, "source_task_id", "") or "")
            if _rm_tid and _rm_tid != "compact_summary":
                _ctid_set.add(_rm_tid)
            _old_ctids = _rm_meta.get("compressed_task_ids")
            if isinstance(_old_ctids, list):
                for _ctid in _old_ctids:
                    if _ctid:
                        _ctid_set.add(str(_ctid))
        compressed_task_ids = list(_ctid_set)

        summary_msg = Message(
            id=str(uuid4()),
            role="user",
            content="[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary,
            message_type=MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_task_id="compact_summary",
            meta={"compressed_task_ids": compressed_task_ids},
        )
        new_messages = [summary_msg] + to_keep

        # --- Progressive keep_recent reduction ---
        # If the compressed result still exceeds the token threshold,
        # drop older kept segments one at a time until we fit or reach 0.
        # Estimate tokens as total chars / 3 (mixed CJK/English).
        _threshold = threshold_tokens
        try:
            from engine.compact import total_content_chars

            _total_chars = total_content_chars(new_messages)
            while keep_recent > 0 and _total_chars / chars_per_token > _threshold:
                keep_recent -= 1
                if keep_recent > 0:
                    kept_segments = segments[-keep_recent:]
                else:
                    kept_segments = []
                to_keep = []
                for seg in kept_segments:
                    to_keep.extend(seg)
                new_messages = [summary_msg] + to_keep
                _total_chars = total_content_chars(new_messages)
                log.info(
                    "conv_store compact: still over threshold, reduced keep_recent to %d",
                    keep_recent,
                )
        except Exception:
            pass  # safety net: never break compact flow

        after = len(new_messages)
        result = {
            "before": before, "after": after,
            "total_segments": len(segments),
            "kept_segments": len(kept_segments),
            "compressed_segments": len(segments) - len(kept_segments),
        }
        if after >= before:
            # [2026-05-17] Why: callers treat after==before as an unsuccessful
            # compact, so persisting a same-length replacement can mutate durable
            # history while the engine still believes compact failed. How: return
            # counts without replace_all unless the new message list is shorter.
            # Purpose: avoid no-op rewrites and prevent compact retry loops.
            return result

        store.replace_all(target_session_id, new_messages)

        self._sync_compact_to_branches(target_session_id, summary_msg, to_keep)
        return result

    def _clone_compact_summary_message(self, summary_msg: Any) -> Any:
        """Clone a compact summary message for a branch session."""

        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import Message, MessageType

        return Message(
            id=str(uuid4()),
            role=getattr(summary_msg, "role", "user") or "user",
            content=str(getattr(summary_msg, "content", "") or ""),
            message_type=getattr(summary_msg, "message_type", "") or MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=dict(getattr(summary_msg, "meta", {}) or {}),
            source_node_id=getattr(summary_msg, "source_node_id", "") or "",
            source_task_id=getattr(summary_msg, "source_task_id", "") or "compact_summary",
        )

    def _sync_compact_to_branches(
        self, target_session_id: str, summary_msg: Any, to_keep: list[Any],
    ) -> None:
        """Synchronize a successful parent compact into active branch sessions."""
 
        from engine.conversation_store import ConversationStore

        target = str(target_session_id or "").strip()
        if not target:
            return
        keep_recent = len(to_keep)
        store = ConversationStore(self.workspace_root / "data" / "conversations")

        entry_branches: dict[str, dict[str, Any]] = {}
        child_sessions: set[str] = set()
        for task in list(self.tasks.values()):
            if self._task_terminal(task):
                continue
            parent_sid = str(task.input.get("parent_session_id") or "").strip()
            branch_sid = str(task.input.get("branch_session_id") or "").strip()
            if parent_sid == target and branch_sid:
                info = entry_branches.setdefault(branch_sid, {"tasks": [], "base_count": 0})
                info["tasks"].append(task)
                try:
                    base_count = int(task.input.get("base_count") or 0)
                except Exception:
                    base_count = 0
                if base_count > int(info.get("base_count") or 0):
                    info["base_count"] = base_count

            child_sid = str(task.input.get("child_session_id") or "").strip()
            if task.session_id == target and child_sid:
                child_sessions.add(child_sid)

        for branch_sid, info in entry_branches.items():
            self._sync_entry_branch_compact_locked(
                store,
                branch_sid,
                summary_msg,
                keep_recent=keep_recent,
                base_count=int(info.get("base_count") or 0),
                tasks=list(info.get("tasks") or []),
            )

        for child_sid in child_sessions:
            self._sync_child_session_compact_locked(
                store,
                child_sid,
                summary_msg,
                keep_recent=keep_recent,
            )

    def _sync_entry_branch_compact_locked(
        self,
        store: Any,
        branch_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
        base_count: int,
        tasks: list[Task],
    ) -> None:
        """Compact the forked parent prefix of one active entry branch."""
        # [AutoC 2026-05-13] Why: entry branch merge uses base_count to append only
        # branch-local tail messages back to the parent. How: replace only the
        # forked prefix with summary + recent prefix messages, then update base_count
        # on active tasks. Purpose: branch sync cannot lose or duplicate the branch
        # tail during final merge.
        branch = str(branch_session_id or "").strip()
        if not branch or base_count <= 0:
            return
        try:
            branch_msgs = list(store.load(branch))
            if len(branch_msgs) < base_count:
                log.warning(
                    "compact branch sync skipped for %s: base_count=%d exceeds len=%d",
                    branch,
                    base_count,
                    len(branch_msgs),
                )
                return
            if base_count <= keep_recent + 1:
                return
            branch_prefix = branch_msgs[:base_count]
            branch_tail = branch_msgs[base_count:]
            prefix_keep = branch_prefix[-keep_recent:] if keep_recent > 0 else []
            branch_summary = self._clone_compact_summary_message(summary_msg)
            new_base = [branch_summary] + prefix_keep
            store.replace_all(branch, new_base + branch_tail)
            for task in tasks:
                task.input["base_count"] = len(new_base)
                task.input["base_last_id"] = getattr(prefix_keep[-1], "id", "") if prefix_keep else branch_summary.id
                task.updated_at = _now()
            log.info(
                "compact branch sync: %s base %d → %d, preserved tail=%d",
                branch,
                base_count,
                len(new_base),
                len(branch_tail),
            )
        except Exception as exc:
            log.warning("compact branch sync failed for %s: %s", branch, exc)

    def _sync_child_session_compact_locked(
        self,
        store: Any,
        child_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
    ) -> None:
        """Apply simplified compact sync to one active ordinary child session."""
        # [AutoC 2026-05-13] Why: ordinary child sessions are not merged by
        # base_count, so they can use the same simple summary + recent messages
        # shape as the parent. How: replace the whole child JSONL when doing so
        # shortens it. Purpose: long-lived active child sessions do not keep an
        # outdated uncompressed parent fork.
        child = str(child_session_id or "").strip()
        if not child:
            return
        try:
            child_msgs = list(store.load(child))
            before = len(child_msgs)
            if before <= keep_recent + 1:
                return
            child_keep = child_msgs[-keep_recent:] if keep_recent > 0 else []
            store.replace_all(child, [self._clone_compact_summary_message(summary_msg)] + child_keep)
            log.info(
                "compact child sync: %s %d → %d",
                child,
                before,
                1 + len(child_keep),
            )
        except Exception as exc:
            log.warning("compact child sync failed for %s: %s", child, exc)

    def _resume_compact_parent_locked(
        self, caller: Task, context_ref: str, *,
        compact_result: dict[str, int] | None = None,
        before: int = 0, after: int = 0, success: bool = True,
    ) -> None:
        """恢复等待压缩的父 task。"""
        caller.status = TaskStatus.pending
        caller.waiting_for_task_id = None
        caller.worker_id = None
        caller.lease_expires_at = None
        caller.updated_at = _now()
        if compact_result:
            _rd = {"type": "compact_done", "success": success, **compact_result}
        else:
            _rd = {"type": "compact_done", "success": success, "before": before, "after": after}
        caller.input["resume_data"] = _rd
        caller.input["context_ref"] = context_ref
        caller.input.pop("_compact_dispatch_pending", None)
        self._event_task_snapshot("task_resumed", caller)


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
