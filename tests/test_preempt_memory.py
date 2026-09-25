from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from engine.builtin.knowledge_inject import build_knowledge_context
from engine.builtin.preempt import _inject_preempt_message
from engine.context import RunContext
from engine.conversation_store import ConversationStore
from engine.memory_subjects import record_interaction
from engine.node import Node
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import Task, TaskKind, TaskStatus


def _archive(root: Path, subject: str, *, constant: bool = False) -> None:
    record_interaction(root, subject)
    directory = root / "data" / "memory" / f"user_{subject}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.yaml").write_text(
        yaml.safe_dump({"entries": [{
            "id": f"fact_{subject}", "subject": subject,
            "content": f"profile for {subject}", "constant": constant,
        }]}), encoding="utf-8",
    )


def _loop(root: Path, payload: dict, *, block_mode: bool = False) -> SimpleNamespace:
    node = Node(id="chat", type="ai")
    context = {"memory_hints": {"subjects": ["UserA"]}, "platform_auth": {"is_admin": False}}
    static = build_knowledge_context(root, node, "old", [], {}, task_context=context)[2]
    prompt = [{"role": "system", "content": "persona"}]
    if block_mode:
        prompt.extend([{"role": "user", "content": "fixed instructions"}, {"role": "history"}])
    prefix = [message for message in prompt if message["role"] != "history"]
    history = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "content": "tool result", "tool_call_id": "call-1"},
    ]
    return SimpleNamespace(
        node=node, runtime_cfg={}, preempt_inject_info=payload,
        rctx=SimpleNamespace(
            workspace_root=root, task_context=context, session_id="branch", child_session_id="child",
            task_id="task", conversation_store=ConversationStore(root / "data" / "conversations"),
            consume_preempt=AsyncMock(), emit_event=AsyncMock(),
        ),
        messages=prefix + static + history + [{"role": "user", "content": "outdated context", "_dynamic": True}],
        history=history, system_prompt=prompt, is_block_mode=block_mode,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("constant", [False, True])
@pytest.mark.parametrize("block_mode", [False, True])
async def test_new_subject_reaches_preempt_prompt_and_history(
    tmp_path: Path, constant: bool, block_mode: bool,
) -> None:
    _archive(tmp_path, "UserA", constant=True)
    _archive(tmp_path, "UserB", constant=constant)
    loop = _loop(tmp_path, {
        "message": "now discuss UserB", "memory_hints": {"subjects": ["UserB", "UserUnknown"]}, "revision": 7,
    }, block_mode=block_mode)
    hook = SimpleNamespace(messages=loop.messages, step=2)

    await _inject_preempt_message(hook, loop)

    text = "\n".join(str(message.get("content", "")) for message in loop.messages)
    assert "profile for UserB" in text
    assert "profile for UserA" not in text
    assert "outdated context" not in text
    assert loop.rctx.task_context == {
        "memory_hints": {"subjects": ["UserB"]}, "platform_auth": {"is_admin": False},
    }
    assert loop.messages[-1] == {"role": "user", "content": "now discuss UserB"}
    assert {"role": "tool", "content": "tool result", "tool_call_id": "call-1"} in loop.messages
    assert loop.messages[0] == {"role": "system", "content": "persona"}
    assert hook.messages is loop.messages
    loop.rctx.consume_preempt.assert_awaited_once_with(revision=7)
    assert [message.content for message in loop.rctx.conversation_store.load("child")] == ["now discuss UserB"]
    assert loop.rctx.conversation_store.load("branch") == []


@pytest.mark.asyncio
async def test_legacy_preempt_keeps_existing_memory_hints(tmp_path: Path) -> None:
    _archive(tmp_path, "UserA", constant=True)
    loop = _loop(tmp_path, {"message": "one more detail"})
    await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=0), loop)
    assert loop.rctx.task_context["memory_hints"] == {"subjects": ["UserA"]}
    assert any("profile for UserA" in str(message.get("content", "")) for message in loop.messages)
    loop.rctx.consume_preempt.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_empty_hints_remove_old_subject_static_memory_from_legacy_snapshot(tmp_path: Path) -> None:
    _archive(tmp_path, "UserA", constant=True)
    loop = _loop(tmp_path, {"message": "new topic", "memory_hints": {"subjects": []}})
    for message in loop.messages:
        message.pop("_knowledge_source", None)
    await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=0), loop)
    assert not any("profile for UserA" in str(message.get("content", "")) for message in loop.messages)


@pytest.mark.asyncio
async def test_run_context_acknowledges_only_the_consumed_revision(tmp_path: Path) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        context = RunContext(
            workspace_root=tmp_path, supervisor_url="http://test", session_id="session",
            worker_id="worker", http=client, llm_http=client, task_id="task",
        )
        await context.consume_preempt(revision=3)
        await context.consume_preempt()
    assert requests[0].content == b'{"revision":3}'
    assert requests[1].content == b'{}'


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="preempt"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    now = datetime.now(timezone.utc)
    state.tasks["task"] = Task(
        task_id="task", session_id="session", kind=TaskKind.node, status=TaskStatus.running,
        created_at=now, updated_at=now,
        input={"task_context": {"platform_auth": {"is_admin": False}, "memory_hints": {"subjects": ["UserA"]}}},
    )
    return state


def test_acknowledging_old_preempt_preserves_new_message_and_hints(state: SupervisorState, tmp_path: Path) -> None:
    _archive(tmp_path, "UserA")
    _archive(tmp_path, "UserB")
    assert state.preempt_task("task", "first", attachments=[{"type": "image"}], memory_hints={"subjects": ["UserA"]})
    first = state.is_task_preempted("task")
    assert state.preempt_task("task", "second", memory_hints={"subjects": ["UserB", "UserUnknown"]})
    assert state.consume_preempt_message("task", expected_revision=first["revision"])["consumed"] is False
    second = state.is_task_preempted("task")
    assert second["message"] == "second"
    assert second["attachments"] == []
    assert second["memory_hints"] == {"subjects": ["UserB"]}
    assert second["revision"] > first["revision"]
    assert state.consume_preempt_message("task", expected_revision=second["revision"])["consumed"] is True
    assert state.is_task_preempted("task")["preempted"] is False
    assert state.tasks["task"].input["task_context"] == {
        "platform_auth": {"is_admin": False}, "memory_hints": {"subjects": ["UserB"]},
    }


def test_legacy_preempt_does_not_erase_task_memory_hints(state: SupervisorState) -> None:
    assert state.preempt_task("task", "legacy")
    state.consume_preempt_message("task")
    assert state.tasks["task"].input["task_context"]["memory_hints"] == {"subjects": ["UserA"]}


def test_inactive_task_rejects_preempt_without_mutating_identity(state: SupervisorState) -> None:
    state.tasks["task"].status = TaskStatus.cancelled
    assert state.preempt_task("task", "late", memory_hints={"subjects": []}) is False
    assert state.tasks["task"].input["task_context"]["platform_auth"] == {"is_admin": False}


@pytest.mark.asyncio
async def test_sdk_api_and_engine_update_the_same_preempt_memory(
    state: SupervisorState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clonoth_sdk.client import ClonothClient
    from engine.inference import ai_step
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore

    _archive(tmp_path, "UserA", constant=True)
    _archive(tmp_path, "UserB")
    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"))
    loop = _loop(tmp_path, {})
    captured_contexts = []
    original_context = ai_step.ToolContext

    def capture_context(**kwargs):
        context = original_context(**kwargs)
        captured_contexts.append(context)
        return context

    monkeypatch.setattr(ai_step, "ToolContext", capture_context)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sdk = ClonothClient("http://test")
        sdk._client = client
        assert await sdk.preempt_task("task", message="UserB follow-up", memory_hints={"subjects": ["UserB"]})
        context = RunContext(
            workspace_root=tmp_path, supervisor_url="http://test", session_id="session",
            worker_id="worker", http=client, llm_http=client, task_id="task",
            task_context=loop.rctx.task_context,
        )
        context.conversation_store = loop.rctx.conversation_store
        loop.rctx = context
        loop.preempt_inject_info = await context.check_preempted()
        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=0), loop)
        loop.run_id = "run"
        loop.registry = None
        await ai_step._execute_real_tools(loop, [], 1)

    assert "profile for UserB" in str(loop.messages)
    assert "profile for UserA" not in str(loop.messages)
    assert captured_contexts[0]._memory_subjects == ["UserB"]
    assert captured_contexts[0].platform_auth == {"is_admin": False}
    assert state.tasks["task"].input["task_context"]["memory_hints"] == {"subjects": ["UserB"]}
    assert not state.is_task_preempted("task")["preempted"]
