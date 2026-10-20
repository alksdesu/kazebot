from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from engine.compact import (
    count_real_task_segments,
    estimate_message_tokens,
    microcompact_messages,
    should_compact,
)
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.loop_state import _persist_ctx, compact_target_session_id
from engine.protocol import ACTION_DISPATCH, TaskAction
from engine.context_archive import ContextArchiveStore

logger = logging.getLogger(__name__)

# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "CompactChecker",
    "hook_points": [
        ("before_step", "handle"),
    ],
    "priority": 50,
}


class CompactChecker:
    """Run idle cleanup and automatic context compaction before each step."""

    name = "compact_checker"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Apply the legacy microcompact, proactive snip, and compact checks.

        Why: ai_step.py contained several context pressure checks inline. How:
        read loop state from HookContext.extra, run the same first-step cleanup,
        then dispatch the system compactor when the threshold is exceeded.
        Purpose: keep context management behavior unchanged while moving it into
        a before_step handler.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        modified = False
        step_count = int(ctx.extra.get("step_count", 0) or 0)
        if ctx.step == step_count:
            modified = await _run_idle_cleanup(ctx, ls) or modified

        action = await _check_and_compact(ctx, ls)
        # Why: snip-based compaction mutates messages but intentionally does not
        # dispatch the LLM compactor. How: _check_and_compact marks this in
        # ctx.extra, and the handler folds it into the returned HookResult.
        # Purpose: callers can observe non-terminal compaction mutations.
        modified = bool(ctx.extra.pop("compact_modified", False)) or modified
        if action is not None:
            return hook_result(action=action, modified=modified)
        if modified:
            return hook_result(modified=True)
        return None


async def _run_idle_cleanup(ctx: Any, ls: Any) -> bool:
    """Run first-step microcompact and proactive snip cleanup.

    Why: these operations reduce stale context before the next model call. How:
    copy the old first-iteration logic from ai_step.py into this helper. Purpose:
    preserve the old trigger timing while keeping CompactChecker readable.
    """
    modified = False
    try:
        compacted, cleared = microcompact_messages(deepcopy(ls.messages))
        if cleared:
            ContextArchiveStore(ls.rctx.workspace_root).archive(
                compact_target_session_id(ls), ls.messages, reason="microcompact",
            )
            ls.messages = compacted
            ctx.messages = compacted
            logger.info("microcompact: cleared %d tool_results", cleared)
            modified = True
    except Exception as archive_error:
        logger.warning("microcompact archive failed; keeping original messages: %s", archive_error)

    try:
        from engine.task_record import load_task_records, snip_history

        last_ts = None
        for msg in reversed(ls.messages):
            meta = msg.get("_meta", {})
            if isinstance(meta, dict) and (meta.get("message_type") == "assistant" or msg.get("role") == "assistant"):
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    try:
                        last_ts = datetime.fromisoformat(ts_str)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                break
        if last_ts is not None:
            gap_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
            if gap_hours >= 1.0:
                proactive_max = max(int(gap_hours) * 2, 2)
                # [AutoC 2026-05-13] Why: proactive L2 cleanup used to snip the
                # branch copy when child_session_id/session_id pointed at a fork.
                # How: use the same parent-first compact target helper as the
                # threshold path. Purpose: all persisted snips affect the durable
                # parent session.
                snip_sid = compact_target_session_id(ls)
                snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
                if snip_records:
                    snipped, snip_count, snipped_ids = snip_history(
                        ls.messages,
                        snip_records,
                        keep_recent_tasks=3,
                        max_snip=proactive_max,
                    )
                    if snip_count > 0:
                        snip_count = await _apply_snip(ctx, ls, snip_sid, snip_records, snipped, snipped_ids, reason="proactive_snip")
                        logger.info(
                            "proactive snip: replaced %d tasks (gap=%.1fh, max=%d)",
                            snip_count,
                            gap_hours,
                            proactive_max,
                        )
                        modified = True
    except Exception as snip_error:
        logger.warning("proactive snip failed: %s", snip_error)

    return modified


async def _apply_snip(ctx: Any, ls: Any, session_id: str, records: list, messages: list[dict], task_ids: set[str], *, reason: str) -> int:
    from engine.task_record import safe_snip_task_ids, snip_history_tasks, snip_store

    parent_id = str(getattr(ls.rctx, "parent_session_id", "") or "")
    child_id = str(getattr(ls.rctx, "child_session_id", "") or "")
    if parent_id and parent_id != ls.rctx.session_id and not child_id:
        response = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/compact-snip",
            json={"target_session_id": session_id, "task_ids": sorted(task_ids)},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("applied"):
            raise RuntimeError("Supervisor could not synchronize task summaries")
        actual_ids = payload.get("snipped_task_ids")
        if not isinstance(actual_ids, list) or any(not isinstance(task_id, str) for task_id in actual_ids):
            raise RuntimeError("Supervisor did not report task summary coverage")
        actual = set(actual_ids)
        if not actual or not actual <= task_ids:
            raise RuntimeError("Supervisor returned unexpected task summary coverage")
        messages, count, _ = snip_history_tasks(ls.messages, records, actual)
        ls.messages = messages
        ctx.messages = messages
        return count
    store = getattr(ls.rctx, "conversation_store", None)
    original = store.load(session_id) if store is not None else ls.messages
    actual = safe_snip_task_ids(original, safe_snip_task_ids(ls.messages, task_ids))
    messages, _, actual = snip_history_tasks(ls.messages, records, actual)
    if not actual:
        raise RuntimeError("Task summaries would split a tool call and its result")
    ContextArchiveStore(ls.rctx.workspace_root).archive(session_id, original, reason=reason)
    if store is not None:
        store.replace_all(session_id, snip_store(original, records, actual))
    ls.messages = messages
    ctx.messages = messages
    return len(actual)


def _build_compactor_input(ctx: Any, ls: Any) -> str:
    from engine.compaction_plan import build_compaction_plan

    _, instruction = build_compaction_plan(
        ls.messages, keep_recent=ls.compact_keep_recent,
        keep_recent_tokens=int(getattr(ls, "compact_keep_recent_tokens", 0) or 0),
    )
    return instruction


async def _request_background_compact(ctx: Any, ls: Any, compact_sid: str) -> None:
    """Ask the supervisor to compact this conversation without suspending the turn."""
    if ls.compact_async_requested or not compact_sid:
        return
    ls.compact_async_requested = True
    conversation_text = _build_compactor_input(ctx, ls)
    if not conversation_text.strip():
        return
    try:
        response = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/compact-async",
            json={
                "session_id": str(getattr(ls.rctx, "session_id", "") or ""),
                "parent_session_id": str(getattr(ls.rctx, "parent_session_id", "") or ""),
                "target_session_id": compact_sid,
                "instruction": conversation_text,
                "caller_node_id": ls.node.id,
                "keep_recent": ls.compact_keep_recent,
                "keep_recent_tokens": int(getattr(ls, "compact_keep_recent_tokens", 0) or 0),
                "threshold_tokens": ls.compact_threshold,
            },
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception as request_error:
        # 后台压缩失败不能影响当轮：硬阈值那条路还会兜底。
        logger.warning("background compact request failed: %s", request_error)
        return

    if not isinstance(payload, dict) or not payload.get("task_id"):
        logger.info("background compact skipped: %s", (payload or {}).get("reason", "already running"))
        return
    logger.info("background compact dispatched task=%s session=%s", payload["task_id"], compact_sid)
    await ls.rctx.emit_event("compact_start", {
        "node_id": ls.node.id,
        "step": ctx.step,
        "mode": "background",
        "target_session_id": compact_sid,
    })


async def _check_and_compact(ctx: Any, ls: Any) -> TaskAction | None:
    """Return a compactor dispatch action when the legacy threshold says so."""
    if ls.compacted or ls.compact_threshold <= 0:
        return None
    compact_sid = compact_target_session_id(ls)
    # 熔断判定归 supervisor：压缩失败几乎全在那一侧记账，而 engine 是多个独立 worker 进程，
    # 各自的计数器既读不到对方也读不到 supervisor 的。后台压缩会被 compact-async 拒回，
    # 硬阈值 dispatch 会被 supervisor 换成应急截断。
    if not should_compact(ls.messages, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    # 软阈值只说明「该压了」，不该让用户等：先在后台起一个压缩任务，当轮继续推理，
    # 下一轮才吃到压缩后的历史。只有顶到硬阈值才挂起当轮同步压，否则供应商会拒。
    hard_threshold = int(getattr(ls, "compact_hard_threshold", 0) or 0)
    over_hard = hard_threshold > 0 and should_compact(ls.messages, hard_threshold, ls.last_prompt_tokens)
    if not over_hard:
        await _request_background_compact(ctx, ls, compact_sid)
        return None

    # ---------------------------------------------------------------
    # Pre-check: count task segments in ConversationStore. If there
    # are not enough segments to compress (≤ keep_recent), skip the
    # LLM compactor call entirely to avoid wasting API calls.
    # ---------------------------------------------------------------
    try:
        _conv_store = getattr(ls.rctx, 'conversation_store', None)
        if _conv_store:
            _stored_msgs = _conv_store.load(compact_sid)
            # [2026-05-17] Why: compact_summary is already compressed history,
            # not a real task segment, so counting it can trigger an endless
            # LLM compactor loop. How: use the shared counter that skips old
            # compact summaries and only counts consecutive real task ids.
            # Purpose: builtin compact and legacy ai_step compact make the same
            # dispatch decision before asking the compactor LLM to summarize.
            _seg_count = count_real_task_segments(_stored_msgs)
            if not getattr(ls, "compact_keep_recent_tokens", 0) and _seg_count <= ls.compact_keep_recent:
                logger.info(
                    "skip compact: only %d task segments (keep_recent=%d), not enough to compress",
                    _seg_count, ls.compact_keep_recent,
                )
                ls.compacted = True
                return None
    except Exception as _seg_err:
        logger.warning("segment pre-check failed, proceeding with compact: %s", _seg_err)

    try:
        from engine.task_record import (
            load_task_records,
            snip_history,
        )

        # [AutoC 2026-05-13] Why: L2 persisted snips must affect the parent
        # ConversationStore, not an entry branch fork. How: reuse compact_sid for
        # transcripts and store replacement. Purpose: the next fork starts from
        # already-snipped durable history.
        snip_sid = compact_sid
        snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
        if snip_records:
            snipped, snip_count, snipped_ids = snip_history(ls.messages, snip_records)
            if snip_count > 0:
                snip_count = await _apply_snip(ctx, ls, snip_sid, snip_records, snipped, snipped_ids, reason="threshold_snip")
                await ls.rctx.emit_event("snip_compact", {
                    "node_id": ls.node.id,
                    "step": ctx.step,
                    "snipped_tasks": snip_count,
                })
                logger.info("snip_compact: replaced %d tasks, skipping LLM compact", snip_count)
                ls.compacted = True
                ctx.extra["compact_modified"] = True
                return None
    except Exception as snip_error:
        logger.warning("snip compact failed, falling through to LLM compact: %s", snip_error)

    ls.compacted = True
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": ctx.step, "mode": "blocking"})
        conversation_text = _build_compactor_input(ctx, ls)
        if conversation_text.strip():
            ctx_ref = _persist_ctx(ls, ctx.step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input={
                    "instruction": conversation_text,
                    "_compact_dispatch": True,
                    "context_mode": "fresh",
                    "_compact_keep_recent": ls.compact_keep_recent,
                    # 不发这两个字段的话 supervisor 的收缩循环只能回落到硬编码 256000，
                    # 把 threshold_tokens 调低就会变成「engine 反复触发、supervisor 压不够」的死循环。
                    "_compact_keep_recent_tokens": int(getattr(ls, "compact_keep_recent_tokens", 0) or 0),
                    "_compact_threshold_tokens": ls.compact_threshold,
                    # [AutoC 2026-05-13] Why: supervisor applies the compactor
                    # result after this task is suspended, so it needs an explicit
                    # durable target. How: pass the parent-first compact session id
                    # through dispatch_input. Purpose: LLM compact cannot fall back
                    # to rewriting a branch session.
                    "target_session_id": compact_sid,
                    "_system_task": True,
                    "use_context": False,
                },
            )
    except Exception as compact_error:
        await ls.rctx.emit_event("compact_failed", {
            "node_id": ls.node.id,
            "step": ctx.step,
            "error": str(compact_error),
        })
    return None


def estimate_context_tokens(messages: list[dict[str, Any]], last_usage: dict | None = None) -> int:
    """Estimate token usage using the same fallback rules as ai_step.py.

    Why: context usage estimation is useful outside ai_step after compaction logic
    moved into hooks. How: prefer the last provider usage, then fall back to
    stored assistant completion usage and character counts. Purpose: keep future
    dynamic-context updates able to reuse the extracted helper.
    """
    if last_usage:
        prompt_tokens = last_usage.get("prompt_tokens", 0) or 0
        completion_tokens = last_usage.get("completion_tokens", 0) or 0
        if prompt_tokens > 0:
            return prompt_tokens + completion_tokens

    total = 0
    for msg in messages:
        if msg.get("_dynamic") or msg.get("_ephemeral"):
            continue
        meta = msg.get("_meta", {})
        if isinstance(meta, dict):
            usage = meta.get("usage", {})
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens", 0)
                if completion_tokens and isinstance(completion_tokens, int) and completion_tokens > 0:
                    total += completion_tokens
                    continue
        total += estimate_message_tokens(msg)
    return total
