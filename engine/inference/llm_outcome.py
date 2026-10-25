"""模型响应怎么处理：同渠道重试、换备用渠道、被动压缩，以及回复是不是被截断了。

provider 报了 error_kind / finish_reason 就按它判；没报（旧 provider）时退回状态码和错误文本。
"""
from __future__ import annotations

import copy
import dataclasses
from typing import Any

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

RETRYABLE_ERROR_KINDS = frozenset({
    "rate_limit", "overloaded", "timeout", "network", "server", "incomplete", "invalid_response",
})
# 同一渠道重试无用、换一家可能就过的失败也走备用链；context_length 交给被动压缩，换渠道窗口多半更小。
FALLBACK_ERROR_KINDS = RETRYABLE_ERROR_KINDS | frozenset({"content_filter", "auth", "bad_request", "unknown"})

TRUNCATED_FINISH_REASONS = frozenset({"length", "incomplete"})
FILTERED_FINISH_REASONS = frozenset({"content_filter", "refusal"})

# 中转把上游 key 池耗尽报成 400/402/403；只认明确的额度措辞，普通参数错误不重试。
_RESOURCE_LIMIT_CLIENT_STATUS_CODES = frozenset({400, 402, 403})
_RESOURCE_LIMIT_MARKERS = (
    "quota exhausted", "credits exhausted", "insufficient quota", "insufficient_quota",
    "insufficient credits", "insufficient credit", "insufficient balance",
    "too many requests", "rate limit", "rate_limit",
)
# HTTP 200 的 SSE 流里夹带的上游临时错误
_STREAM_SERVER_ERROR_MARKERS = (
    "429", "503", "rate_limit", "rate limit", "overloaded", "capacity", "too many requests",
    "server_error", "internal_error", "service_unavailable",
)
# 403 既可能是鉴权拒绝也可能是内容审核；审核换一家可能就过，鉴权换了也没用
_CONTENT_MODERATION_MARKERS = (
    "safeguard", "content_filter", "refused", "safety", "cyber",
    "not allowed", "community guidelines", "moderation",
)
_CONTEXT_OVERFLOW_MARKERS = (
    "too long", "too large", "context_length", "max_tokens", "maximum context", "token limit",
)


def _normalized(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def error_kind(resp: Any) -> str | None:
    return _normalized(getattr(resp, "error_kind", None))


def finish_reason(resp: Any) -> str | None:
    return _normalized(getattr(resp, "finish_reason", None))


def _error_text(resp: Any) -> str:
    return str(getattr(resp, "error", None) or "").lower()


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def is_retryable(resp: Any) -> bool:
    """同一渠道再请求一次有没有意义。"""
    if getattr(resp, "ok", False):
        return False
    kind = error_kind(resp)
    if kind is not None:
        return kind in RETRYABLE_ERROR_KINDS
    status = getattr(resp, "status_code", None)
    if status is None or status <= 0:
        return True
    if status in RETRYABLE_STATUS_CODES:
        return True
    if status in _RESOURCE_LIMIT_CLIENT_STATUS_CODES:
        return _has_marker(_error_text(resp), _RESOURCE_LIMIT_MARKERS)
    if status == 200:
        return _has_marker(_error_text(resp), _STREAM_SERVER_ERROR_MARKERS)
    return False


def should_fallback(resp: Any) -> bool:
    """主渠道失败后值不值得换备用渠道再试。"""
    if getattr(resp, "ok", True):
        return False
    kind = error_kind(resp)
    if kind is not None:
        return kind in FALLBACK_ERROR_KINDS
    status = getattr(resp, "status_code", None)
    error = _error_text(resp)
    if status is None or status in RETRYABLE_STATUS_CODES:
        return True
    if status == 200 and error:
        return True
    if status == 403 and error:
        return _has_marker(error, _CONTENT_MODERATION_MARKERS)
    if 400 <= status < 500:
        return False
    return True


def is_context_overflow(resp: Any) -> bool:
    """请求超出模型上下文窗口，该走被动压缩。"""
    if getattr(resp, "ok", True):
        return False
    kind = error_kind(resp)
    if kind is not None:
        return kind == "context_length"
    return getattr(resp, "status_code", None) == 413 or _has_marker(_error_text(resp), _CONTEXT_OVERFLOW_MARKERS)


def is_truncated(resp: Any) -> bool:
    """回复因输出长度上限等原因没写完。"""
    return finish_reason(resp) in TRUNCATED_FINISH_REASONS


def is_filtered(resp: Any) -> bool:
    """回复被内容审核拦下或模型拒答。"""
    return finish_reason(resp) in FILTERED_FINISH_REASONS


def with_changes(resp: Any, **changes: Any) -> Any:
    """复制一份响应再改几个字段，provider 新加的字段原样带过去。"""
    clone = dataclasses.replace(resp) if dataclasses.is_dataclass(resp) and not isinstance(resp, type) else copy.copy(resp)
    for name, value in changes.items():
        setattr(clone, name, value)
    return clone
