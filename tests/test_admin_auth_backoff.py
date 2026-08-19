"""认证失败要退避，token 比较要恒定时间。

没有退避的话，本机上一个脚本可以不受限地枚举 admin token；用 == 比较则会在第一个
不同字节返回，逐字节试探能把 token 问出来。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402

_TOKEN = "test-admin-token"


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host


class _Request:
    """够 verify_admin_token 用的最小请求替身。"""

    def __init__(self, *, token: str = "", query_token: str = "", host: str = "10.0.0.1") -> None:
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.query_params = {"token": query_token} if query_token else {}
        self.client = _Client(host)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())


def _fail(host: str = "10.0.0.1") -> int:
    try:
        admin_api.verify_admin_token(_Request(token="wrong", host=host))
    except HTTPException as exc:
        return exc.status_code
    raise AssertionError("expected the wrong token to be rejected")


class TestTheHappyPath:
    def test_a_correct_bearer_token_passes(self) -> None:
        admin_api.verify_admin_token(_Request(token=_TOKEN))

    def test_a_correct_query_token_passes(self) -> None:
        admin_api.verify_admin_token(_Request(query_token=_TOKEN))

    def test_an_empty_request_is_rejected_without_matching_an_empty_token(self) -> None:
        # presented 为空时不能走进 compare_digest，否则空 token 配空请求就等于放行。
        assert _fail() == 401
        try:
            admin_api.verify_admin_token(_Request())
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("expected an empty request to be rejected")


class TestBackoff:
    def test_the_first_few_failures_are_not_throttled(self) -> None:
        # 手滑输错几次不该被罚站。
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS):
            assert _fail() == 401

    def test_the_next_failure_starts_the_backoff(self) -> None:
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 1):
            _fail()

        # 退避窗口里连正确的 token 也先挡住，否则攻击者可以用正确请求探测窗口。
        try:
            admin_api.verify_admin_token(_Request(token=_TOKEN))
        except HTTPException as exc:
            assert exc.status_code == 429
            assert "Retry-After" in (exc.headers or {})
        else:
            raise AssertionError("expected the backoff to apply")

    def test_the_delay_grows_and_is_capped(self) -> None:
        now = 1000.0
        delays: list[float] = []
        for _ in range(14):
            admin_api._record_auth_failure("k", now)
            delays.append(admin_api._auth_backoff_remaining("k", now))

        free = admin_api._AUTH_FREE_ATTEMPTS
        assert delays[:free] == [0.0] * free
        assert delays[free] == pytest.approx(admin_api._AUTH_BACKOFF_BASE_SEC)
        assert delays[free + 1] > delays[free]
        assert max(delays) == pytest.approx(admin_api._AUTH_BACKOFF_MAX_SEC)

    def test_a_success_clears_the_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS):
            _fail()
        admin_api.verify_admin_token(_Request(token=_TOKEN))

        # 清零之后又能重新用掉全部免费次数，说明计数真的归零了。
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS):
            assert _fail() == 401

    def test_the_window_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        base = 1000.0
        monkeypatch.setattr(time, "time", lambda: base)
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 1):
            _fail()

        monkeypatch.setattr(time, "time", lambda: base + admin_api._AUTH_BACKOFF_MAX_SEC + 1)
        admin_api.verify_admin_token(_Request(token=_TOKEN))

    def test_backoff_is_per_client(self) -> None:
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 2):
            _fail(host="10.0.0.1")

        # 另一个来源不该被别人的失败连坐。
        admin_api.verify_admin_token(_Request(token=_TOKEN, host="10.0.0.2"))

    def test_a_forged_forwarded_header_cannot_reset_the_backoff(self) -> None:
        # 认 X-Forwarded-For 的话，每次换个伪造值就能绕过退避。
        for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 1):
            _fail(host="10.0.0.1")
        request = _Request(token=_TOKEN, host="10.0.0.1")
        request.headers["X-Forwarded-For"] = "1.2.3.4"

        try:
            admin_api.verify_admin_token(request)
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("expected the forged header to be ignored")

    def test_the_table_does_not_grow_without_bound(self) -> None:
        # 源 IP 随机的探测不该把这张表本身变成内存泄漏。
        for index in range(admin_api._AUTH_TRACK_MAX_CLIENTS + 50):
            admin_api._record_auth_failure(f"host-{index}", 1000.0)

        assert len(admin_api._auth_failures) <= admin_api._AUTH_TRACK_MAX_CLIENTS


class TestConstantTimeComparison:
    def test_it_uses_compare_digest(self) -> None:
        # 逐字节短路的 == 能让攻击者按响应时间问出 token。
        source = Path(admin_api.__file__).read_text(encoding="utf-8")
        body = source[source.index("def verify_admin_token"):]
        body = body[:body.index("\ndef ", 1)]

        assert "compare_digest" in body
        assert "== token" not in body
