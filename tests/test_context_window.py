"""按模型上下文窗口收紧压缩阈值：窗口从节点 yaml、runtime 映射、内置模型表里依次查，
查到后在第一次 before_step 写回 loop state，再随 context_usage 上报给 get_context_window。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _path in (_ROOT, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _engine_loop_fixture import ScriptedRegistry, make_loop_state  # noqa: E402
from engine.builtin import compact  # noqa: E402
from engine.context_window import (  # noqa: E402
    context_window_for,
    effective_thresholds,
    output_reserve_tokens,
    parse_model_name,
    threshold_report,
)
from engine.inference.llm_call import record_response_usage  # noqa: E402
from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402
from providers.options import OptionSet  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.context_window_report import engine_thresholds, with_engine_thresholds  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from toolbox.builtins.tasks import get_context_window  # noqa: E402


def _compact_cfg(**values: Any) -> dict[str, Any]:
    return {"engine": {"compact": values}}


def _options(values: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(get=lambda key: values.get(key))


class TestModelNames:
    @pytest.mark.parametrize("model, core, declared", [
        ("OpenAI/GPT-4o", "gpt-4o", None),
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet-20241022-v2", None),
        ("models/gemini-2.5-pro", "gemini-2.5-pro", None),
        ("claude-3-5-sonnet-v2@20241022", "claude-3-5-sonnet-v2", None),
        ("anthropic/claude-3.5-sonnet:beta", "claude-3.5-sonnet", None),
        ("claude-opus-4-6[1m]", "claude-opus-4-6", 1_000_000),
        ("relay/glm-4.6[200k]", "glm-4.6", 200_000),
    ])
    def test_channel_prefixes_and_suffixes_are_stripped(self, model: str, core: str, declared: int | None) -> None:
        name = parse_model_name(model)

        assert name.core == core
        assert name.declared == declared


class TestBuiltinTable:
    @pytest.mark.parametrize("model, expected", [
        ("claude-sonnet-4-5", 200_000),
        ("claude-opus-4-6[1m]", 1_000_000),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", 200_000),
        ("gpt-4o-mini", 128_000),
        ("chatgpt-4o-latest", 128_000),
        ("gpt-4.1-nano", 1_047_576),
        ("gpt-5", 272_000),
        ("gpt-5.1-codex", 272_000),
        ("gpt-5-chat-latest", 128_000),
        ("o1-mini", 128_000),
        ("o1", 200_000),
        ("o3-pro", 200_000),
        ("o4-mini", 200_000),
        ("gemini-1.5-pro-002", 2_097_152),
        ("gemini-1.5-flash-8b", 1_048_576),
        ("models/gemini-2.5-flash", 1_048_576),
        ("gemini-3-pro-preview", 1_048_576),
        ("deepseek-chat", 128_000),
        ("deepseek-reasoner", 128_000),
    ])
    def test_known_families(self, model: str, expected: int) -> None:
        assert context_window_for(model) == expected

    @pytest.mark.parametrize("model", ["deepseek-v4-pro", "qwen-max", "my-own-model", "", None])
    def test_unknown_models_are_not_adjusted(self, model: Any) -> None:
        assert context_window_for(model) is None


class TestPriority:
    def test_the_node_yaml_beats_everything(self) -> None:
        node = SimpleNamespace(id="worker", extra={"context_window": 64_000})

        window = context_window_for(
            "claude-sonnet-4-5[1m]", runtime_cfg=_compact_cfg(context_windows={"claude-*": 150_000}), node=node,
        )

        assert window == 64_000

    def test_a_runtime_glob_beats_the_name_suffix_and_the_table(self) -> None:
        cfg = _compact_cfg(context_windows={"claude-*": 150_000})

        assert context_window_for("claude-sonnet-4-5[1m]", runtime_cfg=cfg) == 150_000
        assert context_window_for("claude-haiku-4-5", runtime_cfg=cfg) == 150_000

    def test_a_runtime_glob_can_name_the_channel_prefix(self) -> None:
        cfg = _compact_cfg(context_windows={"my-relay/*": 90_000})

        assert context_window_for("my-relay/gpt-4o", runtime_cfg=cfg) == 90_000
        assert context_window_for("gpt-4o", runtime_cfg=cfg) == 128_000

    def test_the_first_matching_glob_wins(self) -> None:
        specific_first = _compact_cfg(context_windows={"gpt-4o*": 100_000, "gpt-*": 50_000})
        broad_first = _compact_cfg(context_windows={"gpt-*": 50_000, "gpt-4o*": 100_000})

        assert context_window_for("gpt-4o", runtime_cfg=specific_first) == 100_000
        assert context_window_for("gpt-4o", runtime_cfg=broad_first) == 50_000

    def test_zero_opts_a_model_out(self) -> None:
        assert context_window_for("gpt-4o", runtime_cfg=_compact_cfg(context_windows={"gpt-4o": 0})) is None
        assert context_window_for("gpt-4o", node=SimpleNamespace(id="n", extra={"context_window": 0})) is None

    def test_bad_entries_are_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _compact_cfg(context_windows={"gpt-4o": "lots", "gpt-4o-*": True, "gpt-*": 70_000})
        caplog.set_level(logging.WARNING, logger="engine.context_window")

        assert context_window_for("gpt-4o", runtime_cfg=cfg) == 70_000
        assert sum("context_windows" in record.getMessage() for record in caplog.records) == 2

    def test_a_bad_node_value_falls_through(self) -> None:
        node = SimpleNamespace(id="n", extra={"context_window": "huge"})

        assert context_window_for("gpt-4o", node=node) == 128_000

    def test_configured_values_are_clamped(self) -> None:
        assert context_window_for("tiny", runtime_cfg=_compact_cfg(context_windows={"tiny": 10})) == 1_000

    def test_a_non_mapping_setting_is_ignored(self) -> None:
        assert context_window_for("gpt-4o", runtime_cfg=_compact_cfg(context_windows=["gpt-4o"])) == 128_000


class TestEffectiveThresholds:
    def test_claude_with_its_default_reply_limit(self) -> None:
        result = effective_thresholds(200_000, soft=256_000, hard=320_000, reserve=64_000)

        assert (result.usable, result.soft, result.hard) == (136_000, 115_600, 136_000)

    def test_thresholds_already_inside_the_window_are_kept(self) -> None:
        result = effective_thresholds(1_048_576, soft=256_000, hard=320_000, reserve=65_536)

        assert (result.soft, result.hard) == (256_000, 320_000)

    def test_disabled_compaction_stays_disabled(self) -> None:
        result = effective_thresholds(128_000, soft=0, hard=0, reserve=20_000)

        assert (result.soft, result.hard) == (0, 0)

    def test_an_oversized_reserve_is_capped_at_half_the_window(self) -> None:
        result = effective_thresholds(32_000, soft=256_000, hard=320_000, reserve=64_000)

        assert (result.reserve, result.usable, result.soft, result.hard) == (16_000, 16_000, 13_600, 16_000)

    def test_the_reserve_comes_from_the_provider_reply_limit(self) -> None:
        anthropic = SimpleNamespace(_options=OptionSet(AnthropicProvider.OPTIONS, {}))
        openai_limited = SimpleNamespace(_options=OptionSet(OpenAIProvider.OPTIONS, {"max_completion_tokens": 4096}))
        gemini_style = SimpleNamespace(_options=_options({"max_output_tokens": 65_536}))

        assert output_reserve_tokens(anthropic, {}) == 64_000
        assert output_reserve_tokens(openai_limited, {}) == 4_096
        assert output_reserve_tokens(gemini_style, {}) == 65_536

    def test_without_a_reply_limit_the_runtime_reserve_is_used(self) -> None:
        openai_default = SimpleNamespace(_options=OptionSet(OpenAIProvider.OPTIONS, {}))

        assert output_reserve_tokens(openai_default, {}) == 20_000
        assert output_reserve_tokens(SimpleNamespace(), _compact_cfg(output_reserve_tokens=50_000)) == 50_000
        assert output_reserve_tokens(SimpleNamespace(_options=_options({"max_tokens": True})), {}) == 20_000
        assert output_reserve_tokens(None, _compact_cfg(output_reserve_tokens=-1)) == 0


def _claude_loop(tmp_path: Path) -> Any:
    loop = make_loop_state(tmp_path, ScriptedRegistry())
    loop.provider = SimpleNamespace(
        name="anthropic", model="claude-sonnet-4-5", _options=OptionSet(AnthropicProvider.OPTIONS, {}),
    )
    loop.compact_threshold = 256_000
    loop.compact_hard_threshold = 320_000
    loop.compact_keep_recent_tokens = 200_000
    return loop


class TestCompactCheckerAppliesTheWindow:
    def test_the_thresholds_are_tightened_once_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        loop = _claude_loop(tmp_path)
        caplog.set_level(logging.INFO, logger="engine.builtin.compact")

        compact._apply_context_window(loop)

        assert loop.context_window == 200_000
        assert (loop.compact_threshold, loop.compact_hard_threshold) == (115_600, 136_000)
        assert loop.compact_keep_recent_tokens == 92_480
        assert sum("context window 200000" in record.getMessage() for record in caplog.records) == 1

        loop.compact_threshold = 999
        compact._apply_context_window(loop)

        assert loop.compact_threshold == 999

    def test_an_unknown_model_keeps_the_configured_thresholds(self, tmp_path: Path) -> None:
        loop = make_loop_state(tmp_path, ScriptedRegistry())
        loop.compact_threshold, loop.compact_hard_threshold = 256_000, 320_000

        compact._apply_context_window(loop)

        assert loop.context_window is None
        assert (loop.compact_threshold, loop.compact_hard_threshold) == (256_000, 320_000)

    def test_the_checker_applies_the_window_on_the_first_step(self, tmp_path: Path) -> None:
        loop = _claude_loop(tmp_path)
        ctx = SimpleNamespace(messages=loop.messages, extra={"loop_state": loop, "step_count": 0}, step=0)

        result = asyncio.run(compact.CompactChecker().handle(ctx))

        assert result is None
        assert (loop.compact_threshold, loop.compact_hard_threshold) == (115_600, 136_000)


class TestReporting:
    def test_context_usage_carries_the_thresholds_in_effect(self) -> None:
        loop = SimpleNamespace(
            node=SimpleNamespace(id="worker"), rctx=SimpleNamespace(task_id="t", emit_event=AsyncMock()),
            compact_threshold=115_600, compact_hard_threshold=136_000, context_window=200_000,
        )

        asyncio.run(record_response_usage(
            loop, SimpleNamespace(usage={"prompt_tokens": 1000}), [{"role": "user", "content": "x" * 300}], None,
        ))

        event, payload = loop.rctx.emit_event.await_args.args
        assert event == "context_usage"
        assert payload["prompt_chars"] == 300 and loop.last_prompt_chars == 300
        assert (payload["compact_threshold"], payload["compact_hard_threshold"], payload["context_window"]) == (
            115_600, 136_000, 200_000,
        )

    def test_an_unknown_window_is_left_out(self) -> None:
        loop = SimpleNamespace(compact_threshold=256_000, compact_hard_threshold=320_000, context_window=None)

        assert threshold_report(loop) == {"compact_threshold": 256_000, "compact_hard_threshold": 320_000}

    def test_only_positive_engine_values_are_taken(self) -> None:
        assert engine_thresholds({"compact_threshold": 0, "compact_hard_threshold": True, "context_window": 9}) == {
            "context_window": 9,
        }
        report = with_engine_thresholds({"compact_threshold": 256_000, "effective_tokens": 100}, {})
        assert report["threshold_source"] == "runtime_config"
        assert report["compact_threshold"] == 256_000

    def test_the_endpoint_reports_what_the_engine_applied(self, tmp_path: Path) -> None:
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-context-window"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )
        app = create_app(
            state=state, process_manager=None,
            config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
        )
        session_id = state.get_or_create_session(channel="test", conversation_key="test:context-window")
        usage_event = {"type": "context_usage", "payload": {
            "node_id": "worker", "task_id": "", "usage": {"prompt_tokens": 23_120}, "prompt_chars": 50_000,
            "compact_threshold": 115_600, "compact_hard_threshold": 136_000, "context_window": 200_000,
        }}

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                before = (await client.get(f"/v1/sessions/{session_id}/context_window")).json()
                posted = await client.post(f"/v1/sessions/{session_id}/events", json=usage_event)
                assert posted.status_code == 200, posted.text
                after = (await client.get(f"/v1/sessions/{session_id}/context_window")).json()
            return before, after

        before, after = asyncio.run(run())

        assert before["threshold_source"] == "runtime_config"
        assert before["compact_hard_threshold"] is None and before["context_window"] is None
        assert after["threshold_source"] == "engine"
        assert (after["compact_threshold"], after["compact_hard_threshold"], after["context_window"]) == (
            115_600, 136_000, 200_000,
        )
        assert after["prompt_tokens"] == 23_120
        assert after["utilization"] == pytest.approx(0.2)

    def test_the_tool_puts_the_numbers_where_the_model_reads_them(self) -> None:
        payload = {
            "effective_tokens": 23_120, "source": "llm_usage", "compact_threshold": 115_600,
            "compact_hard_threshold": 136_000, "context_window": 200_000, "utilization": 0.2,
        }
        response = SimpleNamespace(status_code=200, json=lambda: payload)
        ctx = SimpleNamespace(
            route_session_id=lambda: "session-1", supervisor_url="http://sup",
            http=SimpleNamespace(get=AsyncMock(return_value=response)),
        )

        result = asyncio.run(get_context_window({}, ctx))

        text = result["data"]["result"]
        for number in ("23120", "115600", "136000", "200000", "20.0%"):
            assert number in text
        assert result["data"]["compact_threshold"] == 115_600
        assert ctx.http.get.await_args.args[0] == "http://sup/v1/sessions/session-1/context_window"

    def test_the_tool_says_when_compaction_is_off(self) -> None:
        response = SimpleNamespace(status_code=200, json=lambda: {"effective_tokens": 10, "source": "estimate",
                                                                  "compact_threshold": 0})
        ctx = SimpleNamespace(
            route_session_id=lambda: "s", supervisor_url="http://sup",
            http=SimpleNamespace(get=AsyncMock(return_value=response)),
        )

        text = asyncio.run(get_context_window({}, ctx))["data"]["result"]

        assert "estimated" in text and "off" in text
