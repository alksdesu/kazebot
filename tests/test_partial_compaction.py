from types import SimpleNamespace

import pytest

from engine.compact import total_content_chars
from engine.context_archive import ContextArchiveStore
from engine.context_store import load_context_snapshot, save_context_snapshot
from engine.conversation_store import ConversationStore, Message
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import TaskKind, TaskStatus


@pytest.mark.parametrize("mode", ["sync", "async", "snapshot"])
def test_one_large_record_can_compact_without_reducing_message_count(tmp_path, mode):
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="partial"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state._defer_post_work_enqueue = True
    session = state.get_or_create_session(channel="test", conversation_key="test:partial")
    original = [
        Message(id="large", role="user", content="x" * 200_000, source_task_id="old"),
        Message(id="remainder", role="assistant", content="y" * 150_000, source_task_id="old"),
        Message(id="recent1", role="user", content="recent one", source_task_id="recent1"),
        Message(id="recent2", role="user", content="recent two", source_task_id="recent2"),
    ]
    store = ConversationStore(tmp_path / "data/conversations")
    store.append_batch(session, original)
    context_ref = ""
    if mode == "snapshot":
        path = tmp_path / "config/runtime.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text("engine:\n  child_session:\n    main_session_enabled: false\n")
        history = [
            {"role": message.role, "content": message.content, "_meta": {"source_task_id": message.source_task_id}}
            for message in original
        ]
        context_ref = save_context_snapshot(tmp_path, session, {"messages": history})
    sizing = {"_compact_keep_recent": 2, "_compact_keep_recent_tokens": 0}
    summary = "<summary>" + "Preserved decisions and facts from the first large record. " * 8 + "</summary>"
    state.compact_breaker.record_failure(session)
    if mode == "async":
        plan, _ = state._prepare_compact_input_locked(session, sizing)
        assert plan["partial_segment"] and len(plan["indexed_records"]) == 1
        task = SimpleNamespace(
            input={**sizing, "_compact_plan": plan, "_compact_target_session_id": session},
            result={"action": "finish", "result": {"text": summary}},
        )
        state._apply_async_compact_result_locked(task)
    else:
        caller = state._create_task_locked(
            session_id=session, session_generation=1, kind=TaskKind.node,
            node_id="chat", input_data={},
        )
        caller.result = {"context_ref": context_ref}
        state._route_dispatch_locked(caller, {
            "target_node": "system.compactor", "context_ref": context_ref,
            "dispatch_input": {
                **sizing, "_compact_dispatch": True, "target_session_id": session,
                "context_mode": "fresh", "instruction": "request",
            },
        })
        compactor = state.tasks[caller.waiting_for_task_id]
        compactor.status = TaskStatus.running
        compactor.worker_id = "worker"
        state.complete_task(
            task_id=compactor.task_id, worker_id="worker",
            result={"action": "finish", "result": {"text": summary}},
        )
        assert caller.input["resume_data"]["success"] is True
        assert caller.input["resume_data"]["before"] == caller.input["resume_data"]["after"] == 4
    current = load_context_snapshot(tmp_path, context_ref)["messages"] if mode == "snapshot" else store.load(session)
    assert len(current) == 4
    assert total_content_chars(current) < total_content_chars(original) - 190_000
    first = current[0]
    meta = first["_meta"] if isinstance(first, dict) else first.meta
    assert "old" not in meta["compressed_task_ids"]
    assert state.compact_breaker.failure_count(session) == 0
    assert ContextArchiveStore(tmp_path).search(session, "x" * 30)["matches"]
