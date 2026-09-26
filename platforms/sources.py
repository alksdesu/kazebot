"""操作来源分类。

硬规则只关心两件事：操作来自内部入口还是外部平台，发起人是不是管理员。
内部入口是白名单，其余一律按外部平台处理；没登记过的来源顺带告警。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from .registry import find_platform, normalize_name

logger = logging.getLogger(__name__)

# 控制台、命令行、定时器、子会话、系统任务、MOKAMOKA 桌面端。
INTERNAL_SOURCES: frozenset[str] = frozenset(
    {"web", "cli", "scheduler", "agent", "system", "internal", "moka"}
)

_UNKNOWN = "unknown"
_warned: set[str] = set()
_warned_lock = threading.Lock()


@dataclass(frozen=True)
class OperationSource:
    external: bool
    # 外部平台是平台 id，内部入口是入口名；拒绝文案用它称呼发起方。
    label: str
    declared: bool


def conversation_key_prefix(conversation_key: Any) -> str:
    head, sep, _ = str(conversation_key or "").strip().partition(":")
    return normalize_name(head) if sep else ""


def classify_source_name(name: Any) -> OperationSource | None:
    key = normalize_name(name)
    if not key:
        return None
    if key in INTERNAL_SOURCES:
        return OperationSource(external=False, label=key, declared=True)
    platform = find_platform(key)
    if platform is not None:
        return OperationSource(external=True, label=platform.id, declared=True)
    return OperationSource(external=True, label=key, declared=False)


def is_external_platform(name: Any) -> bool:
    source = classify_source_name(name)
    return source is not None and source.external


def classify_operation_source(
    *, conversation_key: Any = "", channel: Any = "", platform: Any = "",
) -> OperationSource:
    """任一信号指向外部平台就按外部处理，全部缺席也按外部处理。"""
    # Dream 这类系统任务的会话键是记忆命名空间，不带前缀，只能靠通道认出来。
    key_signal = conversation_key_prefix(conversation_key) or channel
    signals = [
        source
        for source in (classify_source_name(platform), classify_source_name(key_signal))
        if source is not None
    ]
    for source in signals:
        if not source.declared:
            _warn_undeclared(source.label)
    if not signals:
        _warn_undeclared(_UNKNOWN)
        return OperationSource(external=True, label=_UNKNOWN, declared=False)
    return next((source for source in signals if source.external), signals[0])


def _warn_undeclared(label: str) -> None:
    with _warned_lock:
        if label in _warned:
            return
        _warned.add(label)
    logger.warning("operation source %r is not declared; treating it as an external platform", label)
