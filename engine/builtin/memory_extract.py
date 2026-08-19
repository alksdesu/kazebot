"""Built-in supervisor hook handler for automatic memory extraction."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config


log = logging.getLogger(__name__)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "MemoryExtractHandler",
    "hook_points": [
        ("on_entry_task_complete", "on_task_complete"),
        ("on_inbound_message", "on_inbound"),
    ],
    "priority": 100,
    # Why: memory_extract writes new entries that knowledge_inject must cache-invalidate.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing rather than silent runtime errors.
    "requires": ["knowledge_inject"],
}


class MemoryExtractHandler:
    """Handle idle automatic memory extraction through injected callbacks.

    Why: this handler now lives under engine.builtin and therefore cannot import
    supervisor internals. How: every supervisor operation is read from the hook
    context as a callback or plain value. Purpose: keep memory extraction behavior
    while removing the old supervisor -> handler -> supervisor dependency loop.
    """

    name = "memory_extract"
    # Delayed extraction is replay-safe because its intent and created-task marker
    # are durable; the effect is completed only after task creation is durable.
    post_work_idempotent = True

    def __init__(self) -> None:
        # [2026-05-23] Why: compacted conversations can shrink message counts,
        # while entry-task completions remain monotonic within this handler. How:
        # count every qualifying entry-task finish per session. Purpose: make the
        # auto-extract increment gate use task granularity instead of message offsets.
        self._memory_extract_task_counts: dict[str, int] = {}
        # [2026-05-23] Why: idle timers can be cancelled before extraction, but
        # those passed entry tasks should still count toward a later threshold.
        # How: keep a separate committed cursor that only moves when the timer
        # fires. Purpose: compute task increments correctly across cancelled idle windows.
        self._memory_extract_last_extracted_task_counts: dict[str, int] = {}
        # [AutoC 2026-05-24] Why: compact summaries can occupy many durable
        # message indexes, so a message cursor can select the wrong transcript
        # slice. How: keep the exact unextracted entry task ids per session and
        # later match ConversationStore.source_task_id. Purpose: extract only the
        # messages that belong to completed entry tasks that have not committed.
        self._memory_extract_pending_task_ids: dict[str, list[str]] = {}
        # Why: automatic extraction waits for user idleness. How: keep one daemon
        # Timer per session and replace it on later qualifying finishes. Purpose:
        # avoid extracting memory while the user is still sending follow-ups.
        self._memory_extract_timers: dict[str, threading.Timer] = {}
        # Why: pending task ids must commit only if the idle timer fires. How:
        # keep the prepared transcript until the timer callback commits it.
        # Purpose: cancelled idle windows can be retried by the next entry-node
        # finish without losing the associated source_task_id values.
        self._memory_extract_pending: dict[str, dict[str, Any]] = {}

    def on_task_complete(self, ctx: dict[str, Any]) -> None:
        """Schedule memory extraction once for one durable post-work phase."""
        task = ctx.get("task")
        if task is None:
            return
        self._maybe_trigger_memory_extract_locked(ctx=ctx, task=task)

    def on_inbound(self, ctx: dict[str, Any]) -> None:
        """Cancel pending idle extraction when a new inbound message arrives."""
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            return
        cancel_persisted = ctx.get("cancel_memory_extract_intents")
        if callable(cancel_persisted):
            cancel_persisted(session_id)
        lock = ctx.get("acquire_lock")
        if lock is None:
            self._cancel_memory_extract_idle_locked(session_id)
            return
        # Why: inbound cancellation touches the same handler dictionaries as the
        # timer callback. How: use the injected re-entrant supervisor lock when it
        # is available. Purpose: keep cancellation serialized without importing
        # SupervisorState or reaching into private attributes.
        with lock:
            self._cancel_memory_extract_idle_locked(session_id)

    def _maybe_trigger_memory_extract_locked(self, *, ctx: dict[str, Any], task: Any) -> None:
        """Entry-node finish gate for automatic memory extraction."""
        # Why: only successful entry-node finishes represent complete conversation
        # turns. How: keep the old action/kind/system-task gates using only task
        # values. Purpose: prevent child tasks or internal system tasks from
        # recursively triggering extraction.
        act = str((getattr(task, "result", None) or {}).get("action") or "").strip()
        if act != "finish" or not _task_kind_is_node(task):
            log.debug("memory_extract gate: blocked by action=%s kind=%s", act, getattr(task, "kind", "?"))
            return
        task_input = getattr(task, "input", {}) or {}
        if task_input.get("_system_task"):
            log.debug("memory_extract gate: blocked by _system_task")
            return
        # 用户轮次按 task 来源判定，不比对入口节点 id：入口是"平台 × 会话 × 消息能力"
        # 三维决定的（qq.orchestrator / qq.vision / draw.* / 前端任选节点），
        # 单个全局标量不可能同时等于它们。
        if getattr(task, "source_inbound_seq", None) is None:
            log.debug("memory_extract gate: task not created from an inbound turn")
            return
        # [2026-06-04] Why: fresh child tasks are short-lived and disposable (session
        # cleaned within 24h), so extracting memory from them wastes CPU. However,
        # accumulate/persistent child nodes (like Smith) maintain long-lived sessions
        # with valuable context worth extracting. How: check dispatch_context_mode
        # from task input — only skip fresh children. Purpose: enable memory
        # extraction for persistent sub-agents while still filtering throwaway tasks.
        caller_tid = getattr(task, "caller_task_id", None)
        if caller_tid:
            ctx_mode = str(task_input.get("dispatch_context_mode") or "").strip()
            if ctx_mode == "fresh" or not ctx_mode:
                log.debug("memory_extract gate: blocked by caller_task_id=%s (fresh/unknown child)", str(caller_tid)[:8])
                return
            log.debug("memory_extract gate: allowing %s child (context_mode=%s)", str(caller_tid)[:8], ctx_mode)
        # [2026-05-23 fix] Entry tasks run on temporary branch sessions (task.session_id
        # starts with 'branch_'), but they represent real user conversation turns. The
        # on_entry_task_complete hook passes the merged parent session in ctx["session_id"].
        # Only filter by task_id prefix (which is never 'branch_' for entry tasks);
        # do NOT filter by task.session_id — that would block all entry task extractions.
        task_id_str = str(getattr(task, "task_id", "") or "")
        if task_id_str.startswith("branch_"):
            log.debug("memory_extract gate: blocked by branch task_id=%s", task_id_str[:16])
            return
        log.debug("memory_extract: entered gate check for task %s node=%s", task_id_str[:8], getattr(task, 'node_id', '?'))

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            log.debug("memory_extract gate: no workspace_root")
            return
        workspace_root = Path(workspace_root)
        runtime_cfg = load_runtime_config(workspace_root)
        if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
            log.debug("memory_extract gate: auto_extract disabled")
            return
        log.debug(
            "memory_extract: passed gates for task %s runtime_session=%s route_session=%s",
            str(getattr(task, "task_id", "?") or "?")[:8],
            getattr(task, "session_id", "?"),
            ctx.get("session_id"),
        )

        # Why: a task that explicitly saved memory has already handled the turn.
        # How: preserve the old save_memory tool-name mutual exclusion. Purpose:
        # avoid duplicate memory entries from automatic extraction.
        tool_names = (getattr(task, "result", None) or {}).get("_tool_names") or []
        if "save_memory" in tool_names:
            log.debug("memory_extract TRACE: BLOCKED by save_memory in tool_names")
            return

        # [Fork/Merge 2026-05-17] Why: completed entry tasks run on temporary
        # branch sessions, while on_entry_task_complete passes the merged parent
        # session in ctx["session_id"]. How: prefer the hook route session and
        # fall back to the task runtime session for legacy/non-branch tasks.
        # Purpose: pending task ids and transcript extraction read durable parent
        # history rather than the temporary branch name after merge.
        session_id = str(ctx.get("session_id") or getattr(task, "session_id", "") or "").strip()
        if not session_id:
            log.debug("memory_extract: skip, empty session_id")
            return
        log.debug("memory_extract TRACE: session_id=%s (ctx=%s task=%s)", session_id, ctx.get('session_id'), getattr(task, 'session_id', '?'))
        # [2026-05-23] Why: entry-task completions remain monotonic within this
        # handler and are still useful for diagnostics. How: keep the historical
        # total task counter unchanged. Purpose: preserve observability while the
        # trigger threshold below uses the unextracted pending-task-id list.
        task_count = self._memory_extract_task_counts.get(session_id, 0) + 1
        self._memory_extract_task_counts[session_id] = task_count
        # [AutoC 2026-05-24] Why: message indexes are polluted by compact summary
        # records and no longer identify new dialogue. How: append the exact entry
        # task id that passed every gate. Purpose: later build the transcript by
        # ConversationStore.source_task_id membership.
        pending_task_ids = self._memory_extract_pending_task_ids.setdefault(session_id, [])
        pending_task_ids.append(task_id_str)
        pending_task_count = len(pending_task_ids)

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 3, min_value=1, max_value=100)
        last_extracted_task_count = self._memory_extract_last_extracted_task_counts.get(session_id, 0)
        log.debug(
            "memory_extract TRACE: task_count=%d last_extracted_task_count=%d pending_task_count=%d min_incr=%d",
            task_count,
            last_extracted_task_count,
            pending_task_count,
            min_increment,
        )
        if pending_task_count < min_increment:
            log.debug(
                "memory_extract: pending_task_count=%d < min_increment=%d (task_count=%d, last_extracted_task_count=%d)",
                pending_task_count,
                min_increment,
                task_count,
                last_extracted_task_count,
            )
            # [2026-05-23 / AutoC 2026-05-24] Why: low-volume sessions may not
            # reach the task threshold quickly. How: keep the fallback idle path,
            # but prepare its transcript from pending source_task_id values instead
            # of a message cursor. Purpose: preserve low-volume extraction without
            # reintroducing msg-index slicing.
            fallback_delay = get_int(
                runtime_cfg,
                "memory.auto_extract.idle_fallback_delay_sec",
                120,
                min_value=5,
                max_value=3600,
            )
            log.debug(
                "memory_extract TRACE: scheduling FALLBACK timer (%ds) for session %s task_count=%d pending_task_count=%d",
                fallback_delay,
                session_id,
                task_count,
                pending_task_count,
            )
            pending_extract = self._prepare_memory_extract_pending_locked(
                ctx=ctx,
                task=task,
                workspace_root=workspace_root,
                runtime_cfg=runtime_cfg,
                session_id=session_id,
                task_count=task_count,
            )
            if pending_extract is not None:
                self._schedule_memory_extract_idle_locked(
                    ctx=ctx,
                    session_id=session_id,
                    pending_extract=pending_extract,
                    delay_sec=fallback_delay,
                    timer_label="fallback idle",
                )
            return
        pending_extract = self._prepare_memory_extract_pending_locked(
            ctx=ctx,
            task=task,
            workspace_root=workspace_root,
            runtime_cfg=runtime_cfg,
            session_id=session_id,
            task_count=task_count,
        )
        if pending_extract is None:
            return

        # [2026-04-26] P4b pre-injection stays removed: large memory lists polluted
        # the main session history and could exceed context budgets. The extractor
        # node prompt already contains duplicate-prevention instructions.
        idle_delay = get_int(runtime_cfg, "memory.auto_extract.idle_delay_sec", 30, min_value=5, max_value=120)
        log.debug(
            "memory_extract TRACE: scheduling NORMAL timer (%ds) for session %s task_count=%d pending_task_count=%d",
            idle_delay,
            session_id,
            task_count,
            pending_task_count,
        )
        self._schedule_memory_extract_idle_locked(
            ctx=ctx,
            session_id=session_id,
            pending_extract=pending_extract,
            delay_sec=idle_delay,
            timer_label="idle",
        )

    def _prepare_memory_extract_pending_locked(
        self,
        *,
        ctx: dict[str, Any],
        task: Any,
        workspace_root: Path,
        runtime_cfg: dict[str, Any],
        session_id: str,
        task_count: int,
    ) -> dict[str, Any] | None:
        """Build the pending extraction payload shared by normal and fallback timers."""
        # [AutoC 2026-05-24] Why: msg-index ranges can include compact summaries
        # and miss the real new turn. How: copy the current uncommitted task-id
        # list and read ConversationStore directly. Purpose: prepare a transcript
        # from messages whose source_task_id exactly belongs to pending entry tasks.
        pending_task_ids = list(self._memory_extract_pending_task_ids.get(session_id, []))
        if not pending_task_ids:
            return None
        try:
            from engine.conversation_store import ConversationStore

            store = ConversationStore(workspace_root / "data" / "conversations")
            all_msgs = store.load(session_id)
        except Exception as exc:
            log.debug("memory_extract: failed to load ConversationStore for session %s: %s", session_id, exc)
            return None

        # [AutoC 2026-05-24] Why: the pending list is ordered for payload
        # observability, but membership checks should be exact and efficient. How:
        # convert it to a set only for filtering while preserving store order in
        # the resulting messages. Purpose: the transcript stays chronologically
        # readable and includes every message from each pending task.
        pending_task_id_set = set(pending_task_ids)
        task_msgs = [m for m in all_msgs if m.source_task_id in pending_task_id_set]
        transcript = _format_transcript_for_extract(task_msgs)
        if not transcript.strip():
            return None

        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        task_input = getattr(task, "input", {}) or {}
        task_context = task_input.get("task_context") if isinstance(task_input.get("task_context"), dict) else {}
        conversation_key = str(task_context.get("conversation_key") or "").strip()
        return {
            "session_id": session_id,
            "source_task_id": str(getattr(task, "task_id", "") or ""),
            "session_generation": int(getattr(task, "session_generation", 0) or 0),
            "transcript": transcript,
            "task_count": task_count,
            "pending_task_ids": pending_task_ids,
            "extractor_node": extractor_node,
            "kind": getattr(task, "kind", "node"),
            # [2026-06-17] 让自动提取出的长期 memory 写入当前 conversation_key
            # 对应的专属 namespace，避免私聊/群聊/不同群之间串记忆。
            "conversation_key": conversation_key,
        }

    def _schedule_memory_extract_idle_locked(
        self,
        *,
        ctx: dict[str, Any],
        session_id: str,
        pending_extract: dict[str, Any],
        delay_sec: int,
        timer_label: str,
    ) -> None:
        """Persist a delayed extraction intent; use a local Timer only for legacy contexts."""
        phase_id = str(ctx.get("post_phase_id") or "").strip()
        effect_id = f"{phase_id}:memory_extract_schedule" if phase_id else f"memory_extract:{session_id}"
        intent = dict(pending_extract)
        intent.update({
            "intent_id": effect_id,
            "effect_id": effect_id,
            "phase_id": phase_id,
            "post_work_id": str(ctx.get("post_work_id") or ""),
            "due_at": (datetime.now(timezone.utc) + timedelta(seconds=delay_sec)).isoformat(),
            "timer_label": timer_label,
        })
        persist_intent = ctx.get("persist_memory_extract_intent")
        if callable(persist_intent):
            # Persist first: a crash after the effect-start ledger write can no
            # longer strand a started effect without the delayed work payload.
            persisted = persist_intent(intent)
            if persisted is False:
                log.info("memory_extract: completed durable intent suppressed effect=%s", effect_id)
                return
            claim_effect = ctx.get("claim_phase_effect")
            if effect_id and callable(claim_effect):
                claim_effect(effect_id)
            self._memory_extract_pending[session_id] = intent
            log.debug(
                "memory_extract TRACE: durable intent scheduled [%s] session=%s due_at=%s",
                timer_label, session_id, intent["due_at"],
            )
            return

        old_timer = self._memory_extract_timers.pop(session_id, None)
        if old_timer is not None:
            old_timer.cancel()
        self._memory_extract_pending[session_id] = intent
        timer = threading.Timer(delay_sec, self._fire_memory_extract_idle, args=[dict(ctx), session_id])
        timer.daemon = True
        self._memory_extract_timers[session_id] = timer
        timer.start()

    def _cancel_memory_extract_idle_locked(self, session_id: str) -> None:
        """Cancel a pending idle memory extraction for one session."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        timer = self._memory_extract_timers.pop(sid, None)
        if timer is not None:
            timer.cancel()
        pending = self._memory_extract_pending.pop(sid, None)
        if timer is not None or pending is not None:
            log.debug("memory_extract TRACE: INBOUND CANCEL timer for session %s (had_timer=%s had_pending=%s)", sid, timer is not None, pending is not None)

    def restore_persisted_intent(self, intent: dict[str, Any]) -> None:
        """Rebuild handler counters needed if a recovered intent is later cancelled."""
        sid = str(intent.get("session_id") or "").strip()
        if not sid:
            return
        task_count = _safe_int(intent.get("task_count"), 0)
        pending_ids = [str(item) for item in (intent.get("pending_task_ids") or []) if str(item)]
        self._memory_extract_task_counts[sid] = max(
            task_count, self._memory_extract_task_counts.get(sid, 0),
        )
        self._memory_extract_pending_task_ids[sid] = pending_ids
        self._memory_extract_pending[sid] = dict(intent)

    def _fire_memory_extract_idle(self, ctx: dict[str, Any], session_id: str) -> None:
        """Legacy local-Timer path; durable Supervisor timers call the same executor."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        pending_extract = self._memory_extract_pending.get(sid)
        self._memory_extract_timers.pop(sid, None)
        if isinstance(pending_extract, dict):
            self.fire_persisted_intent(ctx, pending_extract)

    def fire_persisted_intent(self, ctx: dict[str, Any], pending_extract: dict[str, Any]) -> None:
        """Create the real extractor task once, then complete the durable intent/effect."""
        sid = str(pending_extract.get("session_id") or "").strip()
        intent_id = str(pending_extract.get("intent_id") or pending_extract.get("effect_id") or "").strip()
        transcript = str(pending_extract.get("transcript") or "")
        if not sid or not intent_id or not transcript.strip():
            raise ValueError("invalid persisted memory extraction intent")

        task_exists = ctx.get("memory_extract_task_exists")
        already_created = bool(callable(task_exists) and task_exists(intent_id))
        task_count = _safe_int(
            pending_extract.get("task_count"), self._memory_extract_task_counts.get(sid, 0),
        )
        session_generation = _safe_int(pending_extract.get("session_generation"), 0)
        if session_generation <= 0:
            current_generation = ctx.get("current_session_generation")
            session_generation = int(current_generation(sid) or 1) if callable(current_generation) else 1
        extractor_node = str(pending_extract.get("extractor_node") or "system.memory_extractor").strip()
        extractor_node = extractor_node or "system.memory_extractor"

        if not already_created:
            create_task = ctx.get("create_task")
            if not callable(create_task):
                raise RuntimeError("memory extraction create_task callback is unavailable")
            workspace_value = ctx.get("workspace_root") or pending_extract.get("workspace_root")
            workspace_root = Path(workspace_value) if workspace_value is not None else None
            conversation_key = str(pending_extract.get("conversation_key") or "").strip()
            try:
                from engine.builtin.knowledge_inject import _conversation_memory_namespace
                memory_namespace = _conversation_memory_namespace(conversation_key)
            except Exception:
                memory_namespace = ""
            mem_dir = workspace_root / "data" / "memory" if workspace_root is not None else None
            if mem_dir is not None and memory_namespace:
                mem_dir = mem_dir / memory_namespace
            book_names = sorted(
                path.stem for path in mem_dir.glob("*.yaml")
            ) if mem_dir is not None and mem_dir.exists() else []
            book_list_header = ""
            if book_names:
                book_list_header = (
                    f"当前已有的 memory book 列表：{', '.join(book_names)}\n"
                    "保存时优先使用已有 book，也可以创建新 book。\n\n"
                )
            child_sid = f"child_{uuid.uuid4().hex[:12]}"
            create_task(
                session_id=sid,
                session_generation=session_generation,
                kind=pending_extract.get("kind") or "node",
                node_id=extractor_node,
                input_data={
                    "instruction": book_list_header + transcript,
                    "child_session_id": child_sid,
                    "_system_task": True,
                    "_memory_extract_intent_id": intent_id,
                    "task_context": {"conversation_key": conversation_key},
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )

        # Move cursors only after task_created is durable (or observed on recovery).
        self._memory_extract_task_counts[sid] = task_count
        self._memory_extract_last_extracted_task_counts[sid] = task_count
        self._memory_extract_pending_task_ids[sid] = []
        self._memory_extract_pending.pop(sid, None)
        complete_intent = ctx.get("complete_memory_extract_intent")
        if callable(complete_intent):
            complete_intent(intent_id)


def _task_kind_is_node(task: Any) -> bool:
    """Return whether a task is a node task without depending on task enums."""
    # Why: importing the supervisor task enum would recreate the cycle this move is
    # meant to remove. How: compare the enum value when present, otherwise compare
    # the plain string. Purpose: keep the gate compatible with real tasks and
    # lightweight tests.
    kind = getattr(task, "kind", "")
    value = getattr(kind, "value", kind)
    return str(value) == "node"


def _format_transcript_for_extract(messages: list[Any], *, max_chars: int = 12000) -> str:
    """Format task-scoped messages into readable transcript text."""
    # [AutoC 2026-05-24] Why: memory extraction now receives ConversationStore
    # Message objects selected by source_task_id, and the old [role]\ncontent
    # blocks are harder for the extractor to read. How: format each message as a
    # natural role label such as "User: ..." or "Assistant: ...". Purpose: give
    # the memory extractor only the precise pending-task dialogue in a stable form.
    parts: list[str] = []
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "")
            content: Any = msg.get("content", "")
            message_type = str(msg.get("message_type") or msg.get("type") or "")
            name = str(msg.get("name") or "")
        else:
            role = str(getattr(msg, "role", "") or "")
            content = getattr(msg, "content", "")
            message_type = str(getattr(msg, "message_type", "") or "")
            name = str(getattr(msg, "name", "") or "")
        if role == "system":
            continue
        if isinstance(content, list):
            # Why: older session message dictionaries can contain multimodal
            # content lists. How: preserve only text parts before formatting.
            # Purpose: avoid leaking raw attachment dictionaries to the extractor.
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            continue
        is_tool_result = role == "tool" or message_type == "tool_result"
        limit = 500 if is_tool_result else 2000
        if len(content) > limit:
            content = content[:limit] + "...<truncated>"
        if is_tool_result:
            label = f"Tool ({name})" if name else "Tool"
        elif role == "user":
            label = "User"
        elif role == "assistant":
            label = "Assistant"
        else:
            label = role.capitalize() if role else "Message"
        parts.append(f"{label}: {content}")

    # 超预算时保留最近的几条。原来是正序累加、超了就 break，丢掉的是当条**及其后
    # 全部** —— 于是一个长回合里提取器只看得到最早的部分，跟「总结刚刚发生了什么」
    # 正好相反。（送压缩器的文本方向相反是对的：它要总结的是即将被删的最老内容。）
    kept: list[str] = []
    total = 0
    for line in reversed(parts):
        if kept and total + len(line) > max_chars:
            break
        total += len(line)
        kept.append(line)
    kept.reverse()
    return "\n\n".join(kept)


def _safe_int(value: Any, default: int) -> int:
    """Convert a value to int with a small fallback."""
    # Why: hook contexts can be supplied by tests or future registries. How: guard
    # integer conversion at the boundary. Purpose: keep timer callbacks best-effort
    # and avoid losing the supervisor thread to malformed context data.
    try:
        return int(value)
    except Exception:
        return default
