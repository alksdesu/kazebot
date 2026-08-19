"""参数最终落到请求体上的样子。

各家都有「同时给就报错」和「给了默认值反而被拒」的字段，光看配置读没读到不够，
得盯住真正发出去的那个 dict。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.deepseek import DeepSeekProvider  # noqa: E402
from providers.gemini import GeminiProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402
from providers.openai_responses import OpenAIResponsesProvider  # noqa: E402

_MESSAGES = [{"role": "user", "content": "hi"}]


def _gemini(**options) -> dict:
    provider = GeminiProvider(
        http=None, api_key="k", base_url=None, model="g", provider_options=options,
    )
    return provider._build_body(_MESSAGES, None)


def _anthropic(**options) -> dict:
    provider = AnthropicProvider(
        http=None, api_key="k", base_url=None, model="c", provider_options=options,
    )
    return provider._build_payload(_MESSAGES)


def _openai(**options) -> dict:
    provider = OpenAIProvider(
        http=None, api_key="k", base_url="https://x/v1", model="m", provider_options=options,
    )
    return provider._build_payload(_MESSAGES, None)


def _responses(**options) -> dict:
    provider = OpenAIResponsesProvider(
        http=None, api_key="k", base_url="https://x/v1", model="m", provider_options=options,
    )
    return provider._build_payload(input_items=[], instructions="", tools=None, stream=False)


# ---------------------------------------------------------------------------
# Gemini：档位与预算同时出现就是 400
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["auto", "level", "budget"])
def test_gemini_never_sends_both_thinking_forms(mode: str) -> None:
    config = _gemini(thinking_mode=mode)["generationConfig"].get("thinkingConfig", {})
    assert not ("thinkingLevel" in config and "thinkingBudget" in config), (
        f"thinking_mode={mode} 同时发了档位和预算，对面会直接拒："
        f"{sorted(config)}"
    )


def test_gemini_auto_sends_no_thinking_config_at_all() -> None:
    """auto 的意思是不表态，让对面按自己这一代的默认来。"""
    assert "thinkingConfig" not in _gemini(thinking_mode="auto")["generationConfig"]


def test_gemini_level_and_budget_pick_the_right_field() -> None:
    level = _gemini(thinking_mode="level", thinking_level="high")["generationConfig"]["thinkingConfig"]
    budget = _gemini(thinking_mode="budget", thinking_budget=4096)["generationConfig"]["thinkingConfig"]
    assert level["thinkingLevel"] == "HIGH"
    assert budget["thinkingBudget"] == 4096


def test_gemini_safety_is_opt_in_and_covers_every_category() -> None:
    assert "safetySettings" not in _gemini()
    settings = _gemini(safety_threshold="block_none")["safetySettings"]
    assert {item["threshold"] for item in settings} == {"BLOCK_NONE"}
    # 少一类就会在那一类上继续被拦，而且是静默的空回复。
    assert len(settings) == 4


def test_gemini_sampling_is_only_sent_when_configured() -> None:
    assert "temperature" not in _gemini()["generationConfig"]
    assert _gemini(temperature=0.9)["generationConfig"]["temperature"] == 0.9


# ---------------------------------------------------------------------------
# Anthropic：请求体里出现采样键就会被新模型拒
# ---------------------------------------------------------------------------

def test_anthropic_omits_sampling_keys_entirely_when_unset() -> None:
    payload = _anthropic()
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload


def test_anthropic_cache_is_off_by_default() -> None:
    """缓存会改计费结构，不该替人默认打开。"""
    assert "cache_control" not in _anthropic()
    assert _anthropic(cache="1h")["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_anthropic_effort_goes_under_output_config() -> None:
    assert "output_config" not in _anthropic()
    assert _anthropic(effort="high")["output_config"] == {"effort": "high"}


def test_anthropic_thinking_budget_stays_below_max_tokens() -> None:
    """budget 必须小于 max_tokens，否则换一个 400 继续撞。"""
    payload = _anthropic(thinking_mode="budget", thinking_budget_tokens=99000, max_tokens=8192)
    assert payload["thinking"]["budget_tokens"] < payload["max_tokens"]


def test_anthropic_beta_header_is_opt_in() -> None:
    plain = AnthropicProvider(http=None, api_key="k", base_url=None, model="c")
    with_beta = AnthropicProvider(
        http=None, api_key="k", base_url=None, model="c",
        provider_options={"interleaved_thinking": True},
    )
    assert "anthropic-beta" not in plain._headers()
    assert with_beta._headers()["anthropic-beta"] == "interleaved-thinking-2025-05-14"


# ---------------------------------------------------------------------------
# OpenAI 系
# ---------------------------------------------------------------------------

def test_openai_uses_the_current_token_limit_field() -> None:
    """官方已把 max_tokens 换成 max_completion_tokens。"""
    payload = _openai(max_completion_tokens=1024)
    assert payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in payload


def test_openai_sends_nothing_extra_by_default() -> None:
    """默认必须和改造前一模一样，否则等于替所有现有部署改了行为。"""
    assert set(_openai()) == {"model", "messages"}


def test_deepseek_drops_sampling_while_thinking() -> None:
    """思考模式下这些参数被静默忽略，发出去只会让人以为调过了。"""
    provider = DeepSeekProvider(api_key="k", provider_options={"temperature": 0.7})
    payload = provider._build_payload(_MESSAGES, None)
    assert payload["thinking"] == {"type": "enabled"}
    assert "temperature" not in payload


def test_deepseek_keeps_sampling_when_not_thinking() -> None:
    provider = DeepSeekProvider(api_key="k", provider_options={"thinking": False, "temperature": 0.7})
    payload = provider._build_payload(_MESSAGES, None)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.7
    # 关了思考还发档位是自相矛盾的。
    assert "reasoning_effort" not in payload


def test_deepseek_never_leaks_options_it_does_not_support() -> None:
    """父类认识 verbosity，DeepSeek 不认；漏出去就是一个 null 字段。"""
    payload = DeepSeekProvider(api_key="k")._build_payload(_MESSAGES, None)
    assert "verbosity" not in payload
    assert "prompt_cache_key" not in payload
    assert all(value is not None for value in payload.values())


def test_deepseek_preserves_reasoning_content_for_tool_turns() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "a", "reasoning_content": "why", "_meta": {"x": 1}},
        {"role": "user", "content": "go on"},
    ]
    payload = DeepSeekProvider(api_key="k")._build_payload(history, None)
    assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "why"
    # 内部字段不能漏给对面。
    assert "_meta" not in assistant


def test_responses_reasoning_is_assembled_from_two_knobs() -> None:
    assert "reasoning" not in _responses()
    assert _responses(reasoning_effort="high")["reasoning"] == {"effort": "high"}
    assert _responses(reasoning_effort="low", reasoning_summary="auto")["reasoning"] == {
        "effort": "low", "summary": "auto",
    }


def test_responses_still_asks_for_encrypted_reasoning() -> None:
    """多轮要把推理原样回传，这一项丢了会退化成每轮重新想。"""
    assert _responses()["include"] == ["reasoning.encrypted_content"]


def test_responses_store_is_tri_state() -> None:
    assert "store" not in _responses()
    assert _responses(store="false")["store"] is False
    assert _responses(store="true")["store"] is True


def test_responses_verbosity_is_nested_under_text() -> None:
    """Chat Completions 是顶层 verbosity，Responses 在 text 下面。"""
    assert _responses(verbosity="low")["text"] == {"verbosity": "low"}


# ---------------------------------------------------------------------------
# 配错一个选项名只该是一行 warning


def _deepseek(**options) -> dict:
    return DeepSeekProvider(api_key="k", provider_options=options)._build_payload(_MESSAGES, None)


@pytest.mark.parametrize("build", [_gemini, _anthropic, _openai, _responses, _deepseek])
def test_an_unknown_option_warns_instead_of_taking_the_channel_down(build, caplog) -> None:
    # openai 那家原本连 logger 都没定义，写错一个键名整条渠道就 NameError 起不来。
    with caplog.at_level(logging.WARNING):
        build(no_such_option=1)

    assert any("no_such_option" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("build", [_gemini, _anthropic, _openai, _responses, _deepseek])
def test_a_known_option_says_nothing(build, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        build(temperature=0.5)

    assert [r for r in caplog.records if "provider_options" in r.getMessage()] == []
