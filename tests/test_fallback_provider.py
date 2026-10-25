from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.inference.llm_outcome import should_fallback


def _failed(status: int | None, error: str, kind: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(ok=False, status_code=status, error=error, error_kind=kind)


@pytest.mark.parametrize("status", [None, 200, 429, 500, 502, 503, 504])
def test_exhausted_empty_response_can_reach_fallback(status: int | None) -> None:
    assert should_fallback(_failed(status, "empty_response (retried 3 times)")) is True


def test_definitive_client_error_still_does_not_fallback() -> None:
    assert should_fallback(_failed(400, "empty_response is not a valid request")) is False


@pytest.mark.parametrize("kind", ["content_filter", "auth", "bad_request", "rate_limit", "server", "unknown"])
def test_classified_failures_that_another_channel_may_survive_go_to_the_fallback(kind: str) -> None:
    assert should_fallback(_failed(400, "provider said no", kind)) is True


def test_context_overflow_is_left_to_reactive_compaction() -> None:
    assert should_fallback(_failed(400, "maximum context length exceeded", "context_length")) is False
