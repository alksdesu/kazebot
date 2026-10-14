from __future__ import annotations

import pytest

from engine.builtin.fallback_provider import _is_retryable


@pytest.mark.parametrize("status", [None, 200, 429, 500, 502, 503, 504])
def test_exhausted_empty_response_can_reach_fallback(status: int | None) -> None:
    assert _is_retryable(status, "empty_response (retried 3 times)") is True


def test_definitive_client_error_still_does_not_fallback() -> None:
    assert _is_retryable(400, "empty_response is not a valid request") is False
