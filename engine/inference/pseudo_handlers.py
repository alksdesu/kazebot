"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具和 dispatch:{target_id} 动态伪工具的执行逻辑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes as _mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import SwitchValueError, parse_bool, switch_arg, switch_refusal
from clonoth_sdk.refusals import refusal_reason

from .resume_builder import _select_attachments
from .async_delivery import RetriedPost, post_until_answered
from .attachment_access import RefusedAttachment, screen_model_attachment_paths
from ..attachments import is_allowed_attachment_path
from ..compaction_plan import build_compaction_plan
from ..context import EVENT_UNCONFIRMED
from ..conversation_routing import short_reply as _is_short_reply
from ..dispatch_batch import (
    AWAIT_ALL,
    AWAIT_MODES,
    CONTINUE_FROM_KEY,
    ISOLATION_KEY,
    ISOLATION_WORKTREE,
    ITEM_TIMEOUT_KEY,
    MAX_AWAIT_HANDLES,
    MAX_DISPATCH_BATCH_ITEMS,
    OWN_SESSION_MODES,
    REPO_KEY,
    AwaitRequest,
    DispatchTimeouts,
    context_mode_refusal,
    parse_context_mode,
    parse_timeout_sec,
)
from ..dispatch_handle import format_handle, parse_handle
from ..isolation_notice import nested_isolation_refusal, task_isolation
from ..protocol import TaskAction, ACTION_ASK, ACTION_DISPATCH, ACTION_FINISH
from ..todo_list import TODO_TOOL_NAME, TodoListError, parse_todo_items, render_update_result
from .loop_state import _LoopState, _persist_ctx, _short, compact_target_session_id, flush_conversation_writes
# 【Fix 3】reply 工具结果统一走 formatter.format_tool_result，需要 ParsedToolCall 构造、
# MessageMeta 标注 message_type、MessageType 影子写入。延迟导入 _shadow_write 避免循环依赖。
from .tool_format import ParsedToolCall
from .pseudo_tools import (
    AWAIT_DISPATCHES_TOOL_NAME,
    CANCEL_DISPATCH_TOOL_NAME,
    LIST_DISPATCHES_TOOL_NAME,
    NO_REPLY_MARKER,
    STAY_SILENT_TOOL_NAME,
    _dispatch_target_from_tool_name,
)
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType
from ..node import canonical_node_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  [2026-04-22] 辅助函数：将 workspace-relative 路径列表转换为 attachment dict 列表。
#  [2026-05-04] 现在只服务 dispatch:{target_id} 动态伪工具。
#  为什么：旧聚合委派工具已删除，但动态委派仍需把父节点文件传给子节点。
#  怎么做：保留路径到 attachment dict 的转换函数，删除旧分支调用。
#  目的：让动态 dispatch 的附件行为保持不变。
# ---------------------------------------------------------------------------
def _paths_to_attachments(paths: list, workspace_root: Path) -> list[dict]:
    """Convert workspace-relative file paths to attachment dicts for dispatch.

    Model-supplied paths must go through screen_model_attachment_paths first.
    """
    result = []
    for p in paths:
        p_str = str(p).strip()
        # 路径是模型填的，和子节点读附件时用同一道门
        if not p_str or not is_allowed_attachment_path(p_str):
            continue
        full = workspace_root / p_str
        if not full.exists():
            continue
        mime = _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        att_type = "image" if mime.startswith("image/") else "file"
        result.append({
            "type": att_type,
            "path": p_str,
            "mime_type": mime,
            "name": full.name,
        })
    return result


def _dispatch_result_input_attachments(ls: _LoopState) -> list[dict]:
    """Return current inbound attachments from an async dispatch_result task."""
    task_context = getattr(ls.rctx, "task_context", {}) or {}
    if not isinstance(task_context, dict):
        return []
    if str(task_context.get("inbound_message_type") or "").strip() != "dispatch_result":
        return []
    if bool(task_context.get("inbound_attachments_outbound_sent")):
        return []
    attachments = task_context.get("inbound_attachments")
    if not isinstance(attachments, list):
        return []
    return [att for att in attachments if isinstance(att, dict)]


def _dispatch_result_attachments_already_sent(ls: _LoopState) -> bool:
    """Return True if this turn's dispatch_result attachments were already
    auto-forwarded to the platform by the supervisor (dispatch_attachment).

    [2026-07-08] Why: async draw children (NovelAI) return generated images that
    the supervisor already delivers once via dispatch_attachment. The caller LLM
    is then handed a dispatch_result whose text used to leak the real disk paths,
    tempting it to call reply(attachment_paths=...) and send the SAME images a
    second time. How: expose the "already sent" flag so reply/finish can drop
    those paths. Purpose: never send the same generated image twice.
    """
    task_context = getattr(ls.rctx, "task_context", {}) or {}
    if not isinstance(task_context, dict):
        return False
    if str(task_context.get("inbound_message_type") or "").strip() != "dispatch_result":
        return False
    return bool(task_context.get("inbound_attachments_outbound_sent"))


_BAD_ATTACHMENT_PATHS = "attachment_paths 要写成文件路径的列表（字符串数组）。"


def _named_attachment_paths(args: dict[str, Any]) -> tuple[list[str] | None, str]:
    """模型点名的 attachment_paths：(路径列表, 读不懂时交回模型的原因)。

    没写、写 null 或写空都给 None，按没点名处理：不少模型给可选数组顺手填 []，不能因此把子任务的产物扣下。
    一个字符串当一条路径，别的类型不猜。
    """
    raw = args.get("attachment_paths")
    if raw is None:
        return None, ""
    if isinstance(raw, str):
        return ([raw.strip()] if raw.strip() else None), ""
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        named = [item.strip() for item in raw if item.strip()]
        return (named or None), ""
    return None, _BAD_ATTACHMENT_PATHS


def _reject_already_sent_dispatch_paths(ls: _LoopState, selected_paths: Any) -> list:
    """Drop attachment paths on a dispatch_result turn whose files the supervisor
    already auto-forwarded to the platform.

    [2026-07-08 v2] Why the earlier file-name match failed: when
    ``attachments_outbound_sent`` is True the supervisor deliberately omits the
    attachments from the dispatch_result inbound payload, so
    ``task_context.inbound_attachments`` is empty and there was nothing to match
    against — every path leaked through and got re-sent. On top of that,
    ``_select_attachments`` re-saves the file under a new ``branch_xxx`` path, so
    even a populated name set would not reliably line up with what actually goes
    out. How: on any dispatch_result turn where the images were already sent, the
    caller must not hand-send *any* attachment through reply()/finish(); drop the
    whole selection. Purpose: a single generated image is delivered exactly once.
    """
    if not isinstance(selected_paths, list) or not selected_paths:
        return selected_paths if isinstance(selected_paths, list) else []
    if not _dispatch_result_attachments_already_sent(ls):
        return selected_paths
    # Images for this dispatch_result were already delivered via
    # dispatch_attachment; refuse to re-send anything the caller selected.
    return []


def _http_failure(response: Any) -> str:
    """supervisor 应答不是 2xx 时给出原因；拿不到状态码的按成功算。"""
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or 200 <= status < 300:
        return ""
    return refusal_reason(response)


def _refusal_note(refused: list[RefusedAttachment]) -> str:
    detail = "；".join(f"{item.path}（{item.reason}）" for item in refused)
    return f"以下附件没有发出：{detail}"


def _with_refusals(text: str, refused: list[RefusedAttachment]) -> str:
    return f"{text}\n{_refusal_note(refused)}" if refused else text


def _ok_with_refusals(refused: list[RefusedAttachment]) -> str:
    return _with_refusals("ok", refused)


_REPLY_NOT_SENT = "Error: 这条 reply 没有送到 supervisor，用户没有收到。可以稍后再发一次，或把内容放进 finish。"
_REPLY_UNCONFIRMED = (
    "这条 reply 重发几次都没等到 supervisor 的应答，不确定用户有没有收到，别原样重发；"
    "要紧的内容可以在 finish 里换个说法带一句。"
)


def _emit_pseudo_tool_result(
    ls: _LoopState,
    pseudo_call,
    content: str,
    *,
    persist: bool = True,
    control_tool_name: str = "",
    control_tool_status: str = "",
) -> None:
    """统一写入伪工具的 tool_result，确保 native 模式下 tool_use/tool_result 配对完整。

    [2026-05-07] 正常 finish 也通过本函数写普通工具结果。
    原因：finish 现在是完整落盘的真实 API 工具，不能再被改写为普通 assistant 文本。
    做法：默认 persist=True 且不设置 control 标记；只有未交付的拦截类结果才使用 control 参数。
    目的：保留 provider 配对，同时让长期历史保存 assistant.tool_call + tool_result。
    """
    from .ai_step import _shadow_write
    _parsed = ParsedToolCall(
        id=getattr(pseudo_call, "id", "") or "",
        name=pseudo_call.name,
        arguments=dict(pseudo_call.arguments or {}),
    )
    tool_msg = ls.formatter.format_tool_result(_parsed, content)
    if control_tool_name:
        # [2026-05-07] 控制流工具结果必须保留运行期配对字段，但不能成为长期历史。
        # 原因：finish 是真实 provider tool_use，需要 ACK；同时 fake-native/json 结果默认没有 call_id，旧清洗只能按全局兜底处理。
        # 做法：给控制 ACK 标记 _ephemeral，并补齐 tool_call_id/name，供当轮内存配对和清洗函数精确识别。
        # 目的：满足 provider 配对要求，同时避免 finish 结果进入 ConversationStore、快照、压缩和摘要。
        tool_msg["_ephemeral"] = True
        if _parsed.id:
            tool_msg.setdefault("tool_call_id", _parsed.id)
        tool_msg.setdefault("name", control_tool_name)
    set_message_meta(tool_msg, MessageMeta(
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="tool_result",
        control_tool_name=control_tool_name,
        control_tool_status=control_tool_status,
    ))
    ls.messages.append(tool_msg)
    if persist:
        _shadow_write(ls, tool_msg, MessageType.TOOL_RESULT)


async def _handle_pseudo_tool(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理伪工具调用。

    返回 TaskAction 则退出循环（终止型伪工具或 compact dispatch）；
    返回 None 表示已处理完毕，调用方判断是否继续。
    """
    args = pseudo_call.arguments or {}
    from ..conversation_routing import concise_text, short_reply
    if short_reply(getattr(ls.rctx, "task_context", {}) or {}):
        # 短答只给 finish，不发言除外：插话的短答本来就可以选择不接。
        if pseudo_call.name == STAY_SILENT_TOOL_NAME:
            return await _handle_pseudo_stay_silent(ls, pseudo_call, step)
        if pseudo_call.name != "finish":
            _emit_pseudo_tool_result(ls, pseudo_call, _short_reply_only_finish(ls))
            return None
        args = {**args, "text": concise_text(str(args.get("text") or "")), "attachment_paths": []}

    # reply: 非终止，发送中间消息
    if pseudo_call.name == "reply":
        reply_text = str(args.get("text") or "").strip()
        selected_atts = []
        refused: list[RefusedAttachment] = []
        named_paths, path_problem = _named_attachment_paths(args)
        if path_problem:
            _emit_pseudo_tool_result(ls, pseudo_call, f"Error: {path_problem}这条 reply 没有发出，改好后再调用一次。")
            return None
        # [2026-07-08] Drop paths already auto-forwarded via dispatch_attachment,
        # so the caller LLM cannot re-send generated draw images a second time.
        selected_paths = _reject_already_sent_dispatch_paths(ls, named_paths)
        if selected_paths:
            allowed_paths, refused = await screen_model_attachment_paths(ls, pseudo_call, selected_paths)
            if allowed_paths:
                # 点名的文件不在已收集的附件里时整份读进来另存一份，大小不限
                selected_atts = await asyncio.to_thread(
                    _select_attachments,
                    list(ls.collected_attachments), allowed_paths,
                    workspace_root=ls.rctx.workspace_root,
                    session_id=ls.rctx.session_id,
                )
        if reply_text or selected_atts:
            delivered = await ls.rctx.emit_event("intermediate_reply", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
                "attachments": selected_atts,
                # 没等到应答时带着它重发，supervisor 按它只落一条
                "delivery_id": uuid.uuid4().hex,
            })
            if delivered is EVENT_UNCONFIRMED:
                _emit_pseudo_tool_result(ls, pseudo_call, _with_refusals(_REPLY_UNCONFIRMED, refused))
            elif delivered is False:
                _emit_pseudo_tool_result(ls, pseudo_call, _REPLY_NOT_SENT)
            else:
                _emit_pseudo_tool_result(ls, pseudo_call, _ok_with_refusals(refused))
        elif refused:
            _emit_pseudo_tool_result(ls, pseudo_call, _refusal_note(refused))
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # preempt_task: 非终止，软打断子任务
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, pseudo_call, args)

    if pseudo_call.name == TODO_TOOL_NAME:
        return await _handle_pseudo_update_todos(ls, pseudo_call, args)

    if pseudo_call.name == STAY_SILENT_TOOL_NAME:
        return await _handle_pseudo_stay_silent(ls, pseudo_call, step)

    if pseudo_call.name == LIST_DISPATCHES_TOOL_NAME:
        return await _handle_pseudo_list_dispatches(ls, pseudo_call)

    if pseudo_call.name == CANCEL_DISPATCH_TOOL_NAME:
        return await _handle_pseudo_cancel_dispatch(ls, pseudo_call, args)

    if pseudo_call.name == AWAIT_DISPATCHES_TOOL_NAME:
        # 正常由 _handle_tool_calls 收到本轮最后再挂起；走到这里说明是从别的路径进来的，只补一个结果保住配对
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(_AWAIT_NOT_SENT[WAIT_ABORTED]))
        return None

    _fixed_dispatch_target = _dispatch_target_from_tool_name(pseudo_call.name)
    if _fixed_dispatch_target:
        return await _handle_pseudo_dispatch(ls, {**args, "target": _fixed_dispatch_target}, pseudo_call)

    # ---- 终止型伪工具：finish / ask / switch_node ----

    if pseudo_call.name in ("finish", "ask"):
        # [2026-05-07] 正常 finish 重新按真实 API 工具处理。
        terminal_name = str(pseudo_call.name or "").strip()
        result_text = str(args.get("text") or "").strip()

        if not result_text:
            _emit_pseudo_tool_result(
                ls, pseudo_call,
                f'❌ REJECTED: {terminal_name}() called with empty text. Your visible content MUST go '
                f'in the {terminal_name} tool\'s `text` parameter, NOT in free prose outside tool calls. '
                'Free prose is never delivered to the user. Put your actual answer/question/data '
                f'in text and call {terminal_name} again.',
            )
            return None

        # None 表示模型没点名附件，走下面的自动转发；点名了就只发点名且放行的，点名的都已发过或被拒时一个都不发
        explicit_paths: list[str] | None = None
        refused: list[RefusedAttachment] = []
        if terminal_name == "finish":
            _named_paths, _path_problem = _named_attachment_paths(args)
            if _path_problem:
                _emit_pseudo_tool_result(
                    ls, pseudo_call, f"❌ REJECTED: {_path_problem}改好后再调用一次 {terminal_name}。",
                )
                return None
            if _named_paths is not None:
                explicit_paths = []
                # [2026-07-08] Drop paths already auto-forwarded via dispatch_attachment
                # (async draw images), so finish() cannot re-send the same files.
                _selected_paths = _reject_already_sent_dispatch_paths(ls, _named_paths)
                if _selected_paths:
                    explicit_paths, refused = await screen_model_attachment_paths(ls, pseudo_call, _selected_paths)
        _emit_pseudo_tool_result(ls, pseudo_call, _ok_with_refusals(refused))

        ctx_ref = _persist_ctx(ls, step + 1)
        summary_text = str(args.get("summary") or "").strip()
        final_atts = []
        if terminal_name == "finish":
            if explicit_paths is not None:
                if explicit_paths:
                    final_atts = await asyncio.to_thread(
                        _select_attachments,
                        list(ls.collected_attachments), explicit_paths,
                        workspace_root=ls.rctx.workspace_root,
                        session_id=ls.rctx.session_id,
                    )
            elif ls.tool_produced_attachments:
                final_atts = list(ls.tool_produced_attachments)
            else:
                # [2026-07-07] Async dispatch_result attachments are child output
                # (e.g. NovelAI generated images), not normal user-uploaded input.
                # The caller LLM often replies with a textual summary like "image
                # generated" without explicitly passing attachment_paths, which used
                # to drop the file before it reached QQ. Auto-forward only
                # for dispatch_result payloads so ordinary image-analysis turns do
                # not echo the user's pictures back.
                final_atts = _dispatch_result_input_attachments(ls)
        if short_reply(getattr(ls.rctx, "task_context", {}) or {}):
            final_atts = []
        result_payload = {
            "summary": summary_text,
            "text": result_text,
        }
        if terminal_name == "finish":
            result_payload["attachments"] = final_atts
        return TaskAction(
            action=ACTION_ASK if terminal_name == "ask" else ACTION_FINISH,
            node_id=ls.node.id,
            result=result_payload,
            context_ref=ctx_ref,
            summary=_short(summary_text, 240) if summary_text else "",
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    # switch_node 也需要 ctx_ref，单独计算
    if pseudo_call.name == "switch_node":
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        route_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
        try:
            response = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/switch_node",
                json={"target_node_id": switch_target},
            )
            failure = _http_failure(response)
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
        if failure:
            # 没切成就别结束本轮：会话还在当前节点手里，让模型知道并接着处理
            logger.warning("switch_node to %r failed: %s", switch_target, failure)
            _emit_pseudo_tool_result(
                ls, pseudo_call,
                f"Error: 切换节点失败（{failure}），会话仍由当前节点处理，请继续完成本轮任务。",
            )
            return None
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        ctx_ref = _persist_ctx(ls, step + 1)
        await ls.rctx.emit_event("node_switch", {
            "target_node_id": switch_target,
            "node_id": ls.node.id,
        })
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": switch_text,
                "attachments": list(ls.tool_produced_attachments),
            },
            context_ref=ctx_ref,
            summary=f"switch → {switch_target or 'default'}",
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    return None  # 未知伪工具，按非终止处理


async def _handle_pseudo_compact(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理 compact_context 伪工具。可能返回 DISPATCH action。"""
    _manual_keep = ls.compact_keep_recent
    try:
        _kr_arg = pseudo_call.arguments.get("keep_recent")
        if _kr_arg is not None:
            _manual_keep = int(_kr_arg)
    except (TypeError, ValueError):
        pass
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step, "manual": True})
        _, conversation_text = build_compaction_plan(
            ls.messages, keep_recent=_manual_keep,
            keep_recent_tokens=(
                0 if _manual_keep != ls.compact_keep_recent
                else int(getattr(ls, "compact_keep_recent_tokens", 0) or 0)
            ),
        )
        if conversation_text.strip():
            # Dispatch 路径：写 tool_result 后退出循环
            _emit_pseudo_tool_result(ls, pseudo_call, "compacting...")
            ctx_ref = _persist_ctx(ls, step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input={
                    "instruction": conversation_text,
                    "_compact_dispatch": True,
                    "context_mode": "fresh",
                    "_compact_keep_recent": _manual_keep,
                    # 手动压缩也要带上尺寸参数，否则 supervisor 收缩时回落到硬编码默认值。
                    # 模型显式给了 keep_recent 就尊重段数语义，不让 token 预算把它盖掉。
                    "_compact_keep_recent_tokens": (
                        0 if _manual_keep != ls.compact_keep_recent
                        else int(getattr(ls, "compact_keep_recent_tokens", 0) or 0)
                    ),
                    "_compact_threshold_tokens": ls.compact_threshold,
                    # 不带这个字段，supervisor 会回落到 parent_session_id：子节点手动压缩时
                    # instruction 是子节点自己的对话，摘要却整段替换父会话的历史。
                    "target_session_id": compact_target_session_id(ls),
                    "_system_task": True,
                    "use_context": False,
                },
            )
        else:
            _emit_pseudo_tool_result(ls, pseudo_call, "skipped: no compressible content")
    except Exception as compact_err:
        await ls.rctx.emit_event("compact_failed", {"node_id": ls.node.id, "step": step, "error": str(compact_err)})
        _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {compact_err}")
    return None


async def _handle_pseudo_preempt_task(ls: _LoopState, pseudo_call, args: dict) -> None:
    """处理 preempt_task 伪工具。始终返回 None（非终止）。

    task_id 可以是完整 id、id 前缀或节点名，由 supervisor 只在调用方的会话家族里认。
    """
    result = await _preempt_in_family(
        ls, str(args.get("task_id") or "").strip(), str(args.get("message") or "").strip(),
    )
    _emit_pseudo_tool_result(ls, pseudo_call, result)
    return None


async def _preempt_in_family(ls: _LoopState, ref: str, message: str) -> str:
    if not ref:
        return "task_id 不能为空"
    caller_task_id = str(getattr(ls.rctx, "task_id", "") or "").strip()
    if not caller_task_id:
        return "找不到发起这次调用的任务，没法确定该在哪些任务里找"
    supervisor_url = ls.rctx.supervisor_url
    try:
        found = await ls.rctx.http.get(
            f"{supervisor_url}/v1/tasks/{caller_task_id}/family/resolve", params={"ref": ref},
        )
        if found.status_code == 404:
            return (
                f"这段对话和它派出去的任务里没有能插话的 '{ref}'："
                "不存在、已结束、正挂起等子任务，或者是工具任务、系统任务"
            )
        if found.status_code == 409:
            return _ambiguous_preempt_target(ref, found)
        if found.status_code != 200:
            return f"查询任务 '{ref}' 失败: HTTP {found.status_code}"
        target = found.json()
        if not isinstance(target, dict):
            target = {}
        target_id = str(target.get("task_id") or "").strip()
        if not target_id:
            return f"查询任务 '{ref}' 失败: 没有返回任务 ID"
        label = f"{target.get('node_id') or 'task'}({target_id[:8]})"
        body: dict[str, Any] = {"caller_task_id": caller_task_id}
        if message:
            body["message"] = message
        response = await ls.rctx.http.post(f"{supervisor_url}/v1/tasks/{target_id}/preempt", json=body)
    except Exception as error:
        return f"调用失败 {error}"
    if response.status_code == 200:
        if message:
            return f"已向 task {label} 注入追加指令，子任务继续执行"
        return f"已标记 task {label} 为 preempt，等待优雅退出"
    if response.status_code == 404:
        return f"task {label} 刚刚结束，或挂起去等子任务了，这次没有生效"
    return f"API 返回 {response.status_code}"


def _ambiguous_preempt_target(ref: str, response: Any) -> str:
    """对上多个任务时列出候选，让模型换成其中一个 ID 前缀再调。"""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    detail = detail if isinstance(detail, dict) else {}
    candidates = detail.get("candidates")
    lines = [f"'{ref}' 对上了多个正在运行的任务，task_id 换成下面某一个 ID 前缀再调用："]
    listed = 0
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, dict) and item.get("task_id"):
            lines.append(f"- {item['task_id']}（{item.get('node_id') or '?'}，{item.get('status') or '?'}）")
            listed += 1
    total = detail.get("total")
    if isinstance(total, int) and total > listed:
        lines.append(f"一共 {total} 个，这里只列了 {listed} 个。")
    return "\n".join(lines)


def _target_is_persistent(ls: _LoopState, target: str) -> bool:
    """读取 dispatch 目标节点的 persistent 声明，用于推导默认 context_mode。

    [AutoC 2026-07-09] Why: 只有声明为长期节点（persistent=true）的 target
    才应默认 accumulate；其余节点默认 fresh。How: 复用 engine.node.load_node
    的同一搭载逻辑（system_nodes、config/nodes、defaults/nodes 的搜索顺序），失败时保守返回
    False。Purpose: 避免把普通工具型/一次性节点默认持久化。
    """
    tid = str(target or "").strip()
    if not tid:
        return False
    try:
        from ..node import load_node
        _n = load_node(Path(ls.rctx.workspace_root), tid)
        return bool(_n is not None and getattr(_n, "persistent", False))
    except Exception:
        return False


@dataclass(frozen=True)
class DispatchRequest:
    """一次委派调用校验过的参数：异步和 wait 两条路用同一套目标、上下文模式、附件和隔离规则。

    continue_from 非空时 continuation 是 supervisor 核对句柄后给的子会话信息，子节点接着那份历史做。
    isolation 是 ISOLATION_WORKTREE 时子任务在自己的 git worktree 里做，repo 是要隔离的仓库（空串是工作区所在的仓库）。
    """

    target: str
    instruction: str
    context_mode: str
    context_key: str
    attachments: list[dict[str, Any]]
    refused_attachments: list[RefusedAttachment]
    continue_from: str = ""
    continuation: dict[str, Any] = field(default_factory=dict)
    isolation: str = ""
    repo: str = ""

    def isolation_fields(self) -> dict[str, str]:
        """写进批次项、dispatch_origin 的隔离字段，supervisor 按 dispatch_isolation、dispatch_repo 读；不隔离时是空表。"""
        if not self.isolation:
            return {}
        fields = {ISOLATION_KEY: self.isolation}
        if self.repo:
            fields[REPO_KEY] = self.repo
        return fields


def _unfilled(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _isolation_request(ls: _LoopState, target: str, args: dict) -> tuple[str, str, str]:
    """模型填的 isolation、repo：返回 (隔离方式, 仓库, 拒绝原因)。

    supervisor 把认不出的 isolation 一律当不隔离，所以写错的取值只能在这里拦下；repo 指得对不对由 supervisor 判。
    """
    raw_isolation = args.get(ISOLATION_KEY)
    raw_repo = args.get(REPO_KEY)
    if _unfilled(raw_isolation) and _unfilled(raw_repo):
        return "", "", ""
    current = task_isolation(getattr(ls.rctx, "task_context", None))
    if current:
        return "", "", nested_isolation_refusal(current)
    if not _unfilled(raw_isolation):
        chosen = raw_isolation.strip().lower() if isinstance(raw_isolation, str) else ""
        if chosen != ISOLATION_WORKTREE:
            return "", "", (
                f"没有派出：{ISOLATION_KEY} 只能填 {ISOLATION_WORKTREE}（让目标节点在自己的 git worktree 里做），"
                f"不要隔离就不填；收到的是 {_short(json.dumps(raw_isolation, ensure_ascii=False, default=str), 80)}。"
            )
    if not _unfilled(raw_repo) and not isinstance(raw_repo, str):
        return "", "", f"没有派出：{REPO_KEY} 要填仓库的绝对路径。"
    if _unfilled(raw_isolation):
        return "", "", (
            f"没有派出：{REPO_KEY} 只在 {ISOLATION_KEY}={ISOLATION_WORKTREE} 时有效；"
            f"要隔离就同时填 {ISOLATION_KEY}: {ISOLATION_WORKTREE}，不隔离就去掉 {REPO_KEY}。"
        )
    if target not in (getattr(ls, "isolating_targets", None) or set()):
        return "", "", (
            f"没有派出：{target} 的节点配置没有写 {ISOLATION_KEY}: {ISOLATION_WORKTREE}，委派它不能要求隔离；"
            f"去掉 {ISOLATION_KEY} 和 {REPO_KEY} 再派。"
        )
    return ISOLATION_WORKTREE, raw_repo.strip() if isinstance(raw_repo, str) else "", ""


async def _resolve_continuation(
    ls: _LoopState, raw_handle: str, target: str, *, wait: bool,
) -> tuple[dict[str, Any], str]:
    """请 supervisor 核对 continue_from 的句柄：属于调用方的会话家族、目标节点一致、此刻没在跑。返回 (子会话信息, 拒绝原因)。"""
    handle = parse_handle(raw_handle)
    if handle is None:
        return {}, "continue_from 的句柄格式不对：要填委派结果里给的 task_id@session_id。"
    caller_task_id = str(getattr(ls.rctx, "task_id", "") or "").strip()
    if not caller_task_id:
        return {}, "找不到发起这次委派的任务，没法核对 continue_from 的句柄。"
    try:
        response = await ls.rctx.http.get(
            f"{ls.rctx.supervisor_url}/v1/tasks/{caller_task_id}/continuation",
            params={"handle": str(handle), "target": target, "wait": "true" if wait else "false"},
        )
    except Exception as error:
        return {}, f"continue_from 核对失败：{type(error).__name__}: {error}"
    failure = _http_failure(response)
    if failure:
        return {}, f"continue_from 核对失败：{failure}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return {}, "continue_from 核对失败：supervisor 没有给出结果。"
    if not body.get("ok"):
        return {}, str(body.get("message") or body.get("reason") or "continue_from 被拒绝。")
    return body, ""


async def _resolve_dispatch_request(
    ls: _LoopState, args: dict, pseudo_call: Any, *, wait: bool = False,
) -> tuple[DispatchRequest | None, str]:
    """校验委派参数，返回 (请求, 拒绝原因)；目标不在委派清单里、隔离参数不对、continue_from 的句柄不能用时请求为 None。"""
    # 委派清单加载时已换成现在的 id；模型照着旧历史写出旧 id，也换过来再比
    target = canonical_node_id(Path(ls.rctx.workspace_root), str(args.get("target") or ""))

    # 目标是从工具名反查出来的，而反查表 _DISPATCH_TOOL_REVERSE 是进程内全局的，
    # 别的节点注册过的目标这里一样查得到，查不到还会原样退回字符串。
    # 不复核的话，白名单只管住了「模型看得见什么」，管不住「它敢写什么」——
    # 编一个名字就能把活派给委派清单外的节点，连带用上那个节点的全部工具。
    if target not in ls.allowed_dispatch_targets:
        logger.warning(
            "node %s attempted dispatch to non-delegate target: %s (allowed: %s)",
            ls.node.id, target, sorted(ls.allowed_dispatch_targets),
        )
        return None, (
            f"'{target}' is not in this node's delegate_targets. "
            f"Allowed: {sorted(ls.allowed_dispatch_targets) or '(none)'}"
        )

    # [AutoC 2026-07-09] Why: dispatch 的默认 context_mode 原先写死 accumulate，
    # 导致所有未显式指定模式的子节点都长期复用 child session（历史污染 / 成本上升）。
    # How: 显式传参优先；未传时按 target 节点的 persistent 声明推导——
    # persistent=true → accumulate（长期人格 / 专属助手 / 立项的长期维护 agent），
    # 否则 fresh（一次性搜索、一次性工程执行、审查/debug 等）。
    # Purpose: 让 persistent 字段真正驱动默认行为，语义与 node.persistent 一致。
    _raw_ctx_mode = args.get("context_mode")
    to_self = target == ls.node.id
    raw_continue = str(args.get(CONTINUE_FROM_KEY) or "").strip()
    written_mode = parse_context_mode(_raw_ctx_mode)
    if to_self and not raw_continue and written_mode not in ("", *OWN_SESSION_MODES):
        # 子会话按（祖先会话，目标节点，context_key）找：沿用会话的话，同时派出的几份、以及它们再派出的会落进同一个会话
        return None, (
            f"没有派出：委派给自己时 context_mode 只能是 fresh 或 fork，收到的是 "
            f"{_short(json.dumps(_raw_ctx_mode, ensure_ascii=False, default=str), 40)}；"
            "沿用会话的话，同时派出的几份、以及它们再派出的，都会落进同一个会话。"
            "要接着之前派出的那一份，用 continue_from 带上它的句柄。"
        )
    # 接着之前的子 agent 时模式沿用它当初的，写错了也用不上
    if written_mode is None and not raw_continue:
        return None, context_mode_refusal(_raw_ctx_mode)
    if written_mode:
        ctx_mode = written_mode
    elif to_self:
        # 委派给自己是拆成几份各做各的，持久节点也不沿用会话
        ctx_mode = "fresh"
    else:
        ctx_mode = "accumulate" if _target_is_persistent(ls, target) else "fresh"
    context_key = str(args.get("context_key") or "").strip()
    isolation, repo, refusal = _isolation_request(ls, target, args)
    if refusal:
        return None, refusal
    continuation: dict[str, Any] = {}
    if raw_continue:
        continuation, refusal = await _resolve_continuation(ls, raw_continue, target, wait=wait)
        if not continuation:
            return None, refusal
        # 接着的是那个子会话自己的历史：模式沿用它当初的，context_key 不再参与找会话
        ctx_mode = str(continuation.get("context_mode") or "").strip() or ctx_mode
        context_key = ""
    named_paths, path_problem = _named_attachment_paths(args)
    if path_problem:
        return None, path_problem
    attachment_paths, refused_attachments = await screen_model_attachment_paths(
        ls, pseudo_call, named_paths or [], data_dir_only=True,
    )
    # 接着一个隔离过的子 agent 时 supervisor 沿用它的副本、repo 指向别的仓库时另建，隔离字段照原样交过去由它定
    return DispatchRequest(
        target=target,
        instruction=str(args.get("instruction") or "").strip(),
        context_mode=ctx_mode,
        context_key=context_key,
        attachments=_paths_to_attachments(attachment_paths, ls.rctx.workspace_root),
        refused_attachments=list(refused_attachments),
        continue_from=str(parse_handle(raw_continue)) if continuation else "",
        continuation=continuation,
        isolation=isolation,
        repo=repo,
    ), ""


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch:{target_id} 动态伪工具（异步委派）。始终返回 None（非终止）。

    委派是一条带 dispatch_origin 回调信息的 inbound（POST /v1/inbound）：子节点在自己的 agent: 会话里跑，
    和用户消息共用会话、入口分支和对话存储；做完后结果交回发起方会话：发起方那一轮还没做完时先留给它，结束后作为新消息交回。
    """
    # wait 写了认不出的值也会走到这里：按异步派出去的话，想等结果的调用方拿到的只是一个句柄
    if _wait_flag(args) is None:
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(_wait_value_refusal(args.get("wait"))))
        return None
    request, refusal = await _resolve_dispatch_request(ls, args, pseudo_call)
    if request is None:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": refusal,
        }, ensure_ascii=False))
        return None
    if not request.instruction:
        # supervisor 对空消息不建任务，不拦的话这里会报「已派出」却什么也没派
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(_EMPTY_INSTRUCTION))
        return None
    target = request.target
    instr = request.instruction
    ctx_mode = request.context_mode
    ctx_key = request.context_key or None
    attachments = request.attachments
    refused_attachments = request.refused_attachments

    # 获取父 session 信息
    parent_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
    parent_conv_key = str(
        ls.rctx.task_context.get("route_conversation_key")
        or ls.rctx.task_context.get("conversation_key")
        or ""
    ).strip()
    parent_channel = ls.rctx.task_context.get("channel", "internal")
    target_node_id = target

    # Why: 子节点(如 cmd_reviewer 执行 curl 读网页)在 dispatch 后
    # 丢失了发起者身份，platform_auth.is_admin 变成 False，导致管理员发起的读网页 curl
    # 仍被要求审批。How: 从父 task_context 透传 platform_auth 与 route_hints 到 dispatch
    # inbound。Purpose: 子节点的策略判定沿用发起者身份，管理员任务的普通命令自动放行。
    parent_platform_auth = ls.rctx.task_context.get("platform_auth")
    if not isinstance(parent_platform_auth, dict):
        parent_platform_auth = {}
    parent_route_hints = ls.rctx.task_context.get("route_hints")
    if not isinstance(parent_route_hints, dict):
        parent_route_hints = {}

    # 生成 conversation_key
    if request.continuation:
        # 接着上一个子 agent：投进它原来那个会话，入口分支会带着它的全部历史
        conv_key = str(request.continuation.get("conversation_key") or "").strip()
        if not conv_key:
            _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
                "success": False,
                "error": "continue_from 指向的子会话找不到会话键，没法接着派；需要的话重新委派。",
            }, ensure_ascii=False))
            return None
    elif ctx_mode in OWN_SESSION_MODES:
        conv_key = f"agent:{target_node_id}:{parent_conv_key}:{uuid.uuid4()}"
    else:  # accumulate
        if ctx_key:
            conv_key = f"agent:{target_node_id}:{ctx_key}:{parent_conv_key}"
        else:
            conv_key = f"agent:{target_node_id}:{parent_conv_key}"

    # 构造 inbound payload
    inbound_payload: dict[str, Any] = {
        "channel": parent_channel,
        "conversation_key": conv_key,
        "text": instr,
        "entry_node_id": target_node_id,
        "use_context": True,
        "attachments": attachments or [],
        "platform_auth": dict(parent_platform_auth),
        "route_hints": dict(parent_route_hints),
        "dispatch_origin": {
            "parent_session_id": parent_session_id,
            "caller_node_id": ls.node.id,
            "parent_conversation_key": parent_conv_key,
            "context_mode": ctx_mode,
        },
        "dispatch_context_mode": ctx_mode,
        # 没等到应答时带着它重发，supervisor 认出来就交回第一次建的任务，不会派出第二个
        "delivery_id": uuid.uuid4().hex,
    }
    parent_generation = getattr(ls.rctx, "session_generation", 0)
    if parent_generation:
        inbound_payload["dispatch_origin"]["parent_session_generation"] = parent_generation
    caller_task_id = str(getattr(ls.rctx, "task_id", "") or "")
    if caller_task_id:
        # 结果回来时，转述它的那一轮靠这个找回发起任务的输出说明。
        inbound_payload["dispatch_origin"]["caller_task_id"] = caller_task_id
    inbound_payload["dispatch_origin"].update(request.isolation_fields())
    if request.continue_from:
        inbound_payload["dispatch_origin"][CONTINUE_FROM_KEY] = request.continue_from
    elif ctx_mode == "fork":
        # 和同步 fork 一样从调用方自己写对话的会话复制：入口分支上这一轮还没合并的内容也要带上
        inbound_payload["dispatch_fork_from_session"] = _runtime_session_id(ls)
        # supervisor 建任务时当场复制，排在写入队列里的消息得先落盘
        await flush_conversation_writes(ls.rctx)

    outcome = await post_until_answered(ls.rctx.http, f"{ls.rctx.supervisor_url}/v1/inbound", inbound_payload)
    dispatch_result = _async_dispatch_outcome(target, outcome, isolation=request.isolation_fields())
    if dispatch_result.get("success") and args.get(ITEM_TIMEOUT_KEY) not in (None, ""):
        dispatch_result["note"] = (
            f"timeout_sec 只对 wait=true 的委派有效，这次没有用上；要限时等异步委派，用 {AWAIT_DISPATCHES_TOOL_NAME} 的 timeout_sec。"
        )
    if refused_attachments:
        dispatch_result["refused_attachments"] = [item.as_dict() for item in refused_attachments]
    _emit_pseudo_tool_result(ls, pseudo_call, json.dumps(dispatch_result, ensure_ascii=False))
    return None


def _runtime_session_id(ls: _LoopState) -> str:
    """调用方这一程写对话的会话：子节点是它的 child session，入口任务是它的入口分支。"""
    return str(getattr(ls.rctx, "child_session_id", "") or ls.rctx.session_id or "").strip()


def _isolated_note(isolation: dict[str, str]) -> str:
    if not isolation:
        return ""
    repo = isolation.get(REPO_KEY)
    return f"（在隔离副本里执行，仓库 {repo}）" if repo else "（在隔离副本里执行）"


def _async_dispatch_outcome(target: str, outcome: RetriedPost, *, isolation: dict[str, str]) -> dict[str, Any]:
    """异步委派请求的结局交给模型：派出了、被拒了、确定没派出，或者重发用完仍不知道派没派出。"""
    answer = outcome.answer
    if outcome.settled:
        if getattr(answer, "status_code", None) != 200:
            return {"success": False, "error": refusal_reason(answer)}
        try:
            body = answer.json()
        except Exception as exc:
            # 回了 200 就是建了任务，读不出应答也按派出算，只是没有句柄
            logger.warning("async dispatch to %s answered 200 with an unreadable body: %s", target, exc)
            body = {}
        if isinstance(body, dict) and body.get("deduped"):
            logger.info("async dispatch to %s was resent; supervisor answered with the task it already created", target)
        return _async_dispatch_result(target, body, isolation=isolation)
    if outcome.unanswered:
        return {
            "success": False,
            "status": "unconfirmed",
            "error": (
                f"不确定有没有派出：请求发出后一直没等到 supervisor 的应答（{outcome.error}）。"
                f"先用 {LIST_DISPATCHES_TOOL_NAME} 看有没有这个委派，没有再重新派，别直接重发。"
            ),
        }
    if answer is not None:
        return {"success": False, "error": refusal_reason(answer)}
    return {"success": False, "error": f"没有派出：连不上 supervisor（{outcome.error}）。"}


def _async_dispatch_result(target: str, body: Any, *, isolation: dict[str, str] | None = None) -> dict[str, Any]:
    """异步委派派出后的工具结果：句柄由 supervisor 建出的任务 id 和它所在的 agent: 会话拼成；要求了隔离的注明在隔离副本里执行。"""
    data = body if isinstance(body, dict) else {}
    session_id = str(data.get("session_id") or "").strip()
    handle = format_handle(data.get("task_id"), session_id)
    isolated = dict(isolation or {})
    where = _isolated_note(isolated)
    result: dict[str, Any] = {"success": True, "session_id": session_id}
    if handle:
        result["handle"] = handle
        result["status"] = "dispatched"
        result["message"] = (
            f"已异步委派给 {target}{where}。结果回来时你这一轮还没做完，就先留给这一轮：要在本任务里拿结果，"
            f"用 {AWAIT_DISPATCHES_TOOL_NAME} 带上这个 handle；没收取的等这一轮结束后，和其他回来的结果合成一条消息交回。"
            f"用 {LIST_DISPATCHES_TOOL_NAME} 查看进度，用 {CANCEL_DISPATCH_TOOL_NAME} 取消。"
        )
    else:
        result["message"] = f"已异步委派给 {target}{where}，结果等你这一轮结束后作为消息交回。"
    result.update(isolated)
    return result


WAIT_DISPATCHED = "dispatched"
WAIT_INTERCEPTED = "intercepted"
WAIT_PREEMPTED = "preempted"
WAIT_CANCELLED = "cancelled"
WAIT_ABORTED = "aborted"

_WAIT_NOT_SENT = {
    WAIT_INTERCEPTED: "没有派出：用户刚发来了新消息。先看新消息，还需要的话再重新委派。",
    WAIT_PREEMPTED: "没有派出：任务被打断了。",
    WAIT_CANCELLED: "没有派出：任务已取消。",
    WAIT_ABORTED: "没有派出：这一轮提前结束了，还需要的话重新委派。",
}
_AWAIT_NOT_SENT = {
    WAIT_INTERCEPTED: "没有开始等：用户刚发来了新消息。先看新消息，还需要的话再调用 await_dispatches。",
    WAIT_PREEMPTED: "没有开始等：任务被打断了。",
    WAIT_CANCELLED: "没有开始等：任务已取消。",
    WAIT_ABORTED: "没有开始等：这一轮提前结束了，还需要的话再调用 await_dispatches。",
}
_EMPTY_INSTRUCTION = "没有派出：instruction 不能为空，写清楚要目标节点做什么。"
_BAD_ITEM_TIMEOUT = "没有派出：timeout_sec 要填正数秒数；不想限时就不填。"


def _wait_flag(arguments: Any) -> bool | None:
    """委派调用写的 wait，读法同 switch_arg：没写（缺省、null、空串）是 False，认不出的给 None。"""
    try:
        return switch_arg(arguments if isinstance(arguments, dict) else {}, "wait", False)
    except SwitchValueError:
        return None


def _wait_value_refusal(raw: Any) -> str:
    return f"没有派出：{switch_refusal('wait', raw)}；要等结果就填 true，不等就填 false 或不填。"


def is_wait_dispatch(call: Any) -> bool:
    """带 wait=true 的委派调用：不当场派出，等本轮其它调用跑完再成批同步派出。"""
    if not _dispatch_target_from_tool_name(str(getattr(call, "name", "") or "")):
        return False
    return _wait_flag(getattr(call, "arguments", None)) is True


def is_await_dispatch(call: Any) -> bool:
    """await_dispatches 调用：和 wait 委派一样等本轮其它调用跑完，再挂起本任务。"""
    return str(getattr(call, "name", "") or "").strip() == AWAIT_DISPATCHES_TOOL_NAME


def _wait_refusal(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _requested_timeout(raw: Any) -> tuple[int | None, bool]:
    """模型填的 timeout_sec：(秒数, 写得对不对)。没填（缺省、null、空串）给 (None, True)。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, True
    seconds = parse_timeout_sec(raw)
    return seconds, seconds is not None


@dataclass
class WaitDispatch:
    """一个通过校验、等着和同轮其它 wait 委派一起派出的调用。isolation 是它要求隔离时写进批次项的那几个字段。"""

    call: Any
    target: str
    item: dict[str, Any]
    refused_attachments: list[RefusedAttachment] = field(default_factory=list)
    timeout_sec: int = 0
    isolation: dict[str, str] = field(default_factory=dict)

    def result_text(self, outcome: str) -> str:
        """这次调用的工具结果：派出去了就是占位，真正的结果在恢复后的下一条消息里；没派出去说明原因。"""
        if outcome == WAIT_DISPATCHED:
            limit = f"（最多等 {self.timeout_sec} 秒，到点没做完按失败交回）" if self.timeout_sec else ""
            payload: dict[str, Any] = {
                "success": True,
                "status": "waiting",
                "message": (
                    f"已派出给 {self.target}{_isolated_note(self.isolation)}，正在等待结果{limit}；结果会在下一条消息给出。"
                ),
                **self.isolation,
            }
        else:
            payload = {"success": False, "error": _WAIT_NOT_SENT.get(outcome, _WAIT_NOT_SENT[WAIT_ABORTED])}
        if self.refused_attachments:
            payload["refused_attachments"] = [item.as_dict() for item in self.refused_attachments]
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class WaitDispatchPlan:
    items: list[WaitDispatch] = field(default_factory=list)
    rejected: list[tuple[Any, str]] = field(default_factory=list)


async def prepare_wait_dispatches(ls: _LoopState, calls: list[Any]) -> WaitDispatchPlan:
    """校验本轮的 wait 委派并组成批次项，目标、上下文模式、附件和隔离的规则与异步委派共用。

    简短回应和系统任务里的、超出每轮上限的、参数不合格的调用都进 rejected，带着给模型看的原因。
    """
    plan = WaitDispatchPlan()
    task_context = getattr(ls.rctx, "task_context", None) or {}
    if _is_short_reply(task_context):
        plan.rejected.extend((call, _wait_refusal("本轮为简短回应，请仅调用 finish。")) for call in calls)
        return plan
    if task_context.get("is_system_task"):
        plan.rejected.extend(
            (call, _wait_refusal("没有派出：系统任务不能挂起等待子节点的结果；不需要结果就去掉 wait 异步派出。"))
            for call in calls
        )
        return plan
    shared_contexts: set[tuple[str, str]] = set()
    continued_sessions: set[str] = set()
    timeouts = DispatchTimeouts.from_config(getattr(ls, "runtime_cfg", None))
    for call in calls:
        # 按实际收进这一批的计：前面因为参数不对被拒的不占名额
        if len(plan.items) >= MAX_DISPATCH_BATCH_ITEMS:
            plan.rejected.append((call, _wait_refusal(
                f"没有派出：同一轮最多 {MAX_DISPATCH_BATCH_ITEMS} 个 wait 委派，这一个超出了；等这批结果回来后再派。"
            )))
            continue
        arguments = dict(getattr(call, "arguments", None) or {})
        requested_timeout, timeout_ok = _requested_timeout(arguments.get(ITEM_TIMEOUT_KEY))
        if not timeout_ok:
            plan.rejected.append((call, _wait_refusal(_BAD_ITEM_TIMEOUT)))
            continue
        target = _dispatch_target_from_tool_name(str(getattr(call, "name", "") or ""))
        request, refusal = await _resolve_dispatch_request(ls, {**arguments, "target": target}, call, wait=True)
        if request is None:
            plan.rejected.append((call, _wait_refusal(refusal)))
            continue
        if not request.instruction:
            plan.rejected.append((call, _wait_refusal(_EMPTY_INSTRUCTION)))
            continue
        if request.continue_from:
            continued = str(request.continuation.get("session_id") or request.continue_from)
            if continued in continued_sessions:
                plan.rejected.append((call, _wait_refusal(
                    "没有派出：同一轮已经有一个委派用 continue_from 接着这个子 agent，两个同时跑会写乱它的历史；等结果回来后再派。"
                )))
                continue
            continued_sessions.add(continued)
        # fresh / fork 每项各开一个子会话；accumulate 同一目标同一 context_key 共用一个，同时跑会写乱它
        elif request.context_mode not in OWN_SESSION_MODES:
            shared = (request.target, request.context_key)
            if shared in shared_contexts:
                plan.rejected.append((call, _wait_refusal(
                    f"没有派出：同一轮已经有一个用同一份上下文的 {request.target} 委派，两个同时跑会写乱这份上下文；"
                    "等结果回来后再派，或者改用 fresh、换一个 context_key。"
                )))
                continue
            shared_contexts.add(shared)
        item: dict[str, Any] = {
            "kind": "node",
            "target": request.target,
            "instruction": request.instruction,
            "context_mode": request.context_mode,
        }
        if request.context_key:
            item["context_key"] = request.context_key
        if request.continue_from:
            item[CONTINUE_FROM_KEY] = request.continue_from
        item.update(request.isolation_fields())
        if request.attachments:
            item["attachments"] = [dict(attachment) for attachment in request.attachments]
        call_id = str(getattr(call, "id", "") or "").strip()
        if call_id:
            item["call_id"] = call_id
        item_timeout = timeouts.clamp(requested_timeout) if requested_timeout is not None else 0
        if item_timeout:
            item[ITEM_TIMEOUT_KEY] = item_timeout
        plan.items.append(WaitDispatch(
            call=call, target=request.target, item=item,
            refused_attachments=list(request.refused_attachments),
            timeout_sec=item_timeout,
            isolation=request.isolation_fields(),
        ))
    return plan


@dataclass
class AwaitDispatch:
    """本轮通过校验的那次 await_dispatches：等本轮其它调用跑完，再挂起本任务。"""

    call: Any
    request: AwaitRequest
    timeout_sec: int
    adjusted: bool = False

    def result_text(self, outcome: str) -> str:
        """占位结果：开始等了就说在等什么，真正的结果在恢复后的下一条消息里；没开始等说明原因。"""
        if outcome != WAIT_DISPATCHED:
            return _wait_refusal(_AWAIT_NOT_SENT.get(outcome, _AWAIT_NOT_SENT[WAIT_ABORTED]))
        until = "全部结束" if self.request.mode == AWAIT_ALL else "任意一个结束"
        payload: dict[str, Any] = {
            "success": True,
            "status": "waiting",
            "message": (
                f"正在等 {len(self.request.handles)} 个异步委派，{until}或者等满 {self.timeout_sec} 秒就恢复；"
                "结果会在下一条消息给出。"
            ),
        }
        if self.adjusted:
            payload["note"] = f"timeout_sec 超出了允许的范围，按 {self.timeout_sec} 秒等。"
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class AwaitDispatchPlan:
    item: AwaitDispatch | None = None
    rejected: list[tuple[Any, str]] = field(default_factory=list)


def _await_request_from(call: Any, timeouts: DispatchTimeouts) -> tuple[AwaitDispatch | None, str]:
    """把一次 await_dispatches 调用的参数理成请求；参数不合格时给出原因。"""
    arguments = getattr(call, "arguments", None)
    arguments = arguments if isinstance(arguments, dict) else {}
    raw_handles = arguments.get("handles")
    if isinstance(raw_handles, str):
        raw_handles = [raw_handles]
    if not isinstance(raw_handles, list):
        return None, "没有开始等：handles 要填句柄列表，照抄异步委派返回的 handle。"
    handles: list[str] = []
    malformed: list[str] = []
    for raw in raw_handles:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        text = raw.strip() if isinstance(raw, str) else ""
        if not text or parse_handle(text) is None:
            malformed.append(_short(text, 80))
        elif text not in handles:
            handles.append(text)
    if malformed:
        return None, (
            "没有开始等：这些句柄格式不对，要照抄异步委派返回的 handle（task_id@session_id）："
            + "、".join(malformed)
        )
    if not handles:
        return None, "没有开始等：handles 不能为空，填异步委派返回的 handle。"
    if len(handles) > MAX_AWAIT_HANDLES:
        return None, f"没有开始等：一次最多等 {MAX_AWAIT_HANDLES} 个句柄，这次有 {len(handles)} 个；分几次等。"
    raw_mode = arguments.get("mode")
    mode = str(raw_mode or "").strip().lower() or AWAIT_ALL
    if mode not in AWAIT_MODES:
        return None, f"没有开始等：mode 只能是 {' 或 '.join(AWAIT_MODES)}。"
    requested, timeout_ok = _requested_timeout(arguments.get("timeout_sec"))
    if not timeout_ok:
        return None, f"没有开始等：timeout_sec 要填正数秒数；不填就最多等 {timeouts.await_default} 秒。"
    seconds = timeouts.clamp(requested) if requested is not None else timeouts.await_default
    request = AwaitRequest(
        handles=tuple(handles),
        mode=mode,
        timeout_sec=seconds if requested is not None else None,
        call_id=str(getattr(call, "id", "") or "").strip(),
    )
    return AwaitDispatch(
        call=call, request=request, timeout_sec=seconds, adjusted=requested is not None and requested != seconds,
    ), ""


async def prepare_await_dispatches(
    ls: _LoopState, calls: list[Any], *, has_wait_items: bool = False,
) -> AwaitDispatchPlan:
    """校验本轮的 await_dispatches：一轮只认一次，不能和 wait 委派同轮，简短回应和系统任务里不能挂起。"""
    plan = AwaitDispatchPlan()
    if not calls:
        return plan
    task_context = getattr(ls.rctx, "task_context", None) or {}
    blocked = ""
    if _is_short_reply(task_context):
        blocked = "本轮为简短回应，请仅调用 finish。"
    elif task_context.get("is_system_task"):
        blocked = "没有开始等：系统任务不能挂起等待异步委派的结果。"
    elif not ls.allowed_dispatch_targets:
        blocked = "没有开始等：这个节点没有委派目标，也就没有异步委派可等。"
    elif has_wait_items:
        blocked = "没有开始等：同一轮已经有 wait=true 的委派，它们的结果回来之后再调用 await_dispatches。"
    if blocked:
        plan.rejected.extend((call, _wait_refusal(blocked)) for call in calls)
        return plan
    timeouts = DispatchTimeouts.from_config(getattr(ls, "runtime_cfg", None))
    for call in calls:
        if plan.item is not None:
            plan.rejected.append((call, _wait_refusal(
                "没有开始等：同一轮只能调用一次 await_dispatches，要等的句柄写进同一次调用。"
            )))
            continue
        item, refusal = _await_request_from(call, timeouts)
        if item is None:
            plan.rejected.append((call, _wait_refusal(refusal)))
            continue
        plan.item = item
    return plan


def _dispatch_tools_refusal(ls: _LoopState) -> str:
    caller_task_id = str(getattr(ls.rctx, "task_id", "") or "").strip()
    if not ls.allowed_dispatch_targets:
        return "这个节点没有委派目标，也就没有异步委派可以查看或取消。"
    if not caller_task_id:
        return "找不到发起这次调用的任务，没法确定该在哪些委派里找。"
    return ""


async def _handle_pseudo_list_dispatches(ls: _LoopState, pseudo_call: Any) -> None:
    """list_dispatches：这段对话里还没结束的异步委派，加上最近结束的几个。始终返回 None。"""
    refusal = _dispatch_tools_refusal(ls)
    if refusal:
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(refusal))
        return None
    try:
        response = await ls.rctx.http.get(f"{ls.rctx.supervisor_url}/v1/tasks/{ls.rctx.task_id}/dispatches")
        failure = _http_failure(response)
        body = response.json() if not failure else None
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        body = None
    if failure or not isinstance(body, dict):
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(f"查询异步委派失败：{failure or 'supervisor 没有给出结果'}"))
        return None
    active = body.get("active") if isinstance(body.get("active"), list) else []
    finished = body.get("finished") if isinstance(body.get("finished"), list) else []
    listed: dict[str, Any] = {"success": True, "active": active, "finished": finished}
    active_total = int(body.get("active_total") or len(active))
    finished_total = int(body.get("finished_total") or len(finished))
    notes: list[str] = []
    if not active and not finished:
        notes.append("这段对话里现在没有异步委派。")
    if active_total > len(active):
        notes.append(f"还没结束的一共 {active_total} 个，这里列了最早的 {len(active)} 个。")
    if finished_total > len(finished):
        notes.append(f"结束的只列了最近 {len(finished)} 个。")
    if notes:
        listed["note"] = "".join(notes)
    _emit_pseudo_tool_result(ls, pseudo_call, json.dumps(listed, ensure_ascii=False))
    return None


async def _handle_pseudo_cancel_dispatch(ls: _LoopState, pseudo_call: Any, args: dict) -> None:
    """cancel_dispatch：取消这段对话里的一个异步委派，连同它派出去的子任务。始终返回 None。"""
    refusal = _dispatch_tools_refusal(ls)
    handle = str(args.get("handle") or "").strip()
    if not refusal and parse_handle(handle) is None:
        refusal = "handle 的格式不对：要照抄异步委派返回的句柄（task_id@session_id）。"
    if refusal:
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(refusal))
        return None
    try:
        response = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/{ls.rctx.task_id}/dispatches/cancel", json={"handle": handle},
        )
        failure = _http_failure(response)
        body = response.json() if not failure else None
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        body = None
    if failure or not isinstance(body, dict):
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(f"取消失败：{failure or 'supervisor 没有给出结果'}"))
        return None
    if not body.get("ok"):
        _emit_pseudo_tool_result(ls, pseudo_call, _wait_refusal(str(body.get("message") or "取消被拒绝。")))
        return None
    _emit_pseudo_tool_result(ls, pseudo_call, json.dumps(_cancel_outcome(body), ensure_ascii=False))
    return None


def _cancel_outcome(body: dict[str, Any]) -> dict[str, Any]:
    node = str(body.get("node_id") or "子节点")
    status = str(body.get("status") or "")
    cancelled = int(body.get("cancelled_count") or 0)
    stopping = int(body.get("stopping_count") or 0)
    finished = bool(body.get("already_finished"))
    # 计数里含委派本身；已经结束的委派没有算进去
    descendants = cancelled + stopping - (0 if finished else 1)
    if finished:
        message = f"{node} 这个委派已经结束了（{status or '已结束'}），它本身不用取消。"
    elif status == "cancelled":
        message = f"已取消 {node} 这个委派。"
    else:
        message = f"已通知 {node} 停下：它会在手上这一步做完后结束，不再交回结果。"
    if descendants > 0:
        message += f"它派出去还没结束的 {descendants} 个子任务一并取消。"
    return {
        "success": True,
        "handle": str(body.get("handle") or ""),
        "status": status,
        "cancelled_count": cancelled,
        "stopping_count": stopping,
        "message": message,
    }


async def _handle_pseudo_update_todos(ls: _LoopState, pseudo_call: Any, args: dict) -> None:
    """update_todos：整份替换本任务的待办清单，结果里回显清单，并发 todo_updated 事件。始终返回 None。"""
    if not getattr(ls, "todo_enabled", False):
        _emit_pseudo_tool_result(ls, pseudo_call, "Error: 这个节点没有启用待办清单，不能调用 update_todos。")
        return None
    try:
        items = parse_todo_items(args.get("items"))
    except TodoListError as error:
        _emit_pseudo_tool_result(ls, pseudo_call, f"Error: {error}")
        return None
    ls.todos = items
    ls.todos_restored = True
    _emit_pseudo_tool_result(ls, pseudo_call, render_update_result(items))
    await ls.rctx.emit_event("todo_updated", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "items": [dict(item) for item in items],
    })
    return None


def _stay_silent_enabled(ls: _LoopState) -> bool:
    """和工具表注入同一个条件：节点声明了 stay_silent: true，且不是系统任务。"""
    if (getattr(ls.rctx, "task_context", {}) or {}).get("is_system_task"):
        return False
    return bool(parse_bool((getattr(ls.node, "extra", None) or {}).get("stay_silent"), False))


def _ambient_turn(ls: _LoopState) -> bool:
    """这一轮是没人叫 bot、由它自己决定插不插话的那种。"""
    from ..conversation_routing import routing_meta

    return routing_meta(getattr(ls.rctx, "task_context", {}) or {}).get("delivery_purpose") == "ambient"


def _short_reply_only_finish(ls: _LoopState) -> str:
    if _stay_silent_enabled(ls) and _ambient_turn(ls):
        return f"本轮为简短回应：要说就调用 finish，不想接话就调用 {STAY_SILENT_TOOL_NAME}。"
    return "本轮为简短回应，请仅调用 finish。"


async def _handle_pseudo_stay_silent(ls: _LoopState, pseudo_call: Any, step: int) -> TaskAction | None:
    """不发言：只在主动插话的任务里生效，任务照常结束但不产出回复，结果带不发言标记一路传到适配层。

    有人直接找 bot 的任务调用它会被拒，结果告诉模型照常回答，任务继续。
    """
    if not _stay_silent_enabled(ls):
        _emit_pseudo_tool_result(ls, pseudo_call, "Error: 这个节点不能选择不发言，请用 finish 正常回复。")
        return None
    if not _ambient_turn(ls):
        _emit_pseudo_tool_result(
            ls, pseudo_call,
            "Error: 这一轮是有人在直接找你（@、回复、点名或前缀），不能选择不发言，请正常回答并用 finish 交付。",
        )
        return None
    # 和 finish 一样先看有没有新消息要注入：直接结束的话，刚进来的那条就没人看了。
    if ls.preempt_inject_info is None and not ls.preempt_after_step:
        pending = await ls.rctx.check_preempted()
        if pending.get("preempted"):
            if pending.get("message"):
                ls.preempt_inject_info = pending
            else:
                ls.preempt_after_step = True
    if ls.preempt_inject_info is not None:
        _emit_pseudo_tool_result(ls, pseudo_call, "刚有新消息进来，这次不发言没有生效；先看新消息再决定。")
        return None
    reason = str((pseudo_call.arguments or {}).get("reason") or "").strip()
    logger.info("node %s stays silent: %s", ls.node.id, _short(reason, 120) if reason else "-")
    _emit_pseudo_tool_result(ls, pseudo_call, "好的，这一轮不发言。")
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FINISH,
        node_id=ls.node.id,
        # text 和 summary 都得是空的：supervisor 交付时 text 为空会拿 summary 顶上。
        result={"summary": "", "text": "", "attachments": [], NO_REPLY_MARKER: True},
        context_ref=ctx_ref,
        summary="不发言",
        llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
    )
