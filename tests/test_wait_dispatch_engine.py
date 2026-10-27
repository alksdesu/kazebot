"""wait=true 的委派：同一轮成批交给 supervisor，占位结果和调用一一配对；和异步委派、真工具、finish/ask 同轮时各自怎么走；哪些情况不派出。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    NodeRun,
    ScriptedRegistry,
    answered_once,
    call,
    echo,
    make_loop_state,
    make_rctx,
    results_for,
    stored,
    turn,
    use_hooks,
)
from engine.builtin.attachment import AttachmentCollector  # noqa: E402
from engine.builtin.finish_guard import FinishGuardHandler  # noqa: E402
from engine.dispatch_batch import MAX_DISPATCH_BATCH_ITEMS  # noqa: E402
from engine.inference.ai_step import _FINAL_STEP_REJECT_MESSAGE, _handle_tool_calls  # noqa: E402
from engine.inference.tool_format import repair_tool_result_pairing  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_CANCELLED, ACTION_DISPATCH, ACTION_FINISH, ACTION_PREEMPTED  # noqa: E402

TARGETS = ("scout", "helper")
COLOCATED = "finish 没有执行：同一轮还有其它工具调用。先看这些工具的结果，再单独调用 finish。"


def _wait(target: str, instruction: str = "look around", *, call_id: str, **extra: Any):
    return call(f"dispatch_to_{target}", {"instruction": instruction, "wait": True, **extra}, call_id=call_id)


def _loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_mode: str = "native",
    handlers: dict[str, list[Any]] | None = None,
    task_context: dict[str, Any] | None = None,
) -> tuple[Any, ScriptedRegistry]:
    use_hooks(monkeypatch, handlers)
    registry = ScriptedRegistry(tools={"probe": echo("probe output")})
    ls = make_loop_state(
        tmp_path, registry, tool_mode=tool_mode, rctx=make_rctx(tmp_path, task_context=task_context),
    )
    ls.allowed_dispatch_targets = set(TARGETS)
    return ls, registry


def _run(ls: Any, *tool_calls: Any, final_step: bool = False) -> Any:
    return asyncio.run(_handle_tool_calls(ls, turn(*tool_calls), 1, final_step=final_step))


def _payload(ls: Any, call_id: str) -> dict[str, Any]:
    [message] = results_for(ls.messages, call_id)
    return json.loads(message["content"])


def _answer_order(ls: Any) -> list[str]:
    return [message["tool_call_id"] for message in ls.messages if message.get("role") == "tool"]


def _http_ok(session_id: str = "s-async") -> SimpleNamespace:
    response = SimpleNamespace(status_code=200, text="", json=lambda: {"session_id": session_id})
    return SimpleNamespace(post=AsyncMock(return_value=response))


class TestBatching:
    def test_a_single_wait_call_becomes_a_one_item_batch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action is not None and action.action == ACTION_DISPATCH
        assert action.dispatch_batch == [{
            "kind": "node", "target": "scout", "instruction": "look around",
            "context_mode": "fresh", "call_id": "w1",
        }]
        assert action.to_dict()["dispatch_batch"] == action.dispatch_batch
        assert _payload(ls, "w1")["success"] is True and _payload(ls, "w1")["status"] == "waiting"
        assert answered_once(ls.messages)

    def test_the_placeholder_is_stored_before_the_task_suspends(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        _run(ls, _wait("scout", call_id="w1"))

        stored_results = [message for message in stored(tmp_path) if message.tool_call_id == "w1"]
        assert len(stored_results) == 1
        assert "等待结果" in str(stored_results[0].content)

    def test_several_wait_calls_in_one_round_go_out_as_one_batch_in_call_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", "first", call_id="w1"), _wait("helper", "second", call_id="w2"))

        assert [item["target"] for item in action.dispatch_batch] == ["scout", "helper"]
        assert [item["instruction"] for item in action.dispatch_batch] == ["first", "second"]
        assert [item["call_id"] for item in action.dispatch_batch] == ["w1", "w2"]
        assert _answer_order(ls) == ["w1", "w2"]

    def test_explicit_context_settings_are_carried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1", context_mode="accumulate", context_key="topic-a"))

        assert action.dispatch_batch[0]["context_mode"] == "accumulate"
        assert action.dispatch_batch[0]["context_key"] == "topic-a"

    @pytest.mark.parametrize("raw", ["true", "yes", 1])
    def test_wait_accepts_the_usual_truthy_spellings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call("dispatch_to_scout", {"instruction": "go", "wait": raw}, call_id="w1"))

        assert action is not None and action.action == ACTION_DISPATCH

    @pytest.mark.parametrize("tool_mode", ["native", "json"])
    def test_every_call_is_answered_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_mode: str,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, tool_mode=tool_mode)

        action = _run(ls, call("probe", {}, call_id="p"), _wait("scout", call_id="w1"), _wait("helper", call_id="w2"))

        assert action.action == ACTION_DISPATCH
        assert answered_once(ls.messages)
        assert _answer_order(ls) == ["p", "w1", "w2"]
        assistant_index = next(index for index, message in enumerate(ls.messages) if message.get("role") == "assistant")
        assert all(
            index > assistant_index
            for index, message in enumerate(ls.messages) if message.get("role") == "tool"
        )
        assert {message.tool_call_id for message in stored(tmp_path) if message.role == "tool"} == {"p", "w1", "w2"}


class TestMixedRounds:
    def test_async_and_wait_dispatches_in_one_round_each_take_their_own_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        action = _run(ls, call("dispatch_to_helper", {"instruction": "later"}, call_id="a1"), _wait("scout", call_id="w1"))

        [posted] = ls.rctx.http.post.await_args_list
        assert posted.args[0].endswith("/v1/inbound")
        assert posted.kwargs["json"]["entry_node_id"] == "helper"
        assert [item["target"] for item in action.dispatch_batch] == ["scout"]
        # 应答里没有 task_id（旧 supervisor）时拼不出句柄，照样算派出了
        assert _payload(ls, "a1") == {
            "success": True, "session_id": "s-async", "message": "已异步委派给 helper，结果稍后会作为一条新消息送达。",
        }
        assert _payload(ls, "w1")["status"] == "waiting"
        assert answered_once(ls.messages)

    def test_real_tools_in_the_round_run_before_the_batch_goes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1"), call("probe", {}, call_id="p"))

        assert registry.executed_names() == ["probe"]
        assert action.action == ACTION_DISPATCH
        assert results_for(ls.messages, "p")[0]["_meta"]["tool_status"] == "success"


class TestTerminalsInTheSameRound:
    def test_finish_is_refused_through_the_guard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, handlers={"before_tool_call": [FinishGuardHandler()]})

        action = _run(ls, _wait("scout", call_id="w1"), call("finish", {"text": "done"}, call_id="f"))

        assert action.action == ACTION_DISPATCH
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED
        assert _answer_order(ls) == ["w1", "f"]
        assert answered_once(ls.messages)

    @pytest.mark.parametrize("terminal", ["finish", "ask"])
    def test_a_terminal_call_is_refused_even_without_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call(terminal, {"text": "done"}, call_id="t"), _wait("scout", call_id="w1"))

        assert action.action == ACTION_DISPATCH
        refusal = results_for(ls.messages, "t")[0]["content"]
        assert refusal.startswith(f"{terminal} 没有执行：同一轮发出了等结果的委派（wait=true）")
        assert _answer_order(ls) == ["t", "w1"]

    def test_a_terminal_call_next_to_only_refused_waits_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("intruder", call_id="w1"), call("finish", {"text": "done"}, call_id="f"))

        assert action is not None and action.action == ACTION_FINISH
        assert _payload(ls, "w1")["success"] is False


class TestRefusals:
    def test_a_target_outside_delegate_targets_is_refused_and_nothing_goes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("intruder", call_id="w1"))

        assert action is None
        payload = _payload(ls, "w1")
        assert payload["success"] is False and "delegate_targets" in payload["error"]
        assert "scout" in payload["error"]
        assert answered_once(ls.messages)

    def test_an_invalid_wait_next_to_a_valid_one_only_drops_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("intruder", call_id="x1"), _wait("scout", call_id="w1"))

        assert [item["target"] for item in action.dispatch_batch] == ["scout"]
        assert _payload(ls, "x1")["success"] is False
        assert _answer_order(ls) == ["x1", "w1"]

    def test_calls_beyond_the_per_round_limit_are_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        calls = [_wait("scout", f"job {index}", call_id=f"w{index}", context_mode="fresh") for index in range(MAX_DISPATCH_BATCH_ITEMS + 1)]

        action = _run(ls, *calls)

        assert len(action.dispatch_batch) == MAX_DISPATCH_BATCH_ITEMS
        overflow = _payload(ls, f"w{MAX_DISPATCH_BATCH_ITEMS}")
        assert overflow["success"] is False and f"最多 {MAX_DISPATCH_BATCH_ITEMS} 个" in overflow["error"]
        assert answered_once(ls.messages)

    def test_an_empty_instruction_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", "   ", call_id="w1"))

        assert action is None
        assert "instruction 不能为空" in _payload(ls, "w1")["error"]

    def test_two_waits_sharing_one_accumulate_context_keep_only_the_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(
            ls,
            _wait("scout", "a", call_id="w1", context_mode="accumulate"),
            _wait("scout", "b", call_id="w2", context_mode="accumulate"),
            _wait("scout", "c", call_id="w3", context_mode="accumulate", context_key="other"),
            _wait("scout", "d", call_id="w4", context_mode="fresh"),
        )

        assert [item["call_id"] for item in action.dispatch_batch] == ["w1", "w3", "w4"]
        assert "同一份上下文" in _payload(ls, "w2")["error"]

    def test_system_tasks_cannot_wait(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, task_context={"is_system_task": True})

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action is None
        assert "系统任务" in _payload(ls, "w1")["error"]

    def test_short_replies_cannot_wait(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, task_context={"route_hints": {"response_action": "short_reply"}})

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action is None
        assert "简短回应" in _payload(ls, "w1")["error"]

    def test_the_last_step_does_not_run_wait_dispatches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1"), final_step=True)

        assert action is None
        assert results_for(ls.messages, "w1")[0]["content"] == _FINAL_STEP_REJECT_MESSAGE.format(tool="dispatch_to_scout")


def _bad_wait(raw: Any) -> str:
    return (
        f"没有派出：wait 只能是 true 或 false，收到的是 {json.dumps(raw, ensure_ascii=False)}；"
        "要等结果就填 true，不等就填 false 或不填。"
    )


class TestWaitValues:
    @pytest.mark.parametrize("raw", ["sync", "是", "wait", 2, 1.5, ["true"], {"on": True}])
    def test_a_value_that_is_not_a_switch_is_refused_instead_of_going_out_async(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        action = _run(ls, call("dispatch_to_scout", {"instruction": "go", "wait": raw}, call_id="w1"))

        assert action is None
        assert ls.rctx.http.post.await_count == 0
        assert _payload(ls, "w1") == {"success": False, "error": _bad_wait(raw)}
        assert answered_once(ls.messages)

    @pytest.mark.parametrize("raw", [None, "", "  ", False, "false", "No", 0])
    def test_unset_or_false_still_goes_out_async(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        action = _run(ls, call("dispatch_to_scout", {"instruction": "go", "wait": raw}, call_id="a1"))

        assert action is None
        assert ls.rctx.http.post.await_count == 1
        assert _payload(ls, "a1")["success"] is True

    @pytest.mark.parametrize("raw", [True, "TRUE", " on "])
    def test_other_spellings_of_true_still_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        # 文本工具格式解出来的参数全是字符串，"true" 这类写法得照常认
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call("dispatch_to_scout", {"instruction": "go", "wait": raw}, call_id="w1"))

        assert action is not None and action.action == ACTION_DISPATCH
        assert [item["call_id"] for item in action.dispatch_batch] == ["w1"]

    def test_a_refused_value_leaves_the_rest_of_the_round_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        action = _run(
            ls,
            call("dispatch_to_helper", {"instruction": "later"}, call_id="a1"),
            call("dispatch_to_scout", {"instruction": "go", "wait": "同步"}, call_id="x1"),
            _wait("scout", call_id="w1"),
        )

        assert [item["call_id"] for item in action.dispatch_batch] == ["w1"]
        assert ls.rctx.http.post.await_count == 1
        assert _payload(ls, "x1") == {"success": False, "error": _bad_wait("同步")}
        assert _answer_order(ls) == ["a1", "x1", "w1"]
        assert answered_once(ls.messages)

    def test_a_long_value_is_cut_in_the_reason(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        _run(ls, call("dispatch_to_scout", {"instruction": "go", "wait": "x" * 200}, call_id="w1"))

        assert f'收到的是 "{"x" * 39}...<truncated>；' in _payload(ls, "w1")["error"]


class TestAttachments:
    def test_screened_attachments_ride_in_the_batch_item(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        image = tmp_path / "data" / "attachments" / "a.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.collected_attachments.append({"type": "image", "path": "data/attachments/a.png"})

        action = _run(ls, _wait(
            "scout", call_id="w1", attachment_paths=["data/attachments/a.png", "notes/secret.txt"],
        ))

        assert action.dispatch_batch[0]["attachments"] == [
            {"type": "image", "path": "data/attachments/a.png", "mime_type": "image/png", "name": "a.png"},
        ]
        payload = _payload(ls, "w1")
        assert payload["success"] is True
        assert [item["path"] for item in payload["refused_attachments"]] == ["notes/secret.txt"]

    def test_without_attachments_the_item_has_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1"))

        assert "attachments" not in action.dispatch_batch[0]


class TestInterruptions:
    def test_a_new_user_message_stops_the_batch_and_is_injected_next(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.check_preempted = AsyncMock(return_value={"preempted": True, "message": "等等，换个方向"})

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action is None
        assert ls.preempt_inject_info["message"] == "等等，换个方向"
        payload = _payload(ls, "w1")
        assert payload["success"] is False and "新消息" in payload["error"]
        assert answered_once(ls.messages)

    def test_a_soft_preempt_saves_the_context_instead_of_dispatching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.check_preempted = AsyncMock(return_value={"preempted": True})

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action.action == ACTION_PREEMPTED
        assert "打断" in _payload(ls, "w1")["error"]

    def test_a_cancelled_task_does_not_dispatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.check_cancelled = AsyncMock(return_value=True)

        action = _run(ls, _wait("scout", call_id="w1"))

        assert action.action == ACTION_CANCELLED
        assert "已取消" in _payload(ls, "w1")["error"]

    def test_a_round_that_ends_early_marks_its_waits_as_not_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = _http_ok()

        action = _run(ls, _wait("scout", call_id="w1"), call("switch_node", {"target": "", "text": "好"}, call_id="s"))

        assert action.action == ACTION_FINISH
        payload = _payload(ls, "w1")
        assert payload["success"] is False and "提前结束" in payload["error"]
        assert answered_once(ls.messages)


class TestRoundTail:
    """工具产出图片时那条 user 消息要排在本轮所有结果后面，夹在中间的话配对修复会把后面的结果当孤儿删掉。"""

    def _draw_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, guard: bool) -> Any:
        image = tmp_path / "data" / "attachments" / "drawn.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\n")

        async def draw(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            attachment = {"type": "image", "path": "data/attachments/drawn.png", "mime_type": "image/png"}
            return {"ok": True, "data": {"result": "drawn", "attachments": [attachment]}}

        handlers: dict[str, list[Any]] = {"after_tool_call": [AttachmentCollector()]}
        if guard:
            handlers["before_tool_call"] = [FinishGuardHandler()]
        use_hooks(monkeypatch, handlers)
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"draw": draw}))
        ls.allowed_dispatch_targets = set(TARGETS)
        return ls

    @staticmethod
    def _after_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = next(i for i, message in enumerate(messages) if message.get("role") == "assistant")
        return messages[index + 1:]

    def test_wait_placeholders_and_refusals_come_before_the_image_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = self._draw_loop(tmp_path, monkeypatch, guard=False)

        action = _run(ls, call("draw", {}, call_id="d"), _wait("scout", call_id="w1"), call("finish", {"text": "x"}, call_id="f"))

        assert action.action == ACTION_DISPATCH
        tail = self._after_assistant(ls.messages)
        assert [message.get("tool_call_id") or message["role"] for message in tail] == ["d", "w1", "f", "user"]
        assert tail[-1]["_meta"]["message_type"] == "tool_result_attachment"
        repaired = repair_tool_result_pairing(ls.messages)
        assert [message["tool_call_id"] for message in repaired if message.get("role") == "tool"] == ["d", "w1", "f"]
        reloaded = [message.role for message in stored(tmp_path)]
        assert reloaded == ["assistant", "tool", "tool", "tool", "user"]

    def test_a_late_refused_finish_also_stays_paired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = self._draw_loop(tmp_path, monkeypatch, guard=True)

        action = _run(ls, call("draw", {}, call_id="d"), call("finish", {"text": "x"}, call_id="f"))

        assert action is None
        tail = self._after_assistant(ls.messages)
        assert [message.get("tool_call_id") or message["role"] for message in tail] == ["d", "f", "user"]
        assert results_for(ls.messages, "f")[0]["content"] == COLOCATED
        repaired = repair_tool_result_pairing(ls.messages)
        assert [message["tool_call_id"] for message in repaired if message.get("role") == "tool"] == ["d", "f"]
        assert ls.tool_produced_attachments[0]["path"] == "data/attachments/drawn.png"


def _delegating_node(**fields: Any) -> Node:
    values: dict[str, Any] = {
        "id": "node-1", "type": "ai", "prompt": "Work carefully.", "tool_mode": "native",
        "tool_access": ToolAccess(mode="all"), "delegate_targets": ["scout"],
    }
    values.update(fields)
    return Node(**values)


def _dispatch_tool(run: NodeRun, request_index: int = 0) -> dict[str, Any]:
    return next(
        tool["function"] for tool in run.requests[request_index]["tools"]
        if tool.get("function", {}).get("name") == "dispatch_to_scout"
    )


class TestWholeNode:
    def test_dispatch_tools_offer_wait(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())

        action = run.run(turn(_wait("scout", call_id="w1")))

        assert action.action == ACTION_DISPATCH
        assert "wait" in _dispatch_tool(run)["parameters"]["properties"]
        assert "wait: true" in _dispatch_tool(run)["description"]

    def test_system_tasks_get_no_wait_parameter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node(),
            task_context={"is_system_task": True},
        )

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert "wait" not in _dispatch_tool(run)["parameters"]["properties"]

    def test_a_wait_next_to_an_unauthorized_call_on_a_node_without_tools_still_goes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(),
            node=_delegating_node(tool_access=ToolAccess(mode="none")),
        )

        action = run.run(turn(_wait("scout", call_id="w1"), call("bogus", {}, call_id="b"), text="交给 scout 了"))

        assert action.action == ACTION_DISPATCH
        assert [item["target"] for item in action.dispatch_batch] == ["scout"]


def _batch_resume(**entry: Any) -> dict[str, Any]:
    base = {
        "kind": "node", "node_id": "scout", "status": "finish", "instruction": "look around",
        "text": "found two leads", "summary": "", "call_id": "w1", "child_task_id": "child-1",
        "child_session_id": "child_abc",
    }
    base.update(entry)
    return {"type": "batch_results", "entries": [base]}


class TestResume:
    def test_the_batch_result_reaches_the_model_and_is_stored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")), instruction="", resume_data=_batch_resume())

        rendered = [
            message for message in run.requests[0]["messages"]
            if str(message.get("content") or "").startswith("子节点 scout")
        ]
        assert len(rendered) == 1
        assert rendered[0]["content"].startswith("子节点 scout（调用 w1；指令：look around）已完成。")
        assert "found two leads" in rendered[0]["content"]
        [kept] = [message for message in stored(tmp_path) if message.message_type == "dispatch_result"]
        assert kept.content == rendered[0]["content"]
        assert kept.meta["child_node_id"] == "scout"
        assert kept.meta["child_task_id"] == "child-1"
        assert kept.meta["child_session_id"] == "child_abc"
        assert kept.meta["resume_type"] == "child_result"
        assert kept.source_task_id == run.rctx.task_id

    def test_child_attachments_go_out_with_a_finish_that_names_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        image = tmp_path / "data" / "attachments" / "child.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        attachment = {"type": "image", "path": "data/attachments/child.png", "mime_type": "image/png", "name": "child.png"}
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())

        action = run.run(
            turn(call("finish", {"text": "画好了"}, call_id="f")),
            instruction="",
            resume_data=_batch_resume(attachments=[attachment, {"type": "image", "path": "../outside.png"}]),
        )

        assert action.action == ACTION_FINISH
        assert [item["path"] for item in action.result["attachments"]] == ["data/attachments/child.png"]
