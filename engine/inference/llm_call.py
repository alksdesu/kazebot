"""LLM 调用（含重试逻辑）。

从 ai_step.py 抽出。依赖 _LoopState、_StreamBuffer。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from clonoth_runtime import get_bool, parse_bool

from .stream_buffer import _StreamBuffer
from ..attachments import prepare_messages_for_llm, strip_image_blocks
from ..compact import (
    DEFAULT_CHARS_PER_TOKEN,
    TOOL_RESULT_VIEW_CHARS,
    bounded_tool_result_view,
    observed_chars_per_token,
    total_content_chars,
)
from ..context_window import threshold_report
from ..task_digest import (
    OVERFLOW_REASON,
    DigestOutcome,
    apply_task_digest,
    digest_settings,
    run_task_digest,
)
from ..prompt import finalize_system_tail
from ..protocol import TaskAction, ACTION_CANCELLED, ACTION_FAIL
from ..view_marks import render_view_marks
from .loop_state import _LoopState, _persist_ctx, _short, context_archive_session_id
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
from plugin_runtime import PluginExecutionError, plugin_scope
from plugin_runtime.manager import current_scope

logger = logging.getLogger(__name__)

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
    modes, then performs its own final API conversion.  Anthropic bypasses L2 in
    native mode only, for the signed thinking blocks kept in ``_meta``.  Other
    providers keep the existing L2 formatter path.
    """
    messages = finalize_system_tail(messages)
    provider_name = getattr(provider, "name", "") or ""
    formatter_mode = getattr(formatter, "mode", "") if formatter else ""
    # [2026-05-01] Responses conversion needs storage-level tool_calls and _meta.
    # Bypass both true native and fake-native L2; fake-native would text-collapse,
    # while true native would strip provider metadata needed for raw_output replay.
    # Gemini also needs _meta for raw_parts (thoughtSignature + functionCall round-trip)
    # Anthropic 开着扩展思考时，工具循环里漏传上一轮带签名的 thinking 块就被拒；fake-native 不发 tool_use，照旧走 L2
    keeps_provider_meta = (
        (provider_name in {"openai-responses", "gemini"} and formatter_mode in {"native", "fake-native"})
        or (provider_name == "anthropic" and formatter_mode == "native")
    )
    if keeps_provider_meta:
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


@dataclass(frozen=True)
class ViewSettings:
    supersede: bool = False
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
    workspace_root: Any = None
    # 渠道会不会把历史思维链发回去：会的话估 token 时思维链也算字数，和实测比值的口径一致
    include_reasoning: bool = False


_SESSION_RATIO_KEY = "chars_per_token"
_RATIO_MIN = 1.0
_RATIO_MAX = 6.0


def session_chars_per_token(task_context: Any) -> float | None:
    """supervisor 建任务时放进 task_context 的会话实测字符/token 比；没有或认不出时为 None。"""
    raw = task_context.get(_SESSION_RATIO_KEY) if isinstance(task_context, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not raw > 0 or raw == float("inf"):
        return None
    return min(max(float(raw), _RATIO_MIN), _RATIO_MAX)


def loop_chars_per_token(ls: Any) -> float:
    """本任务上一次请求实测的字符/token 比；本任务还没有实测值时用会话上次请求的比值，都没有才回落默认值。"""
    tokens = getattr(ls, "last_prompt_tokens", None)
    chars = int(getattr(ls, "last_prompt_chars", 0) or 0)
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 and chars > 0:
        return observed_chars_per_token(chars, tokens)
    task_context = getattr(getattr(ls, "rctx", None), "task_context", None)
    return session_chars_per_token(task_context) or DEFAULT_CHARS_PER_TOKEN


def _provider_option(provider: Any, key: str) -> Any:
    # provider 没有公开生效参数的接口，只能读它构造时合并好的 OptionSet（runtime < 渠道 < 节点）
    getter = getattr(getattr(provider, "_options", None), "get", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:
        return None


def replays_reasoning(provider: Any, formatter: Any = None) -> bool:
    """这个渠道会不会把历史里的思维链随请求发回去。

    DeepSeek 原样回传；通用 OpenAI 渠道没关「回传思维链」就回传；Anthropic 只在 native 模式回放带签名的
    thinking 块，内容和思维链相当；Gemini、OpenAI Responses 和认不出的渠道按不发算。
    """
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if name == "deepseek":
        return True
    if name == "openai":
        return _provider_option(provider, "replay_reasoning") is not False
    if name == "anthropic":
        return str(getattr(formatter, "mode", "") or "") == "native"
    return False


def view_settings(ls: Any) -> ViewSettings:
    """请求视图按 loop state 取的设置：读后让位开关、校准过的字符/token 比和思维链算不算字数。"""
    if ls is None:
        return ViewSettings()
    cfg = getattr(ls, "runtime_cfg", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    return ViewSettings(
        supersede=get_bool(cfg, "engine.compact.supersede_reads", True),
        chars_per_token=loop_chars_per_token(ls),
        workspace_root=getattr(getattr(ls, "rctx", None), "workspace_root", None),
        include_reasoning=replays_reasoning(getattr(ls, "provider", None), getattr(ls, "formatter", None)),
    )


def tool_result_view(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """和 messages 逐条对应的视图：打过标记的旧工具结果按标记截短、让位或清空，其余照原文。"""
    return render_view_marks(messages)


def with_task_digest(messages: list[dict[str, Any]], view: list[dict[str, Any]], ls: Any) -> list[dict[str, Any]]:
    """本任务有进度摘要、且它覆盖的消息指纹全部对得上时，把那些消息换成一条摘要；对不上就原样返回。"""
    digest = getattr(ls, "task_digest", None)
    task_id = str(getattr(getattr(ls, "rctx", None), "task_id", "") or "")
    if digest is None or not task_id:
        return view
    applied = apply_task_digest(messages, view, task_id, digest)
    return view if applied is None else applied


def request_view(messages: list[dict[str, Any]], ls: Any = None) -> list[dict[str, Any]]:
    """发给模型的那份消息视图，持久历史不动。压缩阈值也按它估，两边口径一致。

    旧工具结果只按落了盘的标记渲染，请求时不再现算截短和让位，同一条消息每次发出去都一样；
    给了 loop state 再换入本任务的进度摘要。
    """
    view = tool_result_view(messages)
    return view if ls is None else with_task_digest(messages, view, ls)


def overflow_view(messages: list[dict[str, Any]], ls: Any) -> list[dict[str, Any]]:
    """供应商报上下文溢出后重试用的视图：在标记之上现算截短，不再保护，最近一轮也截到普通上限，不落盘。"""
    settings = view_settings(ls)
    view, _ = bounded_tool_result_view(
        render_view_marks(messages),
        max_chars=TOOL_RESULT_VIEW_CHARS,
        keep_latest_round=False,
        chars_per_token=settings.chars_per_token,
        supersede=settings.supersede,
        workspace_root=settings.workspace_root,
    )
    return with_task_digest(messages, view, ls)


async def provider_messages(
    view: list[dict[str, Any]],
    *,
    formatter: Any,
    provider: Any,
    workspace_root: Any,
    strip_images: bool,
) -> list[dict[str, Any]]:
    """把请求视图转成某个 provider 能收的消息：格式转换、file:// 图片转 base64，模型不收图时剥掉图片。

    读图、压缩和编码在线程里做，不占事件循环。
    """
    built = _build_messages_for_provider(view, formatter, provider)
    prepared = await asyncio.to_thread(prepare_messages_for_llm, built, workspace_root)
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
    """记下这次请求的真实 prompt 用量，压缩阈值、请求视图的保护预算和 supervisor 的字符预算都按它校准。"""
    usage = getattr(resp, "usage", None)
    if not isinstance(usage, dict) or not isinstance(usage.get("prompt_tokens"), int):
        return
    include_reasoning = replays_reasoning(getattr(ls, "provider", None), getattr(ls, "formatter", None))
    prompt_chars = total_content_chars(llm_messages, include_reasoning=include_reasoning) + (
        len(json.dumps(tools_arg, ensure_ascii=False, default=str)) if tools_arg else 0
    )
    ls.last_prompt_tokens = usage["prompt_tokens"]
    ls.last_prompt_chars = prompt_chars
    ls.last_usage = dict(usage)
    ls.compacted = False
    payload: dict[str, Any] = {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "usage": usage,
        # 配上真实 token 数，supervisor 侧的字符预算才能按本会话的实际比例裁剪；
        # 硬编码 3 字符/token 是按英文定的，中文会把预算放大两三倍。
        "prompt_chars": prompt_chars,
        "reasoning_counted": include_reasoning,
        # get_context_window 报的阈值得是按模型窗口收紧后真正生效的这一份
        **threshold_report(ls),
    }
    # 子节点和系统任务的事件也发到路由会话，supervisor 靠它把比值记到这段对话自己名下
    conversation = context_archive_session_id(getattr(ls, "rctx", None))
    if conversation:
        payload["conversation_session_id"] = conversation
    await ls.rctx.emit_event("context_usage", payload)


def digest_provider_call(ls: Any, step: int) -> Any:
    """任务进度摘要用的那次请求：节点自己的 provider，不带工具，等待期间照常响应取消和抢占。"""
    async def call(messages: list[dict[str, Any]]) -> Any:
        prepared = await provider_messages(
            messages, formatter=ls.formatter, provider=ls.provider,
            workspace_root=ls.rctx.workspace_root, strip_images=True,
        )
        return await wait_provider_call(ls, ls.provider.chat(messages=prepared, tools=None), step)

    return call


async def _overflow_digest(ls: Any, step: int) -> DigestOutcome:
    settings = digest_settings(getattr(ls, "runtime_cfg", None))
    if not settings.enabled:
        return DigestOutcome.SKIPPED
    view_cfg = view_settings(ls)
    threshold = int(getattr(ls, "compact_threshold", 0) or 0)
    return await run_task_digest(
        ls,
        step=step,
        view=tool_result_view(ls.messages),
        chars_per_token=view_cfg.chars_per_token,
        include_reasoning=view_cfg.include_reasoning,
        trigger_tokens=0,
        keep_tokens=settings.keep_tokens(threshold),
        call=digest_provider_call(ls, step),
        reason=OVERFLOW_REASON,
        require_no_digest=True,
    )


_RETRY_CANCELLED = object()


async def _overflow_retry_messages(ls: Any, step: int, resp: Any, strip_images: bool) -> Any:
    """上下文溢出后重发的消息：本任务还没有进度摘要就先摘要，摘要不了或摘要后还溢出再截短旧工具结果。

    两条路各只走一次；都走不通返回 None，被取消返回 _RETRY_CANCELLED。
    """
    if not getattr(ls, "_reactive_digest_done", False):
        ls._reactive_digest_done = True
        try:
            outcome = await _overflow_digest(ls, step)
        except PluginExecutionError:
            raise
        except Exception:
            # 摘要这条路出了问题也不能连截短重试一起丢掉
            logger.warning("task digest after a context overflow failed: task=%s", ls.rctx.task_id, exc_info=True)
            outcome = DigestOutcome.SKIPPED
        if outcome is DigestOutcome.CANCELLED:
            return _RETRY_CANCELLED
        if outcome is DigestOutcome.CREATED:
            return await provider_messages(
                request_view(ls.messages, ls), formatter=ls.formatter, provider=ls.provider,
                workspace_root=ls.rctx.workspace_root, strip_images=strip_images,
            )
    if getattr(ls, "_reactive_compact_done", False):
        return None
    ls._reactive_compact_done = True
    # 刚才那次请求已经按 request_view 截过旧结果，这里连最近一轮也一起截；比它短不了就不必再发
    sent_view = request_view(ls.messages, ls)
    retry_view = overflow_view(ls.messages, ls)
    cleared = sum(1 for sent, retry in zip(sent_view, retry_view) if sent.get("content") != retry.get("content"))
    if not cleared:
        return None
    await ls.rctx.emit_event("reactive_compact", {
        "node_id": ls.node.id, "step": step,
        "cleared": cleared, "error": resp.error or "",
    })
    return await provider_messages(
        retry_view, formatter=ls.formatter, provider=ls.provider,
        workspace_root=ls.rctx.workspace_root, strip_images=strip_images,
    )


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
    llm_messages = await provider_messages(
        request_view(ls.messages, ls), formatter=ls.formatter, provider=ls.provider,
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
        # 思考计时只属于这一次请求：不流式或没有思考的那次，不能沿用上一次留下的值
        ls._reasoning_started_iso = ""
        ls._reasoning_ended_iso = ""

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

        # 请求超出上下文窗口：先摘要本任务较早的轮次，再不行截短旧工具结果，各重试一次
        if not resp.ok and not _is_retryable_error(resp) and is_context_overflow(resp):
            _retry_messages = await _overflow_retry_messages(ls, step, resp, strip_images)
            if _retry_messages is _RETRY_CANCELLED:
                _emit_call_end(cancelled=True)
                return cancelled_action(ls)
            if _retry_messages is not None:
                llm_messages = _retry_messages
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
        # 旧 provider 不报类别：照样是模型调用失败，只是说不出是哪一种
        error_kind=error_kind(resp) or "unknown",
        context_ref=ctx_ref,
        summary=_short(_fail_msg, 240),
    )
