from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest

from engine import runner
from engine.builtin.fallback_provider import _create_fallback_provider, _release_fallback_provider
from providers.openai import OpenAIProvider
from toolbox._common import safe_subprocess_env


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy") or name == "CLONOTH_LLM_TRUST_ENV":
            monkeypatch.delenv(name)


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("invalid", False),
    ("1", True), ("true", True), (" YES ", True), ("on", True),
])
def test_model_proxy_is_explicitly_enabled(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("CLONOTH_LLM_TRUST_ENV", value)
    seen = {}
    sentinel = object()

    def make_client(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(runner.httpx, "AsyncClient", make_client)
    assert runner._create_llm_http_client(42.0) is sentinel
    assert seen == {
        "timeout": 42.0,
        "trust_env": expected,
        "headers": {"User-Agent": "Clonoth"},
    }


@asynccontextmanager
async def fake_endpoint():
    received = []
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            lines = raw.decode("ascii").split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            size = int(next((v for k, v in headers.items() if k.lower() == "content-length"), "0"))
            body = json.loads(await reader.readexactly(size)) if size else {}
            received.append({"line": lines[0], "body": body})
            if lines[0].startswith("CONNECT "):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if body.get("stream"):
                packets = [
                    ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]}) + "\n\n").encode()
                    for text in ("proxy", "-ok")
                ]
                packets.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                content_type = "text/event-stream"
            elif lines[0].startswith("GET "):
                packets = [b'{"data":[{"id":"test-model"}]}']
                content_type = "application/json"
            else:
                packets = [b'{"choices":[{"message":{"role":"assistant","content":"proxy-ok"},"finish_reason":"stop"}]}']
                content_type = "application/json"
            length = sum(map(len, packets))
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n").encode())
            for packet in packets:
                writer.write(packet)
                await writer.drain()
                await asyncio.sleep(0.005)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], received
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*tuple(handlers), return_exceptions=True)


def set_proxy(monkeypatch, port):
    monkeypatch.setenv("CLONOTH_LLM_TRUST_ENV", "1")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost,127.0.0.1,::1")


@asynccontextmanager
async def model_provider(kind, base_url):
    if kind == "primary":
        async with runner._create_llm_http_client(3.0) as client:
            yield OpenAIProvider(http=client, api_key="test-key", base_url=base_url, model="test-model")
    else:
        provider = _create_fallback_provider(
            provider_type="openai", base_url=base_url,
            api_key="test-key", model="test-model", timeout=3.0,
        )
        assert provider is not None
        try:
            yield provider
        finally:
            await _release_fallback_provider(provider)
        assert provider._fallback_owned_http.is_closed


@pytest.mark.parametrize("kind", ["primary", "fallback"])
@pytest.mark.parametrize("stream", [False, True])
def test_model_request_and_response_travel_through_proxy(monkeypatch, kind, stream):
    async def run():
        async with fake_endpoint() as (port, received):
            set_proxy(monkeypatch, port)
            chunks = []

            async def on_text(text):
                chunks.append(text)

            async with model_provider(kind, "http://model.invalid/v1") as provider:
                messages = [{"role": "user", "content": "test"}]
                if stream:
                    result = await provider.chat_stream(messages=messages, on_text=on_text)
                else:
                    result = await provider.chat(messages=messages, tools=None)
            assert result.ok, result.error
            assert result.text == "proxy-ok"
            assert received[0]["line"] == "POST http://model.invalid/v1/chat/completions HTTP/1.1"
            assert received[0]["body"]["messages"][-1]["content"] == "test"
            if stream:
                assert chunks == ["proxy", "-ok"]
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["primary", "fallback"])
def test_https_uses_connect_and_does_not_fall_back_to_direct(monkeypatch, kind):
    async def run():
        async with fake_endpoint() as (port, received):
            set_proxy(monkeypatch, port)
            async with model_provider(kind, "https://model.invalid/v1") as provider:
                result = await provider.chat(messages=[{"role": "user", "content": "test"}], tools=None)
            assert not result.ok
            assert "502" in result.error
            assert received == [{"line": "CONNECT model.invalid:443 HTTP/1.1", "body": {}}]
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["primary", "fallback"])
def test_local_model_bypasses_proxy(monkeypatch, kind):
    async def run():
        async with fake_endpoint() as (port, received):
            set_proxy(monkeypatch, 1)
            async with model_provider(kind, f"http://127.0.0.1:{port}/v1") as provider:
                result = await provider.chat(messages=[{"role": "user", "content": "test"}], tools=None)
            assert result.ok, result.error
            assert received[0]["line"] == "POST /v1/chat/completions HTTP/1.1"
    asyncio.run(run())


def test_disabled_model_proxy_keeps_direct_path(monkeypatch):
    async def run():
        async with fake_endpoint() as (port, received):
            set_proxy(monkeypatch, 1)
            monkeypatch.setenv("CLONOTH_LLM_TRUST_ENV", "0")
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("no_proxy", "")
            async with runner._create_llm_http_client(3.0) as client:
                result = await client.get(f"http://127.0.0.1:{port}/models")
            assert result.status_code == 200
            assert received[0]["line"] == "GET /models HTTP/1.1"
    asyncio.run(run())


def test_model_catalog_inherits_proxy(monkeypatch):
    async def run():
        async with fake_endpoint() as (port, received):
            set_proxy(monkeypatch, port)
            async with httpx.AsyncClient(timeout=3.0) as client:
                result = await client.get("http://catalog.invalid/v1/models")
            assert result.json()["data"][0]["id"] == "test-model"
            assert received[0]["line"] == "GET http://catalog.invalid/v1/models HTTP/1.1"
    asyncio.run(run())


def test_tools_inherit_proxy_without_api_keys(monkeypatch):
    set_proxy(monkeypatch, 7893)
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    env = safe_subprocess_env()
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7893"
    if os.name != "nt":
        assert env["https_proxy"] == env["HTTPS_PROXY"]
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert "OPENAI_API_KEY" not in env
