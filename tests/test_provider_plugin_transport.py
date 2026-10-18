from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_runtime
from plugin_sdk import PluginExecutionError
from providers import AnthropicProvider, GeminiProvider, OpenAIProvider, OpenAIResponsesProvider
from providers.deepseek import DeepSeekProvider
from providers.plugin_transport import iter_sse_events, wrap_client


MESSAGES = [{"role": "user", "content": "hello"}]
PROVIDERS = [OpenAIProvider, OpenAIResponsesProvider, AnthropicProvider, GeminiProvider, DeepSeekProvider]


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, wait: asyncio.Event | None = None):
        self.body = body
        self.wait = wait
        self.closed = 0

    async def __aiter__(self):
        for byte in self.body:
            yield bytes([byte])
        if self.wait is not None:
            self.wait.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed += 1


class Session:
    active = True

    def __init__(self, *, provider, streaming, fail_phase=None):
        self.provider = provider
        self.streaming = streaming
        self.fail_phase = fail_phase
        self.requests = []
        self.responses = []
        self.events = []
        self.outcome = {"ok": True, "cancelled": False}
        self.ended = 0
        self.state = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.ended += 1
        if exc is not None:
            self.set_outcome(ok=False, cancelled=isinstance(exc, asyncio.CancelledError))

    def set_outcome(self, **values):
        self.outcome.update(values)

    async def request(self, wire):
        assert not self.state
        self.state["seen"] = True
        self.requests.append(copy.deepcopy(wire))
        wire["headers"]["x-plugin"] = "applied"
        wire["body"]["plugin_marker"] = True
        return wire

    async def response(self, wire):
        self.responses.append(copy.deepcopy(wire))
        wire["body"] = json.loads(json.dumps(wire["body"]).replace("raw", "changed"))
        return wire

    async def stream_event(self, event):
        self.events.append(copy.deepcopy(event))
        if self.fail_phase == "stream":
            raise PluginExecutionError("test", "stream_event", "ValueError")
        if event["event"] == "drop":
            return []
        event["data"] = event["data"].replace("raw", "changed")
        if event["event"] == "double":
            return [event, copy.deepcopy(event)]
        return [event]


@pytest.fixture
def sessions(monkeypatch):
    created = []

    def factory(**kwargs):
        session = Session(**kwargs)
        created.append(session)
        return session

    monkeypatch.setattr(plugin_runtime, "provider_request", factory)
    return created


def response_body(provider):
    if issubclass(provider, OpenAIProvider):
        return {"choices": [{"message": {"content": "raw"}, "finish_reason": "stop"}]}
    if provider is OpenAIResponsesProvider:
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "raw"}]}]}
    if provider is AnthropicProvider:
        return {"content": [{"type": "text", "text": "raw"}]}
    return {"candidates": [{"content": {"parts": [{"text": "raw"}]}}]}


def text_event(provider, text="raw你好"):
    if issubclass(provider, OpenAIProvider):
        return {"choices": [{"delta": {"content": text}}]}
    if provider is OpenAIResponsesProvider:
        return {"type": "response.output_text.delta", "delta": text}
    if provider is AnthropicProvider:
        return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def sse(body, *, event="double", event_id="event-1"):
    encoded = json.dumps(body, ensure_ascii=False, indent=2)
    lines = [": heartbeat", f"event:{event}", f"id:{event_id}", "retry:1000"]
    lines.extend("data:" + line for line in encoded.splitlines())
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def make_provider(provider, client, **options):
    return provider(http=client, api_key="secret-key", base_url="https://example.test", model="test-model", provider_options=options)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_nonstream_request_and_response_transform_before_parsing(provider, sessions):
    async def run():
        observed = []

        def handler(request):
            observed.append(request)
            return httpx.Response(200, json=response_body(provider), headers={"x-upstream": "yes"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await make_provider(provider, client).chat(messages=MESSAGES, tools=None)
        assert result.ok, result.error
        assert result.text == "changed"
        assert observed[0].headers["x-plugin"] == "applied"
        assert json.loads(observed[0].content)["plugin_marker"] is True
        assert sessions[0].responses[0]["headers"]["x-upstream"] == "yes"
        assert sessions[0].ended == 1

    asyncio.run(run())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stream_transform_updates_callbacks_and_result(provider, sessions):
    async def run():
        source = ChunkStream(sse(text_event(provider)) + sse(text_event(provider, "discard"), event="drop") + b"data:[DONE]\r\n\r\n")
        callbacks = []

        async def on_text(text):
            callbacks.append(text)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            result = await make_provider(provider, client).chat_stream(messages=MESSAGES, on_text=on_text)
        assert result.ok, result.error
        assert result.text == "changed你好changed你好"
        assert callbacks == ["changed你好", "changed你好"]
        assert source.closed == 1
        assert sessions[0].ended == 1
        assert sessions[0].events[0]["id"] == "event-1"
        assert sessions[0].events[0]["retry"] == 1000
        assert sessions[0].events[-1]["data"] == "[DONE]"

    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_parameter_degradation_creates_fresh_attempts(streaming, sessions):
    async def run():
        sent = []
        sources = []

        def handler(request):
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx.Response(400, json={"error": {"message": "Unrecognized request argument supplied: max_completion_tokens"}})
            if streaming:
                source = ChunkStream(sse(text_event(OpenAIProvider), event="text") + b"data: [DONE]\n\n")
                sources.append(source)
                return httpx.Response(200, stream=source)
            return httpx.Response(200, json=response_body(OpenAIProvider))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(OpenAIProvider, client, max_completion_tokens=128)
            result = await (provider.chat_stream(messages=MESSAGES) if streaming else provider.chat(messages=MESSAGES, tools=None))
        assert result.ok, result.error
        assert len(sent) == len(sessions) == 2
        assert sent[0]["max_completion_tokens"] == 128
        assert sent[1]["max_tokens"] == 128
        assert all(session.ended == 1 for session in sessions)
        assert sessions[0].outcome["ok"] is False
        assert sessions[1].outcome["ok"] is True
        assert all(source.closed == 1 for source in sources)

    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_responses_encrypted_retry_is_intercepted(streaming, sessions):
    async def run():
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx.Response(400, json={"error": {"message": "encrypted_content could not be verified"}})
            if streaming:
                return httpx.Response(200, stream=ChunkStream(sse(text_event(OpenAIResponsesProvider), event="text") + b"data: [DONE]\n\n"))
            return httpx.Response(200, json=response_body(OpenAIResponsesProvider))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(OpenAIResponsesProvider, client)
            result = await (provider.chat_stream(messages=MESSAGES) if streaming else provider.chat(messages=MESSAGES, tools=None))
        assert result.ok, result.error
        assert len(sent) == len(sessions) == 2
        assert all(session.ended == 1 and len(session.requests) == 1 for session in sessions)

    asyncio.run(run())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_cancellation_closes_stream_and_request_context(provider, sessions):
    async def run():
        waiting = asyncio.Event()
        source = ChunkStream(sse(text_event(provider), event="text"), wait=waiting)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            task = asyncio.create_task(make_provider(provider, client).chat_stream(messages=MESSAGES))
            await asyncio.wait_for(waiting.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert source.closed == 1
        assert sessions[0].ended == 1
        assert sessions[0].outcome["cancelled"] is True

    asyncio.run(run())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stream_plugin_error_fails_and_closes(provider, monkeypatch):
    session_list = []

    def factory(**kwargs):
        session = Session(**kwargs, fail_phase="stream")
        session_list.append(session)
        return session

    monkeypatch.setattr(plugin_runtime, "provider_request", factory)

    async def run():
        source = ChunkStream(sse(text_event(provider)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            with pytest.raises(PluginExecutionError, match="Plugin test") as error:
                await make_provider(provider, client).chat_stream(messages=MESSAGES)
        assert "secret-key" not in str(error.value)
        assert source.closed == 1
        assert session_list[0].ended == 1

    asyncio.run(run())


def test_sse_boundaries_empty_data_bom_ids_and_final_event():
    async def run():
        source = ChunkStream("\ufeff:id\rid: first\rdata: 你好\rdata: next\r\rid: bad\x00id\ndata:\n\nevent:last\ndata:tail".encode())
        response = httpx.Response(200, stream=source)
        events = [event async for event in iter_sse_events(response)]
        assert events == [
            {"event": "", "data": "你好\nnext", "id": "first", "retry": None},
            {"event": "", "data": "", "id": "first", "retry": None},
            {"event": "last", "data": "tail", "id": "first", "retry": None},
        ]

    asyncio.run(run())


def test_error_response_rewritten_to_success_releases_send_lifetime(sessions, monkeypatch):
    original = Session.response

    async def response(self, wire):
        result = await original(self, wire)
        result["status_code"] = 200
        return result

    monkeypatch.setattr(Session, "response", response)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "raw"}))) as client:
            wrapped = wrap_client(client, "test")
            result = await wrapped.send(wrapped.build_request("POST", "https://example.test", json={}), stream=True)
            assert result.status_code == 200
            assert result.json() == {"error": "changed"}
            assert sessions[0].ended == 1

    asyncio.run(run())


@pytest.mark.parametrize("provider", [OpenAIProvider, GeminiProvider])
def test_cancel_during_callback_releases_send_lifetime(provider, sessions):
    async def run():
        callback_started = asyncio.Event()
        source = ChunkStream(sse(text_event(provider), event="text"))

        async def on_text(text):
            callback_started.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            task = asyncio.create_task(make_provider(provider, client).chat_stream(messages=MESSAGES, on_text=on_text))
            await asyncio.wait_for(callback_started.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert source.closed == 1
        assert sessions[0].ended == 1
        assert sessions[0].outcome["cancelled"] is True

    asyncio.run(run())


def test_disabled_plugins_preserve_wire_request_and_response():
    async def run():
        observed = []

        def handler(request):
            observed.append((request.content, dict(request.headers)))
            return httpx.Response(200, json=response_body(OpenAIProvider))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(OpenAIProvider, client)
            result = await provider.chat(messages=MESSAGES, tools=None)
            await client.post("https://example.test/v1/chat/completions", headers=provider._headers(), json=provider._build_payload(MESSAGES, None), timeout=provider._timeout)
        assert result.text == "raw"
        assert observed[0] == observed[1]

    asyncio.run(run())


def test_real_plugin_sdk_context_isolated_across_concurrent_calls(tmp_path):
    from plugin_runtime.store import PluginStore

    source = '''
PLUGIN_META = {"name": "probe"}
records = []

def setup(api):
    @api.before_request
    def request(ctx, wire):
        assert not ctx.state
        ctx.state["request_id"] = ctx.request_id
        wire["headers"]["x-plugin-task"] = ctx.task_id
        records.append(("request", ctx.task_id, ctx.channel, ctx.provider, ctx.request_id))

    @api.after_response
    async def response(ctx, wire):
        assert ctx.state["request_id"] == ctx.request_id
        wire["body"]["choices"][0]["message"]["content"] = ctx.task_id

    @api.on_request_end
    def end(ctx, outcome):
        records.append(("end", ctx.task_id, outcome["ok"]))
'''
    store = PluginStore(tmp_path)
    item = store.install("probe.py", source)
    store.configure("probe", enabled=True, config={}, selectors={"channels": ["selected"]}, base_hash=item["desired_hash"])

    async def run():
        tasks = []

        async def handler(request):
            await asyncio.sleep(0)
            tasks.append(request.headers["x-plugin-task"])
            return httpx.Response(200, json=response_body(OpenAIProvider))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(OpenAIProvider, client)

            async def call(task_id):
                with plugin_runtime.plugin_scope(workspace_root=tmp_path, channel="selected", task_id=task_id):
                    return await provider.chat(messages=MESSAGES, tools=None)

            results = await asyncio.gather(call("alpha"), call("beta"))
        assert [result.text for result in results] == ["alpha", "beta"]
        assert sorted(tasks) == ["alpha", "beta"]

    try:
        asyncio.run(run())
        manager = plugin_runtime.get_manager(tmp_path)
        records = manager._versions["probe"].module.records
        requests = [record for record in records if record[0] == "request"]
        assert len({record[4] for record in requests}) == 2
        assert all(record[2:4] == ("selected", "openai") for record in requests)
        assert sorted(record[1:] for record in records if record[0] == "end") == [("alpha", True), ("beta", True)]
        assert manager.status()["plugins"][0]["active_requests"] == 0
    finally:
        plugin_runtime.get_manager(tmp_path).close()


def test_reasoning_tool_arguments_and_usage_share_transformed_stream(sessions):
    async def run():
        reasoning = []
        tools = []
        event = {"choices": [{"delta": {
            "reasoning_content": "raw reasoning",
            "tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "lookup", "arguments": '{"value":"raw"}'}}],
        }}]}
        usage = {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        source = ChunkStream(sse(event, event="text") + sse(usage, event="usage") + b"data: [DONE]\n\n")

        async def on_thinking(text):
            reasoning.append(text)

        async def on_tool_delta(delta):
            tools.append(delta)

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            result = await make_provider(OpenAIProvider, client).chat_stream(messages=MESSAGES, on_thinking=on_thinking, on_tool_delta=on_tool_delta)
        assert result.ok
        assert result.reasoning == "changed reasoning" == "".join(reasoning)
        assert result.tool_calls[0].arguments == {"value": "changed"}
        assert next(item["delta"] for item in tools if item["event"] == "tool_call_args_delta") == '{"value":"changed"}'
        assert result.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    asyncio.run(run())


def test_responses_terminal_metadata_is_transformed(sessions):
    async def run():
        completed = {"type": "response.completed", "response": {"output": [{"type": "reasoning", "encrypted_content": "raw"}], "usage": {"input_tokens": 4, "output_tokens": 2}}}
        source = ChunkStream(sse(text_event(OpenAIResponsesProvider), event="text") + sse(completed, event="response.completed"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source))) as client:
            result = await make_provider(OpenAIResponsesProvider, client).chat_stream(messages=MESSAGES)
        assert result.ok
        assert result.provider_meta["raw_output"][0]["encrypted_content"] == "changed"
        assert result.usage == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        assert sessions[0].events[-1]["event"] == "response.completed"
        assert source.closed == 1

    asyncio.run(run())


def test_transport_exception_does_not_expose_credentials(sessions):
    async def run():
        def handler(request):
            raise httpx.ConnectError("url=https://secret-key@example.test/?key=secret-key")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await make_provider(OpenAIProvider, client).chat(messages=MESSAGES, tools=None)
        assert not result.ok
        assert "ConnectError" in result.error
        assert "secret-key" not in result.error
        assert sessions[0].ended == 1

    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_stream_response_metadata_callback_and_cleanup(sessions, monkeypatch, fail):
    async def inspect_response(session, wire):
        assert wire["body"] is None
        assert wire["headers"]["x-original"] == "present"
        session.responses.append(wire)
        if fail:
            raise PluginExecutionError("probe", "response", "ValueError")
        wire["headers"]["x-plugin-response"] = "applied"
        return wire

    monkeypatch.setattr(Session, "response", inspect_response)

    async def run():
        source = ChunkStream(sse(text_event(OpenAIProvider)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"x-original": "present"}, stream=source),
        )) as client:
            wrapped = wrap_client(client, "openai")
            if fail:
                with pytest.raises(PluginExecutionError):
                    async with wrapped.stream("POST", "https://example.test", json={}):
                        pytest.fail("failed response middleware must not expose a stream")
            else:
                async with wrapped.stream("POST", "https://example.test", json={}) as response:
                    assert response.headers["x-plugin-response"] == "applied"
                    await response.aread()
        assert source.closed == 1
        assert sessions[0].ended == 1
        assert len(sessions[0].responses) == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["send", "read", "callback"])
def test_gemini_cancellation_survives_failing_end_callback(tmp_path, stage):
    from plugin_runtime.store import PluginStore

    store = PluginStore(tmp_path)
    source_code = '''
outcomes = []

def setup(api):
    @api.on_request_end
    def end(ctx, outcome):
        outcomes.append(outcome)
        raise ValueError("end callback failed")
'''
    item = store.install("cancel_probe.py", source_code)
    store.configure("cancel_probe", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])

    async def run():
        reached = asyncio.Event()
        source = ChunkStream(sse(text_event(GeminiProvider), event="text"), wait=reached if stage == "read" else None)

        async def handler(request):
            if stage == "send":
                reached.set()
                await asyncio.Event().wait()
            return httpx.Response(200, stream=source)

        async def on_text(text):
            if stage == "callback":
                reached.set()
                await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with plugin_runtime.plugin_scope(workspace_root=tmp_path):
                task = asyncio.create_task(make_provider(GeminiProvider, client).chat_stream(messages=MESSAGES, on_text=on_text))
            await asyncio.wait_for(reached.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert source.closed == (0 if stage == "send" else 1)

    try:
        asyncio.run(run())
        manager = plugin_runtime.get_manager(tmp_path)
        outcomes = manager._versions["cancel_probe"].module.outcomes
        assert len(outcomes) == 1 and outcomes[0]["cancelled"] is True
        state = manager.status()["plugins"][0]
        assert state["active_requests"] == 0
        assert state["error_details"]["phase"] == "end"
    finally:
        plugin_runtime.get_manager(tmp_path).close()
