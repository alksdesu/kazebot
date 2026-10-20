from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from engine import runner
from engine.conversation_store import ConversationStore, Message
from engine.node import Node
from engine.protocol import TaskAction
from toolbox.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_kind", ["compact_done", "child_result", "child_preempted"])
@pytest.mark.parametrize("child", [False, True])
async def test_resume_reloads_own_durable_history_without_repeating_instruction(
    tmp_path, monkeypatch, resume_kind, child,
):
    monkeypatch.setattr(runner, "load_node", lambda *args: Node(id="test", type="ai", prompt="test"))
    monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(runner, "install_event_bridge", lambda *args, **kwargs: None)
    capture = AsyncMock(return_value=TaskAction(action="finish", node_id="test", result={"text": "done"}))
    monkeypatch.setattr(runner, "run_ai_node", capture)
    store = ConversationStore(tmp_path / "data/conversations")
    session_id = "child" if child else "parent"
    records = [
        Message(id="sum", role="user", content="earlier summary", source_task_id="compact_summary", message_type="summary"),
        Message(id="input", role="user", content="original request", source_task_id="task", meta={"topic_id": "old"}),
        Message(id="reply", role="assistant", content="progress", source_task_id="task", meta={"topic_id": "other"}),
    ]
    store.append_batch(session_id, records)
    store.append("unrelated", Message(id="private", role="user", content="unrelated private history"))
    task = {
        "task_id": "task", "node_id": "test",
        "input": {
            "instruction": "original request", "resume_data": {"type": resume_kind, "success": True},
            "context_mode": "accumulate", "attachments": [{"type": "image", "path": "missing.png"}],
            **({"child_session_id": "child", "parent_session_id": "parent"} if child else {}),
        },
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as http:
        await runner._run_node_task(
            http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
            registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
            task=task, worker_id="worker", session_id="parent", session_generation=1,
            api_key="test", base_url="http://model.invalid", default_model="test",
        )
    call = capture.await_args.kwargs
    assert [message["content"] for message in call["history"]] == [record.content for record in records]
    assert call["instruction"] == "" and call["attachments"] is None
    assert call["context_ref"] == ""
    assert len(store.load(session_id)) == 3
