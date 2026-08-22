"""Supervisor 核心状态 —— 组合类，继承三个 mixin。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# adapter 侧 platform_auth.platform 的取值，与 toolbox 那份判断保持一致。
_QQ_PLATFORMS = {"qq", "onebot", "onebot11"}

from ._helpers import SessionInfo, _now
from .branch_finalize_store import BranchFinalizeClaimStore
from .compact_breaker import CompactBreaker
from .qq_intent import IntentRegistry
from .eventlog import EventLog, SYSTEM_SESSION_ID
from engine.builtin.loader import auto_discover_and_register
from engine.hooks import HookRegistry
from .policy import PolicyEngine
from .session import SessionMixin
from .session_store import SessionStore
from .task_router import TaskRouterMixin
from .task_store import TaskStoreMixin
from .types import (
    AdminStateOut,
    Approval,
    ApprovalStatus,
    OpRequestOut,
    SafetyLevel,
    Task,
    RouteStatus,
    TaskKind,
    TaskStatus,
)


class SupervisorState(SessionMixin, TaskStoreMixin, TaskRouterMixin):
    """Supervisor 核心状态。管理会话、inbound 队列、task 队列与审批。"""

    def __init__(self, *, workspace_root: Path, eventlog: EventLog, policy: PolicyEngine) -> None:
        self.workspace_root = workspace_root
        self.eventlog = eventlog
        self.policy = policy
        self.started_at = _now()
        # main 启动时注入。带图路由要问「当前渠道收不收图」，没注入就按老行为走。
        self.config_store: Any = None

        self._lock = threading.RLock()

        self.sessions: dict[str, SessionInfo] = {}
        self.conversation_map: dict[str, str] = {}
        self.session_generations: dict[str, int] = {}

        self.approvals: dict[str, Approval] = {}

        self.tasks: dict[str, Task] = {}
        self._task_order: list[str] = []

        self._engine_last_seen_at: datetime | None = None
        self._engine_last_worker_id: str | None = None

        self._inbound_order: list[int] = []
        self._inbound_events: dict[int, dict[str, Any]] = {}
        self._inbound_processed: set[int] = set()
        self._inbound_routed: dict[int, dict[str, Any]] = {}
        self._inbound_cursor: int = 0
        self._post_work_states: dict[str, str] = {}
        self._post_work_enqueued: set[str] = set()
        self._post_work_running: set[str] = set()
        self._post_phase_states: dict[str, str] = {}
        self._post_effect_states: dict[str, str] = {}
        self._memory_extract_intents: dict[str, dict[str, Any]] = {}
        self._memory_extract_intent_timers: dict[str, threading.Timer] = {}
        self._recovered_post_works: dict[str, Any] = {}
        self._defer_post_work_enqueue = True
        self._branch_finalize_claims: dict[str, dict[str, Any]] = {}
        self._branch_finalize_lease_seconds = 6.0
        self._branch_finalize_store = BranchFinalizeClaimStore(
            workspace_root / "data" / "branch_finalize_claims.sqlite3",
        )
        self._outbound_delivery_ids: set[str] = {
            str((event.get("payload") or {}).get("delivery_id") or "").strip()
            for event in eventlog.events
            if event.get("type") == "outbound_message"
            and isinstance(event.get("payload"), dict)
            and str((event.get("payload") or {}).get("delivery_id") or "").strip()
        }

        @dataclass
        class _InboundLease:
            worker_id: str
            expires_at: datetime

        self._InboundLease = _InboundLease
        self._inbound_leases: dict[int, _InboundLease] = {}

        self._cancelled_sessions: set[str] = set()
        self._tools_reload_seq: int = 0
        self.session_entry_overrides: dict[str, str] = {}  # session_id -> node_id (AI switch)
        self.session_last_entry_node: dict[str, str] = {}  # session_id -> last used entry node
        self._session_context_usage: dict[str, dict[str, Any]] = {}  # session_id -> latest usage
        self.compact_breaker = CompactBreaker()
        self.qq_intent = IntentRegistry()
        self._engine_generations: dict[str, str] = {}  # worker_id -> generation_id (Direction 2)
        self._pending_restart_notify: str | None = None  # session_id to notify after engine registers

        # Why: supervisor feature side effects should be registered handlers rather
        # than hard-coded router or scheduler branches. How: create one unified
        # HookRegistry per SupervisorState and auto-discover built-ins before
        # session-store reconciliation. Purpose: runtime hook notifications can run
        # without storing mutable handler state on SupervisorState or using a
        # separate supervisor registry.
        self.hook_registry = HookRegistry()
        # Why: built-in supervisor handlers now live under engine.builtin, where
        # they cannot import SupervisorState or other supervisor internals. How:
        # register every PLUGIN_META declaration through the auto loader and retain
        # the returned instances for diagnostics and existing tests. Purpose:
        # preserve handler-owned timer/cursor state while removing cyclic imports
        # and hard-coded registration.
        _builtin_handlers = auto_discover_and_register(self.hook_registry)
        # Why: the Clonoth runtime may temporarily run with a reduced built-in
        # handler set while features are being rolled out. How: keep optional
        # references with dict.get instead of indexing. Purpose: synchronizing
        # this file does not remove the existing deployment compatibility guard.
        self._memory_extract_handler = _builtin_handlers.get("memory_extract")
        self._dream_handler = _builtin_handlers.get("dream")

        # External plugins: same mechanism as engine, scan plugins/ directory
        from engine.hooks.loader import load_external_plugins
        _plugins_dir = workspace_root / "plugins"
        _ext_count = load_external_plugins(self.hook_registry, _plugins_dir)
        if _ext_count:
            logger.info("Loaded %d external plugin(s) for supervisor hooks", _ext_count)

        # ---- Child Session 隔离（Phase A）：映射表 ----
        # (parent_session_id, node_id, context_key) → child_session_id
        self.child_session_map: dict[tuple[str, str, str], str] = {}
        # 反向索引：parent_session_id → set of child_session_ids，用于 clear 时快速查找
        self.parent_children: dict[str, set[str]] = {}
        # [Fork/Merge 2026-05-12] 记录入口分支与主 session 的关系。
        # 原因：入口 task 现在运行在 branch session 上，主 session 的取消、重置和
        # running_tasks 查询仍需要找到这些分支。做法：维护 parent→branches 与
        # branch→parent 两张轻量内存索引，持久清理仍复用 parent_children/session_store。
        # 目的：允许同一主 session 下多个入口分支并发执行，同时保持旧 session 映射不变。
        self.parent_entry_branches: dict[str, set[str]] = {}
        self.entry_branch_parents: dict[str, str] = {}

        # ---- 方案 A: 独立 session 持久化 ----
        self._session_store = SessionStore(workspace_root / "data" / "sessions.json")
        loaded_sessions, loaded_conv_map, loaded_child_map, loaded_parent_children = self._session_store.load()
        if loaded_sessions:
            self.sessions.update(loaded_sessions)
            self.conversation_map.update(loaded_conv_map)
        # Child Session 隔离（Phase A）：从 sessions.json 恢复 child session 映射
        if loaded_child_map:
            self.child_session_map.update(loaded_child_map)
        if loaded_parent_children:
            for psid, children in loaded_parent_children.items():
                self.parent_children.setdefault(psid, set()).update(children)
        # [Fork/Merge 2026-05-17] Why: sessions.json stores entry branches as
        # child sessions with node_id="__entry_branch__", but the fast branch→parent
        # indexes are in-memory only. How: rebuild those indexes from the loaded
        # child registry before startup reconciliation. Purpose: public APIs and
        # async dispatch can resolve branch sessions correctly after supervisor restart.
        for psid, children in self.parent_children.items():
            for child_sid in children:
                raw = self._session_store.get_entry(child_sid)
                if isinstance(raw, dict) and raw.get("node_id") == "__entry_branch__":
                    self.entry_branch_parents[child_sid] = psid
                    self.parent_entry_branches.setdefault(psid, set()).add(child_sid)

        self._restore_post_completion_work_from_events()
        self._restore_branch_finalize_claims_from_events()
        self._restore_memory_extract_intents_from_events()
        self._restore_recoverable_routes_from_events()
        self.recover_pending_completion_routes()
        self._reconcile_after_restart()
        self._recover_memory_extract_intents()
        self._defer_post_work_enqueue = False
        self.recover_pending_post_completion_work()

    def reload_session_registry(self) -> int:
        """重读 sessions.json 并重建会话键映射，返回活跃会话数。

        账号迁移改的是盘上的 conversation_key，而内存才是权威 —— 不重读的话
        下一次 flush 会把旧键写回去，迁移结果当场失效。
        """
        loaded_sessions, loaded_conv_map, _child_map, _parent_children = self._session_store.reload()
        with self._lock:
            self.sessions.clear()
            self.sessions.update(loaded_sessions)
            self.conversation_map.clear()
            self.conversation_map.update(loaded_conv_map)
        return len(loaded_sessions)

    def _restore_branch_finalize_claims_from_events(self) -> int:
        """Seed missing SQLite identities from legacy EventLog ownership receipts."""
        event_types = {
            "task_branch_finalize_claimed", "task_branch_finalize_started",
            "task_branch_finalize_heartbeat", "task_branch_finalize_completed",
            "task_branch_finalize_failed",
        }
        claims: dict[str, dict[str, Any]] = {}
        for event in self.eventlog.iter_persisted_events():
            if event.get("type") not in event_types:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            identity = str(payload.get("identity") or "").strip()
            if identity:
                claims[identity] = dict(payload)
        for identity, legacy_claim in claims.items():
            self._branch_finalize_store.seed_legacy(identity, legacy_claim)
            current = self._branch_finalize_store.get(identity)
            if current is not None:
                self._branch_finalize_claims[identity] = dict(current.__dict__)
        return len(claims)

    def _restore_post_completion_work_from_events(self) -> int:
        """Rebuild schema-v2 work plus each phase/effect's latest ledger state."""
        from .task_router import PostCompletionWork

        works: dict[str, PostCompletionWork] = {}
        states: dict[str, str] = {}
        status_types = {
            "task_post_work_pending",
            "task_post_work_started",
            "task_post_work_completed",
            "task_post_work_failed",
        }
        for event in self.eventlog.iter_persisted_events():
            event_type = str(event.get("type") or "")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event_type == "task_routed":
                if int(payload.get("post_work_schema_version") or 0) < 2:
                    continue
                post_work_id = str(payload.get("post_work_id") or "").strip()
                if not post_work_id:
                    continue
                try:
                    task = Task.model_validate(payload)
                except Exception:
                    logger.exception(
                        "invalid post-work intent snapshot",
                        extra={"event": "task_post_work_restore_invalid", "post_work_id": post_work_id},
                    )
                    continue
                route_session_id = str(
                    (task.route_context or {}).get("route_session_id")
                    or (task.input or {}).get("_route_session_id")
                    or (task.input or {}).get("parent_session_id")
                    or task.session_id
                )
                works[post_work_id] = PostCompletionWork(
                    task=task,
                    route_session_id=route_session_id,
                    session_generation=task.session_generation,
                    post_work_id=post_work_id,
                    schema_version=int(task.post_work_schema_version or 2),
                    phases=[dict(phase) for phase in task.post_work_phases if isinstance(phase, dict)],
                )
                states[post_work_id] = str(task.post_work_status or "pending")
            elif event_type in status_types:
                if int(payload.get("post_work_schema_version") or 0) < 2:
                    continue
                post_work_id = str(payload.get("post_work_id") or "").strip()
                if post_work_id:
                    states[post_work_id] = str(payload.get("status") or "")
            elif event_type.startswith("task_post_phase_"):
                if int(payload.get("post_work_schema_version") or 0) < 2:
                    continue
                phase_id = str(payload.get("phase_id") or "").strip()
                if phase_id:
                    self._post_phase_states[phase_id] = str(payload.get("status") or event_type.rsplit("_", 1)[-1])
            elif event_type in {"task_post_effect_started", "task_post_effect_completed"}:
                if int(payload.get("post_work_schema_version") or 0) < 2:
                    continue
                effect_id = str(payload.get("effect_id") or "").strip()
                if effect_id:
                    self._post_effect_states[effect_id] = str(payload.get("status") or event_type.rsplit("_", 1)[-1])

        self._post_work_states.update(states)
        self._recovered_post_works.update({
            work_id: work for work_id, work in works.items()
            if states.get(work_id, "pending") in {"pending", "started", "partial"}
        })
        logger.info(
            "post-completion work intents restored",
            extra={
                "event": "task_post_work_restore_scanned",
                "pending_count": len(self._recovered_post_works),
            },
        )
        return len(self._recovered_post_works)

    def _restore_memory_extract_intents_from_events(self) -> int:
        """Restore the latest durable state of each delayed extraction intent."""
        restored: dict[str, dict[str, Any]] = {}
        for event in self.eventlog.iter_persisted_events():
            event_type = str(event.get("type") or "")
            if event_type not in {
                "memory_extract_intent_scheduled",
                "memory_extract_intent_cancelled",
                "memory_extract_intent_completed",
            }:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            intent_id = str(payload.get("intent_id") or "").strip()
            if not intent_id:
                continue
            if event_type == "memory_extract_intent_scheduled":
                intent = payload.get("intent")
                if isinstance(intent, dict):
                    restored[intent_id] = dict(intent)
            else:
                restored.pop(intent_id, None)
        self._memory_extract_intents.update(restored)
        logger.info(
            "memory extraction intents restored",
            extra={"event": "memory_extract_intent_restore", "pending_count": len(restored)},
        )
        return len(restored)

    def _memory_extract_intent_session(self, intent: dict[str, Any]) -> str:
        return str(intent.get("session_id") or "").strip() or SYSTEM_SESSION_ID

    def _append_memory_extract_intent_event_locked(
        self, event_type: str, intent: dict[str, Any],
    ) -> None:
        intent_id = str(intent.get("intent_id") or "").strip()
        payload: dict[str, Any] = {
            "intent_id": intent_id,
            "effect_id": str(intent.get("effect_id") or ""),
            "phase_id": str(intent.get("phase_id") or ""),
            "post_work_id": str(intent.get("post_work_id") or ""),
            "task_id": str(intent.get("source_task_id") or ""),
            "session_id": str(intent.get("session_id") or ""),
            "node_id": str(intent.get("extractor_node") or ""),
            "due_at": str(intent.get("due_at") or ""),
        }
        if event_type == "memory_extract_intent_scheduled":
            payload["intent"] = dict(intent)
        self.eventlog.append(
            session_id=self._memory_extract_intent_session(intent),
            component="supervisor",
            type_=event_type,
            payload=payload,
        )

    @staticmethod
    def _intent_delay(intent: dict[str, Any]) -> float:
        raw = str(intent.get("due_at") or "").strip()
        if not raw:
            return 0.0
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            return max(0.0, (due - _now()).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def _arm_memory_extract_intent_locked(
        self, intent: dict[str, Any], *, retry_delay: float | None = None,
    ) -> None:
        intent_id = str(intent.get("intent_id") or "").strip()
        if not intent_id:
            return
        old = self._memory_extract_intent_timers.pop(intent_id, None)
        if old is not None:
            old.cancel()
        delay = max(0.0, retry_delay if retry_delay is not None else self._intent_delay(intent))
        timer = threading.Timer(delay, self._fire_memory_extract_intent, args=[intent_id])
        timer.daemon = True
        self._memory_extract_intent_timers[intent_id] = timer
        timer.start()

    def _persist_memory_extract_intent(self, intent: dict[str, Any]) -> bool:
        """Durably publish and arm one delayed extraction before the hook returns."""
        value = dict(intent or {})
        intent_id = str(value.get("intent_id") or value.get("effect_id") or "").strip()
        if not intent_id:
            raise ValueError("memory extraction intent requires intent_id")
        value["intent_id"] = intent_id
        if not str(value.get("session_id") or "").strip():
            raise ValueError("memory extraction intent requires session_id")
        with self._lock:
            effect_id = str(value.get("effect_id") or "").strip()
            if effect_id and self._post_effect_states.get(effect_id) == "completed":
                return False
            previous = self._memory_extract_intents.get(intent_id)
            if isinstance(previous, dict):
                # A replay may recompute a later due_at. Keep the first durable
                # schedule so repeated recovery cannot postpone or duplicate it.
                self._arm_memory_extract_intent_locked(previous)
                return True
            self._append_memory_extract_intent_event_locked(
                "memory_extract_intent_scheduled", value,
            )
            self._memory_extract_intents[intent_id] = value
            self._arm_memory_extract_intent_locked(value)
            return True

    def _finish_memory_extract_intent_locked(
        self, intent: dict[str, Any], *, cancelled: bool,
    ) -> None:
        intent_id = str(intent.get("intent_id") or "").strip()
        if not intent_id or intent_id not in self._memory_extract_intents:
            return
        timer = self._memory_extract_intent_timers.pop(intent_id, None)
        if timer is not None:
            timer.cancel()
        self._append_memory_extract_intent_event_locked(
            "memory_extract_intent_cancelled" if cancelled else "memory_extract_intent_completed",
            intent,
        )
        self._memory_extract_intents.pop(intent_id, None)
        effect_id = str(intent.get("effect_id") or "").strip()
        if effect_id and self._post_effect_states.get(effect_id) != "completed":
            self.eventlog.append(
                session_id=self._memory_extract_intent_session(intent),
                component="supervisor",
                type_="task_post_effect_completed",
                payload={
                    "post_work_schema_version": 2,
                    "post_work_id": str(intent.get("post_work_id") or ""),
                    "phase_id": str(intent.get("phase_id") or ""),
                    "phase_kind": "hook",
                    "phase_name": "memory_extract",
                    "effect_id": effect_id,
                    "status": "completed",
                    "task_id": str(intent.get("source_task_id") or ""),
                    "session_id": str(intent.get("session_id") or ""),
                },
            )
            self._post_effect_states[effect_id] = "completed"

    def _complete_memory_extract_intent(self, intent_id: str) -> None:
        with self._lock:
            intent = self._memory_extract_intents.get(str(intent_id or "").strip())
            if isinstance(intent, dict):
                self._finish_memory_extract_intent_locked(intent, cancelled=False)

    def _cancel_memory_extract_intents(self, session_id: str) -> int:
        sid = str(session_id or "").strip()
        if not sid:
            return 0
        with self._lock:
            matches = [
                intent for intent in self._memory_extract_intents.values()
                if str(intent.get("session_id") or "").strip() == sid
            ]
            for intent in matches:
                self._finish_memory_extract_intent_locked(intent, cancelled=True)
            return len(matches)

    def _memory_extract_task_exists(self, intent_id: str) -> bool:
        key = str(intent_id or "").strip()
        if not key:
            return False
        with self._lock:
            if any(
                str((task.input or {}).get("_memory_extract_intent_id") or "") == key
                for task in self.tasks.values()
            ):
                return True
        for event in self.eventlog.iter_persisted_events():
            if event.get("type") != "task_created":
                continue
            payload = event.get("payload")
            task_input = payload.get("input") if isinstance(payload, dict) else None
            if isinstance(task_input, dict) and str(task_input.get("_memory_extract_intent_id") or "") == key:
                return True
        return False

    def _fire_memory_extract_intent(self, intent_id: str) -> None:
        key = str(intent_id or "").strip()
        with self._lock:
            self._memory_extract_intent_timers.pop(key, None)
            intent = self._memory_extract_intents.get(key)
        if not isinstance(intent, dict):
            return
        handler = self._memory_extract_handler
        if handler is None or not hasattr(handler, "fire_persisted_intent"):
            logger.error("memory extraction intent has no capable handler", extra={"intent_id": key})
            with self._lock:
                if key in self._memory_extract_intents:
                    self._arm_memory_extract_intent_locked(intent, retry_delay=5.0)
            return
        try:
            handler.fire_persisted_intent(self._build_supervisor_hook_ctx(), dict(intent))
        except Exception:
            logger.exception("memory extraction intent execution failed", extra={"intent_id": key})
            with self._lock:
                if key in self._memory_extract_intents:
                    self._arm_memory_extract_intent_locked(intent, retry_delay=5.0)

    def _recover_memory_extract_intents(self) -> int:
        handler = self._memory_extract_handler
        intents = list(self._memory_extract_intents.values())
        if handler is not None and hasattr(handler, "restore_persisted_intent"):
            for intent in intents:
                handler.restore_persisted_intent(dict(intent))
        with self._lock:
            for intent in intents:
                self._arm_memory_extract_intent_locked(intent)
        return len(intents)


    def recover_pending_post_completion_work(self) -> int:
        """Idempotently queue restored pending/started post work."""
        queued = 0
        for work_id, work in list(self._recovered_post_works.items()):
            if self._post_work_states.get(work_id, "pending") not in {"pending", "started", "partial"}:
                continue
            if self._enqueue_post_completion_work(work):
                queued += 1
        return queued

    def _restore_recoverable_routes_from_events(self) -> int:
        """Restore only versioned terminal route intents from the EventLog tail.

        Legacy terminal snapshots lack route_schema_version and are deliberately
        considered handled. Non-terminal snapshots are not reintroduced into the
        live worker queue by this completion-only recovery path.
        """
        task_event_types = {
            "task_created", "task_started", "task_completed", "task_cancelled",
            "task_cancel_requested", "task_requeued", "task_suspended",
            "task_resumed", "task_route_failed", "task_branch_finalized", "task_routed",
        }
        snapshots: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in self.eventlog.iter_persisted_events():
            if event.get("type") not in task_event_types:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                continue
            previous = snapshots.get(task_id)
            merged = dict(payload)
            raw_input = merged.get("input")
            if (
                isinstance(raw_input, dict)
                and raw_input.get("_input_slimmed")
                and isinstance(previous, dict)
                and isinstance(previous.get("input"), dict)
            ):
                preserved_input = dict(previous["input"])
                preserved_input.update({
                    key: value for key, value in raw_input.items()
                    if key != "_input_slimmed"
                })
                merged["input"] = preserved_input
            snapshots[task_id] = merged
            if task_id not in order:
                order.append(task_id)

        restored = 0
        legacy_terminal = 0
        for task_id in order:
            payload = snapshots[task_id]
            status = str(payload.get("status") or "")
            terminal = status in {"completed", "failed", "cancelled"}
            if not terminal:
                continue
            if int(payload.get("route_schema_version") or 0) < 1:
                legacy_terminal += 1
                continue
            if str(payload.get("route_status") or "pending") == RouteStatus.routed.value:
                continue
            if not str(payload.get("delivery_id") or "").strip():
                continue
            try:
                task = Task.model_validate(payload)
            except Exception:
                logger.exception(
                    "invalid recoverable completion snapshot",
                    extra={"event": "task_route_restore_invalid", "task_id": task_id},
                )
                continue
            self.tasks[task_id] = task
            self._task_order.append(task_id)
            restored += 1

        logger.info(
            "completion route intents restored",
            extra={
                "event": "task_route_restore_scanned",
                "restored_count": restored,
                "legacy_terminal_skipped": legacy_terminal,
            },
        )
        return restored

    def recover_pending_completion_routes(self) -> int:
        """Retry restored versioned terminal routes without replaying legacy tasks."""
        recovered = 0
        with self._lock:
            candidates = [
                task for task in self.tasks.values()
                if self._task_terminal(task)
                and int(task.route_schema_version or 0) >= 1
                and bool(str(task.delivery_id or "").strip())
                and task.route_status != RouteStatus.routed
            ]
        for task in candidates:
            try:
                self._route_completed_task(task)
                recovered += 1
            except Exception:
                with self._lock:
                    extra = self._task_route_log_extra_locked(
                        task, event="task_route_recovery_failed",
                    )
                logger.exception("startup completion route recovery failed", extra=extra)
        if recovered:
            logger.info(
                "startup completion routes recovered",
                extra={"event": "task_route_recovered", "recovered_count": recovered},
            )
        return recovered

    def _build_supervisor_hook_ctx(self, **extra: Any) -> dict[str, Any]:
        """Build the callback-only context passed to built-in supervisor hooks."""
        # Why: engine.builtin handlers must not import or keep a reference to
        # SupervisorState. How: expose the small set of required supervisor
        # operations as callbacks and plain values. Purpose: make hook handlers
        # cycle-free while preserving the previous behavior at each fire point.
        ctx: dict[str, Any] = {
            "workspace_root": self.workspace_root,
            "session_messages": lambda sid, limit=0: self.session_messages(session_id=sid, limit=limit),
            "create_task": self._create_task_from_hook_locked,
            "acquire_lock": self._lock,
            "format_transcript": self._format_transcript_for_extract_callback,
            "current_session_generation": lambda sid: self._current_session_generation_locked(sid),
            "session_count": lambda: len(self.sessions),
            "task_snapshots": self._task_snapshots_from_hook_locked,
            "persist_memory_extract_intent": self._persist_memory_extract_intent,
            "cancel_memory_extract_intents": self._cancel_memory_extract_intents,
            "complete_memory_extract_intent": self._complete_memory_extract_intent,
            "memory_extract_task_exists": self._memory_extract_task_exists,
            "post_outbound": self._post_outbound_from_hook_locked,
        }
        ctx.update(extra)
        return ctx

    def fire_manual_schedule(self, schedule_type: str, **extra: Any) -> dict[str, Any]:
        """按需触发一次系统级定时特性，返回 handler 写回 outcome 的内容。

        走的是 cron 那条 on_schedule_tick 路径，handler 靠 manual 标志自己决定
        跳过哪些门槛 —— 复制一条并行的启动路径迟早会和定时那条走散。
        """
        outcome: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        with self._lock:
            self.hook_registry.fire(
                "on_schedule_tick",
                self._build_supervisor_hook_ctx(
                    schedule_type=str(schedule_type or "").strip(),
                    manual=True,
                    outcome=outcome,
                    now=now,
                    now_key=now.strftime("%Y-%m-%d %H:%M"),
                    **extra,
                ),
            )
        return outcome

    def _post_outbound_from_hook_locked(self, *, session_id: str, text: str) -> bool:
        """让内置 handler 直接往会话推一条消息，不经过 LLM。

        用于长流程结束后回报结果 —— 触发的人早已离开那一轮对话，没有 inbound 可以搭。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return False
        try:
            result = self.append_outbound_message(session_id=sid, text=str(text or ""))
        except Exception:
            logger.exception("hook outbound failed", extra={"session_id": sid})
            return False
        return bool(result.get("ok", True))

    def _task_snapshots_from_hook_locked(self, task_ids: list[str]) -> dict[str, dict[str, str]]:
        """Return safe task status/result snapshots for built-in hook handlers."""
        # [AutoC 2026-05-31] Why: built-in handlers such as Dream need to poll
        # child task completion without importing SupervisorState or receiving
        # mutable Task objects. How: expose only status, result text, and error
        # strings through the hook context. Purpose: keep the capability generic
        # and avoid adding feature-specific branches to the core router.
        result: dict[str, dict[str, str]] = {}
        for tid in task_ids:
            task_id = str(tid or "")
            task = self.tasks.get(task_id)
            if task is None:
                result[task_id] = {"status": "missing", "result_text": "", "error": ""}
                continue
            task_result = task.result or {}
            inner = task_result.get("result", {}) if isinstance(task_result.get("result"), dict) else {}
            result[task_id] = {
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "result_text": str(inner.get("text") or task_result.get("text") or ""),
                "error": str(task_result.get("error") or ""),
            }
        return result

    def _create_task_from_hook_locked(self, **kwargs: Any) -> Task:
        """Create a task for a built-in hook using callback-friendly arguments."""
        # Why: relocated supervisor handlers cannot import TaskKind or call session
        # helpers directly. How: normalize string/enum kind values and optionally
        # create a session from channel plus conversation_key before delegating to
        # _create_task_locked. Purpose: keep handler code callback-only while still
        # using the existing task creation path and event snapshot behavior.
        data = dict(kwargs)
        conversation_key = str(data.pop("conversation_key", "") or "").strip()
        channel = str(data.pop("channel", "") or "").strip()
        if not str(data.get("session_id") or "").strip():
            if not conversation_key:
                raise ValueError("session_id or conversation_key is required")
            if not channel:
                channel = conversation_key.split(":", 1)[0] if ":" in conversation_key else "system"
            data["session_id"] = self.get_or_create_session(channel=channel, conversation_key=conversation_key)

        sid = str(data.get("session_id") or "").strip()
        generation = self._to_positive_int(data.get("session_generation"))
        if generation is None:
            generation = self._current_session_generation_locked(sid) or 1
        data["session_generation"] = generation
        if sid and not self.session_generations.get(sid):
            self.session_generations[sid] = generation

        kind = data.get("kind") or TaskKind.node
        if not isinstance(kind, TaskKind):
            value = getattr(kind, "value", kind)
            try:
                kind = TaskKind(str(value or "node"))
            except Exception:
                kind = TaskKind.node
        data["kind"] = kind

        input_data = data.get("input_data")
        if isinstance(input_data, dict):
            task_context = input_data.get("task_context")
            if isinstance(task_context, dict):
                # Why: hooks that create tasks directly bypass inbound processing,
                # which used to add these identifiers. How: fill missing values in
                # the provided task_context. Purpose: preserve system-task metadata
                # for engine-side checks without requiring supervisor imports.
                task_context.setdefault("session_id", sid)
                task_context.setdefault("session_generation", generation)

        data.setdefault("continuation", {})
        data.setdefault("source_inbound_seq", None)
        data.setdefault("caller_task_id", None)
        return self._create_task_locked(**data)

    @staticmethod
    def _format_transcript_for_extract_callback(messages: list[dict[str, Any]], *, max_chars: int = 12000) -> str:
        """Format conversation messages into transcript text for extraction hooks."""
        # Why: MemoryExtractHandler now receives formatting as an injected callback
        # rather than importing supervisor code. How: keep the previous pure
        # formatter on SupervisorState and pass it through ctx. Purpose: retain the
        # fallback transcript format while removing handler-side supervisor imports.
        parts: list[str] = []
        total = 0
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                ]
                content = "\n".join(texts)
            if not isinstance(content, str):
                content = str(content)
            if len(content) > 2000:
                content = content[:2000] + "...<truncated>"
            line = f"[{role}]\n{content}"
            total += len(line)
            if total > max_chars:
                break
            parts.append(line)
        parts.reverse()
        return "\n\n---\n\n".join(parts)

    def _reconcile_after_restart(self) -> None:
        """启动期状态协调：清理上一进程遗留的运行期状态。

        [AutoC 2026-05-30] Why: 移除 EventLog 启动回放后，需要独立的
        启动清理逻辑替代原有的 cancel_orphaned_tasks 等依赖回放重建的机制。
        How: 从 sessions.json 扫描并清理 orphan branch、过期 child session。
        Purpose: 确保重启后内存状态干净，不留上一进程的运行期残余。
        """
        cleaned = 0
        stale_sids: list[str] = []
        seen_stale: set[str] = set()

        def mark_stale(session_id: str) -> None:
            """记录待清理 session，保持顺序并避免重复。"""
            # [AutoC 2026-05-30] Why: 同一个 session 可能同时满足 reset、branch、
            # 过期 child 等条件。How: 用 set 做去重，用 list 保留可读的处理顺序。
            # Purpose: 清理日志和计数稳定，不重复删除同一条 sessions.json 记录。
            sid = str(session_id or "").strip()
            if sid and sid not in seen_stale:
                seen_stale.add(sid)
                stale_sids.append(sid)

        # 1. 清理 sessions.json 中 reset=true 的条目。
        for sid, entry in self._session_store.items_snapshot():
            if isinstance(entry, dict) and entry.get("reset"):
                mark_stale(sid)

        # 2. 清理 orphan branch session（重启后上一进程的 branch 不再有活跃任务）。
        for sid in self._session_store.keys_snapshot():
            if str(sid).startswith("branch_"):
                mark_stale(sid)

        # 3. 清理非 accumulate 的 child session。
        # [AutoC 2026-05-30] 与 _cleanup_stale_sessions_locked 保持一致：
        #   - accumulate: 永久保留
        #   - 无 context_mode（历史遗留）: 直接清理，不等 TTL
        #   - fresh/fork: 24h 无活动后清理
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=24)
        for sid, entry in self._session_store.items_snapshot():
            if sid in seen_stale or not isinstance(entry, dict):
                continue
            if not entry.get("is_child"):
                continue
            ctx_mode = str(entry.get("context_mode") or "").strip()
            if ctx_mode == "accumulate":
                continue
            # 无 context_mode 的历史 child session 直接清理
            if not ctx_mode:
                mark_stale(sid)
                continue
            # fresh/fork: 正常 TTL 检查
            updated_str = (
                entry.get("last_active_at")
                or entry.get("updated_at")
                or entry.get("created_at")
                or ""
            )
            try:
                updated_at = datetime.fromisoformat(str(updated_str))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at < stale_threshold:
                    mark_stale(sid)
            except Exception:
                pass

        # 执行清理。
        for sid in stale_sids:
            # [AutoC 2026-05-30] Why: startup reconcile 不再依赖 EventLog 中的 task
            # 快照，因此必须同步清理 session 相关内存索引、会话文件和 registry。
            # How: 对 branch 复用已有递归清理逻辑；对普通 child/reset session 执行
            # 轻量清理并收缩 parent/child 索引。Purpose: 重启后只保留 sessions.json
            # 中仍可作为主状态源的有效 session。
            if self._is_entry_branch_session_locked(sid):
                self._cleanup_branch_locked(sid)
                cleaned += 1
                continue

            self.sessions.pop(sid, None)
            self.session_generations.pop(sid, None)
            self._cancelled_sessions.discard(sid)
            self._session_context_usage.pop(sid, None)
            self._remove_child_mapping_for_session_locked(sid)
            self.entry_branch_parents.pop(sid, None)
            for branches in self.parent_entry_branches.values():
                branches.discard(sid)
            for children in self.parent_children.values():
                children.discard(sid)
            self.parent_entry_branches = {
                parent_sid: branches
                for parent_sid, branches in self.parent_entry_branches.items()
                if branches
            }
            self.parent_children = {
                parent_sid: children
                for parent_sid, children in self.parent_children.items()
                if children
            }

            self.purge_session_files(sid)
            if self._session_store.remove_sessions([sid], flush=False) is not None:
                cleaned += 1

        if cleaned:
            self._session_store._flush()
            logger.info(
                "startup reconcile: cleaned %d stale sessions (reset/orphan-branch/expired-child)",
                cleaned,
            )
        else:
            logger.info("startup reconcile: no stale sessions found")

    # ------------------------------------------------------------------ #
    #  事件回放
    # ------------------------------------------------------------------ #

    def rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        """[DEPRECATED] 从 EventLog 事件重建内存状态。

        [AutoC 2026-05-30] 此方法不再在启动路径使用。
        EventLog 已降级为纯审计日志，启动状态完全从 sessions.json 恢复。
        保留此方法仅供测试和一次性迁移脚本使用。
        """
        for e in events:
            et = e.get("type")
            session_id = str(e.get("session_id") or "")
            payload = e.get("payload") or {}

            try:
                seq = int(e.get("seq", 0))
            except Exception:
                seq = 0

            if et == "session_created":
                self._apply_session_created(session_id, payload)
            elif et == "inbound_message":
                conv = payload.get("conversation_key")
                if isinstance(conv, str) and session_id:
                    self.conversation_map[conv] = session_id
                    # 方案 B: 若 session 记录缺失，从 inbound 事件恢复最小 SessionInfo
                    if session_id not in self.sessions:
                        self.sessions[session_id] = SessionInfo(
                            session_id=session_id,
                            channel=payload.get("channel", ""),
                            conversation_key=conv,
                            created_at=_now(),
                            updated_at=_now(),
                            # [AutoC 2026-06-01] Why: event replay may synthesize
                            # a SessionInfo for old logs that never had a stored
                            # provider override. How: leave the new field at its
                            # empty default. Purpose: rebuild compatibility stays
                            # explicit while the dataclass grows a new field.
                        )
                self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "inbound_processed":
                self._apply_inbound_processed(payload)
            elif et == "outbound_message":
                self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "approval_requested":
                self._apply_approval_requested(payload)
            elif et == "approval_decided":
                self._apply_approval_decided(payload)
            elif et in {
                "task_created", "task_started", "task_completed", "task_cancelled",
                "task_cancel_requested", "task_requeued", "task_suspended",
                "task_resumed", "task_route_failed", "task_branch_finalized", "task_routed",
            }:
                self._apply_task_snapshot(payload)
            elif et == "context_reset":
                conv = payload.get("conversation_key")
                if isinstance(conv, str):
                    self.conversation_map.pop(conv, None)
            elif et == "cancel_requested":
                self._apply_cancel_requested(session_id, payload)
            elif et == "node_switch":
                self._apply_node_switch(session_id, payload)
            elif et == "engine_registered":
                wid = str(payload.get("worker_id") or "").strip()
                gid = str(payload.get("generation_id") or "").strip()
                if wid and gid:
                    self._engine_generations[wid] = gid

        self._advance_inbound_cursor()
    # ------------------------------------------------------------------ #
    #  节点切换
    # ------------------------------------------------------------------ #

    def _apply_node_switch(self, session_id: str, payload: dict[str, Any]) -> None:
        target = str(payload.get("target_node_id") or "").strip()
        if session_id and target:
            self.session_entry_overrides[session_id] = target
        elif session_id and not target:
            self.session_entry_overrides.pop(session_id, None)

    def switch_session_node(self, session_id: str, target_node_id: str) -> dict[str, Any]:
        """设置或清除 session 级入口节点覆盖。"""
        sid = (session_id or "").strip()
        target = (target_node_id or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id required"}
        with self._lock:
            # [Fork/Merge 2026-05-17] Why: switch_node can be invoked from an
            # entry branch, but future inbound routing reads overrides from the
            # parent session. How: normalize the supplied session id before
            # mutating session_entry_overrides or writing node_switch. Purpose:
            # the switch survives branch merge/cleanup.
            route_sid = self._route_session_id_for_session_locked(sid)
            if route_sid not in self.sessions:
                return {"ok": False, "error": "session not found"}
            default_node = self._default_entry_node()
            if target and target != default_node:
                self.session_entry_overrides[route_sid] = target
                persisted_entry_node = target
            else:
                self.session_entry_overrides.pop(route_sid, None)
                target = ""  # 清除覆盖
                persisted_entry_node = ""
            info = self.sessions.get(route_sid)
            if info is not None and info.entry_node_id != persisted_entry_node:
                # Why: switch_node is an explicit session-level route change, but
                # session_entry_overrides is intentionally cleared on restart.
                # How: mirror the effective override state into SessionInfo and
                # sessions.json whenever the live switch endpoint runs. Purpose:
                # restart recovery uses the same entry node, and clearing a switch
                # does not leave a stale persisted target behind.
                info.entry_node_id = persisted_entry_node
                self._session_store.update_entry_node(route_sid, persisted_entry_node)
            self.eventlog.append(
                session_id=route_sid,
                component="engine",
                type_="node_switch",
                payload={
                    "target_node_id": target,
                    "default_node_id": default_node,
                    "ts": _now().isoformat(),
                    "requested_session_id": sid,
                },
            )
            return {
                "ok": True,
                "session_id": route_sid,
                "target_node_id": target or default_node,
                "is_override": bool(target),
            }

    def get_session_provider_override(self, session_id: str) -> dict[str, Any] | None:
        """读取 session 级 provider 覆盖配置。"""
        sid = (session_id or "").strip()
        with self._lock:
            # [AutoC 2026-06-01] Why: engine tasks may run on entry branches while
            # the durable provider override is attached to the parent session. How:
            # normalize a possible branch id before reading SessionInfo. Purpose:
            # all runtime tasks see the same session-scoped provider choice.
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            return dict(info.provider_override or {})

    def set_session_provider_override(self, session_id: str, provider_override: dict[str, Any]) -> dict[str, Any] | None:
        """设置 session 级 provider 覆盖配置并持久化。"""
        sid = (session_id or "").strip()
        clean = dict(provider_override or {}) if isinstance(provider_override, dict) else {}
        with self._lock:
            route_sid = self._route_session_id_for_session_locked(sid)
            info = self.sessions.get(route_sid)
            if info is None:
                return None
            # [AutoC 2026-06-01] Why: the API accepts arbitrary JSON dicts for
            # provider adapters and future fields. How: copy the dict into
            # SessionInfo and mirror it to sessions.json through SessionStore.
            # Purpose: runtime provider overrides are durable without constraining
            # future provider-specific configuration keys.
            info.provider_override = clean
            self._session_store.update_provider_override(route_sid, clean)
            self.eventlog.append(
                session_id=route_sid,
                component="supervisor",
                type_="provider_override_updated",
                # [AutoC 2026-06-01] Why: provider_override may contain api_key or
                # provider-specific secret fields. How: audit only safe summary
                # metadata and the field names, never the raw dict. Purpose:
                # updates remain observable without leaking credentials to
                # data/events.jsonl.
                payload={
                    "provider": str(clean.get("provider") or clean.get("provider_type") or ""),
                    "model": str(clean.get("model") or ""),
                    "keys": sorted(str(k) for k in clean.keys()),
                    "api_key_present": bool(clean.get("api_key")),
                    "requested_session_id": sid,
                    "ts": _now().isoformat(),
                },
            )
            return dict(clean)

    def clear_session_provider_override(self, session_id: str) -> dict[str, Any] | None:
        """清除 session 级 provider 覆盖配置并持久化。"""
        # [AutoC 2026-06-01] Why: DELETE should share the same persistence and
        # event path as PUT. How: delegate to the setter with an empty dict.
        # Purpose: the in-memory session and sessions.json are cleared together.
        return self.set_session_provider_override(session_id, {})

    def get_session_active_node(self, session_id: str) -> dict[str, Any]:
        """获取 session 当前实际使用的入口节点。
        优先级: AI switch override > 上次 inbound 实际用的节点 > session 记录 > 全局默认"""
        sid = (session_id or "").strip()
        with self._lock:
            # [Fork/Merge 2026-05-17] Why: callers may ask using a branch id seen
            # in task metadata. How: resolve entry branches to their parent before
            # reading override/last-entry maps. Purpose: active-node queries match
            # the session that will receive the next inbound.
            route_sid = self._route_session_id_for_session_locked(sid)
            override = self.session_entry_overrides.get(route_sid, "").strip()
            last_used = self.session_last_entry_node.get(route_sid, "").strip()
            info = self.sessions.get(route_sid)
            # Why: immediately after restart, getActiveNode has no last-used
            # memory. How: include the persisted SessionInfo.entry_node_id before
            # falling back to the global default. Purpose: UI callers see the
            # same session-level route that inbound task creation will use.
            recorded = (info.entry_node_id if info else "").strip() if info else ""
            default_node = self._default_entry_node()
            node_id = override or last_used or recorded or default_node
            return {
                "node_id": node_id,
                "is_override": bool(override),
                "default_node_id": default_node,
                "session_id": route_sid,
            }

    # ------------------------------------------------------------------ #
    #  Child Session 隔离（Phase A + B）
    # ------------------------------------------------------------------ #

    def session_children(self, *, session_id: str) -> list[dict[str, Any]]:
        """Return non-branch child sessions that belong to a parent session.

        [2026-06-03] Why: the web frontend cannot rebuild childNodes from
        ConversationStore history alone after a browser refresh. How: read the
        durable sessions.json child registry, walk active entry branches, and enrich
        each child row with the latest matching task status. Purpose: the frontend can
        restore child-session navigation without replaying old WebSocket events.
        """
        parent = str(session_id or "").strip()
        if not parent:
            return []

        def _task_sort_key(task: Task) -> datetime:
            return task.updated_at or task.created_at or _now()

        def _frontend_status(task_status: str) -> str:
            # [2026-06-03] Why: Supervisor task states are more granular than the web
            # child-node badge states. How: collapse pending/running/suspended-like
            # states into "running" and preserve terminal states. Purpose: the API
            # returns the same small status vocabulary the frontend already renders.
            if task_status in {"completed", "failed", "cancelled"}:
                return task_status
            if task_status == "awaiting_approval":
                return "awaiting_approval"
            return "running"

        with self._lock:
            registry = getattr(self._session_store, "_registry", {})
            child_ids: set[str] = set()
            branch_ids: set[str] = set()
            stack = [parent]
            visited: set[str] = set()

            while stack:
                current_parent = stack.pop()
                if current_parent in visited:
                    continue
                visited.add(current_parent)
                for child_sid in self.parent_children.get(current_parent, set()):
                    raw = registry.get(child_sid) if isinstance(registry, dict) else None
                    if not isinstance(raw, dict) or raw.get("reset"):
                        continue
                    if str(raw.get("node_id") or "") == "__entry_branch__" or child_sid.startswith("branch_"):
                        branch_ids.add(child_sid)
                        stack.append(child_sid)
                        continue
                    child_ids.add(child_sid)

            for (child_parent, _node_id, _ctx_key), child_sid in self.child_session_map.items():
                if child_parent == parent or child_parent in branch_ids:
                    raw = registry.get(child_sid) if isinstance(registry, dict) else None
                    if isinstance(raw, dict) and not raw.get("reset"):
                        child_ids.add(child_sid)

            rows: list[dict[str, Any]] = []
            for child_sid in sorted(child_ids):
                raw = registry.get(child_sid) if isinstance(registry, dict) else None
                if not isinstance(raw, dict) or raw.get("reset"):
                    continue
                node_id = str(raw.get("node_id") or "")
                context_key = str(raw.get("context_key") or "")
                if not node_id:
                    for (child_parent, mapped_node, mapped_key), mapped_sid in self.child_session_map.items():
                        if mapped_sid == child_sid:
                            node_id = mapped_node
                            context_key = context_key or mapped_key
                            break

                related_tasks = [
                    task for task in self.tasks.values()
                    if str(task.input.get("child_session_id") or "").strip() == child_sid
                    or task.session_id == child_sid
                ]
                latest_task = max(related_tasks, key=_task_sort_key) if related_tasks else None
                task_status = latest_task.status.value if latest_task is not None else "running"
                started_at = latest_task.created_at.isoformat() if latest_task and latest_task.created_at else str(raw.get("created_at") or "")
                updated_at = latest_task.updated_at.isoformat() if latest_task and latest_task.updated_at else str(raw.get("last_active_at") or raw.get("created_at") or "")
                terminal = task_status in {"completed", "failed", "cancelled"}

                rows.append({
                    "session_id": child_sid,
                    "parent_session_id": str(raw.get("parent_session_id") or ""),
                    "route_parent_session_id": parent,
                    "node_id": node_id or "unknown",
                    "context_key": context_key,
                    "context_mode": str(raw.get("context_mode") or ""),
                    "status": _frontend_status(task_status),
                    "task_id": latest_task.task_id if latest_task is not None else "",
                    "started_at": started_at,
                    "updated_at": updated_at,
                    "completed_at": updated_at if terminal else "",
                })

            rows.sort(key=lambda item: str(item.get("started_at") or ""))
            return rows

    def anchor_session_for_locked(self, session_id: str, *, max_depth: int = 8) -> str:
        """Resolve a possibly-child session id up to the anchor session that owns tasks.

        Child session 从不进 self.sessions，所以任何「给这个会话建一个 task」的调用都
        必须先换成它的 anchor，否则子节点的历史压根没法被压缩。
        """
        sid = str(session_id or "").strip()
        for _ in range(max_depth):
            if not sid or sid in self.sessions:
                return sid
            parent = ""
            for parent_sid, children in self.parent_children.items():
                if sid in children:
                    parent = parent_sid
                    break
            if not parent:
                return ""
            sid = parent
        return ""

    def get_or_create_child_session(
        self,
        parent_session_id: str,
        node_id: str,
        context_key: str,
        context_mode: str,
    ) -> tuple[str, bool]:
        """根据 context_mode 获取或创建 child session。

        Child Session 隔离核心方法。三种 context_mode：
        - accumulate：复用已有 child session（如存在且未过期），否则新建
        - fresh：强制创建新 child session，旧的不删除（等 TTL 过期清理）
        - fork：创建新 child session，调用方需额外做 ConversationStore.fork()

        Returns:
            (child_session_id, is_new) — is_new 表示是否新建了 session
        """
        map_key = (parent_session_id, node_id, context_key)

        if context_mode == "accumulate":
            existing_cid = self.child_session_map.get(map_key)
            if existing_cid and not self._is_child_session_expired(existing_cid):
                # 复用现有 child session，更新 last_active_at
                self._session_store.update_last_active(existing_cid)
                return existing_cid, False
            # 已过期或不存在：新建（过期的旧 session 留给 TTL 清理）
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        elif context_mode == "fresh":
            # 强制新建，旧映射更新为新 ID
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        elif context_mode == "fork":
            # fork 与 fresh 类似：总是新建
            return self._create_child_session(parent_session_id, node_id, context_key, context_mode), True

        else:
            # 未知模式，按 accumulate 处理
            logger.warning("unknown context_mode '%s', falling back to accumulate", context_mode)
            return self.get_or_create_child_session(
                parent_session_id, node_id, context_key, "accumulate",
            )

    def _create_child_session(
        self, parent_session_id: str, node_id: str, context_key: str, context_mode: str,
    ) -> str:
        """生成新 child session 并持久化映射。

        Child Session 隔离（Phase A）：生成 child_{uuid4().hex[:12]} 格式的 ID，
        写入 child_session_map、parent_children、sessions.json。
        """
        child_sid = f"child_{uuid.uuid4().hex[:12]}"
        map_key = (parent_session_id, node_id, context_key)

        # 更新内存映射
        self.child_session_map[map_key] = child_sid
        self.parent_children.setdefault(parent_session_id, set()).add(child_sid)

        # 持久化到 sessions.json
        self._session_store.on_child_session_created(
            child_session_id=child_sid,
            parent_session_id=parent_session_id,
            node_id=node_id,
            context_key=context_key,
        )
        # [AutoC 2026-05-30] Why: 24h TTL 清理需要知道 child session 的
        # context_mode，才能删除 fresh/fork 并永久保留 accumulate。How: child
        # 创建后立即补写 context_mode 到持久 registry 并落盘。Purpose: 重启后
        # stale cleanup 仍能按模式执行正确清理。
        self._session_store.set_entry_field(
            child_sid, "context_mode", str(context_mode or "").strip(),
        )

        logger.info(
            "child session created: %s (parent=%s, node=%s, key=%s)",
            child_sid, parent_session_id, node_id, context_key,
        )
        return child_sid

    def _is_child_session_expired(self, child_session_id: str) -> bool:
        """检查 child session 是否已超过 TTL。

        Child Session 隔离（Phase A）：从 sessions.json 的 last_active_at 字段
        判定是否过期。如果记录不存在或已 reset，视为过期。
        """
        from clonoth_runtime import get_int, load_runtime_config
        runtime_cfg = load_runtime_config(self.workspace_root)
        ttl_hours = get_int(runtime_cfg, "engine.child_session.ttl_hours", 24, min_value=1)

        entry = self._session_store.get_entry(child_session_id)
        if entry is None or entry.get("reset"):
            return True
        # [AutoC 2026-05-30] Why: accumulate child session 是长期上下文，老大要求
        # 永久保留，不能再按 24h TTL 失效。How: 在 TTL 判断前直接保留明确标记
        # 为 accumulate 的记录。Purpose: get_or_create_child_session 不会因时间流逝
        # 创建新的 accumulate 会话并留下旧记录。
        if str(entry.get("context_mode") or "").strip() == "accumulate":
            return False
        last_active_str = entry.get("last_active_at", "")
        if not last_active_str:
            return True
        try:
            last_active = datetime.fromisoformat(last_active_str)
            age_hours = (_now() - last_active).total_seconds() / 3600
            return age_hours > ttl_hours
        except (ValueError, TypeError):
            return True

    def _expire_child_session(self, child_session_id: str) -> None:
        """删除一个过期的 child session。

        Child Session 隔离（Phase A）：删除 JSONL 文件、从映射表移除、物理删除 registry 条目。
        """
        # 1. 删除运行期文件（对话历史 + 任务转写）
        self.purge_session_files(child_session_id)

        # 2. 从映射表移除
        key_to_remove = None
        for key, cid in self.child_session_map.items():
            if cid == child_session_id:
                key_to_remove = key
                break
        if key_to_remove:
            del self.child_session_map[key_to_remove]
            parent_sid = key_to_remove[0]
            if parent_sid in self.parent_children:
                self.parent_children[parent_sid].discard(child_session_id)

        # [AutoC 2026-05-30] Why: fresh/fork child session 过期后应从
        # sessions.json 物理删除。How: 删除 registry 条目而不是写 reset=true。
        # Purpose: 让完成或过期的临时 child session 不再堆积。
        self.sessions.pop(child_session_id, None)
        self.session_generations.pop(child_session_id, None)
        self._cancelled_sessions.discard(child_session_id)
        self._session_context_usage.pop(child_session_id, None)
        self._session_store.remove_session(child_session_id)

        logger.info("child session expired: %s", child_session_id)

    def _cleanup_stale_sessions_locked(self) -> None:
        """清理 sessions.json 中已无用的 branch 和 child session 记录。

        [AutoC 2026-05-30] Why: 历史上 branch/child session 只标记 reset 不删除，
        导致 sessions.json 无限膨胀。
        How: 遍历 registry，删除所有 reset=true 的条目，以及超过 24h 无活动的
        非 accumulate child session。
        Purpose: 定期清理积累的历史记录。
        """
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        stale_threshold = now - timedelta(hours=24)
        to_remove: list[str] = []

        for sid, info in self._session_store.items_snapshot():
            if not isinstance(info, dict):
                continue
            # [AutoC 2026-05-30] Why: reset=true 条目已经不会参与活跃会话恢复。
            # How: 后台 sweep 统一物理删除历史 reset 行。Purpose: 修复旧版本遗留
            # reset 标记长期占用 sessions.json 的问题。
            if info.get("reset"):
                to_remove.append(sid)
                continue
            # [AutoC 2026-05-30] Why: branch session 是入口执行副本，不应长期
            # 持久化。How: 只要没有非终态 task 仍运行在该 branch，就删除 registry。
            # Purpose: 清理历史孤儿 branch，同时避免误删仍在执行的 branch。
            if sid.startswith("branch_"):
                if not any(t.session_id == sid and not self._task_terminal(t) for t in self.tasks.values()):
                    to_remove.append(sid)
                continue
            # [AutoC 2026-05-30] Why: fresh/fork child session 只用于一次性或
            # 隔离上下文，24h 无活动后应释放；accumulate 是长期上下文，必须保留。
            # How: 读取 context_mode 和 last_active_at，超过阈值才加入删除列表。
            # Purpose: 同时满足 TTL 清理和 accumulate 永久保留规则。
            if info.get("is_child"):
                ctx_mode = str(info.get("context_mode") or "").strip()
                # [AutoC 2026-05-30] 条件从 in ("fresh", "fork") 改为 != "accumulate"，
                # 因为历史系统节点（turn_summarizer/compactor）创建的 child session
                # 没有 context_mode 字段（空字符串），旧条件无法匹配，导致堆积。
                if ctx_mode == "accumulate":
                    continue
                # [AutoC 2026-05-30] Why: 没有 context_mode 的历史 child session
                # 是旧代码遗留，运行时清理对它们无效（旧 task 已消失）。
                # How: 无 context_mode 的直接清理，不等 24h TTL。
                # Purpose: 一次性清除历史堆积。
                if not ctx_mode:
                    to_remove.append(sid)
                    continue
                # fresh/fork: 正常 TTL 检查
                updated_str = str(
                    info.get("last_active_at")
                    or info.get("updated_at")
                    or info.get("created_at")
                    or ""
                )
                try:
                    updated_at = datetime.fromisoformat(updated_str)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    if updated_at < stale_threshold:
                        to_remove.append(sid)
                except Exception:
                    pass

        if to_remove:
            self._session_store.remove_sessions(set(to_remove), flush=False)
            for sid in to_remove:
                self.sessions.pop(sid, None)
                self.session_generations.pop(sid, None)
                self._cancelled_sessions.discard(sid)
                self._session_context_usage.pop(sid, None)
                self._remove_child_mapping_for_session_locked(sid)
                self.entry_branch_parents.pop(sid, None)
                for branches in self.parent_entry_branches.values():
                    branches.discard(sid)
                for children in self.parent_children.values():
                    children.discard(sid)
            self.parent_entry_branches = {pid: branches for pid, branches in self.parent_entry_branches.items() if branches}
            self.parent_children = {pid: children for pid, children in self.parent_children.items() if children}
            self._session_store._flush()
            logger.info("stale session cleanup: removed %d sessions from registry", len(to_remove))

    # ------------------------------------------------------------------ #
    #  审批
    # ------------------------------------------------------------------ #

    def _apply_approval_requested(self, payload: dict[str, Any]) -> None:
        try:
            a = Approval.model_validate(payload)
        except Exception:
            return
        self.approvals[a.approval_id] = a

    def _apply_approval_decided(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("approval_id")
        if approval_id in self.approvals:
            a = self.approvals[approval_id]
            decision = payload.get("decision")
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = payload.get("comment")
            a.decided_at = _now()

    def create_approval(
        self,
        *,
        session_id: str,
        operation: str,
        details: dict[str, Any],
        tool_call_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
    ) -> Approval:
        now = _now()
        # [AutoC 2026-05-31] Why: duplicate pending approval suppression must not
        # collapse two simultaneous calls to the same operation and path. How: add
        # optional execution identity to the fingerprint source. Purpose: each
        # ToolCallCard can own its own approval while legacy callers still dedupe by
        # operation/details when no identity is available.
        fingerprint_src = json.dumps({
            "operation": operation,
            "details": details,
            "tool_call_id": tool_call_id or "",
            "node_id": node_id or "",
            "task_id": task_id or "",
        }, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:12]

        approval = Approval(
            approval_id=str(uuid.uuid4()),
            session_id=session_id,
            operation=operation,
            details=details,
            fingerprint=fingerprint,
            status=ApprovalStatus.pending,
            decision=None,
            comment=None,
            requested_at=now,
            decided_at=None,
            tool_call_id=tool_call_id,
            node_id=node_id,
            task_id=task_id,
        )

        with self._lock:
            # 同指纹的 pending 审批不再重复创建
            for a in self.approvals.values():
                if a.fingerprint == fingerprint and a.status == ApprovalStatus.pending:
                    return a

            self.approvals[approval.approval_id] = approval
            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="approval_requested",
                payload=approval.model_dump(mode="json"),
            )
        return approval

    def decide_approval(self, *, approval_id: str, decision: str, comment: str | None = None) -> Approval | None:
        with self._lock:
            a = self.approvals.get(approval_id)
            if a is None or a.status != ApprovalStatus.pending:
                return a
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = comment
            a.decided_at = _now()
            self.eventlog.append(
                session_id=a.session_id,
                component="supervisor",
                type_="approval_decided",
                payload={
                    "approval_id": a.approval_id,
                    "decision": decision,
                    "comment": comment,
                    "ts": a.decided_at.isoformat(),
                    # [AutoC 2026-05-31] Why: approval_decided must find the same
                    # ToolExecution that approval_requested updated. How: mirror
                    # the stored execution identity into the decision event. Purpose:
                    # the frontend does not need a separate lookup endpoint.
                    "tool_call_id": a.tool_call_id,
                    "node_id": a.node_id,
                    "task_id": a.task_id,
                },
            )
            return a

    def _authorizing_conversation_key(self, session_id: str, task_id: str | None) -> str:
        """Conversation key whose permissions govern this operation.

        dispatch 出去的子任务跑在自己的 `agent:...` 会话里，按运行期 session 判权就等于：
        任何群成员只要能让 bot 派一个子节点，「QQ 非管理员禁止写文件 / 禁止敏感命令」
        那几道硬拒就整段跳过。发起会话的 key 由 dispatch 一路透传，嵌套派发也保留最初那个。
        """
        if task_id:
            task = self.tasks.get(task_id)
            if task is not None:
                raw_ctx = task.input.get("task_context")
                ctx = raw_ctx if isinstance(raw_ctx, dict) else {}
                routed = str(
                    ctx.get("route_conversation_key") or ctx.get("parent_conversation_key") or ""
                ).strip()
                if routed:
                    return routed
                session = self.sessions.get(task.session_id or session_id)
                if session is not None:
                    return str(getattr(session, "conversation_key", "") or "")
        session = self.sessions.get(session_id)
        return str(getattr(session, "conversation_key", "") or "") if session else ""

    def request_operation(
        self,
        *,
        session_id: str,
        op: str,
        parameters: dict[str, Any],
        tool_call_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
    ) -> OpRequestOut:
        conv_key = self._authorizing_conversation_key(session_id, task_id)
        platform_auth = dict(parameters.get("platform_auth") or {}) if isinstance(parameters.get("platform_auth"), dict) else {}
        # 只认 conv_key 前缀的话，一旦某条路径把 key 换成 scheduler:/agent: 之类，
        # 底下那一整段 QQ 硬规则就静默失效；带着 QQ 身份跑的任务照样得吃这些限制。
        is_qq = conv_key.startswith("qq_") or str(platform_auth.get("platform") or "").strip().lower() in _QQ_PLATFORMS
        is_admin = bool(platform_auth.get("is_admin"))

        # QQ 普通用户硬限制：禁止敏感工具与提示词读取。
        if is_qq and not is_admin:
            if op == "execute_command":
                # 例外：允许非管理员群友用 curl 纯 GET 读取公网网页。
                # 方法：由 policy.is_safe_public_curl 严格判定（不落盘、无注入、
                # 非写方法、禁 localhost/内网）。群友无审批入口，故安全情况下直接 auto放行；
                # 其余命令（下载、装包、危险命令等）仍然拒绝。
                command = str(parameters.get("command") or "")
                if self.policy.is_safe_public_curl(command):
                    return OpRequestOut(
                        safety_level=SafetyLevel.auto,
                        reason="auto-allowed: qq non-admin safe public curl read",
                        approval_id=None,
                    )
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason="qq non-admin users cannot execute sensitive operations",
                    approval_id=None,
                )
            if op == "restart":
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason="qq non-admin users cannot execute sensitive operations",
                    approval_id=None,
                )
            if op == "write_file":
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason="qq non-admin users cannot modify files",
                    approval_id=None,
                )
            # 读取一律拒绝，不再逐条列敏感前缀：源码里有鉴权逻辑本身、工具脚本里有
            # 服务器地址、config 里有管理员 QQ 号，黑名单补不全，漏一条就是全泄。
            # 群友没有读工作区文件的正当用途 —— 他们发的图和文件走附件通道，不经这里。
            # list_dir 与 search_in_files 也报 read_file，一并挡住。
            if op == "read_file":
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason="qq non-admin users cannot read workspace files",
                    approval_id=None,
                )

        decision = self.policy.evaluate(op=op, parameters=parameters)
        if decision.safety_level == SafetyLevel.auto:
            return OpRequestOut(safety_level=SafetyLevel.auto, reason=decision.reason, approval_id=None)
        # [AutoC 2026-06-04] Why: hard policy denials must not be converted into
        # human approval requests. How: return SafetyLevel.deny immediately before
        # any scheduler or approval handling. Purpose: destructive or invalid
        # operations stay blocked even when they come from unattended jobs.
        if decision.safety_level == SafetyLevel.deny:
            return OpRequestOut(safety_level=SafetyLevel.deny, reason=decision.reason, approval_id=None)

        # [AutoC 2026-06-04] Why: scheduler-triggered tasks have no source inbound
        # sequence, so their approval events cannot be routed to an adapter and would
        # leave the task suspended forever. How: when the active task belongs to a
        # session whose conversation_key starts with "scheduler:", convert the
        # approval-required decision into an automatic allow. Purpose: scheduled
        # maintenance tasks can continue while normal user tasks still use approval.
        if conv_key.startswith("scheduler:"):
            # 定时任务没有 source_inbound_seq，审批投递不到人，所以只能自动决定。
            # 但不能放行一切 op：敏感决策（写 config/** 与源码、下载落盘、装包、chmod +x）
            # 正是从"能建一个 schedule"走到"任意代码执行"的那几步。这类直接 deny 而不是
            # 转审批 —— 转审批只会让任务挂到硬上限被回收，排障方向还会被带偏。
            if getattr(decision, "sensitive", False):
                self.eventlog.append(
                    session_id=session_id,
                    component="supervisor",
                    type_="scheduler_sensitive_op_denied",
                    payload={"op": op, "reason": decision.reason, "task_id": task_id or ""},
                )
                return OpRequestOut(
                    safety_level=SafetyLevel.deny,
                    reason=f"scheduler tasks cannot run sensitive operations unattended ({decision.reason})",
                    approval_id=None,
                )
            return OpRequestOut(
                safety_level=SafetyLevel.auto,
                reason="auto-approved: scheduler task",
                approval_id=None,
            )

        #  Why: QQ 管理员亲自触发的任务，对普通命令/文件操作不再逐条审批，
        # 避免管理员反复确认。How: is_admin 且本次决策不是敏感规则(sensitive=False)
        # 时，将 approval_required 直接转为 auto。Purpose: 改源码、重启、动
        # config/nodes、data、工作区外部路径等敏感操作仍需人工确认，deny 硬拦截不变。
        if is_qq and is_admin and not getattr(decision, "sensitive", False):
            return OpRequestOut(
                safety_level=SafetyLevel.auto,
                reason=f"auto-approved: qq admin task ({decision.reason})",
                approval_id=None,
            )

        # [AutoC 2026-05-31] Why: request_guard can now identify the active tool
        # call. How: forward that identity to create_approval. Purpose: the emitted
        # approval_requested event can be merged into the same ToolCallCard.
        approval = self.create_approval(
            session_id=session_id,
            operation=op,
            details=parameters,
            tool_call_id=tool_call_id,
            node_id=node_id,
            task_id=task_id,
        )
        return OpRequestOut(safety_level=SafetyLevel.approval_required, reason=decision.reason, approval_id=approval.approval_id)

    # ------------------------------------------------------------------ #
    #  Admin / 杂项
    # ------------------------------------------------------------------ #

    def list_events(self, *, session_id: str, after_seq: int) -> list[dict[str, Any]]:
        return self.eventlog.list_events(session_id=session_id, after_seq=after_seq)

    def admin_state(self) -> AdminStateOut:
        approval_counts = {s.value: 0 for s in ApprovalStatus}
        pending: list[Approval] = []
        for a in self.approvals.values():
            approval_counts[a.status.value] = approval_counts.get(a.status.value, 0) + 1
            if a.status == ApprovalStatus.pending:
                pending.append(a)

        task_counts = {s.value: 0 for s in TaskStatus}
        for t in self.tasks.values():
            task_counts[t.status.value] = task_counts.get(t.status.value, 0) + 1

        engine_last_seen_at, engine_worker_id = self.engine_seen_snapshot()

        return AdminStateOut(
            sessions=len(self.sessions),
            approvals=approval_counts,
            tasks=task_counts,
            pending_approvals=pending,
            engine_runtime={
                "worker_id": engine_worker_id,
                "last_seen_at": engine_last_seen_at,
                "workers": sorted({t.worker_id for t in self.tasks.values() if t.worker_id}),
            },
        )

    # ------------------------------------------------------------------ #
    #  Direction 2: Engine Generation ID
    # ------------------------------------------------------------------ #

    def register_engine(self, worker_id: str, generation_id: str) -> dict[str, Any]:
        """Register an engine worker with its generation ID.

        Cancels any running/pending tasks from the same worker_id with a
        different generation (orphans from a previous engine instance).
        """
        wid = (worker_id or "").strip()
        gid = (generation_id or "").strip()
        if not wid or not gid:
            return {"ok": False, "error": "worker_id and generation_id required"}

        with self._lock:
            old_gen = self._engine_generations.get(wid)
            self._engine_generations[wid] = gid

            orphan_count = 0
            if old_gen and old_gen != gid:
                # Same worker restarted with new generation — cancel its old tasks
                orphan_count = self._cancel_worker_orphans_locked(wid)
            elif not old_gen:
                # First registration of this worker — also clean up any tasks
                # from a previous run where this worker_id was used
                orphan_count = self._cancel_worker_orphans_locked(wid)

            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="engine",
                type_="engine_registered",
                payload={
                    "worker_id": wid,
                    "generation_id": gid,
                    "previous_generation_id": old_gen or "",
                    "orphans_cancelled": orphan_count,
                    "ts": _now().isoformat(),
                },
            )

            # ---- Deferred restart notification ----
            # Injected here (after orphan cleanup) so the new task gets the
            # current generation and won't be reaped as an orphan.
            if self._pending_restart_notify:
                _notify_sid = self._pending_restart_notify
                self._pending_restart_notify = None  # consume once
                _si = self.sessions.get(_notify_sid)
                if _si:
                    _restart_evt = self.eventlog.append(
                        session_id=_notify_sid,
                        component="supervisor",
                        type_="inbound_message",
                        payload={
                            "channel": _si.channel,
                            "conversation_key": _si.conversation_key,
                            "text": "[系统通知] Engine 重启已完成，新代码已生效。",
                        },
                    )
                    self.record_inbound_message_event(_restart_evt)

            return {"ok": True, "orphans_cancelled": orphan_count, "generation_id": gid}

    def _cancel_worker_orphans_locked(self, worker_id: str) -> int:
        """Cancel all non-terminal tasks assigned to a specific worker_id."""
        now = _now()
        count = 0
        for task in self.tasks.values():
            if self._task_terminal(task):
                continue
            if task.worker_id != worker_id:
                continue
            task.cancel_requested = True
            task.status = TaskStatus.cancelled
            task.updated_at = now
            task.lease_expires_at = None
            if task.waiting_for_task_id:
                task.waiting_for_task_id = None
            task.result = {"action": "cancelled", "error": f"engine worker {worker_id} restarted (generation mismatch)"}
            self._event_task_snapshot("task_cancelled", task)
            count += 1
        return count

    def write_boot_event(self) -> dict[str, Any]:
        prev = self.eventlog.last_boot_run_id()
        payload = {
            "previous_run_id": prev,
            "restarted": prev is not None,
            "ts": _now().isoformat(),
        }
        return self.eventlog.append(session_id=SYSTEM_SESSION_ID, component="supervisor", type_="boot", payload=payload)
