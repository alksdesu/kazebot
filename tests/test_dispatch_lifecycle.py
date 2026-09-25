from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.conversation_store import ConversationStore, Message
from engine.inference import pseudo_handlers
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import RouteStatus, Task, TaskStatus


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="dispatch-lifecycle"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _inbound(state: SupervisorState, session_id: str, **extra) -> Task:
    payload = {
        "channel": "qq_private",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "request",
        **extra,
    }
    event = state.eventlog.append(
        session_id=session_id, component="shell", type_="inbound_message", payload=payload,
    )
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _parent(state: SupervisorState, key: str = "qq_private:123") -> str:
    parent = state.get_or_create_session(channel="qq_private", conversation_key=key)
    task = _inbound(state, parent)
    _finish(state, task)
    return parent


def _dispatch(
    state: SupervisorState, parent: str, *, mode: str = "fresh", generation: int | None = None,
) -> tuple[str, Task]:
    key = state.sessions[parent].conversation_key
    child = state.get_or_create_session(
        channel="qq_private", conversation_key=f"agent:child:{key}:{mode}",
    )
    origin = {
        "parent_session_id": parent, "caller_node_id": "main",
        "parent_conversation_key": key, "context_mode": mode,
    }
    if generation is not None:
        origin["parent_session_generation"] = generation
    task = _inbound(
        state, child, entry_node_id="child", dispatch_context_mode=mode, dispatch_origin=origin,
    )
    return child, task


def _finish(state: SupervisorState, task: Task) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker"
    state.complete_task(
        task_id=task.task_id, worker_id="worker",
        result={"action": "finish", "result": {"text": "done"}},
    )


def _callbacks(state: SupervisorState) -> list[Task]:
    return [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]


@pytest.mark.parametrize("mode", ["fresh", "fork", "accumulate"])
def test_new_dispatch_after_cancel_uses_parent_generation(state: SupervisorState, mode: str) -> None:
    parent = _parent(state)
    assert state.cancel_session(parent)
    _inbound(state, parent)
    _, task = _dispatch(state, parent, mode=mode)

    assert state.session_generations[parent] == 2
    assert task.session_generation == 1
    assert task.input["_dispatch_origin"]["parent_session_generation"] == 2
    _finish(state, task)

    assert len(_callbacks(state)) == 1
    assert _callbacks(state)[0].input["parent_session_id"] == parent


def test_dispatch_retains_generation_from_before_cancel(state: SupervisorState) -> None:
    parent = _parent(state)
    state.cancel_session(parent)
    _inbound(state, parent)
    _, task = _dispatch(state, parent, generation=1)

    _finish(state, task)

    assert task.input["_dispatch_origin"]["parent_session_generation"] == 1
    assert _callbacks(state) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_legacy_task_retains_generation_guard(state: SupervisorState, cancelled: bool) -> None:
    parent = _parent(state)
    _, task = _dispatch(state, parent)
    task.input["_dispatch_origin"].pop("parent_session_generation")
    if cancelled:
        state.cancel_session(parent)
        _inbound(state, parent)
    _finish(state, task)

    assert len(_callbacks(state)) == (0 if cancelled else 1)


def test_dispatch_sender_includes_parent_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(status_code=200, json=lambda: {"session_id": "child"})
    http = SimpleNamespace(post=AsyncMock(return_value=response))
    loop = SimpleNamespace(
        allowed_dispatch_targets={"child"}, node=SimpleNamespace(id="main"),
        rctx=SimpleNamespace(
            parent_session_id="parent", session_id="branch", session_generation=4,
            workspace_root=tmp_path, supervisor_url="http://unused", http=http,
            task_context={"conversation_key": "qq_private:123", "channel": "qq_private"},
        ),
    )
    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", lambda *args: None)

    asyncio.run(pseudo_handlers._handle_pseudo_dispatch(
        loop, {"target": "child", "instruction": "work", "context_mode": "fresh"}, None,
    ))

    origin = http.post.call_args.kwargs["json"]["dispatch_origin"]
    assert origin["parent_session_id"] == "parent"
    assert origin["parent_session_generation"] == 4


def test_reset_cancels_nested_dispatch_and_keeps_other_conversations(state: SupervisorState) -> None:
    parent = _parent(state)
    child, child_task = _dispatch(state, parent)
    nested, nested_task = _dispatch(state, child)
    child_task.status = TaskStatus.running
    child_task.worker_id = "worker"
    nested_task.status = TaskStatus.suspended
    nested_task.waiting_for_task_id = "pending-tool"
    unrelated = _parent(state, "qq_private:1234")
    unrelated_child, unrelated_task = _dispatch(state, unrelated)
    removed = state._session_tree_locked(parent, "qq_private:123")
    store = ConversationStore(state.workspace_root / "data/conversations")
    for sid in removed:
        store.append(sid, Message(id=sid, role="user", content="history"))

    state.reset_conversation(conversation_key="qq_private:123")

    for task in (child_task, nested_task):
        assert task.status == TaskStatus.cancelled
        assert task.cancel_requested
        assert task.route_status == RouteStatus.routed
        store.append(task.session_id, Message(id="late", role="assistant", content="late shadow write"))
        state.complete_task(
            task_id=task.task_id, worker_id="worker",
            result={"action": "finish", "result": {"text": "late result"}},
        )
    assert _callbacks(state) == []
    assert not (removed & state.sessions.keys())
    assert not (removed & state.entry_branch_parents.keys())
    assert all(not (removed & children) for children in state.parent_children.values())
    assert all(store.load(sid) == [] for sid in removed)
    assert state._session_store.get_entry(parent)["reset"]
    assert state._session_store.get_entry(child) is None
    assert state._session_store.get_entry(nested) is None
    assert unrelated in state.sessions and unrelated_child in state.sessions
    assert unrelated_task.status == TaskStatus.pending
    fresh = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:123")
    assert fresh != parent
    assert not state.is_cancelled(fresh)
    fresh_task = _inbound(state, fresh)
    assert fresh_task.status == TaskStatus.pending


def test_finalize_prepared_before_reset_does_not_restore_deleted_history(state: SupervisorState) -> None:
    parent = _parent(state)
    task = _inbound(state, parent)
    store = ConversationStore(state.workspace_root / "data/conversations")
    store.append(task.session_id, Message(id="tail", role="assistant", content="old reply"))
    plan = state._prepare_branch_finalize_locked(task, merge=True)
    assert plan is not None

    state.reset_conversation(conversation_key="qq_private:123")
    state._execute_branch_finalize_io(plan)

    assert store.load(parent) == []
    assert store.load(task.session_id) == []


def test_deleting_old_session_does_not_clear_current_session_with_same_key(state: SupervisorState) -> None:
    old = _parent(state)
    old_child, old_task = _dispatch(state, old)
    state.conversation_map.pop("qq_private:123")
    current = _parent(state)
    current_child, current_task = _dispatch(state, current, mode="accumulate")
    store = ConversationStore(state.workspace_root / "data/conversations")
    store.append(current, Message(id="current", role="user", content="keep"))
    store.append(current_child, Message(id="child", role="user", content="keep"))

    result = state.reset_session(session_id=old)

    assert result["ok"]
    assert result["old_session_id"] == old
    assert old not in state.sessions and old_child not in state.sessions
    assert old_task.status == TaskStatus.cancelled
    assert state.conversation_map["qq_private:123"] == current
    assert current in state.sessions and current_child in state.sessions
    assert current_task.status == TaskStatus.pending
    assert store.load(current)[0].id == "current"
    assert store.load(current_child)[0].id == "child"


def test_reset_waits_for_inflight_merge_without_restoring_old_history(
    state: SupervisorState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent(state)
    task = _inbound(state, parent)
    store = ConversationStore(state.workspace_root / "data/conversations")
    store.append(task.session_id, Message(id="tail", role="assistant", content="old reply"))
    merging = threading.Event()
    resetting = threading.Event()
    release = threading.Event()
    original_merge = state._execute_branch_finalize_io_body
    original_reset = state._session_store.on_session_reset

    def paused_merge(plan):
        merging.set()
        assert release.wait(5)
        return original_merge(plan)

    def observe_reset(session_id):
        original_reset(session_id)
        resetting.set()

    monkeypatch.setattr(state, "_execute_branch_finalize_io_body", paused_merge)
    monkeypatch.setattr(state._session_store, "on_session_reset", observe_reset)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(_finish, state, task)
        assert merging.wait(5)
        reset = pool.submit(state.reset_session, session_id=parent)
        try:
            assert resetting.wait(5)
        finally:
            release.set()
        assert reset.result(timeout=5)["ok"]
        completion.result(timeout=5)

    assert task.status == TaskStatus.cancelled
    assert store.load(parent) == []
    assert store.load(task.session_id) == []
    assert not [
        event for event in state.eventlog.events
        if event.get("type") == "outbound_message"
        and (event.get("payload") or {}).get("source_inbound_seq") == task.source_inbound_seq
    ]


@pytest.mark.parametrize("always_conflict", [False, True])
def test_compact_retries_concurrent_writes_without_failing_the_caller(
    state: SupervisorState, monkeypatch: pytest.MonkeyPatch, always_conflict: bool,
) -> None:
    parent = _parent(state)
    store = ConversationStore(state.workspace_root / "data/conversations")
    store.append_batch(parent, [
        Message(id=f"m{index}", role="user", content="history", source_task_id=f"t{index}")
        for index in range(6)
    ])
    original_replace = ConversationStore.replace_all
    attempts = 0

    def concurrent_replace(writer: ConversationStore, session_id: str, messages) -> None:
        nonlocal attempts
        if session_id == parent:
            attempts += 1
            if attempts == 1 or always_conflict:
                ConversationStore(writer._data_dir).append(
                    parent, Message(id=f"new{attempts}", role="user", content="new request", source_task_id="new"),
                )
        original_replace(writer, session_id, messages)

    monkeypatch.setattr(ConversationStore, "replace_all", concurrent_replace)
    result = state._apply_compact_via_conv_store_locked(parent, "summary", keep_recent=2)

    messages = ConversationStore(store._data_dir).load(parent)
    assert messages[-1].id == f"new{3 if always_conflict else 1}"
    if always_conflict:
        assert attempts == 3
        assert result["before"] == result["after"] == 9
    else:
        assert attempts == 2
        assert result["after"] < result["before"]
