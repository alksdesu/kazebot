"""推理循环共享状态与上下文持久化辅助。

从 ai_step.py 抽出，供 ai_step、llm_call、pseudo_handlers 共用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from providers.base import BaseProvider
from ..node import Node
from ..context_store import save_context_snapshot, write_context_snapshot

if TYPE_CHECKING:
    from ..context import RunContext
    from .tool_format import ToolFormatter


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "...<truncated>"


def _sanitize_context_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "ctx").strip() or "ctx")[:80]


def _persist_node_context(
    workspace_root: Path,
    session_id: str,
    task_id: str,
    node_id: str,
    messages: list[dict[str, Any]],
    *,
    step_count: int,
    context_ref: str = "",
    last_message_id: str = "",
) -> str:
    # DEPRECATED — Child Session 隔离（Phase D）
    # 本函数仅在 child session 模式关闭时（主节点 + compact dispatch）使用。
    # _persist_ctx 已在 child session 模式下跳过调用。
    # 待 child session 稳定后可删除。
    # [2026-05-07] 快照写入前清理控制流工具历史。
    # 原因：旧 snapshot 路径仍可能保存 finish tool_call/tool_result，后续恢复会造成 native 配对错误。
    # 做法：复用 L2 的控制流清洗，再过滤 dynamic/ephemeral/retry_hint。
    # 目的：ConversationStore 与旧 snapshot 两条持久化路径保持同一语义。
    from .tool_format import sanitize_control_tool_history
    _clean_history = sanitize_control_tool_history(messages)
    _persisted_msgs = [m for m in _clean_history if not m.get("_dynamic") and not m.get("_ephemeral") and not m.get("_retry_hint")]
    # Phase 3 (Session Conversation Store): snapshot version 升至 2，
    # 新增 last_message_id 字段指向 ConversationStore 中最后一条影子写入的消息。
    # messages 数组暂时保留（向后兼容），后续 Phase 可移除。
    snapshot: dict[str, Any] = {
        "version": 2,
        "node_id": node_id,
        "messages": _persisted_msgs,
        "step_count": int(step_count),
    }
    if last_message_id:
        snapshot["last_message_id"] = last_message_id
    if context_ref:
        return write_context_snapshot(workspace_root, context_ref, snapshot)
    context_id = _sanitize_context_id(f"{task_id}_{node_id}")
    return save_context_snapshot(workspace_root, session_id, snapshot, context_id=context_id)


# ---------------------------------------------------------------------------
#  推理循环共享状态
# ---------------------------------------------------------------------------

@dataclass
class _LoopState:
    """推理循环中的共享状态，供各子函数读写。"""

    # ---- 核心引用 ----
    rctx: "RunContext"
    node: Node
    # [provider-registry 2026-05-03] 推理状态只保存通用 provider 接口。
    # 原因：registry 可返回任意 BaseProvider 子类；做法：把类型改为 BaseProvider；
    # 目的：消除循环状态中的硬编码 provider 类型。
    provider: BaseProvider
    registry: ToolRegistry
    run_id: str
    context_ref: str
    runtime_cfg: dict[str, Any]
    streaming: bool

    # ---- 消息与工具 ----
    messages: list[dict[str, Any]]
    system_prompt: list[dict[str, Any]]
    is_block_mode: bool
    openai_tools: list[dict[str, Any]]
    history: list[dict[str, Any]]

    # ---- 附件 ----
    collected_attachments: list[dict[str, Any]]
    tool_produced_attachments: list[dict[str, Any]]

    # ---- 压缩 ----
    compact_threshold: int = 0
    compact_keep_recent: int = 6
    # 软阈值触发后台压缩、硬阈值才挂起当轮同步压；keep_recent_tokens > 0 时按 token
    # 决定保留多少原文，keep_recent 的段数语义退居兼容路径。
    compact_hard_threshold: int = 0
    compact_keep_recent_tokens: int = 0
    compacted: bool = False
    compact_async_requested: bool = False
    last_prompt_tokens: int | None = None
    last_usage: dict | None = None  # 最近一次 LLM resp.usage，用于写入 assistant 消息 meta

    # ---- LLM 重试 ----
    retry_max: int = 3
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 30.0
    retry_backoff: float = 2.0
    plaintext_retry_count: int = 0
    plaintext_retry_max: int = 2

    # ---- Preempt ----
    preempt_after_step: bool = False
    preempt_inject_info: dict[str, Any] | None = None

    # ---- 流式 ----
    use_stream: bool = False

    # ---- 工具格式 ----
    formatter: ToolFormatter | None = None

    # ---- 已授权的真工具名集合（由 _filter_tool_specs 输出决定）----
    allowed_real_tools: set = field(default_factory=set)

    # ---- 可委派的目标节点（node.delegate_targets 去掉被 gating 摘掉的）----
    # 和上面那份一样，装的是「给模型看的那一份」。执行时拿它复核：
    # dispatch 目标是从工具名反查的，反查表是进程内全局的，别的节点注册过就查得到。
    allowed_dispatch_targets: set = field(default_factory=set)

    # ---- 本任务内真实工具执行记录 ----
    # [AutoC 2026-07-11] Why: 某些绘图/媒体节点的模型会“伪造工具调用”——
    # 在自然语言里编造 tool result 说生图成功，却从未真正调用 nai_generate_*，
    # 导致 finish 谎报成功、图片从未生成也从未发送。How: 每次真实工具执行完成后，
    # 记录成功执行过的真实工具名，供 finish 硬校验钩子核对。Purpose: 让节点无法在
    # 没有真实成功工具调用的情况下 finish 报成功。
    succeeded_real_tools: set = field(default_factory=set)
    failed_real_tools: set = field(default_factory=set)

    # ---- Phase 3 (Session Conversation Store): 最近一次 shadow write 的 Message.id ----
    # 由 _shadow_write() 更新，_persist_ctx() 写入 snapshot 的 last_message_id 字段，
    # 为后续 snapshot 瘦身（不再存完整 messages 数组）做准备。
    last_shadow_message_id: str = ""


def compact_target_session_id(ls: _LoopState) -> str:
    """压缩应该改写哪个 durable session。

    accumulate 模式的子节点历史存在自己的 JSONL 里，压它自己的 session；没有
    child_session_id 时（entry branch）才回落到父会话，否则改写的是马上要删的分支。
    自动压缩、手动 compact_context、L2 snip 必须用同一个答案 —— 少一处就会把子节点
    的摘要写进父会话，把父会话历史整段换成「子节点干了什么」。
    """
    child_sid = str(getattr(ls.rctx, "child_session_id", "") or "").strip()
    if child_sid:
        return child_sid
    return str(getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id or "").strip()


def _persist_ctx(ls: _LoopState, step_count: int) -> str:
    """便捷函数：持久化当前节点上下文快照。

    Phase 3: 将 ls.last_shadow_message_id 传入 snapshot，
    记录 ConversationStore 中最后一条影子写入消息的 ID。

    Child Session 隔离（Phase B）：child session 模式下跳过 snapshot 写入，
    所有消息已实时写入 child session 的 JSONL，不需要冗余 snapshot。
    返回空字符串，task result 中不产生 context_ref。

    Step 2（2026-04-16）：主节点切 ConversationStore 时同样跳过 snapshot。
    engine.child_session.main_session_enabled=true 时，主节点的消息
    同样由 _shadow_write 实时写入 data/conversations/{session_id}.jsonl，
    写 snapshot 是冗余的。flag 关闭时回退到旧行为。
    """
    # Child Session 模式：消息已实时写入 JSONL，不需要 snapshot
    child_sid = getattr(ls.rctx, 'child_session_id', '')
    if child_sid:
        return ""  # 不产生 context_ref
    # Step 2：主节点 ConversationStore 模式也跳过 snapshot
    try:
        from clonoth_runtime import load_runtime_config
        _rc = load_runtime_config(ls.rctx.workspace_root)
        if bool(_rc.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)):
            return ""
    except Exception:
        pass
    # 旧路径（flag 关闭时的回退）：正常写 snapshot
    return _persist_node_context(
        ls.rctx.workspace_root, ls.rctx.session_id,
        ls.rctx.task_id or ls.run_id or ls.node.id, ls.node.id, ls.messages,
        step_count=step_count, context_ref=ls.context_ref,
        last_message_id=ls.last_shadow_message_id,
    )
