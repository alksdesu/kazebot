from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from engine.context_archive import ContextArchiveStore
from engine.context_store import load_context_snapshot, save_context_snapshot, write_context_snapshot
from engine.conversation_store import ConversationStore, Message
from engine.task_record import TaskRecord, snip_history, write_task_record
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import TaskKind, TaskStatus


SUMMARY = "<summary>" + "Verified task history, decisions, constraints, and completed work. " * 8 + "</summary>"


@pytest.fixture
def scenario(tmp_path):
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="compact-coverage"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state._defer_post_work_enqueue = True
    session_id = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:coverage")
    messages = [
        Message(
            id=f"message-{index}", role="user", content=f"Original turn {index}: " + "detail " * 80,
            source_task_id=f"task-{index}", message_type="user_input",
            created_at=f"2026-10-20T10:0{index}:00+00:00",
            meta={"topic_id": "earlier" if index % 2 == 0 else "later", "custom": index},
        )
        for index in range(6)
    ]
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append_batch(session_id, messages)
    return state, session_id, store, messages


def caller_task(state, session_id, **inputs):
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=state._current_session_generation_locked(session_id) or 1,
            kind=TaskKind.node, node_id="qq.orchestrator",
            input_data={"route_hints": {"topic_id": "later"}, **inputs},
        )


def dispatch(state, session_id, caller=None, context_ref=""):
    caller = caller or caller_task(state, session_id)
    if context_ref:
        caller.result = {"context_ref": context_ref}
    with state._lock:
        state._route_dispatch_locked(caller, {
            "action": "dispatch", "target_node": "system.compactor", "context_ref": context_ref,
            "dispatch_input": {
                "_compact_dispatch": True, "target_session_id": session_id,
                "_compact_keep_recent": 2, "_compact_keep_recent_tokens": 0,
                "_compact_threshold_tokens": 100000, "context_mode": "fresh",
                "instruction": "INCOMPLETE_TOPIC_ONLY_DO_NOT_TRUST",
            },
        })
    child = state.tasks[caller.waiting_for_task_id]
    return caller, child


def background(state, session_id):
    app = create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(
            path=state.workspace_root / "config" / "models.yaml", workspace_root=state.workspace_root,
        ),
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            return await client.post("/v1/tasks/compact-async", json={
                "target_session_id": session_id, "caller_node_id": "qq.orchestrator",
                "instruction": "INCOMPLETE_TOPIC_ONLY_DO_NOT_TRUST",
                "keep_recent": 2, "threshold_tokens": 100000,
            })

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    assert response.json()["task_id"], response.text
    return None, state.tasks[response.json()["task_id"]]


def start(state, session_id, mode):
    return dispatch(state, session_id) if mode == "sync" else background(state, session_id)


def complete(state, task, *, action="finish", text=SUMMARY):
    task.status = TaskStatus.running
    task.worker_id = "compact-test-worker"
    state.complete_task(
        task_id=task.task_id, worker_id=task.worker_id,
        result={"action": action, "result": {"text": text}},
    )


def record_list(store, session_id):
    return [message.to_dict() for message in store.load(session_id)]


def archive_records(state, session_id, query="Original turn 0"):
    archives = ContextArchiveStore(state.workspace_root)
    matches = archives.search(session_id, query)["matches"]
    assert matches
    return [
        item["message"] for item in
        archives.read(session_id, matches[0]["archive_id"], limit=50)["messages"]
    ]


@pytest.mark.parametrize("mode", ["sync", "background"])
def test_compactor_uses_complete_durable_coverage_and_preserves_appended_turns(scenario, mode):
    state, session_id, store, original = scenario
    caller, compactor = start(state, session_id, mode)
    instruction = compactor.input["instruction"]
    assert "INCOMPLETE_TOPIC_ONLY" not in instruction
    assert [json.loads(line)["id"] for line in instruction.splitlines()] == [message.id for message in original[:4]]
    assert '"earlier"' in instruction and '"later"' in instruction
    assert all(message.content not in instruction for message in original[4:])
    new = Message(id="arrived-during-compact", role="user", content="Keep newly arrived message", source_task_id="new-task")
    store.append(session_id, new)
    complete(state, compactor)
    result = store.load(session_id)
    assert result[0].message_type == "summary"
    assert [message.id for message in result[1:]] == [message.id for message in original[4:]] + [new.id]
    assert archive_records(state, session_id) == [message.to_dict() for message in [*original, new]]
    assert set(result[0].meta["compressed_task_ids"]) == {f"task-{index}" for index in range(4)}
    if caller is not None:
        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["success"] is True


@pytest.mark.parametrize("mode", ["sync", "background"])
@pytest.mark.parametrize("result", [
    ("fail", SUMMARY), ("finish", ""), ("finish", "<summary>too short</summary>"),
    ("finish", "unstructured response " * 30),
])
def test_failed_or_invalid_compactor_results_preserve_all_messages(scenario, mode, result):
    state, session_id, store, _ = scenario
    caller, compactor = start(state, session_id, mode)
    before = record_list(store, session_id)
    complete(state, compactor, action=result[0], text=result[1])
    assert record_list(store, session_id) == before
    assert ContextArchiveStore(state.workspace_root).search(session_id)["matches"] == []
    if caller is not None:
        assert caller.status == TaskStatus.pending and caller.input["resume_data"]["success"] is False


@pytest.mark.parametrize("mode", ["sync", "background"])
def test_archive_failure_aborts_compaction_without_deleting_history(scenario, monkeypatch, mode):
    state, session_id, store, _ = scenario
    caller, compactor = start(state, session_id, mode)
    before = record_list(store, session_id)

    def unavailable(*args, **kwargs):
        raise OSError("archive disk unavailable")

    monkeypatch.setattr(ContextArchiveStore, "archive", unavailable)
    complete(state, compactor)
    assert record_list(store, session_id) == before
    if caller is not None:
        assert caller.input["resume_data"]["success"] is False


@pytest.mark.parametrize("mode", ["sync", "background"])
def test_old_inflight_compaction_without_coverage_plan_is_a_noop(scenario, mode):
    state, session_id, store, _ = scenario
    caller, compactor = start(state, session_id, mode)
    (caller if caller is not None else compactor).input.pop("_compact_plan")
    before = record_list(store, session_id)
    complete(state, compactor)
    assert record_list(store, session_id) == before
    assert ContextArchiveStore(state.workspace_root).search(session_id)["matches"] == []
    if caller is not None:
        assert caller.status == TaskStatus.pending and caller.input["resume_data"]["success"] is False


@pytest.mark.parametrize("mode", ["sync", "background"])
def test_changed_covered_record_rejects_stale_summary(scenario, mode):
    state, session_id, store, _ = scenario
    caller, compactor = start(state, session_id, mode)
    changed = deepcopy(store.load(session_id))
    changed[0].content = "Edited while compactor was working"
    store.replace_all(session_id, changed)
    complete(state, compactor)
    assert record_list(store, session_id) == [message.to_dict() for message in changed]
    if caller is not None:
        assert caller.input["resume_data"]["success"] is False


@pytest.mark.parametrize("branch_matches", [True, False])
def test_entry_branch_sync_matches_records_updates_base_and_merges_tail_once(scenario, branch_matches):
    state, session_id, store, original = scenario
    with state._lock:
        branch_id, fork = state._create_entry_branch_locked(session_id, 999)
    branch_task = caller_task(
        state, branch_id, parent_session_id=session_id, branch_session_id=branch_id,
        base_count=fork["base_count"], base_last_id=fork["base_last_id"],
    )
    if not branch_matches:
        branch_messages = deepcopy(store.load(branch_id))
        branch_messages[0].id = "distinct-record-at-same-index"
        store.replace_all(branch_id, branch_messages)
    tail = Message(id="branch-tail", role="assistant", content="New branch work", source_task_id=branch_task.task_id)
    store.append(branch_id, tail)
    child_id, _ = state.get_or_create_child_session(session_id, "independent.node", "case", "accumulate")
    independent = Message(id="independent", role="user", content="Independent child work", source_task_id="independent-task")
    store.append(child_id, independent)
    caller_task(state, session_id, child_session_id=child_id, parent_session_id=session_id)
    _, compactor = dispatch(state, session_id, caller=branch_task)
    complete(state, compactor)
    parent = store.load(session_id)
    branch = store.load(branch_id)
    if branch_matches:
        assert branch_task.input["base_count"] == 3
        assert branch_task.input["base_last_id"] == original[-1].id
        assert [message.id for message in branch] == [message.id for message in parent] + [tail.id]
    else:
        assert branch_task.input["base_count"] == len(original)
        assert branch[0].id == "distinct-record-at-same-index"
    assert record_list(store, child_id) == [independent.to_dict()]
    with state._lock:
        finalize = state._prepare_branch_finalize_locked(branch_task, merge=True)
    assert finalize is not None
    assert state._execute_branch_finalize_io(finalize) == 1
    assert state._execute_branch_finalize_io(finalize) == 0
    ids = [message.id for message in store.load(session_id)]
    assert ids == [message.id for message in parent] + [tail.id]
    assert len(ids) == len(set(ids))
    assert record_list(store, child_id) == [independent.to_dict()]


@pytest.mark.parametrize("failure", ["none", "archive", "invalid_summary", "missing_plan"])
def test_legacy_snapshot_compaction_preserves_coverage_and_new_messages(scenario, monkeypatch, failure):
    state, session_id, store, original = scenario
    config = state.workspace_root / "config" / "runtime.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("engine:\n  child_session:\n    main_session_enabled: false\n", encoding="utf-8")
    history = [
        {"role": message.role, "content": message.content, "_meta": {
            **message.meta, "message_id": message.id, "source_task_id": message.source_task_id,
            "timestamp": message.created_at, "message_type": message.message_type,
        }}
        for message in original
    ]
    context_ref = save_context_snapshot(state.workspace_root, session_id, {"messages": history, "counter": 7})
    caller, compactor = dispatch(state, session_id, context_ref=context_ref)
    assert caller.input["_compact_plan"]["storage"] == "snapshot"
    assert [json.loads(line)["_meta"]["message_id"] for line in compactor.input["instruction"].splitlines()] == [message.id for message in original[:4]]
    snapshot = load_context_snapshot(state.workspace_root, context_ref)
    new = {"role": "user", "content": "Snapshot arrived after dispatch", "_meta": {"source_task_id": "new"}}
    snapshot["messages"].append(new)
    write_context_snapshot(state.workspace_root, context_ref, snapshot)
    before = deepcopy(snapshot)
    if failure == "archive":
        def unavailable(*args, **kwargs):
            raise OSError("archive unavailable")
        monkeypatch.setattr(ContextArchiveStore, "archive", unavailable)
    elif failure == "missing_plan":
        caller.input.pop("_compact_plan")
    complete(state, compactor, text="" if failure == "invalid_summary" else SUMMARY)
    after = load_context_snapshot(state.workspace_root, context_ref)
    if failure == "none":
        assert after["messages"][0]["_meta"]["message_type"] == "summary"
        assert after["messages"][1:] == [*history[4:], new]
        assert archive_records(state, session_id) == before["messages"]
    else:
        assert after == before
    assert after["counter"] == 7
    assert caller.status == TaskStatus.pending
    assert caller.input["resume_data"]["success"] is (failure == "none")
    assert record_list(store, session_id) == [message.to_dict() for message in original]


def snip_branch(scenario, *, metadata_only=False):
    state, session_id, store, original = scenario
    records = []
    expanded = []
    for message in original:
        message = deepcopy(message)
        task_id = message.source_task_id
        if metadata_only:
            message.meta["source_task_id"] = task_id
            message.source_task_id = ""
        expanded.extend([message, Message(
            id=f"{message.id}-answer", role="assistant", content=f"Answer to {task_id}",
            source_task_id=task_id, created_at=message.created_at,
        )])
        record = TaskRecord(task_id=task_id, session_id=session_id, node_id="test", summary=f"Task summary {task_id}")
        records.append(record)
        write_task_record(state.workspace_root, record)
    store.replace_all(session_id, expanded)
    with state._lock:
        branch_id, fork = state._create_entry_branch_locked(session_id, 888)
    task = caller_task(
        state, branch_id, parent_session_id=session_id, branch_session_id=branch_id,
        base_count=fork["base_count"], base_last_id=fork["base_last_id"],
    )
    tail = Message(id="snip-tail", role="user", content="Current user request", source_task_id=task.task_id)
    store.append(branch_id, tail)
    return state, session_id, store, expanded, branch_id, task, tail, records


@pytest.mark.parametrize("metadata_only", [False, True])
def test_worker_snip_uses_supervisor_and_cannot_restore_old_branch_prefix(scenario, metadata_only):
    from types import SimpleNamespace
    from engine.builtin.compact import _apply_snip
    from engine.runner import _message_to_history_dict

    state, session_id, store, original, branch_id, task, tail, records = snip_branch(
        scenario, metadata_only=metadata_only,
    )
    history = [_message_to_history_dict(message) for message in store.load(branch_id)]
    snipped, count, task_ids = snip_history(history, records)
    assert count == 2
    app = create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=state.workspace_root / "config" / "models.yaml", workspace_root=state.workspace_root),
    )
    child_id, _ = state.get_or_create_child_session(session_id, "independent", "snip", "accumulate")
    independent = Message(id="independent-snip", role="user", content="Separate task")
    store.append(child_id, independent)
    caller_task(state, session_id, parent_session_id=session_id, child_session_id=child_id)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            loop = SimpleNamespace(messages=history, rctx=SimpleNamespace(
                workspace_root=state.workspace_root, session_id=branch_id,
                parent_session_id=session_id, child_session_id="", supervisor_url="http://testserver",
                http=client, conversation_store=store,
            ))
            ctx = SimpleNamespace(messages=history)
            await _apply_snip(ctx, loop, session_id, records, snipped, task_ids, reason="proactive_snip")
            assert loop.messages is ctx.messages and loop.messages == snipped

    asyncio.run(run())
    parent = store.load(session_id)
    branch = store.load(branch_id)
    assert len(parent) == len(original) - 2
    assert [message.id for message in branch] == [message.id for message in parent] + [tail.id]
    assert task.input["base_count"] == len(parent)
    assert task.input["base_last_id"] == original[-1].id
    assert parent[0].message_type == "summary" and parent[1].message_type == "summary"
    assert not any(message.content == original[0].content for message in branch)
    assert record_list(store, child_id) == [independent.to_dict()]
    archived = archive_records(state, session_id)
    assert archived[:len(original)] == [message.to_dict() for message in original]
    with state._lock:
        plan = state._prepare_branch_finalize_locked(task, merge=True)
    assert state._execute_branch_finalize_io(plan) == 1
    assert [message.id for message in store.load(session_id)] == [message.id for message in parent] + [tail.id]


def test_snip_branch_fingerprint_mismatch_never_rewrites_distinct_records(scenario):
    state, session_id, store, _, branch_id, task, _, records = snip_branch(scenario)
    branch = deepcopy(store.load(branch_id))
    branch[0].content = "Branch changed this original record"
    store.replace_all(branch_id, branch)
    old_base = task.input["base_count"]
    with state._lock:
        result = state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id})
    assert result["applied"] is False
    assert record_list(store, branch_id) == [message.to_dict() for message in branch]
    assert task.input["base_count"] == old_base


@pytest.mark.parametrize("failure", ["archive", "branch_replace"])
def test_snip_failures_preserve_branch_prefix_and_base_count(scenario, monkeypatch, failure):
    state, session_id, store, original, branch_id, task, _, records = snip_branch(scenario)
    branch_before = record_list(store, branch_id)
    old_base = task.input["base_count"]
    if failure == "archive":
        def archive(*args, **kwargs):
            raise OSError("archive unavailable")
        monkeypatch.setattr(ContextArchiveStore, "archive", archive)
        with pytest.raises(OSError):
            state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id})
        assert record_list(store, session_id) == [message.to_dict() for message in original]
    else:
        replace_all = ConversationStore.replace_all

        def replace(writer, target, messages):
            if target == branch_id:
                raise OSError("branch replacement unavailable")
            return replace_all(writer, target, messages)

        monkeypatch.setattr(ConversationStore, "replace_all", replace)
        result = state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id})
        assert result["applied"] is False
    assert record_list(store, branch_id) == branch_before
    assert task.input["base_count"] == old_base


def test_snip_retries_parent_concurrent_append_without_losing_new_message(scenario, monkeypatch):
    state, session_id, store, _, branch_id, _, tail, records = snip_branch(scenario)
    original_replace = ConversationStore.replace_all
    late = Message(id="snip-concurrent", role="user", content="Arrived during snip")
    injected = False

    def replace(writer, target, messages):
        nonlocal injected
        if target == session_id and not injected:
            injected = True
            ConversationStore(state.workspace_root / "data/conversations").append(session_id, late)
        return original_replace(writer, target, messages)

    monkeypatch.setattr(ConversationStore, "replace_all", replace)
    result = state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id})
    assert result["applied"] is True
    assert store.load(session_id)[-1].id == late.id
    assert store.load(branch_id)[-1].id == tail.id


@pytest.mark.parametrize("response_status,applied", [(500, False), (200, False)])
def test_worker_retains_runtime_history_when_supervisor_snip_does_not_apply(tmp_path, response_status, applied):
    from types import SimpleNamespace
    from engine.builtin.compact import _apply_snip

    original = [{"role": "user", "content": "untouched"}]
    request = httpx.Request("POST", "http://testserver/v1/tasks/compact-snip")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda incoming: httpx.Response(response_status, request=request, json={"applied": applied}),
        )) as client:
            loop = SimpleNamespace(messages=original, rctx=SimpleNamespace(
                workspace_root=tmp_path, session_id="branch", parent_session_id="parent",
                child_session_id="", supervisor_url="http://testserver", http=client,
            ))
            ctx = SimpleNamespace(messages=original)
            with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
                await _apply_snip(ctx, loop, "parent", [], [], {"task"}, reason="threshold_snip")
            assert loop.messages is original and ctx.messages is original

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["snip", "llm"])
def test_compacted_branch_base_metadata_survives_task_event_replay(scenario, mode):
    state, session_id, _, _, _, task, _, records = snip_branch(scenario)
    if mode == "snip":
        state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id, records[1].task_id})
    else:
        _, compactor = background(state, session_id)
        complete(state, compactor)
    expected = task.input["base_count"]
    restored = SupervisorState(
        workspace_root=state.workspace_root,
        eventlog=EventLog(state.workspace_root / "data" / "events.jsonl", run_id="compact-restart"),
        policy=PolicyEngine(workspace_root=state.workspace_root),
    )
    restored.rebuild_from_events(list(state.eventlog.iter_persisted_events()))
    assert restored.tasks[task.task_id].input["base_count"] == expected
    assert restored.tasks[task.task_id].input["base_last_id"] == task.input["base_last_id"]


@pytest.mark.parametrize("always_conflict", [False, True])
def test_snip_branch_retries_concurrent_stream_append_without_deleting_tail(scenario, monkeypatch, always_conflict):
    state, session_id, store, _, branch_id, task, tail, records = snip_branch(scenario)
    original_replace = ConversationStore.replace_all
    attempts = 0
    old_base = task.input["base_count"]

    def replace(writer, target, messages):
        nonlocal attempts
        if target == branch_id:
            attempts += 1
            if always_conflict or attempts == 1:
                ConversationStore(state.workspace_root / "data/conversations").append(
                    branch_id, Message(id=f"stream-{attempts}", role="assistant", content="New streamed result"),
                )
        return original_replace(writer, target, messages)

    monkeypatch.setattr(ConversationStore, "replace_all", replace)
    result = state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id})
    assert result["applied"] is not always_conflict
    assert attempts == (3 if always_conflict else 2)
    ids = [message.id for message in store.load(branch_id)]
    assert tail.id in ids and "stream-1" in ids
    if always_conflict:
        assert task.input["base_count"] == old_base
        assert ids[-3:] == ["stream-1", "stream-2", "stream-3"]
    else:
        assert task.input["base_count"] == old_base - 1


def test_snip_never_replaces_tasks_without_a_nonempty_record_summary(scenario):
    state, session_id, store, original = scenario
    for task_id, summary in [(original[0].source_task_id, ""), (original[1].source_task_id, "   ")]:
        write_task_record(state.workspace_root, TaskRecord(
            task_id=task_id, session_id=session_id, node_id="test", summary=summary,
        ))
    result = state._apply_snip_via_conv_store_locked(
        session_id, {original[0].source_task_id, original[1].source_task_id, "missing-task"},
    )
    assert result["applied"] is False
    assert record_list(store, session_id) == [message.to_dict() for message in original]


def test_snip_api_rejects_invalid_targets_and_task_identifiers(scenario):
    state, session_id, store, original = scenario
    app = create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=state.workspace_root / "config" / "models.yaml", workspace_root=state.workspace_root),
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            for body, expected in [
                ({}, 400), ([], 400),
                ({"target_session_id": session_id, "task_ids": []}, 400),
                ({"target_session_id": session_id, "task_ids": [""]}, 400),
                ({"target_session_id": session_id, "task_ids": [1]}, 400),
                ({"target_session_id": session_id, "task_ids": "task"}, 400),
                ({"target_session_id": "../unknown", "task_ids": ["task"]}, 404),
            ]:
                response = await client.post("/v1/tasks/compact-snip", json=body)
                assert response.status_code == expected, response.text

    asyncio.run(request())
    assert record_list(store, session_id) == [message.to_dict() for message in original]


def test_independent_child_snip_stays_local_and_does_not_call_parent_endpoint(scenario):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.builtin.compact import _apply_snip
    from engine.runner import _message_to_history_dict

    state, session_id, store, original, _, _, _, records = snip_branch(scenario)
    child_id, _ = state.get_or_create_child_session(session_id, "independent", "local", "accumulate")
    store.append_batch(child_id, original)
    history = [_message_to_history_dict(message) for message in original]
    snipped, _, task_ids = snip_history(history, records)
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("independent child called parent")))
    loop = SimpleNamespace(messages=history, rctx=SimpleNamespace(
        workspace_root=state.workspace_root, session_id=session_id, parent_session_id=session_id,
        child_session_id=child_id, supervisor_url="http://testserver", http=http, conversation_store=store,
    ))
    ctx = SimpleNamespace(messages=history)
    asyncio.run(_apply_snip(ctx, loop, child_id, records, snipped, task_ids, reason="threshold_snip"))
    http.post.assert_not_called()
    assert len(store.load(child_id)) == len(original) - 2
    assert record_list(store, session_id) == [message.to_dict() for message in original]
    assert ctx.messages is loop.messages and loop.messages == snipped


def test_synchronous_compaction_plan_survives_suspend_replay_and_is_removed_after_resume(scenario):
    state, session_id, store, original = scenario
    caller, compactor = dispatch(state, session_id)
    expected_plan = deepcopy(caller.input["_compact_plan"])
    suspended = next(
        event["payload"] for event in reversed(list(state.eventlog.iter_persisted_events()))
        if event["type"] == "task_suspended" and event["payload"]["task_id"] == caller.task_id
    )
    assert suspended["input"]["_input_slimmed"] is True
    assert suspended["input"]["_compact_plan"] == expected_plan

    def restore(previous):
        result = SupervisorState(
            workspace_root=previous.workspace_root,
            eventlog=EventLog(previous.workspace_root / "data" / "events.jsonl", run_id="compact-plan-replay"),
            policy=PolicyEngine(workspace_root=previous.workspace_root),
        )
        result._defer_post_work_enqueue = True
        result.rebuild_from_events(list(previous.eventlog.iter_persisted_events()))
        return result

    restored = restore(state)
    active_caller = restored.tasks[caller.task_id]
    assert active_caller.status == TaskStatus.suspended
    assert active_caller.input["_compact_plan"] == expected_plan
    assert active_caller.input["_compact_target_session_id"] == session_id
    assert active_caller.input["_compact_keep_recent"] == 2
    assert active_caller.input["_compact_keep_recent_tokens"] == 0
    assert active_caller.input["_compact_threshold_tokens"] == 100000
    complete(restored, restored.tasks[compactor.task_id])
    assert [message.id for message in store.load(session_id)[1:]] == [message.id for message in original[4:]]
    assert active_caller.input["resume_data"]["success"] is True
    again = restore(restored).tasks[caller.task_id]
    assert again.status == TaskStatus.pending
    assert "_compact_plan" not in again.input and "_compact_dispatch_pending" not in again.input
    assert again.input["resume_data"] == active_caller.input["resume_data"]
    assert again.input["context_ref"] == active_caller.input["context_ref"]


def test_supervisor_snip_rechecks_tool_dependencies_after_concurrent_append(scenario, monkeypatch):
    state, session_id, store, original, _, _, _, records = snip_branch(scenario)
    replace_all = ConversationStore.replace_all
    injected = False
    added = [
        Message(
            id="late-call", role="assistant", content="", source_task_id=records[0].task_id,
            tool_calls=[{"id": "late-tool", "function": {"name": "read_file"}}],
        ),
        Message(
            id="late-result", role="tool", content="New result", source_task_id=records[-1].task_id,
            tool_call_id="late-tool",
        ),
    ]

    def replace(writer, target, messages):
        nonlocal injected
        if target == session_id and not injected:
            injected = True
            ConversationStore(state.workspace_root / "data/conversations").append_batch(session_id, added)
        return replace_all(writer, target, messages)

    monkeypatch.setattr(ConversationStore, "replace_all", replace)
    outcome = state._apply_snip_via_conv_store_locked(session_id, {records[0].task_id, records[1].task_id})
    assert outcome["applied"] is True
    assert outcome["snipped_task_ids"] == [records[1].task_id]
    stored = store.load(session_id)
    assert stored[0].to_dict() == original[0].to_dict()
    assert [message.to_dict() for message in stored[-2:]] == [message.to_dict() for message in added]
