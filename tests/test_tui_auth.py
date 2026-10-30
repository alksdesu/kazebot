"""TUI 遇到令牌被拒：客户端抛专门的异常，轮询停下并提示一次。

非 200 一律当空列表的话，令牌失效时 TUI 会静默空转，每轮还替来源记一次认证失败。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shell.tui.services.event_poller import EventPoller  # noqa: E402
from shell.tui.services.supervisor_client import SupervisorAuthRejected, SupervisorClient  # noqa: E402


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, json=[], request=httpx.Request("GET", "http://supervisor/v1/events"))


class _FakeHTTP:
    def __init__(self, status: int) -> None:
        self._status = status

    async def get(self, *_args, **_kwargs) -> httpx.Response:
        return _response(self._status)


@pytest.mark.parametrize("status", [401, 403])
def test_the_client_raises_on_a_rejected_token(status: int) -> None:
    client = SupervisorClient("http://supervisor")
    client._client = _FakeHTTP(status)  # type: ignore[assignment]

    async def go() -> None:
        with pytest.raises(SupervisorAuthRejected):
            await client.get_global_events()
        with pytest.raises(SupervisorAuthRejected):
            await client.get_session_events("sess")

    asyncio.run(go())


def test_a_plain_error_is_still_an_empty_list() -> None:
    client = SupervisorClient("http://supervisor")
    client._client = _FakeHTTP(503)  # type: ignore[assignment]

    assert asyncio.run(client.get_global_events()) == []


class _RejectingClient:
    async def get_global_events(self, *_a, **_k):
        raise SupervisorAuthRejected(401)

    async def get_session_events(self, *_a, **_k):
        raise SupervisorAuthRejected(401)


class _App:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, message: str, *, severity: str = "information", timeout: float = 0) -> None:
        self.notices.append((message, severity))


def test_the_global_loop_stops_and_notifies_on_a_rejected_token() -> None:
    app = _App()
    poller = EventPoller(_RejectingClient(), app)
    poller._running = True

    # 轮询循环调 tick 抛 SupervisorAuthRejected，被循环捕获后停下并提示
    asyncio.run(poller._poll_global_loop())

    assert poller._running is False
    assert len(app.notices) == 1
    assert app.notices[0][1] == "error"


def test_give_up_only_notifies_the_first_time() -> None:
    app = _App()
    poller = EventPoller(_RejectingClient(), app)
    poller._running = True

    poller._give_up(SupervisorAuthRejected(401))
    poller._give_up(SupervisorAuthRejected(401))

    assert len(app.notices) == 1
