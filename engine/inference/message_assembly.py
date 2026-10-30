from __future__ import annotations

from pathlib import Path
from typing import Any

from ..attachments import build_multimodal_content
from ..conversation_routing import render_history_inputs, render_user_input, turn_input_meta
from ..isolation_notice import isolation_parts
from ..node import Node
from ..output_guide import OUTPUT_GUIDE_KEY, static_guide_messages, turn_guide_parts
from ..project_root import ISOLATION_CONTEXT_KEY
from ..prompt import assemble_prompt


def _conversational_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter history to only real conversation messages for keyword scanning.

    Excludes:
      - tool_result messages (tool execution output)
      - _dynamic injections (previously injected dynamic context)
      - messages with no text content (pure tool_call assistants)
    Keeps everything else: user_input, summary, assistant with text,
    and legacy messages without message_type.
    """
    result: list[dict[str, Any]] = []
    for m in history:
        # Skip old dynamic context injections
        if m.get("_dynamic"):
            continue
        # Skip tool execution results
        if m.get("message_type") == "tool_result":
            continue
        # Only include messages with actual text content
        role = m.get("role", "")
        if role in ("user", "assistant"):
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                result.append(m)
    return result


def _turn_input_message(
    workspace_root: Path,
    instruction: str,
    attachments: list[dict[str, Any]] | None,
    turn_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """当轮用户输入，按它落盘时会带的 meta 渲染；instruction 和附件都为空时没有这一条。"""
    if attachments:
        content: Any = build_multimodal_content(instruction, attachments, workspace_root=workspace_root)
    elif instruction:
        content = instruction
    else:
        return None
    return {"role": "user", "content": render_user_input(content, turn_meta)}


def assemble_messages_with_injections(
    *,
    workspace_root: Path,
    system_prompt: list[dict[str, Any]],
    history: list[dict[str, Any]],
    instruction: str,
    attachments: list[dict[str, Any]] | None = None,
    skill_static: list[dict[str, Any]] | None = None,
    skill_dynamic: list[dict[str, Any]] | None = None,
    memory_static: list[dict[str, Any]] | None = None,
    memory_dynamic: list[dict[str, Any]] | None = None,
    output_guide: dict[str, Any] | None = None,
    turn_meta: dict[str, Any] | None = None,
    isolation: Any = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Lay out prompt messages using precomputed skill and memory injections.

    Why: KnowledgeInjector must be able to rebuild the same prompt layout that
    assemble_initial_messages historically produced, without duplicating
    injection in two places. How: keep the block-mode and string-mode
    placement rules in this shared helper and pass precomputed static/dynamic
    injection messages into it. Purpose: allow before_prompt_build hooks to own
    skill and memory construction while preserving byte-level prompt layout.

    turn_meta 是当轮输入落盘时会带的 meta（turn_input_meta 算出）。历史里的用户输入按各自的 meta、
    当轮输入按它渲染，同一条输入当轮发出去的和之后重载的才一字不差。
    isolation 是 task_context 里的隔离说明：隔离副本里的任务在动态区多一段 [ISOLATION]，排在技能和记忆之前；
    其余任务的提示词一字不变。
    带文本附件时要读盘，协程里放到线程里调。
    """
    skill_static = list(skill_static or [])
    skill_dynamic = list(skill_dynamic or [])
    memory_static = list(memory_static or [])
    memory_dynamic = list(memory_dynamic or [])
    guide_static = static_guide_messages(output_guide)
    guide_turn = turn_guide_parts(output_guide)
    isolation_turn = isolation_parts(isolation, workspace_root=workspace_root)
    rendered_history = render_history_inputs(history)

    # ---- Prompt cache friendly layout ----
    # Detect block list mode: assemble_prompt returns blocks with {role: history}
    _is_block_mode = any(
        isinstance(m, dict) and m.get("role") == "history"
        for m in system_prompt
    )

    messages: list[dict[str, Any]] = []

    if _is_block_mode:
        # === Block list mode ===
        # User controls message structure via prompt blocks.
        # {role: history} marks where conversation history is expanded.

        # Partition blocks around history marker
        _before_history: list[dict[str, Any]] = []
        _after_history: list[dict[str, Any]] = []
        _found_marker = False
        for _blk in system_prompt:
            if isinstance(_blk, dict) and _blk.get("role") == "history":
                _found_marker = True
                continue
            if _found_marker:
                _after_history.append(_blk)
            else:
                _before_history.append(_blk)

        # Separate depth-blocks from normal post-history blocks
        _depth_blocks: list[dict[str, Any]] = []
        _post_blocks: list[dict[str, Any]] = []
        for _pb in _after_history:
            if "depth" in _pb:
                _depth_blocks.append(_pb)
            else:
                _post_blocks.append(_pb)

        # --- Pre-history blocks (user-defined) ---
        messages.extend(_before_history)
        # Skill/memory static (cache-friendly, before history)
        messages.extend(skill_static)
        messages.extend(memory_static)
        messages.extend(guide_static)

        # --- Conversation history ---
        messages.extend(rendered_history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- Dynamic (在 instruction 之前) ---
        _dynamic_parts: list[str] = list(isolation_turn)
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        _dynamic_parts.extend(guide_turn)
        if _dynamic_parts:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文，每轮可能变化。\n\n" + "\n\n".join(_dynamic_parts),
                "_dynamic": True,
            })

        # --- Post-history blocks (no depth) ---
        for _pb in _post_blocks:
            messages.append({k: v for k, v in _pb.items() if k != "depth"})

        # --- Instruction（始终在 dynamic/post_blocks 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        _turn_input = _turn_input_message(workspace_root, instruction, attachments, turn_meta)
        if _turn_input is not None:
            messages.append(_turn_input)

        # --- Depth blocks (insert from highest depth to lowest) ---
        if _depth_blocks:
            _depth_blocks.sort(key=lambda b: b.get("depth", 0), reverse=True)
            for _db in _depth_blocks:
                _d = int(_db.get("depth", 0))
                _clean = {k: v for k, v in _db.items() if k != "depth"}
                _insert_pos = max(0, len(messages) - _d)
                messages.insert(_insert_pos, _clean)

    else:
        # === String mode (existing behavior) ===
        # Stable prefix → history → dynamic suffix → instruction
        #
        # Dynamic content uses role=user instead of role=system so that
        # Anthropic/Gemini (which merge all system messages into a single
        # system field) keep a stable system cache across turns.

        # --- stable prefix (system) ---
        if system_prompt:
            messages.append(system_prompt[0])  # static part
        messages.extend(skill_static)
        messages.extend(memory_static)
        messages.extend(guide_static)

        # --- history ---
        messages.extend(rendered_history)

        # 如果 history 末尾就是当前 instruction，先 pop 掉（后面统一追加在 dynamic 之后）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if _already_in_history:
            messages.pop()

        # --- dynamic (在 instruction 之前) ---
        _dynamic_parts_s: list[str] = []
        if len(system_prompt) >= 2 and system_prompt[1].get("content"):
            _dynamic_parts_s.append(system_prompt[1]["content"])
        _dynamic_parts_s.extend(isolation_turn)
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts_s.append(_dm["content"])
        _dynamic_parts_s.extend(guide_turn)
        if _dynamic_parts_s:
            messages.append({
                "role": "user",
                "content": "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n" + "\n\n".join(_dynamic_parts_s),
                "_dynamic": True,
            })

        # --- instruction（始终在 dynamic 之后） ---
        # Step 2（2026-04-16）：主节点 ConversationStore resume 场景下 runner 会把
        # instruction 和 attachments 清空，此时不追加末尾 user 消息，避免与 JSONL
        # 中已有的原 instruction 重复。
        _turn_input = _turn_input_message(workspace_root, instruction, attachments, turn_meta)
        if _turn_input is not None:
            messages.append(_turn_input)

    return messages, _is_block_mode


def assemble_initial_messages(
    *,
    workspace_root: Path,
    runtime_cfg: dict[str, Any],
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    task_context: dict[str, Any] | None = None,
    session_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
    project_root: Path | None = None,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    """构建初始 messages 数组。

    返回 (messages, is_block_mode, system_prompt_blocks)。
    system_prompt_blocks 是 assemble_prompt 的原始输出，供后续 preempt 注入使用。
    project_root 是任务的项目根，渲染 {{project_root}}；没给就等于 workspace_root。
    要读 include 进来的提示词片段和当轮的文本附件，协程里放到线程里调。
    """
    prompt_vars: dict[str, str] = {
        "node_id": node.id,
        "node_name": node.name,
        "instruction": instruction,
    }
    system_prompt = assemble_prompt(workspace_root, node, variables=prompt_vars, project_root=project_root)
    # 技能和记忆由 before_prompt_build 钩子补上后按同一个布局重排；输出说明和隔离说明不依赖钩子，骨架里就带上。
    messages, is_block_mode = assemble_messages_with_injections(
        workspace_root=workspace_root,
        system_prompt=system_prompt,
        history=history,
        instruction=instruction,
        attachments=attachments,
        output_guide=(task_context or {}).get(OUTPUT_GUIDE_KEY),
        turn_meta=turn_input_meta(task_context),
        isolation=(task_context or {}).get(ISOLATION_CONTEXT_KEY),
    )
    return messages, is_block_mode, system_prompt
