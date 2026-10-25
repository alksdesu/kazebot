from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from copy import deepcopy
from functools import wraps
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from toolbox.tool_names import is_pseudo_tool_name

from .pseudo_tools import (
    _dispatch_delegate_specs,
    _finish_spec,
    _ask_spec,
    _reply_spec,
    _compact_context_spec,
    _preempt_task_spec,
    _switch_node_spec,
    _to_openai_tools,
    _filter_tool_specs,
)
from .resume_builder import _build_resume_messages
from .loop_state import (
    TOOL_CONTEXT_EXTRA_ATTRS,
    _LoopState,
    _persist_ctx,
    _short,
    build_tool_context,
    conversation_writer,
    flush_conversation_writes,
)
from .llm_call import _call_llm_with_retry, _build_failure_action
from .llm_outcome import is_truncated, with_changes
from .async_delivery import build_async_result_payload, deliver_async_result
from .pseudo_handlers import _handle_pseudo_tool

from ..context_store import load_context_snapshot
from ..attachments import build_multimodal_content, payload_attachments, screen_attachments
from ..conversation_store import Message, MessageType
from ..node import Node
from .message_assembly import assemble_initial_messages
from ..conversation_routing import concise_text, routing_meta, short_reply
from ..protocol import (
    TaskAction,
    ACTION_DISPATCH,
    ACTION_FINISH,
    ACTION_ASK,
    ACTION_FAIL,
    ACTION_CANCELLED,
    ACTION_PREEMPTED,
)
from ..tool_step import TOOL_CANCELLED, result_to_raw, summarize_result, tool_outcome
from clonoth_runtime import get_int, get_float, get_bool, load_runtime_config
from toolbox.context import ToolContext
# build_llm_messages: 反序列化方向的格式转换，在 llm_call.py 中实际调用。
# 此处导入供外部通过 ai_step 模块访问（如测试、调试）。
from .tool_format import (
    ParsedToolCall,
    create_tool_formatter,
    build_llm_messages,
    invalid_arguments,
)
from .message_model import MessageMeta, set_message_meta
from providers.base import BaseProvider, ToolCall
# Phase 2 Signal System: 导入信号总线，用于发射 tool.call 和 task.error 信号。
# get_bus() 返回全局单例 SignalBus，Signal 是不可变事件数据类。
from ..signals import Signal, get_bus
# Phase 3 Hook System：引入 hook registry 与上下文对象。
# 原因：before_tool_call 的业务检查要从 ai_step.py 的硬编码分支迁出。
# 做法：ai_step 只负责构造 HookContext 并触发 registry；具体规则由 handler 实现。
# 目的：后续内核逻辑可以插件化注册，同时保持当前推理循环行为不变。
from ..hooks import HookContext, hook_registry
from ..builtin.loader import auto_discover_and_register
from ..builtin.plaintext import build_implicit_finish
from ..hooks.loader import load_external_plugins
from plugin_runtime import plugin_scope

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import httpx

    from ..context import RunContext


# ---------------------------------------------------------------------------
#  异步工具跟踪表（Async Tool Tracking）
#  [2026-04-23] 从 commit 7d10197 恢复，在 864a333 大扫除中被误删。
#  key = async_tool_id (8 位 hex), value = 状态字典
#  用于关联异步工具的启动占位消息与 preempt 回传结果。
#  done/failed 条目保留 5 分钟后自动清理，防止无限增长。
# ---------------------------------------------------------------------------
_async_tool_tasks: dict[str, dict] = {}

# 清理阈值：done/failed 条目保留秒数
_ASYNC_TRACK_RETAIN_SEC = 300  # 5 minutes


def _cleanup_async_tracker() -> None:
    """清理已完成超过 _ASYNC_TRACK_RETAIN_SEC 的条目。

    在每次新增 tracking 条目时调用，避免 map 无限增长。
    只清理 status 为 done 或 failed 且 finished_at 已过期的条目。
    """
    now = time.monotonic()
    expired = [
        k for k, v in _async_tool_tasks.items()
        if v.get("status") in ("done", "failed")
        and now - v.get("finished_at", now) > _ASYNC_TRACK_RETAIN_SEC
    ]
    for k in expired:
        del _async_tool_tasks[k]


def get_async_tool_tasks() -> list[dict]:
    """导出当前所有异步工具跟踪条目，供外部查询。

    返回列表，每项包含 async_id, tool_name, status, elapsed 等字段。
    """
    result = []
    now = time.monotonic()
    for aid, info in _async_tool_tasks.items():
        entry = {"async_id": aid, **info}
        # 对 running 状态补算已经过的时间
        if info.get("status") == "running" and "started_at" in info:
            entry["elapsed"] = round(now - info["started_at"], 1)
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
#  影子写入：运行期消息同步进 ConversationStore
# ---------------------------------------------------------------------------

def _shadow_write(ls: _LoopState, msg_dict: dict, message_type: str = "") -> None:
    """把一条运行期消息排进本任务的对话写入队列，按提交顺序在后台落盘。_dynamic/_ephemeral 消息不落盘。

    子节点写自己的 child session，其余写运行 session（可能是入口分支，合并由 supervisor 做）。
    """
    if msg_dict.get('_dynamic') or msg_dict.get('_ephemeral'):
        return
    _routing_meta = routing_meta(getattr(ls.rctx, "task_context", {}) or {})
    _routing_meta["source_task_id"] = getattr(ls.rctx, "task_id", "")
    msg_dict.setdefault("_meta", {}).update(_routing_meta)
    writer = conversation_writer(ls.rctx)
    if writer is None:
        return
    try:
        _raw_content = msg_dict.get('content', '')
        # 落盘在后台线程里做，排队期间运行期消息可能还会被改，这里先拷一份
        msg = Message(
            id=str(uuid.uuid4()),
            role=msg_dict.get('role', 'user'),
            content=deepcopy(_raw_content) if isinstance(_raw_content, (str, list)) else str(_raw_content),
            message_type=message_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=deepcopy(msg_dict.get('_meta', {})),
            source_node_id=getattr(ls.node, 'id', ''),
            source_task_id=getattr(ls.rctx, 'task_id', ''),
            tool_calls=deepcopy(msg_dict.get('tool_calls', [])),
            tool_call_id=str(msg_dict.get('tool_call_id') or ''),
            name=str(msg_dict.get('name') or ''),
        )
        target_session = getattr(ls.rctx, 'child_session_id', '') or ls.rctx.session_id
        writer.submit(target_session, msg)
    except Exception:
        logger.exception("shadow write failed: task=%s", getattr(ls.rctx, "task_id", ""))
        return
    ls.last_shadow_message_id = msg.id
    if not getattr(ls.rctx, "first_shadow_message_id", ""):
        ls.rctx.first_shadow_message_id = msg.id
    ls.rctx.last_shadow_message_id = msg.id


# ---------------------------------------------------------------------------
#  推理循环子函数
# ---------------------------------------------------------------------------

async def _check_preempt_and_cancel(ls: _LoopState, step: int) -> TaskAction | None:
    """循环顶部：取消检查 + preempt 检查。返回 TaskAction 则退出循环。"""
    if await ls.rctx.check_cancelled():
        await ls.rctx.emit_event("cancel_acknowledged", {
            "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
        })
        return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")

    if not ls.preempt_after_step and ls.preempt_inject_info is None:
        _pi = await ls.rctx.check_preempted()
        if _pi.get("preempted"):
            if _pi.get("message"):
                ls.preempt_inject_info = _pi
            else:
                ls.preempt_after_step = True
                await ls.rctx.emit_event("preempt_acknowledged", {
                    "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
                })
    return None


def _invalid_arguments_error(tool_name: str, raw_arguments: str) -> str:
    return (
        f"Error: 参数不是合法 JSON，{tool_name} 没有执行。请用一个完整的 JSON 对象作为 arguments 重新调用。"
        f"收到的参数（截断）：{raw_arguments[:300]}"
    )


def _append_tool_error(ls: _LoopState, tool_call: Any, text: str) -> None:
    """给一个没有执行的工具调用补上错误结果，保持调用与结果的配对。"""
    message = ls.formatter.format_tool_result(tool_call, text)
    # 结果带上当前 tool_mode，真 native 的 role=tool 消息下一轮才会被原样透传
    set_message_meta(message, MessageMeta(
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="tool_result",
    ))
    ls.messages.append(message)
    _shadow_write(ls, message, message_type=MessageType.TOOL_RESULT)


def _append_truncation_hint(ls: _LoopState) -> None:
    ls.messages.append({
        "role": "user",
        "content": ls.formatter.build_truncation_hint(),
        "_retry_hint": True,
    })


def _handle_truncated_response(ls: _LoopState, resp: Any, step: int) -> TaskAction | None:
    """回复被输出上限截断：不当完整回复交付，带提示重来；次数用完把残文照发并注明被截断。"""
    if ls.preempt_after_step:
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=_persist_ctx(ls, step + 1), summary="任务被软打断，上下文已保存。",
        )
    ls.truncation_retry_count += 1
    if ls.truncation_retry_count <= ls.truncation_retry_max:
        logger.warning(
            "model reply truncated by the output limit; retrying (node=%s step=%d attempt=%d/%d)",
            ls.node.id, step, ls.truncation_retry_count, ls.truncation_retry_max,
        )
        _append_truncation_hint(ls)
        ls.use_stream = ls.streaming
        return None
    text = (getattr(resp, "text", None) or "").strip()
    if text:
        logger.warning(
            "model reply still truncated after %d retries; delivering it marked as cut off (node=%s step=%d)",
            ls.truncation_retry_max, ls.node.id, step,
        )
        return build_implicit_finish(ls, resp, text, step=step, truncated=True)
    error = "模型回复多次超出输出长度上限被截断，未交付。"
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id, error=error,
        context_ref=_persist_ctx(ls, step + 1), summary=error,
    )


async def _handle_tool_calls(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理 tool_calls（伪工具 + 真工具）。

    返回 TaskAction 则退出循环；返回 None 则 continue 到下一轮。
    """
    # 【方案 A 重构】伪工具改为列表按序执行，不再是 last-wins
    # 原本单标量 `pseudo_call = tc` 会导致同轮多伪工具只有最后一个生效，
    # 在 Fix 2（JSON 自由正文反向包装为 reply）后尤其危险——
    # 例如“委派工具 + 自由正文”会被吞掉委派，只留 reply。
    # 现改为按 LLM 输出顺序收集所有伪工具，后续按序处理，
    # 遇到返回 TaskAction 的（finish / switch_node / dispatch 等）立即退出循环，
    # 返回 None 的（reply / compact_context / preempt_task）继续执行下一个。
    pseudo_calls: list = []
    real_tool_calls: list[dict[str, Any]] = []
    _unauthorized_names: list[str] = []
    # 不执行的调用等 assistant 消息进了历史再补错误结果：写在它前面的结果会被配对修复当孤儿删掉
    _rejected_calls: list[tuple[Any, str]] = []
    for tc in resp.tool_calls:
        _raw_arguments = invalid_arguments(tc.arguments)
        if _raw_arguments is not None:
            logger.warning("node %s tool call %s carried non-JSON arguments; not executed", ls.node.id, tc.name)
            _rejected_calls.append((tc, _invalid_arguments_error(tc.name, _raw_arguments)))
            continue
        # dispatch:{target} 这类按委派目标生成的名字也是伪工具，不走真工具授权
        if is_pseudo_tool_name(tc.name):
            pseudo_calls.append(tc)
        elif tc.name not in ls.allowed_real_tools:
            logger.warning("node %s attempted unauthorized tool call: %s (allowed: %s)",
                           ls.node.id, tc.name, ls.allowed_real_tools)
            _unauthorized_names.append(tc.name)
            _rejected_calls.append((
                tc,
                f"Error: Tool '{tc.name}' is not in this node's allowed tool list. "
                f"Use finish() to provide your output directly.",
            ))
        else:
            real_tool_calls.append({
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            })
            # P0 Task 内核化：记录工具调用摘要
            _args_str = str(tc.arguments or {})[:200]
            ls.rctx.tool_call_log.append({"name": tc.name, "args_summary": _args_str})

    # 将 LLM 的工具调用决策追加到对话历史
    _assistant_msg = ls.formatter.build_assistant_message(resp, resp.text or "", resp.tool_calls)
    # [refactor 2026-04-18] raw_parts → metadata, thinking_text → reasoning, has_thinking → has_reasoning
    # provider_meta 由 ProviderResponse 透传；engine 只搬运不解读
    # [fix 2026-04-18] provider 名称改为动态获取，不再硬编码 "openai"。
    # ls.provider.name 由 BaseProvider.name 提供，各 provider 子类在初始化时传入。
    _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
    _tc_meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        metadata={_provider_name: resp.provider_meta} if resp.provider_meta else {},
        tool_call_ids=[tc.id for tc in (resp.tool_calls or [])],
        reasoning=resp.reasoning or "",
        has_reasoning=bool(resp.reasoning),
        inline_data=resp.inline_data or [],
        usage=dict(ls.last_usage) if ls.last_usage else {},
        # [thinking-time 2026-06-01] Persist precise reasoning timing from stream.
        reasoning_started_at=getattr(ls, '_reasoning_started_iso', '') or '',
        reasoning_ended_at=getattr(ls, '_reasoning_ended_iso', '') or '',
    )
    set_message_meta(_assistant_msg, _tc_meta)
    # [2026-05-07] Store reasoning_content at top level for API round-trip.
    # DeepSeek V4 and similar models require this field in the message dict.
    # _meta.reasoning is kept for internal use but top-level survives L2 stripping.
    if resp.reasoning:
        _assistant_msg["reasoning_content"] = resp.reasoning
    ls.messages.append(_assistant_msg)
    # 含 finish 的 assistant 消息原样落盘：拆掉它后面的 tool_result 就没了配对来源
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)
    for _rejected_call, _rejected_error in _rejected_calls:
        _append_tool_error(ls, _rejected_call, _rejected_error)

    # 正文处理策略（JSON / Fake Native / Native 模式统一）：
    # 工具调用伴随的自由正文不发送给用户，也不合成为 reply 工具调用。
    # 正文通过 build_assistant_message 保留在 assistant 消息的 content 字段中，
    # LLM 下一轮能看到自己说过的话，但用户看不到。
    # 用户可见的输出仅通过 finish / reply 伪工具产生。
    # 纯文本重试逻辑（_handle_plaintext_response）保留，仅覆盖「完全没有任何工具调用」的分支。

    # ---- before_tool_call hook：本轮工具调用级检查 ----
    # Phase 3 Hook System：先触发 round-level hook，再进入伪工具和真实工具处理。
    # 原因：finish 并列检测这类业务规则不应继续硬编码在 ai_step.py。
    # 做法：把本轮所有 tool_calls 以及 legacy 过滤后的 pseudo/real 列表放进 HookContext。
    # 目的：handler 能复刻旧判断，同时后续可以继续迁移其他 before_tool_call 规则。
    _before_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step,
        response=resp,
        tool_calls=list(resp.tool_calls or []),
        extra={"pseudo_calls": pseudo_calls, "real_tool_calls": real_tool_calls, "loop_state": ls},
    )
    _before_result = await hook_registry.afire("before_tool_call", _before_ctx)
    if _before_result.action is not None:
        return _before_result.action
    if _before_result.block:
        _reject_msg = (
            _before_result.error_message
            or _before_result.reason
            or "Tool call blocked by before_tool_call hook."
        )
        # 拒绝结果按普通工具结果落盘：标成 ephemeral 的话下一轮 provider 会看到 tool_call 没有回复
        _answered = {id(_call) for _call, _ in _rejected_calls}
        for tc in resp.tool_calls:
            if id(tc) not in _answered:
                _append_tool_error(ls, tc, _reject_msg)
        return None  # 不执行任何工具，回到主循环让 AI 重试
    if _before_result.skip_step:
        return None

    # LEGACY: replaced by hook FinishGuardHandler in engine.builtin.finish_guard.
    # 原因：保留原始硬编码判断，便于下一轮清理前核对行为。
    # 做法：只注释旧逻辑，不再执行；hook 使用 pseudo_calls/real_tool_calls 复刻同一判断。
    # 目的：迁移期间可快速回溯，不破坏当前 finish 并列拒绝语义。
    # _has_finish = any(_pc.name == "finish" for _pc in pseudo_calls)
    # _has_non_reply_others = bool(real_tool_calls) or any(
    #     _pc.name not in ("finish", "reply") for _pc in pseudo_calls
    # )
    # if _has_finish and _has_non_reply_others:
    #     _reject_msg = (
    #         "\u274c REJECTED: finish() cannot be called alongside other tools "
    #         "(except reply). Execute your other tools first, wait for their "
    #         "results, then call finish() alone in a separate turn."
    #     )
    #     logger.warning(
    #         "Rejected finish + other tools in same turn (node=%s, step=%d, tools=%s)",
    #         ls.node.id, step, [tc.name for tc in resp.tool_calls],
    #     )
    #     for tc in resp.tool_calls:
    #         _err = ls.formatter.format_tool_result(tc, _reject_msg)
    #         set_message_meta(_err, MessageMeta(
    #             tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
    #             message_type="tool_result",
    #         ))
    #         ls.messages.append(_err)
    #         _shadow_write(ls, _err, message_type="tool_result")
    #     return None  # 不执行任何工具，回到主循环让 AI 重试

    # ---------------------------------------------------------------
    # [fix 2026-07-17] turn_summarizer/compactor 等 tool_access:none 的系统节点
    # 空转保护：部分模型（尤其摘要/压缩场景）会反复调一个不存在的
    # 伪工具（如 default_api:_done）而不是直接 finish，未授权拦截后只写一条
    # 错误 tool_result，回到主循环又接着空转，直到 max_steps 才失败（engine 日志
    # 里大量“attempted unauthorized tool call: _done”就是这个现象）。
    # 做法：当本轮既没有合法伪工具也没有合法真工具，但出现了未授权工具调用，
    # 且该节点不允许任何真工具（allowed_real_tools 为空，典型的系统输出型节点）时，
    # 把模型本轮输出的自由文本当作隐式 finish 直接交付，而不再回到循环空转。
    # 目的：让摘要/压缩结果能落地（或快速失败），避免无意义重试。
    # ---------------------------------------------------------------
    _has_finish = any(_pc.name in ("finish", "ask") for _pc in pseudo_calls)
    if (
        _unauthorized_names
        and not real_tool_calls
        and not _has_finish
        and not ls.allowed_real_tools
    ):
        _plain = (resp.text or "").strip()
        if _plain:
            logger.warning(
                "node %s only issued unauthorized tool call(s) %s with no allowed tools; "
                "treating plaintext output as implicit finish to avoid empty spinning",
                ls.node.id, _unauthorized_names,
            )
            return await _fire_task_end_hook_if_finish(
                ls,
                _build_unauthorized_implicit_finish(ls, resp, _plain, step),
                step + 1,
            )
        # 没有任何可交付文本，直接快速失败，不在同一个坏模型上继续空转。
        logger.warning(
            "node %s issued only unauthorized tool call(s) %s with no plaintext; failing fast",
            ls.node.id, _unauthorized_names,
        )
        _uctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_FAIL, node_id=ls.node.id,
            error=f"节点只调用了未授权工具 {_unauthorized_names} 且无文本输出。",
            context_ref=_uctx_ref,
            summary="unauthorized_tool_only",
        )

    # 处理伪工具（finish/ask 延后到真实工具之后，确保同轮真实工具不被跳过）
    # [AutoC 2026-05-31] Why: ask terminates the task just like finish in Phase 0,
    # so it must share finish's delayed execution and preempt-intercept path. How:
    # store either terminal call in one slot and execute it after real tools.
    # Purpose: preserve existing finish semantics while introducing action="ask".
    _terminal_call = None
    if pseudo_calls:
        for _pc in pseudo_calls:
            if _pc.name in ("finish", "ask"):
                _terminal_call = _pc
                continue  # finish/ask 延后执行
            action = await _handle_pseudo_tool(ls, _pc, step)
            if action is not None:
                # 其他终止型伪工具（如 switch_node）仍然立刻退出。
                return action
            # 非终止型（reply / compact_context / preempt_task）继续

    # 处理真实工具
    if real_tool_calls:
        action = await _execute_real_tools(ls, real_tool_calls, step)
        if action is not None:
            return action
        ls.use_stream = ls.streaming

        if ls.preempt_after_step:
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )

    # finish/ask 最后执行（同轮真实工具已完成）
    if _terminal_call:
        # ---------------------------------------------------------------
        # Preempt V3 需求2: finish/ask 拦截
        # 在执行 finish/ask 之前再次检查 preempt 状态。如果有待注入的 preempt
        # 消息（用户在 LLM 推理/工具执行期间发了新消息），拦截 finish/ask：
        # 不产生 TaskAction(FINISH/ASK)，改为塞一个假 tool_result 维持 native
        # 模式下 tool_use/tool_result 的配对完整性（Claude API 强校验），
        # 然后让主循环继续，下一轮由 PreemptChecker 注入新用户消息。
        #
        # 同时补全 V2 遗漏：preempt_after_step（无消息 preempt）在只有 finish/ask
        # 没有真工具的场景下也需要被检查，此前会跳过导致终止工具照常执行。
        # ---------------------------------------------------------------
        if ls.preempt_inject_info is None and not ls.preempt_after_step:
            _pi_finish = await ls.rctx.check_preempted()
            if _pi_finish.get("preempted"):
                if _pi_finish.get("message"):
                    ls.preempt_inject_info = _pi_finish
                else:
                    ls.preempt_after_step = True

        if ls.preempt_inject_info is not None:
            # 有消息的 preempt：拦截 finish/ask，塞假 tool_result，任务继续
            from .tool_format import ParsedToolCall as _FinishPTC
            _terminal_name = str(getattr(_terminal_call, "name", "") or "finish").strip() or "finish"
            _finish_parsed = _FinishPTC(
                id=getattr(_terminal_call, "id", "") or "",
                name=_terminal_name,
                arguments=dict(_terminal_call.arguments or {}),
            )
            _intercept_msg = ls.formatter.format_tool_result(
                _finish_parsed,
                "\u26a0\ufe0f Preempted: new user input received. Task continues.",
            )
            # [2026-05-01] 写入当前 tool_mode，避免真 native 的拦截结果被当作旧 fake-native。
            # [2026-05-07] preempt 拦截 ACK 只服务当前运行期配对。
            # 原因：该终止工具未交付，不能让 fake-native/json 的文本结果在恢复后压制未来正常 finish/ask。
            # 做法：补齐 ephemeral、tool_call_id 和 name，让清洗函数按调用 ID 精确移除。
            # 目的：任务继续时不会向下一轮 provider 回放被拦截的终止工具。
            _intercept_msg["_ephemeral"] = True
            if _finish_parsed.id:
                _intercept_msg.setdefault("tool_call_id", _finish_parsed.id)
            _intercept_msg.setdefault("name", _terminal_name)
            set_message_meta(_intercept_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name=_terminal_name,
                control_tool_status="preempt_intercepted",
            ))
            ls.messages.append(_intercept_msg)
            # [2026-05-07] 被新用户输入拦截的 finish 未交付，不能写入长期历史。
            # [AutoC 2026-05-31] Why: ask has the same terminal semantics, so an
            # intercepted ask is also not a delivered clarification request. How:
            # keep the result only in runtime memory and do not call _shadow_write.
            # Purpose: next prompt is driven by real user input, not by replaying an
            # intercepted terminal tool.
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": step,
            })
            # 不 return TaskAction — 函数返回 None，主循环 continue 到下一轮
        elif ls.preempt_after_step:
            # 无消息的 preempt：与真工具后的 preempt_after_step 路径对齐，
            # 保存上下文后退出任务
            # [2026-05-01] 补写 finish/ask 的 tool_result，确保 native 模式下
            # functionCall/functionResponse 严格 1:1 配对（Gemini 强校验）
            from .tool_format import ParsedToolCall as _FinishPTC2
            _terminal_name2 = str(getattr(_terminal_call, "name", "") or "finish").strip() or "finish"
            _finish_parsed2 = _FinishPTC2(
                id=getattr(_terminal_call, "id", "") or "",
                name=_terminal_name2,
                arguments=dict(_terminal_call.arguments or {}),
            )
            _preempt_result = ls.formatter.format_tool_result(
                _finish_parsed2, "preempted",
            )
            # [2026-05-07] 无消息 preempt 的 finish ACK 同样只保留在运行期。
            # 原因：保存上下文后恢复时不应看到 finish tool_call/tool_result；但本轮内存仍需满足 provider 配对。
            # 做法：设置 ephemeral，并补齐 tool_call_id/name 供 snapshot 清洗精确匹配。
            # 目的：preempt 快照只恢复真实对话，不恢复控制流占位结果。
            _preempt_result["_ephemeral"] = True
            if _finish_parsed2.id:
                _preempt_result.setdefault("tool_call_id", _finish_parsed2.id)
            _preempt_result.setdefault("name", _terminal_name2)
            set_message_meta(_preempt_result, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name=_terminal_name2,
                control_tool_status="preempted",
            ))
            ls.messages.append(_preempt_result)
            # [2026-05-07] 无消息 preempt 也不能持久化未交付的 finish/ask 结果。
            # 原因：任务会带上下文退出，恢复后不应看到已经终止的工具轮。
            # 做法：只把结果留在运行期消息中，并依赖 ephemeral 过滤快照。
            # 目的：恢复后的历史保持待继续执行的状态。
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )
        else:
            action = await _handle_pseudo_tool(ls, _terminal_call, step)
            if action is not None:
                return action

    # 无终止型动作 → 继续下一轮推理
    if pseudo_calls or real_tool_calls:
        ls.use_stream = ls.streaming
    return None


# ---------------------------------------------------------------------------
#  异步工具后台执行器 / 自适应转异步辅助
#  spec 标了 async_mode 的工具在后台跑；execute_command 同步跑超过阈值也转到后台。
#  两条路都经 async_tool_result 把结果注入回会话，模型当轮先拿到占位结果。
# ---------------------------------------------------------------------------


def _snapshot_tool_context(tool_ctx: ToolContext) -> ToolContext:
    """为后台工作复制一份足够稳定的 ToolContext 快照。"""
    # [2026-07-14] Why: _execute_real_tools 复用同一个 ToolContext，
    # 并在每轮循环里改写 tool_call_id。How: 在调度后台任务前复制 dataclass 字段以及
    # 节点级动态属性。Purpose: 后台异步的 execute_command 回调仍保留原始工具调用的
    # 审批与产物身份，不会被后续同批次工具覆盖。
    snapshot = replace(tool_ctx)
    for attr in TOOL_CONTEXT_EXTRA_ATTRS:
        if hasattr(tool_ctx, attr):
            setattr(snapshot, attr, getattr(tool_ctx, attr))
    return snapshot


def _execute_command_async_upgrade_threshold(
    tool_name: str,
    tool_args: dict[str, Any],
    runtime_cfg: dict[str, Any] | None,
) -> float | None:
    """返回 execute_command 的自适应转异步阈值秒数；不适用时返回 None。"""
    # [2026-07-14] Why: execute_command 自身的 timeout_sec 仍是硬性
    # 终止上限，但引擎可以更早停止等待。How: 仅对 execute_command 生效，取
    # min(threshold, timeout*0.8)，并跳过 timeout 太接近阈值的调用。Purpose: 慢命令
    # 在后台继续运行而无需改变工具调用语法。
    if str(tool_name or "") != "execute_command":
        return None
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    if not get_bool(cfg, "meta.execute_command.async_upgrade.enabled", True):
        return None
    threshold_sec = get_float(
        cfg,
        "meta.execute_command.async_upgrade.threshold_sec",
        60.0,
        min_value=0.01,
        max_value=3600.0,
    )
    default_timeout_sec = get_float(
        cfg,
        "meta.execute_command.default_timeout_sec",
        90.0,
        min_value=1.0,
        max_value=3600.0,
    )
    timeout_raw = (tool_args or {}).get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)
    if timeout_sec <= threshold_sec + 1.0:
        return None
    effective = min(float(threshold_sec), float(timeout_sec) * 0.8)
    return effective if effective > 0 else None


async def _execute_registry_tool_with_span(
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_ctx: ToolContext,
    *,
    async_call: bool = False,
) -> Any:
    """在既有 SignalBus span 内执行单个 registry 工具调用。"""
    # [2026-07-14] Why: 同步、声明式异步、自适应异步三条路径必须保持
    # 相同的 tool.call signal 语义。How: 把 registry.execute 统一收敛到一个 span 辅助
    # 函数。Purpose: 重构异步交付时不会丢失 signal 元数据或重复执行逻辑。
    _args_summary = _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200)
    payload: dict[str, Any] = {"tool": tool_name, "args_summary": _args_summary}
    if async_call:
        payload["async"] = True
    with get_bus().span('tool.call', payload=payload):
        return await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx)


_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coroutine: Any, *, name: str) -> asyncio.Task:
    # 事件循环只弱引用任务，不自己留一份引用的话后台回传可能半路被回收
    task = asyncio.create_task(coroutine, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _deliver_started_async_task(
    exec_task: "asyncio.Task[Any]",
    **delivery_kwargs: Any,
) -> None:
    """等一个已经在跑的工具任务结束再回传结果；自适应转异步不重跑命令，子进程始终只有一个。"""
    try:
        result = await exec_task
    except Exception as e:
        await _run_async_deliver(error=e, **delivery_kwargs)
    else:
        await _run_async_deliver(result=result, **delivery_kwargs)


async def _run_async_tool(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict,
    tool_ctx: ToolContext,
    **delivery_kwargs: Any,
) -> None:
    """后台执行声明了 async_mode 的工具，结果走 async_tool_result 回传。"""
    try:
        result = await _execute_registry_tool_with_span(registry, tool_name, tool_args, tool_ctx, async_call=True)
    except Exception as e:
        await _run_async_deliver(registry=registry, tool_name=tool_name, tool_args=tool_args, error=e, **delivery_kwargs)
    else:
        await _run_async_deliver(registry=registry, tool_name=tool_name, tool_args=tool_args, result=result, **delivery_kwargs)


def _async_attachment_paths(result: Any, workspace_root: Any) -> list[str]:
    items = [
        item if isinstance(item, dict) else {"path": item}
        for item in payload_attachments(result)
        if isinstance(item, (dict, str))
    ]
    if workspace_root is not None:
        items = screen_attachments(workspace_root, items)
    return [str(item["path"]) for item in items if item.get("path")]


def _async_failure_message(tool_name: str, async_tool_id: str, error: str, elapsed: float | None = None) -> str:
    took = f" in {elapsed:.1f}s" if elapsed is not None else ""
    return (
        f'\u274c Async tool "{tool_name}" (id: {async_tool_id}) failed{took}: {error}\n'
        "Do not infer or fabricate the missing tool result. "
        "Tell the user the operation failed and ask them to retry."
    )


def _async_result_message(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict,
    async_tool_id: str,
    elapsed: float,
    result: Any,
    error: Exception | None,
    workspace_root: Any,
) -> tuple[str, str, list[str]]:
    """返回 (回传正文, 失败原因, 附件路径)；失败原因为空表示成功。"""
    if error is not None:
        failure = str(error) or type(error).__name__
        return _async_failure_message(tool_name, async_tool_id, failure), failure, []
    outcome = tool_outcome(result)
    if not outcome.ok:
        return _async_failure_message(tool_name, async_tool_id, outcome.error, elapsed), outcome.error, []
    summary = summarize_result(tool_name, result, args=tool_args)
    _fmt, raw = result_to_raw(tool_name, result, tool_spec=registry.get_spec(tool_name))
    message = (
        f'\u2705 Async tool "{tool_name}" (id: {async_tool_id}) completed in {elapsed:.1f}s.'
        f'\nSummary: {summary}\nResult:\n{raw}'
    )
    return message, "", _async_attachment_paths(result, workspace_root)


async def _run_async_deliver(
    *,
    registry: ToolRegistry,
    http: "httpx.AsyncClient",
    supervisor_url: str,
    task_id: str,
    session_id: str,
    tool_name: str,
    tool_args: dict,
    async_tool_id: str,
    started_at: float,
    node_id: str = "",
    result: Any = None,
    error: Exception | None = None,
    workspace_root: Any = None,
) -> None:
    """整形、记账并回传一个异步工具结果。session_id 是面向用户的路由会话，分支任务传父会话。"""
    elapsed = time.monotonic() - started_at
    try:
        message, failure_error, attachments = _async_result_message(
            registry=registry, tool_name=tool_name, tool_args=tool_args, async_tool_id=async_tool_id,
            elapsed=elapsed, result=result, error=error, workspace_root=workspace_root,
        )
    except Exception as format_error:
        logger.exception("formatting async tool result failed: tool=%s id=%s", tool_name, async_tool_id)
        failure_error = str(format_error) or type(format_error).__name__
        message, attachments = _async_failure_message(tool_name, async_tool_id, failure_error), []

    _async_tool_tasks[async_tool_id] = {
        "tool_name": tool_name,
        "status": "failed" if failure_error else "done",
        "task_id": task_id,
        "started_at": started_at,
        "finished_at": time.monotonic(),
        "elapsed": round(elapsed, 1),
        **({"error": failure_error} if failure_error else {}),
    }
    payload = build_async_result_payload(
        message=message,
        tool_name=tool_name,
        task_id=task_id,
        node_id=str(node_id or ""),
        failure_error=failure_error,
        attachments=attachments,
    )
    await deliver_async_result(http, supervisor_url, session_id, payload, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
#  真实工具执行
# ---------------------------------------------------------------------------

async def _execute_real_tools(
    ls: _LoopState, real_tool_calls: list[dict[str, Any]], step: int,
) -> TaskAction | None:
    """批量执行真实工具调用，将结果追加到 messages。"""
    tool_names = [str(call.get("name") or call.get("tool_name") or "").strip() for call in real_tool_calls]
    tool_names = [name for name in tool_names if name]
    tool_names_text = "、".join(tool_names[:5])
    if len(tool_names) > 5:
        tool_names_text += f" 等 {len(tool_names)} 个工具"
    progress_message = f"[{ls.node.id}] 执行 {len(real_tool_calls)} 个工具"
    if tool_names_text:
        progress_message += f"：{tool_names_text}"

    await ls.rctx.emit_event("handoff_progress", {
        "message": progress_message,
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })

    # 整批共用一个实例，每次调用前在循环里刷新 tool_call_id
    _tool_ctx = build_tool_context(ls)

    _tool_entries: list[dict[str, Any]] = []
    _tool_atts: list[dict[str, Any]] = []
    # Phase 3 Hook System：预构造当前批次的真实工具调用对象。
    # 原因：before_tool_call 的审批类 handler 需要看到“当前工具”和“本轮工具集合”。
    # 做法：把 legacy dict 形状转换为 ParsedToolCall，避免 handler 直接依赖 ai_step 内部字典。
    # 目的：在不改变工具执行结果格式的前提下，为真实工具执行前检查提供统一输入。
    _hook_real_tool_calls = [
        ParsedToolCall(
            id=str(_call.get("id") or ""),
            name=str(_call.get("name") or ""),
            arguments=dict(_call.get("arguments") or {}),
        )
        for _call in real_tool_calls
    ]

    for _rtc in real_tool_calls:
        if await ls.rctx.check_cancelled():
            break
        _t_name = _rtc["name"]
        _t_args = _rtc["arguments"]
        # [AutoC 2026-05-31] Why: one ToolContext instance is reused for the whole
        # batch, but approval guards run during each individual tool execution. How:
        # refresh the active provider tool_call_id before hooks and registry.execute.
        # Purpose: approvals can be merged into the exact ToolCallCard for this call.
        _tool_ctx.tool_call_id = str(_rtc.get("id") or "")
        _tool_ctx.node_id = ls.node.id

        # Phase 3 Hook System：触发单个真实工具的 before_tool_call hook。
        # 原因：审批类 handler 以当前 tool_call 为粒度，不能只看整轮工具列表。
        # 做法：在实际执行工具前构造 HookContext；block/skip 时写入一个 tool_result 保持
        # native 工具调用配对完整。目的：新增 hook 不破坏后续 LLM 消息格式。
        _current_tool_call = ParsedToolCall(
            id=str(_rtc.get("id") or ""),
            name=str(_t_name),
            arguments=dict(_t_args or {}),
        )
        _tool_hook_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            tool_calls=_hook_real_tool_calls,
            extra={"real_tool_calls": real_tool_calls},
        )
        _tool_hook_result = await hook_registry.afire("before_tool_call", _tool_hook_ctx)
        if _tool_hook_result.action is not None:
            return _tool_hook_result.action
        if _tool_hook_result.block or _tool_hook_result.skip_step:
            _blocked_msg = (
                _tool_hook_result.error_message
                or _tool_hook_result.reason
                or "Tool call blocked by before_tool_call hook."
            )
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": "text",
                "raw_inline": _blocked_msg,
                "truncated": False,
                "ref": "",
                "summary": _blocked_msg[:200],
            })
            continue

        # [2026-04-23] 异步工具分流：查询 spec 判断该工具是否为 async_mode。
        # 若是，则在后台 asyncio.Task 中执行，不阻塞当前推理循环，
        # 结果通过 preempt API 异步回传。从 commit 7d10197 恢复。
        _spec = ls.registry.get_spec(_t_name)
        _is_async = _spec.get("async_mode", False) if _spec else False

        if _is_async:
            _async_tool_ctx = _snapshot_tool_context(_tool_ctx)
            prepare = getattr(ls.registry, "prepare", None)
            if prepare is not None:
                try:
                    await prepare(name=_t_name, arguments=_t_args, ctx=_async_tool_ctx)
                except Exception as error:
                    _blocked_msg = f"工具准备失败，尚未执行：{error}"
                    ls.failed_real_tools.add(_t_name)
                    _tool_entries.append({
                        "id": _rtc.get("id", ""), "name": _t_name, "args": _t_args,
                        "format": "text", "raw_inline": _blocked_msg, "truncated": False,
                        "ref": "", "summary": _blocked_msg[:200],
                    })
                    continue
            # [WS tool result fields 2026-05-19] Why: tool_call_end now exposes
            # elapsed_ms for both synchronous and async-started tools. How: capture
            # a monotonic timestamp before the lifecycle start event is emitted.
            # Purpose: downstream WebSocket consumers can show one consistent
            # duration field without depending on SignalBus internals.
            _tool_t0 = time.monotonic()
            # [WS tool events 2026-05-17] Why: WebSocket clients need structured
            # tool lifecycle events in the durable EventLog, not only localized
            # handoff_progress text. How: emit a non-transient start event before
            # the async tool is scheduled. Purpose: reconnecting clients can replay
            # the tool start through the existing EventLog catch-up path.
            await ls.rctx.emit_event("tool_call_start", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "arguments": _t_args,
            })
            _cleanup_async_tracker()
            _async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[_async_id] = {
                "tool_name": _t_name,
                "status": "running",
                "started_at": _tool_t0,
                "task_id": ls.rctx.task_id,
            }
            _spawn_background(
                _run_async_tool(
                    registry=ls.registry,
                    tool_name=_t_name,
                    tool_args=_t_args,
                    tool_ctx=_async_tool_ctx,
                    http=ls.rctx.http,
                    supervisor_url=ls.rctx.supervisor_url,
                    task_id=ls.rctx.task_id,
                    # 回传要落到 SDK 看得见的父会话，入口分支只装运行期历史
                    session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                    async_tool_id=_async_id,
                    started_at=_tool_t0,
                    node_id=ls.node.id,
                    workspace_root=ls.rctx.workspace_root,
                ),
                name=f"async_tool_{_t_name}_{_async_id}",
            )
            _async_summary = f"异步执行已启动 (id: {_async_id})，结果将通过 preempt 自动回传"
            # [WS tool result fields 2026-05-19] Why: async-started calls have no
            # final tool result yet, but clients still need the same result schema.
            # How: define the same local variables used by the synchronous branch,
            # with result=None and the immediate placeholder text as raw_inline.
            # Purpose: tool_call_end consumers can parse sync and async lifecycle
            # events without special-casing missing keys.
            _t_result = None
            _t_fmt = "text"
            _t_raw_inline = f'\u23f3 Async tool "{_t_name}" started (id: {_async_id}). Result will be delivered via preempt when ready.'
            # [2026-07-22] Why: 异步工具（如 nai_generate）成功启动后立即返回占位，
            # 永远走不到下方同步分支的 succeeded_real_tools 登记，导致 finish_guard 的
            # finish_requires_tool 硬校验永远判定“无成功工具”，画图节点因此陷入反复
            # Rejected finish 死循环、回复迟迟发不出（现象：用户追问“图呢”）。
            # How: 异步工具“成功发起”即视为满足 finish 前置——图片由工具子进程直发平台，
            # 模型只需在结果回传后 finish 播报，不需要自己再发图。Purpose: 让异步生图
            # 节点能正常 finish，不再空转。
            ls.succeeded_real_tools.add(_t_name)
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": _t_fmt,
                "raw_inline": _t_raw_inline,
                "truncated": False,
                "ref": "",
                "summary": _async_summary,
            })
            # [WS tool events 2026-05-17] Why: async tools return control before
            # the real result exists, so clients still need a lifecycle closure for
            # this immediate call. How: emit tool_call_end with async_started rather
            # than success or error. Purpose: UIs can show that the background task
            # was accepted while waiting for the later preempt-delivered result.
            await ls.rctx.emit_event("tool_call_end", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "status": "async_started",
                "summary": _async_summary,
                "result": _t_result,
                "raw_inline": _t_raw_inline,
                "format": _t_fmt,
                "elapsed_ms": round((time.monotonic() - _tool_t0) * 1000, 1),
            })
            await ls.rctx.emit_event("handoff_progress", {
                "message": f"[{ls.node.id}] {_t_name}: 异步执行已启动",
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            continue

        # ---- 同步工具：阻塞等待执行完成（原有逻辑）----
        # [WS tool result fields 2026-05-19] Why: tool_call_end should include the
        # tool execution duration. How: capture a monotonic start timestamp before
        # emitting the structured start event and running the registry call. Purpose:
        # expose elapsed_ms without changing SignalBus span behavior.
        _tool_t0 = time.monotonic()
        # [WS tool events 2026-05-17] Why: handoff_progress remains for legacy
        # consumers, but the web UI needs structured lifecycle data. How: emit a
        # durable tool_call_start immediately before the SignalBus span. Purpose:
        # the EventLog can replay the exact tool name, call id, and arguments.
        await ls.rctx.emit_event("tool_call_start", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "arguments": _t_args,
        })
        # Phase 2 Signal: tool.call span 包裹每个工具的执行过程。
        # 自动发射 tool.call.start（含工具名和参数摘要）和 tool.call.end（含 elapsed_ms 和 error）。
        # span 是同步 contextmanager，在 async 函数中直接 with 即可。
        # [2026-07-14] execute_command 长任务自动转异步：
        # 先照常发起 registry 执行，只等到自适应阈值；到点还没完成，则把
        # 仍在运行的 task 转到后台，通过 async_tool_result 回传，给模型返回
        # 「已转异步」占位结果。execute_command 自己的 timeout_sec 仍是硬 kill 上限。
        _adaptive_threshold = _execute_command_async_upgrade_threshold(_t_name, _t_args, ls.runtime_cfg)
        if _adaptive_threshold is not None:
            _exec_tool_ctx = _snapshot_tool_context(_tool_ctx)
            _exec_task = asyncio.create_task(
                _execute_registry_tool_with_span(ls.registry, _t_name, _t_args, _exec_tool_ctx),
                name=f"execute_command_adaptive_{str(_rtc.get('id') or '')[:24] or 'call'}",
            )
            _done, _pending = await asyncio.wait({_exec_task}, timeout=float(_adaptive_threshold))
            if _exec_task not in _done:
                _cleanup_async_tracker()
                _async_id = uuid.uuid4().hex[:8]
                _async_tool_tasks[_async_id] = {
                    "tool_name": _t_name,
                    "status": "running",
                    "started_at": _tool_t0,
                    "task_id": ls.rctx.task_id,
                    "upgraded_from": "sync_timeout",
                }
                _spawn_background(
                    _deliver_started_async_task(
                        _exec_task,
                        registry=ls.registry,
                        http=ls.rctx.http,
                        supervisor_url=ls.rctx.supervisor_url,
                        task_id=ls.rctx.task_id,
                        session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                        tool_name=_t_name,
                        tool_args=_t_args,
                        async_tool_id=_async_id,
                        started_at=_tool_t0,
                        node_id=ls.node.id,
                        workspace_root=ls.rctx.workspace_root,
                    ),
                    name=f"async_upgrade_{_t_name}_{_async_id}",
                )
                _async_summary = f"执行超过 {_adaptive_threshold:.1f}s，已自动转为异步 (id: {_async_id})，结果将通过 preempt 自动回传"
                _t_result = None
                _t_fmt = "text"
                _t_raw_inline = (
                    f'\u23f3 Tool "{_t_name}" exceeded {_adaptive_threshold:.1f}s and was '
                    f'auto-upgraded to async (id: {_async_id}). Result will be delivered via preempt when ready.'
                )
                _t_elapsed_ms = round((time.monotonic() - _tool_t0) * 1000, 1)
                _tool_entries.append({
                    "id": _rtc.get("id", ""),
                    "name": _t_name,
                    "args": _t_args,
                    "format": _t_fmt,
                    "raw_inline": _t_raw_inline,
                    "truncated": False,
                    "ref": "",
                    "summary": _async_summary,
                })
                await ls.rctx.emit_event("tool_call_end", {
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                    "tool_call_id": _rtc.get("id", ""),
                    "tool_name": _t_name,
                    "status": "async_started",
                    "summary": _async_summary,
                    "result": _t_result,
                    "raw_inline": _t_raw_inline,
                    "format": _t_fmt,
                    "elapsed_ms": _t_elapsed_ms,
                })
                await ls.rctx.emit_event("handoff_progress", {
                    "message": f"[{ls.node.id}] {_t_name}: 已自动转为异步执行",
                    "node_id": ls.node.id,
                    "task_id": ls.rctx.task_id,
                })
                continue
            _t_result = _exec_task.result()
        else:
            _t_result = await _execute_registry_tool_with_span(ls.registry, _t_name, _t_args, _tool_ctx)
        # 被取消的工具结果照样进 _tool_entries 再 break：tool_use 不能悬空没有结果
        _t_outcome = tool_outcome(_t_result)
        _t_cancelled = _t_outcome.status == TOOL_CANCELLED

        # finish 硬校验靠这两份记录核对本任务里真实工具有没有成功执行过，防止编造工具结果的假成功
        if _t_outcome.ok:
            ls.succeeded_real_tools.add(_t_name)
        else:
            ls.failed_real_tools.add(_t_name)

        # [summary-args 2026-05-19] Why: handoff_progress keeps the legacy
        # "[node] tool: summary" format, so argument detail must come from the
        # summary itself. How: pass the parsed tool arguments alongside the result.
        # Purpose: show commands, queries, and target paths without changing the
        # event payload shape.
        _t_summary = summarize_result(_t_name, _t_result, args=_t_args)
        # [AutoC 2026-05-31] Why: structural result routing also supports an
        # optional spec-level result_format override. How: read the spec from the
        # active registry immediately after execution and pass it to result_to_raw.
        # Purpose: keep external and built-in synchronous tool results formatted by
        # metadata or by structure without hard-coded tool-name checks.
        _t_spec = ls.registry.get_spec(_t_name)
        _t_fmt, _t_raw = result_to_raw(_t_name, _t_result, tool_spec=_t_spec)
        # [2026-04-17] 移除工具结果截断机制：不再截断、不再写 artifact，直接传完整结果。
        _t_raw_inline = _t_raw

        _tool_entries.append({
            "id": _rtc.get("id", ""),
            "name": _t_name,
            "args": _t_args,
            "format": _t_fmt,
            "raw_inline": _t_raw_inline,
            "truncated": False,  # [2026-04-17] 截断机制已移除，保留字段兼容 format_tool_trace
            "ref": "",
            "summary": _t_summary,
        })

        # 工具报错或被取消也要发结构化的结束事件，断线重连的客户端靠 EventLog 还原工具卡片
        await ls.rctx.emit_event("tool_call_end", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "status": _t_outcome.status,
            "summary": _t_summary,
            # 完整结果原样带上，截不截断由下游适配器决定
            "result": _t_result,
            "raw_inline": _t_raw_inline,
            "format": _t_fmt,
            "elapsed_ms": round((time.monotonic() - _tool_t0) * 1000, 1),
        })

        # [硬取消-场景1] 已取消的工具结果已存入 entries（上方 append），不处理附件和进度事件，
        # 直接退出循环。未执行的后续工具被跳过（循环顶部 check_cancelled），不产生 tool_result。
        if _t_cancelled:
            break

        # Phase 3 Hook System：工具附件收集交给 AttachmentCollector。
        # 原因：附件收集是 after_tool_call 的副作用，不应散落在真实工具执行主体中。
        # 做法：传入原始工具结果、局部附件列表和 loop state，由 handler 统一扩展。
        # 目的：保持最终附件选择和多模态结果提示不变。
        _attachment_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            extra={
                "loop_state": ls,
                "tool_result": _t_result,
                "tool_attachments": _tool_atts,
            },
        )
        _attachment_result = await hook_registry.afire("after_tool_call", _attachment_ctx)
        if _attachment_result.action is not None:
            return _attachment_result.action

        # LEGACY: replaced by hook AttachmentCollector.
        # if isinstance(_t_result, dict) and isinstance(_t_result.get("attachments"), list):
        #     _tool_atts.extend(_t_result["attachments"])
        #     ls.collected_attachments.extend(_t_result["attachments"])
        #     ls.tool_produced_attachments.extend(_t_result["attachments"])

        await ls.rctx.emit_event("handoff_progress", {
            "message": f"[{ls.node.id}] {_t_name}: {_t_summary}",
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        })

    if _tool_entries:
        for _entry in _tool_entries:
            _result_body = _entry["raw_inline"]
            # [AutoC 2026-05-31] Why: MCP/tools 返回值无大小限制，几 MB 的 base64 或日志
            # 全量塞进上下文导致 context_length_exceeded。
            # How: 超过阈值时截断并附提示。
            # Purpose: 防止单个 tool result 炸掉上下文。
            _MAX_TOOL_RESULT_CHARS = 32000
            if isinstance(_result_body, str) and len(_result_body) > _MAX_TOOL_RESULT_CHARS:
                _original_len = len(_result_body)
                _result_body = _result_body[:_MAX_TOOL_RESULT_CHARS] + f"\n\n...[truncated, showing {_MAX_TOOL_RESULT_CHARS:,} of {_original_len:,} chars. Use more specific parameters to get the full content.]"
            # [2026-04-17] 截断机制已移除，不再追加 truncated 提示
            # [2026-05-01] 真实工具结果统一走 formatter.format_tool_result。
            # 原因：真 native 需要 role=tool + tool_call_id，而旧代码在这里手写 user 文本，
            # 会绕过新 NativeToolFormatter。fake-native/json 仍由各自 formatter 生成旧文本。
            _tool_msg = ls.formatter.format_tool_result(
                ParsedToolCall(
                    id=str(_entry.get("id") or ""),
                    name=str(_entry["name"]),
                    arguments=dict(_entry.get("args") or {}),
                ),
                _result_body,
            )
            set_message_meta(_tool_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
            ))
            ls.messages.append(_tool_msg)
            # Phase 1: 影子写入 tool_result 消息到 ConversationStore
            _shadow_write(ls, _tool_msg, MessageType.TOOL_RESULT)
        if _tool_atts:
            # [AutoC 2026-06-01] Why: tool-generated image attachments were only
            # appended to runtime memory, so a later task could not reload them
            # from ConversationStore. How: keep the exact multimodal message in
            # a variable, append it, then shadow-write it. Purpose: persist tool
            # image results across task boundaries without changing prompt text.
            _tool_att_msg = {"role": "user", "content": build_multimodal_content(
                "以上工具执行产生了以下图片结果：", _tool_atts, workspace_root=ls.rctx.workspace_root,
            )}
            ls.messages.append(_tool_att_msg)
            _shadow_write(ls, _tool_att_msg, message_type="tool_result_attachment")

    # 一批工具结果在下一次推理前落盘，进程这时候挂了下一个 worker 恢复也看得到这批结果
    await flush_conversation_writes(ls.rctx)
    return None


# ---------------------------------------------------------------------------
#  纯文本响应处理
# ---------------------------------------------------------------------------

def _build_unauthorized_implicit_finish(ls: _LoopState, resp, text: str, step: int) -> TaskAction:
    """[fix 2026-07-17] 将只调了未授权工具但带有自由文本的输出包装为隐式 finish。

    为什么：system.turn_summarizer / system.compactor 等 tool_access:none 的系统节点，
    部分模型会反复调一个不存在的伪工具（如 default_api:_done）而不直接 finish，
    导致在循环里空转到 max_steps。怎么改：把模型本轮输出的文本当作成果直接
    交付（与 hybrid 隐式 finish 一致）。目的：摘要/压缩结果能落地，不再空转。
    """
    from ..conversation_routing import concise_text, short_reply
    concise = short_reply(getattr(ls.rctx, "task_context", {}) or {})
    if concise:
        text = concise_text(text)
    _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
    _provider_name = getattr(ls.provider, "name", "") or "unknown"
    _meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, "tool_mode", "fake-native"),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
        tool_call_ids=[],
        reasoning="",
        has_reasoning=False,
        usage=dict(ls.last_usage) if ls.last_usage else {},
    )
    set_message_meta(_assistant_msg, _meta)
    ls.messages.append(_assistant_msg)
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FINISH, node_id=ls.node.id,
        result={
            "text": text,
            "attachments": [] if concise else list(ls.tool_produced_attachments),
            "implicit_finish": True,
            "unauthorized_tool_recovery": True,
        },
        context_ref=ctx_ref,
        summary=_short(text, 240),
    )


def _handle_plaintext_response(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理纯文本响应（无 tool_calls）。"""
    text = (resp.text or "").strip()
    if not text:
        return None

    if ls.preempt_after_step:
        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
        )

    # ---- hybrid 模式：纯文本视为隐式 finish，直接投递给用户 ----
    # 不 reject、不重试，将裸文本包装为 ACTION_FINISH 返回。
    # result 中标记 implicit_finish=True，供事件日志/管理界面区分显式与隐式 finish。
    # 参见 RFC: data/rfc_hybrid_output_mode.md
    if getattr(ls.node, 'output_mode', 'tool_only') == 'hybrid':
        # 写入 assistant 消息到对话历史 + ConversationStore，与 _handle_tool_calls 对齐
        _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
        # [refactor 2026-04-18] 与 _handle_tool_calls 对齐：动态 provider 名、metadata/reasoning 新字段
        _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
        _implicit_meta = MessageMeta(
            provider=_provider_name,
            tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
            message_type="assistant",
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={},
            tool_call_ids=[],
            reasoning="",
            has_reasoning=False,
            usage=dict(ls.last_usage) if ls.last_usage else {},
        )
        set_message_meta(_assistant_msg, _implicit_meta)
        ls.messages.append(_assistant_msg)
        _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": text,
                "attachments": list(ls.tool_produced_attachments),
                "implicit_finish": True,
            },
            context_ref=ctx_ref,
            summary=_short(text, 240),
        )

    # ---- tool_only 模式：现有行为，reject 纯文本并重试 ----
    ls.plaintext_retry_count += 1
    if ls.plaintext_retry_count <= ls.plaintext_retry_max:
        _retry_hint = ls.formatter.build_retry_hint()
        ls.messages.append({
            "role": "user",
            "content": _retry_hint,
            "_retry_hint": True,
        })
        ls.use_stream = ls.streaming
        return None

    # [2026-08-06] 格式保底：重试耗尽后不再 FAIL（FAIL 不产 outbound，用户侧回复会“没了”），
    # 而是把裸文本包装为隐式 finish 正常投递给用户（对齐 hybrid 模式/plaintext.py 行为）。
    _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
    _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
    _implicit_meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
        tool_call_ids=[],
        reasoning="",
        has_reasoning=False,
        usage=dict(ls.last_usage) if ls.last_usage else {},
    )
    set_message_meta(_assistant_msg, _implicit_meta)
    ls.messages.append(_assistant_msg)
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FINISH, node_id=ls.node.id,
        result={
            "text": text,
            "attachments": list(ls.tool_produced_attachments),
            "implicit_finish": True,
            "plaintext_recovery": True,
        },
        context_ref=ctx_ref,
        summary=_short(text, 240),
    )


# ---------------------------------------------------------------------------
#  AI 节点主执行函数
# ---------------------------------------------------------------------------

def _resolve_node_int(node: Any, key: str, default: int, minimum: int, maximum: int) -> int:
    """[fix 2026-07-17] 从节点 yaml 的 extra 中读取整型覆盖值，并 clamp 到 [minimum, maximum]。

    为什么：系统节点（压缩/摘要/读图）希望把 max_steps / retry_max 等循环上限
    配得比对话主链更保守，避免在坏模型上无限重试。怎么改：node.extra 收了 yaml
    中 core 不认识的字段，直接取。目的：不新增 Node dataclass 字段即可支持节点级覆盖。
    """
    _extra = getattr(node, "extra", None) or {}
    _raw = _extra.get(key)
    if _raw is None:
        return default
    try:
        _val = int(_raw)
    except (TypeError, ValueError):
        logger.warning("node %s has invalid %s override: %r", getattr(node, "id", "?"), key, _raw)
        return default
    return max(minimum, min(maximum, _val))


def _resolve_compact_hard_threshold(runtime_cfg: dict[str, Any]) -> int:
    """Return the token budget above which compaction must suspend the live turn.

    留空时按软阈值的 1.25 倍推导：软阈值只是「该在后台压了」，真顶到窗口还得同步压，
    否则这一轮直接被供应商拒。配得比软阈值低没有意义，故取两者较大值。
    """
    soft = get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0)
    hard = get_int(runtime_cfg, "engine.compact.hard_threshold_tokens", 0, min_value=0)
    if hard <= 0:
        hard = int(soft * 1.25)
    return max(hard, soft)


def _resolve_compact_keep_recent_tokens(runtime_cfg: dict[str, Any]) -> int:
    """Return how much verbatim history to keep, capped below the trigger threshold.

    保留量必须明显小于触发阈值，否则压完仍在阈值之上：下一轮再触发、再压出同样的
    结果，每轮白烧一次压缩调用（落盘那步会因 after >= before 拒绝写入，数据不会坏）。
    """
    keep = get_int(runtime_cfg, "engine.compact.keep_recent_tokens", 0, min_value=0)
    if keep <= 0:
        return 0
    soft = get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0)
    ceiling = int(soft * 0.8)
    if soft > 0 and keep > ceiling:
        logger.warning(
            "engine.compact.keep_recent_tokens=%d 超过 threshold_tokens=%d 的 80%%，按 %d 处理",
            keep, soft, ceiling,
        )
        return ceiling
    return keep


async def _fire_task_end_hook_if_finish(ls: _LoopState, action: TaskAction, step_count: int) -> TaskAction:
    """Fire on_task_end for successful finish/ask actions and keep the action updated.

    Why: most normal AI-node exits are produced inside finish, ask, or hybrid
    plaintext branches before run_ai_node reaches its outer max_steps fallback.
    How: route ACTION_FINISH and ACTION_ASK through the registered on_task_end
    handlers and copy the snapshot context_ref back when the handler reports that
    persistence ran. Purpose: connect ContextSnapshotSaver to safe normal-end
    paths without changing dispatch, fail, cancel, or preempt terminal semantics.
    """
    if action.action not in (ACTION_FINISH, ACTION_ASK):
        return action

    # Phase 3 Hook System：普通完成路径也触发 on_task_end。
    # 原因：finish/ask 可能从多个内部 helper 提前返回，外层没有统一的“成功结束”落点。
    # 做法：只在 ACTION_FINISH/ACTION_ASK 返回前构造 HookContext，并传入 loop_state 与正确步数。
    # 目的：先覆盖低风险成功路径，后续再逐步迁移 fail/preempt/dispatch 的快照保存。
    _end_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step_count,
        extra={"loop_state": ls, "step_count": step_count, "task_action": action},
    )
    _end_result = await hook_registry.afire("on_task_end", _end_ctx)
    if _end_result.action is not None:
        action = _end_result.action
    if short_reply(getattr(ls.rctx, "task_context", {}) or {}) and action.action in (ACTION_FINISH, ACTION_ASK):
        action.action = ACTION_FINISH
        action.result = {**(action.result or {}), "text": concise_text(str((action.result or {}).get("text") or "")), "attachments": []}
    if _end_ctx.extra.get("snapshot_saved"):
        action.context_ref = str(_end_ctx.extra.get("context_ref") or "")
    return action


def _isolated_plugin_context(function):
    @wraps(function)
    async def run(**kwargs):
        rctx = kwargs["rctx"]
        node = kwargs["node"]
        provider = kwargs["provider"]
        with hook_registry.scoped(), plugin_scope(
            workspace_root=rctx.workspace_root, node_id=node.id,
            task_id=rctx.task_id,
            channel=getattr(provider, "plugin_channel", "") or getattr(node, "provider", ""),
            provider=getattr(provider, "provider_name", "") or getattr(provider, "name", ""),
            purpose="system" if node.id.startswith("system.") else "inference",
        ):
            try:
                return await function(**kwargs)
            finally:
                # 结果交回 supervisor 之前本任务排队的对话写入必须全部落盘：合并分支、压缩都要读这份历史
                await flush_conversation_writes(rctx)
    return run


@_isolated_plugin_context
async def run_ai_node(
    *,
    rctx: "RunContext",
    streaming: bool = False,
    # [provider-registry 2026-05-03] 推理循环只依赖 BaseProvider 接口。
    # 原因：provider 由 registry 创建后不一定是 OpenAI；做法：类型标注改为 BaseProvider；
    # 目的：删除不必要的具体 OpenAI 类型引用。
    provider: BaseProvider,
    registry: ToolRegistry,
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    run_id: str = "",
    context_ref: str = "",
    resume_data: dict[str, Any] | None = None,
    downstream_info: list[dict[str, str]] | None = None,
    switch_info: list[dict[str, str]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> TaskAction:
    # Phase 3 Hook System：每次进入 AI 节点都注册内置 handler。
    # 原因：内置 handler 已统一迁入 engine.builtin，并通过 PLUGIN_META 声明
    # 自己的 hook point。做法：自动扫描内置目录并注册到共享 hook_registry；
    # HookRegistry 会按名称替换旧实例。目的：删除集中硬编码注册，同时保持
    # finish_guard、approval、prompt 注入等内置规则始终可用。
    # Why: PLUGIN_META can now also declare builtin tools. How: pass the active
    # ToolRegistry into discovery so plugin-owned tools are registered before the
    # model-visible tool list is built. Purpose: keep hook and tool registration
    # in one plugin discovery pass.
    auto_discover_and_register(hook_registry, tool_registry=registry)
    load_external_plugins(hook_registry, rctx.workspace_root / "plugins")

    runtime_cfg = load_runtime_config(rctx.workspace_root)
    route_meta = routing_meta(rctx.task_context)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 64, min_value=1, max_value=200)
    # [fix 2026-07-17] 支持节点级 max_steps 覆盖：系统节点（压缩/摘要/读图等）是
    # 单轮一次性任务，不需要 32 步的大循环。压缩模型持续空响应/非法 JSON 时，若沿用
    # 全局 max_steps=32，会在同一坏模型上原地空转十几分钟拖垮整条会话（system.compactor
    # 卡死根因）。做法：优先读取节点 yaml 的 max_steps（收进 node.extra），命中即覆盖；
    # 目的：让压缩/摘要节点快速失败放行，而不是无谓地反复重试。
    _node_extra = getattr(node, "extra", None) or {}
    _node_max_steps = _node_extra.get("max_steps")
    if _node_max_steps is not None:
        try:
            _nms = int(_node_max_steps)
            if 1 <= _nms <= 200:
                max_steps = _nms
        except (TypeError, ValueError):
            logger.warning("node %s has invalid max_steps override: %r", node.id, _node_max_steps)

    # ---- 收集附件 ----
    collected_attachments: list[dict[str, Any]] = []
    _tool_produced_attachments: list[dict[str, Any]] = []
    if attachments:
        collected_attachments.extend(attachments)
    if resume_data and isinstance(resume_data, dict):
        for e in (resume_data.get("tool_results") or resume_data.get("entries") or []):
            collected_attachments.extend(payload_attachments(e))
        if isinstance(resume_data.get("attachments"), list):
            collected_attachments.extend(resume_data["attachments"])
        collected_attachments.extend(payload_attachments(resume_data.get("result")))

    # ---- 恢复或新建消息历史 ----
    step_count = 0
    _is_block_mode = False
    system_prompt: list[dict[str, Any]] = []
    # Phase 3 Hook System：初始组装只生成不含知识注入的 prompt 骨架。
    # Why: inference core should know only the hook point, not concrete knowledge
    # injection handlers. How: always fire before_prompt_build after fresh assembly
    # and let registered handlers rebuild messages in place. Purpose: keep prompt
    # ownership behind hooks while preserving the final prompt layout.
    _assembled_fresh = False
    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if snapshot and isinstance(snapshot.get("messages"), list):
        messages = list(snapshot.get("messages") or [])
        try:
            step_count = int(snapshot.get("step_count") or 0)
        except Exception:
            step_count = 0
    else:
        messages, _is_block_mode, system_prompt = assemble_initial_messages(
            workspace_root=rctx.workspace_root,
            runtime_cfg=runtime_cfg,
            node=node,
            instruction=instruction,
            history=history,
            task_context=rctx.task_context,
            session_id=rctx.session_id,
            attachments=attachments,
        )
        _assembled_fresh = True

    if _assembled_fresh:
        # Phase 3 Hook System：初始 messages 完成后始终触发 before_prompt_build。
        # Why: the skeleton returned above intentionally contains no skill or
        # memory blocks, so the hook is the single place that may add them. How:
        # pass the rendered system prompt, history, instruction, and attachments
        # through HookContext.extra and request an in-place rebuild. Purpose: keep
        # zero behavior drift while removing knowledge injection from inference.
        _prompt_ctx = HookContext(
            messages=messages,
            tools=[],
            node=node,
            provider=provider,
            rctx=rctx,
            step=step_count,
            extra={
                "runtime_cfg": runtime_cfg,
                "instruction_text": instruction,
                "history": history,
                "attachments": attachments,
                "system_prompt": system_prompt,
                # Why: the initial message list is now only a prompt skeleton.
                # How: always request hook-side rebuild when before_prompt_build
                # runs. Purpose: ensure knowledge injection is handled solely by
                # hook handlers, not by the inference loop.
                "apply_injection": True,
            },
        )
        _prompt_result = await hook_registry.afire("before_prompt_build", _prompt_ctx)
        if _prompt_result.action is not None:
            return _prompt_result.action
        _is_block_mode = bool(_prompt_ctx.extra.get("is_block_mode", _is_block_mode))

    # ---- 追加恢复消息 ----
    if resume_data:
        resumed_messages = _build_resume_messages(resume_data)
        for message in resumed_messages:
            message.setdefault("_meta", {}).update({**route_meta, "source_task_id": rctx.task_id})
        messages.extend(resumed_messages)
        if str(resume_data.get("type") or "") == "compact_done":
            # Phase 2 Signal: compact.done 信号，通过 SignalBus 发射供监控使用
            _cd_payload = {
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            }
            # task 粒度信息（ConvStore 路径产生）
            for _k in ("total_segments", "kept_segments", "compressed_segments"):
                if _k in resume_data:
                    _cd_payload[_k] = resume_data[_k]
            get_bus().emit(Signal(name="compact.done", payload=_cd_payload))
            await rctx.emit_event("compact_done", _cd_payload)

    if instruction:
        for message in reversed(messages):
            if message.get("role") == "user" and not message.get("_dynamic"):
                content = message.get("content")
                if content == instruction or isinstance(content, list):
                    message.setdefault("_meta", {}).update({**route_meta, "source_task_id": rctx.task_id})
                    break

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    # 跑不通的生图工具（gpt_image_2 / gemini_image）从可用工具列表里彻底移除，
    # 杜绝「未配置却被误调用、调完 404」。
    try:
        from ..builtin.image_gen_gating import disabled_image_tools, IMAGE_GEN_TOOLS
        _disabled_img = disabled_image_tools(rctx.workspace_root)
        if _disabled_img:
            _before = {s.get("name") for s in tool_specs}
            tool_specs = [s for s in tool_specs if s.get("name") not in _disabled_img]
            _removed = _before & _disabled_img
            if _removed:
                logger.info("image_gen_gating: removed unconfigured image tools %s (node=%s)", sorted(_removed), node.id)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("image_gen_gating tool filter skipped: %s", _exc)
    _allowed_real_tools = {s.get("name") for s in tool_specs if s.get("name")}
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    delegate_targets = list(node.delegate_targets)
    # 一张图都画不出来的绘图节点不参与委派：留着只会让模型认真规划一轮，再撞上工具报缺 key。
    try:
        from ..builtin.image_gen_gating import disabled_draw_nodes
        _disabled_nodes = disabled_draw_nodes(rctx.workspace_root) & set(delegate_targets)
        if _disabled_nodes:
            delegate_targets = [t for t in delegate_targets if t not in _disabled_nodes]
            logger.info("image_gen_gating: removed delegate targets %s (no channel configured, node=%s)", sorted(_disabled_nodes), node.id)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("image_gen_gating delegate filter skipped: %s", _exc)
    if delegate_targets:
        # [2026-05-04] Register one dynamic dispatch tool per delegate target.
        # Why: target selection should happen through tool choice, not through an
        # aggregate dispatch schema. How: expand node.delegate_targets into only
        # dispatch:{target_id} specs. Purpose: keep dynamic dispatch intact while
        # removing the old aggregate dispatch tools from the model-visible list.
        openai_tools.extend(_dispatch_delegate_specs(delegate_targets, downstream_info))

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
    _is_system_task = bool((rctx.task_context or {}).get("is_system_task"))
    if not _is_system_task:
        _sw_targets = [info["id"] for info in (switch_info or [])]
        openai_tools.append(_switch_node_spec(_sw_targets, switch_info, current_node_id=node.id, current_node_name=node.name))

    openai_tools.append(_finish_spec())
    # [AutoC 2026-05-31] Why: ask must be injected through the same provider tool
    # list path as finish, including native/json formatter adaptation. How: append
    # the ask schema immediately beside finish. Purpose: every node sees both
    # terminal exits before workflow topology routing is implemented.
    openai_tools.append(_ask_spec())
    openai_tools.append(_reply_spec())
    openai_tools.append(_compact_context_spec())
    openai_tools.append(_preempt_task_spec())

    if short_reply(rctx.task_context):
        max_steps = min(max_steps, 2)
        _allowed_real_tools = set()
        delegate_targets = []
        openai_tools = [_finish_spec()]
        messages.append({
            "role": "system",
            "content": "本轮只接一句自然的简短回应，最多240字。无需工具或委派，直接用finish给出回复；不展开任务、不连续发消息。",
        })

    # ---- 工具定义注入（formatter 统一处理 native/json 差异）----
    formatter = create_tool_formatter(node.tool_mode)
    if openai_tools:
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"], _api_tools = formatter.inject_tool_definitions(
                    openai_tools, msg.get("content", ""),
                )
                openai_tools = _api_tools or []
                break

    # ---- 构造循环状态 ----
    ls = _LoopState(
        rctx=rctx,
        node=node,
        provider=provider,
        registry=registry,
        run_id=run_id,
        context_ref=context_ref,
        runtime_cfg=runtime_cfg,
        streaming=streaming,
        messages=messages,
        system_prompt=system_prompt,
        is_block_mode=_is_block_mode,
        openai_tools=openai_tools,
        history=history,
        collected_attachments=collected_attachments,
        tool_produced_attachments=_tool_produced_attachments,
        formatter=formatter,
        allowed_real_tools=_allowed_real_tools,
        allowed_dispatch_targets=set(delegate_targets),
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
        compact_keep_recent=get_int(runtime_cfg, "engine.compact.keep_recent", 6, min_value=2, max_value=50),
        compact_hard_threshold=_resolve_compact_hard_threshold(runtime_cfg),
        compact_keep_recent_tokens=_resolve_compact_keep_recent_tokens(runtime_cfg),
        compacted=False,
        last_prompt_tokens=None,
        # [fix 2026-07-17] retry_max 同样支持节点级覆盖，让系统节点可以把 LLM 重试
        # （含 empty_response）次数压低，更快进入 fallback / 失败。
        retry_max=_resolve_node_int(node, "retry_max",
                                    get_int(runtime_cfg, "engine.retry.max_retries", 3, min_value=0, max_value=10),
                                    0, 10),
        retry_initial_delay=get_float(runtime_cfg, "engine.retry.initial_delay_sec", 1.0, min_value=0.1, max_value=60.0),
        retry_max_delay=get_float(runtime_cfg, "engine.retry.max_delay_sec", 30.0, min_value=1.0, max_value=300.0),
        retry_backoff=get_float(runtime_cfg, "engine.retry.backoff_multiplier", 2.0, min_value=1.0, max_value=10.0),
        plaintext_retry_count=0,
        # 改动：plaintext retry 默认值从 2 → 3，与 retry_max（LLM 报错重试）对齐，
        # 给模型更多机会自行修正未调 finish 的问题。
        plaintext_retry_max=get_int(runtime_cfg, "engine.plaintext_retry_max", 3, min_value=0, max_value=10),
        preempt_after_step=False,
        preempt_inject_info=None,
        use_stream=streaming,
        resume_data=resume_data if isinstance(resume_data, dict) else None,
    )

    # ---- 推理循环 ----
    for step in range(step_count, max_steps):
        # Phase 3 Hook System：循环顶部统一触发 before_step。
        # 原因：取消、preempt 注入、microcompact、proactive snip 和自动压缩都属于
        # prompt 生成前的可插拔检查。做法：把完整 loop state 放入 HookContext.extra，
        # 由 PreemptChecker 与 CompactChecker 按优先级执行。目的：减少 ai_step.py
        # 中的硬编码控制流，同时保持旧行为顺序不变。
        _step_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            extra={"loop_state": ls, "step_count": step_count},
        )
        _step_result = await hook_registry.afire("before_step", _step_ctx)
        if _step_result.action is not None:
            return _step_result.action
        if _step_result.skip_step:
            continue

        # LEGACY: replaced by hook PreemptChecker and CompactChecker.
        # Why: inference core should only keep the before_step trigger point for
        # preempt and compact behavior. How: the old direct preempt injection path
        # was removed, while compact fallback helpers remain unused for comparison.
        # Purpose: avoid knowledge injection imports in this loop while keeping the
        # migrated execution order visible.

        result = await _call_llm_with_retry(ls, step)
        if isinstance(result, TaskAction):
            return result
        # ---------------------------------------------------------------
        # Preempt V3 需求1: _call_llm_with_retry 返回 None 表示流式输出
        # 在思考阶段被 preempt 截断。partial assistant message 已丢弃（不存
        # 历史），preempt 消息已存储在 ls.preempt_inject_info 中。
        # 跳到下一轮循环顶部，由 PreemptChecker 注入新用户消息后
        # 重新推理。与 cancel 的区别：不终止 task，继续循环。
        # ---------------------------------------------------------------
        if result is None:
            continue
        resp = result

        # P0 Task 内核化：记录实际完成的步数
        ls.rctx.completed_steps = step + 1

        # Phase 3 Hook System：LLM 调用后的 usage 统计交给 UsageTracker。
        # 原因：token 累加是 after_llm_call 的典型横切逻辑。做法：传入响应和
        # loop state，由 handler 更新 rctx.total_usage。目的：保持 TaskRecord
        # 用量统计不变，同时从 ai_step.py 中抽出 bookkeeping。
        _usage_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _usage_result = await hook_registry.afire("after_llm_call", _usage_ctx)
        if _usage_result.action is not None:
            return _usage_result.action
        # [2026-05-24] Allow after_llm_call hooks (e.g. fallback_provider) to
        # replace the response. Without this, a hook that sets ctx.response to
        # a successful fallback result would be ignored because the local `resp`
        # variable still points to the original failed response.
        if _usage_ctx.response is not resp:
            resp = _usage_ctx.response

        if not resp.ok:
            return _build_failure_action(ls, resp, step)

        # ---- 从文本中解析工具调用（formatter 统一处理）----
        if not resp.tool_calls:
            _parsed = formatter.parse_tool_calls(resp)
            if _parsed:
                _clean_text = formatter.get_plain_text(resp)
                # 只换正文和工具调用，finish_reason 这类字段得留着：后面还要据此判断有没有被截断
                resp = with_changes(
                    resp,
                    text=_clean_text,
                    tool_calls=[
                        ToolCall(id=p.id, name=p.name, arguments=p.arguments)
                        for p in _parsed
                    ],
                )
                # [stream-clean 2026-05-31] Why: JSON tool mode mixes protocol
                # markers (<<<TOOL_CALL>>>) into stream_delta output. The markers
                # are needed for parse_tool_calls but should not remain visible
                # in the frontend. How: after parsing, emit the cleaned plain text
                # so the frontend can replace dirty stream blocks. Purpose: the
                # user sees only the final authoritative text, not raw protocol.
                if _clean_text and _clean_text.strip() and ls.use_stream:
                    await ls.rctx.emit_event("stream_text_final", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
                        "text": _clean_text,
                    })
            else:
                # 解析失败也得把协议标记剥掉。hybrid 会把剩下的正文当隐式 finish 发出去，
                # 不剥就是把 <<<TOOL_CALL>>> 连同脚本原样甩给用户 —— 越出错暴露得越多。
                _stripped = formatter.get_plain_text(resp)
                if (_stripped or "") != (resp.text or ""):
                    logger.error(
                        "tool call parse failed; stripped protocol markers from plain text (node=%s)",
                        ls.node.id,
                    )
                    resp = with_changes(resp, text=_stripped)

        if resp.tool_calls:
            action = await _handle_tool_calls(ls, resp, step)
            if action is not None:
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            if is_truncated(resp):
                # 截断点之后的调用已经丢了，告诉模型这轮没说完
                _append_truncation_hint(ls)
            continue

        if is_truncated(resp):
            action = _handle_truncated_response(ls, resp, step)
            if action is not None:
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            continue

        # Phase 3 Hook System：纯文本响应交给 PlaintextRetryHandler。
        # 原因：hybrid 隐式 finish 与 tool_only 重试是 before_response 决策。
        # 做法：handler 根据 output_mode 返回 TaskAction 或追加 retry hint。
        # 目的：保留原行为，同时让响应策略可注册。
        _plaintext_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _plaintext_result = await hook_registry.afire("before_response", _plaintext_ctx)
        if _plaintext_result.action is not None:
            return await _fire_task_end_hook_if_finish(ls, _plaintext_result.action, step + 1)

        # LEGACY: replaced by hook PlaintextRetryHandler.
        # action = _handle_plaintext_response(ls, resp, step)
        # if action is not None:
        #     return action

    # ---- 达到最大步数 ----
    # Phase 3 Hook System：max_steps 是任务错误结束路径，先交给 on_task_error
    # 保存上下文。原因：ContextSnapshotSaver 应成为后续终止路径的统一入口；
    # 做法：传入正确 step_count=max_steps，并从 ctx.extra 读取 context_ref。
    # 目的：先安全覆盖此处单一错误路径，其他复杂终止路径保留旧逻辑。
    _error_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=max_steps,
        extra={"loop_state": ls, "step_count": max_steps},
    )
    _error_result = await hook_registry.afire("on_task_error", _error_ctx)
    if _error_result.action is not None:
        return _error_result.action
    ctx_ref = str(_error_ctx.extra.get("context_ref") or "")
    if not _error_ctx.extra.get("snapshot_saved"):
        # LEGACY fallback: replaced by hook ContextSnapshotSaver for max_steps.
        ctx_ref = _persist_ctx(ls, max_steps)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
