"""缓存命中量照实记：渠道报了 0 就记 0，没报就不写；Gemini 没命中时 cachedContentTokenCount 不出现，按 0 记。

流式和非流式结果必须一致。上游一律用构造的 SSE / JSON 模拟，不发真实请求。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference import llm_call  # noqa: E402
from engine.builtin import fallback_provider as fallback_module  # noqa: E402
from engine.builtin.usage_tracker import UsageTracker, accumulate_usage, note_serving_provider  # noqa: E402
from engine.context import ServingProvider  # noqa: E402
from providers import registry  # noqa: E402
from providers.assembly import int_field, reported_int, usage_dict  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]
_ABSENT = object()


def _sse(*events: Any) -> bytes:
    lines = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


def _call(name: str, *, stream: bool, body: Any) -> ProviderResponse:
    def handler(request: httpx.Request) -> httpx.Response:
        if stream:
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=body)

    async def run() -> ProviderResponse:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = registry.get(name)(http=client, api_key="k", base_url="https://relay.test", model="m")
            if stream:
                return await provider.chat_stream(messages=MESSAGES)
            return await provider.chat(messages=MESSAGES, tools=None)

    return asyncio.run(run())


def _openai_usage(cached: Any = _ABSENT, *, flat: Any = _ABSENT) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if cached is not _ABSENT:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    if flat is not _ABSENT:
        usage["prompt_cache_hit_tokens"] = flat
    return usage


def _openai_bodies(usage: dict[str, Any]) -> tuple[Any, bytes]:
    plain = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": usage}
    stream = _sse(
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": usage},
        "[DONE]",
    )
    return plain, stream


def _responses_usage(cached: Any = _ABSENT) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    if cached is not _ABSENT:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return usage


def _responses_bodies(usage: dict[str, Any]) -> tuple[Any, bytes]:
    output = [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]
    plain = {"status": "completed", "output": output, "usage": usage}
    stream = _sse(
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"status": "completed", "usage": usage}},
    )
    return plain, stream


def _anthropic_usage(read: Any = _ABSENT, write: Any = _ABSENT) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_tokens": 40}
    if read is not _ABSENT:
        usage["cache_read_input_tokens"] = read
    if write is not _ABSENT:
        usage["cache_creation_input_tokens"] = write
    return usage


def _anthropic_bodies(input_usage: dict[str, Any]) -> tuple[Any, bytes]:
    plain = {
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        "usage": {**input_usage, "output_tokens": 20},
    }
    stream = _sse(
        {"type": "message_start", "message": {"usage": {**input_usage, "output_tokens": 1}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    )
    return plain, stream


def _gemini_usage(cached: Any = _ABSENT) -> dict[str, Any]:
    usage: dict[str, Any] = {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120}
    if cached is not _ABSENT:
        usage["cachedContentTokenCount"] = cached
    return usage


def _gemini_bodies(usage: dict[str, Any]) -> tuple[Any, bytes]:
    plain = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}], "usageMetadata": usage}
    return plain, _sse(plain)


def _both(name: str, bodies: tuple[Any, bytes]) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    plain, stream = bodies
    plain_resp = _call(name, stream=False, body=plain)
    stream_resp = _call(name, stream=True, body=stream)
    assert plain_resp.ok, plain_resp.error
    assert stream_resp.ok, stream_resp.error
    return plain_resp.usage, stream_resp.usage


class TestUsageShape:
    def test_reported_zero_is_kept_and_unreported_is_left_out(self) -> None:
        assert usage_dict(prompt=10, completion=2, cache_read=0, cache_write=0) == {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        assert usage_dict(prompt=10, completion=2) == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

    def test_reasoning_still_appears_only_when_spent(self) -> None:
        assert "reasoning_tokens" not in usage_dict(prompt=1, completion=1, reasoning=0)
        assert usage_dict(prompt=1, completion=1, reasoning=3)["reasoning_tokens"] == 3

    @pytest.mark.parametrize(("holder", "expected"), [
        ({"a": {"b": 0}}, 0),
        ({"a": {"b": 7}}, 7),
        ({"a": {"b": None}}, None),
        ({"a": None}, None),
        ({"a": {}}, None),
        ({"a": {"b": True}}, None),
        ({"a": {"b": "7"}}, None),
        ({}, None),
    ])
    def test_reported_int_tells_zero_from_missing(self, holder: dict, expected: int | None) -> None:
        assert reported_int(holder, "a", "b") == expected
        assert int_field(holder, "a", "b") == (expected or 0)


class TestOpenAICompatible:
    @pytest.mark.parametrize("name", ["openai", "deepseek"])
    def test_a_reported_zero_hit_is_recorded(self, name: str) -> None:
        plain, stream = _both(name, _openai_bodies(_openai_usage(cached=0)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 0

    @pytest.mark.parametrize("name", ["openai", "deepseek"])
    def test_deepseeks_flat_zero_is_recorded(self, name: str) -> None:
        plain, stream = _both(name, _openai_bodies(_openai_usage(flat=0)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 0

    @pytest.mark.parametrize("name", ["openai", "deepseek"])
    def test_a_relay_that_reports_nothing_writes_nothing(self, name: str) -> None:
        plain, stream = _both(name, _openai_bodies(_openai_usage()))
        assert plain == stream == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    def test_null_details_count_as_not_reported(self) -> None:
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "prompt_tokens_details": None}
        plain, stream = _both("openai", _openai_bodies(usage))
        assert plain == stream
        assert plain is not None and "cache_read_tokens" not in plain
        plain, _ = _both("openai", _openai_bodies(_openai_usage(cached=None)))
        assert plain is not None and "cache_read_tokens" not in plain

    def test_a_hit_in_either_place_wins_over_a_zero_in_the_other(self) -> None:
        plain, stream = _both("deepseek", _openai_bodies(_openai_usage(cached=0, flat=48)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 48


class TestOpenAIResponses:
    def test_a_reported_zero_hit_is_recorded(self) -> None:
        plain, stream = _both("openai-responses", _responses_bodies(_responses_usage(cached=0)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 0

    def test_a_hit_is_recorded(self) -> None:
        plain, stream = _both("openai-responses", _responses_bodies(_responses_usage(cached=64)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 64

    def test_no_details_writes_nothing(self) -> None:
        plain, stream = _both("openai-responses", _responses_bodies(_responses_usage()))
        assert plain == stream
        assert plain is not None and "cache_read_tokens" not in plain


class TestAnthropic:
    def test_reported_zero_reads_and_writes_are_recorded(self) -> None:
        plain, stream = _both("anthropic", _anthropic_bodies(_anthropic_usage(read=0, write=0)))
        assert plain == stream == {
            "prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }

    def test_cache_reads_and_writes_still_count_as_input(self) -> None:
        plain, stream = _both("anthropic", _anthropic_bodies(_anthropic_usage(read=300, write=60)))
        assert plain == stream
        assert plain is not None
        assert (plain["prompt_tokens"], plain["cache_read_tokens"], plain["cache_write_tokens"]) == (400, 300, 60)

    def test_a_proxy_without_cache_fields_writes_nothing(self) -> None:
        plain, stream = _both("anthropic", _anthropic_bodies(_anthropic_usage()))
        assert plain == stream == {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}


class TestGemini:
    def test_a_missing_cached_count_means_zero(self) -> None:
        plain, stream = _both("gemini", _gemini_bodies(_gemini_usage()))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 0

    def test_a_hit_is_recorded(self) -> None:
        plain, stream = _both("gemini", _gemini_bodies(_gemini_usage(cached=60)))
        assert plain == stream
        assert plain is not None and plain["cache_read_tokens"] == 60

    def test_no_usage_metadata_is_still_no_usage(self) -> None:
        plain = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        assert _call("gemini", stream=False, body=plain).usage is None


class TestTaskTotals:
    def test_a_zero_hit_puts_the_key_in_the_task_total(self) -> None:
        total: dict[str, int] = {}
        accumulate_usage(total, {"prompt_tokens": 100, "cache_read_tokens": 0})
        assert total == {"prompt_tokens": 100, "cache_read_tokens": 0}

    def test_one_reporting_request_is_enough_for_the_task_total(self) -> None:
        total: dict[str, int] = {}
        accumulate_usage(total, {"prompt_tokens": 100})
        accumulate_usage(total, {"prompt_tokens": 50, "cache_read_tokens": 30})
        assert total == {"prompt_tokens": 150, "cache_read_tokens": 30}


def _rctx() -> SimpleNamespace:
    return SimpleNamespace(total_usage={}, serving_provider=None)


class TestServingProvider:
    def test_a_successful_answer_notes_its_provider(self) -> None:
        rctx = _rctx()
        provider = SimpleNamespace(model="deepseek-v4", name="deepseek", plugin_channel="ds-main")
        ctx = SimpleNamespace(rctx=rctx, provider=provider, response=ProviderResponse(
            ok=True, text="ok", usage={"prompt_tokens": 10, "cache_read_tokens": 0},
        ))

        asyncio.run(UsageTracker().handle(ctx))

        assert rctx.serving_provider == ServingProvider(model="deepseek-v4", provider="deepseek", channel="ds-main")
        assert rctx.total_usage == {"prompt_tokens": 10, "cache_read_tokens": 0}

    def test_a_failed_answer_leaves_the_last_successful_one(self) -> None:
        rctx = _rctx()
        earlier = ServingProvider(model="m1", provider="openai", channel="main")
        rctx.serving_provider = earlier
        ctx = SimpleNamespace(rctx=rctx, provider=SimpleNamespace(model="m2", name="gemini"), response=ProviderResponse(
            ok=False, error="boom", error_kind="server", usage={"prompt_tokens": 5},
        ))

        asyncio.run(UsageTracker().handle(ctx))

        assert rctx.serving_provider is earlier
        assert rctx.total_usage == {"prompt_tokens": 5}

    def test_missing_attributes_become_empty_strings(self) -> None:
        rctx = _rctx()
        note_serving_provider(rctx, SimpleNamespace(model="only-model"))
        assert rctx.serving_provider == ServingProvider(model="only-model")
        note_serving_provider(None, SimpleNamespace(model="ignored"))

    def test_a_fallback_that_answers_is_the_one_noted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        class Backup:
            name = "gemini"
            model = "backup-model"

            async def chat(self, *, messages, tools):
                return ProviderResponse(ok=True, text="done", usage={"prompt_tokens": 30, "cache_read_tokens": 0})

        monkeypatch.setattr(fallback_module, "load_models_config", lambda _root: {
            "gemini-中转A": {"type": "gemini", "base_url": "https://relay.test", "api_key": "k", "model": "backup-model"},
            "fallbacks": [{"provider": "gemini-中转A"}],
        })
        monkeypatch.setattr(fallback_module, "_create_fallback_provider", lambda **_kwargs: Backup())
        monkeypatch.setattr(llm_call, "_build_messages_for_provider", lambda messages, formatter, provider: messages)
        monkeypatch.setattr(llm_call, "prepare_messages_for_llm", lambda messages, workspace_root: messages)
        rctx = SimpleNamespace(workspace_root=tmp_path, total_usage={}, serving_provider=None)
        ctx = SimpleNamespace(
            response=ProviderResponse(ok=False, error="primary down", status_code=502, error_kind="server"),
            rctx=rctx, node=SimpleNamespace(id="entry"), messages=[{"role": "user", "content": "hi"}], tools=[],
            provider=SimpleNamespace(model="primary-model", name="deepseek", plugin_channel="ds-main", timeout=10.0),
            extra={"loop_state": SimpleNamespace(formatter=None)},
        )

        asyncio.run(fallback_module.FallbackProviderHandler().handle(ctx))

        assert ctx.response.ok
        assert rctx.serving_provider == ServingProvider(model="backup-model", provider="gemini", channel="gemini-中转A")
        assert rctx.total_usage == {"prompt_tokens": 30, "cache_read_tokens": 0}
