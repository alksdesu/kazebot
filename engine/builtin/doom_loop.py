"""连续几轮用完全相同的参数调用同一组真工具时，整轮拦下，逼模型换方法或者 finish 说明卡在哪。

历史直接从 ls.messages 里本任务的 assistant 工具调用推导：任务恢复后照样接得上，
压缩深拷贝或换掉消息列表也不会和另存的一份状态对不上。中途有新的用户输入就重新计数。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from clonoth_runtime import get_int, parse_bool
from engine.compact import is_tool_result_message
from engine.inference.loop_state import TOOL_RESULT_ATTACHMENT_TYPE
from engine.inference.tool_format import invalid_arguments
from toolbox.tool_names import is_pseudo_tool_name

from .result import hook_result

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "handler_class": "DoomLoopGuard",
    "hook_points": [
        ("before_tool_call", "handle"),
    ],
    # 排在 finish_guard(100) 前面：整轮重复时整轮都不执行，再单独拒 finish 已经没有意义
    "priority": 105,
}

def doom_loop_threshold(runtime_cfg: Any) -> int:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    threshold = get_int(cfg, "engine.doom_loop.threshold", 3, min_value=0, max_value=50)
    # 1 等于每一轮都拦，没有意义，按最小的 2 算
    return 2 if threshold == 1 else threshold


def call_signature(name: str, arguments: Any) -> str:
    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(arguments)
    return f"{name}\x1f{canonical}"


class _RepeatableTools:
    """spec 标了 repeatable 的工具（轮询类）不计入；同一轮里只查一次注册表。"""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._known: dict[str, bool] = {}

    def __contains__(self, name: object) -> bool:
        key = str(name or "")
        if key not in self._known:
            self._known[key] = self._lookup(key)
        return self._known[key]

    def _lookup(self, name: str) -> bool:
        get_spec = getattr(self._registry, "get_spec", None)
        if not callable(get_spec):
            return False
        try:
            spec = get_spec(name)
        except Exception:
            logger.debug("doom_loop: spec lookup failed for %s", name, exc_info=True)
            return False
        return isinstance(spec, dict) and bool(parse_bool(spec.get("repeatable"), False))


def _signatures(calls: Iterable[tuple[str, Any]], repeatable: _RepeatableTools) -> frozenset[str]:
    return frozenset(call_signature(name, arguments) for name, arguments in calls if name not in repeatable)


def _call_parts(call: Any) -> tuple[str, Any] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(call.get("name") or function.get("name") or "").strip()
    arguments = call.get("arguments", function.get("arguments"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return None
    return name, {} if arguments is None else arguments


def _meta(message: dict[str, Any]) -> dict[str, Any]:
    meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else message.get("meta")
    return meta if isinstance(meta, dict) else {}


def _round_signatures(
    message: dict[str, Any], allowed: set[str], repeatable: _RepeatableTools,
) -> frozenset[str] | None:
    """一条 assistant 消息里真正会执行的真工具调用；一个都没有（只调了伪工具或全被拒）返回 None。"""
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    real: list[tuple[str, Any]] = []
    for call in calls:
        parts = _call_parts(call)
        if parts is None:
            continue
        name, arguments = parts
        if not name or is_pseudo_tool_name(name) or name not in allowed:
            continue
        if invalid_arguments(arguments) is not None:
            continue
        real.append((name, arguments))
    if not real:
        return None
    return _signatures(real, repeatable)


def _is_new_input(message: dict[str, Any]) -> bool:
    if message.get("role") != "user" or is_tool_result_message(message):
        return False
    return _meta(message).get("message_type") != TOOL_RESULT_ATTACHMENT_TYPE


def repeated_rounds(
    loop_state: Any,
    current: frozenset[str],
    *,
    limit: int,
    current_message: Any = None,
    repeatable: _RepeatableTools | None = None,
) -> int:
    """本任务里紧挨着本轮、和本轮签名完全相同的前几轮有多少，数到 limit 为止。"""
    messages = list(getattr(loop_state, "messages", None) or [])
    end = len(messages)
    if current_message is not None:
        end = next((index for index in range(len(messages) - 1, -1, -1) if messages[index] is current_message), end)
    task_id = str(getattr(getattr(loop_state, "rctx", None), "task_id", "") or "")
    allowed = set(getattr(loop_state, "allowed_real_tools", None) or ())
    lookup = repeatable if repeatable is not None else _RepeatableTools(getattr(loop_state, "registry", None))
    streak = 0
    for message in reversed(messages[:end]):
        if not isinstance(message, dict):
            continue
        if message.get("_ephemeral") or message.get("_retry_hint") or message.get("_dynamic"):
            continue
        if message.get("role") == "assistant":
            if str(_meta(message).get("source_task_id") or "") != task_id:
                break
            signatures = _round_signatures(message, allowed, lookup)
            if signatures is None:
                continue
            if signatures != current:
                break
            streak += 1
            if streak >= limit:
                break
            continue
        if _is_new_input(message):
            break
    return streak


class DoomLoopGuard:
    """同一个签名或同一组签名连续出现在最近 N 轮，就拦下本轮。"""

    name = "doom_loop"
    priority = 105

    async def handle(self, ctx: Any) -> Any | None:
        if getattr(ctx, "tool_call", None) is not None:
            return None
        extra = ctx.extra if isinstance(getattr(ctx, "extra", None), dict) else {}
        loop_state = extra.get("loop_state")
        current_calls = extra.get("real_tool_calls")
        if loop_state is None or not current_calls:
            return None
        threshold = doom_loop_threshold(getattr(loop_state, "runtime_cfg", None))
        if threshold <= 0:
            return None
        repeatable = _RepeatableTools(getattr(loop_state, "registry", None))
        pairs = [
            (str(call.get("name") or ""), call.get("arguments") or {})
            for call in current_calls
            if isinstance(call, dict)
        ]
        current = _signatures(pairs, repeatable)
        if not current:
            return None
        streak = repeated_rounds(
            loop_state, current,
            limit=threshold - 1,
            current_message=extra.get("assistant_message"),
            repeatable=repeatable,
        )
        if streak < threshold - 1:
            return None
        names = "、".join(dict.fromkeys(name for name, _arguments in pairs if name not in repeatable))
        logger.warning(
            "Blocked a repeated tool round (node=%s, step=%s, rounds=%d, tools=%s)",
            getattr(getattr(ctx, "node", None), "id", ""), getattr(ctx, "step", -1), threshold, names,
        )
        return hook_result(
            block=True,
            reason="doom_loop",
            error_message=(
                f"你已经连续 {threshold} 轮用完全相同的参数调用 {names}，结果不会变。"
                "换一种方法，或者用 finish 说明卡在哪里。"
            ),
        )
