"""Focused tests for recoverable Supervisor completion routing."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin.memory_extract import MemoryExtractHandler  # noqa: E402
from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.hooks import HookRegistry  # noqa: E402
import supervisor.branch_finalize_store as branch_store_module  # noqa: E402
import supervisor.eventlog as eventlog_module  # noqa: E402
from supervisor.branch_finalize_store import BranchFinalizeClaimStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_router import BranchFinalizePlan, PostCompletionWork  # noqa: E402
from supervisor.types import RouteStatus, Task, TaskKind, TaskStatus  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="route-recovery-test"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _running_task(state: SupervisorState, session_id: str, *, source_seq: int = 17) -> Task:
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.node",
            input_data={"task_context": {"conversation_key": "test:route-recovery"}},
            continuation={},
            source_inbound_seq=source_seq,
        )
        task.status = TaskStatus.running
        task.worker_id = "worker-1"
        return task


def _events(workspace: Path) -> list[dict]:
    path = workspace / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_types(workspace: Path) -> list[str]:
    return [str(event.get("type") or "") for event in _events(workspace)]


def _outbounds(workspace: Path, delivery_id: str = "") -> list[dict]:
    rows = [event for event in _events(workspace) if event.get("type") == "outbound_message"]
    if delivery_id:
        rows = [
            event for event in rows
            if str((event.get("payload") or {}).get("delivery_id") or "") == delivery_id
        ]
    return rows


def _enable_real_outbound(state: SupervisorState, task: Task) -> None:
    """Register the real source inbound expected by append_outbound_message."""
    if task.source_inbound_seq is None:
        return
    state._inbound_events[task.source_inbound_seq] = {
        "session_id": task.session_id,
        "payload": {"conversation_key": "test:route-recovery", "text": "hello"},
    }


def test_legacy_task_snapshot_defaults_to_unrouted() -> None:
    """Snapshots written before route fields existed remain valid and retryable."""
    now = datetime.now(timezone.utc).isoformat()
    task = Task.model_validate({
        "task_id": "legacy-task",
        "session_id": "legacy-session",
        "kind": "node",
        "status": "completed",
        "created_at": now,
        "updated_at": now,
        "result": {"action": "finish", "result": {"text": "done"}},
    })

    assert task.route_status == RouteStatus.pending
    assert task.routed_at is None
    assert task.route_error == ""
    dumped = task.model_dump(mode="json")
    assert dumped["route_status"] == "pending"
    assert dumped["routed_at"] is None


def test_route_failure_is_persisted_and_terminal_complete_retry_resumes_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(
        channel="test", conversation_key="test:route-recovery",
    )
    task = _running_task(state, session_id)
    calls: list[str] = []

    def flaky_route(self: SupervisorState, routed_task: Task) -> None:
        calls.append(routed_task.task_id)
        if len(calls) == 1:
            raise RuntimeError("synthetic route failure")

    monkeypatch.setattr(SupervisorState, "_route_completed_task_inner_locked", flaky_route)
    caplog.set_level(logging.INFO)
    result = {"action": "finish", "result": {"text": "delivered once"}}

    with pytest.raises(RuntimeError, match="synthetic route failure"):
        state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)

    assert task.status == TaskStatus.completed
    assert task.result == result
    assert task.route_status == RouteStatus.failed
    assert task.routed_at is None
    assert task.route_error == "synthetic route failure"
    assert "task_route_failed" in _event_types(tmp_path)

    retried = state.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "fail", "error": "must not replace terminal result"},
    )

    assert retried is task
    assert calls == [task.task_id, task.task_id]
    assert task.result == result
    assert task.route_status == RouteStatus.routed
    assert task.routed_at is not None
    assert task.route_error == ""
    assert "task_routed" in _event_types(tmp_path)

    routed_record = next(record for record in caplog.records if getattr(record, "event", "") == "task_routed")
    assert routed_record.task == task.task_id
    assert routed_record.source_seq == 17
    assert routed_record.conversation == "test:route-recovery"
    assert routed_record.session_id == session_id


def test_already_routed_terminal_complete_does_not_route_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(
        channel="test", conversation_key="test:route-recovery",
    )
    task = _running_task(state, session_id)
    calls = 0

    def count_route(self: SupervisorState, routed_task: Task) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(SupervisorState, "_route_completed_task_inner_locked", count_route)
    completion = {"action": "cancelled"}

    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=completion)
    first_routed_at = task.routed_at
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=completion)

    assert calls == 1
    assert task.route_status == RouteStatus.routed
    assert task.routed_at == first_routed_at
    assert _event_types(tmp_path).count("task_routed") == 1


def test_cancel_requested_completion_is_marked_handled_without_later_output_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(
        channel="test", conversation_key="test:route-recovery",
    )
    task = _running_task(state, session_id)
    task.cancel_requested = True
    calls = 0

    def count_route(self: SupervisorState, routed_task: Task) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(SupervisorState, "_route_completed_task_inner_locked", count_route)
    stale_result = {"action": "finish", "result": {"text": "must not be emitted"}}

    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=stale_result)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=stale_result)

    assert calls == 0
    assert task.status == TaskStatus.cancelled
    assert task.route_status == RouteStatus.routed
    assert task.routed_at is not None


def test_new_completion_resets_route_state_from_prior_dispatch_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed task's next result must route even if its prior dispatch was routed."""
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(
        channel="test", conversation_key="test:route-recovery",
    )
    task = _running_task(state, session_id)
    task.route_status = RouteStatus.routed
    task.routed_at = datetime.now(timezone.utc)
    calls = 0

    def count_route(self: SupervisorState, routed_task: Task) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(SupervisorState, "_route_completed_task_inner_locked", count_route)

    state.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "finish", "result": {"text": "second phase"}},
    )

    assert calls == 1
    assert task.route_status == RouteStatus.routed
    assert task.routed_at is not None


def test_same_delivery_id_is_persistently_deduped(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:dedupe")
    first = state.append_outbound_message(session_id=session_id, text="once", delivery_id="delivery:test:1")
    second = state.append_outbound_message(session_id=session_id, text="must not append", delivery_id="delivery:test:1")
    assert first["deduped"] is False
    assert second["deduped"] is True
    rows = _outbounds(tmp_path, "delivery:test:1")
    assert len(rows) == 1
    assert rows[0]["payload"]["text"] == "once"


def test_real_route_retry_after_append_failure_emits_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:route-recovery")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)
    original_append = state.append_outbound_message
    failed_once = False

    def append_then_crash(**kwargs):
        nonlocal failed_once
        result = original_append(**kwargs)
        if not failed_once:
            failed_once = True
            raise RuntimeError("crash after durable outbound append")
        return result

    monkeypatch.setattr(state, "append_outbound_message", append_then_crash)
    result = {"action": "finish", "result": {"text": "visible once"}}
    with pytest.raises(RuntimeError, match="crash after durable"):
        state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)
    delivery_id = task.delivery_id
    assert task.route_status == RouteStatus.failed
    assert len(_outbounds(tmp_path, delivery_id)) == 1
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)
    assert task.route_status == RouteStatus.routed
    assert len(_outbounds(tmp_path, delivery_id)) == 1
    types = _event_types(tmp_path)
    assert types.index("task_completed") < types.index("outbound_message") < types.index("task_routed")


def test_task_routed_snapshot_failure_retries_without_duplicate_outbound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:route-recovery")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)
    original_snapshot = state._event_task_snapshot
    failed_once = False

    def fail_routed_snapshot(event_type: str, routed_task: Task, **kwargs) -> None:
        nonlocal failed_once
        if event_type == "task_routed" and not failed_once:
            failed_once = True
            raise OSError("snapshot disk failure")
        original_snapshot(event_type, routed_task, **kwargs)

    monkeypatch.setattr(state, "_event_task_snapshot", fail_routed_snapshot)
    result = {"action": "finish", "result": {"text": "one reply"}}
    with pytest.raises(OSError, match="snapshot disk failure"):
        state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)
    delivery_id = task.delivery_id
    assert task.route_status == RouteStatus.failed
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)
    assert task.route_status == RouteStatus.routed
    assert len(_outbounds(tmp_path, delivery_id)) == 1


def test_hook_and_summary_failures_do_not_change_main_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:route-recovery")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)

    def fail_hook(*args, **kwargs):
        raise RuntimeError("hook failed")

    def fail_summary(*args, **kwargs):
        raise RuntimeError("summary failed")

    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    state.hook_registry = HookRegistry()
    state.hook_registry.register("on_entry_task_complete", fail_hook)
    monkeypatch.setattr(state, "_maybe_trigger_turn_summary_locked", fail_summary)
    monkeypatch.setattr(state, "_execute_post_cleanup", fail_cleanup)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "already delivered"}})
    state._post_completion_queue.join()
    assert task.route_status == RouteStatus.routed
    assert len(_outbounds(tmp_path, task.delivery_id)) == 1
    failure_kinds = {
        str((event.get("payload") or {}).get("phase_kind") or "")
        for event in _events(tmp_path)
        if event.get("type") == "task_post_phase_failed"
    }
    assert {"hook", "turn_summary", "cleanup"} <= failure_kinds
    assert task.post_work_status == "partial"


def test_eventlog_startup_recovery_dedupes_already_appended_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _make_state(tmp_path)
    session_id = first.get_or_create_session(channel="test", conversation_key="test:restart")
    task = _running_task(first, session_id)
    first._inbound_events[17] = {"session_id": session_id, "payload": {"conversation_key": "test:restart", "text": "hello"}}
    original_append = first.append_outbound_message

    def append_then_crash(**kwargs):
        original_append(**kwargs)
        raise RuntimeError("process died after append")

    monkeypatch.setattr(first, "append_outbound_message", append_then_crash)
    with pytest.raises(RuntimeError, match="process died"):
        first.complete_task(task_id=task.task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "restart-safe"}})
    delivery_id = task.delivery_id
    assert len(_outbounds(tmp_path, delivery_id)) == 1
    restarted = _make_state(tmp_path)
    restored = restarted.tasks[task.task_id]
    assert restored.route_status == RouteStatus.routed
    assert len(_outbounds(tmp_path, delivery_id)) == 1
    assert _event_types(tmp_path).count("task_routed") == 1


def test_legacy_terminal_snapshot_and_new_nonterminal_are_not_startup_replayed(tmp_path: Path) -> None:
    seed = _make_state(tmp_path)
    session_id = seed.get_or_create_session(channel="test", conversation_key="test:legacy")
    now = datetime.now(timezone.utc).isoformat()
    base = {"session_id": session_id, "session_generation": 1, "kind": "node", "created_at": now, "updated_at": now, "result": {"action": "finish", "result": {"text": "historical"}}}
    seed.eventlog.append(session_id=session_id, component="engine", type_="task_completed", payload={"task_id": "legacy-terminal", "status": "completed", **base})
    seed.eventlog.append(session_id=session_id, component="engine", type_="task_started", payload={"task_id": "new-running", "status": "running", "route_schema_version": 1, "route_status": "pending", "route_generation": 1, "delivery_id": "task:new-running:completion:1:v1", **base})
    restarted = _make_state(tmp_path)
    assert "legacy-terminal" not in restarted.tasks
    assert "new-running" not in restarted.tasks
    assert not _outbounds(tmp_path)


def test_branch_route_context_survives_restart_after_append_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _make_state(tmp_path)
    parent_id = first.get_or_create_session(channel="test", conversation_key="test:branch-restart")
    inbound = first.eventlog.append(session_id=parent_id, component="test", type_="inbound_message", payload={"channel": "test", "conversation_key": "test:branch-restart", "text": "branch input"})
    first.record_inbound_message_event(inbound)
    assigned = first.assign_next_task(worker_id="worker-1")
    assert assigned is not None
    task = first.tasks[str(assigned["task_id"])]
    branch_id = task.session_id
    original_append = first.append_outbound_message

    def append_then_crash(**kwargs):
        original_append(**kwargs)
        raise RuntimeError("branch crash after append")

    monkeypatch.setattr(first, "append_outbound_message", append_then_crash)
    with pytest.raises(RuntimeError, match="branch crash"):
        first.complete_task(task_id=task.task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "branch-safe"}})
    delivery_id = task.delivery_id
    restarted = _make_state(tmp_path)
    restored = restarted.tasks[task.task_id]
    assert restored.route_status == RouteStatus.routed
    assert restored.route_context["route_session_id"] == parent_id
    assert restored.route_context["branch_session_id"] == branch_id
    assert len(_outbounds(tmp_path, delivery_id)) == 1
    assert branch_id not in restarted.sessions


def test_slow_post_hook_does_not_hold_supervisor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:slow-hook")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)
    hook_started = threading.Event()
    release_hook = threading.Event()

    def slow_hook(*args, **kwargs):
        hook_started.set()
        assert release_hook.wait(timeout=3)

    state.hook_registry = HookRegistry()
    state.hook_registry.register("on_entry_task_complete", slow_hook)
    state.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "finish", "result": {"text": "already routed"}},
    )
    assert hook_started.wait(timeout=2)
    started_at = time.monotonic()
    with state._lock:
        state.session_generations[session_id] = state.session_generations.get(session_id, 1)
    lock_wait = time.monotonic() - started_at
    release_hook.set()
    state._post_completion_queue.join()
    assert lock_wait < 0.2
    assert task.route_status == RouteStatus.routed
    assert task.post_work_status == "completed"


def test_pending_post_work_recovers_after_restart_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def count_hook(ctx):
        calls.append(str(ctx.get("post_work_id") or ""))

    def handlers_for(self, hook_point):
        return [{
            "name": "restart_counter",
            "callback": count_hook,
            "post_work_idempotent": True,
        }]

    monkeypatch.setattr(HookRegistry, "handlers_for", handlers_for)
    first = _make_state(tmp_path)
    session_id = first.get_or_create_session(channel="test", conversation_key="test:post-restart")
    task = _running_task(first, session_id)
    _enable_real_outbound(first, task)
    monkeypatch.setattr(first, "_enqueue_post_completion_work", lambda work: True)
    first.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "finish", "result": {"text": "route before crash"}},
    )
    post_work_id = task.post_work_id
    assert post_work_id
    assert not [event for event in _events(tmp_path) if event.get("type") == "task_post_work_completed"]
    restarted = _make_state(tmp_path)
    restarted._post_completion_queue.join()
    completed = [
        event for event in _events(tmp_path)
        if event.get("type") == "task_post_work_completed"
        and str((event.get("payload") or {}).get("post_work_id") or "") == post_work_id
    ]
    assert calls == [post_work_id]
    assert len(completed) == 1
    restarted.recover_pending_post_completion_work()
    restarted._post_completion_queue.join()
    completed_again = [
        event for event in _events(tmp_path)
        if event.get("type") == "task_post_work_completed"
        and str((event.get("payload") or {}).get("post_work_id") or "") == post_work_id
    ]
    assert calls == [post_work_id]
    assert len(completed_again) == 1


def _append_rotation_probe(log: EventLog, label: str) -> dict:
    return log.append(
        session_id="rotation-test",
        component="test",
        type_="rotation_probe",
        payload={"label": label},
    )


def _force_eventlog_rotate(log: EventLog) -> None:
    with log._lock:
        log._maybe_rotate_locked()


@pytest.mark.parametrize("recreate_empty_active", [False, True])
def test_eventlog_restart_seq_continues_when_active_was_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recreate_empty_active: bool,
) -> None:
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_MAX_BYTES", 1)
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", 3)
    path = tmp_path / "events.jsonl"
    first = EventLog(path, run_id="first")
    assert _append_rotation_probe(first, "one")["seq"] == 1
    assert _append_rotation_probe(first, "two")["seq"] == 2
    _force_eventlog_rotate(first)
    assert not path.exists()
    assert (tmp_path / "events.jsonl.1").exists()
    if recreate_empty_active:
        path.touch()
    restarted = EventLog(path, run_id="second")
    assert _append_rotation_probe(restarted, "three")["seq"] == 3


def test_eventlog_iter_persisted_events_is_oldest_first_across_rotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_MAX_BYTES", 1)
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", 3)
    path = tmp_path / "events.jsonl"
    log = EventLog(path, run_id="first")
    _append_rotation_probe(log, "one")
    _append_rotation_probe(log, "two")
    _force_eventlog_rotate(log)
    log = EventLog(path, run_id="second")
    _append_rotation_probe(log, "three")
    _force_eventlog_rotate(log)
    assert not path.exists()
    restarted = EventLog(path, run_id="third")
    assert _append_rotation_probe(restarted, "four")["seq"] == 4
    retained = [
        event for event in restarted.iter_persisted_events()
        if event.get("type") == "rotation_probe"
    ]
    assert [event["seq"] for event in retained] == [1, 2, 3, 4]
    assert [(event.get("payload") or {}).get("label") for event in retained] == [
        "one", "two", "three", "four",
    ]


def test_unknown_hook_side_effect_started_crash_becomes_ambiguous_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    state.hook_registry = HookRegistry()
    side_effects: list[str] = []

    def third_party_hook(ctx):
        side_effects.append(str(ctx.get("post_phase_id") or ""))

    state.hook_registry.register("on_entry_task_complete", third_party_hook)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:unknown-hook")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)
    original_persist = state._persist_post_phase_status_locked
    crashed = False

    def crash_before_hook_completed(work, phase, *, status, error=""):
        nonlocal crashed
        if phase.get("kind") == "hook" and status == "completed" and not crashed:
            crashed = True
            raise RuntimeError("crash after third-party side effect")
        return original_persist(work, phase, status=status, error=error)

    monkeypatch.setattr(state, "_persist_post_phase_status_locked", crash_before_hook_completed)
    state.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "finish", "result": {"text": "routed"}},
    )
    state._post_completion_queue.join()
    assert len(side_effects) == 1

    restarted = _make_state(tmp_path)
    restarted._post_completion_queue.join()
    assert len(side_effects) == 1
    ambiguous = [
        event for event in _events(tmp_path)
        if event.get("type") == "task_post_phase_ambiguous"
        and str((event.get("payload") or {}).get("phase_kind") or "") == "hook"
    ]
    assert len(ambiguous) == 1
    assert any(
        event.get("type") == "task_post_phase_completed"
        and str((event.get("payload") or {}).get("phase_kind") or "") == "cleanup"
        for event in _events(tmp_path)
    )


def _memory_intent(session_id: str, *, due_at: datetime, suffix: str = "one") -> dict:
    effect_id = f"post:memory:{suffix}:hook:memory_extract:memory_extract_schedule"
    return {
        "intent_id": effect_id,
        "effect_id": effect_id,
        "phase_id": f"post:memory:{suffix}:hook:memory_extract",
        "post_work_id": f"post:memory:{suffix}",
        "source_task_id": f"source-{suffix}",
        "session_id": session_id,
        "session_generation": 1,
        "extractor_node": "system.memory_extractor",
        "kind": "node",
        "transcript": "User: remember this",
        "task_count": 1,
        "pending_task_ids": [f"source-{suffix}"],
        "conversation_key": "test:memory-ledger",
        "due_at": due_at.isoformat(),
    }


def _cancel_memory_timers(state: SupervisorState) -> None:
    for timer in list(state._memory_extract_intent_timers.values()):
        timer.cancel()
    state._memory_extract_intent_timers.clear()


def test_memory_extract_scheduled_intent_restores_timer_after_process_rebuild(
    tmp_path: Path,
) -> None:
    first = _make_state(tmp_path)
    session_id = first.get_or_create_session(
        channel="test", conversation_key="test:memory-ledger",
    )
    intent = _memory_intent(
        session_id, due_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    first._persist_memory_extract_intent(intent)
    assert intent["intent_id"] in first._memory_extract_intent_timers
    _cancel_memory_timers(first)  # simulate process death without a cancellation event

    restarted = _make_state(tmp_path)
    assert restarted._memory_extract_intents[intent["intent_id"]] == intent
    assert intent["intent_id"] in restarted._memory_extract_intent_timers
    _cancel_memory_timers(restarted)


def test_memory_extract_due_intent_creates_task_and_completes_once(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(
        channel="test", conversation_key="test:memory-ledger",
    )
    intent = _memory_intent(
        session_id, due_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    state._persist_memory_extract_intent(intent)
    _cancel_memory_timers(state)
    state._fire_memory_extract_intent(intent["intent_id"])
    state._fire_memory_extract_intent(intent["intent_id"])

    created = [
        event for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "task_created"
        and str((((event.get("payload") or {}).get("input") or {}).get("_memory_extract_intent_id")) or "")
        == intent["intent_id"]
    ]
    assert len(created) == 1
    assert intent["intent_id"] not in state._memory_extract_intents
    effect_types = [
        event.get("type") for event in state.eventlog.iter_persisted_events()
        if str((event.get("payload") or {}).get("effect_id") or "") == intent["effect_id"]
    ]
    assert effect_types[-1] == "task_post_effect_completed"


def test_memory_extract_crash_after_task_created_before_completed_dedupes_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _make_state(tmp_path)
    session_id = first.get_or_create_session(
        channel="test", conversation_key="test:memory-ledger",
    )
    intent = _memory_intent(
        session_id,
        due_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        suffix="crash",
    )
    first._persist_memory_extract_intent(intent)
    _cancel_memory_timers(first)

    def crash_before_completed(intent_id: str) -> None:
        raise RuntimeError("crash after task_created")

    monkeypatch.setattr(first, "_complete_memory_extract_intent", crash_before_completed)
    first._fire_memory_extract_intent(intent["intent_id"])
    _cancel_memory_timers(first)

    restarted = _make_state(tmp_path)
    _cancel_memory_timers(restarted)
    restarted._fire_memory_extract_intent(intent["intent_id"])
    created = [
        event for event in restarted.eventlog.iter_persisted_events()
        if event.get("type") == "task_created"
        and str((((event.get("payload") or {}).get("input") or {}).get("_memory_extract_intent_id")) or "")
        == intent["intent_id"]
    ]
    assert len(created) == 1
    assert intent["intent_id"] not in restarted._memory_extract_intents


def test_turn_summary_phase_identity_dedupes_created_task_across_restart(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:summary-ledger")
    task = _running_task(state, session_id)
    task.status = TaskStatus.completed
    task.result = {
        "action": "finish",
        "result": {"text": "done"},
        "_tool_call_count": 3,
    }
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append(
        session_id,
        Message(
            id="summary-source",
            role="assistant",
            content="summary source",
            message_type=MessageType.ASSISTANT,
            source_task_id=task.task_id,
        ),
    )
    phase_id = "post:summary:turn_summary"
    state._maybe_trigger_turn_summary_locked(task, phase_id=phase_id)

    restarted = _make_state(tmp_path)
    restarted._maybe_trigger_turn_summary_locked(task.model_copy(deep=True), phase_id=phase_id)
    matching_created = [
        event for event in restarted.eventlog.iter_persisted_events()
        if event.get("type") == "task_created"
        and str((((event.get("payload") or {}).get("input") or {}).get("_post_phase_id")) or "") == phase_id
    ]
    assert len(matching_created) == 1


def test_cleanup_file_io_does_not_hold_supervisor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:cleanup-lock")
    child_sid, _ = state.get_or_create_child_session(parent, "cleanup.node", "case", "fresh")
    child_path = tmp_path / "data" / "conversations" / f"{child_sid}.jsonl"
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_text("", encoding="utf-8")
    task = _running_task(state, parent, source_seq=0)
    task.status = TaskStatus.completed
    task.input.update({"child_session_id": child_sid, "context_mode": "fresh"})
    work = PostCompletionWork(
        task=task.model_copy(deep=True),
        route_session_id=parent,
        session_generation=1,
        post_work_id="post:cleanup",
        schema_version=2,
    )
    phase = {"phase_id": "post:cleanup:phase", "kind": "cleanup", "name": "cleanup", "retry_policy": "retry"}
    unlink_started = threading.Event()
    release_unlink = threading.Event()
    flush_started = threading.Event()
    release_flush = threading.Event()
    original_unlink = Path.unlink
    original_flush = state._session_store.flush_snapshot

    def slow_unlink(path, *args, **kwargs):
        if path == child_path:
            unlink_started.set()
            assert release_unlink.wait(timeout=3)
        return original_unlink(path, *args, **kwargs)

    def slow_flush(snapshot):
        flush_started.set()
        assert release_flush.wait(timeout=3)
        return original_flush(snapshot)

    monkeypatch.setattr(Path, "unlink", slow_unlink)
    monkeypatch.setattr(state._session_store, "flush_snapshot", slow_flush)
    worker = threading.Thread(target=state._execute_post_cleanup, args=(work, phase))
    worker.start()
    assert unlink_started.wait(timeout=2)
    started_at = time.monotonic()
    with state._lock:
        state.session_generations[parent] = state.session_generations.get(parent, 1)
    lock_wait = time.monotonic() - started_at
    release_unlink.set()
    assert flush_started.wait(timeout=2)
    flush_lock_started = time.monotonic()
    with state._lock:
        state.session_generations[parent] = state.session_generations.get(parent, 1)
    flush_lock_wait = time.monotonic() - flush_lock_started
    release_flush.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert lock_wait < 0.2
    assert flush_lock_wait < 0.2


def test_failed_hook_phase_does_not_block_summary_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    state.hook_registry = HookRegistry()

    def fail_hook(ctx):
        raise RuntimeError("hook side effect failed")

    state.hook_registry.register("on_entry_task_complete", fail_hook)
    later: list[str] = []
    monkeypatch.setattr(
        state,
        "_maybe_trigger_turn_summary_locked",
        lambda task, phase_id="": later.append("summary"),
    )
    monkeypatch.setattr(
        state,
        "_execute_post_cleanup",
        lambda work, phase: later.append("cleanup"),
    )
    session_id = state.get_or_create_session(channel="test", conversation_key="test:phase-isolation")
    task = _running_task(state, session_id)
    _enable_real_outbound(state, task)
    state.complete_task(
        task_id=task.task_id,
        worker_id="worker-1",
        result={"action": "finish", "result": {"text": "done"}},
    )
    state._post_completion_queue.join()
    assert later == ["summary", "cleanup"]


def test_entry_branch_finalize_io_never_holds_supervisor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:branch-io-lock")
    inbound = state.eventlog.append(
        session_id=parent,
        component="test",
        type_="inbound_message",
        payload={"channel": "test", "conversation_key": "test:branch-io-lock", "text": "go"},
    )
    state.record_inbound_message_event(inbound)
    assigned = state.assign_next_task(worker_id="worker-1")
    assert assigned is not None
    task = state.tasks[str(assigned["task_id"])]
    branch_sid = task.session_id
    ConversationStore(tmp_path / "data" / "conversations").append(
        branch_sid,
        Message(
            id="branch-final-message",
            role="assistant",
            content="branch output",
            message_type=MessageType.ASSISTANT,
            source_task_id=task.task_id,
        ),
    )
    merge_started, release_merge = threading.Event(), threading.Event()
    delete_started, release_delete = threading.Event(), threading.Event()
    flush_started, release_flush = threading.Event(), threading.Event()
    original_append_batch = ConversationStore.append_batch
    original_delete = ConversationStore.delete
    original_flush = state._session_store.flush_snapshot

    def slow_append_batch(store, session_id, messages):
        if session_id == parent:
            merge_started.set()
            assert release_merge.wait(timeout=3)
        return original_append_batch(store, session_id, messages)

    def slow_delete(store, session_id):
        if session_id == branch_sid:
            delete_started.set()
            assert release_delete.wait(timeout=3)
        return original_delete(store, session_id)

    def slow_flush(snapshot):
        flush_started.set()
        assert release_flush.wait(timeout=3)
        return original_flush(snapshot)

    monkeypatch.setattr(ConversationStore, "append_batch", slow_append_batch)
    monkeypatch.setattr(ConversationStore, "delete", slow_delete)
    monkeypatch.setattr(state._session_store, "flush_snapshot", slow_flush)
    errors: list[Exception] = []

    def complete() -> None:
        try:
            state.complete_task(
                task_id=task.task_id,
                worker_id="worker-1",
                result={"action": "finish", "result": {"text": "done"}},
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=complete)
    worker.start()
    for started, release in (
        (merge_started, release_merge),
        (delete_started, release_delete),
        (flush_started, release_flush),
    ):
        assert started.wait(timeout=2)
        acquired_at = time.monotonic()
        with state._lock:
            state.session_generations[parent] = state.session_generations.get(parent, 1)
        assert time.monotonic() - acquired_at < 0.2
        release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert errors == []
    state._post_completion_queue.join()
    assert task.route_status == RouteStatus.routed
    assert task.input.get("_branch_finalized") is True


def _make_branch_task(state: SupervisorState, parent: str, inbound_seq: int, worker_id: str) -> Task:
    with state._lock:
        branch, fork_meta = state._create_entry_branch_locked(parent, inbound_seq=inbound_seq)
        task = state._create_task_locked(
            session_id=branch,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.node",
            input_data={
                "parent_session_id": parent,
                "branch_session_id": branch,
                "base_count": int(fork_meta.get("base_count") or 0),
                "task_context": {"conversation_key": "test:branch-concurrency"},
            },
            continuation={},
            source_inbound_seq=None,
        )
        task.status = TaskStatus.running
        task.worker_id = worker_id
        return task


def test_concurrent_complete_same_branch_has_one_finalize_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    state._branch_finalize_lease_seconds = 0.3
    parent = state.get_or_create_session(channel="test", conversation_key="test:same-branch-owner")
    task = _make_branch_task(state, parent, 901, "worker-1")
    branch = task.session_id
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append(
        branch,
        Message(id="same-tail", role="assistant", content="tail", message_type=MessageType.ASSISTANT, source_task_id=task.task_id),
    )
    entered = threading.Event()
    release = threading.Event()
    execute_count = 0
    cleanup_count = 0
    original_body = state._execute_branch_finalize_io_body
    original_delete = ConversationStore.delete

    def slow_body(plan):
        nonlocal execute_count
        execute_count += 1
        entered.set()
        assert release.wait(timeout=3)
        return original_body(plan)

    def count_delete(conv_store, session_id):
        nonlocal cleanup_count
        if session_id == branch:
            cleanup_count += 1
        return original_delete(conv_store, session_id)

    monkeypatch.setattr(state, "_execute_branch_finalize_io_body", slow_body)
    monkeypatch.setattr(ConversationStore, "delete", count_delete)
    start = threading.Barrier(3)
    errors: list[Exception] = []

    def complete(worker_id: str) -> None:
        try:
            start.wait(timeout=2)
            state.complete_task(
                task_id=task.task_id,
                worker_id=worker_id,
                result={"action": "finish", "result": {"text": "done"}},
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=complete, args=("worker-1",))
    second = threading.Thread(target=complete, args=("worker-2",))
    first.start()
    second.start()
    start.wait(timeout=2)
    assert entered.wait(timeout=2)
    time.sleep(0.7)
    release.set()
    first.join(timeout=3)
    second.join(timeout=3)
    assert errors == []
    assert execute_count == 1
    assert cleanup_count == 1
    parent_messages = ConversationStore(tmp_path / "data" / "conversations").load(parent)
    assert [message.id for message in parent_messages].count("same-tail") == 1
    parent_path = tmp_path / "data" / "conversations" / f"{parent}.jsonl"
    for line in parent_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
    event_types = _event_types(tmp_path)
    assert event_types.count("task_branch_finalize_started") == 1
    assert event_types.count("task_branch_finalize_heartbeat") >= 1
    assert event_types.count("task_branch_finalize_completed") == 1


def test_branch_finalize_owner_from_previous_run_is_taken_over(tmp_path: Path) -> None:
    first = _make_state(tmp_path)
    plan = BranchFinalizePlan(
        identity="branch-owner-crash",
        task_id="task-owner",
        parent_session_id="parent-owner",
        branch_session_id="branch-owner",
        base_count=0,
        merge=True,
        cleanup_session_ids=("branch-owner",),
    )
    with first._lock:
        old_owner = first._claim_branch_finalize_locked(plan)
    assert old_owner.status == "owned"

    restarted = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="new-owner-run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    with restarted._lock:
        live_claim = restarted._claim_branch_finalize_locked(plan)
    assert live_claim.status == "deferred"
    assert live_claim.owner_id == old_owner.owner_id

    new_owner = restarted._branch_finalize_store.claim(
        plan.identity,
        owner_run_id="new-owner-run",
        owner_pid=os.getpid(),
        lease_seconds=6.0,
        now=old_owner.lease_expires_at + 1.0,
    )
    with restarted._lock:
        with pytest.raises(RuntimeError, match="ownership lost"):
            restarted._assert_branch_finalize_owner_locked(plan, old_owner)
        restarted._assert_branch_finalize_owner_locked(plan, new_owner)
    assert new_owner.status == "owned"
    assert new_owner.owner_id != old_owner.owner_id
    assert new_owner.fencing_generation == old_owner.fencing_generation + 1


def test_different_branches_same_parent_write_valid_jsonl_concurrently(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:shared-parent")
    first_task = _make_branch_task(state, parent, 911, "worker-a")
    second_task = _make_branch_task(state, parent, 912, "worker-b")
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append(first_task.session_id, Message(id="tail-a", role="assistant", content="a", message_type=MessageType.ASSISTANT, source_task_id=first_task.task_id))
    store.append(second_task.session_id, Message(id="tail-b", role="assistant", content="b", message_type=MessageType.ASSISTANT, source_task_id=second_task.task_id))
    start = threading.Barrier(3)
    errors: list[Exception] = []

    def complete(task: Task, worker_id: str) -> None:
        try:
            start.wait(timeout=2)
            state.complete_task(
                task_id=task.task_id,
                worker_id=worker_id,
                result={"action": "finish", "result": {"text": worker_id}},
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=complete, args=(first_task, "worker-a"))
    second = threading.Thread(target=complete, args=(second_task, "worker-b"))
    first.start()
    second.start()
    start.wait(timeout=2)
    first.join(timeout=5)
    second.join(timeout=5)
    assert errors == []
    messages = ConversationStore(tmp_path / "data" / "conversations").load(parent)
    ids = [message.id for message in messages]
    assert ids.count("tail-a") == 1
    assert ids.count("tail-b") == 1
    parent_path = tmp_path / "data" / "conversations" / f"{parent}.jsonl"
    lines = parent_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_sqlite_claim_store_cross_instance_cas_fencing_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "data" / "branch_finalize_claims.sqlite3"
    first_store = BranchFinalizeClaimStore(path)
    second_store = BranchFinalizeClaimStore(path)
    start = threading.Barrier(3)
    claims = []

    def claim(store, run_id):
        start.wait(timeout=2)
        claims.append(store.claim(
            "shared-identity",
            owner_run_id=run_id,
            owner_pid=os.getpid(),
            lease_seconds=5.0,
        ))

    first = threading.Thread(target=claim, args=(first_store, "run-a"))
    second = threading.Thread(target=claim, args=(second_store, "run-b"))
    first.start()
    second.start()
    start.wait(timeout=2)
    first.join(timeout=3)
    second.join(timeout=3)
    assert sorted(claim.status for claim in claims) == ["deferred", "owned"]
    owner = next(claim for claim in claims if claim.status == "owned")
    deferred = next(claim for claim in claims if claim.status == "deferred")
    assert deferred.owner_id == owner.owner_id
    assert deferred.owner_run_id == owner.owner_run_id

    takeover = second_store.claim(
        "shared-identity",
        owner_run_id="run-c",
        owner_pid=os.getpid(),
        lease_seconds=5.0,
        now=owner.lease_expires_at + 1.0,
    )
    assert takeover.status == "owned"
    assert takeover.fencing_generation == owner.fencing_generation + 1
    with pytest.raises(RuntimeError, match="fence lost"):
        first_store.fenced_action(owner, lambda: None)
    side_effects = []
    second_store.fenced_action(takeover, lambda: side_effects.append("routed"))
    assert side_effects == ["routed"]

    restarted = BranchFinalizeClaimStore(path)
    persisted = restarted.get("shared-identity")
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.fencing_generation == takeover.fencing_generation


def test_sqlite_claim_store_cross_process_cas_and_pid_death_takeover(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "branch_finalize_claims.sqlite3"
    start_path = tmp_path / "claim-start"
    release_path = tmp_path / "claim-release"
    script = """
import json
import os
import sys
import time
from pathlib import Path
from supervisor.branch_finalize_store import BranchFinalizeClaimStore

db_path, start_path, release_path, result_path, run_id = map(Path, sys.argv[1:6])
while not start_path.exists():
    time.sleep(0.005)
claim = BranchFinalizeClaimStore(db_path).claim(
    "cross-process-identity",
    owner_run_id=str(run_id),
    owner_pid=os.getpid(),
    lease_seconds=30.0,
)
result_path.write_text(json.dumps(claim.__dict__), encoding="utf-8")
while not release_path.exists():
    time.sleep(0.005)
"""
    result_paths = [tmp_path / "claim-a.json", tmp_path / "claim-b.json"]
    processes = [
        subprocess.Popen(
            [
                sys.executable, "-c", script, str(db_path), str(start_path),
                str(release_path), str(result_path), run_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
        )
        for result_path, run_id in zip(result_paths, ("process-a", "process-b"), strict=True)
    ]
    start_path.touch()
    deadline = time.monotonic() + 10.0
    while not all(path.exists() for path in result_paths):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    claims = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    assert sorted(claim["status"] for claim in claims) == ["deferred", "owned"]
    release_path.touch()
    for process in processes:
        assert process.wait(timeout=10) == 0
    owner = next(claim for claim in claims if claim["status"] == "owned")
    deferred = next(claim for claim in claims if claim["status"] == "deferred")
    assert deferred["owner_id"] == owner["owner_id"]

    # Both child processes have exited. A live lease from a confirmed-dead PID
    # must not block immediate takeover, and the old fence must stay invalid.
    store = BranchFinalizeClaimStore(db_path)
    takeover = store.claim(
        "cross-process-identity",
        owner_run_id="parent-takeover",
        owner_pid=os.getpid(),
        lease_seconds=5.0,
    )
    assert takeover.status == "owned"
    assert takeover.fencing_generation == int(owner["fencing_generation"]) + 1
    old_claim = store.get("cross-process-identity")
    assert old_claim is not None
    stale = old_claim.__class__(**{
        **old_claim.__dict__,
        "owner_run_id": str(owner["owner_run_id"]),
        "owner_id": str(owner["owner_id"]),
        "owner_pid": int(owner["owner_pid"]),
        "token": str(owner["token"]),
        "fencing_generation": int(owner["fencing_generation"]),
    })
    with pytest.raises(RuntimeError, match="fence lost"):
        store.fenced_action(stale, lambda: None)


def test_two_supervisor_states_compete_same_task_one_merge_and_outbound(tmp_path: Path) -> None:
    first = _make_state(tmp_path)
    parent = first.get_or_create_session(channel="test", conversation_key="test:two-supervisors")
    second = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="second-live-supervisor"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    task_a = _make_branch_task(first, parent, 931, "worker-shared")
    branch = task_a.session_id
    ConversationStore(tmp_path / "data" / "conversations").append(
        branch,
        Message(id="cross-state-tail", role="assistant", content="tail", message_type=MessageType.ASSISTANT, source_task_id=task_a.task_id),
    )
    with second._lock:
        second.sessions[parent] = first.sessions[parent]
        second.sessions[branch] = first.sessions[branch]
        second.session_generations[parent] = 1
        second.session_generations[branch] = 1
        second.entry_branch_parents[branch] = parent
        second.parent_entry_branches.setdefault(parent, set()).add(branch)
        second.parent_children.setdefault(parent, set()).add(branch)
        second._session_store.load()
        task_b = task_a.model_copy(deep=True)
        second.tasks[task_b.task_id] = task_b
        second._task_order.append(task_b.task_id)
    start = threading.Barrier(3)
    errors: list[Exception] = []

    def complete(state):
        try:
            start.wait(timeout=2)
            state.complete_task(
                task_id=task_a.task_id,
                worker_id="worker-shared",
                result={"action": "finish", "result": {"text": "one outbound"}},
            )
        except Exception as exc:
            errors.append(exc)

    thread_a = threading.Thread(target=complete, args=(first,))
    thread_b = threading.Thread(target=complete, args=(second,))
    thread_a.start()
    thread_b.start()
    start.wait(timeout=2)
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert errors == []
    parent_messages = ConversationStore(tmp_path / "data" / "conversations").load(parent)
    assert [message.id for message in parent_messages].count("cross-state-tail") == 1
    delivery_id = f"task:{task_a.task_id}:completion:1:v1"
    persisted_outbounds = [
        event for event in first.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
        and str((event.get("payload") or {}).get("delivery_id") or "") == delivery_id
    ]
    assert len(persisted_outbounds) == 1

class _FakeWinFunction:
    def __init__(self, result):
        self.result = result
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


class _FakeKernel32:
    def __init__(self, *, open_result=0, wait_result=0x102, exit_result=1):
        self.OpenProcess = _FakeWinFunction(open_result)
        self.WaitForSingleObject = _FakeWinFunction(wait_result)
        self.GetExitCodeProcess = _FakeWinFunction(exit_result)
        self.CloseHandle = _FakeWinFunction(1)


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [(87, False), (5, True), (1234, None)],
)
def test_branch_store_windows_open_process_errors_are_conservative(
    monkeypatch: pytest.MonkeyPatch, last_error: int, expected: bool | None,
) -> None:
    import ctypes

    kernel32 = _FakeKernel32(open_result=0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    assert branch_store_module._windows_pid_alive(12345) is expected
    assert kernel32.CloseHandle.calls == []
    assert kernel32.OpenProcess.argtypes is not None
    assert kernel32.OpenProcess.restype is not None


@pytest.mark.parametrize(
    ("wait_result", "exit_code", "exit_success", "expected"),
    [(0x102, 259, True, True), (0, 0, True, False), (0, 259, True, True), (0, 0, False, None)],
)
def test_branch_store_windows_wait_and_exit_code_are_conservative(
    monkeypatch: pytest.MonkeyPatch,
    wait_result: int,
    exit_code: int,
    exit_success: bool,
    expected: bool | None,
) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _FakeKernel32(open_result=123, wait_result=wait_result)

    def get_exit_code(handle, output):
        if exit_success:
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = exit_code
            return 1
        return 0

    kernel32.GetExitCodeProcess = _FakeWinFunction(get_exit_code)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32, raising=False)
    assert branch_store_module._windows_pid_alive(12345) is expected
    assert len(kernel32.CloseHandle.calls) == 1
    assert kernel32.WaitForSingleObject.argtypes is not None
    assert kernel32.GetExitCodeProcess.argtypes is not None
    assert kernel32.CloseHandle.argtypes is not None


def test_branch_store_access_denied_does_not_take_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BranchFinalizeClaimStore(tmp_path / "claims.sqlite3")
    owner = store.claim(
        "access-denied-owner", owner_run_id="owner", owner_pid=424242,
        lease_seconds=30.0,
    )
    assert owner.status == "owned"
    monkeypatch.setattr(branch_store_module, "_windows_pid_alive", lambda pid: True)
    contender = store.claim(
        "access-denied-owner", owner_run_id="contender", owner_pid=os.getpid(),
        lease_seconds=30.0,
    )
    assert contender.status == "deferred"
    assert contender.owner_id == owner.owner_id
    assert contender.fencing_generation == owner.fencing_generation


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle semantics")
def test_branch_store_windows_pid_probe_preserves_current_and_child_process() -> None:
    assert BranchFinalizeClaimStore._pid_alive(os.getpid()) is True
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert BranchFinalizeClaimStore._pid_alive(child.pid) is True
        time.sleep(0.05)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert BranchFinalizeClaimStore._pid_alive(child.pid) is False
