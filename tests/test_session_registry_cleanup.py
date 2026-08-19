"""Regression tests for physical cleanup of transient session registry entries.

[AutoC 2026-05-30] These tests are written before the implementation because the
reported production failure is persistent-file growth: branch and fresh/fork
child sessions were only marked reset and remained in data/sessions.json.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import timedelta
from pathlib import Path

# Why: the test checkout is executed directly, not installed as a package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules and verify the real persistence code path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import _now  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.session_store import SessionStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import TaskKind, TaskStatus  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    """Create a SupervisorState with only temporary persistence files."""
    # [AutoC 2026-05-30] Why: cleanup behavior must be checked through the same
    # SessionStore path used in production. How: build a full SupervisorState on a
    # pytest tmp_path. Purpose: avoid touching live sessions.json while covering
    # registry, in-memory indexes, and JSONL cleanup together.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-session-cleanup")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _registry(workspace: Path) -> dict[str, dict]:
    """Read the temporary sessions.json registry."""
    # [AutoC 2026-05-30] Why: the bug is physical records left in sessions.json.
    # How: inspect the file on disk instead of only SupervisorState memory.
    # Purpose: assertions fail if cleanup merely marks reset=true.
    path = workspace / "data" / "sessions.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_session_store_remove_session_physically_deletes_registry_entry(tmp_path: Path) -> None:
    """remove_session should pop the entry rather than mark it reset."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:remove-session")
    child_sid, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")

    state._session_store.remove_session(child_sid)

    # [AutoC 2026-05-30] Why: branch and child cleanup must shrink sessions.json.
    # How: assert the registry key is gone from both memory and disk. Purpose:
    # prevent a regression to reset-marker-only cleanup.
    assert state._session_store.get_entry(child_sid) is None
    assert child_sid not in _registry(tmp_path)


def test_cleanup_branch_physically_removes_branch_and_derived_child_entries(tmp_path: Path) -> None:
    """Branch merge cleanup should remove branch and branch-owned child records."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:branch-cleanup")

    with state._lock:
        branch_sid, _ = state._create_entry_branch_locked(parent, inbound_seq=31)
        derived_sid, _ = state.get_or_create_child_session(branch_sid, "child.node", "case", "fresh")
        (tmp_path / "data" / "conversations").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "conversations" / f"{derived_sid}.jsonl").write_text("", encoding="utf-8")
        state._cleanup_branch_locked(branch_sid)

    registry = _registry(tmp_path)
    # [AutoC 2026-05-30] Why: completed branches have no restart value after
    # merge. How: assert both branch and derived child registry rows are absent.
    # Purpose: sessions.json cannot keep growing with branch_* and child_* rows.
    assert branch_sid not in registry
    assert derived_sid not in registry
    assert branch_sid not in state.sessions
    assert derived_sid not in state.sessions


def _seed_stale_registry(state: SupervisorState, parent: str) -> tuple[str, str, str, str]:
    """Create stale transient session records for cleanup tests."""
    # [AutoC 2026-05-30] Why: startup reconcile and periodic sweep must follow the
    # same retention rule. How: share one fixture that creates reset, orphan branch,
    # old fresh/fork, and old accumulate records. Purpose: both paths are checked
    # against identical persistent state.
    fresh_sid, _ = state.get_or_create_child_session(parent, "fresh.node", "case", "fresh")
    fork_sid, _ = state.get_or_create_child_session(parent, "fork.node", "case", "fork")
    accumulate_sid, _ = state.get_or_create_child_session(parent, "acc.node", "case", "accumulate")

    old_ts = (_now() - timedelta(hours=25)).isoformat()
    state._session_store.set_entry_field(fresh_sid, "last_active_at", old_ts, flush=False)
    state._session_store.set_entry_field(fork_sid, "last_active_at", old_ts, flush=False)
    state._session_store.set_entry_field(accumulate_sid, "last_active_at", old_ts, flush=False)
    state._session_store.upsert_entry("branch_orphan", {
        "session_id": "branch_orphan",
        "channel": "internal",
        "conversation_key": "",
        "created_at": old_ts,
        "reset": False,
        "is_child": True,
        "parent_session_id": parent,
        "node_id": "__entry_branch__",
        "context_key": "orphan",
        "context_mode": "branch",
        "last_active_at": old_ts,
    }, flush=False)
    state._session_store.upsert_entry("reset_old", {
        "session_id": "reset_old",
        "channel": "internal",
        "conversation_key": "",
        "created_at": old_ts,
        "reset": True,
    }, flush=False)
    state.parent_children.setdefault(parent, set()).update({"branch_orphan"})
    state._session_store._flush()
    return fresh_sid, fork_sid, accumulate_sid, "branch_orphan"


def test_cleanup_stale_sessions_removes_reset_branch_and_old_fresh_fork_but_keeps_accumulate(tmp_path: Path) -> None:
    """The periodic sweep should delete stale transient sessions and keep accumulate children."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:stale-cleanup")
    fresh_sid, fork_sid, accumulate_sid, branch_sid = _seed_stale_registry(state, parent)

    with state._lock:
        state._cleanup_stale_sessions_locked()

    registry = _registry(tmp_path)
    # [AutoC 2026-05-30] Why: reset=true rows are historical debris and fresh/fork
    # children older than 24 hours are disposable. How: verify each such key is
    # physically absent. Purpose: the background sweep bounds sessions.json size.
    assert "reset_old" not in registry
    assert branch_sid not in registry
    assert fresh_sid not in registry
    assert fork_sid not in registry
    assert accumulate_sid in registry
    assert registry[accumulate_sid]["context_mode"] == "accumulate"


def test_startup_reconcile_replaces_eventlog_replay_for_transient_session_cleanup(tmp_path: Path) -> None:
    """Supervisor startup should clean transient state from sessions.json without EventLog replay."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:startup-reconcile")
    session_from_event_only = "event-only-session"
    state.eventlog.append(
        session_id=session_from_event_only,
        component="test",
        type_="session_created",
        payload={"channel": "test", "conversation_key": "test:event-only"},
    )
    fresh_sid, fork_sid, accumulate_sid, branch_sid = _seed_stale_registry(state, parent)
    (tmp_path / "data" / "conversations").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "transcripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "conversations" / f"{branch_sid}.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "data" / "transcripts" / f"{branch_sid}.jsonl").write_text("", encoding="utf-8")

    restarted = _make_state(tmp_path)
    registry = _registry(tmp_path)

    # [AutoC 2026-05-30] Why: EventLog is now audit-only and must not restore
    # sessions that exist only in events.jsonl. How: restart from the same
    # sessions.json after writing an event-only session_created row. Purpose: prove
    # startup state is sourced from sessions.json plus reconcile, not event replay.
    assert session_from_event_only not in restarted.sessions
    assert session_from_event_only not in restarted.conversation_map.values()
    assert "reset_old" not in registry
    assert branch_sid not in registry
    assert fresh_sid not in registry
    assert fork_sid not in registry
    assert accumulate_sid in registry
    assert not (tmp_path / "data" / "conversations" / f"{branch_sid}.jsonl").exists()
    assert not (tmp_path / "data" / "transcripts" / f"{branch_sid}.jsonl").exists()


def test_route_completed_ask_outputs_to_user_and_marks_action_type(tmp_path: Path) -> None:
    """Phase 0 ask should route like finish while preserving action_type metadata."""
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:ask-route")

    with state._lock:
        task = state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="ask.node",
            input_data={},
            continuation={},
            source_inbound_seq=None,
        )
        task.status = TaskStatus.completed
        task.result = {"action": "ask", "result": {"summary": "need input", "text": "Which branch?"}}

        state._route_completed_task_locked(task)

    # [AutoC 2026-05-31] Why: Phase 0 has no topology router, so ask must not
    # error and should use the same user-visible path as finish. How: inspect the
    # outbound event payload. Purpose: keep future Phase 1 routing able to
    # distinguish ask through action_type while preserving current behavior.
    events_path = tmp_path / "data" / "events.jsonl"
    outbound_events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "outbound_message"
    ]
    assert outbound_events[-1]["payload"]["text"] == "Which branch?"
    assert outbound_events[-1]["payload"]["action_type"] == "ask"



def test_route_completed_batch_task_cleans_fresh_child_session_from_finally(tmp_path: Path) -> None:
    """A terminal batch task should clean its non-accumulate child session after routing."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:batch-child-cleanup")
    child_sid, _ = state.get_or_create_child_session(parent, "batch.node", "case", "fresh")
    conv_path = tmp_path / "data" / "conversations" / f"{child_sid}.jsonl"
    conv_path.parent.mkdir(parents=True, exist_ok=True)
    conv_path.write_text("", encoding="utf-8")

    with state._lock:
        task = state._create_task_locked(
            session_id=parent,
            session_generation=1,
            kind=TaskKind.node,
            node_id="batch.node",
            input_data={
                # [AutoC 2026-05-30] Why: this test covers a route that returns
                # before the old scattered cleanup sites. How: attach a fresh
                # child session to a completed batch child. Purpose: prove the new
                # route-level finally cleanup catches early returns.
                "child_session_id": child_sid,
                "context_mode": "fresh",
            },
            continuation={},
            batch_id="batch-cleanup",
            batch_index=0,
        )
        task.status = TaskStatus.completed
        task.result = {"action": "finish", "result": {"summary": "done", "text": "done"}}
        state._reset_task_route_state_locked(task)
        state._route_completed_task_locked(task)

    state._post_completion_queue.join()
    registry = _registry(tmp_path)
    assert child_sid not in registry
    assert child_sid not in state.sessions
    assert not conv_path.exists()


def test_route_completed_dispatch_keeps_child_session_while_task_is_suspended(tmp_path: Path) -> None:
    """A dispatch action should not clean the caller child session while the task is suspended."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:dispatch-keeps-child")
    child_sid, _ = state.get_or_create_child_session(parent, "caller.node", "case", "fresh")

    with state._lock:
        task = state._create_task_locked(
            session_id=parent,
            session_generation=1,
            kind=TaskKind.node,
            node_id="caller.node",
            input_data={
                # [AutoC 2026-05-30] Why: route-level finally runs after every
                # action, including dispatch. How: give the caller a fresh child
                # session and make it dispatch a subtask. Purpose: verify the
                # helper checks terminal status before deleting reusable state.
                "child_session_id": child_sid,
                "context_mode": "fresh",
            },
            continuation={},
        )
        task.status = TaskStatus.completed
        task.result = {
            "action": "dispatch",
            "target_node": "worker.node",
            "dispatch_input": {"instruction": "work", "context_mode": "fresh"},
        }

        state._route_completed_task_locked(task)

    registry = _registry(tmp_path)
    assert task.status == TaskStatus.suspended
    assert child_sid in registry
    assert child_sid in state.child_session_map.values()
    assert child_sid in state.parent_children[parent]


def test_route_cleanup_prefers_registry_context_mode_over_task_input(tmp_path: Path) -> None:
    """The cleanup helper should keep accumulate children according to the registry."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:registry-mode")
    child_sid, _ = state.get_or_create_child_session(parent, "acc.node", "case", "accumulate")

    with state._lock:
        task = state._create_task_locked(
            session_id=parent,
            session_generation=1,
            kind=TaskKind.node,
            node_id="acc.node",
            input_data={
                # [AutoC 2026-05-30] Why: task.input can be stale or incomplete,
                # while sessions.json is the durable source of the child mode. How:
                # intentionally put a conflicting fresh mode in input. Purpose:
                # ensure accumulate child sessions are not deleted by mistake.
                "child_session_id": child_sid,
                "context_mode": "fresh",
            },
            continuation={},
            batch_id="batch-accumulate",
            batch_index=0,
        )
        task.status = TaskStatus.completed
        task.result = {"action": "finish", "result": {"summary": "done", "text": "done"}}

        state._route_completed_task_locked(task)

    registry = _registry(tmp_path)
    assert child_sid in registry
    assert registry[child_sid]["context_mode"] == "accumulate"


def test_session_store_out_of_order_snapshot_never_overwrites_newer_generation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    old_snapshot = store.upsert_entry("old", {"session_id": "old", "reset": False}, flush=False)
    new_snapshot = store.upsert_entry("new", {"session_id": "new", "reset": False}, flush=False)
    assert store.flush_snapshot(new_snapshot)
    assert store.flush_snapshot(old_snapshot)
    persisted = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert set(persisted) == {"old", "new"}


def test_session_store_concurrent_mutation_and_cleanup_flush_keeps_latest_registry(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    for index in range(25):
        store.upsert_entry(f"stale-{index}", {"session_id": f"stale-{index}", "reset": False}, flush=False)
    errors: list[Exception] = []
    start = threading.Barrier(3)

    def create_and_update() -> None:
        try:
            start.wait(timeout=2)
            for index in range(40):
                session_id = f"new-{index}"
                store.upsert_entry(session_id, {"session_id": session_id, "reset": False, "value": 0}, flush=True)
                store.set_entry_field(session_id, "value", 1, flush=True)
        except Exception as exc:
            errors.append(exc)

    def cleanup_and_flush() -> None:
        try:
            start.wait(timeout=2)
            for index in range(25):
                store.remove_sessions([f"stale-{index}"], flush=True)
        except Exception as exc:
            errors.append(exc)

    creators = threading.Thread(target=create_and_update)
    cleanup = threading.Thread(target=cleanup_and_flush)
    creators.start()
    cleanup.start()
    start.wait(timeout=2)
    creators.join(timeout=5)
    cleanup.join(timeout=5)
    assert not creators.is_alive()
    assert not cleanup.is_alive()
    assert errors == []
    assert store.flush_snapshot(store.snapshot())
    persisted = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    expected = dict(store.items_snapshot())
    assert persisted == expected
    assert all(persisted[f"new-{index}"]["value"] == 1 for index in range(40))
    assert not any(key.startswith("stale-") for key in persisted)
