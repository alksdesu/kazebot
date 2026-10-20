from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from engine.builtin.compact import _apply_snip
from engine.conversation_store import Message
from engine.task_record import TaskRecord, safe_snip_task_ids, snip_history, snip_history_tasks, snip_store


def call(task_id, call_id="call"):
    return {
        "role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": {"name": "read_file"}}],
        "_meta": {"source_task_id": task_id, "message_type": "assistant"},
    }


def result(task_id, call_id="call"):
    return {
        "role": "tool", "content": "Original tool result", "tool_call_id": call_id,
        "_meta": {"source_task_id": task_id, "message_type": "tool_result"},
    }


@pytest.mark.parametrize("messages,candidates,expected", [
    ([call("a"), result("a")], {"a"}, {"a"}),
    ([call("a"), result("b")], {"a"}, set()),
    ([call("a"), result("b")], {"b"}, set()),
    ([call("a"), result("b")], {"a", "b"}, {"a", "b"}),
    ([call("a")], {"a"}, set()),
    ([result("a")], {"a"}, set()),
    ([call(""), result("b")], {"b"}, set()),
    ([call("a"), result("")], {"a"}, set()),
    ([call("a", "ab"), result("b", "ab"), call("b", "bc"), result("c", "bc")], {"a", "b"}, set()),
    ([call("a", "ab"), result("b", "ab"), call("b", "pending")], {"a", "b"}, set()),
    ([call("a"), result("b"), {"role": "user", "content": "plain", "_meta": {"source_task_id": "c"}}], {"a", "c"}, {"c"}),
    ([{"role": "assistant", "tool_calls": [{"function": {"name": "unknown"}}], "_meta": {"source_task_id": "a"}}], {"a"}, set()),
    ([{"role": "tool", "content": "unidentified", "_meta": {"source_task_id": "a"}}], {"a"}, set()),
])
def test_safe_task_selection_keeps_complete_tool_dependencies(messages, candidates, expected):
    assert safe_snip_task_ids(messages, candidates) == expected


def test_legacy_metadata_tool_ids_follow_the_same_dependency_rules():
    messages = [
        {"role": "assistant", "content": "call", "_meta": {"source_task_id": "a", "tool_call_ids": ["legacy"]}},
        {"role": "user", "content": "result", "_meta": {"source_task_id": "b", "tool_call_id": "legacy", "message_type": "tool_result"}},
    ]
    assert safe_snip_task_ids(messages, {"a"}) == set()
    assert safe_snip_task_ids(messages, {"a", "b"}) == {"a", "b"}


@pytest.mark.parametrize("source_in_meta", [False, True])
def test_history_and_store_never_split_cross_task_tool_pairs(source_in_meta):
    records = [TaskRecord(task_id=f"t{index}", session_id="session", node_id="test", summary=f"Summary {index}") for index in range(6)]
    messages = [
        call("t0"),
        {"role": "user", "content": "plain old task", "_meta": {"source_task_id": "t1"}},
        result("t5"),
    ]
    history, count, selected = snip_history(messages, records)
    assert count == 1 and selected == {"t1"}
    assert history[0] == messages[0] and history[-1] == messages[-1]
    stored = [
        Message(
            id=f"message-{index}", role=message["role"], content=message["content"],
            meta=deepcopy(message["_meta"]) if source_in_meta else {},
            source_task_id="" if source_in_meta else message["_meta"]["source_task_id"],
            tool_calls=message.get("tool_calls", []), tool_call_id=message.get("tool_call_id", ""),
        )
        for index, message in enumerate(messages)
    ]
    persisted = snip_store(stored, records, {"t0", "t1"})
    assert persisted[0].to_dict() == stored[0].to_dict()
    assert persisted[-1].to_dict() == stored[-1].to_dict()
    assert persisted[1].message_type == "summary"


def test_worker_only_applies_task_ids_confirmed_by_supervisor(tmp_path):
    records = [TaskRecord(task_id=task_id, session_id="parent", node_id="test", summary=task_id) for task_id in ("a", "b")]
    original = [
        {"role": "user", "content": "original a", "_meta": {"source_task_id": "a"}},
        {"role": "user", "content": "original b", "_meta": {"source_task_id": "b"}},
    ]
    proposed, _, selected = snip_history_tasks(original, records, {"a", "b"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"applied": True, "snipped_task_ids": ["b"]}),
        )) as client:
            loop = SimpleNamespace(messages=original, rctx=SimpleNamespace(
                workspace_root=tmp_path, session_id="branch", parent_session_id="parent",
                child_session_id="", supervisor_url="http://testserver", http=client,
            ))
            ctx = SimpleNamespace(messages=original)
            count = await _apply_snip(ctx, loop, "parent", records, proposed, selected, reason="proactive_snip")
            assert count == 1
            assert loop.messages[0] == original[0]
            assert loop.messages[1]["_meta"]["message_type"] == "summary"
            assert ctx.messages is loop.messages

    asyncio.run(run())
