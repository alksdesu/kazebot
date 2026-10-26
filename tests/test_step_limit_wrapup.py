"""max_steps 收尾：最后一步的提示、native 只给 finish、模型不听话时怎么交付、哪些任务维持原来的失败。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, echo, stored, turn  # noqa: E402
from engine.builtin.finish_guard import FinishGuardHandler  # noqa: E402
from engine.builtin.plaintext import TRUNCATED_NOTICE  # noqa: E402
from engine.inference.ai_step import STEP_LIMIT_NOTICE  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FAIL, ACTION_FINISH  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402

FINAL_HINT = "这是本任务允许的最后一步"


def _probe(index: int) -> ProviderResponse:
    return turn(call("probe", {"round": index}, call_id=f"p{index}"))


def _finish(text: str, call_id: str = "f") -> ProviderResponse:
    return turn(call("finish", {"text": text}, call_id=call_id))


def _node_run(
    monkeypatch: Any,
    workspace: Path,
    *,
    max_steps: int = 3,
    tool_mode: str = "native",
    task_context: dict[str, Any] | None = None,
    handlers: dict[str, list[Any]] | None = None,
    node_extra: dict[str, Any] | None = None,
) -> NodeRun:
    node = Node(
        id="node-1", type="ai", prompt="Work carefully.", tool_mode=tool_mode,
        tool_access=ToolAccess(mode="all"), extra=dict(node_extra or {}),
    )
    return NodeRun(
        monkeypatch, workspace,
        registry=ScriptedRegistry(tools={"probe": echo("probe output")}),
        node=node,
        runtime_cfg={"engine": {"max_steps": max_steps}},
        task_context=task_context,
        handlers=handlers,
    )


def _hints(run: NodeRun) -> list[int]:
    return [
        sum(1 for message in request["messages"] if FINAL_HINT in str(message.get("content")))
        for request in run.requests
    ]


class TestHint:
    def test_the_hint_is_added_only_before_the_last_step_and_never_stored(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _probe(1), _finish("all done"))

        assert action.action == ACTION_FINISH and action.result["text"] == "all done"
        # 听提示在最后一步 finish 也是被迫收尾：标记照打，正文不加说明
        assert action.result["step_limit_reached"] is True
        assert action.result["wrapup"] == {
            "reason": "steps", "steps_used": 3, "max_steps": 3, "todos": [], "unfinished": 0,
        }
        assert _hints(run) == [0, 0, 1]
        last = run.requests[2]["messages"][-1]
        assert last["role"] == "user" and last["_retry_hint"] is True
        assert last["content"].startswith("[SYSTEM] 这是本任务允许的最后一步（已用 2/3 步）。")
        assert not any(FINAL_HINT in str(message.content) for message in stored(tmp_path))

    def test_a_finish_before_the_last_step_carries_no_wrap_up_marker(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _finish("early"))

        assert action.action == ACTION_FINISH and action.result["text"] == "early"
        assert "step_limit_reached" not in action.result
        assert "time_limit_reached" not in action.result
        assert "wrapup" not in action.result

    def test_native_mode_offers_only_finish_on_the_last_step(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)

        run.run(_probe(0), _probe(1), _finish("ok"))

        assert run.tool_names(2) == ["finish"]
        assert {"probe", "finish", "ask", "reply"} <= set(run.tool_names(0))
        assert run.tool_names(1) == run.tool_names(0)

    @pytest.mark.parametrize("tool_mode", ["json", "fake-native"])
    def test_json_and_fake_native_rely_on_the_hint_alone(self, tmp_path: Path, monkeypatch: Any, tool_mode: str) -> None:
        run = _node_run(monkeypatch, tmp_path, tool_mode=tool_mode)

        run.run(_probe(0), _probe(1), _finish("ok"))

        assert run.tool_names(2) == run.tool_names(0)
        assert run.requests[2]["messages"][-1]["content"].startswith("[SYSTEM] " + FINAL_HINT)


class TestDisobedientLastStep:
    def test_tool_calls_are_refused_and_the_text_is_delivered(self, tmp_path: Path, monkeypatch: Any) -> None:
        ended: list[Any] = []

        class EndRecorder:
            name = "end_recorder"
            priority = 0

            async def handle(self, ctx: Any) -> None:
                ended.append(ctx.extra["task_action"])

        run = _node_run(monkeypatch, tmp_path, handlers={"on_task_end": [EndRecorder()]})
        last = turn(call("probe", {"round": 2}, call_id="p2"), text="查到了两条线索，第三条还没查。")

        action = run.run(_probe(0), _probe(1), last)

        assert action.action == ACTION_FINISH
        assert action.result["text"] == f"查到了两条线索，第三条还没查。\n\n{STEP_LIMIT_NOTICE}"
        assert action.result["step_limit_reached"] is True and action.result["implicit_finish"] is True
        assert run.registry.executed_names() == ["probe", "probe"]
        refused = [message for message in stored(tmp_path) if message.tool_call_id == "p2"]
        assert len(refused) == 1 and FINAL_HINT in refused[0].content
        assert refused[0].meta["tool_status"] == "blocked"
        assert ended == [action]

    def test_without_any_text_the_wrap_up_lists_the_tools_that_ran(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _probe(1), turn(call("probe", {"round": 2}, call_id="p2")))

        assert action.action == ACTION_FINISH
        assert action.result["text"].startswith("任务还没有完成。已经执行过的工具：probe ×2。")
        assert action.result["text"].endswith(STEP_LIMIT_NOTICE)
        assert action.result["step_limit_reached"] is True

    def test_plain_text_on_the_last_step_is_delivered_as_is(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _probe(1), turn(text="结论：问题出在配置。"))

        assert action.action == ACTION_FINISH
        assert action.result["text"] == "结论：问题出在配置。"
        assert action.result["step_limit_reached"] is True

    def test_truncated_plain_text_on_the_last_step_says_it_was_cut(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)
        cut = ProviderResponse(ok=True, text="写到一半的结", finish_reason="length")

        action = run.run(_probe(0), _probe(1), cut)

        assert action.result["text"] == f"写到一半的结\n\n{TRUNCATED_NOTICE}"
        assert action.result["truncated"] is True and action.result["step_limit_reached"] is True

    def test_json_mode_strips_the_protocol_blocks_from_the_delivered_text(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path, tool_mode="json")
        last = ProviderResponse(ok=True, text=(
            '还差最后一个文件没看。\n<<<TOOL_CALL>>>\n{"name": "probe", "arguments": {"round": 2}}\n<<<END_TOOL_CALL>>>'
        ))

        action = run.run(_probe(0), _probe(1), last)

        assert action.result["text"] == f"还差最后一个文件没看。\n\n{STEP_LIMIT_NOTICE}"
        assert run.registry.executed_names() == ["probe", "probe"]

    def test_a_finish_the_guard_refuses_does_not_deliver_its_claim(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(
            monkeypatch, tmp_path,
            handlers={"before_tool_call": [FinishGuardHandler()]},
            node_extra={"finish_requires_tool": {"any": ["draw"]}},
        )
        claim = turn(call("finish", {"text": "画好了"}, call_id="f"), text="画好了")

        action = run.run(_probe(0), _probe(1), claim)

        assert action.action == ACTION_FINISH
        assert action.result["text"].startswith("任务还没有完成。已经执行过的工具：probe ×2。")
        assert "画好了" not in action.result["text"]

    def test_with_nothing_to_deliver_the_task_still_fails(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path)
        replies = [turn(call("reply", {"text": f"进度 {index}"}, call_id=f"r{index}")) for index in range(3)]

        action = run.run(*replies)

        assert action.action == ACTION_FAIL and action.error == "达到最大步数限制。"


class TestUntouched:
    def test_system_tasks_keep_the_old_failure(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path, task_context={"is_system_task": True})

        action = run.run(_probe(0), _probe(1), _probe(2))

        assert action.action == ACTION_FAIL and action.error == "达到最大步数限制。"
        assert run.registry.executed_names() == ["probe"] * 3
        assert _hints(run) == [0, 0, 0]
        assert run.tool_names(2) == run.tool_names(0)

    def test_nodes_with_fewer_than_three_steps_keep_the_old_failure(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = _node_run(monkeypatch, tmp_path, max_steps=10, node_extra={"max_steps": 2})

        action = run.run(_probe(0), _probe(1))

        assert action.action == ACTION_FAIL and action.error == "达到最大步数限制。"
        assert run.registry.executed_names() == ["probe", "probe"]
        assert _hints(run) == [0, 0]
