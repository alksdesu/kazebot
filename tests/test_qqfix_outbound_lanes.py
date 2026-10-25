"""出站投递不堵 WS 接收循环，续租出错也不会盖掉投递结果。

接收落盘即确认，投递按会话分道：同会话串行，不同会话互不等待。合并转发一次最长等 240 秒，
不能让别的会话和审批请求排在它后面。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.delivery_lanes import DeliveryLanes  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.lease import lease_heartbeat, renew_until_lost  # noqa: E402
from clonoth_sdk.outbound_store import OutboundOwnershipError  # noqa: E402
from clonoth_sdk.state import SessionState  # noqa: E402
from clonoth_sdk.types import Event  # noqa: E402


async def _settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class TestDeliveryLanes:
    def test_one_lane_is_serial_and_other_lanes_do_not_wait(self) -> None:
        log: list[str] = []

        async def exercise() -> None:
            gate = asyncio.Event()

            async def run(name: str) -> None:
                log.append(f"start {name}")
                if name == "a1":
                    await gate.wait()
                log.append(f"end {name}")

            lanes: DeliveryLanes[str] = DeliveryLanes(run)
            lanes.submit("A", "a1", "a1")
            lanes.submit("A", "a2", "a2")
            lanes.submit("B", "b1", "b1")
            await _settle()
            assert "end b1" in log
            assert "start a2" not in log
            gate.set()
            await lanes.join()

        asyncio.run(exercise())

        assert log.index("end a1") < log.index("start a2")

    def test_a_key_already_queued_is_not_taken_twice(self) -> None:
        runs: list[str] = []

        async def exercise() -> tuple[bool, bool, bool]:
            async def run(name: str) -> None:
                runs.append(name)

            lanes: DeliveryLanes[str] = DeliveryLanes(run)
            first = lanes.submit("A", "row-1", "first")
            again = lanes.submit("A", "row-1", "again")
            await lanes.join()
            later = lanes.submit("A", "row-1", "later")
            await lanes.join()
            return first, again, later

        assert asyncio.run(exercise()) == (True, False, True)
        assert runs == ["first", "later"]

    def test_a_failing_job_does_not_stop_its_lane(self, caplog: pytest.LogCaptureFixture) -> None:
        runs: list[str] = []

        async def exercise() -> None:
            async def run(name: str) -> None:
                runs.append(name)
                if name == "boom":
                    raise RuntimeError("platform down")

            lanes: DeliveryLanes[str] = DeliveryLanes(run)
            lanes.submit("A", "1", "boom")
            lanes.submit("A", "2", "next")
            await lanes.join()
            assert not lanes.busy

        with caplog.at_level(logging.ERROR):
            asyncio.run(exercise())

        assert runs == ["boom", "next"]
        assert "delivery lane job failed" in caplog.text

    def test_closing_cancels_and_forgets_queued_keys(self) -> None:
        async def exercise() -> DeliveryLanes[str]:
            async def run(name: str) -> None:
                await asyncio.sleep(10)

            lanes: DeliveryLanes[str] = DeliveryLanes(run)
            lanes.submit("A", "1", "x")
            lanes.submit("A", "2", "y")
            await _settle()
            await lanes.close()
            return lanes

        lanes = asyncio.run(exercise())

        assert not lanes.busy and "1" not in lanes and "2" not in lanes


class TestLease:
    def test_renewal_stops_when_the_lease_is_gone(self) -> None:
        calls: list[int] = []

        def renew() -> bool:
            calls.append(1)
            return len(calls) < 3

        asyncio.run(asyncio.wait_for(renew_until_lost(renew, interval=0.001), timeout=2))

        assert len(calls) == 3

    def test_an_ownership_error_ends_renewal_quietly(self) -> None:
        async def renew() -> None:
            raise OutboundOwnershipError("taken over")

        asyncio.run(asyncio.wait_for(renew_until_lost(renew, interval=0.001, lost=(OutboundOwnershipError,)), timeout=2))

    def test_an_unexpected_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        def renew() -> None:
            raise RuntimeError("database is locked")

        with caplog.at_level(logging.WARNING):
            asyncio.run(asyncio.wait_for(renew_until_lost(renew, interval=0.001, label="row-9"), timeout=2))

        assert "row-9" in caplog.text

    def test_a_broken_renewal_never_replaces_the_result_of_the_block(self) -> None:
        def renew() -> None:
            raise RuntimeError("heartbeat exploded")

        async def exercise() -> str:
            async with lease_heartbeat(renew, interval=0.001):
                await asyncio.sleep(0.02)
                return "sent"

        assert asyncio.run(exercise()) == "sent"

    def test_the_block_error_is_the_one_that_surfaces(self) -> None:
        def renew() -> None:
            raise RuntimeError("heartbeat exploded")

        async def exercise() -> None:
            async with lease_heartbeat(renew, interval=0.001):
                await asyncio.sleep(0.02)
                raise ConnectionError("platform rejected the send")

        with pytest.raises(ConnectionError, match="platform rejected"):
            asyncio.run(exercise())


class _Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.shown: list[str] = []
        self.gate: asyncio.Event | None = None
        self.delay = 0.0

    async def send_to_channel(self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any) -> None:
        if self.gate is not None and text == "slow":
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(text)

    async def show_approval_ui(self, approval_id: str, operation: str, details: dict[str, Any], **kwargs: Any) -> None:
        self.shown.append(approval_id)


def _router(tmp_path: Path, channel: _Channel, **config: Any) -> EventRouter:
    client = SimpleNamespace(approve=None)
    return EventRouter(
        client, SessionState(), channel,
        BotConfig(base_url="http://127.0.0.1:1", workspace_root=tmp_path, conversation_key_prefix="chat",
                  auto_approve_internal=False, **config),
    )


def _outbound(seq: int, text: str, conversation_key: str) -> Event:
    return Event(
        seq=seq, event_id=f"event-{seq}", ts="", run_id="run", session_id="session",
        component="supervisor", type="outbound_message",
        payload={"text": text, "conversation_key": conversation_key},
    )


class TestRouterLanes:
    def test_a_slow_delivery_does_not_hold_back_an_approval_elsewhere(self, tmp_path: Path) -> None:
        channel = _Channel()
        router = _router(tmp_path, channel)

        async def exercise() -> None:
            channel.gate = asyncio.Event()
            assert await router._receive(_outbound(10, "slow", "chat:A"))
            await router._receive(Event(
                seq=11, event_id="event-11", ts="", run_id="run", session_id="session",
                component="supervisor", type="approval_requested",
                payload={"approval_id": "ap-1", "conversation_key": "chat:B", "details": {"tool_name": "run_command"}},
            ))
            await _settle()
            assert channel.shown == ["ap-1"]
            assert channel.sent == []
            channel.gate.set()
            await router.drain_outbound()

        try:
            asyncio.run(exercise())
            assert channel.sent == ["slow"]
            assert router._outbound_store.pending() == []
        finally:
            router._outbound_store.close()

    def test_one_conversation_keeps_its_order_while_another_moves_on(self, tmp_path: Path) -> None:
        channel = _Channel()
        router = _router(tmp_path, channel)

        async def exercise() -> list[str]:
            channel.gate = asyncio.Event()
            await router._receive(_outbound(10, "slow", "chat:A"))
            await router._receive(_outbound(11, "after slow", "chat:A"))
            await router._receive(_outbound(12, "other room", "chat:B"))
            await _settle()
            early = list(channel.sent)
            channel.gate.set()
            await router.drain_outbound()
            return early

        try:
            early = asyncio.run(exercise())
            assert early == ["other room"]
            assert channel.sent == ["other room", "slow", "after slow"]
        finally:
            router._outbound_store.close()

    def test_a_failing_heartbeat_neither_loses_the_result_nor_the_row(self, tmp_path: Path) -> None:
        channel = _Channel()
        channel.delay = 0.35
        router = _router(tmp_path, channel, outbound_heartbeat_interval=0.1, outbound_lease_seconds=30.0)

        def broken_heartbeat(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database is locked")

        router._outbound_store.heartbeat = broken_heartbeat

        async def exercise() -> bool:
            accepted, record, trigger = router._persist_durable_outbound(_outbound(10, "hello", "chat:A"))
            assert accepted and record is not None
            return await router._deliver_outbound_record(record, trigger_hint=trigger)

        try:
            assert asyncio.run(exercise()) is True
            assert channel.sent == ["hello"]
            assert router._outbound_inflight == set()
            assert router._outbound_store.pending() == []
        finally:
            router._outbound_store.close()

    def test_a_crashed_delivery_never_stays_in_flight(self, tmp_path: Path) -> None:
        channel = _Channel()
        router = _router(tmp_path, channel)

        async def explode(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("bug in the adapter")

        router._deliver_claimed_outbound = explode

        async def exercise() -> None:
            accepted, record, _ = router._persist_durable_outbound(_outbound(10, "hello", "chat:A"))
            assert accepted and record is not None
            with pytest.raises(RuntimeError, match="bug in the adapter"):
                await router._deliver_outbound_record(record)

        try:
            asyncio.run(exercise())
            assert router._outbound_inflight == set()
        finally:
            router._outbound_store.close()
