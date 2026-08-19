from __future__ import annotations

import logging
from typing import Any

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result

logger = logging.getLogger(__name__)

_REJECT_MESSAGE_TEMPLATE = (
    "\u274c REJECTED: {tool}() cannot be called alongside other tools "
    "(except reply). Execute your other tools first, wait for their "
    "results, then call {tool}() alone in a separate turn."
)

# [AutoC 2026-07-11] finish 硬校验拒绝文案。Why: 绘图/媒体节点的模型会“伪造
# 工具调用”（在自然语言里编造 tool result 说生图成功）却从未真正调用工具，
# 导致 finish 谎报成功、图片从未生成也从未发送。How: 节点通过 finish_requires_tool
# 声明 finish 前必须成功执行过的真实工具，finish 时核对本任务真实工具执行记录。
_REQUIRE_TOOL_MESSAGE = (
    "\u274c REJECTED: {tool}() cannot claim success yet. This node requires a "
    "successful call to one of these tools before finishing: {tools}. "
    "You have NOT actually called it successfully in this task — do not fabricate "
    "a tool result or claim the image/media was generated or sent. Call the real "
    "tool now, wait for its actual result, and only finish after it truly succeeds. "
    "If the tool keeps failing, finish with an honest failure message instead."
)

_VISION_FAILURE_REJECT_MESSAGE = (
    "\u274c REJECTED: the latest image-reading operation failed, so you do not have "
    "grounded visual information. Do not describe, OCR, translate, identify, or infer "
    "anything from the image or chat history. Call finish() with only a brief honest "
    "failure notice and ask the user to resend/re-reference the image or retry later."
)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "FinishGuardHandler",
    "hook_points": [
        ("before_tool_call", "handle"),
    ],
    "priority": 100,
}


class FinishGuardHandler:
    """Reject terminal finish()/ask() when colocated with non-reply tool calls."""

    name = "finish_guard"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        """Apply the terminal finish/ask colocation guard.

        Why: finish and ask terminate the task, so other same-turn tool results
        would never be read by the model. How: prefer ai_step's already-filtered
        legacy pseudo/real call lists when present, and otherwise fall back to raw
        ctx.tool_calls for isolated tests. Purpose: move the guard out of
        ai_step.py while applying the same protection to the Phase 0 ask tool.
        """
        terminal_tools = {"finish", "ask"}
        pseudo_calls = ctx.extra.get("pseudo_calls")
        real_tool_calls = ctx.extra.get("real_tool_calls")

        if pseudo_calls is not None or real_tool_calls is not None:
            pseudo_list = list(pseudo_calls or [])
            real_list = list(real_tool_calls or [])
            terminal_call = next(
                (call for call in pseudo_list if _tool_name(call) in terminal_tools),
                None,
            )
            terminal_name = _tool_name(terminal_call) if terminal_call is not None else ""
            has_terminal = bool(terminal_name)
            has_non_reply_others = bool(real_list) or any(
                _tool_name(call) not in (*terminal_tools, "reply") for call in pseudo_list
            )
        else:
            calls = list(ctx.tool_calls or [])
            terminal_call = next(
                (call for call in calls if _tool_name(call) in terminal_tools),
                None,
            )
            terminal_name = _tool_name(terminal_call) if terminal_call is not None else ""
            has_terminal = bool(terminal_name)
            has_non_reply_others = any(
                _tool_name(call) not in (*terminal_tools, "reply") for call in calls
            )

        # [AutoC 2026-07-11] finish 硬校验：若节点声明 finish_requires_tool，且本轮
        # 调用了终止工具（finish），则校验本任务内是否真正成功执行过要求的工具。
        # 只对 finish 生效（ask 不受限，因为 ask 是向用户提问而非声称完成）。
        _finish_terminal = terminal_name if 'terminal_name' in dir() else ""
        if _finish_terminal == "finish":
            if _has_unresolved_vision_failure_for_context(ctx):
                finish_text = _finish_text(terminal_call)
                if not _is_honest_vision_failure_text(finish_text):
                    logger.warning(
                        "Rejected ungrounded finish after vision failure (node=%s, step=%d)",
                        getattr(ctx.node, "id", ""),
                        ctx.step,
                    )
                    return hook_result(
                        block=True,
                        reason="vision_failure_ungrounded_finish",
                        error_message=_VISION_FAILURE_REJECT_MESSAGE,
                    )
            _require_reject = self._check_finish_requires_tool(ctx)
            if _require_reject is not None:
                return _require_reject

        if not (has_terminal and has_non_reply_others):
            return None

        # [AutoC 2026-05-31] Why: ask is terminal like finish, so the same
        # colocated-tool rejection must name the offending terminal tool. How: use
        # a shared template populated from the detected pseudo call. Purpose: keep
        # model-facing retry guidance precise for both finish and ask.
        logger.warning(
            "Rejected %s + other tools in same turn (node=%s, step=%d, tools=%s)",
            terminal_name or "terminal",
            getattr(ctx.node, "id", ""),
            ctx.step,
            [_tool_name(call) for call in (ctx.tool_calls or [])],
        )
        return hook_result(
            block=True,
            reason=f"{terminal_name or 'terminal'}_colocated",
            error_message=_REJECT_MESSAGE_TEMPLATE.format(tool=terminal_name or "finish"),
        )

    def _check_finish_requires_tool(self, ctx: Any) -> Any | None:
        """Reject a finish() that claims success without a real successful tool call.

        Why: some draw/media node models fabricate a tool result in prose (e.g.
        "NovelAI 生图成功") without ever calling the real generate tool, so finish
        lies about success and the image is never generated or sent. How: read the
        node-level ``finish_requires_tool`` declaration and the loop state's
        ``succeeded_real_tools`` set; if none of the required tools truly succeeded
        in this task, block finish and tell the model to actually call the tool.
        Purpose: make it structurally impossible to finish-as-success without a
        genuine successful tool execution.
        """
        node = getattr(ctx, "node", None)
        required_any = _finish_required_tools(node)
        if not required_any:
            return None

        ls = ctx.extra.get("loop_state")
        succeeded = set(getattr(ls, "succeeded_real_tools", set()) or set()) if ls is not None else set()
        if succeeded & required_any:
            return None

        # [2026-07-22] Why: 异步工具（如 nai_generate）成功启动后其结果通过
        # async_tool_result 注入会 fork 出一个新的 branch task，新 task 的 loop_state
        # 是全新实例，succeeded_real_tools 为空，导致模型在新 task 里 finish 播报时
        # 又被这里拦下，陷入反复 Rejected finish 死循环、回复发不出（现象：用户追问
        # “图呢”）。How: 当本 task 的 succeeded 集合不满足时，回退扫描消息历史，若历史
        # 里出现过所需工具“已成功发起异步执行”的占位记录（async_started），也视为满足
        # finish 前置。Purpose: 让异步生图结果回传后的 finish 播报能正常通过。
        if self._history_has_async_started_tool(ctx, required_any):
            return None

        logger.warning(
            "Rejected finish without required tool success (node=%s, step=%d, required=%s, succeeded=%s)",
            getattr(node, "id", ""),
            getattr(ctx, "step", -1),
            sorted(required_any),
            sorted(succeeded),
        )
        return hook_result(
            block=True,
            reason="finish_requires_tool",
            error_message=_REQUIRE_TOOL_MESSAGE.format(
                tool="finish",
                tools=", ".join(sorted(required_any)),
            ),
        )

    def _history_has_async_started_tool(self, ctx: Any, required_any: set[str]) -> bool:
        """Scan message history for a successfully *started* async required tool.

        Why: async tools (nai_generate) return an ``async_started`` placeholder in
        the current task and their real result is injected into a NEW forked task,
        whose fresh loop_state has an empty ``succeeded_real_tools``. Without this
        fallback the model's finish in that new task keeps getting rejected. How:
        walk ctx.messages and match the async placeholder text (which embeds the
        tool name) emitted by ai_step for started async tools. Purpose: treat a
        genuinely dispatched async generation as satisfying the finish precondition.
        """
        messages = getattr(ctx, "messages", None)
        if not isinstance(messages, list):
            return False
        for msg in messages:
            try:
                content = msg.get("content") if isinstance(msg, dict) else None
            except Exception:
                content = None
            if not content:
                continue
            text = content if isinstance(content, str) else str(content)
            # ai_step 的异步占位文本：'⏳ Async tool "<name>" started (id: ...)'
            if "Async tool" not in text or "started" not in text:
                continue
            for name in required_any:
                if f'"{name}"' in text or f"'{name}'" in text:
                    return True
        return False


def _finish_required_tools(node: Any) -> set[str]:
    """Read node-level ``finish_requires_tool.any`` into a set of tool names.

    Why: the finish hard-guard is opt-in per node via YAML. How: unknown YAML
    keys land in ``Node.extra``; accept both {"any": [...]} and a bare list for
    convenience. Purpose: keep the guard generic and configuration-driven instead
    of hard-coding draw-specific tool names.
    """
    if node is None:
        return set()
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        return set()
    raw = extra.get("finish_requires_tool")
    if raw is None:
        return set()
    if isinstance(raw, dict):
        names = raw.get("any") or raw.get("tools") or []
    elif isinstance(raw, (list, tuple)):
        names = list(raw)
    elif isinstance(raw, str):
        names = [raw]
    else:
        names = []
    return {str(n).strip() for n in names if str(n).strip()}


def _has_unresolved_vision_failure_for_context(ctx: Any) -> bool:
    """Prefer structured current-task tool state, then inspect resumed callbacks."""
    loop_state = ctx.extra.get("loop_state") if isinstance(getattr(ctx, "extra", None), dict) else None
    succeeded = set(getattr(loop_state, "succeeded_real_tools", set()) or set()) if loop_state else set()
    failed = set(getattr(loop_state, "failed_real_tools", set()) or set()) if loop_state else set()
    read_succeeded = "read_image" in succeeded
    read_failed = "read_image" in failed
    if read_succeeded and not read_failed:
        return False
    if read_failed and not read_succeeded:
        return True
    # Both sets are cumulative. When retries produced both outcomes, use the
    # ordered message history to determine which result happened last.
    return _has_unresolved_vision_failure(getattr(ctx, "messages", None))


def _has_unresolved_vision_failure(messages: Any) -> bool:
    """Find the latest vision status within the current user turn.

    Scan backwards without an arbitrary message-count window. Stop at the current
    turn's ordinary user input so a failure from an older turn cannot poison later,
    unrelated requests, while any number of tool messages after a current failure
    still cannot push it out of the guard.
    """
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        is_read_image_result = text.startswith('Tool result for "read_image"')
        failed_result = is_read_image_result and any(
            marker in text for marker in ("ERROR:", '"ok": false', "must_not_guess")
        )
        if (
            text.startswith('\u274c Async tool "read_image"')
            or text.startswith('Tool "read_image" 执行失败')
            or text.startswith("下游节点 qq.vision 执行失败")
            or text.startswith("read_image 工具调用失败")
            or failed_result
        ):
            return True
        if (
            text.startswith('\u2705 Async tool "read_image"')
            or text.startswith("下游节点 qq.vision 已完成")
            or (is_read_image_result and not failed_result)
        ):
            return False
        if str(message.get("role") or "") == "user":
            return False
    return False


def _finish_text(call: Any) -> str:
    """Extract finish text from dict/object tool-call shapes."""
    if call is None:
        return ""
    arguments = call.get("arguments") if isinstance(call, dict) else getattr(call, "arguments", None)
    if isinstance(arguments, dict):
        return str(arguments.get("text") or "").strip()
    return ""


def _is_honest_vision_failure_text(text: str) -> bool:
    """Allow only concise failure notices after an unresolved vision failure."""
    value = str(text or "").strip()
    if not value or len(value) > 500:
        return False
    lowered = value.lower()
    failure_markers = (
        "识图失败", "读取图片失败", "图片读取", "读取接口", "无法读取", "没能读取",
        "看不到图片", "视觉模型", "超时", "限流", "image reading failed",
        "cannot read the image", "unable to read", "timeout", "rate limit",
    )
    visual_claim_markers = (
        "图中", "画面中", "这张图是", "图片内容", "我看到", "识别到",
        "文字为", "翻译如下", "the image shows", "i can see",
    )
    return any(marker in lowered for marker in failure_markers) and not any(
        marker in lowered for marker in visual_claim_markers
    )


def _tool_name(call: Any) -> str:
    """Read a tool-call name from object or dict shapes.

    Why: ai_step and tests may pass Provider ToolCall objects, ParsedToolCall
    objects, or dicts. How: support attribute and dict access. Purpose: keep the
    handler decoupled from one formatter implementation.
    """
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")
