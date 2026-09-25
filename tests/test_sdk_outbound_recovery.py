from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.client import ClonothClient
from clonoth_sdk.config import BotConfig
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.outbound_store import OutboundStore
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import Event


class ClientSocket:
    def __init__(self, ready=None):
        self.ready = ready or {"type": "outbound_replay_start", "version": 1}
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return json.dumps(self.ready)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.parametrize("legacy", [False, True])
def test_ws_resolves_static_file_and_rotated_tokens(tmp_path, monkeypatch, legacy):
    captures = []
    socket = ClientSocket()

    def modern_connect(uri, *, additional_headers=None, **kwargs):
        captures.append(additional_headers)
        return socket

    def legacy_connect(uri, *, extra_headers=None, **kwargs):
        captures.append(extra_headers)
        return socket

    monkeypatch.setattr(websockets, "connect", legacy_connect if legacy else modern_connect)
    token_path = tmp_path / "token"
    client = ClonothClient("http://local", admin_token="static", admin_token_path=str(token_path))

    async def exercise():
        assert [item async for item in client.ws_connect(7)] == []
        token_path.write_text("from-file", encoding="utf-8")
        assert [item async for item in client.ws_connect(7)] == []
        token_path.write_text("rotated", encoding="utf-8")
        assert [item async for item in client.ws_connect(7)] == []
        await client.close()

    asyncio.run(exercise())
    assert captures == [
        {"Authorization": "Bearer static"},
        {"Authorization": "Bearer from-file"},
        {"Authorization": "Bearer rotated"},
    ]
    assert socket.sent == [{"last_seq": 7, "replay_outbound": True}] * 3


def test_ws_rejects_server_without_recovery_protocol(monkeypatch):
    monkeypatch.setattr(websockets, "connect", lambda *a, **kw: ClientSocket({"type": "ping"}))

    async def exercise():
        client = ClonothClient("http://local")
        with pytest.raises(RuntimeError, match="does not support outbound recovery"):
            _ = [event async for event in client.ws_connect()]
        await client.close()

    asyncio.run(exercise())


def event(seq, event_type="outbound_message"):
    return Event(
        seq=seq, event_id=f"event-{seq}", ts="", run_id="run", session_id="session",
        component="supervisor", type=event_type,
        payload={"text": str(seq), "conversation_key": "qq_group:test"},
    ).to_dict()


class ReplayClient:
    def __init__(self, events):
        self.events = events
        self.cursors = []

    async def ws_connect(self, last_seq=None):
        self.cursors.append(last_seq)
        for item in self.events:
            yield item


def router(tmp_path, client, sends):
    async def send_to_channel(key, text, attachments, **kwargs):
        sends.append(text)

    return EventRouter(
        client, SessionState(), SimpleNamespace(send_to_channel=send_to_channel),
        BotConfig(base_url="http://local", workspace_root=tmp_path, conversation_key_prefix="qq_group"),
    )


def test_persistence_failure_stops_stream_and_restart_recovers_missing_reply(tmp_path, monkeypatch):
    sends = []
    client = ReplayClient([event(10), event(11)])
    first = router(tmp_path, client, sends)
    first._outbound_store.advance_received_seq(9)
    enqueue = first._outbound_store.enqueue

    def fail_first(item, **kwargs):
        if item.seq == 10:
            raise OSError("temporary disk failure")
        return enqueue(item, **kwargs)

    monkeypatch.setattr(first._outbound_store, "enqueue", fail_first)

    async def exercise():
        first._running = True
        with pytest.raises(OSError, match="not persisted"):
            await first._run_ws()
        assert first._outbound_store.received_seq == 9
        assert sends == []
        first._outbound_store.close()
        second = router(tmp_path, client, sends)
        second._running = True
        try:
            with pytest.raises(ConnectionError):
                await second._run_ws()
            assert second._outbound_store.received_seq == 11
        finally:
            second._outbound_store.close()

    asyncio.run(exercise())
    assert client.cursors == [9, 9]
    assert sends == ["10", "11"]


def test_cursor_failure_after_send_replays_without_duplicate_delivery(tmp_path, monkeypatch):
    sends = []
    client = ReplayClient([event(10)])
    first = router(tmp_path, client, sends)
    first._outbound_store.advance_received_seq(9)

    def failed_checkpoint(seq):
        raise OSError("checkpoint failed")

    monkeypatch.setattr(first._outbound_store, "advance_received_seq", failed_checkpoint)

    async def exercise():
        first._running = True
        with pytest.raises(OSError, match="checkpoint failed"):
            await first._run_ws()
        first._outbound_store.close()
        second = router(tmp_path, client, sends)
        second._running = True
        try:
            with pytest.raises(ConnectionError):
                await second._run_ws()
            assert second._outbound_store.received_seq == 10
        finally:
            second._outbound_store.close()

    asyncio.run(exercise())
    assert sends == ["10"]
    assert client.cursors == [9, 9]


def test_transient_events_do_not_advance_durable_receive_cursor(tmp_path):
    client = ReplayClient([{"type": "outbound_checkpoint", "seq": 8}, event(100, "inbound_accepted")])
    instance = router(tmp_path, client, [])
    instance._running = True

    async def exercise():
        with pytest.raises(ConnectionError):
            await instance._run_ws()

    try:
        asyncio.run(exercise())
        assert client.cursors == [None]
        assert instance._outbound_store.received_seq == 8
    finally:
        instance._outbound_store.close()


def test_delivery_ack_cannot_move_a_migrated_receive_cursor(tmp_path):
    path = tmp_path / "outbound.sqlite3"
    store = OutboundStore(path)
    record = store.enqueue(Event.from_dict(event(10)))
    store.close()
    reopened = OutboundStore(path)
    try:
        assert reopened.received_seq == 0
        claim = reopened.claim(record, owner="sender", lease_seconds=30)
        reopened.acknowledge(claim, owner="sender")
        assert reopened.processed_seq == 10
        assert reopened.received_seq == 0
    finally:
        reopened.close()


def test_failed_delivery_is_retried_locally_after_receive_cursor_advances(tmp_path):
    sends = []
    first = router(tmp_path, ReplayClient([event(10), event(11)]), sends)
    first._outbound_store.advance_received_seq(9)

    async def fail_ten(key, text, attachments, **kwargs):
        if text == "10":
            raise ConnectionError("QQ unavailable")
        sends.append(text)

    first._cb.send_to_channel = fail_ten

    async def exercise():
        first._running = True
        with pytest.raises(ConnectionError, match="stream ended"):
            await first._run_ws()
        assert first._outbound_store.received_seq == 11
        first._outbound_store.close()
        second = router(tmp_path, ReplayClient([]), sends)
        try:
            assert second._outbound_store.received_seq == 11
            pending = second._outbound_store.pending()
            assert [row.seq for row in pending] == [10]
            second._outbound_store._db.execute("UPDATE outbound SET next_retry=0 WHERE status='pending'")
            assert await second._deliver_outbound_record(pending[0])
            assert second._outbound_store.pending() == []
        finally:
            second._outbound_store.close()

    asyncio.run(exercise())
    assert sends == ["11", "10"]


def test_other_adapter_replies_are_skipped_without_creating_retry_rows(tmp_path):
    foreign = event(10)
    foreign["payload"]["conversation_key"] = "web:other"
    sends = []
    instance = router(tmp_path, ReplayClient([foreign]), sends)
    instance._running = True

    async def exercise():
        with pytest.raises(ConnectionError):
            await instance._run_ws()

    try:
        asyncio.run(exercise())
        assert sends == []
        assert instance._outbound_store.pending() == []
        assert instance._outbound_store.received_seq == 10
    finally:
        instance._outbound_store.close()


def test_intermediate_delivery_remains_compatible_with_old_callback(tmp_path):
    sends = []
    instance = router(tmp_path, ReplayClient([]), sends)

    async def old_callback(key, text, attachments, *, node_id=""):
        sends.append(text)

    instance._cb.send_to_channel = old_callback
    try:
        asyncio.run(instance._handle_intermediate_reply(Event.from_dict(event(1, "intermediate_reply"))))
        assert sends == ["1"]
    finally:
        instance._outbound_store.close()
