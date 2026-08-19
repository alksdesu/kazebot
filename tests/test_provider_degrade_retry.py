"""被对面拒掉之后，改一次参数再试。

各家把参数互斥做成硬 400，而模型名经反代改写后不可靠，判不出该发哪套。
这里盯三件事：确实改对了、确实重试了、以及修正被记住——否则每一轮对话都要
先撞一次 400，白付一个来回。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402

_MESSAGES = [{"role": "user", "content": "hi"}]


def _rejection(message: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": message}})


def _anthropic_ok() -> httpx.Response:
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    })


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


def _anthropic(client: _ScriptedClient, **options) -> AnthropicProvider:
    return AnthropicProvider(
        http=client, api_key="k", base_url=None, model="claude-x", provider_options=options,
    )


def test_fixed_budget_thinking_is_retried_as_adaptive() -> None:
    client = _ScriptedClient(
        _rejection('"thinking.type.enabled" is not supported by this model'),
        _anthropic_ok(),
    )
    provider = _anthropic(client, thinking_mode="budget", thinking_budget_tokens=8192)

    result = asyncio.run(provider.chat(messages=_MESSAGES, tools=None))

    assert result.ok, result.error
    assert len(client.sent) == 2
    assert client.sent[0]["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert client.sent[1]["thinking"] == {"type": "adaptive"}


def test_adaptive_thinking_is_retried_as_fixed_budget() -> None:
    """反向也要成立：老模型不认自适应。"""
    client = _ScriptedClient(
        _rejection('"thinking.type.adaptive" is not supported by this model'),
        _anthropic_ok(),
    )
    provider = _anthropic(client, thinking_mode="adaptive")

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert client.sent[1]["thinking"]["type"] == "enabled"
    assert client.sent[1]["thinking"]["budget_tokens"] < client.sent[1]["max_tokens"]


def test_sampling_params_are_dropped_when_rejected() -> None:
    client = _ScriptedClient(
        _rejection("temperature and top_p are not supported for this model"),
        _anthropic_ok(),
    )
    provider = _anthropic(client, temperature=0.7, top_p=0.9)

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert "temperature" in client.sent[0]
    assert "temperature" not in client.sent[1]
    assert "top_p" not in client.sent[1]


def test_max_tokens_is_lowered_to_the_ceiling_the_error_reports() -> None:
    client = _ScriptedClient(
        _rejection("max_tokens: 64000 > 20000, which is the maximum allowed number of output tokens"),
        _anthropic_ok(),
    )
    provider = _anthropic(client)

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert client.sent[0]["max_tokens"] == 64000
    # 20000 不是 64000 的一半，能分辨出到底是读了错误里的数字还是在盲目折半。
    assert client.sent[1]["max_tokens"] == 20000


def test_the_correction_is_remembered_for_later_calls() -> None:
    """第二轮对话不该再撞同一堵墙。"""
    client = _ScriptedClient(
        _rejection('"thinking.type.enabled" is not supported'),
        _anthropic_ok(),
        _anthropic_ok(),
    )
    provider = _anthropic(client, thinking_mode="budget")

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    # 三次请求：撞一次、改对一次、第二轮直接就是对的。
    assert len(client.sent) == 3
    assert client.sent[2]["thinking"] == {"type": "adaptive"}


def test_two_different_walls_are_both_climbed() -> None:
    client = _ScriptedClient(
        _rejection('"thinking.type.enabled" is not supported'),
        _rejection("max_tokens: 64000 > 8192, which is the maximum allowed"),
        _anthropic_ok(),
    )
    provider = _anthropic(client, thinking_mode="budget")

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert client.sent[2]["thinking"] == {"type": "adaptive"}
    assert client.sent[2]["max_tokens"] == 8192


def test_an_unrelated_rejection_is_not_retried() -> None:
    """认证失败重试多少次都一样，只会拖长故障时间。"""
    client = _ScriptedClient(_rejection("invalid x-api-key"))
    provider = _anthropic(client)

    result = asyncio.run(provider.chat(messages=_MESSAGES, tools=None))

    assert not result.ok
    assert result.status_code == 400
    assert len(client.sent) == 1


def test_retries_are_bounded() -> None:
    """对面一直拒也得停下来，不能拿同一条消息刷爆配额。"""
    client = _ScriptedClient(*[
        _rejection("max_tokens: too big, maximum allowed is 100000") for _ in range(10)
    ])
    provider = _anthropic(client)

    result = asyncio.run(provider.chat(messages=_MESSAGES, tools=None))

    assert not result.ok
    assert len(client.sent) <= 4, f"重试了 {len(client.sent)} 次，上限应该是 4"


def test_openai_falls_back_to_the_legacy_token_limit_field() -> None:
    """不少兼容端点只认 max_tokens。"""
    client = _ScriptedClient(
        _rejection("Unrecognized request argument supplied: max_completion_tokens"),
        _openai_ok(),
    )
    provider = OpenAIProvider(
        http=client, api_key="k", base_url="https://x/v1", model="m",
        provider_options={"max_completion_tokens": 2048},
    )

    assert asyncio.run(provider.chat(messages=_MESSAGES, tools=None)).ok
    assert client.sent[0]["max_completion_tokens"] == 2048
    assert "max_completion_tokens" not in client.sent[1]
    assert client.sent[1]["max_tokens"] == 2048


@pytest.mark.parametrize("mode,expected", [
    ("auto", None),
    ("adaptive", {"type": "adaptive"}),
    ("off", {"type": "disabled"}),
])
def test_thinking_mode_auto_sends_no_field_at_all(mode: str, expected: dict | None) -> None:
    """auto 的意思是「不表态」，发一个具体值就等于替对面做了决定。"""
    client = _ScriptedClient(_anthropic_ok())
    provider = _anthropic(client, thinking_mode=mode)

    asyncio.run(provider.chat(messages=_MESSAGES, tools=None))

    assert client.sent[0].get("thinking") == expected
