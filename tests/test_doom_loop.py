"""跨轮重复调用检测：命中与不命中、任务恢复后从消息重建、repeatable 豁免、阈值。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    TASK_ID,
    ScriptedRegistry,
    answered_once,
    call,
    echo,
    make_loop_state,
    results_for,
    turn,
    use_hooks,
)
from engine.builtin.doom_loop import DoomLoopGuard, call_signature, doom_loop_threshold  # noqa: E402
from engine.builtin.finish_guard import FinishGuardHandler  # noqa: E402
from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.hooks.types import HookContext  # noqa: E402
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.tool_format import JsonToolFormatter  # noqa: E402
from engine.runner import _message_to_history_dict  # noqa: E402
from providers.base import ProviderResponse, ToolCall  # noqa: E402

_ALLOWED = {"probe", "search", "poll"}


def _assistant(*calls: tuple[str, dict[str, Any]], prefix: str, task_id: str = TASK_ID) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"{prefix}{index}", "name": name, "arguments": dict(arguments)}
            for index, (name, arguments) in enumerate(calls)
        ],
        "_meta": {"message_type": "assistant", "source_task_id": task_id},
    }


def _round(*calls: tuple[str, dict[str, Any]], prefix: str, task_id: str = TASK_ID) -> list[dict[str, Any]]:
    message = _assistant(*calls, prefix=prefix, task_id=task_id)
    results = [
        {
            "role": "tool", "tool_call_id": item["id"], "name": item["name"], "content": "same output",
            "_meta": {"message_type": "tool_result", "source_task_id": task_id},
        }
        for item in message["tool_calls"]
    ]
    return [message, *results]


def _user(text: str = "go") -> dict[str, Any]:
    return {"role": "user", "content": text, "_meta": {"message_type": "user_input", "source_task_id": TASK_ID}}


def _guard_ctx(
    history: list[dict[str, Any]],
    current: list[tuple[str, dict[str, Any]]],
    *,
    threshold: int | None = None,
    repeatable: tuple[str, ...] = (),
) -> HookContext:
    current_message = _assistant(*current, prefix="now")
    messages = [_user(), *history, current_message]
    loop_state = SimpleNamespace(
        messages=messages,
        rctx=SimpleNamespace(task_id=TASK_ID),
        registry=SimpleNamespace(get_spec=lambda name: {"name": name, "repeatable": name in repeatable}),
        runtime_cfg={} if threshold is None else {"engine": {"doom_loop": {"threshold": threshold}}},
        allowed_real_tools=set(_ALLOWED),
    )
    real = [
        {"id": item["id"], "name": item["name"], "arguments": item["arguments"]}
        for item in current_message["tool_calls"]
        if item["name"] in _ALLOWED
    ]
    return HookContext(
        messages=messages, tools=[], node=SimpleNamespace(id="node-1"), provider=None, rctx=None, step=4,
        extra={"loop_state": loop_state, "real_tool_calls": real, "pseudo_calls": [], "assistant_message": current_message},
    )


def _verdict(ctx: HookContext) -> Any:
    return asyncio.run(DoomLoopGuard().handle(ctx))


PROBE = ("probe", {"q": 1})


class TestHits:
    def test_the_third_identical_round_is_blocked(self) -> None:
        verdict = _verdict(_guard_ctx([*_round(PROBE, prefix="a"), *_round(PROBE, prefix="b")], [PROBE]))

        assert verdict is not None and verdict.block is True and verdict.reason == "doom_loop"
        assert verdict.error_message == (
            "你已经连续 3 轮用完全相同的参数调用 probe，结果不会变。换一种方法，或者用 finish 说明卡在哪里。"
        )

    def test_two_identical_rounds_are_still_allowed(self) -> None:
        assert _verdict(_guard_ctx(_round(PROBE, prefix="a"), [PROBE])) is None

    def test_argument_key_order_does_not_matter(self) -> None:
        shuffled = ("probe", {"b": 2, "a": 1})
        ordered = ("probe", {"a": 1, "b": 2})

        assert call_signature(*shuffled) == call_signature(*ordered)
        assert _verdict(_guard_ctx([*_round(shuffled, prefix="a"), *_round(shuffled, prefix="b")], [ordered])) is not None

    def test_the_same_set_of_calls_in_another_order_counts(self) -> None:
        search = ("search", {"k": "x"})
        history = [*_round(PROBE, search, prefix="a"), *_round(search, PROBE, prefix="b")]

        verdict = _verdict(_guard_ctx(history, [PROBE, search]))

        assert verdict is not None
        assert "probe、search" in verdict.error_message

    def test_rounds_that_only_called_pseudo_tools_do_not_break_the_streak(self) -> None:
        history = [*_round(PROBE, prefix="a"), *_round(("reply", {"text": "还在查"}), prefix="r"), *_round(PROBE, prefix="b")]

        assert _verdict(_guard_ctx(history, [PROBE])) is not None

    def test_image_results_and_retry_hints_are_not_new_input(self) -> None:
        history = [
            *_round(PROBE, prefix="a"),
            {"role": "user", "content": [{"type": "text", "text": "以上工具执行产生了以下图片结果："}],
             "_meta": {"message_type": "tool_result_attachment", "source_task_id": TASK_ID}},
            {"role": "user", "content": "[SYSTEM] answer again", "_retry_hint": True},
            *_round(PROBE, prefix="b"),
        ]

        assert _verdict(_guard_ctx(history, [PROBE])) is not None

    def test_calls_the_node_could_not_run_are_left_out_of_old_rounds(self) -> None:
        history = [
            *_round(PROBE, ("forbidden_tool", {}), prefix="a"),
            *_round(PROBE, ("probe", {"_raw": '{"q": '}), prefix="b"),
        ]

        assert _verdict(_guard_ctx(history, [PROBE])) is not None

    def test_history_in_the_provider_wire_shape_counts_too(self) -> None:
        wire = {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "w0", "type": "function", "function": {"name": "probe", "arguments": json.dumps({"q": 1})}}],
            "_meta": {"source_task_id": TASK_ID},
        }

        assert _verdict(_guard_ctx([wire, *_round(PROBE, prefix="b")], [PROBE])) is not None


class TestMisses:
    def test_different_arguments_are_not_a_repeat(self) -> None:
        history = [*_round(PROBE, prefix="a"), *_round(PROBE, prefix="b")]

        assert _verdict(_guard_ctx(history, [("probe", {"q": 2})])) is None

    def test_a_round_with_another_call_set_breaks_the_streak(self) -> None:
        history = [*_round(PROBE, prefix="a"), *_round(("search", {"k": "x"}), prefix="m"), *_round(PROBE, prefix="b")]

        assert _verdict(_guard_ctx(history, [PROBE])) is None

    def test_new_user_input_restarts_the_count(self) -> None:
        history = [*_round(PROBE, prefix="a"), _user("再试一次"), *_round(PROBE, prefix="b")]

        assert _verdict(_guard_ctx(history, [PROBE])) is None

    def test_rounds_from_another_task_are_not_counted(self) -> None:
        history = [*_round(PROBE, prefix="a", task_id="older-task"), *_round(PROBE, prefix="b", task_id="older-task")]

        assert _verdict(_guard_ctx(history, [PROBE])) is None

    def test_per_call_checks_are_ignored(self) -> None:
        ctx = _guard_ctx([*_round(PROBE, prefix="a"), *_round(PROBE, prefix="b")], [PROBE])
        ctx.tool_call = SimpleNamespace(id="now0", name="probe", arguments={"q": 1})

        assert _verdict(ctx) is None


class TestRepeatable:
    def test_repeatable_tools_are_exempt(self) -> None:
        poll = ("poll", {"job": "7"})
        history = [*_round(poll, prefix="a"), *_round(poll, prefix="b")]

        assert _verdict(_guard_ctx(history, [poll], repeatable=("poll",))) is None

    def test_a_repeatable_tool_next_to_others_is_left_out_of_the_signature(self) -> None:
        poll = ("poll", {"job": "7"})
        history = [*_round(poll, PROBE, prefix="a"), *_round(PROBE, prefix="b")]

        verdict = _verdict(_guard_ctx(history, [poll, PROBE], repeatable=("poll",)))

        assert verdict is not None
        assert "poll" not in verdict.error_message


class TestThreshold:
    def test_zero_turns_the_guard_off(self) -> None:
        history = [item for index in range(6) for item in _round(PROBE, prefix=f"r{index}")]

        assert _verdict(_guard_ctx(history, [PROBE], threshold=0)) is None

    def test_one_is_treated_as_two(self) -> None:
        assert doom_loop_threshold({"engine": {"doom_loop": {"threshold": 1}}}) == 2
        assert _verdict(_guard_ctx(_round(PROBE, prefix="a"), [PROBE], threshold=1)) is not None

    def test_a_higher_threshold_needs_more_rounds(self) -> None:
        two = [*_round(PROBE, prefix="a"), *_round(PROBE, prefix="b")]
        three = [*two, *_round(PROBE, prefix="c")]

        assert _verdict(_guard_ctx(two, [PROBE], threshold=4)) is None
        verdict = _verdict(_guard_ctx(three, [PROBE], threshold=4))
        assert verdict is not None and "连续 4 轮" in verdict.error_message

    @pytest.mark.parametrize("configured, expected", [(None, 3), (-5, 0), (99, 50), ("abc", 3)])
    def test_values_are_clamped(self, configured: Any, expected: int) -> None:
        cfg = {} if configured is None else {"engine": {"doom_loop": {"threshold": configured}}}

        assert doom_loop_threshold(cfg) == expected


class TestRebuiltHistory:
    def test_rounds_reloaded_from_the_store_after_a_resume_are_counted(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "data" / "conversations")
        records = [Message(id="u", role="user", content="go", message_type=MessageType.USER_INPUT, source_task_id=TASK_ID)]
        for index in range(2):
            records.append(Message(
                id=f"a{index}", role="assistant", content="", message_type=MessageType.ASSISTANT, source_task_id=TASK_ID,
                tool_calls=[{"id": f"c{index}", "name": "probe", "arguments": {"q": 1}}],
            ))
            records.append(Message(
                id=f"t{index}", role="tool", content="same output", message_type=MessageType.TOOL_RESULT,
                source_task_id=TASK_ID, tool_call_id=f"c{index}", name="probe",
            ))
        store.append_batch("session-1", records)
        reloaded = [_message_to_history_dict(message, current_task_id=TASK_ID) for message in store.load("session-1")]

        ctx = _guard_ctx([], [PROBE])
        current = ctx.extra["assistant_message"]
        ctx.extra["loop_state"].messages = [*reloaded, current]

        assert _verdict(ctx) is not None

    def test_json_mode_assistant_messages_carry_their_calls(self) -> None:
        formatter = JsonToolFormatter()
        history: list[dict[str, Any]] = []
        for index in range(2):
            text = f'<<<TOOL_CALL>>>\n{{"name": "probe", "arguments": {{"q": 1}}}}\n<<<END_TOOL_CALL>>>'
            message = formatter.build_assistant_message(
                ProviderResponse(ok=True, text=text), text, [ToolCall(id=f"j{index}", name="probe", arguments={"q": 1})],
            )
            message["_meta"] = {"source_task_id": TASK_ID}
            history.extend([message, {"role": "tool", "tool_call_id": f"j{index}", "name": "probe", "content": "x"}])

        assert _verdict(_guard_ctx(history, [PROBE])) is not None


def test_the_loop_stops_running_a_round_repeated_three_times(tmp_path: Path, monkeypatch: Any) -> None:
    use_hooks(monkeypatch, {"before_tool_call": [DoomLoopGuard(), FinishGuardHandler()]})
    registry = ScriptedRegistry(tools={"probe": echo("same output")})
    ls = make_loop_state(tmp_path, registry)
    ls.messages.append(_user("find it"))

    async def rounds() -> list[Any]:
        outcomes = []
        for index in range(3):
            outcomes.append(await _handle_tool_calls(ls, turn(call("probe", {"q": 1}, call_id=f"p{index}")), index))
        outcomes.append(await _handle_tool_calls(
            ls, turn(call("probe", {"q": 1}, call_id="p3"), call("finish", {"text": "done"}, call_id="f3")), 3,
        ))
        return outcomes

    outcomes = asyncio.run(rounds())

    assert outcomes == [None, None, None, None]
    assert registry.executed_names() == ["probe", "probe"]
    blocked = results_for(ls.messages, "p2")
    assert len(blocked) == 1
    assert blocked[0]["content"].startswith("你已经连续 3 轮用完全相同的参数调用 probe")
    assert blocked[0]["_meta"]["tool_status"] == "blocked"
    # 整轮拦下时 finish 也不执行，拿到同一条说明；它是伪工具，不记 tool_status
    finish_answer = results_for(ls.messages, "f3")
    assert finish_answer[0]["content"] == results_for(ls.messages, "p3")[0]["content"]
    assert "tool_status" not in finish_answer[0]["_meta"]
    assert ls.last_round_blocked is True
    assert answered_once(ls.messages)
