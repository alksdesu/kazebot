"""思维链在「存储 → 发给模型」的每条路径上都得保住，各家要原样带回的字段也一样。

DeepSeek 思考模式带 tools 时，之前每一条 assistant 的 reasoning_content 都要原样传回，少一条整个请求就 400。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    SESSION_ID,
    NodeRun,
    ScriptedRegistry,
    call,
    echo,
    make_loop_state,
    stored,
)
from engine.builtin.plaintext import build_implicit_finish  # noqa: E402
from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.inference import ai_step  # noqa: E402
from engine.inference.llm_call import _build_messages_for_provider, _call_llm_with_retry  # noqa: E402
from engine.inference.loop_state import _reasoning_fields, flush_conversation_writes  # noqa: E402
from engine.inference.message_model import apply_reasoning_content, get_message_meta  # noqa: E402
from engine.inference.tool_format import (  # noqa: E402
    FakeNativeToolFormatter,
    NativeToolFormatter,
    build_llm_messages,
    sanitize_control_tool_history,
)
from engine.node import Node  # noqa: E402
from engine.prompt import finalize_system_tail  # noqa: E402
from engine.runner import _message_to_history_dict, _strip_images_from_content, _strip_trailing_pseudo_call  # noqa: E402
from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402
from providers.deepseek import DeepSeekProvider  # noqa: E402
from providers.gemini import GeminiProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402
from providers.openai_responses import OpenAIResponsesProvider  # noqa: E402

_TOOLS = [{
    "type": "function",
    "function": {"name": "read_file", "description": "read", "parameters": {"type": "object", "properties": {}}},
}]
_MISSING_REASONING = "The `reasoning_content` in the thinking mode must be passed back to the API."


def _assistants(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("role") == "assistant"]


def _deepseek(**options: Any) -> DeepSeekProvider:
    return DeepSeekProvider(api_key="k", provider_options=options)


def _reloaded(workspace: Path, *, task_id: str = "") -> list[dict[str, Any]]:
    """按 runner 的方式从存储重载，再走 native 的 L2，得到交给 provider 的那一份。"""
    history = [
        _message_to_history_dict(message, current_task_id=task_id)
        for message in stored(workspace) if message.role != "system"
    ]
    history = _strip_trailing_pseudo_call(sanitize_control_tool_history(history))
    return build_llm_messages(history, NativeToolFormatter())


def _tool_task(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> NodeRun:
    """一个两步的 native 任务：先读文件、再 finish，两次回复都带思维链。"""
    run = NodeRun(monkeypatch, workspace, registry=ScriptedRegistry(tools={"read_file": echo("hello")}))
    run.run(
        ProviderResponse(ok=True, tool_calls=[call("read_file", call_id="c1")], reasoning="先读文件"),
        ProviderResponse(ok=True, tool_calls=[call("finish", {"text": "完成"}, call_id="f1")], reasoning="读完可以收尾"),
    )
    return run


class TestApplyReasoningContent:
    def test_the_reasoning_in_meta_moves_to_the_top_level(self) -> None:
        message = {"role": "assistant", "content": "a", "_meta": {"reasoning": "why"}}

        assert apply_reasoning_content(message) is message
        assert message["reasoning_content"] == "why"
        assert message["_meta"]["reasoning"] == "why"

    def test_an_existing_top_level_value_wins(self) -> None:
        message = {"role": "assistant", "content": "a", "reasoning_content": "live", "_meta": {"reasoning": "stored"}}

        assert apply_reasoning_content(message)["reasoning_content"] == "live"

    @pytest.mark.parametrize("message", [
        pytest.param({"role": "assistant", "content": "a"}, id="no-meta"),
        pytest.param({"role": "assistant", "content": "a", "_meta": {"reasoning": ""}}, id="empty-reasoning"),
        pytest.param({"role": "user", "content": "q", "_meta": {"reasoning": "why"}}, id="not-assistant"),
        pytest.param({"role": "tool", "content": "r", "_meta": {"reasoning": "why"}}, id="tool-result"),
    ])
    def test_nothing_is_invented(self, message: dict[str, Any]) -> None:
        assert "reasoning_content" not in apply_reasoning_content(dict(message))

    def test_the_legacy_thinking_text_key_is_still_read(self) -> None:
        message = {"role": "assistant", "content": "a", "_meta": {"thinking_text": "old"}}

        assert apply_reasoning_content(message)["reasoning_content"] == "old"


class TestReloadFromStore:
    def test_a_reloaded_turn_has_the_shape_it_had_at_runtime(self, monkeypatch, tmp_path: Path) -> None:
        run = _tool_task(monkeypatch, tmp_path)

        at_runtime = _assistants(run.requests[1]["messages"])
        reloaded = _assistants(_reloaded(tmp_path))

        assert at_runtime[0]["reasoning_content"] == "先读文件"
        assert reloaded[0]["reasoning_content"] == "先读文件"
        assert [message.get("reasoning_content") for message in reloaded] == ["先读文件", "读完可以收尾"]

    def test_the_stored_meta_keeps_the_reasoning_too(self, monkeypatch, tmp_path: Path) -> None:
        _tool_task(monkeypatch, tmp_path)

        stored_assistants = [message for message in stored(tmp_path) if message.role == "assistant"]

        assert [message.meta.get("reasoning") for message in stored_assistants] == ["先读文件", "读完可以收尾"]

    @pytest.mark.parametrize("task_id", [pytest.param("", id="next-task"), pytest.param("task-1", id="resumed-task")])
    def test_the_deepseek_request_carries_every_turns_real_reasoning(
        self, monkeypatch, tmp_path: Path, task_id: str,
    ) -> None:
        _tool_task(monkeypatch, tmp_path)

        payload = _deepseek()._build_payload(_reloaded(tmp_path, task_id=task_id), _TOOLS)

        assert [message["reasoning_content"] for message in _assistants(payload["messages"])] == ["先读文件", "读完可以收尾"]

    def test_a_message_without_reasoning_gets_no_field(self) -> None:
        message = Message(id="a", role="assistant", content="plain", meta={"tool_mode": "native"})

        assert "reasoning_content" not in _message_to_history_dict(message)

    def test_legacy_history_gets_the_placeholder_and_old_keys_still_count(self, tmp_path: Path) -> None:
        ConversationStore(tmp_path / "data" / "conversations").append_batch(SESSION_ID, [
            Message(id="u1", role="user", content="old question"),
            Message(id="a1", role="assistant", content="old answer", meta={"tool_mode": "native"}),
            Message(id="u2", role="user", content="older question"),
            Message(id="a2", role="assistant", content="legacy", meta={"tool_mode": "native", "thinking_text": "老字段"}),
            Message(id="u3", role="user", content="now"),
        ])

        payload = _deepseek()._build_payload(_reloaded(tmp_path), _TOOLS)

        assert [message["reasoning_content"] for message in _assistants(payload["messages"])] == [" ", "老字段"]

    def test_images_from_other_tasks_are_still_stripped(self) -> None:
        content = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "file:///tmp/cat.png"}},
        ]
        message = Message(
            id="a", role="assistant", content=content, meta={"reasoning": "图里有猫"}, source_task_id="old",
        )

        restored = _message_to_history_dict(message, current_task_id="new")

        assert restored["content"] == _strip_images_from_content(content)
        assert restored["reasoning_content"] == "图里有猫"


def _hybrid(ls: Any, resp: ProviderResponse) -> Any:
    return ai_step._handle_plaintext_response(ls, resp, 0)


def _plaintext_retries_exhausted(ls: Any, resp: ProviderResponse) -> Any:
    ls.plaintext_retry_count = ls.plaintext_retry_max
    return ai_step._handle_plaintext_response(ls, resp, 0)


def _unauthorized_tool(ls: Any, resp: ProviderResponse) -> Any:
    return ai_step._build_unauthorized_implicit_finish(ls, resp, resp.text or "", 0)


def _plaintext_hook(ls: Any, resp: ProviderResponse) -> Any:
    return build_implicit_finish(ls, resp, resp.text or "", step=0)


_IMPLICIT_FINISHES = [
    pytest.param(_hybrid, "hybrid", id="hybrid"),
    pytest.param(_plaintext_retries_exhausted, "tool_only", id="plaintext-retries-exhausted"),
    pytest.param(_unauthorized_tool, "tool_only", id="unauthorized-tool"),
    pytest.param(_plaintext_hook, "tool_only", id="plaintext-hook"),
]


class TestImplicitFinishKeepsReasoning:
    """模型直接回文本、被当成隐式 finish 的几条路径，之前把思维链写死成空串。"""

    @pytest.mark.parametrize("finish,output_mode", _IMPLICIT_FINISHES)
    def test_the_reasoning_is_recorded_like_on_a_tool_turn(
        self, tmp_path: Path, finish: Callable[[Any, ProviderResponse], Any], output_mode: str,
    ) -> None:
        node = Node(id="node-1", type="ai", tool_mode="native", output_mode=output_mode)
        ls = make_loop_state(tmp_path, ScriptedRegistry(), node=node)

        action = finish(ls, ProviderResponse(ok=True, text="直接回答", reasoning="想了一下"))
        asyncio.run(flush_conversation_writes(ls.rctx))

        assert action.action == "finish"
        last = ls.messages[-1]
        assert last["reasoning_content"] == "想了一下"
        assert get_message_meta(last).reasoning == "想了一下"
        saved = [message for message in stored(tmp_path) if message.role == "assistant"][-1]
        assert saved.meta.get("reasoning") == "想了一下"
        assert _message_to_history_dict(saved)["reasoning_content"] == "想了一下"

    @pytest.mark.parametrize("finish,output_mode", _IMPLICIT_FINISHES)
    def test_a_reply_without_reasoning_stays_without_it(
        self, tmp_path: Path, finish: Callable[[Any, ProviderResponse], Any], output_mode: str,
    ) -> None:
        node = Node(id="node-1", type="ai", tool_mode="native", output_mode=output_mode)
        ls = make_loop_state(tmp_path, ScriptedRegistry(), node=node)

        finish(ls, ProviderResponse(ok=True, text="直接回答"))

        assert "reasoning_content" not in ls.messages[-1]
        assert get_message_meta(ls.messages[-1]).has_reasoning is False


class _Provider:
    name = "test"
    model = "test"

    def __init__(self, *responses: ProviderResponse) -> None:
        self.responses = list(responses)

    async def chat(self, *, messages: list[dict], tools: object) -> ProviderResponse:
        return self.responses.pop(0)


class TestReasoningTiming:
    def test_each_request_starts_without_the_previous_requests_timing(self, tmp_path: Path) -> None:
        ls = SimpleNamespace(
            messages=[{"role": "user", "content": "hi"}], formatter=None,
            provider=_Provider(ProviderResponse(ok=True, text="done")),
            node=Node(id="entry", type="ai"), openai_tools=[], use_stream=False,
            retry_max=2, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
            rctx=SimpleNamespace(
                workspace_root=tmp_path, task_id="task", begin_llm_request=lambda: "request", emit_event=AsyncMock(),
            ),
            _reasoning_started_iso="2026-01-01T00:00:00+00:00", _reasoning_ended_iso="2026-01-01T00:00:01+00:00",
        )

        assert asyncio.run(_call_llm_with_retry(ls, 0)).ok

        assert ls._reasoning_started_iso == "" and ls._reasoning_ended_iso == ""

    def test_the_fields_come_from_the_response_and_this_requests_timing(self) -> None:
        ls = SimpleNamespace(_reasoning_started_iso="t1", _reasoning_ended_iso="t2")

        assert _reasoning_fields(ls, ProviderResponse(ok=True, reasoning="r")) == {
            "reasoning": "r", "has_reasoning": True, "reasoning_started_at": "t1", "reasoning_ended_at": "t2",
        }
        assert _reasoning_fields(ls, ProviderResponse(ok=True))["has_reasoning"] is False


def _finish_record(text: str) -> str:
    return f'[Tool call history record: finish was executed with args: {json.dumps({"text": text})}]'


class TestTrailingPseudoCallKeepsReasoning:
    def test_a_restored_finish_reply_keeps_its_reasoning_but_not_its_meta(self) -> None:
        last = {
            "role": "assistant", "content": f"先说一句 {_finish_record('结论')}", "reasoning_content": "why",
            "_meta": {"metadata": {"gemini": {"raw_parts": [{"functionCall": {"name": "finish"}}]}}},
        }

        restored = _strip_trailing_pseudo_call([{"role": "user", "content": "q"}, last])[-1]

        assert restored == {"role": "assistant", "content": "先说一句\n\n结论", "reasoning_content": "why"}

    def test_a_record_found_in_a_user_message_gets_no_reasoning(self) -> None:
        last = {"role": "user", "content": _finish_record("结论"), "reasoning_content": "not mine"}

        restored = _strip_trailing_pseudo_call([last])[-1]

        assert restored == {"role": "assistant", "content": "结论"}

    def test_a_trimmed_assistant_keeps_its_reasoning(self) -> None:
        last = {
            "role": "assistant", "reasoning_content": "why",
            "content": '先说一句 [Tool call history record: dispatch_to_helper was executed with args: {}]',
        }

        restored = _strip_trailing_pseudo_call([last])[-1]

        assert restored == {"role": "assistant", "content": "先说一句", "reasoning_content": "why"}


_MIXED_HISTORY = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1", "reasoning_content": "real"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
    {"role": "user", "content": "q3"},
    {"role": "assistant", "content": "a3", "reasoning_content": ""},
    {"role": "user", "content": "q4"},
    {"role": "assistant", "content": "a4", "reasoning_content": "  \n"},
    {"role": "user", "content": "q5"},
]


class TestDeepSeekPayload:
    def test_thinking_with_tools_fills_every_assistant_that_has_no_reasoning(self) -> None:
        payload = _deepseek()._build_payload(_MIXED_HISTORY, _TOOLS)

        assert [message["reasoning_content"] for message in _assistants(payload["messages"])] == ["real", " ", " ", " "]

    @pytest.mark.parametrize("options,tools", [
        pytest.param({}, None, id="thinking-without-tools"),
        pytest.param({"thinking": False}, _TOOLS, id="tools-without-thinking"),
    ])
    def test_nothing_is_filled_when_the_api_does_not_need_it(self, options: dict[str, Any], tools: Any) -> None:
        payload = _deepseek(**options)._build_payload(_MIXED_HISTORY, tools)

        assert [message.get("reasoning_content") for message in _assistants(payload["messages"])] == [
            "real", None, "", "  \n",
        ]

    def test_other_roles_are_left_alone(self) -> None:
        payload = _deepseek()._build_payload(_MIXED_HISTORY, _TOOLS)

        assert all("reasoning_content" not in message for message in payload["messages"] if message["role"] != "assistant")


def _rejection(message: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": message}})


def _openai_ok() -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    })


class _ScriptedClient:
    """按脚本逐个返回响应，并记下每次实际发出去的请求体。"""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.sent: list[dict] = []

    async def post(self, _url: str, **kwargs) -> httpx.Response:
        self.sent.append(kwargs["json"])
        if not self._responses:
            raise AssertionError(f"第 {len(self.sent)} 次请求没有预备响应，说明重试次数超了预期")
        return self._responses.pop(0)


_HISTORY = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "a", "reasoning_content": "why"},
    {"role": "user", "content": "go on"},
]


def _sent_reasoning(body: dict[str, Any]) -> list[Any]:
    return [message.get("reasoning_content") for message in _assistants(body["messages"])]


class TestDeepSeekDegrade:
    def test_a_missing_reasoning_rejection_does_not_switch_thinking_off(self) -> None:
        provider = _deepseek()

        assert provider._degrade(_MISSING_REASONING) is False
        assert provider._options.get("thinking") is True

    def test_an_endpoint_without_thinking_support_still_gets_it_switched_off(self) -> None:
        provider = _deepseek()

        assert provider._degrade("thinking is not supported for this model") is True
        assert provider._options.get("thinking") is False

    def test_the_rejection_is_reported_instead_of_retried_without_thinking(self) -> None:
        client = _ScriptedClient(_rejection(_MISSING_REASONING))
        provider = DeepSeekProvider(http=client, api_key="k")

        result = asyncio.run(provider.chat(messages=_HISTORY, tools=_TOOLS))

        assert not result.ok
        assert len(client.sent) == 1
        assert client.sent[0]["thinking"] == {"type": "enabled"}
        assert provider._options.get("thinking") is True

    def test_deepseek_never_offers_to_stop_replaying_reasoning(self) -> None:
        assert "replay_reasoning" not in {spec.key for spec in DeepSeekProvider.OPTIONS}
        assert "不回传思维链" not in {rule.name for rule in DeepSeekProvider.DEGRADE_RULES}

    def test_a_strict_looking_rejection_never_strips_deepseeks_reasoning(self) -> None:
        provider = _deepseek()

        provider._degrade("messages.1.reasoning_content: Extra inputs are not permitted")

        assert _sent_reasoning(provider._build_payload(_HISTORY, _TOOLS)) == ["why"]


def _openai(client: _ScriptedClient, **options: Any) -> OpenAIProvider:
    return OpenAIProvider(http=client, api_key="k", base_url="https://x/v1", model="m", provider_options=options)


class TestOpenAIReplay:
    def test_reasoning_is_replayed_by_default(self) -> None:
        client = _ScriptedClient(_openai_ok())

        assert asyncio.run(_openai(client).chat(messages=_HISTORY, tools=None)).ok
        assert _sent_reasoning(client.sent[0]) == ["why"]

    @pytest.mark.parametrize("error", [
        pytest.param("messages.1.reasoning_content: Extra inputs are not permitted", id="pydantic-extra-forbidden"),
        pytest.param("property 'messages.1.assistant.reasoning_content' is unsupported", id="unsupported-property"),
        pytest.param("unrecognizedProperty: messages[1].reasoning_content", id="unrecognized-property"),
        pytest.param("Additional properties are not allowed ('reasoning_content' was unexpected)", id="json-schema"),
    ])
    def test_a_strict_endpoint_gets_it_turned_off_after_one_rejection(self, error: str) -> None:
        client = _ScriptedClient(_rejection(error), _openai_ok(), _openai_ok())
        provider = _openai(client)

        assert asyncio.run(provider.chat(messages=_HISTORY, tools=None)).ok
        assert asyncio.run(provider.chat(messages=_HISTORY, tools=None)).ok

        assert _sent_reasoning(client.sent[0]) == ["why"]
        assert _sent_reasoning(client.sent[1]) == [None]
        assert _sent_reasoning(client.sent[2]) == [None]

    @pytest.mark.parametrize("error", [
        pytest.param(_MISSING_REASONING, id="deepseek-must-pass-back"),
        pytest.param("thinking is enabled but reasoning_content is missing in assistant tool call message at index 1",
                     id="kimi-missing"),
        pytest.param("reasoning_content is required when thinking is enabled", id="required"),
    ])
    def test_an_endpoint_that_wants_reasoning_keeps_getting_it(self, error: str) -> None:
        client = _ScriptedClient(_rejection(error))
        provider = _openai(client)

        assert not asyncio.run(provider.chat(messages=_HISTORY, tools=None)).ok
        assert len(client.sent) == 1
        assert provider._options.get("replay_reasoning") is True

    def test_replay_can_be_switched_off_up_front(self) -> None:
        client = _ScriptedClient(_openai_ok())

        assert asyncio.run(_openai(client, replay_reasoning=False).chat(messages=_HISTORY, tools=None)).ok
        assert _sent_reasoning(client.sent[0]) == [None]


def _anthropic() -> AnthropicProvider:
    return AnthropicProvider(http=None, api_key="k", base_url=None, model="c")


_THINKING_BLOCK = {"type": "thinking", "thinking": "先读文件。", "signature": "sig-1"}
_ANTHROPIC_TOOL_LOOP = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "看一下 a.txt"},
    {
        "role": "assistant", "content": "", "reasoning_content": "先读文件。",
        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}],
        "_meta": {
            "tool_mode": "native", "provider": "anthropic", "reasoning": "先读文件。",
            "metadata": {"anthropic": {"thinking_blocks": [_THINKING_BLOCK]}},
        },
    },
    {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "hello", "_meta": {"tool_mode": "native"}},
]


class TestAnthropicThinkingReplay:
    def test_a_native_tool_loop_sends_the_signed_thinking_block_back(self) -> None:
        provider = _anthropic()

        payload = provider._build_payload(_build_messages_for_provider(_ANTHROPIC_TOOL_LOOP, NativeToolFormatter(), provider))

        assistant = _assistants(payload["messages"])[0]
        assert assistant["content"][0] == _THINKING_BLOCK
        assert assistant["content"][1]["type"] == "tool_use"
        assert assistant["content"][1]["input"] == {"path": "a.txt"}

    def test_fake_native_still_goes_through_l2(self) -> None:
        provider = _anthropic()

        messages = _build_messages_for_provider(_ANTHROPIC_TOOL_LOOP, FakeNativeToolFormatter(), provider)

        assert all("_meta" not in message for message in messages)

    def test_without_thinking_blocks_the_request_is_what_l2_used_to_build(self) -> None:
        history = [
            {"role": "system", "content": "sys", "_system_tail": "tail"},
            {"role": "user", "content": "看一下 a.txt"},
            {
                "role": "assistant", "content": "我先读。",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}],
                "_meta": {"tool_mode": "native"},
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "hello", "_meta": {"tool_mode": "native"}},
            {"role": "user", "content": "动态上下文", "_dynamic": True},
            {"role": "user", "content": "重试提示", "_ephemeral": True},
            {"role": "assistant", "content": "读完了。", "_meta": {"tool_mode": "native"}},
            {"role": "user", "content": "继续"},
        ]
        provider = _anthropic()

        bypass = provider._build_payload(_build_messages_for_provider(history, NativeToolFormatter(), provider))
        through_l2 = provider._build_payload(build_llm_messages(finalize_system_tail(history), NativeToolFormatter()))

        assert bypass == through_l2


class TestOtherProvidersIgnoreTheField:
    """Gemini、Anthropic、Responses 都按自己的格式逐键组装，顶层 reasoning_content 不会被发出去。"""

    def test_gemini(self) -> None:
        body = GeminiProvider(http=None, api_key="k", base_url=None, model="g")._build_body(_HISTORY, None)

        assert "reasoning_content" not in json.dumps(body, ensure_ascii=False)

    def test_anthropic(self) -> None:
        assert "reasoning_content" not in json.dumps(_anthropic()._build_payload(_HISTORY), ensure_ascii=False)

    def test_responses(self) -> None:
        _instructions, items = OpenAIResponsesProvider._convert_messages(_HISTORY)

        assert "reasoning_content" not in json.dumps(items, ensure_ascii=False)
