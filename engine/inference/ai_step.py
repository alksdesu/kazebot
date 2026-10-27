from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from collections import Counter
from copy import deepcopy
from functools import partial, wraps
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from toolbox.approval_wait import relay_approval_waits, report_approval_waits_to
from toolbox.builtins.execute_command import command_timeout
from toolbox.registry import ToolRegistry
from toolbox.tool_names import is_pseudo_tool_name

from .pseudo_tools import (
    NO_REPLY_MARKER,
    _dispatch_control_specs,
    _dispatch_delegate_specs,
    _finish_spec,
    _ask_spec,
    _reply_spec,
    _compact_context_spec,
    _preempt_task_spec,
    _stay_silent_spec,
    _switch_node_spec,
    _update_todos_spec,
    _to_openai_tools,
    _filter_tool_specs,
)
from .resume_builder import _build_resume_messages
from .loop_state import (
    TERMINAL_TOOLS,
    TOOL_CONTEXT_EXTRA_ATTRS,
    TOOL_RESULT_ATTACHMENT_TYPE,
    _LoopState,
    _persist_ctx,
    _reasoning_fields,
    _short,
    build_tool_context,
    compact_target_session_id,
    context_archive_session_id,
    conversation_writer,
    flush_conversation_writes,
)
from .llm_call import _call_llm_with_retry, _build_failure_action
from .llm_outcome import is_truncated, with_changes
from .async_delivery import async_result_tool_status, build_async_result_payload, deliver_async_result
from .pseudo_handlers import (
    WAIT_ABORTED,
    WAIT_CANCELLED,
    WAIT_DISPATCHED,
    WAIT_INTERCEPTED,
    WAIT_PREEMPTED,
    AwaitDispatch,
    WaitDispatch,
    _handle_pseudo_tool,
    is_await_dispatch,
    is_wait_dispatch,
    prepare_await_dispatches,
    prepare_wait_dispatches,
)

from ..context_store import load_context_snapshot
from ..dispatch_batch import AWAIT_KEY, DispatchTimeouts
from ..attachments import build_multimodal_content, payload_attachments, screen_attachments
from ..conversation_store import Message, MessageType
from ..node import Node
from .message_assembly import assemble_initial_messages
from ..conversation_routing import concise_text, render_user_input, routing_meta, short_reply, turn_input_meta
from ..protocol import (
    TaskAction,
    ACTION_DISPATCH,
    ACTION_FINISH,
    ACTION_ASK,
    ACTION_FAIL,
    ACTION_CANCELLED,
    ACTION_PREEMPTED,
)
from ..tool_step import TOOL_CANCELLED, TOOL_ERROR, result_to_raw, summarize_result, tool_outcome
from ..tool_outputs import (
    READ_TOOL_OUTPUT,
    bounded_for_event,
    inline_tool_output,
    tool_output_limits,
    tool_result_for_event,
)
from ..todo_list import carried_todo_items, latest_todos_from_messages, unfinished_items, unfinished_summary
from ..run_budget import (
    STEP_LIMIT_FLAG,
    STEP_LIMIT_NOTICE,
    TIME_LIMIT_FLAG,
    TIME_LIMIT_NOTICE,
    WRAPUP_KEY,
    WRAPUP_STEPS,
    WRAPUP_TIME,
    RunBudget,
)
from clonoth_runtime import get_int, get_float, get_bool, load_runtime_config, parse_bool
from toolbox.context import ToolContext
# build_llm_messages: 反序列化方向的格式转换，在 llm_call.py 中实际调用。
# 此处导入供外部通过 ai_step 模块访问（如测试、调试）。
from .tool_format import (
    ParsedToolCall,
    create_tool_formatter,
    build_llm_messages,
    invalid_arguments,
)
from .message_model import MessageMeta, apply_reasoning_content, set_message_meta
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


TOOL_BLOCKED = "blocked"
TOOL_ASYNC_STARTED = "async_started"

_SUPERSEDED_TERMINAL_MESSAGE = "{tool} 没有执行：同一轮调用了多个 finish/ask，只执行最后一个。"
_WAIT_TERMINAL_MESSAGE = (
    "{tool} 没有执行：同一轮发出了等结果的委派（wait=true），它们的结果在下一条消息回来；看完结果再单独调用 {tool}。"
)
_AWAIT_TERMINAL_MESSAGE = (
    "{tool} 没有执行：同一轮调用了 await_dispatches，等到的结果在下一条消息回来；看完结果再单独调用 {tool}。"
)
_FINAL_STEP_REJECT_MESSAGE = "{tool} 没有执行：这是本任务允许的最后一步，只能调用 finish。"
_FINAL_STEP_HINT = (
    "[SYSTEM] 这是本任务允许的最后一步（已用 {used}/{total} 步）。不要再调用其它工具，直接调用 finish："
    "说明已经完成的工作、没完成的部分和原因、建议的下一步。"
)
_TIME_LIMIT_HINT = (
    "[SYSTEM] 本任务这一段的运行时间快到上限了（已运行约 {minutes} 分钟），这是最后一步。不要再调用其它工具，直接调用 finish："
    "说明已经完成的工作、没完成的部分和原因、建议的下一步。"
)
# 到点收尾可能连着几步都是最后一步（抢占、空回复后重来），提示只留最新的一条
_FINAL_STEP_HINT_KEY = "_final_step_hint"
# 第一步调了别的工具被拒，还剩一步改用 finish
_SHORT_REPLY_MAX_STEPS = 2
_SHORT_REPLY_REJECT_MESSAGE = "{tool} 没有执行：本轮是简短回应，不调用工具，要回应就直接用 finish 给出那一句。"


def _append_tool_error(ls: _LoopState, tool_call: Any, text: str, *, tool_status: str = TOOL_BLOCKED) -> None:
    """给一个没有执行的工具调用补上错误结果，保持调用与结果的配对。"""
    message = ls.formatter.format_tool_result(tool_call, text)
    # 结果带上当前 tool_mode，真 native 的 role=tool 消息下一轮才会被原样透传
    set_message_meta(message, MessageMeta(
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="tool_result",
        # 执行状态只对真工具有意义，伪工具的结果不记
        tool_status="" if is_pseudo_tool_name(str(getattr(tool_call, "name", "") or "")) else tool_status,
    ))
    ls.messages.append(message)
    _shadow_write(ls, message, message_type=MessageType.TOOL_RESULT)


def _flush_tool_errors(ls: _LoopState, pending: list[tuple[Any, str]]) -> None:
    """按顺序写完排着的错误结果；写过的从列表里拿掉，重复调用不会写两遍。"""
    while pending:
        tool_call, text = pending.pop(0)
        _append_tool_error(ls, tool_call, text)


def _append_attachment_message(ls: _LoopState, message: dict[str, Any]) -> None:
    ls.messages.append(message)
    _shadow_write(ls, message, message_type=TOOL_RESULT_ATTACHMENT_TYPE)


def _flush_round_tail(
    ls: _LoopState,
    tool_calls: list,
    late_errors: list[tuple[Any, str]],
    wait_pending: list[WaitDispatch | AwaitDispatch],
    outcome: str,
    attachment_messages: list[dict[str, Any]],
) -> bool:
    """补本轮最后的结果：排在后面的不执行调用，加上按 outcome 写的 wait 委派和 await_dispatches 结果，按调用原顺序写，最后才写工具产出图片的消息。

    图片消息是 user 消息，夹在工具结果中间会被配对修复当成新一轮的开始，排在它后面的结果就成了孤儿。
    三个列表写完就清空，重复调用不会写两遍；返回这次有没有写东西。
    """
    tail = list(late_errors) + [(item.call, item.result_text(outcome)) for item in wait_pending]
    late_errors.clear()
    wait_pending.clear()
    deferred = list(attachment_messages)
    attachment_messages.clear()
    if tail:
        position = {id(call): index for index, call in enumerate(tool_calls)}
        tail.sort(key=lambda item: position.get(id(item[0]), len(tool_calls)))
        _flush_tool_errors(ls, tail)
    for message in deferred:
        _append_attachment_message(ls, message)
    return bool(tail or deferred)


async def _dispatch_wait_batch(ls: _LoopState, items: list[WaitDispatch], step: int) -> TaskAction:
    """本轮的 wait 委派交给 supervisor 成批派出：本任务挂起，子任务全部结束后带着结果恢复。"""
    targets = "、".join(item.target for item in items)
    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] 等待 {len(items)} 个子节点：{targets}",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })
    # 恢复时历史从会话里重新读，挂起前占位结果必须已经落盘
    await flush_conversation_writes(ls.rctx)
    return TaskAction(
        action=ACTION_DISPATCH,
        node_id=ls.node.id,
        dispatch_batch=[dict(item.item) for item in items],
        context_ref=_persist_ctx(ls, step + 1),
        summary=_short(f"等待 {len(items)} 个子节点：{targets}", 240),
    )


async def _dispatch_await(ls: _LoopState, item: AwaitDispatch, step: int) -> TaskAction:
    """await_dispatches 交给 supervisor：本任务挂起，等到点名的异步委派结束（或到点）后带着结果恢复。"""
    count = len(item.request.handles)
    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] 等待 {count} 个异步委派",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })
    # 恢复时历史从会话里重新读，挂起前占位结果必须已经落盘
    await flush_conversation_writes(ls.rctx)
    return TaskAction(
        action=ACTION_DISPATCH,
        node_id=ls.node.id,
        dispatch_input={AWAIT_KEY: item.request.payload()},
        context_ref=_persist_ctx(ls, step + 1),
        summary=_short(f"等待 {count} 个异步委派", 240),
    )


def _rejected_terminal_calls(hook_ctx: HookContext, pseudo_calls: list) -> list[tuple[Any, str]]:
    """round 级 hook 放进 extra["rejected_terminal_calls"] 的终止调用，只认本轮伪工具里的 finish/ask。"""
    pending = {id(call): call for call in pseudo_calls if call.name in TERMINAL_TOOLS}
    rejected: list[tuple[Any, str]] = []
    for item in hook_ctx.extra.get("rejected_terminal_calls") or []:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        call, text = item
        target = pending.pop(id(call), None)
        if target is None:
            call_id = str(getattr(call, "id", "") or "")
            key = next((key for key, known in pending.items() if call_id and known.id == call_id), None)
            target = pending.pop(key, None) if key is not None else None
        if target is not None:
            rejected.append((target, str(text or "") or f"{target.name} 没有执行。"))
    return rejected


def _split_by_position(
    tool_calls: list, skipped: list[tuple[Any, str]], executed_ids: set[int],
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]]]:
    """不执行的调用里，排在本轮第一个会执行的调用之前的现在就写结果，其余等前面的结果写完再补，尽量贴着原顺序。"""
    position = {id(call): index for index, call in enumerate(tool_calls)}
    first_executed = min((position[key] for key in executed_ids if key in position), default=len(tool_calls))
    ordered = sorted(skipped, key=lambda item: position.get(id(item[0]), len(tool_calls)))
    early = [item for item in ordered if position.get(id(item[0]), len(tool_calls)) < first_executed]
    late = [item for item in ordered if position.get(id(item[0]), len(tool_calls)) >= first_executed]
    return early, late


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


async def _handle_tool_calls(
    ls: _LoopState, resp, step: int, *, final_step: bool = False, more_steps: bool = False,
) -> TaskAction | None:
    """处理 tool_calls（伪工具 + 真工具）。

    返回 TaskAction 则退出循环；返回 None 则 continue 到下一轮。
    final_step 为真时是本任务的最后一步：finish/ask 之外的调用都不执行，只补错误结果。
    more_steps 为真时这一步之后还有下一步：短答里只调了被拒工具的这一轮不当场收尾，留给下一步改用 finish。
    """
    ls.last_round_blocked = False
    _short_reply_turn = short_reply(getattr(ls.rctx, "task_context", {}) or {})
    # 【方案 A 重构】伪工具改为列表按序执行，不再是 last-wins
    # 原本单标量 `pseudo_call = tc` 会导致同轮多伪工具只有最后一个生效，
    # 在 Fix 2（JSON 自由正文反向包装为 reply）后尤其危险——
    # 例如“委派工具 + 自由正文”会被吞掉委派，只留 reply。
    # 现改为按 LLM 输出顺序收集所有伪工具，后续按序处理，
    # 遇到返回 TaskAction 的（finish / switch_node / dispatch 等）立即退出循环，
    # 返回 None 的（reply / compact_context / preempt_task）继续执行下一个。
    pseudo_calls: list = []
    real_tool_calls: list[dict[str, Any]] = []
    _real_call_ids: set[int] = set()
    _unauthorized_names: list[str] = []
    # 不执行的调用等 assistant 消息进了历史再补错误结果：写在它前面的结果会被配对修复当孤儿删掉
    _rejected_calls: list[tuple[Any, str, str]] = []
    for tc in resp.tool_calls:
        _raw_arguments = invalid_arguments(tc.arguments)
        if _raw_arguments is not None:
            logger.warning("node %s tool call %s carried non-JSON arguments; not executed", ls.node.id, tc.name)
            _rejected_calls.append((tc, _invalid_arguments_error(tc.name, _raw_arguments), TOOL_ERROR))
            continue
        if final_step and tc.name not in TERMINAL_TOOLS:
            _rejected_calls.append((tc, _FINAL_STEP_REJECT_MESSAGE.format(tool=tc.name), TOOL_BLOCKED))
            continue
        # dispatch:{target} 这类按委派目标生成的名字也是伪工具，不走真工具授权
        if is_pseudo_tool_name(tc.name):
            pseudo_calls.append(tc)
        elif tc.name not in ls.allowed_real_tools:
            _unauthorized_names.append(tc.name)
            if _short_reply_turn:
                # 短答的工具表和普通任务一样完整，模型看得见这个工具：得说清是这一轮不能用，不是节点没有
                logger.info("node %s refused tool call %s in a short reply", ls.node.id, tc.name)
                _reject_text = _SHORT_REPLY_REJECT_MESSAGE.format(tool=tc.name)
            else:
                logger.warning("node %s attempted unauthorized tool call: %s (allowed: %s)",
                               ls.node.id, tc.name, ls.allowed_real_tools)
                _reject_text = (
                    f"Error: Tool '{tc.name}' is not in this node's allowed tool list. "
                    f"Use finish() to provide your output directly."
                )
            _rejected_calls.append((tc, _reject_text, TOOL_BLOCKED))
        else:
            real_tool_calls.append({
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            })
            _real_call_ids.add(id(tc))
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
        inline_data=resp.inline_data or [],
        usage=dict(ls.last_usage) if ls.last_usage else {},
        **_reasoning_fields(ls, resp),
    )
    set_message_meta(_assistant_msg, _tc_meta)
    apply_reasoning_content(_assistant_msg)
    ls.messages.append(_assistant_msg)
    # 含 finish 的 assistant 消息原样落盘：拆掉它后面的 tool_result 就没了配对来源
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)
    for _rejected_call, _rejected_error, _rejected_status in _rejected_calls:
        _append_tool_error(ls, _rejected_call, _rejected_error, tool_status=_rejected_status)

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
        extra={
            "pseudo_calls": pseudo_calls,
            "real_tool_calls": real_tool_calls,
            "loop_state": ls,
            # doom_loop 从这条往前数历史轮次
            "assistant_message": _assistant_msg,
        },
    )
    _before_result = await hook_registry.afire("before_tool_call", _before_ctx)
    if _before_result.action is not None:
        return _before_result.action
    # finish_guard 不拦整轮时，把和其它工具同轮的 finish/ask 交回这里，只拒它们
    _rejected_terminals = _rejected_terminal_calls(_before_ctx, pseudo_calls)
    if _before_result.block or _before_result.skip_step:
        ls.last_round_blocked = True
        _default_msg = (
            "Tool call blocked by before_tool_call hook."
            if _before_result.block else "Tool call skipped by before_tool_call hook."
        )
        _reject_msg = _before_result.error_message or _before_result.reason or _default_msg
        # 拒绝结果按普通工具结果落盘：标成 ephemeral 的话下一轮 provider 会看到 tool_call 没有回复
        _answered = {id(_call) for _call, *_ in _rejected_calls}
        _terminal_msgs = {id(_call): _text for _call, _text in _rejected_terminals}
        for tc in resp.tool_calls:
            if id(tc) not in _answered:
                _append_tool_error(ls, tc, _terminal_msgs.get(id(tc), _reject_msg))
        return None  # 不执行任何工具，回到主循环让 AI 重试

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
    if (
        _unauthorized_names
        and not real_tool_calls
        and not pseudo_calls
        and not ls.allowed_real_tools
        # 短答清空的只是执行许可，模型看到的工具表是完整的：还有下一步就让它看着拒绝结果改用 finish
        and not (_short_reply_turn and more_steps)
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
    # wait=true 的委派不当场派出：本轮其它调用跑完后成批交给 supervisor，本任务挂起等结果
    _wait_calls = [_pc for _pc in pseudo_calls if is_wait_dispatch(_pc)]
    _wait_plan = await prepare_wait_dispatches(ls, _wait_calls) if _wait_calls else None
    _wait_items = list(_wait_plan.items) if _wait_plan is not None else []
    _wait_rejected = list(_wait_plan.rejected) if _wait_plan is not None else []
    if _wait_calls:
        _wait_call_ids = {id(_pc) for _pc in _wait_calls}
        pseudo_calls = [_pc for _pc in pseudo_calls if id(_pc) not in _wait_call_ids]
    # await_dispatches 也等本轮其它调用跑完再挂起；和 wait 委派同轮时不开始等
    _await_calls = [_pc for _pc in pseudo_calls if is_await_dispatch(_pc)]
    _await_plan = (
        await prepare_await_dispatches(ls, _await_calls, has_wait_items=bool(_wait_items)) if _await_calls else None
    )
    _await_item = _await_plan.item if _await_plan is not None else None
    if _await_plan is not None:
        _wait_rejected += _await_plan.rejected
    if _await_calls:
        _await_call_ids = {id(_pc) for _pc in _await_calls}
        pseudo_calls = [_pc for _pc in pseudo_calls if id(_pc) not in _await_call_ids]
    if _wait_items or _await_item is not None:
        # 结果下一条消息才回来，同轮的 finish/ask 不能先把任务结束掉
        _terminal_message = _WAIT_TERMINAL_MESSAGE if _wait_items else _AWAIT_TERMINAL_MESSAGE
        _already_rejected = {id(_call) for _call, _ in _rejected_terminals}
        _rejected_terminals = _rejected_terminals + [
            (_pc, _terminal_message.format(tool=_pc.name))
            for _pc in pseudo_calls
            if _pc.name in TERMINAL_TOOLS and id(_pc) not in _already_rejected
        ]
    _rejected_ids = {id(_call) for _call, _ in _rejected_terminals}
    pseudo_calls = [_pc for _pc in pseudo_calls if id(_pc) not in _rejected_ids]
    _terminal_calls = [_pc for _pc in pseudo_calls if _pc.name in TERMINAL_TOOLS]
    _terminal_call = _terminal_calls[-1] if _terminal_calls else None
    _skipped_terminals = _rejected_terminals + [
        (_pc, _SUPERSEDED_TERMINAL_MESSAGE.format(tool=_pc.name)) for _pc in _terminal_calls[:-1]
    ]
    _executed_ids = _real_call_ids | {id(_pc) for _pc in pseudo_calls if _pc.name not in TERMINAL_TOOLS}
    if _terminal_call is not None:
        _executed_ids.add(id(_terminal_call))
    # wait 委派的结果在本轮最后才补：排位时把它们当成会执行的调用，排在后面的不执行调用跟着留到最后，顺序才对得上
    _wait_pending: list[WaitDispatch | AwaitDispatch] = [*_wait_items, *([_await_item] if _await_item else [])]
    _anchor_ids = _executed_ids | {id(_item.call) for _item in _wait_pending}
    _early_errors, _late_errors = _split_by_position(
        resp.tool_calls, _skipped_terminals + _wait_rejected, _anchor_ids,
    )
    _flush_tool_errors(ls, _early_errors)
    _wait_outcome = WAIT_ABORTED
    _attachment_messages: list[dict[str, Any]] = []
    _tail_written = False

    try:
        for _pc in pseudo_calls:
            if _pc.name in TERMINAL_TOOLS:
                continue  # finish/ask 延后执行
            action = await _handle_pseudo_tool(ls, _pc, step)
            if action is not None:
                # 其他终止型伪工具（如 switch_node）仍然立刻退出。
                return action
            # 非终止型（reply / compact_context / preempt_task）继续

        # 处理真实工具
        if real_tool_calls:
            action = await _execute_real_tools(ls, real_tool_calls, step, attachment_messages=_attachment_messages)
            if action is not None:
                return action
            ls.use_stream = ls.streaming

            if ls.preempt_after_step:
                _flush_round_tail(
                    ls, resp.tool_calls, _late_errors, _wait_pending, WAIT_PREEMPTED, _attachment_messages,
                )
                ctx_ref = _persist_ctx(ls, step + 1)
                return TaskAction(
                    action=ACTION_PREEMPTED, node_id=ls.node.id,
                    context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
                )

        if _wait_pending:
            # 一挂起就要等子任务全部结束，派出前最后看一眼取消和抢占
            _cancelled = await _check_preempt_and_cancel(ls, step)
            if _cancelled is not None:
                _flush_round_tail(
                    ls, resp.tool_calls, _late_errors, _wait_pending, WAIT_CANCELLED, _attachment_messages,
                )
                return _cancelled
            if ls.preempt_after_step:
                _flush_round_tail(
                    ls, resp.tool_calls, _late_errors, _wait_pending, WAIT_PREEMPTED, _attachment_messages,
                )
                return TaskAction(
                    action=ACTION_PREEMPTED, node_id=ls.node.id,
                    context_ref=_persist_ctx(ls, step + 1), summary="任务被软打断，上下文已保存。",
                )
            # 用户刚发来新消息：不挂起，下一步先把新消息注入，和拦截 finish 一样
            _wait_outcome = WAIT_INTERCEPTED if ls.preempt_inject_info is not None else WAIT_DISPATCHED
    finally:
        # 提前退出或抛错也要给不执行的终止调用、没派出的 wait 委派补上结果
        _tail_written = _flush_round_tail(
            ls, resp.tool_calls, _late_errors, _wait_pending, _wait_outcome, _attachment_messages,
        )

    if _tail_written:
        # 补在最后的结果和图片消息也要在下一次推理前落盘
        await flush_conversation_writes(ls.rctx)

    if _wait_items:
        ls.use_stream = ls.streaming
        if _wait_outcome == WAIT_DISPATCHED:
            return await _dispatch_wait_batch(ls, _wait_items, step)
        return None
    if _await_item is not None:
        ls.use_stream = ls.streaming
        if _wait_outcome == WAIT_DISPATCHED:
            return await _dispatch_await(ls, _await_item, step)
        return None

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
#  两条路都把结果交回 supervisor，由它按发起任务定交给哪个会话、哪个节点；模型当轮先拿到占位结果。
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
    """返回 execute_command 的自适应转异步阈值秒数；不适用时返回 None。

    命令自己的 timeout_sec（和工具里一样夹过上下限）仍是硬上限，引擎只是早一点不再干等：
    取 min(阈值, 时限的 80%)，时限离阈值太近的不转。
    """
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
    timeout_sec = command_timeout(cfg, (tool_args or {}).get("timeout_sec"))
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
# 同步委派的子节点收不了新消息，supervisor 会把结果交给派它出来的节点
_ASYNC_RESULT_ARRIVAL = (
    "Its result will arrive later as a new message, to this node or, "
    "if this node cannot receive messages, to the node that dispatched it."
)


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
    """后台执行声明了 async_mode 的工具，结果交回 supervisor。"""
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


async def _context_tool_text(
    workspace_root: Any, session_id: str, tool_name: str, text: Any, limits: Any,
) -> Any:
    """工具结果写进模型上下文的样子：超长的完整文本落盘，换成带 ref 的预览；事件里的原文不受影响。"""
    if not isinstance(text, str) or len(text) <= limits.max_inline_chars:
        return text
    return await asyncio.to_thread(inline_tool_output, workspace_root, session_id, tool_name, text, limits)


# 这两种恢复数据把独立 tool task 的 raw_inline 原样拼进上下文
_RESUME_TOOL_RESULT_TYPES = frozenset({"tool_results", "batch_results"})


async def _resume_data_for_context(resume_data: dict[str, Any], rctx: Any, runtime_cfg: Any) -> dict[str, Any]:
    """恢复数据里超长的工具结果换成落盘后的预览，只给拼上下文用。

    原数据不改：收附件和循环状态还要用它。
    """
    if str(resume_data.get("type") or "").strip() not in _RESUME_TOOL_RESULT_TYPES:
        return resume_data
    limits = tool_output_limits(runtime_cfg)
    session_id = context_archive_session_id(rctx)
    copied: dict[str, Any] = dict(resume_data)
    changed = False
    for key in ("tool_results", "entries"):
        entries = resume_data.get(key)
        if not isinstance(entries, list):
            continue
        replaced: list[Any] = []
        for entry in entries:
            raw = entry.get("raw_inline") if isinstance(entry, dict) else None
            if (
                not isinstance(raw, str)
                or len(raw) <= limits.max_inline_chars
                or str(entry.get("kind") or "tool") != "tool"
            ):
                replaced.append(entry)
                continue
            preview = await _context_tool_text(
                rctx.workspace_root, session_id, str(entry.get("name") or ""), raw, limits,
            )
            replaced.append({**entry, "raw_inline": preview})
            changed = True
        copied[key] = replaced
    return copied if changed else resume_data


def _record_resumed_messages(ls: _LoopState, messages: list[dict[str, Any]]) -> None:
    """恢复时带回来的结果也写进会话历史：之后再挂起恢复、或者下一轮对话，历史里都还看得到。"""
    for message in messages:
        meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else {}
        _shadow_write(ls, message, message_type=str(meta.get("message_type") or MessageType.USER_INPUT))


def _adopt_resumed_attachments(ls: _LoopState, resume_data: dict[str, Any]) -> None:
    """同步委派回来的子任务附件算本任务产出的：finish 没点名附件时，和工具产出的附件一样随最终回复发出。"""
    if str(resume_data.get("type") or "").strip() != "batch_results":
        return
    found = [
        attachment
        for entry in resume_data.get("entries") or []
        if isinstance(entry, dict)
        for attachment in payload_attachments(entry)
        if isinstance(attachment, dict)
    ]
    if not found:
        return
    known = {str(item.get("path") or "") for item in ls.tool_produced_attachments if isinstance(item, dict)}
    # 自动外发的附件之后不再过策略，进门这一下要把住
    for attachment in screen_attachments(ls.rctx.workspace_root, found):
        path = str(attachment.get("path") or "")
        if path and path not in known:
            known.add(path)
            ls.tool_produced_attachments.append(attachment)


def _async_failure_detail(result: Any, error: str) -> str:
    """失败结果里比那一行报错多出来的内容（超时、取消前已经读到的输出）一起交回；没有多出来的就是报错本身。"""
    data = result.get("data") if isinstance(result, dict) else None
    rendered = str(data.get("result") or "").strip() if isinstance(data, dict) else ""
    if rendered.upper().startswith("ERROR:"):
        rendered = rendered[6:].strip()
    if error and rendered.startswith(error) and len(rendered) > len(error):
        return rendered
    return error


async def _async_result_message(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict,
    async_tool_id: str,
    elapsed: float,
    result: Any,
    error: Exception | None,
    workspace_root: Any,
    session_id: str = "",
) -> tuple[str, str, list[str]]:
    """返回 (回传正文, 失败原因, 附件路径)；失败原因为空表示成功。

    回传正文会注入 session_id 这个会话，超长结果也存在它名下，那边的任务才读得回来。
    """
    limits = tool_output_limits(load_runtime_config(workspace_root) if workspace_root is not None else {})
    # 报错也是整段进回传那一轮的上下文，超长的一样落盘换成带 ref 的预览
    if error is not None:
        failure = str(error) or type(error).__name__
        shown = await _context_tool_text(workspace_root, session_id, tool_name, failure, limits)
        return _async_failure_message(tool_name, async_tool_id, shown), failure, []
    outcome = tool_outcome(result)
    if not outcome.ok:
        detail = _async_failure_detail(result, outcome.error)
        shown = await _context_tool_text(workspace_root, session_id, tool_name, detail, limits)
        return _async_failure_message(tool_name, async_tool_id, shown, elapsed), outcome.error, []
    summary = summarize_result(tool_name, result, args=tool_args)
    _fmt, raw = result_to_raw(tool_name, result, tool_spec=registry.get_spec(tool_name))
    # 只换结果正文，开头的状态行要留着：finish_guard 按它认异步结果
    raw = await _context_tool_text(workspace_root, session_id, tool_name, raw, limits)
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
    """整形、记账并回传一个异步工具结果。

    session_id 是发起任务面向用户的会话（分支任务传父会话），超长结果存在它名下；交给哪个会话由 supervisor 按 task_id 定。
    """
    elapsed = time.monotonic() - started_at
    cancelled = error is None and tool_outcome(result).status == TOOL_CANCELLED
    try:
        message, failure_error, attachments = await _async_result_message(
            registry=registry, tool_name=tool_name, tool_args=tool_args, async_tool_id=async_tool_id,
            elapsed=elapsed, result=result, error=error, workspace_root=workspace_root,
            session_id=session_id,
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
        cancelled=cancelled,
    )
    await deliver_async_result(http, supervisor_url, session_id, payload, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
#  真实工具执行
# ---------------------------------------------------------------------------

# 只读、互不依赖的内置工具：同一轮里连着出现时并发执行
PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "list_dir", "search_in_files", "glob", "get_context_window",
    "search_context_archive", "read_context_archive", READ_TOOL_OUTPUT,
})


def _parallel_tools_limit(runtime_cfg: Any) -> int:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return get_int(cfg, "engine.parallel_tools.max_concurrency", 8, min_value=1, max_value=64)


def _is_parallel_safe(name: str, arguments: dict[str, Any], spec: dict[str, Any] | None) -> bool:
    # async_mode 和 execute_command 的自适应转异步要把调用挪到后台，只走顺序路径
    if name == "execute_command" or (isinstance(spec, dict) and spec.get("async_mode")):
        return False
    # replace 模式会改文件
    if name == "search_in_files" and str(arguments.get("mode") or "search").strip().lower() == "replace":
        return False
    if name in PARALLEL_SAFE_TOOLS:
        return True
    return isinstance(spec, dict) and bool(parse_bool(spec.get("parallel_safe"), False))


def _execution_groups(
    ls: _LoopState, real_tool_calls: list[dict[str, Any]], concurrency: int,
) -> list[tuple[bool, list[dict[str, Any]]]]:
    """(是否并发, 调用) 的分组：连续两个以上可并发的调用成一组，其余每个调用单独一组。"""
    if concurrency <= 1 or len(real_tool_calls) < 2:
        return [(False, [call]) for call in real_tool_calls]
    groups: list[list[dict[str, Any]]] = []
    flags: list[bool] = []
    for call in real_tool_calls:
        safe = _is_parallel_safe(call["name"], call["arguments"], ls.registry.get_spec(call["name"]))
        if safe and flags and flags[-1]:
            groups[-1].append(call)
            continue
        groups.append([call])
        flags.append(safe)
    return [(safe and len(group) > 1, group) for safe, group in zip(flags, groups)]


async def _before_tool_hook(
    ls: _LoopState,
    rtc: dict[str, Any],
    hook_calls: list[ParsedToolCall],
    real_tool_calls: list[dict[str, Any]],
    step: int,
) -> tuple[ParsedToolCall, Any]:
    """单个真工具的 before_tool_call：审批类 handler 以当前 tool_call 为粒度判断。"""
    current = ParsedToolCall(
        id=str(rtc.get("id") or ""),
        name=str(rtc["name"]),
        arguments=dict(rtc["arguments"] or {}),
    )
    hook_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step,
        tool_call=current,
        tool_calls=hook_calls,
        extra={"real_tool_calls": real_tool_calls},
    )
    return current, await hook_registry.afire("before_tool_call", hook_ctx)


def _blocked_entry(rtc: dict[str, Any], hook_result: Any) -> dict[str, Any]:
    message = hook_result.error_message or hook_result.reason or "Tool call blocked by before_tool_call hook."
    return {
        "id": rtc.get("id", ""),
        "name": rtc["name"],
        "args": rtc["arguments"],
        "format": "text",
        "raw_inline": message,
        "summary": message[:200],
        "status": TOOL_BLOCKED,
    }


async def _record_tool_result(
    ls: _LoopState,
    rtc: dict[str, Any],
    current_tool_call: ParsedToolCall,
    result: Any,
    *,
    elapsed_ms: float,
    step: int,
    attachments: list[dict[str, Any]],
) -> tuple[dict[str, Any], TaskAction | None, bool]:
    """一个执行完的同步工具：记成败、发结束事件、收附件。返回 (结果条目, after_tool_call 给的终止动作, 是否被取消)。"""
    name = rtc["name"]
    args = rtc["arguments"]
    outcome = tool_outcome(result)

    # finish 硬校验靠这两份记录核对本任务里真实工具有没有成功执行过，防止编造工具结果的假成功
    if outcome.ok:
        ls.succeeded_real_tools.add(name)
    else:
        ls.failed_real_tools.add(name)

    # 进度行沿用「[node] tool: summary」格式，命令、查询词这些参数只能靠 summary 带出去
    summary = summarize_result(name, result, args=args)
    # spec 里声明了 result_format 就按它格式化，没有才按结果结构猜
    fmt, raw = result_to_raw(name, result, tool_spec=ls.registry.get_spec(name))
    entry = {
        "id": rtc.get("id", ""),
        "name": name,
        "args": args,
        "format": fmt,
        "raw_inline": raw,
        "summary": summary,
        "status": outcome.status,
    }

    # 事件写进事件日志、推给控制台，只供展示：超长的正文只带摘录，进上下文的 entry 仍是全文
    event_result, result_clipped = tool_result_for_event(result)
    event_raw, raw_clipped = bounded_for_event(raw)
    # 工具报错或被取消也要发结构化的结束事件，断线重连的客户端靠 EventLog 还原工具卡片
    await ls.rctx.emit_event("tool_call_end", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "tool_call_id": rtc.get("id", ""),
        "tool_name": name,
        "status": outcome.status,
        "summary": summary,
        "result": event_result,
        "raw_inline": event_raw,
        "format": fmt,
        "elapsed_ms": elapsed_ms,
        **({"truncated_for_event": True} if result_clipped or raw_clipped else {}),
    })

    # 已取消的工具不处理附件和进度事件
    if outcome.status == TOOL_CANCELLED:
        return entry, None, True

    # 附件由 after_tool_call 上的 AttachmentCollector 收进 attachments 和 loop state
    attachment_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step,
        tool_call=current_tool_call,
        extra={
            "loop_state": ls,
            "tool_result": result,
            "tool_attachments": attachments,
        },
    )
    attachment_result = await hook_registry.afire("after_tool_call", attachment_ctx)
    if attachment_result.action is not None:
        return entry, attachment_result.action, False

    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] {name}: {summary}",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })
    return entry, None, False


async def _timed_tool_call(ls: _LoopState, rtc: dict[str, Any], base_ctx: ToolContext) -> tuple[Any, float]:
    # 每个调用一份自己的 ToolContext：并发时 tool_call_id 互不覆盖，审批卡片才对得上
    tool_ctx = _snapshot_tool_context(base_ctx)
    tool_ctx.tool_call_id = str(rtc.get("id") or "")
    tool_ctx.node_id = ls.node.id
    started = time.monotonic()
    result = await _execute_registry_tool_with_span(ls.registry, rtc["name"], rtc["arguments"], tool_ctx)
    return result, round((time.monotonic() - started) * 1000, 1)


async def _gather_bounded(factories: list[Any], limit: int) -> list[Any]:
    """最多 limit 个同时跑，结果按传入顺序返回；有一个抛错就取消其余的再往上抛。"""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def bounded(factory: Any) -> Any:
        async with semaphore:
            return await factory()

    tasks = [asyncio.create_task(bounded(factory)) for factory in factories]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _emit_tool_call_start(ls: _LoopState, rtc: dict[str, Any]) -> None:
    # 事件只供展示：write_file 之类的长参数只带摘录，工具执行拿的仍是原参数
    event_arguments, clipped = bounded_for_event(rtc["arguments"])
    # 断线重连的客户端靠 EventLog 里这条结构化事件还原工具卡片
    await ls.rctx.emit_event("tool_call_start", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "tool_call_id": rtc.get("id", ""),
        "tool_name": rtc["name"],
        "arguments": event_arguments,
        **({"truncated_for_event": True} if clipped else {}),
    })


async def _run_parallel_group(
    ls: _LoopState,
    group: list[dict[str, Any]],
    base_ctx: ToolContext,
    hook_calls: list[ParsedToolCall],
    real_tool_calls: list[dict[str, Any]],
    step: int,
    *,
    concurrency: int,
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], TaskAction | None, bool]:
    """一组连续的只读调用：hook 按顺序先全部跑完，放行的并发执行，再按原顺序记结果。

    返回 (结果条目, 终止动作, 组内有没有被取消的)。
    """
    checked: list[tuple[dict[str, Any], ParsedToolCall, Any]] = []
    for rtc in group:
        current, hook_result = await _before_tool_hook(ls, rtc, hook_calls, real_tool_calls, step)
        if hook_result.action is not None:
            return [], hook_result.action, False
        checked.append((rtc, current, hook_result))

    runnable = [rtc for rtc, _current, hook_result in checked if not (hook_result.block or hook_result.skip_step)]
    for rtc in runnable:
        await _emit_tool_call_start(ls, rtc)
    outcomes = iter(await _gather_bounded(
        [partial(_timed_tool_call, ls, rtc, base_ctx) for rtc in runnable], concurrency,
    ))

    entries: list[dict[str, Any]] = []
    cancelled = False
    for rtc, current, hook_result in checked:
        if hook_result.block or hook_result.skip_step:
            entries.append(_blocked_entry(rtc, hook_result))
            continue
        result, elapsed_ms = next(outcomes)
        entry, action, was_cancelled = await _record_tool_result(
            ls, rtc, current, result, elapsed_ms=elapsed_ms, step=step, attachments=attachments,
        )
        entries.append(entry)
        cancelled = cancelled or was_cancelled
        if action is not None:
            return entries, action, cancelled
    return entries, None, cancelled


async def _async_started_entry(
    ls: _LoopState, rtc: dict[str, Any], *, summary: str, placeholder: str, started_at: float, progress: str,
) -> dict[str, Any]:
    """调用挪到后台后当轮先交的占位结果；结束事件照发，客户端看得到后台已经接手。"""
    await ls.rctx.emit_event("tool_call_end", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "tool_call_id": rtc.get("id", ""),
        "tool_name": rtc["name"],
        "status": TOOL_ASYNC_STARTED,
        "summary": summary,
        "result": None,
        "raw_inline": placeholder,
        "format": "text",
        "elapsed_ms": round((time.monotonic() - started_at) * 1000, 1),
    })
    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] {rtc['name']}: {progress}",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })
    return {
        "id": rtc.get("id", ""),
        "name": rtc["name"],
        "args": rtc["arguments"],
        "format": "text",
        "raw_inline": placeholder,
        "summary": summary,
        "status": TOOL_ASYNC_STARTED,
    }


async def _start_async_tool(ls: _LoopState, rtc: dict[str, Any], tool_ctx: ToolContext) -> dict[str, Any]:
    """spec 标了 async_mode 的工具放到后台跑，结果之后作为新消息交回，当轮先交占位结果。

    后台那份工具上下文一开始就按发起任务认取消：本任务挂起再恢复、领取 id 换了，它照样跑。
    """
    name = rtc["name"]
    args = rtc["arguments"]
    async_ctx = _snapshot_tool_context(tool_ctx)
    prepare = getattr(ls.registry, "prepare", None)
    if prepare is not None:
        try:
            await prepare(name=name, arguments=args, ctx=async_ctx)
        except Exception as error:
            message = f"工具准备失败，尚未执行：{error}"
            ls.failed_real_tools.add(name)
            return {
                "id": rtc.get("id", ""), "name": name, "args": args,
                "format": "text", "raw_inline": message,
                "summary": message[:200], "status": TOOL_ERROR,
            }
    started_at = time.monotonic()
    await _emit_tool_call_start(ls, rtc)
    _cleanup_async_tracker()
    async_id = uuid.uuid4().hex[:8]
    _async_tool_tasks[async_id] = {
        "tool_name": name,
        "status": "running",
        "started_at": started_at,
        "task_id": ls.rctx.task_id,
    }
    async_ctx.detached = True
    # 后台协程会继承这里的上下文：它在后台等审批时本任务并没有在等，不能报给本任务的名额
    with report_approval_waits_to(None):
        _spawn_background(
            _run_async_tool(
                registry=ls.registry,
                tool_name=name,
                tool_args=args,
                tool_ctx=async_ctx,
                http=ls.rctx.http,
                supervisor_url=ls.rctx.supervisor_url,
                task_id=ls.rctx.task_id,
                # 结果里的超长输出存在面向用户的会话名下（入口分支只装运行期历史）；交给谁由 supervisor 按发起任务定
                session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                async_tool_id=async_id,
                started_at=started_at,
                node_id=ls.node.id,
                workspace_root=ls.rctx.workspace_root,
            ),
            name=f"async_tool_{name}_{async_id}",
        )
    # 发起成功就算满足 finish_requires_tool：结果回传进的是新任务，模型在那边只需播报，不然 finish 会被反复拒绝
    ls.succeeded_real_tools.add(name)
    return await _async_started_entry(
        ls, rtc,
        summary=f"异步执行已启动 (id: {async_id})，结果之后作为新消息交回",
        placeholder=f'⏳ Async tool "{name}" started (id: {async_id}). {_ASYNC_RESULT_ARRIVAL}',
        started_at=started_at,
        progress="异步执行已启动",
    )


async def _execute_with_async_upgrade(
    ls: _LoopState, rtc: dict[str, Any], tool_ctx: ToolContext, *, threshold: float, started_at: float,
) -> tuple[Any, dict[str, Any] | None]:
    """execute_command 照常执行、只等到阈值；到点没跑完就把还在跑的任务转后台，命令不重跑。

    返回 (结果, None)；转了后台返回 (None, 占位结果条目)。命令自己的 timeout_sec 仍是硬上限。
    转后台那一刻起：命令按发起任务认取消，本任务挂起再恢复不会把它杀掉；它再等审批也不算本任务在等，
    那一刻还没等完的审批交还本任务的名额。
    """
    name = rtc["name"]
    args = rtc["arguments"]
    exec_ctx = _snapshot_tool_context(tool_ctx)
    with relay_approval_waits() as approval_relay:
        exec_task = asyncio.create_task(
            _execute_registry_tool_with_span(ls.registry, name, args, exec_ctx),
            name=f"execute_command_adaptive_{str(rtc.get('id') or '')[:24] or 'call'}",
        )
    try:
        done, _pending = await asyncio.wait({exec_task}, timeout=float(threshold))
    except asyncio.CancelledError:
        # 本轮被打断时命令还没交给后台：跟着一起停，不能留一个没人收结果的子进程
        exec_task.cancel()
        raise
    if exec_task in done:
        return exec_task.result(), None
    approval_relay.detach()
    exec_ctx.detached = True
    _cleanup_async_tracker()
    async_id = uuid.uuid4().hex[:8]
    _async_tool_tasks[async_id] = {
        "tool_name": name,
        "status": "running",
        "started_at": started_at,
        "task_id": ls.rctx.task_id,
        "upgraded_from": "sync_timeout",
    }
    with report_approval_waits_to(None):
        _spawn_background(
            _deliver_started_async_task(
                exec_task,
                registry=ls.registry,
                http=ls.rctx.http,
                supervisor_url=ls.rctx.supervisor_url,
                task_id=ls.rctx.task_id,
                session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                tool_name=name,
                tool_args=args,
                async_tool_id=async_id,
                started_at=started_at,
                node_id=ls.node.id,
                workspace_root=ls.rctx.workspace_root,
            ),
            name=f"async_upgrade_{name}_{async_id}",
        )
    entry = await _async_started_entry(
        ls, rtc,
        summary=f"执行超过 {threshold:.1f}s，已自动转为异步 (id: {async_id})，结果之后作为新消息交回",
        placeholder=(
            f'⏳ Tool "{name}" exceeded {threshold:.1f}s and was auto-upgraded to async (id: {async_id}). '
            f"It keeps running in the background, also while this task waits for a sub-agent. {_ASYNC_RESULT_ARRIVAL}"
        ),
        started_at=started_at,
        progress="已自动转为异步执行",
    )
    return None, entry


async def _run_sequential_call(
    ls: _LoopState,
    rtc: dict[str, Any],
    tool_ctx: ToolContext,
    hook_calls: list[ParsedToolCall],
    real_tool_calls: list[dict[str, Any]],
    step: int,
    *,
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], TaskAction | None, bool]:
    """顺序执行一个真工具调用：per-call hook，再按 async_mode 转后台、execute_command 自适应转异步或同步执行。

    返回 (结果条目, 终止动作, 是否被取消)；hook 直接给出终止动作时没有结果条目。
    """
    # 顺序执行的调用共用一个 ToolContext，执行前换成这次调用的 id，审批卡片才挂得到对应的工具卡片上
    tool_ctx.tool_call_id = str(rtc.get("id") or "")
    tool_ctx.node_id = ls.node.id
    current, hook_result = await _before_tool_hook(ls, rtc, hook_calls, real_tool_calls, step)
    if hook_result.action is not None:
        return [], hook_result.action, False
    if hook_result.block or hook_result.skip_step:
        return [_blocked_entry(rtc, hook_result)], None, False

    spec = ls.registry.get_spec(rtc["name"])
    if spec and spec.get("async_mode", False):
        return [await _start_async_tool(ls, rtc, tool_ctx)], None, False

    started_at = time.monotonic()
    await _emit_tool_call_start(ls, rtc)
    threshold = _execute_command_async_upgrade_threshold(rtc["name"], rtc["arguments"], ls.runtime_cfg)
    if threshold is None:
        result = await _execute_registry_tool_with_span(ls.registry, rtc["name"], rtc["arguments"], tool_ctx)
    else:
        result, upgraded = await _execute_with_async_upgrade(
            ls, rtc, tool_ctx, threshold=threshold, started_at=started_at,
        )
        if upgraded is not None:
            return [upgraded], None, False
    entry, action, cancelled = await _record_tool_result(
        ls, rtc, current, result,
        elapsed_ms=round((time.monotonic() - started_at) * 1000, 1), step=step, attachments=attachments,
    )
    return [entry], action, cancelled


async def _execute_real_tools(
    ls: _LoopState,
    real_tool_calls: list[dict[str, Any]],
    step: int,
    *,
    attachment_messages: list[dict[str, Any]] | None = None,
) -> TaskAction | None:
    """批量执行真实工具调用，将结果追加到 messages。

    连续的只读调用成组并发执行，其余一个个顺序执行；hook、事件、成败记录和结果都按模型给出的顺序。
    传了 attachment_messages 时，工具产出图片的那条 user 消息放进它里面，由调用方等本轮结果全部补完再写。
    """
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

    # 顺序执行的调用共用这一个实例，每次调用前刷新 tool_call_id；并发组里每个调用各拷一份
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

    _concurrency = _parallel_tools_limit(ls.runtime_cfg)
    for _parallel, _group in _execution_groups(ls, real_tool_calls, _concurrency):
        if await ls.rctx.check_cancelled():
            break
        if _parallel:
            _entries, action, _cancelled = await _run_parallel_group(
                ls, _group, _tool_ctx, _hook_real_tool_calls, real_tool_calls, step,
                concurrency=_concurrency, attachments=_tool_atts,
            )
        else:
            _entries, action, _cancelled = await _run_sequential_call(
                ls, _group[0], _tool_ctx, _hook_real_tool_calls, real_tool_calls, step, attachments=_tool_atts,
            )
        _tool_entries.extend(_entries)
        if action is not None:
            return action
        # 被取消的结果已经记下（tool_use 不能悬空没有结果），后面的调用不再执行，也不补结果
        if _cancelled:
            break

    if _tool_entries:
        _limits = tool_output_limits(ls.runtime_cfg)
        # 和 context archive 同一个会话键：read_tool_output 按 ToolContext 的同一个键去找
        _output_session = compact_target_session_id(ls)
        for _entry in _tool_entries:
            # 超长结果的完整文本落盘，上下文里换成带 ref 的预览；tool_call_end 事件里还是原文
            _result_body = await _context_tool_text(
                ls.rctx.workspace_root, _output_session, str(_entry["name"]), _entry["raw_inline"], _limits,
            )
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
                tool_status=str(_entry.get("status") or ""),
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
            _tool_att_msg = {
                "role": "user",
                "content": build_multimodal_content(
                    "以上工具执行产生了以下图片结果：", _tool_atts, workspace_root=ls.rctx.workspace_root,
                ),
                # 运行期也要认得出这不是新的用户输入：重复调用检测按它决定要不要接着往前数
                "_meta": {"message_type": TOOL_RESULT_ATTACHMENT_TYPE},
            }
            if attachment_messages is not None:
                attachment_messages.append(_tool_att_msg)
            else:
                _append_attachment_message(ls, _tool_att_msg)

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
        usage=dict(ls.last_usage) if ls.last_usage else {},
        **_reasoning_fields(ls, resp),
    )
    set_message_meta(_assistant_msg, _meta)
    apply_reasoning_content(_assistant_msg)
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
            usage=dict(ls.last_usage) if ls.last_usage else {},
            **_reasoning_fields(ls, resp),
        )
        set_message_meta(_assistant_msg, _implicit_meta)
        apply_reasoning_content(_assistant_msg)
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
        usage=dict(ls.last_usage) if ls.last_usage else {},
        **_reasoning_fields(ls, resp),
    )
    set_message_meta(_assistant_msg, _implicit_meta)
    apply_reasoning_content(_assistant_msg)
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


@dataclass(frozen=True)
class _Wrapup:
    """这一步为什么是最后一步：步数用完（steps），还是这一段的运行时间快到上限（time）。"""

    reason: str
    max_steps: int

    @property
    def notice(self) -> str:
        return TIME_LIMIT_NOTICE if self.reason == WRAPUP_TIME else STEP_LIMIT_NOTICE


def _append_final_step_hint(
    ls: _LoopState, step: int, max_steps: int, *, reason: str = WRAPUP_STEPS, elapsed_minutes: int = 0,
) -> None:
    if reason == WRAPUP_TIME:
        content = _TIME_LIMIT_HINT.format(minutes=max(0, int(elapsed_minutes)))
    else:
        content = _FINAL_STEP_HINT.format(used=step, total=max_steps)
    pending = unfinished_summary(list(getattr(ls, "todos", None) or []))
    if pending:
        content += f"\n{pending}\n在 finish 里逐项交代这些待办做到哪一步、为什么没做完。"
    ls.messages[:] = [message for message in ls.messages if not message.get(_FINAL_STEP_HINT_KEY)]
    # 只在运行期存在：发给模型，不落盘
    ls.messages.append({
        "role": "user",
        "content": content,
        "_retry_hint": True,
        _FINAL_STEP_HINT_KEY: True,
    })


def _seed_carried_todos(ls: _LoopState, carried: Any) -> None:
    """续跑任务接上上一段的待办；本任务自己改过清单（挂起再恢复的情形）就以自己改的为准。"""
    restored = latest_todos_from_messages(ls.messages, str(getattr(ls.rctx, "task_id", "") or ""))
    ls.todos = restored if restored is not None else carried_todo_items(carried)
    ls.todos_restored = True


def _mark_wrapup(ls: _LoopState, action: TaskAction, wrapup: _Wrapup, step: int) -> TaskAction:
    """最后一步交付的 finish 打上收尾标记，带上用了几步和当时的待办：supervisor 据此决定要不要接着做。"""
    if action.action != ACTION_FINISH:
        return action
    result = dict(action.result or {})
    if result.get(NO_REPLY_MARKER) is True:
        return action
    todos = [dict(item) for item in getattr(ls, "todos", None) or [] if isinstance(item, dict)]
    result[TIME_LIMIT_FLAG if wrapup.reason == WRAPUP_TIME else STEP_LIMIT_FLAG] = True
    result[WRAPUP_KEY] = {
        "reason": wrapup.reason,
        "steps_used": step + 1,
        "max_steps": wrapup.max_steps,
        "todos": todos,
        "unfinished": len(unfinished_items(todos)),
    }
    action.result = result
    return action


def _tool_spec_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function.get("name") or tool.get("name") or "")


def _finish_only_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tool for tool in tools or [] if _tool_spec_name(tool) == "finish"]


def _step_limit_summary(ls: _LoopState) -> str:
    """模型最后一步什么也没交代时，按本任务调用过的工具和没做完的待办说明做到了哪。"""
    names = [
        str(entry.get("name") or "")
        for entry in getattr(ls.rctx, "tool_call_log", None) or []
        if isinstance(entry, dict) and entry.get("name")
    ]
    pending = unfinished_summary(list(getattr(ls, "todos", None) or []))
    if not names and not pending:
        return ""
    if names:
        counts = Counter(names)
        used = "、".join(f"{name} ×{count}" if count > 1 else name for name, count in counts.items())
        summary = f"任务还没有完成。已经执行过的工具：{used}。剩下的部分尚未完成，需要继续处理。"
    else:
        summary = "任务还没有完成，剩下的部分需要继续处理。"
    return f"{summary}\n{pending}" if pending else summary


def _step_limit_finish(
    ls: _LoopState,
    text: str,
    step: int,
    *,
    notice: bool,
    truncated: bool = False,
    wrapup: _Wrapup | None = None,
) -> TaskAction:
    """把收尾文本当隐式 finish 交付，result 里打上 step_limit_reached 或 time_limit_reached。"""
    reason = wrapup or _Wrapup(reason=WRAPUP_STEPS, max_steps=step + 1)
    body = f"{text}\n\n{reason.notice}" if notice else text
    # response 传 None：json 模式的 assistant 消息才会存这里拼好的文本，而不是从原响应里重新取
    action = build_implicit_finish(ls, None, body, step=step, truncated=truncated)
    return _mark_wrapup(ls, action, reason, step)


def _step_limit_wrapup(
    ls: _LoopState,
    step: int,
    *,
    text: str,
    notice: bool,
    truncated: bool = False,
    wrapup: _Wrapup | None = None,
) -> TaskAction | None:
    """最后一步模型没有用 finish 收尾时怎么结束：有正文交付正文，没有就按 tool_call_log 交代做过什么。

    notice 为真时正文末尾注明到了步数或时长上限；按调用记录生成的说明总是注明。
    正文和调用记录都拿不到时返回 None，走原来的 FAIL。
    """
    if ls.preempt_after_step:
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=_persist_ctx(ls, step + 1), summary="任务被软打断，上下文已保存。",
        )
    # 带消息的抢占要到下一步才注入，最后一步之后没有下一步了：维持原来的结束方式
    if ls.preempt_inject_info is not None:
        return None
    body = (text or "").strip()
    if not body:
        body, notice, truncated = _step_limit_summary(ls), True, False
    if not body:
        return None
    reason = wrapup or _Wrapup(reason=WRAPUP_STEPS, max_steps=step + 1)
    logger.warning(
        "node %s reached its %s limit without finish; delivering a wrap-up (step=%d)",
        ls.node.id, "run time" if reason.reason == WRAPUP_TIME else "step", step,
    )
    return _step_limit_finish(ls, body, step, notice=notice, truncated=truncated, wrapup=reason)


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
    steps_used: int = 0,
    run_budget: RunBudget | None = None,
    carried_todos: list[dict[str, Any]] | None = None,
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
    resumed_messages: list[dict[str, Any]] = []
    if resume_data:
        resumed_messages = _build_resume_messages(await _resume_data_for_context(resume_data, rctx, runtime_cfg))
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
        _instruction_meta = {**route_meta, "source_task_id": rctx.task_id}
        # 异步工具结果回传进来的这条输入也是工具结果，和 runner 落盘的那份记同一个执行状态
        _async_tool_status = async_result_tool_status(rctx.task_context)
        if _async_tool_status:
            _instruction_meta["tool_status"] = _async_tool_status
        # 短答的当轮输入组装时接上了要求，得按发出去的样子认：认不出它就标不上本任务的 meta，还会往前错标别的输入
        _shown_instruction = render_user_input(instruction, turn_input_meta(rctx.task_context))
        for message in reversed(messages):
            if message.get("role") == "user" and not message.get("_dynamic"):
                content = message.get("content")
                if content == instruction or content == _shown_instruction or isinstance(content, list):
                    message.setdefault("_meta", {}).update(_instruction_meta)
                    break

    # ---- 构建工具列表 ----
    _registry_specs = registry.list_specs()
    tool_specs = _filter_tool_specs(node, _registry_specs)
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
    # 超长输出的 ref 只能用 read_tool_output 读回；它只读本会话自己的产物，不受 tool_access 限制，没有真工具的节点不加
    if tool_specs and not any(s.get("name") == READ_TOOL_OUTPUT for s in tool_specs):
        _reader_spec = next((s for s in _registry_specs if s.get("name") == READ_TOOL_OUTPUT), None)
        if _reader_spec is not None:
            tool_specs.append(_reader_spec)
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
    _is_system_task = bool((rctx.task_context or {}).get("is_system_task"))
    if delegate_targets:
        # [2026-05-04] Register one dynamic dispatch tool per delegate target.
        # Why: target selection should happen through tool choice, not through an
        # aggregate dispatch schema. How: expand node.delegate_targets into only
        # dispatch:{target_id} specs. Purpose: keep dynamic dispatch intact while
        # removing the old aggregate dispatch tools from the model-visible list.
        # 系统任务不许挂起等子任务（supervisor 也会拒），不给它 wait 参数，也不给 await_dispatches
        _dispatch_timeouts = DispatchTimeouts.from_config(runtime_cfg)
        openai_tools.extend(_dispatch_delegate_specs(
            delegate_targets, downstream_info, allow_wait=not _is_system_task, timeouts=_dispatch_timeouts,
        ))
        openai_tools.extend(_dispatch_control_specs(allow_await=not _is_system_task, timeouts=_dispatch_timeouts))

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
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
    _todo_enabled = not _is_system_task and bool(parse_bool((getattr(node, "extra", None) or {}).get("todo"), False))
    if _todo_enabled:
        openai_tools.append(_update_todos_spec())
    # 开了就每个任务都注入，不随直接找 bot 还是插话变：工具表一变，前缀缓存整段失效。
    if not _is_system_task and parse_bool((getattr(node, "extra", None) or {}).get("stay_silent"), False):
        openai_tools.append(_stay_silent_spec())

    _short_reply_task = short_reply(rctx.task_context)
    if _short_reply_task:
        # 工具表和 system 跟普通任务一字不差，前缀缓存才接得上：只接一句的要求在当轮 user 消息里，这里只收执行许可
        max_steps = min(max_steps, _SHORT_REPLY_MAX_STEPS)
        _allowed_real_tools = set()
        delegate_targets = []
        _todo_enabled = False

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
        todo_enabled=_todo_enabled,
    )
    if resumed_messages:
        _record_resumed_messages(ls, resumed_messages)
    if isinstance(resume_data, dict):
        _adopt_resumed_attachments(ls, resume_data)

    # 步数太少的节点和系统任务不做收尾，维持原逻辑；短答也不做：收尾那一步会把工具表收窄成只剩 finish，前缀缓存就断了
    _wrapup_enabled = not _is_system_task and not _short_reply_task and max_steps >= 3

    # 默认配置不写快照，挂起前用掉的步数由 supervisor 记在任务上带回来
    step_count = max(step_count, max(0, int(steps_used or 0)))
    if _wrapup_enabled and step_count >= max_steps:
        # 步数上限在挂起期间被改小了：留最后一步交代进度，不直接判失败
        step_count = max_steps - 1
    rctx.completed_steps = step_count
    rctx.step_offset = step_count
    _lease = getattr(rctx, "lease", None)
    if _lease is not None:
        _lease.steps_completed = step_count
    if _todo_enabled and carried_todos:
        _seed_carried_todos(ls, carried_todos)

    # ---- 推理循环 ----
    _time_wrapup = False
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

        if _wrapup_enabled and not _time_wrapup and run_budget is not None and run_budget.due():
            _time_wrapup = True
            logger.info(
                "node %s is close to its run time limit; wrapping up at step %d (task=%s)",
                ls.node.id, step, getattr(ls.rctx, "task_id", ""),
            )
        _final_step = _wrapup_enabled and (_time_wrapup or step == max_steps - 1)
        _wrapup = _Wrapup(reason=WRAPUP_TIME if _time_wrapup else WRAPUP_STEPS, max_steps=max_steps)
        _step_tools = ls.openai_tools
        if _final_step:
            _append_final_step_hint(
                ls, step, max_steps, reason=_wrapup.reason,
                elapsed_minutes=int(run_budget.elapsed_seconds() // 60) if run_budget is not None else 0,
            )
            # json / fake-native 只靠提示；native 这一步只给 finish
            if formatter.mode == "native":
                _step_tools = _finish_only_tools(ls.openai_tools) or ls.openai_tools
        _full_tools, ls.openai_tools = ls.openai_tools, _step_tools
        try:
            result = await _call_llm_with_retry(ls, step)
        finally:
            ls.openai_tools = _full_tools
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
        if _lease is not None:
            _lease.steps_completed = step + 1

        # Phase 3 Hook System：LLM 调用后的 usage 统计交给 UsageTracker。
        # 原因：token 累加是 after_llm_call 的典型横切逻辑。做法：传入响应和
        # loop state，由 handler 更新 rctx.total_usage。目的：保持 TaskRecord
        # 用量统计不变，同时从 ai_step.py 中抽出 bookkeeping。
        _usage_ctx = HookContext(
            messages=ls.messages,
            # 备用渠道按这份 tools 重发：最后一步得和主渠道一样只给 finish
            tools=_step_tools,
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
        if _usage_result.skip_step:
            # 这次回复作废：工具不执行、回复不进历史，带着 hook 追加的提示进下一步；最后一步没有下一步，按收尾交付
            if _final_step:
                action = _step_limit_wrapup(ls, step, text="", notice=True, wrapup=_wrapup)
                if action is not None:
                    return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            continue
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
            action = await _handle_tool_calls(
                ls, resp, step, final_step=_final_step, more_steps=step + 1 < max_steps,
            )
            if action is None and _final_step:
                # 整轮被 hook 拦下时（比如 finish 声称成功却没真调过工具），这一轮的正文不能当结论交付
                action = _step_limit_wrapup(
                    ls, step, text="" if ls.last_round_blocked else (resp.text or ""), notice=True, wrapup=_wrapup,
                )
            elif action is not None and _final_step:
                # 听提示在最后一步调了 finish 也是被迫收尾，标记照打，supervisor 才知道还没做完
                action = _mark_wrapup(ls, action, _wrapup, step)
            if action is not None:
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            if is_truncated(resp):
                # 截断点之后的调用已经丢了，告诉模型这轮没说完
                _append_truncation_hint(ls)
            continue

        # 最后一步回了纯文本：没有下一步可以重试了，直接按隐式 finish 交付
        if _final_step:
            action = _step_limit_wrapup(
                ls, step, text=resp.text or "", notice=False, truncated=is_truncated(resp), wrapup=_wrapup,
            )
            if action is not None:
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)

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
