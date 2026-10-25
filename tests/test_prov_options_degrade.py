"""降级必须真的改动请求体，标了「已忽略」的参数必须真的不发。

降级重试的前提是第二次请求和第一次不一样：清掉一个默认值不为空的项只会回落到默认值，
请求体一个字都不变，结果就是拿同一个 400 白撞一轮。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers.deepseek import DeepSeekProvider  # noqa: E402
from providers.options import BOOL, ENUM, FLOAT, OptionSet, OptionSpec  # noqa: E402

_MESSAGES = [{"role": "user", "content": "hi"}]
_OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def _rejection(message: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {"message": message, "type": "invalid_request_error"}})


def _run(responses: list[httpx.Response], rounds: int = 1, **options: Any) -> tuple[list[dict], list[Any]]:
    sent: list[dict] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return pending.pop(0) if pending else httpx.Response(200, json=_OK)

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(http=client, api_key="k", provider_options=options or None)
            return [await provider.chat(messages=_MESSAGES, tools=None) for _ in range(rounds)]

    return sent, asyncio.run(run())


class TestDeepSeekDegrade:
    def test_switching_thinking_off_changes_the_request_body(self) -> None:
        sent, [result] = _run([_rejection("thinking mode is not supported for this model")])
        assert result.ok, result.error
        assert sent[0]["thinking"] == {"type": "enabled"}
        assert sent[1]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" in sent[0] and "reasoning_effort" not in sent[1]
        assert sent[0] != sent[1]

    def test_the_correction_is_remembered(self) -> None:
        sent, results = _run([_rejection("thinking mode is not supported")], rounds=2)
        assert all(result.ok for result in results)
        assert len(sent) == 3
        assert sent[2]["thinking"] == {"type": "disabled"}

    def test_dropping_the_effort_keeps_thinking_on(self) -> None:
        sent, [result] = _run([_rejection("Unrecognized request argument supplied: reasoning_effort")])
        assert result.ok, result.error
        assert sent[1]["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in sent[1]

    def test_a_rule_that_cannot_change_anything_is_not_retried(self) -> None:
        # 思考已经关了还被拒，再关一次请求体也不会变，不该再撞一轮。
        sent, [result] = _run([_rejection("thinking is broken")] * 3, thinking=False)
        assert result.ok is False
        assert len(sent) == 1

    def test_options_deepseek_ignores_are_not_sent(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            payload = DeepSeekProvider(
                api_key="k", provider_options={"verbosity": "low", "prompt_cache_key": "group-a"},
            )._build_payload(_MESSAGES, None)
        assert "verbosity" not in payload
        assert "prompt_cache_key" not in payload
        assert any("verbosity" in record.getMessage() for record in caplog.records)

    def test_effort_can_be_left_to_the_server(self) -> None:
        payload = DeepSeekProvider(api_key="k", provider_options={"reasoning_effort": "default"})._build_payload(_MESSAGES, None)
        assert payload["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in payload


_SPECS = (
    OptionSpec(key="thinking", label="t", kind=BOOL, default=True),
    OptionSpec(key="effort", label="e", kind=ENUM, default="high", choices=(("default", "-"), ("high", "h"))),
    OptionSpec(key="temperature", label="t", kind=FLOAT, default=None),
)


class TestOptionSet:
    def test_unknown_keys_are_never_passed_through(self) -> None:
        options = OptionSet(_SPECS, {"verbosity": "low"})
        assert options.unknown_keys() == ("verbosity",)
        assert options.get("verbosity") is None

    def test_clearing_a_key_with_a_non_empty_default_reports_no_change(self) -> None:
        options = OptionSet(_SPECS, {"thinking": True})
        assert options.override("thinking", None) is False
        assert options.get("thinking") is True

    def test_setting_the_value_it_already_has_reports_no_change(self) -> None:
        options = OptionSet(_SPECS, {})
        assert options.override("effort", "high") is False
        # 没改就不该留下痕迹：「没配」和「配成默认值」对采样参数是两回事。
        assert not options.is_set("effort")

    def test_a_real_change_is_reported_and_kept(self) -> None:
        options = OptionSet(_SPECS, {"temperature": 0.7})
        assert options.override("temperature", None) is True
        assert not options.is_set("temperature")
        assert options.override("thinking", False) is True
        assert options.get("thinking") is False

    def test_unknown_keys_cannot_be_overridden(self) -> None:
        assert OptionSet(_SPECS, {}).override("verbosity", "low") is False
