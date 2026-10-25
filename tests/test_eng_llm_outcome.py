"""模型响应怎么处理：按 error_kind / finish_reason 决定重试、换渠道、被动压缩；截断的回复和解析不了参数的调用都不当真。"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from engine.builtin.plaintext import TRUNCATED_NOTICE, PlaintextRetryHandler
from engine.context import RunContext
from engine.conversation_store import ConversationStore
from engine.hooks.registry import HookRegistry
from engine.inference import ai_step
from engine.inference.llm_call import _call_llm_with_retry
from engine.inference.llm_outcome import (
    is_context_overflow,
    is_filtered,
    is_retryable,
    is_truncated,
    with_changes,
)
from engine.inference.tool_format import JsonToolFormatter, build_llm_messages, create_tool_formatter
from engine.node import Node, ToolAccess
from providers.base import ProviderResponse, ToolCall


def _failed(status: int | None, error: str = "failed", kind: str | None = None) -> ProviderResponse:
    return ProviderResponse(ok=False, error=error, status_code=status, error_kind=kind)


@pytest.mark.parametrize("kind", ["rate_limit", "overloaded", "timeout", "network", "server", "incomplete", "invalid_response"])
def test_transient_kinds_are_retried_on_the_same_channel(kind: str) -> None:
    assert is_retryable(_failed(400, kind=kind)) is True


@pytest.mark.parametrize("kind", ["auth", "bad_request", "content_filter", "context_length", "unknown"])
def test_definitive_kinds_are_not_retried_even_with_a_5xx_status(kind: str) -> None:
    assert is_retryable(_failed(503, kind=kind)) is False


@pytest.mark.parametrize("status, error, expected", [
    (None, "connection reset", True),
    (429, "slow down", True),
    (502, "bad gateway", True),
    (400, "insufficient quota on upstream key pool", True),
    (400, "invalid parameter", False),
    (200, "stream ended: overloaded", True),
    (200, "stream ended", False),
])
def test_legacy_responses_without_a_kind_keep_the_status_rules(status: int | None, error: str, expected: bool) -> None:
    assert is_retryable(_failed(status, error)) is expected


def test_context_overflow_follows_the_kind_first() -> None:
    assert is_context_overflow(_failed(400, "anything", "context_length")) is True
    assert is_context_overflow(_failed(400, "prompt too long", "rate_limit")) is False
    assert is_context_overflow(_failed(413, "payload")) is True
    assert is_context_overflow(_failed(400, "maximum context length exceeded")) is True


def test_finish_reasons_mark_truncated_and_filtered_replies() -> None:
    assert is_truncated(ProviderResponse(ok=True, text="cut", finish_reason="length"))
    assert is_truncated(ProviderResponse(ok=True, text="cut", finish_reason="incomplete"))
    assert not is_truncated(ProviderResponse(ok=True, text="done", finish_reason="stop"))
    assert is_filtered(ProviderResponse(ok=True, text="", finish_reason="content_filter"))
    assert not is_truncated(SimpleNamespace(ok=True))


def test_with_changes_keeps_the_fields_it_does_not_touch() -> None:
    original = ProviderResponse(ok=True, text="raw", finish_reason="length", provider_meta={"id": "x"}, usage={"prompt_tokens": 3})
    changed = with_changes(original, text="clean")
    assert changed.text == "clean" and original.text == "raw"
    assert changed.finish_reason == "length" and changed.provider_meta == {"id": "x"} and changed.usage == {"prompt_tokens": 3}


class _Provider:
    name = "test"
    model = "test"

    def __init__(self, *responses: ProviderResponse) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    async def chat(self, *, messages: list[dict], tools: object) -> ProviderResponse:
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


def _retry_loop(tmp_path: Path, provider: _Provider, messages: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        messages=messages or [{"role": "user", "content": "hi"}], formatter=None, provider=provider,
        node=Node(id="entry", type="ai"), openai_tools=[], use_stream=False,
        retry_max=2, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
        rctx=SimpleNamespace(workspace_root=tmp_path, task_id="task", begin_llm_request=lambda: "request", emit_event=AsyncMock()),
    )


def test_a_rate_limited_call_is_retried(tmp_path: Path) -> None:
    provider = _Provider(_failed(429, "slow down", "rate_limit"), ProviderResponse(ok=True, text="done"))
    response = asyncio.run(_call_llm_with_retry(_retry_loop(tmp_path, provider), 0))
    assert response.ok and len(provider.requests) == 2


def test_an_auth_failure_is_returned_without_retrying(tmp_path: Path) -> None:
    provider = _Provider(_failed(401, "bad key", "auth"))
    response = asyncio.run(_call_llm_with_retry(_retry_loop(tmp_path, provider), 0))
    assert not response.ok and response.error_kind == "auth"
    assert len(provider.requests) == 1


def test_a_blocked_empty_reply_becomes_a_content_filter_failure_without_retries(tmp_path: Path) -> None:
    provider = _Provider(ProviderResponse(ok=True, text="", finish_reason="content_filter"))
    response = asyncio.run(_call_llm_with_retry(_retry_loop(tmp_path, provider), 0))
    assert not response.ok and response.error_kind == "content_filter"
    assert len(provider.requests) == 1


def test_context_overflow_retries_once_with_the_latest_round_cut_as_well(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read-1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "read-1", "name": "read_file", "content": "x" * 20_000},
    ]
    provider = _Provider(_failed(400, "too long", "context_length"), ProviderResponse(ok=True, text="done"))
    loop = _retry_loop(tmp_path, provider, messages)

    response = asyncio.run(_call_llm_with_retry(loop, 0))

    assert response.ok and len(provider.requests) == 2
    first, second = (len(request[-1]["content"]) for request in provider.requests)
    assert second < first < 20_000
    loop.rctx.emit_event.assert_any_await("reactive_compact", ANY)
    assert len(messages[-1]["content"]) == 20_000


def test_context_overflow_without_anything_left_to_cut_is_not_resent(tmp_path: Path) -> None:
    provider = _Provider(_failed(400, "too long", "context_length"))
    response = asyncio.run(_call_llm_with_retry(_retry_loop(tmp_path, provider), 0))
    assert not response.ok and len(provider.requests) == 1


@pytest.fixture()
def loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    hooks = HookRegistry()
    hooks.register("before_response", PlaintextRetryHandler())
    monkeypatch.setattr(ai_step, "hook_registry", hooks)
    monkeypatch.setattr(ai_step, "auto_discover_and_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *args: None)
    monkeypatch.setattr(ai_step, "load_runtime_config", lambda *args: {"engine": {"max_steps": 6}})
    rctx = RunContext(tmp_path, "http://localhost:0", "session", "worker", AsyncMock(), AsyncMock(), task_id="current")
    rctx.emit_event = AsyncMock()
    rctx.check_preempted = AsyncMock(return_value={"preempted": False})
    rctx.check_cancelled = AsyncMock(return_value=False)
    rctx.conversation_store = ConversationStore(tmp_path / "conversations")
    node = Node(id="entry", type="ai", prompt="Speak", tool_mode="native", output_mode="hybrid",
                tool_access=ToolAccess(mode="all"))
    registry = SimpleNamespace(list_specs=lambda: [{"name": "safe_tool", "description": "test", "input_schema": {"type": "object"}}])
    snapshots: list[list[dict]] = []

    async def run(*responses: ProviderResponse):
        queue = list(responses)

        async def provider_call(ls, step):
            snapshots.append(copy.deepcopy(ls.messages))
            return queue.pop(0)

        monkeypatch.setattr(ai_step, "_call_llm_with_retry", provider_call)
        return await ai_step.run_ai_node(rctx=rctx, provider=SimpleNamespace(name="test"), registry=registry,
                                        node=node, instruction="current", history=[])

    return SimpleNamespace(run=run, rctx=rctx, snapshots=snapshots)


def _answer_to(messages: list[dict], call_id: str) -> dict:
    index = next(
        position for position, message in enumerate(messages)
        if any(call.get("id") == call_id for call in message.get("tool_calls") or [])
    )
    return messages[index + 1]


_FINISH = ProviderResponse(ok=True, tool_calls=[ToolCall(id="fin", name="finish", arguments={"text": "ok"})])


def test_a_call_with_unparseable_arguments_is_answered_but_never_executed(loop, monkeypatch) -> None:
    execute = AsyncMock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(ai_step, "_execute_real_tools", execute)
    broken = ProviderResponse(ok=True, tool_calls=[ToolCall(id="bad", name="safe_tool", arguments={"_raw": '{"path": "a'})])

    result = asyncio.run(loop.run(broken, _FINISH))

    assert result.action == "finish"
    execute.assert_not_called()
    answer = _answer_to(loop.snapshots[1], "bad")
    assert answer["role"] == "tool" and answer["tool_call_id"] == "bad"
    assert "参数不是合法 JSON" in answer["content"]
    assert loop.rctx.tool_call_log == []


def test_an_unauthorized_call_is_answered_after_the_assistant_turn_and_survives_pairing(loop) -> None:
    unauthorized = ProviderResponse(ok=True, tool_calls=[ToolCall(id="other", name="other_tool", arguments={})])

    asyncio.run(loop.run(unauthorized, _FINISH))

    history = loop.snapshots[1]
    answer = _answer_to(history, "other")
    assert answer["tool_call_id"] == "other"
    assert "not in this node's allowed tool list" in answer["content"]
    replayed = build_llm_messages(history, create_tool_formatter("native"))
    assert any(message.get("tool_call_id") == "other" for message in replayed)


def test_a_reply_still_truncated_after_the_retries_is_delivered_marked_as_cut_off(loop) -> None:
    cut = ProviderResponse(ok=True, text="写到一半的回", finish_reason="length")

    result = asyncio.run(loop.run(cut, cut, cut))

    assert result.action == "finish"
    assert result.result["truncated"] is True
    assert result.result["text"] == f"写到一半的回\n\n{TRUNCATED_NOTICE}"
    assert sum(1 for message in loop.snapshots[-1] if message.get("_retry_hint")) == 2


def test_a_retry_after_truncation_delivers_the_complete_reply(loop) -> None:
    cut = ProviderResponse(ok=True, text="写到一半的回", finish_reason="length")
    full = ProviderResponse(ok=True, text="完整的回复。", finish_reason="stop")

    result = asyncio.run(loop.run(cut, full))

    assert result.action == "finish" and result.result["text"] == "完整的回复。"
    assert any(message.get("_retry_hint") and "cut off" in message["content"] for message in loop.snapshots[1])


def test_tool_calls_from_a_truncated_turn_run_and_the_model_is_told_it_was_cut(loop) -> None:
    cut = ProviderResponse(
        ok=True, finish_reason="length",
        tool_calls=[ToolCall(id="say", name="reply", arguments={"text": "先说一句"})],
    )

    result = asyncio.run(loop.run(cut, _FINISH))

    assert result.action == "finish"
    loop.rctx.emit_event.assert_any_await("intermediate_reply", ANY)
    assert any(message.get("_retry_hint") for message in loop.snapshots[1])


class TestJsonModeParsing:
    def test_json_without_protocol_markers_is_not_turned_into_a_call(self) -> None:
        response = ProviderResponse(ok=True, text='示例：{"name": "finish", "arguments": {"text": "hi"')
        assert JsonToolFormatter().parse_tool_calls(response) == []

    def test_a_truncated_reply_only_yields_its_closed_blocks(self) -> None:
        text = (
            '<<<TOOL_CALL>>>\n{"name": "reply", "arguments": {"text": "one"}}\n<<<END_TOOL_CALL>>>\n'
            '<<<TOOL_CALL>>>\n{"name": "finish", "arguments": {"text": "cut of'
        )
        calls = JsonToolFormatter().parse_tool_calls(ProviderResponse(ok=True, text=text, finish_reason="length"))
        assert [(call.name, call.arguments) for call in calls] == [("reply", {"text": "one"})]

    def test_a_truncated_reply_with_only_an_open_block_yields_nothing(self) -> None:
        text = '<<<TOOL_CALL>>>\n{"name": "finish", "arguments": {"text": "cut of'
        assert JsonToolFormatter().parse_tool_calls(ProviderResponse(ok=True, text=text, finish_reason="length")) == []
