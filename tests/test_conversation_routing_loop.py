from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.builtin.plaintext import PlaintextRetryHandler
from engine.context import RunContext
from engine.conversation_store import ConversationStore
from engine.hooks.registry import HookRegistry
from engine.inference import ai_step
from engine.node import Node, ToolAccess
from providers.base import ProviderResponse, ToolCall


@pytest.fixture()
def loop(monkeypatch, tmp_path):
    hooks = HookRegistry()
    hooks.register("before_response", PlaintextRetryHandler())
    monkeypatch.setattr(ai_step, "hook_registry", hooks)
    monkeypatch.setattr(ai_step, "auto_discover_and_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *args: None)
    monkeypatch.setattr(ai_step, "load_runtime_config", lambda *args: {"engine": {"max_steps": 5}})
    rctx = RunContext(tmp_path, "http://localhost:0", "session", "worker", AsyncMock(), AsyncMock(),
                      task_id="current", task_context={"route_hints": {"topic_id": "books", "response_action": "short_reply"}})
    rctx.emit_event = AsyncMock()
    rctx.check_preempted = AsyncMock(return_value={"preempted": False})
    rctx.check_cancelled = AsyncMock(return_value=False)
    rctx.conversation_store = ConversationStore(tmp_path)
    node = Node(id="qq.orchestrator", type="ai", prompt="Speak naturally", tool_mode="native", output_mode="hybrid",
                tool_access=ToolAccess(mode="all"), delegate_targets=["helper"])
    registry = SimpleNamespace(list_specs=lambda: [{"name": "dangerous_tool", "description": "test", "input_schema": {"type": "object"}}])
    snapshots = []

    async def run(response, *, history=None, context_ref="", resume_data=None):
        async def provider_call(ls, step):
            snapshots.append({"messages": copy.deepcopy(ls.messages), "tools": copy.deepcopy(ls.openai_tools),
                              "allowed": set(ls.allowed_real_tools), "dispatch": set(ls.allowed_dispatch_targets)})
            ls.tool_produced_attachments.append({"type": "image", "path": "old-output.png"})
            return response

        monkeypatch.setattr(ai_step, "_call_llm_with_retry", provider_call)
        return await ai_step.run_ai_node(rctx=rctx, provider=SimpleNamespace(name="test"), registry=registry,
                                        node=node, instruction="current", history=history or [], context_ref=context_ref,
                                        resume_data=resume_data)

    return SimpleNamespace(run=run, rctx=rctx, node=node, hooks=hooks, snapshots=snapshots)


def test_hybrid_short_reply_restricts_real_loop_output_and_tools(loop):
    result = asyncio.run(loop.run(ProviderResponse(ok=True, text="自然接话。" * 100)))
    assert result.action == "finish"
    assert len(result.result["text"]) <= 240
    assert result.result["attachments"] == []
    assert [item["function"]["name"] for item in loop.snapshots[0]["tools"]] == ["finish"]
    assert not loop.snapshots[0]["allowed"] and not loop.snapshots[0]["dispatch"]


def test_short_finish_does_not_send_old_generated_attachments(loop):
    response = ProviderResponse(ok=True, tool_calls=[ToolCall(id="finish-1", name="finish", arguments={"text": "长" * 500})])
    result = asyncio.run(loop.run(response))
    assert result.action == "finish" and len(result.result["text"]) <= 240
    assert result.result["attachments"] == []


def test_unauthorized_tool_plaintext_fallback_is_still_short(loop, monkeypatch):
    execute = AsyncMock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(ai_step, "_execute_real_tools", execute)
    response = ProviderResponse(ok=True, text="回应。" * 200,
                                tool_calls=[ToolCall(id="bad", name="dangerous_tool", arguments={})])
    result = asyncio.run(loop.run(response))
    assert result.action == "finish" and len(result.result["text"]) <= 240
    assert result.result["attachments"] == []
    execute.assert_not_called()


def test_short_reply_cannot_switch_node_or_send_intermediate_message(loop):
    response = ProviderResponse(ok=True, tool_calls=[
        ToolCall(id="reply", name="reply", arguments={"text": "do not send"}),
        ToolCall(id="switch", name="switch_node", arguments={"target": "helper"}),
        ToolCall(id="finish", name="finish", arguments={"text": "ok"}),
    ])
    result = asyncio.run(loop.run(response))
    assert result.result["text"] == "ok"
    assert not any(call.args[0] in {"intermediate_reply", "node_switch"} for call in loop.rctx.emit_event.call_args_list)
    loop.rctx.http.post.assert_not_called()


def mixed_history():
    return [
        {"role": "system", "content": "system remains"},
        {"role": "user", "content": "shared summary", "_meta": {"message_type": "summary", "source_task_id": "compact_summary"}},
        {"role": "user", "content": "books question", "_meta": {"source_task_id": "a", "topic_id": "books"}},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "lookup", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}], "_meta": {"source_task_id": "a", "topic_id": "books"}},
        {"role": "user", "content": "foreign question", "_meta": {"source_task_id": "b", "topic_id": "games"}},
        {"role": "tool", "tool_call_id": "lookup", "content": "books lookup result"},
        {"role": "assistant", "content": "foreign response", "_meta": {"source_task_id": "b", "topic_id": "games"}},
        {"role": "user", "content": "current", "_meta": {"source_task_id": "current", "topic_id": "books"}},
    ]


@pytest.mark.parametrize("snapshot", [False, True])
def test_actual_loop_filters_resume_and_live_hook_history_without_breaking_tool_pairs(loop, monkeypatch, snapshot):
    messages = mixed_history()
    monkeypatch.setattr(ai_step, "load_context_snapshot", lambda *args: {"messages": messages, "step_count": 0})

    class InsertContext:
        name = "test_context"
        priority = 1

        async def handle(self, ctx):
            ctx.extra["loop_state"].messages.append({"role": "user", "content": "late foreign", "_meta": {"source_task_id": "c", "topic_id": "games"}})
            ctx.extra["loop_state"].messages.append({"role": "user", "content": "current dynamic memory", "_dynamic": True})

    loop.hooks.register("before_step", InsertContext())
    result = asyncio.run(loop.run(ProviderResponse(ok=True, text="ok"), history=messages,
                                  context_ref="snapshot" if snapshot else ""))
    assert result.action == "finish"
    seen = loop.snapshots[0]["messages"]
    content = "\n".join(str(message.get("content")) for message in seen)
    assert "foreign" not in content
    assert "shared summary" in content and "current dynamic memory" in content
    assert any(message.get("tool_call_id") == "lookup" for message in seen)
    assert any(call.get("id") == "lookup" for message in seen for call in message.get("tool_calls", []))
    assert any(message.get("content") == "current" and message.get("_meta", {}).get("source_task_id") == "current" for message in seen)
