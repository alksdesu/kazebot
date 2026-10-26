from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from engine.compact import (
    clear_tool_results,
    count_real_task_segments,
    estimate_message_tokens,
    last_assistant_time,
    microcompact_targets,
    should_compact,
)
from engine.context_window import (
    KEEP_RECENT_SHARE_OF_THRESHOLD,
    context_window_for,
    effective_thresholds,
    output_reserve_tokens,
)
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.llm_call import (
    cancelled_action,
    digest_provider_call,
    request_view,
    tool_result_view,
    view_settings,
)
from engine.inference.loop_state import _persist_ctx, compact_target_session_id, flush_conversation_writes
from engine.protocol import ACTION_DISPATCH, TaskAction
from engine.context_archive import ContextArchiveStore
from engine.task_digest import THRESHOLD_REASON, DigestOutcome, digest_settings, restore_task_digest, run_task_digest
from plugin_runtime import PluginExecutionError

logger = logging.getLogger(__name__)

# 同步压缩失败过的任务：任务被派发子节点后再恢复会换一个新的 loop state，靠这里记住
_HARD_COMPACT_FAILED_TASKS: OrderedDict[str, None] = OrderedDict()
_HARD_COMPACT_FAILED_LIMIT = 1024

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
    """Run idle cleanup, automatic context compaction and in-task progress digests before each step."""

    name = "compact_checker"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """首步按模型窗口定阈值、做空闲清理、读回挂起前的进度摘要，之后每步检查要不要压缩、要不要摘要本任务。"""
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        _note_compact_outcome(ls)
        _apply_context_window(ls)
        modified = False
        step_count = int(ctx.extra.get("step_count", 0) or 0)
        if ctx.step == step_count:
            modified = await _run_idle_cleanup(ctx, ls) or modified
        # 空闲清理会改掉旧工具结果的正文，摘要的指纹要按清理之后的消息校验
        await _restore_task_digest(ls)

        action = await _check_and_compact(ctx, ls)
        # snip 改了消息却不派发 compactor，靠 extra 里的标记把 modified 带出来
        modified = bool(ctx.extra.pop("compact_modified", False)) or modified
        if action is None:
            action = await _check_task_digest(ctx, ls)
        if action is not None:
            return hook_result(action=action, modified=modified)
        if modified:
            return hook_result(modified=True)
        return None


def _task_key(ls: Any) -> str:
    return str(getattr(getattr(ls, "rctx", None), "task_id", "") or "")


def _note_compact_outcome(ls: Any) -> None:
    """读恢复数据里的 compact_done：同步压缩失败过，本任务内不再挂起当轮去压。"""
    resume = getattr(ls, "resume_data", None)
    task_key = _task_key(ls)
    failed = (
        isinstance(resume, dict)
        and str(resume.get("type") or "") == "compact_done"
        and resume.get("success", True) is False
    )
    if failed and task_key:
        _HARD_COMPACT_FAILED_TASKS[task_key] = None
        _HARD_COMPACT_FAILED_TASKS.move_to_end(task_key)
        while len(_HARD_COMPACT_FAILED_TASKS) > _HARD_COMPACT_FAILED_LIMIT:
            _HARD_COMPACT_FAILED_TASKS.popitem(last=False)
    if failed or (task_key and task_key in _HARD_COMPACT_FAILED_TASKS):
        if not getattr(ls, "hard_compact_disabled", False):
            logger.warning("hard compaction disabled for task %s after a failed compaction", task_key or "?")
        ls.hard_compact_disabled = True


def _apply_context_window(ls: Any) -> None:
    """每个 loop state 只算一次：知道模型窗口时把压缩阈值收紧到窗口放得下的范围，写回 loop state。"""
    if getattr(ls, "context_window_checked", False):
        return
    ls.context_window_checked = True
    try:
        provider = getattr(ls, "provider", None)
        model = str(getattr(provider, "model", "") or "")
        runtime_cfg = getattr(ls, "runtime_cfg", None)
        window = context_window_for(model, runtime_cfg=runtime_cfg, node=getattr(ls, "node", None))
        ls.context_window = window
        if window is None:
            return
        soft = int(getattr(ls, "compact_threshold", 0) or 0)
        hard = int(getattr(ls, "compact_hard_threshold", 0) or 0)
        result = effective_thresholds(window, soft=soft, hard=hard, reserve=output_reserve_tokens(provider, runtime_cfg))
        ls.compact_threshold = result.soft
        ls.compact_hard_threshold = result.hard
        keep = int(getattr(ls, "compact_keep_recent_tokens", 0) or 0)
        keep_ceiling = int(result.soft * KEEP_RECENT_SHARE_OF_THRESHOLD)
        if keep > 0 and result.soft > 0 and keep > keep_ceiling:
            ls.compact_keep_recent_tokens = keep_ceiling
        logger.info(
            "context window %d tokens for model %s (node %s, output reserve %d): compact thresholds %d/%d -> %d/%d",
            window, model or "?", getattr(getattr(ls, "node", None), "id", "?"), result.reserve,
            soft, hard, result.soft, result.hard,
        )
    except Exception:
        logger.warning("resolving the model context window failed; keeping the configured thresholds", exc_info=True)


async def _restore_task_digest(ls: Any) -> None:
    if getattr(ls, "task_digest_restored", False):
        return
    ls.task_digest_restored = True
    if getattr(ls, "task_digest", None) is not None:
        return
    try:
        await restore_task_digest(ls)
    except Exception:
        logger.warning("restoring the task digest failed: task=%s", _task_key(ls) or "?", exc_info=True)


async def _check_task_digest(ctx: Any, ls: Any) -> TaskAction | None:
    """本任务自己的消息在请求视图里超过 软阈值 × 触发比例 时，把较早的完整轮次摘要掉；被取消时返回取消动作。"""
    settings = digest_settings(getattr(ls, "runtime_cfg", None))
    soft = int(getattr(ls, "compact_threshold", 0) or 0)
    trigger = settings.trigger_tokens(soft)
    # 软打断后这一步跑完就交出去，花这次摘要调用没有意义
    if trigger <= 0 or getattr(ls, "preempt_after_step", False):
        return None
    try:
        view_cfg = view_settings(ls)
        outcome = await run_task_digest(
            ls,
            step=ctx.step,
            view=tool_result_view(ls.messages, ls, settings=view_cfg),
            chars_per_token=view_cfg.chars_per_token,
            trigger_tokens=trigger,
            keep_tokens=settings.keep_tokens(soft),
            call=digest_provider_call(ls, ctx.step),
            reason=THRESHOLD_REASON,
        )
    except PluginExecutionError:
        raise
    except Exception:
        logger.warning("task digest failed: task=%s", _task_key(ls) or "?", exc_info=True)
        return None
    return cancelled_action(ls) if outcome is DigestOutcome.CANCELLED else None


async def _run_idle_cleanup(ctx: Any, ls: Any) -> bool:
    """任务第一步：空闲太久就清空旧工具结果的正文，再按空闲时长剪掉更早的任务。"""
    modified = False
    try:
        targets = microcompact_targets(ls.messages)
        if targets:
            # 只归档要清空的那几条原文；历史每个任务都从存储重载，同一条会被反复清，归档按内容去重
            originals = [deepcopy(ls.messages[index]) for index in targets]
            await asyncio.to_thread(
                ContextArchiveStore(ls.rctx.workspace_root).archive_each,
                compact_target_session_id(ls), originals, reason="microcompact",
            )
            compacted = deepcopy(ls.messages)
            cleared = clear_tool_results(compacted, targets)
            ls.messages = compacted
            ctx.messages = compacted
            logger.info("microcompact: cleared %d tool_results", cleared)
            modified = True
    except Exception as archive_error:
        logger.warning("microcompact archive failed; keeping original messages: %s", archive_error)

    try:
        from engine.task_record import load_task_records, snip_history

        last_ts = last_assistant_time(ls.messages)
        if last_ts is not None:
            gap_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
            if gap_hours >= 1.0:
                proactive_max = max(int(gap_hours) * 2, 2)
                # 落盘的 snip 一律改父会话，不改入口分支的副本
                snip_sid = compact_target_session_id(ls)
                snip_records = await asyncio.to_thread(load_task_records, ls.rctx.workspace_root, snip_sid)
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
    if store is not None:
        # 本任务排队中的写入得先落盘，不然 replace_all 的指纹比对拿的是旧文件
        await flush_conversation_writes(ls.rctx)
        original = list(await asyncio.to_thread(store.load, session_id))
    else:
        original = ls.messages
    actual = safe_snip_task_ids(original, safe_snip_task_ids(ls.messages, task_ids))
    messages, _, actual = snip_history_tasks(ls.messages, records, actual)
    if not actual:
        raise RuntimeError("Task summaries would split a tool call and its result")
    await asyncio.to_thread(
        ContextArchiveStore(ls.rctx.workspace_root).archive, session_id, original, reason=reason,
    )
    if store is not None:
        await asyncio.to_thread(store.replace_all, session_id, snip_store(original, records, actual))
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
    view = request_view(ls.messages, ls)
    if not should_compact(view, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    # 软阈值只说明「该压了」，不该让用户等：先在后台起一个压缩任务，当轮继续推理，
    # 下一轮才吃到压缩后的历史。只有顶到硬阈值才挂起当轮同步压，否则供应商会拒。
    hard_threshold = int(getattr(ls, "compact_hard_threshold", 0) or 0)
    over_hard = hard_threshold > 0 and should_compact(view, hard_threshold, ls.last_prompt_tokens)
    if not over_hard:
        await _request_background_compact(ctx, ls, compact_sid)
        return None
    if getattr(ls, "hard_compact_disabled", False) or _task_key(ls) in _HARD_COMPACT_FAILED_TASKS:
        # 同步压缩已经失败过一次，再挂起只会原样再失败；交给后台压缩，供应商真拒了还有被动压缩兜底
        await _request_background_compact(ctx, ls, compact_sid)
        return None

    # 历史里可压的任务段不超过 keep_recent 时直接跳过，省一次 compactor 调用
    try:
        _conv_store = getattr(ls.rctx, 'conversation_store', None)
        if _conv_store:
            await flush_conversation_writes(ls.rctx)
            _stored_msgs = await asyncio.to_thread(_conv_store.load, compact_sid)
            # 旧的 compact_summary 已经是压过的历史，算成一段会让 compactor 反复被派发
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

        # 落盘的 snip 改父会话，下一次分叉直接从已经剪过的历史开始
        snip_sid = compact_sid
        snip_records = await asyncio.to_thread(load_task_records, ls.rctx.workspace_root, snip_sid)
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
