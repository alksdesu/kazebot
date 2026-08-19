"""收到 4xx 时按错误文本改一次参数，重试。

各家把参数互斥做成硬错误（新版 Claude 拒固定预算思考、Gemini 拒 level 与 budget
同传），而模型名经反代改写后不可靠，判不出该发哪套。所以不猜：配错了就按对面
报的错改一次，并留日志提醒回去改配置。

修正写在参数覆盖层而不是请求体上，所以只会撞一次，之后每次请求都已经是对的。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .options import OptionSet

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DegradeRule:
    """apply 收到参数集与小写错误文本，就地修正并返回是否真的改动了。"""

    name: str
    match: tuple[str, ...]
    apply: Callable[[OptionSet, str], bool]
    advice: str = ""

    def hits(self, error_text: str) -> bool:
        return any(fragment in error_text for fragment in self.match)


def degrade(
    rules: tuple[DegradeRule, ...],
    options: OptionSet,
    error_text: str,
    *,
    provider: str,
    model: str,
) -> str | None:
    """命中则修正参数并返回规则名，没命中返回 None。"""
    lowered = (error_text or "").lower()
    if not lowered:
        return None
    for rule in rules:
        if not rule.hits(lowered):
            continue
        if not rule.apply(options, lowered):
            # 命中了错误但已经没得可改，再试也是同一个 400。
            continue
        log.warning("%s/%s 被拒后按「%s」重试。%s", provider, model, rule.name, rule.advice)
        return rule.name
    return None


def set_option(key: str, value: Any) -> Callable[[OptionSet, str], bool]:
    def _apply(options: OptionSet, _error: str) -> bool:
        return options.override(key, value)
    return _apply


def unset_options(*keys: str) -> Callable[[OptionSet, str], bool]:
    def _apply(options: OptionSet, _error: str) -> bool:
        # 先全部清掉再判断：any() 遇到生成器会短路，只清掉第一个就返回。
        cleared = [options.override(key, None) for key in keys]
        return any(cleared)
    return _apply
