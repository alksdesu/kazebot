from __future__ import annotations
import logging
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from engine.conversation_routing import render_user_input, routing_meta

from typing import Any

from engine.attachments import build_multimodal_content
from engine.inference.loop_state import conversation_writer, flush_conversation_writes
from engine.isolation_notice import isolation_parts
from engine.memory_subjects import resolve_load_subjects
from engine.output_guide import (
    OUTPUT_GUIDE_KEY,
    STATIC_GUIDE_MARKER,
    has_static_guide,
    static_guide_messages,
    turn_guide_parts,
)
from engine.project_root import ISOLATION_CONTEXT_KEY
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from .knowledge_inject import build_knowledge_context
from engine.protocol import ACTION_CANCELLED, TaskAction

logger = logging.getLogger(__name__)

# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "PreemptChecker",
    "hook_points": [
        ("before_step", "handle"),
    ],
    "priority": 100,
}


class PreemptChecker:
    """Handle cancellation and preempt message injection before each LLM step."""

    name = "preempt_checker"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        """Run the legacy loop-top cancellation and preempt checks.

        Why: cancellation and preempt state were hard-coded in ai_step.py, which
        made the inference loop harder to extend. How: read the current loop
        state from HookContext.extra, perform the same checks, and mutate the
        message list when a preempt message must be injected. Purpose: preserve
        the old control flow while moving the behavior behind before_step.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        if await ls.rctx.check_cancelled():
            await ls.rctx.emit_event("cancel_acknowledged", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
            })
            return hook_result(
                action=TaskAction(
                    action=ACTION_CANCELLED,
                    node_id=ls.node.id,
                    summary="任务已被用户取消。",
                )
            )

        if not ls.preempt_after_step and ls.preempt_inject_info is None:
            preempt_info = await ls.rctx.check_preempted()
            if preempt_info.get("preempted"):
                if preempt_info.get("message"):
                    ls.preempt_inject_info = preempt_info
                else:
                    ls.preempt_after_step = True
                    await ls.rctx.emit_event("preempt_acknowledged", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "step": ctx.step,
                    })

        if ls.preempt_inject_info is None:
            return None

        await _inject_preempt_message(ctx, ls)
        return hook_result(modified=True)


async def _inject_preempt_message(ctx: Any, ls: Any) -> None:
    """把待注入的抢占消息接进本轮：换掉旧的动态上下文，追加用户新消息并落盘，再回执 supervisor。"""
    new_instruction = ls.preempt_inject_info.get("message", "")
    new_attachments = ls.preempt_inject_info.get("attachments", [])
    memory_hints = ls.preempt_inject_info.get("memory_hints")
    output_guide = ls.preempt_inject_info.get(OUTPUT_GUIDE_KEY)
    task_context = dict(getattr(ls.rctx, "task_context", {}) or {})
    if isinstance(memory_hints, dict):
        task_context["memory_hints"] = {
            "subjects": resolve_load_subjects(ls.rctx.workspace_root, memory_hints.get("subjects")),
        }
    if isinstance(output_guide, dict):
        task_context[OUTPUT_GUIDE_KEY] = output_guide
    if isinstance(memory_hints, dict) or isinstance(output_guide, dict):
        ls.rctx.task_context = task_context
    ls.rctx.user_text = new_instruction

    ls.messages = [m for m in ls.messages if not m.get("_dynamic")]
    ctx.messages = ls.messages

    from engine.inference.message_assembly import _conversational_history

    scan_history = _conversational_history(ls.history)
    skill_static, skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
        ls.rctx.workspace_root,
        ls.node,
        new_instruction,
        scan_history,
        ls.runtime_cfg,
        task_context=task_context,
    )
    if isinstance(memory_hints, dict):
        _replace_static_memories(ls, skill_static, memory_static)
        ctx.messages = ls.messages
    guide_in_prefix = has_static_guide(ls.messages)
    if isinstance(output_guide, dict) and guide_in_prefix:
        _replace_static_guide(ls, output_guide)
        ctx.messages = ls.messages

    dynamic_parts: list[str] = []
    if not ls.is_block_mode and len(ls.system_prompt) >= 2 and ls.system_prompt[1].get("content"):
        dynamic_parts.append(ls.system_prompt[1]["content"])
    # 和初次组装排在同一个位置：节点自己的动态段之后、技能和记忆之前
    dynamic_parts.extend(isolation_parts(
        task_context.get(ISOLATION_CONTEXT_KEY), workspace_root=ls.rctx.workspace_root,
    ))
    for dynamic_msg in skill_dynamic:
        if dynamic_msg.get("content"):
            dynamic_parts.append(dynamic_msg["content"])
    for dynamic_msg in memory_dynamic:
        if dynamic_msg.get("content"):
            dynamic_parts.append(dynamic_msg["content"])
    dynamic_parts.extend(turn_guide_parts(task_context.get(OUTPUT_GUIDE_KEY), include_stable=not guide_in_prefix))

    if dynamic_parts:
        dynamic_prefix = (
            "以下是本轮动态上下文，每轮可能变化。\n\n"
            if ls.is_block_mode
            else "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n"
        )
        ls.messages.append({
            "role": "user",
            "content": dynamic_prefix + "\n\n".join(dynamic_parts),
            "_dynamic": True,
        })

    if new_attachments:
        content: Any = build_multimodal_content(
            new_instruction,
            new_attachments,
            workspace_root=ls.rctx.workspace_root,
        )
    else:
        content = new_instruction
    message_id = str(uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    meta = {
        **routing_meta(task_context),
        "message_id": message_id,
        "timestamp": timestamp,
        "message_type": "user_input",
        "source_task_id": getattr(ls.rctx, "task_id", ""),
        **({"attachments": list(new_attachments)} if new_attachments else {}),
    }
    # 短答要求和组装当轮输入时一样按 meta 接上，落盘的是原文：重载时同样按 meta 接，前后才一字不差
    ls.messages.append({"role": "user", "content": render_user_input(content, meta), "_meta": meta})

    # 抢占注入的用户消息也要落盘，分支任务恢复或合并回主会话时才不会丢这一条
    if new_instruction or new_attachments:
        try:
            writer = conversation_writer(ls.rctx)
            if writer is not None:
                from engine.conversation_store import Message, MessageType

                target_session = getattr(ls.rctx, "child_session_id", "") or ls.rctx.session_id
                writer.submit(
                    target_session,
                    Message(
                        id=message_id,
                        role="user",
                        # 落盘原文，带图时同任务内恢复还看得到图
                        content=deepcopy(content),
                        message_type=MessageType.USER_INPUT,
                        created_at=timestamp,
                        meta={
                            **routing_meta(task_context),
                            **({"attachments": list(new_attachments)} if new_attachments else {}),
                        },
                        source_node_id=getattr(ls.node, "id", ""),
                        source_task_id=getattr(ls.rctx, "task_id", ""),
                    ),
                )
        except Exception:
            logger.exception("persisting preempt message failed: task=%s", getattr(ls.rctx, "task_id", ""))
        # 先落盘再回执：supervisor 收到回执就丢掉这条消息，中间崩了用户这句话就没了
        await flush_conversation_writes(ls.rctx)

    revision = ls.preempt_inject_info.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        await ls.rctx.consume_preempt(revision=revision)
    else:
        await ls.rctx.consume_preempt()
    await ls.rctx.emit_event("preempt_injected", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "step": ctx.step,
    })

    ls.preempt_inject_info = None
    ls.plaintext_retry_count = 0
    ls.compacted = False


def _replace_static_memories(ls: Any, skill_static: list, memory_static: list) -> None:
    memory_indices = [
        index for index, message in enumerate(ls.messages)
        if message.get("_knowledge_source") == "memory"
        or (
            message.get("role") == "system"
            and str(message.get("content") or "").startswith("[MEMORY:CONSTANT]\n")
        )
    ]
    if memory_indices:
        insertion_index = memory_indices[0]
        removed = set(memory_indices)
        ls.messages = [message for index, message in enumerate(ls.messages) if index not in removed]
    else:
        prefix_length = 0
        for message in ls.system_prompt:
            if message.get("role") == "history":
                break
            prefix_length += 1
            if not ls.is_block_mode:
                break
        insertion_index = prefix_length + len(skill_static)
    ls.messages[insertion_index:insertion_index] = memory_static


def _replace_static_guide(ls: Any, output_guide: dict[str, Any]) -> None:
    """原位换上新的稳定输出说明：内容没变时前缀也没变，缓存照样命中。"""
    indices = [index for index, message in enumerate(ls.messages) if message.get(STATIC_GUIDE_MARKER)]
    removed = set(indices)
    ls.messages = [message for index, message in enumerate(ls.messages) if index not in removed]
    ls.messages[indices[0]:indices[0]] = static_guide_messages(output_guide)
