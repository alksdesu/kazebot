"""模型的上下文窗口有多大，以及据此收紧的压缩阈值。

窗口依次取节点 yaml 的 context_window、runtime 的 engine.compact.context_windows、内置的常见模型族表，都查不到就不调整。
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import Any

from clonoth_runtime import get_int

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_RESERVE_TOKENS = 20_000
# 估算有误差，软阈值再给当轮推理留一点余地
SOFT_PERCENT_OF_USABLE = 85
# 和启动时 keep_recent_tokens 的封顶比例一致：保留量必须明显小于触发阈值，否则压完仍在阈值之上
KEEP_RECENT_SHARE_OF_THRESHOLD = 0.8
_MIN_WINDOW_TOKENS = 1_000
_MAX_WINDOW_TOKENS = 100_000_000
_OUTPUT_LIMIT_OPTIONS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

# 按顺序匹配规范化后的模型名，先匹配上的生效。值取各家文档写明的上下文长度；
# GPT-5 系列取的是输入上限 272k（400k 总窗口里含 128k 输出），不是总窗口。
BUILTIN_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude-*", 200_000),
    ("gpt-4.1*", 1_047_576),
    ("gpt-4o*", 128_000),
    ("chatgpt-4o*", 128_000),
    ("gpt-4-turbo*", 128_000),
    ("gpt-5-chat*", 128_000),
    ("gpt-5*", 272_000),
    ("o1-mini*", 128_000),
    ("o1-preview*", 128_000),
    ("o1*", 200_000),
    ("o3*", 200_000),
    ("o4-mini*", 200_000),
    ("gemini-1.5-pro*", 2_097_152),
    ("gemini-1.5-*", 1_048_576),
    ("gemini-2.0-*", 1_048_576),
    ("gemini-2.5-*", 1_048_576),
    ("gemini-3*", 1_048_576),
    ("deepseek-chat", 128_000),
    ("deepseek-reasoner", 128_000),
)

_SIZE_SUFFIX = re.compile(r"\[(?P<value>\d+(?:\.\d+)?)(?P<unit>[km])\]$")
_VENDOR_SEGMENT = re.compile(r"^[a-z]+\.(?=[a-z])")
_UNIT_TOKENS = {"k": 1_000, "m": 1_000_000}


@dataclass(frozen=True)
class ModelName:
    raw: str
    bare: str
    core: str
    declared: int | None


def _window_value(raw: Any) -> int | None:
    """配置里写的窗口大小：正整数夹到合理范围，0 表示不按窗口调整，认不出返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if not isinstance(raw, int) or raw < 0:
        return None
    if raw == 0:
        return 0
    return max(_MIN_WINDOW_TOKENS, min(_MAX_WINDOW_TOKENS, raw))


def parse_model_name(model: Any) -> ModelName:
    """去掉渠道前缀（openai/、models/、us.anthropic.）、版本后缀（:0、@20241022）和窗口后缀（[1m]）。"""
    raw = str(model or "").strip().lower()
    declared: int | None = None
    bare = raw
    suffix = _SIZE_SUFFIX.search(raw)
    if suffix is not None:
        declared = _window_value(int(float(suffix.group("value")) * _UNIT_TOKENS[suffix.group("unit")]))
        bare = raw[:suffix.start()].strip()
    core = bare.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0]
    while _VENDOR_SEGMENT.match(core):
        core = _VENDOR_SEGMENT.sub("", core, count=1)
    return ModelName(raw=raw, bare=bare, core=core, declared=declared or None)


def _node_window(node: Any) -> tuple[bool, int | None]:
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict) or extra.get("context_window") is None:
        return False, None
    value = _window_value(extra.get("context_window"))
    if value is None:
        logger.warning(
            "node %s has an invalid context_window %r; ignored", getattr(node, "id", "?"), extra.get("context_window"),
        )
        return False, None
    return True, value or None


def _configured_windows(runtime_cfg: Any) -> list[tuple[str, int]]:
    engine = runtime_cfg.get("engine") if isinstance(runtime_cfg, dict) else None
    compact = engine.get("compact") if isinstance(engine, dict) else None
    mapping = compact.get("context_windows") if isinstance(compact, dict) else None
    if not isinstance(mapping, dict):
        return []
    windows: list[tuple[str, int]] = []
    for pattern, raw in mapping.items():
        key = str(pattern or "").strip().lower()
        value = _window_value(raw)
        if not key or value is None:
            logger.warning("engine.compact.context_windows entry %r: %r is not a token count; ignored", pattern, raw)
            continue
        windows.append((key, value))
    return windows


def context_window_for(model: Any, *, runtime_cfg: Any = None, node: Any = None) -> int | None:
    """这个模型的上下文窗口 token 数；None 表示不知道，压缩阈值不按窗口调整。

    优先级：节点 yaml 的 context_window > runtime 的 engine.compact.context_windows（glob，按书写顺序）
    > 模型名里写明的窗口后缀（[1m]、[200k]）> 内置的常见模型族表。配成 0 表示这个模型不按窗口调整。
    """
    found, value = _node_window(node)
    if found:
        return value
    name = parse_model_name(model)
    if not name.raw:
        return None
    for pattern, tokens in _configured_windows(runtime_cfg):
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in (name.raw, name.bare, name.core)):
            return tokens or None
    if name.declared:
        return name.declared
    for pattern, tokens in BUILTIN_CONTEXT_WINDOWS:
        if fnmatch.fnmatchcase(name.core, pattern):
            return tokens or None
    return None


def provider_output_limit(provider: Any) -> int | None:
    """渠道或节点给这个 provider 配的回复上限（max_tokens 一类），没配返回 None。"""
    # provider 没有公开生效参数的接口，只能读它构造时合并好的 OptionSet（runtime < 渠道 < 节点）
    options = getattr(provider, "_options", None)
    getter = getattr(options, "get", None)
    if not callable(getter):
        return None
    for key in _OUTPUT_LIMIT_OPTIONS:
        try:
            value = getter(key)
        except Exception:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            continue
        return int(value)
    return None


def output_reserve_tokens(provider: Any, runtime_cfg: Any) -> int:
    limit = provider_output_limit(provider)
    if limit is not None:
        return limit
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return get_int(
        cfg, "engine.compact.output_reserve_tokens", DEFAULT_OUTPUT_RESERVE_TOKENS, min_value=0, max_value=1_000_000,
    )


@dataclass(frozen=True)
class EffectiveThresholds:
    soft: int
    hard: int
    usable: int
    reserve: int


def effective_thresholds(window: int, *, soft: int, hard: int, reserve: int) -> EffectiveThresholds:
    """按窗口收紧的阈值：usable = 窗口 − 输出预留，软阈值不超过 usable 的 85%，硬阈值不超过 usable。

    配成 0（关掉压缩）的阈值保持 0。预留最多占窗口一半，回复上限配得比窗口还大时阈值不至于变成负数。
    """
    reserve = max(0, min(int(reserve), window // 2))
    usable = window - reserve
    return EffectiveThresholds(
        soft=min(soft, usable * SOFT_PERCENT_OF_USABLE // 100) if soft > 0 else soft,
        hard=min(hard, usable) if hard > 0 else hard,
        usable=usable,
        reserve=reserve,
    )


def threshold_report(ls: Any) -> dict[str, int]:
    """随 context_usage 上报的有效阈值，supervisor 的 get_context_window 照它回答。"""
    report = {
        "compact_threshold": int(getattr(ls, "compact_threshold", 0) or 0),
        "compact_hard_threshold": int(getattr(ls, "compact_hard_threshold", 0) or 0),
    }
    window = getattr(ls, "context_window", None)
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        report["context_window"] = window
    return report
