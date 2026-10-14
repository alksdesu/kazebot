from __future__ import annotations

from supervisor.api import _provider_catalog_error


def test_catalog_error_keeps_json_upstream_message_and_status() -> None:
    text = _provider_catalog_error(
        502,
        "https://generativelanguage.googleapis.com/v1beta/models",
        '{"error":{"status":"BAD_GATEWAY","message":"upstream disconnected"}}',
    )

    assert "HTTP 502" in text
    assert "BAD_GATEWAY" in text
    assert "upstream disconnected" in text


def test_catalog_error_strips_html_without_shortening_the_first_message() -> None:
    text = _provider_catalog_error(
        530,
        "https://qqbot.example/v1beta/models",
        "<html><title>Bad gateway</title><body>Ray ID: abc</body></html>",
    )

    assert text == "上游 HTTP 530（https://qqbot.example/v1beta/models）：Bad gateway Ray ID: abc"
