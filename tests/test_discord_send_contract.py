from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clonoth_sdk.types import DeliveryContext
from tests._discord_harness import install_discord_stub


install_discord_stub()


_ROOT = _REPO_ROOT / "adapters" / "discord"
_PACKAGE_NAME = "_discord_contract_adapter"
_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _ROOT / "__init__.py",
    submodule_search_locations=[str(_ROOT)],
)
assert _SPEC and _SPEC.loader
_PACKAGE = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)
_SPEC.loader.exec_module(_PACKAGE)

_CALLBACK_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE_NAME}.callbacks", _ROOT / "callbacks.py"
)
assert _CALLBACK_SPEC and _CALLBACK_SPEC.loader
_CALLBACKS = importlib.util.module_from_spec(_CALLBACK_SPEC)
sys.modules[_CALLBACK_SPEC.name] = _CALLBACKS
_CALLBACK_SPEC.loader.exec_module(_CALLBACKS)

from _discord_contract_adapter.callbacks import EreunaCallbacks  # type: ignore[import-not-found]  # noqa: E402
from _discord_contract_adapter.send_contract import (  # type: ignore[import-not-found]  # noqa: E402
    DiscordAmbiguousAckError,
    DiscordIdempotencyStore,
    DiscordSendNotStartedError,
    DiscordSendContractError,
    DiscordSendError,
    classify_discord_send_exception,
)


class _Sent:
    def __init__(self, message_id: int):
        self.id = message_id


class _Channel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_content_once: str | None = None
        self._next_id = 100

    async def send(self, **kwargs: Any) -> _Sent:
        self.calls.append(kwargs)
        if kwargs.get("content") == self.fail_content_once:
            self.fail_content_once = None
            raise ConnectionError("connection refused before send")
        self._next_id += 1
        return _Sent(self._next_id)


class _Message:
    def __init__(self, channel: _Channel) -> None:
        self.channel = channel
        self.reply_calls: list[dict[str, Any]] = []
        self.reactions: list[str] = []
        self._next_id = 10

    async def reply(self, **kwargs: Any) -> _Sent:
        self.reply_calls.append(kwargs)
        self._next_id += 1
        return _Sent(self._next_id)

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


class _Client:
    def __init__(self, channel: _Channel | None = None) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int) -> _Channel | None:
        return self.channel

    async def fetch_channel(self, channel_id: int) -> _Channel | None:
        return self.channel


class _Runtime(SimpleNamespace):
    def __init__(self, workspace: Path, channel: _Channel | None = None):
        super().__init__(
            workspace=workspace,
            dc_client=_Client(channel),
            split_signal="[SPLIT]",
            restart_signal="[BOT_RESTART]",
            react_pattern=re.compile(r"\[REACT:([^\]]+)\]"),
            entry_node_id="entry",
            child_node_display_names={},
            channel_history={},
            history_seq_counter=0,
            history_max_len=20,
            history_gap={},
            session_state=None,
        )


def _context(key: str = "id:event-1") -> DeliveryContext:
    return DeliveryContext(
        event_id="event-1",
        event_seq=7,
        task_id="task-1",
        source_inbound_seq=3,
        conversation_key="discord:42",
        attempt=1,
        idempotency_key=key,
    )


def _trigger(message: _Message | None) -> SimpleNamespace:
    return SimpleNamespace(
        inbound_seq=3,
        platform_data={"message": message, "channel_id": 42},
    )


def test_send_reply_failure_is_classified_and_propagated(tmp_path: Path) -> None:
    channel = _Channel()
    channel.fail_content_once = "second"
    message = _Message(channel)
    callbacks = EreunaCallbacks(_Runtime(tmp_path, channel))

    with pytest.raises(DiscordSendError) as raised:
        asyncio.run(callbacks.send_reply(
            _trigger(message),
            "first[SPLIT]second",
            [],
            delivery_context=_context(),
        ))
    assert raised.value.retryable is True
    assert [call["content"] for call in message.reply_calls] == ["first"]
    assert [call["content"] for call in channel.calls] == ["second"]


def test_missing_trigger_and_channel_are_permanent_contract_failures(tmp_path: Path) -> None:
    callbacks = EreunaCallbacks(_Runtime(tmp_path))
    with pytest.raises(DiscordSendContractError) as missing_trigger:
        asyncio.run(callbacks.send_reply(
            _trigger(None), "hello", [], delivery_context=_context()
        ))
    assert missing_trigger.value.retryable is False

    with pytest.raises(DiscordSendContractError, match="does not exist"):
        asyncio.run(callbacks.send_to_channel(
            "discord:42", "hello", [], delivery_context=_context()
        ))


def test_sent_children_survive_callback_restart_before_sdk_ack(tmp_path: Path) -> None:
    channel = _Channel()
    message = _Message(channel)
    first = EreunaCallbacks(_Runtime(tmp_path, channel))
    asyncio.run(first.send_reply(
        _trigger(message), "durable", [], delivery_context=_context()
    ))
    first._outbound_idempotency.close()

    restarted = EreunaCallbacks(_Runtime(tmp_path, channel))
    asyncio.run(restarted.send_reply(
        _trigger(message), "durable", [], delivery_context=_context()
    ))
    assert [call["content"] for call in message.reply_calls] == ["durable"]


def test_partial_split_with_attachment_replays_only_failed_child(tmp_path: Path) -> None:
    attachment = tmp_path / "result.txt"
    attachment.write_text("payload", encoding="utf-8")
    channel = _Channel()
    channel.fail_content_once = "second"
    message = _Message(channel)
    callbacks = EreunaCallbacks(_Runtime(tmp_path, channel))
    attachments = [{"path": str(attachment), "name": "result.txt"}]

    with pytest.raises(DiscordSendError):
        asyncio.run(callbacks.send_reply(
            _trigger(message),
            "first[SPLIT]second",
            attachments,
            delivery_context=_context(),
        ))
    asyncio.run(callbacks.send_reply(
        _trigger(message),
        "first[SPLIT]second",
        attachments,
        delivery_context=_context(),
    ))

    assert len(message.reply_calls) == 1
    assert len(message.reply_calls[0]["files"]) == 1
    assert [call["content"] for call in channel.calls] == ["second", "second"]


def test_force_replay_generation_uses_new_key_and_really_resends(tmp_path: Path) -> None:
    channel = _Channel()
    callbacks = EreunaCallbacks(_Runtime(tmp_path, channel))
    normal = _context()
    replay = DeliveryContext(
        **{
            **normal.__dict__,
            "attempt": 2,
            "idempotency_key": "id:event-1:replay:1",
            "replay_generation": 1,
            "force_replay": True,
        }
    )
    asyncio.run(callbacks.send_to_channel("discord:42", "again", [], delivery_context=normal))
    asyncio.run(callbacks.send_to_channel("discord:42", "again", [], delivery_context=normal))
    asyncio.run(callbacks.send_to_channel("discord:42", "again", [], delivery_context=replay))
    assert [call["content"] for call in channel.calls] == ["again", "again"]


def test_discord_exception_classification_retry_and_deadletter_semantics() -> None:
    connection = classify_discord_send_exception(ConnectionError("connection refused"))
    assert connection.retryable is True

    timeout = classify_discord_send_exception(asyncio.TimeoutError("ack timeout"))
    assert isinstance(timeout, DiscordAmbiguousAckError)
    assert timeout.retryable is False
    assert timeout.ambiguous_ack is True

    bad_target = classify_discord_send_exception(ValueError("invalid channel id"))
    assert isinstance(bad_target, DiscordSendContractError)
    assert bad_target.retryable is False


def test_discord_sent_claim_ttl_prunes_only_sent_rows(tmp_path: Path) -> None:
    now = [100.0]

    async def exercise() -> None:
        store = DiscordIdempotencyStore(
            tmp_path / "ttl.sqlite3",
            lease_seconds=5,
            sent_ttl=10,
            max_items=0,
            clock=lambda: now[0],
        )
        sent = await store.begin("sent")
        await store.commit(sent, ["101"])
        pending = await store.begin("pending")
        assert pending.acquired
        now[0] = 111.0
        assert await store.prune() == 1
        assert await store.state("sent") is None
        row = store._db.execute("SELECT state FROM claims WHERE key='pending'").fetchone()
        assert row is not None and row[0] == "pending"

    asyncio.run(exercise())


def test_discord_sent_claim_max_items_is_bounded_and_survives_restart(tmp_path: Path) -> None:
    now = [1.0]
    path = tmp_path / "bounded.sqlite3"

    async def exercise() -> None:
        store = DiscordIdempotencyStore(
            path, sent_ttl=0, max_items=2, clock=lambda: now[0],
        )
        for key in ("first", "second", "third"):
            claim = await store.begin(key)
            await store.commit(claim, [key])
            now[0] += 1
        assert await store.state("first") is None
        second = await store.state("second")
        assert second is not None and second.state == "sent"
        store.close()

        restarted = DiscordIdempotencyStore(
            path, sent_ttl=0, max_items=2, clock=lambda: now[0],
        )
        second = await restarted.state("second")
        third = await restarted.state("third")
        assert second is not None and second.platform_message_ids == ("second",)
        assert third is not None and third.platform_message_ids == ("third",)
        assert int(restarted._db.execute(
            "SELECT COUNT(*) FROM claims WHERE state='sent'"
        ).fetchone()[0]) == 2

    asyncio.run(exercise())


def test_discord_outer_cancel_waits_for_send_and_commit(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = EreunaCallbacks(_Runtime(tmp_path, _Channel()))
        started, finish = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            await finish.wait()
            return [_Sent(701)]

        task = asyncio.create_task(callbacks._send_delivery_unit(
            _context(), "outer-cancel", operation,
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == ["701"]
        assert callbacks._outbound_idempotency._db.execute(
            "SELECT state FROM claims"
        ).fetchone()[0] == "sent"

    asyncio.run(exercise())


def test_discord_cancel_after_send_return_during_commit_is_shielded(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = EreunaCallbacks(_Runtime(tmp_path, _Channel()))
        started, finish = asyncio.Event(), asyncio.Event()
        original_commit = callbacks._outbound_idempotency.commit

        async def delayed_commit(*args, **kwargs):
            started.set()
            await finish.wait()
            await original_commit(*args, **kwargs)

        callbacks._outbound_idempotency.commit = delayed_commit  # type: ignore[method-assign]
        task = asyncio.create_task(callbacks._send_delivery_unit(
            _context(), "commit-cancel", lambda: asyncio.sleep(0, result=[_Sent(702)]),
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == ["702"]
        assert callbacks._outbound_idempotency._db.execute(
            "SELECT state FROM claims"
        ).fetchone()[0] == "sent"

    asyncio.run(exercise())


def test_discord_explicit_not_started_releases_claim(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = EreunaCallbacks(_Runtime(tmp_path, _Channel()))

        async def operation():
            raise DiscordSendNotStartedError("cancelled before HTTP request")

        with pytest.raises(DiscordSendNotStartedError):
            await callbacks._send_delivery_unit(_context(), "not-started", operation)
        assert callbacks._outbound_idempotency._db.execute(
            "SELECT COUNT(*) FROM claims"
        ).fetchone()[0] == 0

    asyncio.run(exercise())


def test_discord_internal_cancel_is_ambiguous_restart_and_force_replay(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = EreunaCallbacks(_Runtime(tmp_path, _Channel()))

        async def cancelled_send():
            raise asyncio.CancelledError()

        with pytest.raises(DiscordAmbiguousAckError):
            await callbacks._send_delivery_unit(_context(), "ambiguous", cancelled_send)
        row = callbacks._outbound_idempotency._db.execute(
            "SELECT state,last_error FROM claims"
        ).fetchone()
        assert row[0] == "ambiguous" and "CancelledError" in row[1]
        callbacks._outbound_idempotency.close()

        restarted = EreunaCallbacks(_Runtime(tmp_path, _Channel()))
        with pytest.raises(DiscordAmbiguousAckError):
            await restarted._send_delivery_unit(
                _context(), "ambiguous", lambda: asyncio.sleep(0, result=[_Sent(703)])
            )
        replay = DeliveryContext(**{
            **_context().__dict__, "idempotency_key": "id:event-1:replay:1",
            "replay_generation": 1, "force_replay": True,
        })
        assert await restarted._send_delivery_unit(
            replay, "ambiguous", lambda: asyncio.sleep(0, result=[_Sent(704)])
        ) == ["704"]

    asyncio.run(exercise())


def test_discord_commit_cancel_fault_persists_possible_message_id(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = EreunaCallbacks(_Runtime(tmp_path, _Channel()))

        async def cancelled_commit(*args, **kwargs):
            raise asyncio.CancelledError()

        callbacks._outbound_idempotency.commit = cancelled_commit  # type: ignore[method-assign]
        with pytest.raises(DiscordAmbiguousAckError):
            await callbacks._send_delivery_unit(
                _context(), "commit-fault", lambda: asyncio.sleep(0, result=[_Sent(705)])
            )
        row = callbacks._outbound_idempotency._db.execute(
            "SELECT state,platform_message_ids FROM claims"
        ).fetchone()
        assert row[0] == "ambiguous" and "705" in row[1]

    asyncio.run(exercise())


def test_a_discord_dead_letter_survives_inside_its_ttl(tmp_path: Path) -> None:
    now = [100.0]

    async def exercise() -> None:
        store = DiscordIdempotencyStore(
            tmp_path / "dl.sqlite3", lease_seconds=5, sent_ttl=0, max_items=0,
            ambiguous_ttl=60, clock=lambda: now[0],
        )
        claim = await store.begin("dead")
        await store.mark_ambiguous(claim, error="timeout waiting for ack")
        now[0] = 159.0
        with pytest.raises(DiscordAmbiguousAckError):
            await store.begin("dead")

    asyncio.run(exercise())


def test_a_discord_dead_letter_is_released_after_its_ttl(tmp_path: Path) -> None:
    now = [100.0]

    async def exercise() -> None:
        store = DiscordIdempotencyStore(
            tmp_path / "dl.sqlite3", lease_seconds=5, sent_ttl=0, max_items=0,
            ambiguous_ttl=60, clock=lambda: now[0],
        )
        claim = await store.begin("dead")
        await store.mark_ambiguous(claim, error="timeout waiting for ack")
        now[0] = 161.0
        released = await store.begin("dead")
        assert released.acquired is True
        assert released.state == "pending"

    asyncio.run(exercise())
