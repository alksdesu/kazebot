"""模型的回复命中输出规则时这次回复作废：工具不执行、回复不进历史，带着规则提示让这一步重来。

规则是 strategy: output 的 skill：frontmatter 写 trigger（正则）、scope（text / tool_args / both）、max_fires，正文就是规则。
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from engine.inference.loop_state import _persist_ctx
from engine.protocol import ACTION_PREEMPTED, TaskAction

from .knowledge_inject import DEFAULT_OUTPUT_MAX_FIRES, DEFAULT_OUTPUT_SCOPE, output_rule_skills
from .result import hook_result

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "handler_class": "OutputRuleGuard",
    "hook_points": [
        ("after_llm_call", "handle"),
        ("on_task_end", "forget"),
        ("on_task_error", "forget"),
    ],
    # 排在备用渠道（-20）之后：查的是最终要用的那份回复
    "priority": -30,
}

TRIGGERED_EVENT = "output_rule_triggered"
TEXT_SCOPE = "text"
TOOL_ARGS_SCOPE = "tool_args"
BOTH_SCOPE = "both"
# 正则没法中途打断，只能先把输入截短：单段和整份回复各有上限
MAX_SEGMENT_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000
_RULE_TEXT_LIMIT = 2_000
_MAX_ARGUMENT_DEPTH = 32
_SENTENCE_ENDS = "。！？.!?"

# 按任务计触发次数：任务挂起恢复后换了新的 loop state，同一个进程里照样接得上
_TASK_FIRES: OrderedDict[str, Counter[str]] = OrderedDict()
_TASK_FIRES_LIMIT = 1024


@dataclass(frozen=True)
class OutputRule:
    key: str
    name: str
    pattern: re.Pattern[str]
    scope: str
    max_fires: int
    text: str

    def covers(self, where: str) -> bool:
        return self.scope == BOTH_SCOPE or self.scope == where


@dataclass(frozen=True)
class Segment:
    """回复里拿去匹配的一段：正文，或者某个工具调用参数里的一个字符串。"""

    where: str
    text: str
    tool: str = ""


def node_output_rules(workspace_root: Any, node: Any) -> list[OutputRule]:
    rules: list[OutputRule] = []
    for entry in output_rule_skills(workspace_root, node):
        text = str(entry.get("content") or "").strip() or str(entry.get("description") or "").strip()
        if len(text) > _RULE_TEXT_LIMIT:
            text = text[:_RULE_TEXT_LIMIT] + "…"
        rules.append(OutputRule(
            key=str(entry.get("path") or entry.get("id") or ""),
            name=str(entry.get("id") or ""),
            pattern=entry["pattern"],
            scope=str(entry.get("scope") or DEFAULT_OUTPUT_SCOPE),
            max_fires=int(entry.get("max_fires") or DEFAULT_OUTPUT_MAX_FIRES),
            text=text,
        ))
    return rules


def retry_hint(rule: OutputRule) -> str:
    text = rule.text
    if not text:
        return f"你刚才的输出触发了规则「{rule.name}」。请按规则重新回答。"
    if text[-1] not in _SENTENCE_ENDS:
        text += "。"
    return f"你刚才的输出触发了规则「{rule.name}」：{text}请按规则重新回答。"


def _string_leaves(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > _MAX_ARGUMENT_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item, depth + 1)


def _reply_text(response: Any, formatter: Any) -> str:
    # json 模式的正文里夹着工具调用块，按 formatter 剥掉才是给人看的那部分
    if formatter is not None:
        try:
            return str(formatter.get_plain_text(response) or "")
        except Exception:
            logger.debug("output rules: stripping tool markers failed", exc_info=True)
    return str(getattr(response, "text", None) or "")


def _reply_calls(response: Any, formatter: Any) -> list[Any]:
    calls = list(getattr(response, "tool_calls", None) or [])
    if calls or formatter is None:
        return calls
    # json 模式此时还没解析，工具调用还写在正文里
    try:
        return list(formatter.parse_tool_calls(response) or [])
    except Exception:
        logger.debug("output rules: parsing tool calls from the reply failed", exc_info=True)
        return []


def reply_segments(response: Any, formatter: Any, scopes: Iterable[str]) -> list[Segment]:
    """本次回复里要匹配的各段，每段截到 MAX_SEGMENT_CHARS，加起来不超过 MAX_TOTAL_CHARS，正文排在最前。"""
    wanted = set(scopes)
    candidates: list[Segment] = []
    if TEXT_SCOPE in wanted:
        text = _reply_text(response, formatter)
        if text:
            candidates.append(Segment(TEXT_SCOPE, text))
    if TOOL_ARGS_SCOPE in wanted:
        for call in _reply_calls(response, formatter):
            name = str(getattr(call, "name", "") or "")
            for leaf in _string_leaves(getattr(call, "arguments", None)):
                if leaf:
                    candidates.append(Segment(TOOL_ARGS_SCOPE, leaf, name))
    bounded: list[Segment] = []
    budget = MAX_TOTAL_CHARS
    for segment in candidates:
        if budget <= 0:
            break
        text = segment.text[:min(MAX_SEGMENT_CHARS, budget)]
        budget -= len(text)
        bounded.append(Segment(segment.where, text, segment.tool))
    return bounded


def first_violation(rules: Iterable[OutputRule], segments: list[Segment]) -> tuple[OutputRule, Segment] | None:
    for rule in rules:
        for segment in segments:
            if rule.covers(segment.where) and rule.pattern.search(segment.text):
                return rule, segment
    return None


def _scopes(rules: Iterable[OutputRule]) -> set[str]:
    scopes: set[str] = set()
    for rule in rules:
        scopes.update((TEXT_SCOPE, TOOL_ARGS_SCOPE) if rule.scope == BOTH_SCOPE else (rule.scope,))
    return scopes


def task_fires(ls: Any) -> Counter[str]:
    """这个任务里每条规则已经触发了几次。"""
    task_id = str(getattr(getattr(ls, "rctx", None), "task_id", "") or "")
    if not task_id:
        fires = getattr(ls, "_output_rule_fires", None)
        if not isinstance(fires, Counter):
            fires = Counter()
            ls._output_rule_fires = fires
        return fires
    fires = _TASK_FIRES.get(task_id)
    if fires is None:
        fires = Counter()
        _TASK_FIRES[task_id] = fires
        while len(_TASK_FIRES) > _TASK_FIRES_LIMIT:
            _TASK_FIRES.popitem(last=False)
    else:
        _TASK_FIRES.move_to_end(task_id)
    return fires


def _is_system_task(rctx: Any) -> bool:
    return bool((getattr(rctx, "task_context", None) or {}).get("is_system_task"))


class OutputRuleGuard:
    """after_llm_call：回复命中了还有触发次数的规则，就作废这次回复、追加规则提示，让本步重来。"""

    name = "output_rules"
    priority = -30

    async def handle(self, ctx: Any) -> Any | None:
        extra = ctx.extra if isinstance(getattr(ctx, "extra", None), dict) else {}
        ls = extra.get("loop_state")
        response = getattr(ctx, "response", None)
        if ls is None or response is None or not getattr(response, "ok", False):
            return None
        rctx = getattr(ls, "rctx", None)
        if rctx is None or _is_system_task(rctx):
            return None
        rules = await asyncio.to_thread(node_output_rules, rctx.workspace_root, ls.node)
        if not rules:
            return None
        fires = task_fires(ls)
        live = [rule for rule in rules if fires[rule.key] < rule.max_fires]
        if not live:
            return None
        hit = first_violation(live, reply_segments(response, getattr(ls, "formatter", None), _scopes(live)))
        if hit is None:
            return None
        rule, segment = hit
        fires[rule.key] += 1
        logger.warning(
            "node %s reply matched output rule %s in %s (fire %d/%d); discarding it",
            ls.node.id, rule.name, segment.where, fires[rule.key], rule.max_fires,
        )
        await rctx.emit_event(TRIGGERED_EVENT, {
            "node_id": ls.node.id,
            "task_id": rctx.task_id,
            "step": ctx.step,
            "rule": rule.name,
            "path": rule.key,
            "scope": segment.where,
            "tool_name": segment.tool,
            "fires": fires[rule.key],
            "max_fires": rule.max_fires,
        })
        if getattr(ls, "preempt_after_step", False):
            return hook_result(action=TaskAction(
                action=ACTION_PREEMPTED,
                node_id=ls.node.id,
                context_ref=_persist_ctx(ls, ctx.step + 1),
                summary="任务被软打断，上下文已保存。",
            ))
        # 只活在运行期：发给模型，不落盘
        ls.messages.append({"role": "user", "content": retry_hint(rule), "_retry_hint": True})
        ls.use_stream = ls.streaming
        return hook_result(skip_step=True, reason="output_rule", modified=True)

    async def forget(self, ctx: Any) -> None:
        task_id = str(getattr(getattr(ctx, "rctx", None), "task_id", "") or "")
        if task_id:
            _TASK_FIRES.pop(task_id, None)
