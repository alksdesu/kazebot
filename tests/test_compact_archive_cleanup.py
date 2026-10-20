from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.builtin import compact
from engine.context_archive import ContextArchiveStore
from engine.conversation_store import ConversationStore, Message
from engine.task_record import TaskRecord, snip_history, snip_store, write_task_record


def loop_state(tmp_path, messages, store=None, **overrides):
    rctx = SimpleNamespace(
        workspace_root=tmp_path, session_id="session", parent_session_id="", child_session_id="",
        conversation_store=store, emit_event=AsyncMock(),
    )
    state = SimpleNamespace(
        messages=messages, rctx=rctx, node=SimpleNamespace(id="test"),
        compacted=False, compact_threshold=1, compact_hard_threshold=1,
        last_prompt_tokens=1000, compact_keep_recent=3,
        compact_keep_recent_tokens=0, compact_async_requested=False,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return SimpleNamespace(messages=messages, extra={}, step=0), state


def task_history(tmp_path):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    records = []
    messages = []
    for number in range(6):
        task_id = f"task-{number}"
        record = TaskRecord(task_id=task_id, session_id="session", node_id="test", summary=f"Summary {number}")
        records.append(record)
        write_task_record(tmp_path, record)
        for role in ("user", "assistant"):
            messages.append(Message(
                id=f"{task_id}-{role}", role=role, content=f"Original {task_id} {role}",
                message_type="user_input" if role == "user" else role, created_at=stamp,
                meta={"timestamp": stamp, "custom": {"flag": number}},
                source_node_id="test", source_task_id=task_id,
            ))
    history = [
        {"role": message.role, "content": message.content, "_meta": {
            **deepcopy(message.meta), "source_task_id": message.source_task_id,
            "message_type": message.message_type,
        }} for message in messages
    ]
    return records, messages, history


def archived_messages(tmp_path, session_id="session"):
    archive = ContextArchiveStore(tmp_path)
    matches = archive.search(session_id)["matches"]
    assert matches
    return archive.read(session_id, matches[0]["archive_id"], limit=50)["messages"]


def run_cleanup(mode, ctx, state):
    if mode == "proactive":
        return asyncio.run(compact._run_idle_cleanup(ctx, state))
    return asyncio.run(compact._check_and_compact(ctx, state))


@pytest.mark.parametrize("fail_archive", [False, True])
def test_microcompact_archives_original_before_mutating_nested_history(tmp_path, monkeypatch, fail_archive):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    messages = [{"role": "assistant", "content": "tools", "_meta": {"timestamp": stamp}}]
    messages.extend(
        {"role": "user", "content": f'Tool result for "tool_{number}"\nOriginal output {number}',
         "_meta": {"message_type": "tool_result", "timestamp": stamp}}
        for number in range(7)
    )
    original = deepcopy(messages)
    ctx, state = loop_state(tmp_path, messages)
    if fail_archive:
        monkeypatch.setattr(ContextArchiveStore, "archive", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("archive unavailable")))
    modified = asyncio.run(compact._run_idle_cleanup(ctx, state))
    assert messages == original
    if fail_archive:
        assert modified is False and state.messages is messages and ctx.messages is messages
    else:
        assert modified is True
        assert "Original output 0" not in state.messages[1]["content"]
        assert ctx.messages is state.messages
        archived = [item["message"] for item in archived_messages(tmp_path)]
        assert archived == original
        assert ContextArchiveStore(tmp_path).search("session", after=stamp)["matches"]


@pytest.mark.parametrize("mode", ["proactive", "threshold"])
@pytest.mark.parametrize("durable", [False, True])
def test_snip_archives_complete_source_before_updating_memory(tmp_path, mode, durable):
    records, stored, history = task_history(tmp_path)
    store = ConversationStore(tmp_path / "data" / "conversations") if durable else None
    if store is not None:
        store.append_batch("session", stored)
    ctx, state = loop_state(tmp_path, history, store)
    original = deepcopy(history)
    run_cleanup(mode, ctx, state)
    assert history == original
    assert state.messages is ctx.messages and state.messages != original
    summary = state.messages[0]
    assert summary["_meta"]["timestamp"] == original[0]["_meta"]["timestamp"]
    assert summary["_meta"]["custom"] == original[0]["_meta"]["custom"]
    assert summary["_meta"]["message_type"] == "summary"
    assert "search_context_archive" in summary["content"] and "read_context_archive" in summary["content"]
    archived = [item["message"] for item in archived_messages(tmp_path)]
    assert archived == ([message.to_dict() for message in stored] if durable else original)
    if store is not None:
        persisted = store.load("session")[0]
        assert persisted.created_at == stored[0].created_at
        assert persisted.meta["custom"] == stored[0].meta["custom"]
        assert persisted.message_type == "summary" and persisted.source_node_id == "test"
    assert len(records) == 6


@pytest.mark.parametrize("mode", ["proactive", "threshold"])
@pytest.mark.parametrize("failure", ["archive", "replace", "concurrent_write"])
def test_snip_failure_keeps_runtime_and_durable_originals(tmp_path, monkeypatch, mode, failure):
    _, stored, history = task_history(tmp_path)
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append_batch("session", stored)
    original = deepcopy(history)
    original_archive = ContextArchiveStore.archive
    concurrent = Message(id="concurrent", role="user", content="Arrived during archive", source_task_id="new")
    if failure == "archive":
        def archive(*args, **kwargs):
            raise OSError("archive unavailable")
        monkeypatch.setattr(ContextArchiveStore, "archive", archive)
    elif failure == "replace":
        def replace_all(*args, **kwargs):
            raise OSError("replacement unavailable")
        monkeypatch.setattr(store, "replace_all", replace_all)
    else:
        def archive(self, *args, **kwargs):
            result = original_archive(self, *args, **kwargs)
            ConversationStore(tmp_path / "data" / "conversations").append("session", concurrent)
            return result
        monkeypatch.setattr(ContextArchiveStore, "archive", archive)
    monkeypatch.setattr(compact, "_persist_ctx", lambda *args: "synthetic-context")
    ctx, state = loop_state(tmp_path, history, store)
    run_cleanup(mode, ctx, state)
    assert state.messages is history and ctx.messages is history and history == original
    actual = ConversationStore(tmp_path / "data" / "conversations").load("session")
    expected = stored + ([concurrent] if failure == "concurrent_write" else [])
    assert [message.to_dict() for message in actual] == [message.to_dict() for message in expected]


def test_snip_archives_durable_messages_outside_current_runtime_view(tmp_path):
    _, stored, history = task_history(tmp_path)
    outside = Message(id="outside", role="user", content="Earlier topic original", meta={"topic_id": "historical"})
    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append_batch("session", [outside, *stored])
    ctx, state = loop_state(tmp_path, history, store)
    asyncio.run(compact._run_idle_cleanup(ctx, state))
    assert archived_messages(tmp_path)[0]["message"] == outside.to_dict()
    assert store.load("session")[0].to_dict() == outside.to_dict()


def test_snip_history_and_store_keep_independent_metadata_copies(tmp_path):
    records, stored, history = task_history(tmp_path)
    snipped, count, task_ids = snip_history(history, records)
    persisted = snip_store(stored, records, task_ids)
    assert count == 2 and snipped[0]["content"] == persisted[0].content
    snipped[0]["_meta"]["custom"]["flag"] = "changed"
    persisted[0].meta["custom"]["flag"] = "changed"
    assert history[0]["_meta"]["custom"]["flag"] == 0 and stored[0].meta["custom"]["flag"] == 0
