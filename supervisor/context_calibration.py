"""压缩估算用的字/token 比按对话记：只认这段对话自己那次请求的实测值，连同那次的字数算没算思维链。"""
from __future__ import annotations

from typing import Any


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def usage_calibration(usage: Any, payload: Any) -> dict[str, Any] | None:
    """context_usage 事件里能拿来校准的部分；没有真实 prompt_tokens 时为 None。"""
    if not isinstance(usage, dict) or not isinstance(payload, dict):
        return None
    tokens = _positive_int(usage.get("prompt_tokens"))
    if tokens is None:
        return None
    calibration: dict[str, Any] = {"prompt_tokens": tokens, "reasoning": payload.get("reasoning_counted") is True}
    chars = _positive_int(payload.get("prompt_chars"))
    if chars is not None:
        calibration["prompt_chars"] = chars
    return calibration


def recorded_calibration(usage_entry: Any) -> dict[str, Any] | None:
    """会话用量记录里存的校准记录；没有就是 None。"""
    calibration = usage_entry.get("calibration") if isinstance(usage_entry, dict) else None
    return calibration if isinstance(calibration, dict) else None


def calibrated_chars_per_token(calibration: Any, messages: list[Any] | None = None) -> float | None:
    """按校准记录算字/token 比。

    记录里有同一次请求的字数时用它；没有就拿 messages 的字数兜底，思维链算不算跟那次请求一致；都没有为 None。
    """
    from engine.compact import observed_chars_per_token, total_content_chars

    if not isinstance(calibration, dict):
        return None
    tokens = _positive_int(calibration.get("prompt_tokens"))
    if tokens is None:
        return None
    chars = _positive_int(calibration.get("prompt_chars"))
    if chars is None:
        if not messages:
            return None
        chars = total_content_chars(messages, include_reasoning=calibration.get("reasoning") is True)
        if chars <= 0:
            return None
    return observed_chars_per_token(chars, tokens)


def counts_reasoning(calibration: Any) -> bool:
    return isinstance(calibration, dict) and calibration.get("reasoning") is True
