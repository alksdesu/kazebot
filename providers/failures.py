"""上游失败的统一表示：读错误体、判 error_kind、拼失败响应，以及 chat 入口的兜底。

五家把同一种失败说成五种样子，engine 只看 error_kind 决定重试、换渠道还是压缩上下文，
所以判定只在这里做一次。
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from plugin_sdk import PluginExecutionError
from tools._wire import error_fields

from .base import ProviderResponse
from .plugin_transport import PluginTransportError

log = logging.getLogger(__name__)

_ERROR_TEXT_LIMIT = 500


def _markers(*phrases: str) -> re.Pattern[str]:
    # 数字状态码按整词匹配，免得 "max_tokens: 5000" 里的 500 被当成服务端错误。
    parts = [rf"\b{re.escape(p)}\b" if p.isdigit() else re.escape(p) for p in phrases]
    return re.compile("|".join(parts))


_CONTEXT = _markers(
    "context_length_exceeded", "context length", "context window", "maximum context",
    "prompt is too long", "input is too long", "too many input tokens",
    "input token count", "exceeds the maximum number of tokens", "reduce the length of the messages",
    "request too large", "request_too_large",
)
_CONTENT = _markers(
    "content_filter", "content filter", "content_policy", "content policy", "content management policy",
    "responsible ai", "safety system", "safeguard", "moderation", "flagged", "usage policies",
    "usage policy", "prohibited_content", "blocked by safety",
)
# 403 下沿用 engine 换渠道判定里认的那几个词：Claude 的安全拦截走 403，与鉴权失败共用状态码。
_MODERATION_403 = _markers(
    "safeguard", "content_filter", "refused", "safety", "cyber", "not allowed",
    "community guidelines", "moderation", "usage policies", "usage policy", "flagged",
)
_QUOTA = _markers(
    "quota exhausted", "credits exhausted", "insufficient quota", "insufficient_quota",
    "insufficient credits", "insufficient credit", "insufficient balance", "exceeded your current quota",
    "quota exceeded", "resource_exhausted", "resource exhausted",
)
_RATE = _markers("rate limit", "rate_limit", "too many requests", "429", "requests per", "tokens per min")
_OVERLOAD = _markers(
    "overloaded", "overload", "capacity", "server is busy", "service unavailable", "service_unavailable",
    "temporarily unavailable", "503", "529",
)
_TIMEOUT = _markers("timeout", "timed out", "deadline exceeded", "deadline_exceeded", "504")
_SERVER = _markers(
    "server_error", "server error", "internal_error", "internal error", "bad gateway", "api_error", "500", "502",
)
_AUTH = _markers(
    "invalid api key", "invalid_api_key", "incorrect api key", "api key not valid", "invalid x-api-key",
    "unauthorized", "unauthenticated", "authentication", "permission_denied", "permission denied",
)

# 上游给出的错误类型 / 代码 → error_kind。键一律小写；Gemini 的 status 也在这里。
_TYPE_KINDS: dict[str, str] = {
    "overloaded_error": "overloaded",
    "rate_limit_error": "rate_limit",
    "api_error": "server",
    "authentication_error": "auth",
    "permission_error": "auth",
    "not_found_error": "bad_request",
    "request_too_large": "context_length",
    "invalid_request_error": "bad_request",
    "server_error": "server",
    "insufficient_quota": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "requests": "rate_limit",
    "tokens": "rate_limit",
    "context_length_exceeded": "context_length",
    "content_filter": "content_filter",
    "content_policy_violation": "content_filter",
    "invalid_api_key": "auth",
    "invalid_prompt": "bad_request",
    "vector_store_timeout": "timeout",
    "resource_exhausted": "rate_limit",
    "unavailable": "overloaded",
    "internal": "server",
    "deadline_exceeded": "timeout",
    "permission_denied": "auth",
    "unauthenticated": "auth",
    "invalid_argument": "bad_request",
    "failed_precondition": "bad_request",
    "not_found": "bad_request",
}


@dataclass(frozen=True)
class UpstreamError:
    """上游自己报出来的一次失败。type 与 code 取不到时是空串。"""

    message: str
    type: str = ""
    code: str = ""

    def haystack(self) -> str:
        return " ".join(part for part in (self.type, self.code, self.message) if part).lower()


def read_error(payload: Any, *, fallback: str = "") -> UpstreamError:
    """从错误响应体里取出人话、类型和代码。payload 可以是 bytes、文本或已经解析好的 JSON。"""
    original = ""
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", errors="replace")
    if isinstance(payload, str):
        original = payload.strip()
        try:
            payload = json.loads(original) if original else None
        except ValueError:
            return UpstreamError(original[:_ERROR_TEXT_LIMIT] or fallback)
    elif payload is not None:
        original = json.dumps(payload, ensure_ascii=False, default=str)
    message, kind, code = error_fields(payload)
    return UpstreamError(message or original[:_ERROR_TEXT_LIMIT] or fallback, kind, code)


def _code_as_status(code: str) -> int | None:
    if len(code) == 3 and code.isdigit() and 400 <= int(code) < 600:
        return int(code)
    return None


def _resource_limited(text: str) -> bool:
    # 不少中转把号池耗尽、限流包成 400/402/403 发回来。
    return bool(_QUOTA.search(text) or _RATE.search(text))


def _client_error_kind(text: str) -> str | None:
    if _CONTENT.search(text):
        return "content_filter"
    if _resource_limited(text):
        return "rate_limit"
    return None


def _status_kind(status: int, text: str) -> str:
    if status == 401:
        return "auth"
    if status == 402:
        return "rate_limit" if _resource_limited(text) else "auth"
    if status == 403:
        if _MODERATION_403.search(text):
            return "content_filter"
        return "rate_limit" if _resource_limited(text) else "auth"
    if status == 408:
        return "timeout"
    if status == 413:
        return "context_length"
    if status == 429:
        return "rate_limit"
    if status in (503, 529):
        return "overloaded"
    if status in (504, 524):
        return "timeout"
    if status >= 500:
        return "overloaded" if _OVERLOAD.search(text) else "server"
    return _client_error_kind(text) or "bad_request"


def _text_kind(text: str) -> str:
    for pattern, kind in (
        (_QUOTA, "rate_limit"), (_RATE, "rate_limit"), (_OVERLOAD, "overloaded"),
        (_TIMEOUT, "timeout"), (_SERVER, "server"), (_CONTENT, "content_filter"), (_AUTH, "auth"),
    ):
        if pattern.search(text):
            return kind
    return "unknown"


def classify_upstream(status: int | None, error: UpstreamError) -> str:
    """上游报出的失败归到哪一类。status 是 HTTP 状态码；错误夹在 200 的流或正文里时传 200 或 None。"""
    text = error.haystack()
    if _CONTEXT.search(text):
        return "context_length"
    for key in (error.code.lower(), error.type.lower()):
        kind = _TYPE_KINDS.get(key)
        if kind == "bad_request":
            return _client_error_kind(text) or kind
        if kind:
            return kind
    effective = status if status is not None and status != 200 else _code_as_status(error.code)
    if effective is not None and effective >= 400:
        return _status_kind(effective, text)
    return _text_kind(text)


def classify_exception(exc: BaseException, *, mid_stream: bool = False) -> str:
    """请求没拿到完整答复时归到哪一类。流已经开始往回读之后出的事一律算 incomplete。"""
    if mid_stream:
        return "incomplete"
    cause = exc.cause if isinstance(exc, PluginTransportError) else type(exc)
    if issubclass(cause, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if issubclass(cause, (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.InvalidURL)):
        return "bad_request"
    if issubclass(cause, (httpx.TransportError, OSError)):
        return "network"
    if issubclass(cause, httpx.DecodingError):
        return "invalid_response"
    return "unknown"


def describe_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if not text:
        return name
    return text if name in text else f"{name}: {text}"


def failed(
    kind: str,
    error: str,
    *,
    status_code: int | None = None,
    finish_reason: str | None = None,
    text: str | None = None,
    reasoning: str | None = None,
    usage: dict[str, int] | None = None,
    raw: dict[str, Any] | None = None,
    provider_meta: dict[str, Any] | None = None,
) -> ProviderResponse:
    """失败响应。半截的正文和思维链留着给日志和排障看，工具调用一个都不交出去。"""
    return ProviderResponse(
        ok=False,
        error=error or kind,
        error_kind=kind,
        status_code=status_code,
        finish_reason=finish_reason,
        text=text or None,
        reasoning=reasoning or None,
        usage=usage,
        raw=raw,
        provider_meta=dict(provider_meta or {}),
    )


def upstream_failure(status: int | None, error: UpstreamError, **fields: Any) -> ProviderResponse:
    message = error.message or (f"HTTP {status}" if status else "upstream reported an error")
    return failed(classify_upstream(status, error), message, status_code=status, **fields)


def exception_failure(exc: BaseException, *, mid_stream: bool = False, **fields: Any) -> ProviderResponse:
    return failed(classify_exception(exc, mid_stream=mid_stream), describe_exception(exc), **fields)


def never_raises(method: Any) -> Any:
    """给 chat / chat_stream 套上兜底：取消和插件异常照常上抛，其余异常变成失败响应。"""
    if getattr(method, "__never_raises__", False) or not inspect.iscoroutinefunction(method):
        return method

    @functools.wraps(method)
    async def guarded(self: Any, *args: Any, **kwargs: Any) -> ProviderResponse:
        try:
            return await method(self, *args, **kwargs)
        except PluginExecutionError:
            raise
        except Exception as exc:
            log.exception("%s.%s 没接住的异常，按失败响应返回", type(self).__name__, method.__name__)
            return exception_failure(exc)

    guarded.__never_raises__ = True  # type: ignore[attr-defined]
    return guarded
