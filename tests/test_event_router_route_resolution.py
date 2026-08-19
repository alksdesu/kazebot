"""Tests for structured dispatch route metadata in EventRouter.

[2026-05-29] Why: dispatch child sessions used to infer their parent channel
from agent:-prefixed conversation_key strings, and approval de-duplication ran
before adapter ownership was known. How: these tests exercise the SDK router
with structured parent_conversation_key metadata, unowned approval events, and
unowned child progress events. Purpose: lock in scheme C step 1 before changing
routing code.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

import pytest

# Why: the test runner uses the source checkout directly. How: prepend the
# repository root to sys.path. Purpose: import clonoth_sdk without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.outbound_store import (  # noqa: E402
    OutboundOwnershipError,
    OutboundStore,
)
from clonoth_sdk.state import ChildTaskState, MainTaskState, SessionState, TriggerInfo  # noqa: E402
from clonoth_sdk.types import Event  # noqa: E402


class _FakeClient:
    """Small client double used by approval tests."""

    def __init__(self) -> None:
        # Why: auto approval is not the target of these tests. How: record calls
        # if a regression reaches approve unexpectedly. Purpose: keep failures
        # focused on route ownership rather than network behavior.
        self.approved: list[tuple[str, str, str]] = []

    async def approve(self, approval_id: str, *, decision: str, comment: str = "") -> bool:
        self.approved.append((approval_id, decision, comment))
        return True


class _FakeCallbacks:
    """Adapter callback double that records only calls relevant to routing."""

    def __init__(self) -> None:
        # Why: route ownership is observable through whether SDK calls adapter
        # callbacks. How: store callback arguments. Purpose: assert unowned events
        # are skipped and owned events use the parent conversation_key.
        self.approvals: list[dict[str, Any]] = []
        self.child_creations: list[dict[str, Any]] = []
        self.child_updates: list[str] = []
        self.channel_sends: list[dict[str, Any]] = []

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        self.approvals.append({
            "approval_id": approval_id,
            "operation": operation,
            "details": details,
            "conversation_key": conversation_key,
            "session_id": session_id,
        })

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: TriggerInfo | None = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        self.child_creations.append({
            "task_key": task_key,
            "lines": list(state.lines),
            "trigger": trigger,
            "conversation_key": conversation_key,
            "session_id": session_id,
        })

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        self.child_updates.append(task_key)

    async def update_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_reply(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_intermediate_reply(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_to_channel(self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any) -> None:
        self.channel_sends.append({
            "conversation_key": conversation_key,
            "text": text,
            "attachments": attachments,
            "kwargs": kwargs,
        })

    async def delete_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def edit_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def finalize_child_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def refresh_typing(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_created(self, *args: Any, **kwargs: Any) -> None:
        return None


def _event(event_type: str, *, session_id: str, payload: dict[str, Any], seq: int = 1) -> Event:
    return Event(
        seq=seq,
        event_id=f"evt-{seq}",
        ts="2026-05-29T00:00:00Z",
        run_id="run-test",
        session_id=session_id,
        component="supervisor",
        type=event_type,
        payload=payload,
    )


def _router(state: SessionState, callbacks: _FakeCallbacks | None = None) -> EventRouter:
    return EventRouter(
        _FakeClient(),
        state,
        callbacks or _FakeCallbacks(),
        BotConfig(
            base_url="http://127.0.0.1:8765",
            entry_node_id="ereuna_main",
            conversation_key_prefix="discord",
            workspace_root=Path(__file__).resolve().parents[1],
            auto_approve_internal=False,
        ),
    )


def test_dispatch_child_session_uses_structured_parent_conversation_key() -> None:
    state = SessionState()
    state.register_session("discord:test-parent-channel", "parent-session")
    state.register_session("agent:coder:agent:scout:discord:wrong", "child-session")
    router = _router(state)

    payload = {
        "task_id": "task-child",
        "session_id": "branch-session",
        "input": {
            "parent_session_id": "child-session",
            "branch_session_id": "branch-session",
            "_dispatch_origin": {
                "parent_session_id": "parent-session",
                "caller_node_id": "scout",
                "parent_conversation_key": "discord:test-parent-channel",
                "context_mode": "accumulate",
            },
            "task_context": {
                "conversation_key": "agent:coder:agent:scout:discord:wrong",
                "route_conversation_key": "discord:test-parent-channel",
                "dispatch_context_mode": "accumulate",
            },
        },
    }

    # Why: old code could keep child-session mapped to the agent:-prefixed key.
    # How: register the task_created payload with structured route metadata.
    # Purpose: approvals and progress from child sessions resolve to the parent channel.
    router._register_dispatch_child_session(_event("task_created", session_id="branch-session", payload=payload), payload)

    assert state.get_conversation_key("child-session") == "discord:test-parent-channel"
    assert state.get_conversation_key("branch-session") == "discord:test-parent-channel"


def test_unowned_approval_is_not_marked_handled() -> None:
    state = SessionState()
    state.session_conv_map["qq-child-session"] = "qq:test-external-channel"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "approval_requested",
        session_id="qq-child-session",
        payload={
            "approval_id": "approval-other-adapter",
            "session_id": "qq-child-session",
            "operation": "execute_command",
            "details": {"tool_name": "execute_command"},
        },
    )

    asyncio.run(router._handle_approval_requested(event))

    assert not router._approval.is_handled("approval-other-adapter")
    assert callbacks.approvals == []


def test_unowned_child_progress_is_skipped_before_state_creation() -> None:
    state = SessionState()
    state.session_conv_map["qq-child-session"] = "qq:test-external-channel"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "handoff_progress",
        session_id="qq-child-session",
        payload={
            "task_id": "task-qq-child",
            "session_id": "qq-child-session",
            "node_id": "ereuna_slave1",
            "message": "working",
        },
    )

    asyncio.run(router._handle_handoff_progress(event))

    assert state.get_child_state("task-qq-child") is None
    assert callbacks.child_creations == []


def test_owned_child_progress_uses_parent_conversation_key() -> None:
    state = SessionState()
    state.session_conv_map["child-session"] = "discord:test-parent-channel"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "handoff_progress",
        session_id="child-session",
        payload={
            "task_id": "task-owned-child",
            "session_id": "child-session",
            "node_id": "ereuna_slave1",
            "message": "working",
        },
    )

    asyncio.run(router._handle_handoff_progress(event))

    assert callbacks.child_creations[0]["conversation_key"] == "discord:test-parent-channel"
    assert callbacks.child_creations[0]["session_id"] == "child-session"


def test_outbound_fallback_prefers_payload_conversation_key() -> None:
    """Core-emitted dispatch attachment events can route after adapter state loss."""
    state = SessionState()
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    attachment = {"type": "image", "path": "data/attachments/novelai/example.png"}
    event = _event(
        "outbound_message",
        session_id="parent-session-without-local-map",
        payload={
            "conversation_key": "discord:test-parent-channel",
            "text": "",
            "attachments": [attachment],
            "message_type": "dispatch_attachment",
            "node_id": "draw.novelai_planner",
        },
    )

    asyncio.run(router._handle_outbound_message(event))

    assert len(callbacks.channel_sends) == 1
    sent = callbacks.channel_sends[0]
    assert sent["conversation_key"] == "discord:test-parent-channel"
    assert sent["attachments"] == [attachment]
    assert sent["kwargs"]["node_id"] == "draw.novelai_planner"
    context = sent["kwargs"]["delivery_context"]
    assert (context.event_id, context.event_seq, context.conversation_key) == (
        "evt-1", 1, "discord:test-parent-channel",
    )


class _RecoveringCallbacks(_FakeCallbacks):
    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures
        self.reply_calls: list[tuple[TriggerInfo, MainTaskState | None]] = []

    async def send_reply(self, trigger: TriggerInfo, text: str, attachments: list[dict[str, Any]], *, main_state: MainTaskState | None = None) -> None:
        self.reply_calls.append((trigger, main_state))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("platform unavailable")

    async def send_to_channel(self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("platform unavailable")
        await super().send_to_channel(conversation_key, text, attachments, **kwargs)


def _recovery_router(path: Path, state: SessionState, callbacks: _RecoveringCallbacks, retry: float = 30.0) -> EventRouter:
    return EventRouter(
        _FakeClient(), state, callbacks,
        BotConfig(
            base_url="http://127.0.0.1:8765",
            entry_node_id="ereuna_main",
            conversation_key_prefix="discord",
            outbound_store_path=path,
            outbound_retry_initial=retry,
            outbound_retry_max=max(retry, 60.0),
            outbound_retry_scan_interval=0.05,
        ),
    )


def _recovery_event(event_id: str = "evt-outbound-70") -> Event:
    return Event(
        seq=70, event_id=event_id, ts="2026-05-29T00:00:00Z",
        run_id="run-test", session_id="session-recovery",
        component="supervisor", type="outbound_message",
        payload={
            "task_id": "task-recovery", "source_inbound_seq": 42,
            "conversation_key": "discord:recovery-channel",
            "node_id": "ereuna_main", "text": "hello", "attachments": [],
        },
    )


def _recovery_state() -> tuple[SessionState, TriggerInfo, MainTaskState]:
    state = SessionState()
    trigger = TriggerInfo(
        inbound_seq=42, conversation_key="discord:recovery-channel",
        session_id="session-recovery", is_dm=False,
        platform_data={"opaque_message": object()},
    )
    state.register_trigger(trigger)
    main_state = state.get_or_create_main_state(42)
    return state, trigger, main_state


def test_outbound_failure_keeps_state_and_persists_retry(tmp_path: Path) -> None:
    state, trigger, main_state = _recovery_state()
    callbacks = _RecoveringCallbacks(failures=1)
    router = _recovery_router(tmp_path / "outbound.json", state, callbacks)
    asyncio.run(router._dispatch(_recovery_event()))

    assert state.get_trigger(42) is trigger
    assert state.get_main_state(42) is main_state
    pending = router._outbound_store.pending()
    assert router._outbound_store.processed_seq == 0
    assert len(pending) == 1
    record = pending[0]
    assert (record.event_id, record.seq, record.task_id, record.attempt) == (
        "evt-outbound-70", 70, "task-recovery", 1,
    )
    assert record.event["payload"]["text"] == "hello"
    assert "platform unavailable" in record.last_error
    assert record.next_retry > time.time()

    record = router.force_replay(event_id=record.event_id)
    asyncio.run(router._deliver_outbound_record(record))
    assert state.get_trigger(42) is None
    assert state.get_main_state(42) is None
    assert router._outbound_store.pending() == []
    assert router._outbound_store.processed_seq == 70


def test_outbound_duplicate_id_or_seq_respects_backoff(tmp_path: Path) -> None:
    state, _, _ = _recovery_state()
    callbacks = _RecoveringCallbacks(failures=5)
    router = _recovery_router(tmp_path / "outbound.json", state, callbacks)
    asyncio.run(router._dispatch(_recovery_event()))
    first_retry = router._outbound_store.pending()[0].next_retry

    asyncio.run(router._dispatch(_recovery_event("evt-alias-same-seq")))
    asyncio.run(router._dispatch(_recovery_event()))

    pending = router._outbound_store.pending()
    assert len(pending) == 1
    assert pending[0].attempt == 1
    assert pending[0].next_retry == first_retry
    assert len(callbacks.reply_calls) == 1


def test_outbound_startup_retry_recovers_without_server_replay(tmp_path: Path) -> None:
    path = tmp_path / "outbound.json"
    state, _, _ = _recovery_state()
    first = _recovery_router(path, state, _RecoveringCallbacks(failures=1), 0.05)
    asyncio.run(first._dispatch(_recovery_event()))

    recovered = _RecoveringCallbacks()
    second = _recovery_router(path, SessionState(), recovered, 0.05)

    async def run_local_recovery() -> None:
        second._running = True
        task = asyncio.create_task(second._run_outbound_retry_loop())
        await asyncio.sleep(0.15)
        second._running = False
        second._outbound_retry_wakeup.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run_local_recovery())
    assert recovered.reply_calls == []
    assert len(recovered.channel_sends) == 1
    sent = recovered.channel_sends[0]
    assert sent["conversation_key"] == "discord:recovery-channel"
    assert sent["text"] == "hello"
    assert sent["kwargs"]["node_id"] == "ereuna_main"
    assert sent["kwargs"]["delivery_context"].attempt == 2
    assert second._outbound_store.pending() == []
    assert second._outbound_store.processed_seq == 70


class _NonRetryable(RuntimeError):
    retryable = False


class _DeadLetterCallbacks(_RecoveringCallbacks):
    def __init__(self) -> None:
        super().__init__()
        self.unsafe = True

    async def send_to_channel(
        self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any,
    ) -> None:
        if self.unsafe:
            raise _NonRetryable("invalid target")
        await super().send_to_channel(conversation_key, text, attachments, **kwargs)


def test_nonretryable_dead_letter_requires_explicit_force_replay(tmp_path: Path) -> None:
    callbacks = _DeadLetterCallbacks()
    router = _recovery_router(tmp_path / "dead-letter.sqlite3", SessionState(), callbacks)
    asyncio.run(router._dispatch(_recovery_event()))
    assert router._outbound_store.pending() == []
    assert [row.event_id for row in router._outbound_store.dead_letters()] == ["evt-outbound-70"]
    assert router._outbound_store.processed_seq == 0
    callbacks.unsafe = False
    replay = router.force_replay(seq=70)
    assert asyncio.run(router._deliver_outbound_record(replay)) is True
    assert router._outbound_store.dead_letters() == []
    assert router._outbound_store.processed_seq == 70


def test_outbound_sqlite_fails_closed_and_keeps_multi_instance_updates(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        OutboundStore(corrupt)
    legacy = tmp_path / "broken.json"
    legacy.write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception):
        OutboundStore(legacy)
    path = tmp_path / "shared.sqlite3"
    first = OutboundStore(path)
    second = OutboundStore(path)
    first.enqueue(_recovery_event("multi-one"))
    another = _recovery_event("multi-two")
    another.seq = 71
    second.enqueue(another)
    assert {row.event_id for row in first.pending()} == {"multi-one", "multi-two"}
    assert {row.event_id for row in second.pending()} == {"multi-one", "multi-two"}


class _ConcurrentContextCallbacks(_FakeCallbacks):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[Any] = []
        self.both_started = asyncio.Event()

    async def send_to_channel(
        self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any,
    ) -> None:
        self.contexts.append(kwargs["delivery_context"])
        if len(self.contexts) == 2:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), timeout=1.0)


def test_formal_delivery_context_is_not_shared_between_concurrent_events(tmp_path: Path) -> None:
    callbacks = _ConcurrentContextCallbacks()
    router = _recovery_router(tmp_path / "concurrent.sqlite3", SessionState(), callbacks)
    first = _recovery_event("concurrent-one")
    first.seq = 81
    first.payload = {**first.payload, "task_id": "task-one", "conversation_key": "discord:one"}
    second = _recovery_event("concurrent-two")
    second.seq = 82
    second.payload = {**second.payload, "task_id": "task-two", "conversation_key": "discord:two"}

    async def dispatch_both() -> None:
        await asyncio.gather(router._dispatch(first), router._dispatch(second))

    asyncio.run(dispatch_both())
    assert {
        (context.event_id, context.event_seq, context.task_id, context.conversation_key)
        for context in callbacks.contexts
    } == {
        ("concurrent-one", 81, "task-one", "discord:one"),
        ("concurrent-two", 82, "task-two", "discord:two"),
    }


def test_two_router_instances_atomically_claim_one_row(tmp_path: Path) -> None:
    class CountingCallbacks(_FakeCallbacks):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.release = asyncio.Event()

        async def send_to_channel(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1
            await self.release.wait()

    path = tmp_path / "claimed.sqlite3"
    callbacks = CountingCallbacks()
    first = _recovery_router(path, SessionState(), callbacks)
    second = _recovery_router(path, SessionState(), callbacks)
    record = first._outbound_store.enqueue(_recovery_event())
    assert record is not None

    async def compete() -> tuple[bool, bool]:
        one = asyncio.create_task(first._deliver_outbound_record(record))
        await asyncio.sleep(0)
        two = asyncio.create_task(second._deliver_outbound_record(record))
        await asyncio.sleep(0.05)
        callbacks.release.set()
        return await one, await two

    results = asyncio.run(compete())
    assert sorted(results) == [False, True]
    assert callbacks.calls == 1


def test_stale_failure_cannot_overwrite_sent_owner(tmp_path: Path) -> None:
    path = tmp_path / "ownership.sqlite3"
    first = OutboundStore(path)
    second = OutboundStore(path)
    record = first.enqueue(_recovery_event())
    assert record is not None
    stale = first.claim(record, owner="stale", lease_seconds=1, now=10)
    assert stale is not None
    winner = second.claim(record, owner="winner", lease_seconds=10, now=12)
    assert winner is not None
    second.acknowledge(winner, owner="winner")
    with pytest.raises(OutboundOwnershipError):
        first.mark_failed(
            stale, RuntimeError("late failure"), owner="stale",
            initial_delay=1, max_delay=2, now=13,
        )
    assert first.enqueue(_recovery_event()) is None
    assert first.processed_seq == 70


def test_ack_row_and_cursor_roll_back_together_on_fault(tmp_path: Path) -> None:
    store = OutboundStore(tmp_path / "atomic.sqlite3")
    record = store.enqueue(_recovery_event())
    assert record is not None
    claimed = store.claim(record, owner="owner", lease_seconds=30)
    assert claimed is not None
    store._db.execute(
        "CREATE TRIGGER fail_cursor BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT, 'cursor fault'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="cursor fault"):
        store.acknowledge(claimed, owner="owner")
    row = store._db.execute("SELECT status,owner FROM outbound WHERE key=?", (record.key,)).fetchone()
    assert tuple(row) == ("delivering", "owner")
    assert store.processed_seq == 0


def test_legacy_migration_never_publishes_partial_database(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    event = _recovery_event().to_dict()
    legacy.write_text(json.dumps({
        "processed_seq": 0,
        "outbox": [
            {"event": event, "event_id": "one", "seq": 1},
            {"event": event, "event_id": "two", "seq": 1},
        ],
    }), encoding="utf-8")
    with pytest.raises(sqlite3.IntegrityError):
        OutboundStore(legacy)
    assert not legacy.with_suffix(".sqlite3").exists()
    assert list(tmp_path.glob("*.migrating")) == []


def test_quick_check_requires_every_result_to_be_ok() -> None:
    class FakeDb:
        def execute(self, sql: str):
            assert sql == "PRAGMA quick_check"
            return self
        def fetchall(self):
            return [("ok",), ("page 4 is corrupt",)]

    with pytest.raises(sqlite3.DatabaseError, match="quick_check failed"):
        OutboundStore._verify_integrity(FakeDb())  # type: ignore[arg-type]


def test_force_replay_of_sent_uses_new_platform_identity(tmp_path: Path) -> None:
    callbacks = _FakeCallbacks()
    router = _recovery_router(tmp_path / "force-sent.sqlite3", SessionState(), callbacks)
    event = _recovery_event("force-sent")
    asyncio.run(router._dispatch(event))
    first_context = callbacks.channel_sends[-1]["kwargs"]["delivery_context"]
    replay = router.force_replay(event_id="force-sent")
    asyncio.run(router._deliver_outbound_record(replay))
    second_context = callbacks.channel_sends[-1]["kwargs"]["delivery_context"]
    assert len(callbacks.channel_sends) == 2
    assert second_context.force_replay is True
    assert second_context.replay_generation == 1
    assert second_context.idempotency_key != first_context.idempotency_key


def _intermediate_event(
    *, seq: int = 170, node_id: str = "ereuna_main",
    conversation_key: str = "discord:intermediate",
) -> Event:
    return Event(
        seq=seq, event_id=f"intermediate-{seq}", ts="", run_id="run",
        session_id="intermediate-session", component="supervisor",
        type="intermediate_reply",
        payload={
            "task_id": f"intermediate-task-{seq}", "source_inbound_seq": 42,
            "conversation_key": conversation_key, "node_id": node_id,
            "text": "partial answer", "attachments": [],
        },
    )


class _IntermediateCallbacks(_FakeCallbacks):
    def __init__(self, *, failures: int = 0, unsafe: bool = False) -> None:
        super().__init__()
        self.failures = failures
        self.unsafe = unsafe
        self.intermediate_contexts: list[Any] = []

    async def _maybe_fail(self) -> None:
        if self.failures:
            self.failures -= 1
            if self.unsafe:
                raise _NonRetryable("intermediate invalid target")
            raise ConnectionError("intermediate platform unavailable")

    async def send_intermediate_reply(
        self, trigger: TriggerInfo, text: str, **kwargs: Any,
    ) -> None:
        self.intermediate_contexts.append(kwargs.get("delivery_context"))
        await self._maybe_fail()

    async def send_to_channel(
        self, conversation_key: str, text: str, attachments: list[Any], **kwargs: Any,
    ) -> None:
        self.intermediate_contexts.append(kwargs.get("delivery_context"))
        await self._maybe_fail()
        await super().send_to_channel(conversation_key, text, attachments, **kwargs)


def test_entry_intermediate_retryable_failure_is_durable_and_never_consumes_trigger(
    tmp_path: Path,
) -> None:
    state, trigger, main_state = _recovery_state()
    callbacks = _IntermediateCallbacks(failures=1)
    router = _recovery_router(tmp_path / "intermediate.sqlite3", state, callbacks)
    event = _intermediate_event()
    asyncio.run(router._dispatch(event))
    rows = router._outbound_store.pending()
    assert len(rows) == 1 and rows[0].record_type == "intermediate_reply"
    assert rows[0].attempt == 1
    assert state.get_trigger(42) is trigger
    assert state.get_main_state(42) is main_state
    replay = router.force_replay(event_id=event.event_id, record_type="intermediate_reply")
    assert asyncio.run(router._deliver_outbound_record(replay)) is True
    assert state.get_trigger(42) is trigger
    assert state.get_main_state(42) is main_state
    assert router._outbound_store.pending() == []
    row = router._outbound_store._db.execute(
        "SELECT status FROM outbound WHERE key=?", (replay.key,),
    ).fetchone()
    assert row[0] == "sent"
    assert callbacks.intermediate_contexts[-1].event_id == event.event_id


def test_onebot_like_child_intermediate_nonretryable_failure_becomes_dead_letter(tmp_path: Path) -> None:
    callbacks = _IntermediateCallbacks(failures=1, unsafe=True)
    router = _recovery_router(tmp_path / "intermediate-dead.sqlite3", SessionState(), callbacks)
    event = _intermediate_event(node_id="worker.child")
    asyncio.run(router._dispatch(event))
    assert router._outbound_store.pending() == []
    dead = router._outbound_store.dead_letters()
    assert len(dead) == 1 and dead[0].record_type == "intermediate_reply"
    assert callbacks.intermediate_contexts[0].idempotency_key.startswith("intermediate_reply:")


def test_intermediate_startup_recovery_uses_captured_route_and_acks(tmp_path: Path) -> None:
    path = tmp_path / "intermediate-restart.sqlite3"
    state, trigger, _ = _recovery_state()
    trigger.conversation_key = "discord:intermediate"
    first = _recovery_router(path, state, _IntermediateCallbacks(failures=1), 0.05)
    asyncio.run(first._dispatch(_intermediate_event()))
    recovered = _IntermediateCallbacks()
    second = _recovery_router(path, SessionState(), recovered, 0.05)

    async def recover() -> None:
        second._running = True
        task = asyncio.create_task(second._run_outbound_retry_loop())
        await asyncio.sleep(0.15)
        second._running = False
        second._outbound_retry_wakeup.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(recover())
    assert len(recovered.channel_sends) == 1
    assert recovered.channel_sends[0]["conversation_key"] == "discord:intermediate"
    assert recovered.channel_sends[0]["kwargs"]["delivery_context"].attempt == 2
    assert second._outbound_store.pending() == []


def test_intermediate_two_instances_claim_only_one_platform_send(tmp_path: Path) -> None:
    class BlockingCallbacks(_IntermediateCallbacks):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.calls = 0
        async def send_to_channel(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1
            await self.release.wait()

    path = tmp_path / "intermediate-claim.sqlite3"
    callbacks = BlockingCallbacks()
    first = _recovery_router(path, SessionState(), callbacks)
    second = _recovery_router(path, SessionState(), callbacks)
    record = first._outbound_store.enqueue(_intermediate_event(node_id="worker.child"))
    assert record is not None

    async def compete() -> tuple[bool, bool]:
        one = asyncio.create_task(first._deliver_outbound_record(record))
        await asyncio.sleep(0)
        two = asyncio.create_task(second._deliver_outbound_record(record))
        await asyncio.sleep(0.05)
        callbacks.release.set()
        return await one, await two

    assert sorted(asyncio.run(compete())) == [False, True]
    assert callbacks.calls == 1


def test_typed_identity_allows_intermediate_and_final_same_seq_and_force_replay(
    tmp_path: Path,
) -> None:
    store = OutboundStore(tmp_path / "typed.sqlite3")
    intermediate = _intermediate_event(seq=250)
    final = _recovery_event("final-same-seq")
    final.seq = 250
    first = store.enqueue(intermediate)
    second = store.enqueue(final)
    assert first is not None and second is not None and first.key != second.key
    with pytest.raises(KeyError, match="ambiguous"):
        store.force_replay(seq=250)
    replay = store.force_replay(seq=250, record_type="intermediate_reply")
    assert replay.record_type == "intermediate_reply"
    assert replay.replay_generation == 1


def test_sent_retention_prunes_only_sent_event_json(tmp_path: Path) -> None:
    store = OutboundStore(tmp_path / "sdk-retention.sqlite3")

    def event(seq: int) -> Event:
        row = _recovery_event(f"retention-{seq}")
        row.seq = seq
        return row

    sent_keys: list[str] = []
    for seq in range(300, 304):
        record = store.enqueue(event(seq))
        assert record is not None
        claimed = store.claim(record, owner=f"sent-{seq}", lease_seconds=30)
        assert claimed is not None
        store.acknowledge(claimed, owner=f"sent-{seq}")
        sent_keys.append(record.key)
    pending = store.enqueue(event(304))
    delivering = store.enqueue(event(305))
    dead = store.enqueue(event(306))
    assert pending and delivering and dead
    delivering = store.claim(delivering, owner="active", lease_seconds=300)
    dead_claim = store.claim(dead, owner="dead", lease_seconds=30)
    assert delivering and dead_claim
    store.mark_dead_letter(dead_claim, _NonRetryable("bad"), owner="dead")
    store._db.execute("UPDATE outbound SET sent_at=1 WHERE status='sent'")
    removed = store.prune_sent(ttl_seconds=10, max_rows=1, now=100)
    assert removed == 4
    states = dict(store._db.execute("SELECT key,status FROM outbound").fetchall())
    assert states[pending.key] == "pending"
    assert states[delivering.key] == "delivering"
    assert states[dead.key] == "dead_letter"
    assert not any(key in states for key in sent_keys)



def test_discord_legacy_intermediate_callback_failure_is_not_swallowed(tmp_path: Path) -> None:
    class DiscordLikeCallbacks(_FakeCallbacks):
        async def send_intermediate_reply(
            self, trigger: TriggerInfo, text: str,
        ) -> None:
            raise ConnectionError("discord send failed")

    state, trigger, _ = _recovery_state()
    router = _recovery_router(
        tmp_path / "discord-intermediate.sqlite3", state, DiscordLikeCallbacks(),
    )
    asyncio.run(router._dispatch(_intermediate_event()))
    rows = router._outbound_store.pending()
    assert len(rows) == 1 and rows[0].record_type == "intermediate_reply"
    assert "discord send failed" in rows[0].last_error
    assert state.get_trigger(42) is trigger


def test_sent_retention_max_rows_reclaims_oldest_processed_events(tmp_path: Path) -> None:
    store = OutboundStore(tmp_path / "sdk-retention-bound.sqlite3")
    for seq in range(400, 405):
        event = _recovery_event(f"bound-{seq}")
        event.seq = seq
        record = store.enqueue(event)
        assert record is not None
        claimed = store.claim(record, owner=f"owner-{seq}", lease_seconds=30)
        assert claimed is not None
        store.acknowledge(claimed, owner=f"owner-{seq}")
        store._db.execute(
            "UPDATE outbound SET sent_at=? WHERE key=?", (float(seq), record.key),
        )
    assert store.prune_sent(ttl_seconds=10_000, max_rows=2, now=500) == 3
    remaining = store._db.execute(
        "SELECT seq FROM outbound WHERE status='sent' ORDER BY seq"
    ).fetchall()
    assert [row[0] for row in remaining] == [403, 404]
