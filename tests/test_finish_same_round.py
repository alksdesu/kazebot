"""finish / ask 和其它工具同轮：其它工具照常执行，只拒终止调用；单独出现时读图失败和 finish_requires_tool 照旧拒整轮。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    ScriptedRegistry,
    answered_once,
    call,
    echo,
    make_loop_state,
    results_for,
    turn,
    use_hooks,
)
from engine.builtin.finish_guard import FinishGuardHandler  # noqa: E402
from engine.hooks.types import HookContext  # noqa: E402
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.pseudo_tools import _finish_spec  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.protocol import ACTION_FINISH  # noqa: E402

COLOCATED = "finish 没有执行：同一轮还有其它工具调用。先看这些工具的结果，再单独调用 finish。"
REQUIRES = "finish_requires_tool"


def _guard_ctx(
    pseudo: list[Any],
    real: list[Any],
    *,
    node_extra: dict[str, Any] | None = None,
    succeeded: tuple[str, ...] = (),
    failed: tuple[str, ...] = (),
) -> HookContext:
    loop_state = SimpleNamespace(
        succeeded_real_tools=set(succeeded),
        failed_real_tools=set(failed),
        rctx=SimpleNamespace(workspace_root=None),
        runtime_cfg={},
    )
    return HookContext(
        messages=[], tools=[], node=SimpleNamespace(id="node-1", extra=dict(node_extra or {})),
        provider=None, rctx=None, step=2, tool_calls=[*pseudo, *real],
        extra={
            "pseudo_calls": list(pseudo),
            "real_tool_calls": [{"id": item.id, "name": item.name, "arguments": item.arguments} for item in real],
            "loop_state": loop_state,
        },
    )


def _verdict(ctx: HookContext) -> Any:
    return asyncio.run(FinishGuardHandler().handle(ctx))


FINISH = call("finish", {"text": "done"}, call_id="f")
PROBE = call("probe", {}, call_id="p")
REPLY = call("reply", {"text": "稍等"}, call_id="r")


class TestGuard:
    def test_finish_next_to_another_tool_is_handed_back_instead_of_blocking_the_round(self) -> None:
        ctx = _guard_ctx([FINISH], [PROBE])

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is False and verdict.modified is True
        assert ctx.extra["rejected_terminal_calls"] == [(FINISH, COLOCATED)]

    def test_ask_next_to_a_dispatch_is_handed_back_too(self) -> None:
        ask = call("ask", {"text": "哪个文件？"}, call_id="a")
        dispatch = call("dispatch_to_helper", {"instruction": "look"}, call_id="d")
        ctx = _guard_ctx([dispatch, ask], [])

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is False
        assert ctx.extra["rejected_terminal_calls"] == [
            (ask, "ask 没有执行：同一轮还有其它工具调用。先看这些工具的结果，再单独调用 ask。"),
        ]

    def test_finish_with_reply_alone_passes(self) -> None:
        ctx = _guard_ctx([REPLY, FINISH], [])

        assert _verdict(ctx) is None
        assert "rejected_terminal_calls" not in ctx.extra

    def test_finish_alone_without_the_required_tool_still_blocks_the_round(self) -> None:
        ctx = _guard_ctx([REPLY, FINISH], [], node_extra={REQUIRES: {"any": ["draw"]}})

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is True and verdict.reason == REQUIRES

    def test_finish_without_the_required_tool_next_to_other_tools_only_loses_finish(self) -> None:
        ctx = _guard_ctx([FINISH], [PROBE], node_extra={REQUIRES: {"any": ["draw"]}})

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is False
        assert ctx.extra["rejected_terminal_calls"] == [(FINISH, COLOCATED)]

    def test_an_ungrounded_finish_after_a_vision_failure_alone_blocks_the_round(self) -> None:
        claim = call("finish", {"text": "图里是一只橘猫。"}, call_id="f")

        verdict = _verdict(_guard_ctx([claim], [], failed=("read_image",)))

        assert verdict is not None and verdict.block is True
        assert verdict.reason == "vision_failure_ungrounded_finish"

    def test_an_ungrounded_finish_after_a_vision_failure_next_to_other_tools_only_loses_finish(self) -> None:
        claim = call("finish", {"text": "图里是一只橘猫。"}, call_id="f")
        ctx = _guard_ctx([claim], [PROBE], failed=("read_image",))

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is False
        assert ctx.extra["rejected_terminal_calls"] == [(claim, COLOCATED)]

    def test_with_several_terminal_calls_the_last_one_is_checked(self) -> None:
        claim = call("finish", {"text": "图里是一只橘猫。"}, call_id="f1")
        honest = call("finish", {"text": "识图失败了，请重新发送图片。"}, call_id="f2")

        assert _verdict(_guard_ctx([claim, honest], [], failed=("read_image",))) is None

    def test_without_the_classified_lists_it_reads_the_raw_calls(self) -> None:
        ctx = HookContext(
            messages=[], tools=[], node=SimpleNamespace(id="node-1", extra={}), provider=None, rctx=None,
            tool_calls=[PROBE, FINISH], extra={},
        )

        verdict = _verdict(ctx)

        assert verdict is not None and verdict.block is False
        assert ctx.extra["rejected_terminal_calls"] == [(FINISH, COLOCATED)]

    def test_per_call_checks_are_ignored(self) -> None:
        ctx = _guard_ctx([FINISH], [PROBE])
        ctx.tool_call = PROBE

        assert _verdict(ctx) is None
        assert "rejected_terminal_calls" not in ctx.extra


def _loop(tmp_path: Path, monkeypatch: Any, *, node_extra: dict[str, Any] | None = None) -> tuple[Any, ScriptedRegistry]:
    use_hooks(monkeypatch, {"before_tool_call": [FinishGuardHandler()]})
    registry = ScriptedRegistry(tools={"probe": echo("probe output")})
    node = Node(id="node-1", type="ai", tool_mode="native", extra=dict(node_extra or {}))
    return make_loop_state(tmp_path, registry, node=node), registry


def _answer_order(ls: Any) -> list[str]:
    return [message["tool_call_id"] for message in ls.messages if message.get("role") == "tool"]


class TestLoop:
    def test_the_other_tools_run_and_only_finish_is_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)

        action = asyncio.run(_handle_tool_calls(ls, turn(PROBE, FINISH), 1))

        assert action is None
        assert registry.executed_names() == ["probe"]
        assert _answer_order(ls) == ["p", "f"]
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED
        assert "tool_status" not in results_for(ls.messages, "f")[0]["_meta"]
        assert results_for(ls.messages, "p")[0]["_meta"]["tool_status"] == "success"
        assert ls.last_round_blocked is False
        assert answered_once(ls.messages)

    def test_a_refused_call_listed_first_is_answered_first(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)

        asyncio.run(_handle_tool_calls(ls, turn(FINISH, PROBE), 1))

        assert _answer_order(ls) == ["f", "p"]
        assert registry.executed_names() == ["probe"]
        assert answered_once(ls.messages)

    def test_reply_still_goes_out_next_to_a_refused_finish(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)

        action = asyncio.run(_handle_tool_calls(ls, turn(REPLY, PROBE, FINISH), 1))

        assert action is None
        assert [payload["text"] for payload in ls.rctx.events.of("intermediate_reply")] == ["稍等"]
        assert _answer_order(ls) == ["r", "p", "f"]
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED

    def test_finish_alone_without_the_required_tool_refuses_the_whole_round(
        self, tmp_path: Path, monkeypatch: Any,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, node_extra={REQUIRES: {"any": ["draw"]}})

        action = asyncio.run(_handle_tool_calls(ls, turn(FINISH), 1))

        assert action is None
        assert "REJECTED: finish() cannot claim success yet" in results_for(ls.messages, "f")[0]["content"]
        assert ls.last_round_blocked is True

    def test_a_finish_without_the_required_tool_next_to_other_tools_gets_the_same_round_answer(
        self, tmp_path: Path, monkeypatch: Any,
    ) -> None:
        ls, registry = _loop(tmp_path, monkeypatch, node_extra={REQUIRES: {"any": ["draw"]}})

        asyncio.run(_handle_tool_calls(ls, turn(PROBE, FINISH), 1))

        assert registry.executed_names() == ["probe"]
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED
        assert ls.last_round_blocked is False

    def test_an_ungrounded_finish_next_to_other_tools_after_a_vision_failure(
        self, tmp_path: Path, monkeypatch: Any,
    ) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)
        ls.failed_real_tools.add("read_image")
        claim = call("finish", {"text": "图里是一只橘猫。"}, call_id="f")

        asyncio.run(_handle_tool_calls(ls, turn(PROBE, claim), 1))

        assert registry.executed_names() == ["probe"]
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED

    def test_of_two_finishes_alone_only_the_last_runs(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        first = call("finish", {"text": "first"}, call_id="f1")
        second = call("finish", {"text": "second"}, call_id="f2")

        action = asyncio.run(_handle_tool_calls(ls, turn(first, second), 1))

        assert action is not None and action.action == ACTION_FINISH
        assert action.result["text"] == "second"
        assert _answer_order(ls) == ["f1", "f2"]
        assert results_for(ls.messages, "f1")[0]["content"] == "finish 没有执行：同一轮调用了多个 finish/ask，只执行最后一个。"
        assert answered_once(ls.messages)


def test_the_finish_description_matches_the_new_behaviour() -> None:
    description = _finish_spec()["function"]["description"]

    assert "ALL calls will be REJECTED" not in description
    assert "those other tools still run but finish is REJECTED" in description
