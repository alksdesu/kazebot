from copy import deepcopy

import pytest

from engine.branch_history import branch_base_count, mark_branch_boundary, without_branch_boundary
from engine.compaction_plan import build_compaction_plan
from engine.conversation_store import ConversationStore, Message
from engine.task_record import TaskRecord, write_task_record
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import TaskKind


@pytest.fixture
def branch_case(tmp_path, monkeypatch):
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="branch-recovery"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state._defer_post_work_enqueue = True
    parent = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:recovery")
    store = ConversationStore(tmp_path / "data/conversations")
    messages = [
        Message(
            id=f"{index}-{role}", role=role, content=f"Original task {index}: " + "detail " * 80,
            source_task_id=f"task-{index}",
        )
        for index in range(6) for role in ("user", "assistant")
    ]
    store.append_batch(parent, messages)
    for index in range(6):
        write_task_record(tmp_path, TaskRecord(
            task_id=f"task-{index}", session_id=parent, node_id="test", summary=f"Summary {index}",
        ))
    with state._lock:
        branch, fork = state._create_entry_branch_locked(parent, 101)
        task = state._create_task_locked(
            session_id=branch, session_generation=1, kind=TaskKind.node, node_id="qq.orchestrator",
            input_data={"parent_session_id": parent, "branch_session_id": branch, **fork},
        )
    tail = Message(id="current-tail", role="assistant", content="Current branch result", source_task_id=task.task_id)
    store.append(branch, tail)
    created = deepcopy(next(
        event["payload"] for event in state.eventlog.iter_persisted_events()
        if event["type"] == "task_created" and event["payload"]["task_id"] == task.task_id
    ))
    append = state._event_task_snapshot

    def interrupt(event, current, *args, **kwargs):
        if event == "task_context_compacted":
            raise OSError("event durability interrupted")
        return append(event, current, *args, **kwargs)

    monkeypatch.setattr(state, "_event_task_snapshot", interrupt)
    return state, store, parent, branch, task, tail, created, messages


def compact_llm(state, store, parent, *, keep_recent):
    plan, _ = build_compaction_plan(store.load(parent), keep_recent=keep_recent)
    assert plan
    result = state._apply_compact_via_conv_store_locked(
        parent, "Summary of the complete selected history.", keep_recent=keep_recent, plan=plan,
    )
    assert result["after"] < result["before"]


def restore_old_task(state, created):
    state._apply_task_snapshot(deepcopy(created))
    task = state.tasks[created["task_id"]]
    assert task.input["base_count"] == 12
    return task


@pytest.mark.parametrize("mode", ["llm", "snip", "snip_last_task"])
@pytest.mark.parametrize("merge_path", ["finalize", "legacy"])
def test_interrupted_compaction_event_cannot_drop_branch_tail(branch_case, mode, merge_path):
    state, store, parent, branch, _, tail, created, original = branch_case
    if mode == "llm":
        compact_llm(state, store, parent, keep_recent=2)
    else:
        selected = {"task-0", "task-1"} if mode == "snip" else {"task-5"}
        assert not state._apply_snip_via_conv_store_locked(parent, selected)["applied"]
    task = restore_old_task(state, created)
    branch_messages = store.load(branch)
    boundary = branch_base_count(branch_messages, branch, task.input["base_count"])
    assert boundary == len(store.load(parent))
    assert branch_messages[boundary:] == [tail]
    if mode == "snip_last_task":
        assert original[-1].id not in {message.id for message in branch_messages}
    if merge_path == "finalize":
        plan = state._prepare_branch_finalize_locked(task, merge=True)
        assert state._execute_branch_finalize_io(plan) == 1
        assert state._execute_branch_finalize_io(plan) == 0
    else:
        assert state._merge_branch_locked(parent, branch, task.input["base_count"]) == 1
    assert [message.id for message in store.load(parent)].count(tail.id) == 1


@pytest.mark.parametrize("first", ["snip_last_task", "llm"])
def test_multiple_compactions_use_durable_boundary_and_keep_fingerprints_compatible(branch_case, first):
    state, store, parent, branch, _, tail, created, _ = branch_case
    if first == "snip_last_task":
        state._apply_snip_via_conv_store_locked(parent, {"task-5"})
    else:
        compact_llm(state, store, parent, keep_recent=4)
    restore_old_task(state, created)
    compact_llm(state, store, parent, keep_recent=2)
    task = restore_old_task(state, created)
    branch_messages = store.load(branch)
    boundary = branch_base_count(branch_messages, branch, task.input["base_count"])
    assert boundary == len(store.load(parent))
    assert [message.to_dict() for message in without_branch_boundary(branch_messages[:boundary], branch)] == [
        message.to_dict() for message in store.load(parent)
    ]
    plan = state._prepare_branch_finalize_locked(task, merge=True)
    assert state._execute_branch_finalize_io(plan) == 1
    assert store.load(parent)[-1].id == tail.id


def test_legacy_store_merge_receives_durable_base(branch_case, monkeypatch):
    state, store, parent, branch, _, tail, created, _ = branch_case
    compact_llm(state, store, parent, keep_recent=2)
    task = restore_old_task(state, created)
    expected_base = len(store.load(parent))
    observed = []

    def merge(writer, parent_id, branch_id, *, base_count):
        observed.append(base_count)
        messages = writer.load(branch_id)[base_count:]
        writer.append_batch(parent_id, messages)
        return len(messages)

    monkeypatch.setattr(ConversationStore, "merge", merge, raising=False)
    assert state._merge_branch_locked(parent, branch, task.input["base_count"]) == 1
    assert observed == [expected_base]
    assert store.load(parent)[-1].id == tail.id


def test_boundary_metadata_does_not_mutate_parent_or_other_branch():
    source = [Message(id="base", role="user", content="parent", meta={"custom": "retained"})]
    marked = mark_branch_boundary(source, "branch-a", 1)
    assert source[0].meta == {"custom": "retained"}
    assert marked[0].id == source[0].id
    assert branch_base_count(marked, "branch-a", 99) == 1
    assert branch_base_count(marked, "branch-b", 99) == 99
    assert without_branch_boundary(marked, "branch-a")[0].to_dict() == source[0].to_dict()
    assert branch_base_count(source, "legacy", 7) == 7


@pytest.mark.parametrize("always_conflict", [False, True])
def test_llm_branch_sync_retries_appends_without_losing_tail(branch_case, monkeypatch, caplog, always_conflict):
    state, store, parent, branch, _, tail, created, _ = branch_case
    compact_llm(state, store, parent, keep_recent=4)
    task = restore_old_task(state, created)
    before = [message.to_dict() for message in store.load(branch)]
    previous_base = branch_base_count(store.load(branch), branch, task.input["base_count"])
    replace_all = ConversationStore.replace_all
    attempts = 0

    def replace(writer, session_id, messages):
        nonlocal attempts
        if session_id == branch:
            attempts += 1
            if always_conflict or attempts == 1:
                ConversationStore(state.workspace_root / "data/conversations").append(
                    branch, Message(id=f"concurrent-{attempts}", role="assistant", content=f"New result {attempts}"),
                )
        return replace_all(writer, session_id, messages)

    monkeypatch.setattr(ConversationStore, "replace_all", replace)
    caplog.clear()
    compact_llm(state, store, parent, keep_recent=2)
    assert attempts == (3 if always_conflict else 2)
    branch_messages = store.load(branch)
    ids = [message.id for message in branch_messages]
    assert tail.id in ids and "concurrent-1" in ids
    boundary = branch_base_count(branch_messages, branch, task.input["base_count"])
    if always_conflict:
        assert [message.to_dict() for message in branch_messages[:len(before)]] == before
        assert ids[-3:] == ["concurrent-1", "concurrent-2", "concurrent-3"]
        assert boundary == previous_base and task.input["base_count"] == 12
        assert "compact branch sync failed" in caplog.text
        assert "conversation changed before replacement" in caplog.text
    else:
        assert boundary == len(store.load(parent)) == task.input["base_count"]
        assert [message.to_dict() for message in without_branch_boundary(branch_messages[:boundary], branch)] == [
            message.to_dict() for message in store.load(parent)
        ]
        assert ids[boundary:] == [tail.id, "concurrent-1"]
