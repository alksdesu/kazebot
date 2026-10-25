"""LLM 调用（含重试逻辑）。

从 ai_step.py 抽出。依赖 _LoopState、_StreamBuffer。
"""
from __future__ import annotations

import asyncio
import json
import time
from contextvars import ContextVar
from functools import wraps
from typing import Any

from clonoth_runtime import parse_bool

from .stream_buffer import _StreamBuffer
from ..attachments import prepare_messages_for_llm, strip_image_blocks
from ..compact import bounded_tool_result_view, total_content_chars
from ..prompt import finalize_system_tail
from ..protocol import TaskAction, ACTION_CANCELLED, ACTION_FAIL
from .loop_state import _LoopState, _persist_ctx, _short
from .llm_outcome import (
    error_kind,
    is_context_overflow,
    is_filtered,
    is_retryable as _is_retryable_error,
    with_changes,
)
from .llm_outcome import RETRYABLE_STATUS_CODES as _RETRYABLE_STATUS_CODES  # noqa: F401
# message_to_llm 反序列化：在发送 LLM 前做格式转换（role 修正 + 内部字段剥离）
from .tool_format import build_llm_messages, sanitize_control_tool_history
from engine.signals import Signal, get_bus
from engine.signals.types import make_span_id
from plugin_runtime import plugin_scope
from plugin_runtime.manager import current_scope


_llm_child_tasks: ContextVar[list[asyncio.Task] | None] = ContextVar("llm_child_tasks", default=None)
_POLL_INTERVAL_SEC = 0.3


def _node_strips_images(node: Any) -> bool:
    """Whether this node's model should never receive image_url content blocks.

    Why: draw/analysis nodes may run on non-vision models (e.g. deepseek-v4-flash)
    that reject multimodal content with an HTTP 400 like
    ``unknown variant `image_url`, expected `text```. How: opt-in via YAML; unknown
    keys land in ``Node.extra``. Accept ``strip_images`` or ``no_vision`` truthy.
    Purpose: keep the behavior generic and configuration-driven per node.
    """
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        return False
    return any(parse_bool(extra.get(key), False) for key in ("strip_images", "no_vision"))


def _build_messages_for_provider(
    messages: list[dict[str, Any]],
    formatter: Any,
    provider: Any,
) -> list[dict[str, Any]]:
    """Build the message view that should be handed to the provider.

    [2026-05-01] OpenAI Responses native tools need the L1 storage view: the
    assistant message still has Clonoth's structured ``tool_calls`` field and
    provider metadata.  FakeNativeToolFormatter would collapse that data into
    user text, and true NativeToolFormatter would strip provider metadata.
    Therefore the Responses provider bypasses L2 for both native and fake-native
    modes, then performs its own final API conversion.  Other providers keep
    the existing L2 formatter path.
    """
    messages = finalize_system_tail(messages)
    provider_name = getattr(provider, "name", "") or ""
    formatter_mode = getattr(formatter, "mode", "") if formatter else ""
    # [2026-05-01] Responses conversion needs storage-level tool_calls and _meta.
    # Bypass both true native and fake-native L2; fake-native would text-collapse,
    # while true native would strip provider metadata needed for raw_output replay.
    # Gemini also needs _meta for raw_parts (thoughtSignature + functionCall round-trip)
    if provider_name in {"openai-responses", "gemini"} and formatter_mode in {"native", "fake-native"}:
        # Keep storage-level tool_calls/_meta for the provider, but match
        # build_llm_messages by dropping ephemeral retry/control messages.
        # [2026-05-07] 旁路 provider 也必须执行控制流历史清洗。
        # 原因：Gemini/Responses 为保留 provider meta 绕过 L2，若不在这里清理会继续回放 finish。
        # 做法：先用 L1 形态清洗 finish tool_call/tool_result，再保留 _meta 交给 provider。
        # 目的：不破坏普通工具配对，同时防止控制流伪工具进入原生 provider 历史。
        return [dict(msg) for msg in sanitize_control_tool_history(messages) if not msg.get("_ephemeral")]
    return build_llm_messages(messages, formatter) if formatter else messages


# ---------------------------------------------------------------------------
#  LLM 调用（含重试）
# ---------------------------------------------------------------------------

def _llm_plugin_context(function):
    @wraps(function)
    async def call(ls, step):
        with plugin_scope(
            workspace_root=ls.rctx.workspace_root, node_id=ls.node.id,
            task_id=ls.rctx.task_id,
            channel=getattr(ls.provider, "plugin_channel", "") or getattr(ls.node, "provider", ""),
            provider=getattr(ls.provider, "provider_name", "") or getattr(ls.provider, "name", ""),
            purpose=current_scope().get("purpose") or ("system" if ls.node.id.startswith("system.") else "inference"),
        ):
            tasks = []
            token = _llm_child_tasks.set(tasks)
            try:
                return await function(ls, step)
            finally:
                try:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    _llm_child_tasks.reset(token)
    return call


def _create_llm_task(coroutine):
    task = asyncio.create_task(coroutine)
    tasks = _llm_child_tasks.get()
    if tasks is not None:
        tasks.append(task)
    return task


def request_view(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """发给模型的那份消息视图：较早的大块工具结果截短，持久历史不动。压缩阈值也按它估，两边口径一致。"""
    view, _ = bounded_tool_result_view(messages)
    return view


def provider_messages(
    view: list[dict[str, Any]],
    *,
    formatter: Any,
    provider: Any,
    workspace_root: Any,
    strip_images: bool,
) -> list[dict[str, Any]]:
    """把请求视图转成某个 provider 能收的消息：格式转换、file:// 图片转 base64，模型不收图时剥掉图片。"""
    prepared = prepare_messages_for_llm(_build_messages_for_provider(view, formatter, provider), workspace_root)
    return strip_image_blocks(prepared) if strip_images else prepared


def cancelled_action(ls: Any) -> TaskAction:
    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")


async def _acknowledge_cancel(ls: Any, step: int) -> None:
    await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})


async def poll_preempt(ls: Any) -> dict[str, Any] | None:
    """查一次抢占：带消息的记进 preempt_inject_info 并返回它，不带消息的只标 preempt_after_step。"""
    if ls.preempt_after_step or ls.preempt_inject_info is not None:
        return None
    info = await ls.rctx.check_preempted()
    if not info.get("preempted"):
        return None
    if info.get("message"):
        ls.preempt_inject_info = info
        return info
    ls.preempt_after_step = True
    return None


async def _stop_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def wait_provider_call(ls: Any, coroutine: Any, step: int) -> Any:
    """等一次 provider 调用，期间轮询取消和抢占。被取消时停掉请求、回执 cancel_acknowledged，返回 None。"""
    rctx = getattr(ls, "rctx", None)
    if not callable(getattr(rctx, "check_cancelled", None)):
        return await coroutine
    task = _create_llm_task(coroutine)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_POLL_INTERVAL_SEC)
            if task in done:
                return task.result()
            if await rctx.check_cancelled():
                await _stop_task(task)
                await _acknowledge_cancel(ls, step)
                return None
            await poll_preempt(ls)
    except BaseException:
        await _stop_task(task)
        raise


async def record_response_usage(ls: Any, resp: Any, llm_messages: list[dict[str, Any]], tools_arg: Any) -> None:
    """记下这次请求的真实 prompt 用量，压缩阈值和 supervisor 的字符预算都按它校准。"""
    usage = getattr(resp, "usage", None)
    if not isinstance(usage, dict) or not isinstance(usage.get("prompt_tokens"), int):
        return
    ls.last_prompt_tokens = usage["prompt_tokens"]
    ls.last_usage = dict(usage)
    ls.compacted = False
    await ls.rctx.emit_event("context_usage", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "usage": usage,
        # 配上真实 token 数，supervisor 侧的字符预算才能按本会话的实际比例裁剪；
        # 硬编码 3 字符/token 是按英文定的，中文会把预算放大两三倍。
        "prompt_chars": total_content_chars(llm_messages) + (
            len(json.dumps(tools_arg, ensure_ascii=False, default=str)) if tools_arg else 0
        ),
    })


@_llm_plugin_context
async def _call_llm_with_retry(ls: _LoopState, step: int):
    """LLM 调用（含重试）。

    返回值：
      - ProviderResponse: 正常完成
      - TaskAction(CANCELLED): 被取消
      - None: 思考阶段被 preempt 截断，partial message 已丢弃
    """
    tools_arg = ls.openai_tools if ls.openai_tools else None
    # ls.messages 是运行时状态，只在副本上转换；build_llm_messages 跳过 _ephemeral，保留 _dynamic
    strip_images = _node_strips_images(ls.node)
    llm_messages = provider_messages(
        request_view(ls.messages), formatter=ls.formatter, provider=ls.provider,
        workspace_root=ls.rctx.workspace_root, strip_images=strip_images,
    )

    resp = None
    _retry_attempt = 0

    _bus = get_bus()
    _span_id = make_span_id()
    _sig_payload = {"model": getattr(ls.provider, 'model', 'unknown'), "provider": type(ls.provider).__name__}
    _sig_t0 = time.monotonic()
    _bus.emit(Signal(name="llm.call.start", payload=_sig_payload, span_id=_span_id))

    def _emit_call_end(**extra: Any) -> None:
        _elapsed = round((time.monotonic() - _sig_t0) * 1000, 1)
        _bus.emit(Signal(name="llm.call.end", payload={**_sig_payload, "elapsed_ms": _elapsed, **extra}, span_id=_span_id))

    while True:
        # 每次真正发请求都换 request id，这一次的流式块在前端共用一张卡片
        request_id = ls.rctx.begin_llm_request()

        # ---- 流式调用 ----
        if ls.use_stream:
            text_buf = _StreamBuffer(ls.rctx, ls.node.id, "text", request_id=request_id)
            think_buf = _StreamBuffer(ls.rctx, ls.node.id, "thinking", request_id=request_id)

            async def _emit_tool_delta(payload: dict[str, Any]) -> None:
                # supervisor 只认 emit_event，工具调用增量和正文、思考走同一条事件流
                event_payload = dict(payload or {})
                event_payload.setdefault("node_id", ls.node.id)
                event_payload.setdefault("task_id", ls.rctx.task_id)
                event_payload.setdefault("llm_request_id", request_id)
                await ls.rctx.emit_event("tool_call_delta", event_payload)

            async def _close_stream_buffers() -> None:
                await text_buf.flush()
                await think_buf.flush()
                if text_buf.flushed_any or think_buf.flushed_any:
                    await ls.rctx.emit_event("stream_end", {
                        "node_id": ls.node.id,
                        "llm_request_id": request_id,
                        "has_text": text_buf.flushed_any,
                        "has_reasoning": think_buf.flushed_any,
                    })

            stream_task = _create_llm_task(
                ls.provider.chat_stream(
                    messages=llm_messages,
                    tools=tools_arg,
                    on_text=text_buf.push,
                    on_thinking=think_buf.push,
                    on_tool_delta=_emit_tool_delta,
                )
            )
            while True:
                done, _ = await asyncio.wait({stream_task}, timeout=_POLL_INTERVAL_SEC)
                if stream_task in done:
                    resp = stream_task.result()
                    break
                if await ls.rctx.check_cancelled():
                    # 流式输出中取消：没写完的 assistant 消息不进历史，停在上一轮的完整状态
                    await _stop_task(stream_task)
                    await _close_stream_buffers()
                    await _acknowledge_cancel(ls, step)
                    _emit_call_end(cancelled=True)
                    return cancelled_action(ls)
                preempt = await poll_preempt(ls)
                if preempt is not None and not text_buf.flushed_any and text_buf.is_empty:
                    # 还在思考阶段就来了新消息：丢掉这条没写完的回复，回主循环注入新消息后重新推理；
                    # 已经开始输出正文的照常等它说完
                    await _stop_task(stream_task)
                    await _close_stream_buffers()
                    await ls.rctx.emit_event("preempt_thinking_truncated", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "step": step,
                    })
                    _emit_call_end(preempt_truncated=True)
                    return None
            await text_buf.flush()
            await think_buf.flush()
            # 各家 provider 都是思考在前正文在后：第一个思考 token 算开始，第一个正文 token 算结束
            if think_buf.flushed_any:
                ls._reasoning_started_iso = think_buf.first_push_iso
                if text_buf.first_push_at is not None:
                    ls._reasoning_ended_iso = text_buf.first_push_iso
                else:
                    # No text output — thinking lasted until stream end
                    import datetime as _dt_rc
                    ls._reasoning_ended_iso = _dt_rc.datetime.now(_dt_rc.timezone.utc).isoformat()
            if text_buf.flushed_any or think_buf.flushed_any:
                await ls.rctx.emit_event("stream_end", {
                    "node_id": ls.node.id,
                    "llm_request_id": request_id,
                    "has_text": text_buf.flushed_any,
                    "has_reasoning": think_buf.flushed_any,
                })
            if resp.ok and resp.tool_calls:
                ls.use_stream = False
        else:
            resp = await wait_provider_call(ls, ls.provider.chat(messages=llm_messages, tools=tools_arg), step)
            if resp is None:
                _emit_call_end(cancelled=True)
                return cancelled_action(ls)

        await record_response_usage(ls, resp, llm_messages, tools_arg)

        # ---- 重试判定 ----
        _retry_reason = ""
        _is_empty_ok = resp.ok and not resp.tool_calls and not (resp.text or "").strip()
        # 被审核拦下的空回复，同一渠道重试只会再被拦一次
        _blocked_empty = _is_empty_ok and is_filtered(resp)
        if not resp.ok and _is_retryable_error(resp):
            _retry_reason = resp.error or error_kind(resp) or "unknown"
        elif _is_empty_ok and not _blocked_empty:
            _retry_reason = "empty_response"

        if _retry_reason and _retry_attempt < ls.retry_max:
            _retry_attempt += 1
            _delay = min(
                ls.retry_initial_delay * (ls.retry_backoff ** (_retry_attempt - 1)),
                ls.retry_max_delay,
            )
            await ls.rctx.emit_event("llm_retry", {
                "node_id": ls.node.id,
                "step": step,
                "attempt": _retry_attempt,
                "max_retries": ls.retry_max,
                "delay_sec": round(_delay, 2),
                "error": _retry_reason,
                "error_kind": error_kind(resp),
                "status_code": resp.status_code,
            })
            _bus.emit(Signal(
                name="llm.retry",
                payload={**_sig_payload, "attempt": _retry_attempt, "error": _retry_reason,
                         "error_type": "retryable", "error_kind": error_kind(resp),
                         "status_code": resp.status_code},
                span_id=_span_id,
            ))
            _waited = 0.0
            while _waited < _delay:
                _sleep_step = min(0.5, _delay - _waited)
                await asyncio.sleep(_sleep_step)
                _waited += _sleep_step
                if await ls.rctx.check_cancelled():
                    await _acknowledge_cancel(ls, step)
                    _emit_call_end(cancelled=True)
                    return cancelled_action(ls)
                await poll_preempt(ls)
            resp = None
            continue

        # 空回复保持 ok=True 的话 after_llm_call 里的备用渠道 hook 不会介入，
        # 系统节点会在同一个只回空的模型上一直空转到 max_steps
        if _blocked_empty:
            resp = with_changes(
                resp, ok=False, error_kind="content_filter",
                error=f"response blocked by the provider ({getattr(resp, 'finish_reason', '')})",
            )
        elif _is_empty_ok and _retry_attempt >= ls.retry_max:
            resp = with_changes(resp, ok=False, error=f"empty_response (retried {_retry_attempt} times)")

        # 请求超出上下文窗口：截短旧工具结果再试一次，只试一次
        if (
            not resp.ok
            and not _is_retryable_error(resp)
            and is_context_overflow(resp)
            and not getattr(ls, '_reactive_compact_done', False)
        ):
            ls._reactive_compact_done = True
            # 刚才那次请求已经按 request_view 截过旧结果，这里连最近一轮也一起截；比它短不了就不必再发
            _sent_view = request_view(ls.messages)
            _request_messages, _ = bounded_tool_result_view(
                ls.messages, max_chars=2_000, keep_latest_round=False,
            )
            _rc_cleared = sum(
                1 for _sent, _bounded in zip(_sent_view, _request_messages)
                if _sent.get("content") != _bounded.get("content")
            )
            if _rc_cleared:
                await ls.rctx.emit_event("reactive_compact", {
                    "node_id": ls.node.id, "step": step,
                    "cleared": _rc_cleared, "error": resp.error or "",
                })
                llm_messages = provider_messages(
                    _request_messages, formatter=ls.formatter, provider=ls.provider,
                    workspace_root=ls.rctx.workspace_root, strip_images=strip_images,
                )
                resp = None
                continue

        break

    _end_payload: dict[str, Any] = {"attempt": _retry_attempt + 1, "ok": resp.ok}
    if not resp.ok:
        _end_payload["error"] = resp.error or "unknown"
        _end_payload["error_kind"] = error_kind(resp)
        _bus.emit(Signal(
            name="llm.error",
            payload={**_sig_payload, "error": resp.error or "unknown", "retryable": False,
                     "error_kind": error_kind(resp), "status_code": resp.status_code},
            span_id=_span_id,
        ))
    _emit_call_end(**_end_payload)

    return resp


def _build_failure_action(ls: _LoopState, resp, step: int, retry_attempt: int = 0) -> TaskAction:
    """LLM 调用失败（重试耗尽），构建 FAIL action。"""
    _fail_msg = resp.error or "LLM 调用失败"
    if retry_attempt > 0:
        _fail_msg = f"{_fail_msg} (已重试 {retry_attempt} 次)"
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=_fail_msg,
        context_ref=ctx_ref,
        summary=_short(_fail_msg, 240),
    )
