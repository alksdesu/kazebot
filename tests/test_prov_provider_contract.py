"""五个 provider 对同一种上游情况给出同一种答复。

engine 只看 ok / error_kind / finish_reason 决定重试、换渠道、压缩还是交付：哪一家把半截流当成功、
把安全拦截报成可重试、或者干脆抛异常，都会让同一件事在不同渠道上走出不同的结局。
上游一律用构造的 SSE / JSON 模拟，不发真实请求。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers import registry  # noqa: E402
from providers.base import ERROR_KINDS, FINISH_REASONS, ProviderResponse  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]
ALL = ["openai", "deepseek", "openai-responses", "anthropic", "gemini"]
_Handler = Callable[[httpx.Request], httpx.Response]


class _Stream(httpx.AsyncByteStream):
    """先吐出给定的字节，再按需抛一个传输层异常，模拟流半路断掉。"""

    def __init__(self, body: bytes, fail: BaseException | None = None) -> None:
        self.body = body
        self.fail = fail

    async def __aiter__(self):
        if self.body:
            yield self.body
        if self.fail is not None:
            raise self.fail

    async def aclose(self) -> None:
        return None


def sse(*events: Any) -> bytes:
    lines = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


def streaming(body: bytes, fail: BaseException | None = None) -> _Handler:
    return lambda request: httpx.Response(200, stream=_Stream(body, fail))


def answering(status: int, **kwargs: Any) -> _Handler:
    return lambda request: httpx.Response(status, **kwargs)


def call(
    name: str,
    handler: _Handler,
    *,
    stream: bool,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    on_tool_delta: Callable[[dict[str, Any]], Any] | None = None,
    **options: Any,
) -> ProviderResponse:
    async def run() -> ProviderResponse:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = registry.get(name)(
                http=client, api_key="k", base_url="https://relay.test", model="m", provider_options=options or None,
            )
            if stream:
                return await provider.chat_stream(
                    messages=messages or MESSAGES, tools=tools, on_tool_delta=on_tool_delta,
                )
            return await provider.chat(messages=messages or MESSAGES, tools=tools)

    return asyncio.run(run())


def assert_contract(resp: ProviderResponse) -> None:
    assert resp.finish_reason is None or resp.finish_reason in FINISH_REASONS
    if resp.ok:
        assert resp.error_kind is None
    else:
        assert resp.error_kind in ERROR_KINDS
        assert resp.error


def text_event(name: str, text: str) -> dict[str, Any]:
    if name in ("openai", "deepseek"):
        return {"choices": [{"delta": {"content": text}}]}
    if name == "openai-responses":
        return {"type": "response.output_text.delta", "delta": text}
    if name == "anthropic":
        return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def finish_events(name: str, reason: str) -> list[Any]:
    """reason 取 stop / length。"""
    if name in ("openai", "deepseek"):
        return [{"choices": [{"delta": {}, "finish_reason": reason}]}, "[DONE]"]
    if name == "openai-responses":
        if reason == "length":
            return [{"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            }}]
        return [{"type": "response.completed", "response": {"status": "completed"}}]
    if name == "anthropic":
        stop = "max_tokens" if reason == "length" else "end_turn"
        return [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}},
                {"type": "message_stop"}]
    return [{"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS" if reason == "length" else "STOP"}]}]


def body_with_text(name: str, text: str, reason: str = "stop") -> dict[str, Any]:
    if name in ("openai", "deepseek"):
        return {"choices": [{"message": {"content": text}, "finish_reason": reason}]}
    if name == "openai-responses":
        body: dict[str, Any] = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        if reason == "length":
            body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        return body
    if name == "anthropic":
        blocks = [{"type": "text", "text": text}] if text else []
        return {"content": blocks, "stop_reason": "max_tokens" if reason == "length" else "end_turn"}
    parts = [{"text": text}] if text else []
    return {"candidates": [{"content": {"parts": parts}, "finishReason": "MAX_TOKENS" if reason == "length" else "STOP"}]}


@pytest.mark.parametrize("name", ALL)
def test_a_stream_that_breaks_midway_is_incomplete_not_a_success(name: str) -> None:
    resp = call(name, streaming(sse(text_event(name, "half")), httpx.ReadError("connection reset")), stream=True)
    assert_contract(resp)
    assert resp.ok is False
    assert resp.error_kind == "incomplete"
    # 没有收到完整答复，没有状态码可言。
    assert resp.status_code is None
    assert resp.text == "half"
    assert resp.tool_calls == []


@pytest.mark.parametrize("name", ALL)
def test_a_stream_that_just_stops_without_its_terminal_event_is_incomplete(name: str) -> None:
    resp = call(name, streaming(sse(text_event(name, "half"))), stream=True)
    assert_contract(resp)
    assert (resp.ok, resp.error_kind, resp.finish_reason) == (False, "incomplete", "incomplete")


@pytest.mark.parametrize("name", ALL)
def test_a_properly_finished_stream_succeeds(name: str) -> None:
    resp = call(name, streaming(sse(text_event(name, "done"), *finish_events(name, "stop"))), stream=True)
    assert_contract(resp)
    assert resp.ok, resp.error
    assert resp.text == "done"
    assert resp.finish_reason == "stop"


@pytest.mark.parametrize("name", ALL)
def test_an_explicit_done_marker_counts_as_the_end(name: str) -> None:
    # 中转自己补的 [DONE] 是明确的收尾，不是断流。
    resp = call(name, streaming(sse(text_event(name, "done"), "[DONE]")), stream=True)
    assert resp.ok, resp.error


@pytest.mark.parametrize("name", ALL)
def test_truncated_with_content_is_handed_over_marked_length(name: str) -> None:
    resp = call(name, streaming(sse(text_event(name, "partial"), *finish_events(name, "length"))), stream=True)
    assert_contract(resp)
    assert resp.ok is True
    assert resp.finish_reason == "length"
    assert resp.text == "partial"


@pytest.mark.parametrize("name", ALL)
def test_truncated_before_any_content_is_a_failure(name: str) -> None:
    resp = call(name, answering(200, json=body_with_text(name, "", "length")), stream=False)
    assert_contract(resp)
    assert resp.ok is False
    assert resp.finish_reason == "length"


@pytest.mark.parametrize("name", ALL)
def test_non_streaming_truncation_with_content_is_marked(name: str) -> None:
    resp = call(name, answering(200, json=body_with_text(name, "partial", "length")), stream=False)
    assert_contract(resp)
    assert (resp.ok, resp.finish_reason, resp.text) == (True, "length", "partial")


@pytest.mark.parametrize("name", ALL)
def test_a_non_json_200_body_is_an_invalid_response(name: str) -> None:
    resp = call(name, answering(200, content=b"<html>gateway hiccup</html>"), stream=False)
    assert_contract(resp)
    assert (resp.ok, resp.error_kind, resp.status_code) == (False, "invalid_response", 200)


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("stream", [False, True])
def test_a_bug_inside_the_provider_becomes_a_failed_response(name: str, stream: bool, monkeypatch) -> None:
    cls = registry.get(name)
    target = "_build_body" if name == "gemini" else "_build_payload"

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("boom")

    monkeypatch.setattr(cls, target, broken)
    resp = call(name, answering(200, json={}), stream=stream)
    assert_contract(resp)
    assert (resp.ok, resp.error_kind) == (False, "unknown")
    assert "KeyError" in resp.error


@pytest.mark.parametrize("name", ALL)
def test_string_content_parts_are_accepted(name: str) -> None:
    messages = [
        {"role": "system", "content": ["rule a. ", {"type": "text", "text": "rule b."}]},
        {"role": "user", "content": ["hello ", {"type": "text", "text": "world"}]},
    ]
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body_with_text(name, "ok"))

    resp = call(name, handler, stream=False, messages=messages)
    assert resp.ok, resp.error
    sent = json.dumps(seen[0], ensure_ascii=False)
    assert "rule b." in sent and "world" in sent
    # 块数组里的裸字符串折成了 text 块，没有被 str() 成 Python 字面量。
    assert "{'type'" not in sent


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize(("status", "body", "kind"), [
    (429, {"error": {"message": "Rate limit reached for requests", "type": "requests", "code": "rate_limit_exceeded"}}, "rate_limit"),
    (401, {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}, "auth"),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "overloaded"),
    (503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}, "overloaded"),
    (500, "<html>Internal Server Error</html>", "server"),
    (504, "upstream timed out", "timeout"),
    (400, {"error": {"message": "This model's maximum context length is 128000 tokens."}}, "context_length"),
    (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                     "message": "The input token count (1200000) exceeds the maximum number of tokens allowed."}},
     "context_length"),
    (400, {"error": {"message": "upstream key pool: quota exhausted"}}, "rate_limit"),
    (400, {"error": {"message": "Invalid value for field 'foo'"}}, "bad_request"),
    (403, {"error": {"message": "Request was refused by the safeguard"}}, "content_filter"),
    (403, {"error": {"message": "Your organization is not verified"}}, "auth"),
])
def test_http_failures_are_classified_the_same_by_every_provider(name: str, status: int, body: Any, kind: str) -> None:
    payload = {"json": body} if isinstance(body, dict) else {"content": body.encode()}
    for stream in (False, True):
        resp = call(name, answering(status, **payload), stream=stream)
        assert_contract(resp)
        assert (resp.ok, resp.status_code, resp.error_kind) == (False, status, kind), (name, stream, resp.error)


@pytest.mark.parametrize("name", ALL)
def test_a_connection_failure_is_a_network_error(name: str) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    for stream in (False, True):
        resp = call(name, refuse, stream=stream)
        assert (resp.ok, resp.error_kind, resp.status_code) == (False, "network", None)


@pytest.mark.parametrize("name", ALL)
def test_a_timeout_is_a_timeout(name: str) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    resp = call(name, slow, stream=False)
    assert (resp.ok, resp.error_kind) == (False, "timeout")


@pytest.mark.parametrize(("name", "event", "kind"), [
    ("openai", {"error": {"message": "upstream exploded", "code": "server_error"}}, "server"),
    ("deepseek", {"error": {"message": "Request was blocked", "code": "content_filter"}}, "content_filter"),
    ("anthropic", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "overloaded"),
    ("openai-responses", {"type": "response.failed", "response": {
        "status": "failed", "error": {"code": "rate_limit_exceeded", "message": "slow down"}}}, "rate_limit"),
    ("openai-responses", {"type": "error", "code": "server_error", "message": "boom"}, "server"),
    ("gemini", {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}, "overloaded"),
])
def test_an_error_inside_a_200_stream_keeps_its_kind_and_status(name: str, event: dict, kind: str) -> None:
    resp = call(name, streaming(sse(text_event(name, "half"), event)), stream=True)
    assert_contract(resp)
    assert (resp.ok, resp.error_kind, resp.status_code) == (False, kind, 200)
    assert resp.error


@pytest.mark.parametrize(("name", "events"), [
    ("openai", [{"choices": [{"delta": {}, "finish_reason": "content_filter"}]}, "[DONE]"]),
    ("anthropic", [{"type": "message_delta", "delta": {"stop_reason": "refusal"}}, {"type": "message_stop"}]),
    ("gemini", [{"candidates": [{"finishReason": "SAFETY"}]}]),
    ("gemini", [{"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}]),
])
def test_a_blocked_empty_answer_is_a_content_filter_failure(name: str, events: list) -> None:
    resp = call(name, streaming(sse(*events)), stream=True)
    assert_contract(resp)
    assert (resp.ok, resp.error_kind) == (False, "content_filter")
    assert resp.finish_reason in ("content_filter", "refusal")


def test_gemini_non_streaming_safety_block_is_a_content_filter_failure() -> None:
    resp = call("gemini", answering(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), stream=False)
    assert (resp.ok, resp.error_kind, resp.status_code) == (False, "content_filter", 200)


def test_deepseek_resource_shortage_mid_answer_is_not_a_success() -> None:
    events = [text_event("deepseek", "half"), {"choices": [{"delta": {}, "finish_reason": "insufficient_system_resource"}]}, "[DONE]"]
    resp = call("deepseek", streaming(sse(*events)), stream=True)
    assert (resp.ok, resp.error_kind) == (False, "overloaded")


_BROKEN_ARGS = '{"path": "a.t'


def _broken_tool_stream(name: str) -> bytes:
    if name in ("openai", "deepseek"):
        return sse(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                     "function": {"name": "write_file", "arguments": _BROKEN_ARGS}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        )
    if name == "openai-responses":
        return sse(
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "call_id": "c1", "name": "write_file"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": _BROKEN_ARGS},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "call_id": "c1", "name": "write_file", "arguments": _BROKEN_ARGS}},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
    if name == "anthropic":
        return sse(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "c1", "name": "write_file"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": _BROKEN_ARGS}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
    return sse({"candidates": [{"content": {"parts": [{"functionCall": {"name": "write_file", "args": _BROKEN_ARGS}}]},
                                "finishReason": "STOP"}]})


@pytest.mark.parametrize("name", ALL)
def test_unparseable_tool_arguments_arrive_as_raw_text(name: str) -> None:
    resp = call(name, streaming(_broken_tool_stream(name)), stream=True)
    assert resp.ok, resp.error
    [tool] = resp.tool_calls
    assert tool.name == "write_file"
    assert tool.arguments == {"_raw": _BROKEN_ARGS}
    assert resp.finish_reason == "tool_calls"


@pytest.mark.parametrize(("name", "body"), [
    ("openai", {"choices": [{"message": {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": _BROKEN_ARGS}}]},
        "finish_reason": "tool_calls"}]}),
    ("openai-responses", {"output": [{"type": "function_call", "call_id": "c1", "name": "write_file",
                                      "arguments": _BROKEN_ARGS}]}),
    ("anthropic", {"content": [{"type": "tool_use", "id": "c1", "name": "write_file", "input": "not an object"}],
                   "stop_reason": "tool_use"}),
    ("gemini", {"candidates": [{"content": {"parts": [{"functionCall": {"name": "write_file", "args": ["x"]}}]},
                                "finishReason": "STOP"}]}),
])
def test_non_streaming_tool_arguments_that_are_not_objects_arrive_as_raw(name: str, body: dict) -> None:
    resp = call(name, answering(200, json=body), stream=False)
    assert resp.ok, resp.error
    [tool] = resp.tool_calls
    assert set(tool.arguments) == {"_raw"}
    assert isinstance(tool.arguments["_raw"], str)


@pytest.mark.parametrize("name", ALL)
def test_missing_usage_is_none_not_zeros(name: str) -> None:
    resp = call(name, answering(200, json=body_with_text(name, "ok")), stream=False)
    assert resp.ok, resp.error
    assert resp.usage is None


def test_gemini_completion_tokens_include_thinking_like_everyone_else() -> None:
    body = body_with_text("gemini", "ok")
    body["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 7, "totalTokenCount": 22}
    resp = call("gemini", answering(200, json=body), stream=False)
    # 没命中时 Gemini 不出 cachedContentTokenCount，按 0 记
    assert resp.usage == {
        "prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22, "cache_read_tokens": 0, "reasoning_tokens": 7,
    }


def _openai_tool_stream(*deltas: dict[str, Any]) -> bytes:
    events: list[Any] = [{"choices": [{"delta": {"tool_calls": [delta]}}]} for delta in deltas]
    events += [{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]"]
    return sse(*events)


def _calls(resp: ProviderResponse) -> list[tuple[str, str, dict]]:
    return [(tool.id, tool.name, tool.arguments) for tool in resp.tool_calls]


def test_calls_without_index_are_told_apart_by_id() -> None:
    # 审查时实测：这种中转下 read_file 和 write_file 被合成一个，拿空内容覆写了 a.txt。
    body = _openai_tool_stream(
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
        {"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"b.txt","content":"x"}'}},
    )
    resp = call("openai", streaming(body), stream=True)
    assert _calls(resp) == [
        ("c1", "read_file", {"path": "a.txt"}),
        ("c2", "write_file", {"path": "b.txt", "content": "x"}),
    ]


def test_every_call_at_index_zero_with_fragments_is_still_split_by_id() -> None:
    body = _openai_tool_stream(
        {"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": ""}},
        {"index": 0, "function": {"arguments": '{"path":'}},
        {"index": 0, "function": {"arguments": '"a.txt"}'}},
        {"index": 0, "id": "c2", "type": "function", "function": {"name": "list_dir", "arguments": ""}},
        {"index": 0, "function": {"arguments": '{"path":"."}'}},
    )
    events: list[dict[str, Any]] = []

    async def record(event: dict[str, Any]) -> None:
        events.append(event)

    resp = call("openai", streaming(body), stream=True, on_tool_delta=record)
    assert _calls(resp) == [("c1", "read_file", {"path": "a.txt"}), ("c2", "list_dir", {"path": "."})]
    # 前端按 index 归卡片：同一次响应里两个调用的 index 不能撞。
    starts = [event for event in events if event["event"] == "tool_call_start"]
    assert [(event["index"], event["id"]) for event in starts] == [(0, "c1"), (1, "c2")]
    last_delta = [event for event in events if event["event"] == "tool_call_args_delta"][-1]
    assert (last_delta["index"], last_delta["id"]) == (1, "c2")
    assert sum(event["event"] == "tool_call_done" for event in events) == 2


def test_a_null_index_does_not_sink_the_whole_response() -> None:
    body = _openai_tool_stream(
        {"index": None, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
    )
    resp = call("openai", streaming(body), stream=True)
    assert resp.ok, resp.error
    assert _calls(resp) == [("c1", "read_file", {})]


def test_complete_calls_without_id_or_index_are_split_by_name() -> None:
    body = _openai_tool_stream(
        {"type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
        {"type": "function", "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}},
    )
    resp = call("openai", streaming(body), stream=True)
    assert [(name, args) for _id, name, args in _calls(resp)] == [
        ("read_file", {"path": "a.txt"}), ("read_file", {"path": "b.txt"}),
    ]
    ids = [tool_id for tool_id, _name, _args in _calls(resp)]
    assert all(ids) and len(set(ids)) == 2


def test_standard_indexed_fragments_still_merge() -> None:
    body = _openai_tool_stream(
        {"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": ""}},
        {"index": 1, "id": "c2", "type": "function", "function": {"name": "list_dir", "arguments": ""}},
        {"index": 0, "function": {"arguments": '{"path":"a.txt"}'}},
        {"index": 1, "function": {"arguments": '{"path":"."}'}},
    )
    resp = call("openai", streaming(body), stream=True)
    assert _calls(resp) == [("c1", "read_file", {"path": "a.txt"}), ("c2", "list_dir", {"path": "."})]


def test_gemini_call_ids_do_not_restart_at_call_1_every_response() -> None:
    body = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "read_file", "args": {}}}]}, "finishReason": "STOP"}]}
    first = call("gemini", answering(200, json=body), stream=False).tool_calls[0].id
    second = call("gemini", answering(200, json=body), stream=False).tool_calls[0].id
    assert first and second and first != second


def test_gemini_keeps_an_id_the_upstream_gave() -> None:
    body = {"candidates": [{"content": {"parts": [{"functionCall": {"id": "fc-7", "name": "read_file", "args": {}}}]},
                            "finishReason": "STOP"}]}
    assert call("gemini", answering(200, json=body), stream=False).tool_calls[0].id == "fc-7"


def test_gemini_history_maps_tool_results_back_by_the_stored_ids() -> None:
    from providers.gemini import GeminiProvider

    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_ab12", "name": "read_file", "arguments": {}}],
         "_meta": {"metadata": {"gemini": {"raw_parts": [{"functionCall": {"name": "read_file", "args": {}}}]}}}},
        {"role": "tool", "tool_call_id": "call_ab12", "content": "file body"},
    ]
    _system, contents = GeminiProvider._convert_messages(messages)
    response = contents[-1]["parts"][0]["functionResponse"]
    assert response["name"] == "read_file"
