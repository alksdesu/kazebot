"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具和 dispatch:{target_id} 动态伪工具的执行逻辑。
"""
from __future__ import annotations

import json
import logging
import mimetypes as _mimetypes
from pathlib import Path
from typing import Any

from .resume_builder import _select_attachments
from ..compact import _format_messages_for_summary
from ..protocol import TaskAction, ACTION_ASK, ACTION_DISPATCH, ACTION_FINISH
from .loop_state import _LoopState, _persist_ctx, _short, compact_target_session_id
# 【Fix 3】reply 工具结果统一走 formatter.format_tool_result，需要 ParsedToolCall 构造、
# MessageMeta 标注 message_type、MessageType 影子写入。延迟导入 _shadow_write 避免循环依赖。
from .tool_format import ParsedToolCall
from .pseudo_tools import _dispatch_target_from_tool_name
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  [2026-04-22] 辅助函数：将 workspace-relative 路径列表转换为 attachment dict 列表。
#  [2026-05-04] 现在只服务 dispatch:{target_id} 动态伪工具。
#  为什么：旧聚合委派工具已删除，但动态委派仍需把父节点文件传给子节点。
#  怎么做：保留路径到 attachment dict 的转换函数，删除旧分支调用。
#  目的：让动态 dispatch 的附件行为保持不变。
# ---------------------------------------------------------------------------
def _paths_to_attachments(paths: list, workspace_root: Path) -> list[dict]:
    """Convert workspace-relative file paths to attachment dicts for dispatch."""
    result = []
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
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

    # reply: 非终止，发送中间消息
    if pseudo_call.name == "reply":
        reply_text = str(args.get("text") or "").strip()
        selected_atts = []
        selected_paths = args.get("attachment_paths")
        # [2026-07-08] Drop paths already auto-forwarded via dispatch_attachment,
        # so the caller LLM cannot re-send generated draw images a second time.
        selected_paths = _reject_already_sent_dispatch_paths(ls, selected_paths)
        if isinstance(selected_paths, list) and selected_paths:
            selected_atts = _select_attachments(
                ls.collected_attachments, selected_paths,
                workspace_root=ls.rctx.workspace_root,
                session_id=ls.rctx.session_id,
            )
        if reply_text or selected_atts:
            await ls.rctx.emit_event("intermediate_reply", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
                "attachments": selected_atts,
            })
            _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # preempt_task: 非终止，软打断子任务
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, pseudo_call, args)

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
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")

        ctx_ref = _persist_ctx(ls, step + 1)
        summary_text = str(args.get("summary") or "").strip()
        final_atts = []
        if terminal_name == "finish":
            _selected_paths = args.get("attachment_paths")
            # [2026-07-08] Drop paths already auto-forwarded via dispatch_attachment
            # (async draw images), so finish() cannot re-send the same files.
            _selected_paths = _reject_already_sent_dispatch_paths(ls, _selected_paths)
            if isinstance(_selected_paths, list) and _selected_paths:
                final_atts = _select_attachments(
                    ls.collected_attachments, _selected_paths,
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
                # to drop the file before it reached QQ/Discord. Auto-forward only
                # for dispatch_result payloads so ordinary image-analysis turns do
                # not echo the user's pictures back.
                final_atts = _dispatch_result_input_attachments(ls)
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
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        route_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
        try:
            await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/switch_node",
                json={"target_node_id": switch_target},
            )
        except Exception:
            pass
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
        conversation_text = _format_messages_for_summary(
            [m for m in ls.messages if m.get("role") != "system" and not m.get("_dynamic")]
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


# [2026-05-28] 判断字符串是否像 UUID 或 hex 前缀（task_id 支持前缀匹配）。
# 为什么：preempt_task 新增 node_id 支持，需要区分传入值是 task_id 还是 node_id。
# 怎么判断：全 hex+连字符且长度≥8 视为 task_id/前缀，否则视为 node_id。
import re as _re
_UUID_FULL_PATTERN = _re.compile(r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$', _re.I)

def _looks_like_task_id(s: str) -> bool:
    """Return True if s looks like a UUID or hex prefix (for task_id prefix match)."""
    if _UUID_FULL_PATTERN.fullmatch(s):
        return True
    # hex prefix: all hex chars (plus optional dashes), length >= 8
    return len(s) >= 8 and all(c in '0123456789abcdef-' for c in s.lower())


async def _handle_pseudo_preempt_task(ls: _LoopState, pseudo_call, args: dict) -> None:
    """处理 preempt_task 伪工具。始终返回 None（非终止）。"""
    _pt_tid = str(args.get("task_id") or "").strip()
    _pt_msg = str(args.get("message") or "").strip()
    if _pt_tid:
        try:
            # [2026-05-28] 支持按 node_id 查找活跃任务再 preempt。
            # 为什么：调用方通常只知道子节点名（如 "bob"），不知道动态 task_id。
            # 怎么改：如果传入值不像 UUID/hex 前缀，视为 node_id，先查出真实 task_id。
            # 目的：简化 preempt 调用，无需事先查询 task_id。
            real_task_id = _pt_tid
            if not _looks_like_task_id(_pt_tid):
                # 当作 node_id 处理，查询当前 session 中该节点的活跃任务
                _route_sid = ls.rctx.parent_session_id or ls.rctx.session_id
                _node_resp = await ls.rctx.http.get(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{_route_sid}/tasks/by-node/{_pt_tid}",
                )
                if _node_resp.status_code == 200:
                    _node_data = _node_resp.json()
                    real_task_id = _node_data.get("task_id", "")
                elif _node_resp.status_code == 404:
                    # [2026-05-28] fallback 到全局 by-node 查找。
                    # 为什么：持久节点的任务运行在独立 session 上，
                    #   session 内查找找不到。
                    # 怎么改：404 时 fallback 到 /v1/tasks/active-by-node/{node_id}。
                    # 目的：跨 session 支持 preempt 持久节点任务。
                    _global_resp = await ls.rctx.http.get(
                        f"{ls.rctx.supervisor_url}/v1/tasks/active-by-node/{_pt_tid}",
                    )
                    if _global_resp.status_code == 200:
                        _global_data = _global_resp.json()
                        real_task_id = _global_data.get("task_id", "")
                    else:
                        _pt_result = f"节点 '{_pt_tid}' 在当前 session 及全局范围内均无活跃任务"
                        _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                        return None
                else:
                    _pt_result = f"查询节点 '{_pt_tid}' 的任务失败: HTTP {_node_resp.status_code}"
                    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                    return None

            # [2026-05-23] Pass message to supervisor so preempt can inject
            # additional instructions instead of just stopping the child.
            _pt_body: dict[str, Any] = {}
            if _pt_msg:
                _pt_body["message"] = _pt_msg
            _pt_resp = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/tasks/{real_task_id}/preempt",
                json=_pt_body if _pt_body else None,
            )
            _display_id = _pt_tid if _looks_like_task_id(_pt_tid) else f"{_pt_tid}({real_task_id[:8]})"
            if _pt_resp.status_code == 200:
                if _pt_msg:
                    _pt_result = f"已向 task {_display_id} 注入追加指令，子任务继续执行"
                else:
                    _pt_result = f"已标记 task {_display_id} 为 preempt，等待优雅退出"
            elif _pt_resp.status_code == 404:
                _pt_result = f"task {_display_id} 不存在或已结束"
            else:
                _pt_result = f"API 返回 {_pt_resp.status_code}"
        except Exception as _pt_e:
            _pt_result = f"调用失败 {_pt_e}"
    else:
        _pt_result = "task_id 不能为空"
    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
    return None


def _target_is_persistent(ls: _LoopState, target: str) -> bool:
    """读取 dispatch 目标节点的 persistent 声明，用于推导默认 context_mode。

    [AutoC 2026-07-09] Why: 只有声明为长期节点（persistent=true）的 target
    才应默认 accumulate；其余节点默认 fresh。How: 复用 engine.node.load_node
    的同一搭载逻辑（config/nodes 与 system_nodes 搜索顺序），失败时保守返回
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


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch:{target_id} 动态伪工具。始终返回 None（非终止）。

    [2026-05-28] 所有异步 dispatch 统一走 inbound 路径。
    为什么：让异步子节点复用现有的 session / entry-branch / conversation 机制，
    而不是用单独的 dispatch-async 端点。
    怎么改：构造 inbound payload（含 dispatch_origin 回调信息），POST /v1/inbound。
    目的：统一生命周期管理，为 persistent node 支持铺路。
    """
    target = str(args.get("target") or "").strip()
    instr = str(args.get("instruction") or "").strip()

    # 目标是从工具名反查出来的，而反查表 _DISPATCH_TOOL_REVERSE 是进程内全局的，
    # 别的节点注册过的目标这里一样查得到，查不到还会原样退回字符串。
    # 不复核的话，白名单只管住了「模型看得见什么」，管不住「它敢写什么」——
    # 编一个名字就能把活派给委派清单外的节点，连带用上那个节点的全部工具。
    if target not in ls.allowed_dispatch_targets:
        logger.warning(
            "node %s attempted dispatch to non-delegate target: %s (allowed: %s)",
            ls.node.id, target, sorted(ls.allowed_dispatch_targets),
        )
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": (
                f"'{target}' is not in this node's delegate_targets. "
                f"Allowed: {sorted(ls.allowed_dispatch_targets) or '(none)'}"
            ),
        }, ensure_ascii=False))
        return None

    # [AutoC 2026-07-09] Why: dispatch 的默认 context_mode 原先写死 accumulate，
    # 导致所有未显式指定模式的子节点都长期复用 child session（历史污染 / 成本上升）。
    # How: 显式传参优先；未传时按 target 节点的 persistent 声明推导——
    # persistent=true → accumulate（长期人格 / 专属助手 / 立项的长期维护 agent），
    # 否则 fresh（一次性搜索、一次性工程执行、审查/debug 等）。
    # Purpose: 让 persistent 字段真正驱动默认行为，语义与 node.persistent 一致。
    _raw_ctx_mode = args.get("context_mode")
    if _raw_ctx_mode is not None and str(_raw_ctx_mode).strip():
        ctx_mode = str(_raw_ctx_mode).strip()
    else:
        ctx_mode = "accumulate" if _target_is_persistent(ls, target) else "fresh"
    ctx_key = str(args.get("context_key") or "").strip() or None
    attachment_paths = args.get("attachment_paths") or []
    attachments = _paths_to_attachments(attachment_paths, ls.rctx.workspace_root)

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
    if ctx_mode in ("fresh", "fork"):
        import uuid as _uuid
        conv_key = f"agent:{target_node_id}:{parent_conv_key}:{_uuid.uuid4()}"
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
    }
    if ctx_mode == "fork":
        inbound_payload["dispatch_fork_from_session"] = parent_session_id

    try:
        _dispatch_resp = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/inbound",
            json=inbound_payload,
            timeout=10.0,
        )
        if _dispatch_resp.status_code == 200:
            _d_data = _dispatch_resp.json()
            _dispatch_result = json.dumps({
                "success": True,
                "session_id": _d_data.get("session_id", ""),
                "message": f"已异步委派给 {target}",
            }, ensure_ascii=False)
        else:
            _dispatch_result = json.dumps({
                "success": False,
                "error": f"inbound API 返回 {_dispatch_resp.status_code}: {_dispatch_resp.text}",
            }, ensure_ascii=False)
    except Exception as e:
        _dispatch_result = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
    _emit_pseudo_tool_result(ls, pseudo_call, _dispatch_result)
    return None
