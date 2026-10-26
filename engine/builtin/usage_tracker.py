from __future__ import annotations

from typing import Any

from engine.context import ServingProvider

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "UsageTracker",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": 0,
}


# cache_read_tokens / reasoning_tokens 是各家 provider 归一化后的可选项：
# 缓存命中按一折计价、推理量算在输出里，不累加就看不出开缓存和开思考的实际代价。
_USAGE_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)


def accumulate_usage(total: dict, usage: dict) -> bool:
    """把一次请求的用量加进任务累计，返回有没有加上东西。"""
    added = False
    for usage_key in _USAGE_KEYS:
        if usage_key in usage:
            total[usage_key] = total.get(usage_key, 0) + usage[usage_key]
            added = True
    return added


def note_serving_provider(rctx: Any, provider: Any) -> None:
    """记下这次出答案的 provider，任务结束时 TaskRecord 记最后一次的。"""
    if rctx is not None:
        rctx.serving_provider = ServingProvider.of(provider)


class UsageTracker:
    """Accumulate provider token usage after each LLM call."""

    name = "usage_tracker"
    priority = 0

    async def handle(self, ctx: Any) -> Any | None:
        """Move response.usage into RunContext.total_usage.

        Why: ai_step.py should not own bookkeeping that can live in an after-call
        hook. How: read the ProviderResponse usage dict and add known token keys
        to rctx.total_usage. Purpose: keep task-level usage records identical
        while making usage tracking pluggable.
        """
        if ctx.rctx is None:
            return None
        # 只认成功的那次：主渠道失败、备用渠道顶上时由 fallback_provider 记它自己
        if getattr(ctx.response, "ok", False):
            note_serving_provider(ctx.rctx, ctx.provider)
        usage = getattr(ctx.response, "usage", None)
        if not usage or not isinstance(usage, dict):
            return None
        if accumulate_usage(ctx.rctx.total_usage, usage):
            return hook_result(modified=True)
        return None
