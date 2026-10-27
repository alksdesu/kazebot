"""engine 一侧的异步委派：派出后拿到句柄，await_dispatches 本轮最后挂起，list / cancel 转给 supervisor，wait 委派的单项超时，异步 fork 从调用方自己写对话的会话复制，恢复后的结果和异步结果前缀怎么写给模型。"""
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
    TASK_ID,
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
from engine.dispatch_batch import (  # noqa: E402
    AWAIT_KEY,
    AwaitRequest,
    DispatchTimeouts,
    await_request,
    parse_timeout_sec,
)
from engine.inference import pseudo_handlers  # noqa: E402
from engine.inference.ai_step import _FINAL_STEP_REJECT_MESSAGE, _handle_tool_calls  # noqa: E402
from engine.inference.pseudo_tools import _dispatch_control_specs, _dispatch_delegate_spec  # noqa: E402
from engine.inference.resume_builder import DISPATCH_RESULT_TYPE, _build_resume_messages  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_CANCELLED, ACTION_DISPATCH  # noqa: E402
from engine.runner import _dispatch_result_headline  # noqa: E402
from toolbox.tool_names import PSEUDO_TOOL_NAMES, is_pseudo_tool_name  # noqa: E402

TARGETS = ("scout", "helper")
SUPERVISOR = "http://supervisor.invalid"
H1 = "child-1@agent-session-1"
H2 = "child-2@agent-session-2"
CONTROL_TOOLS = {"await_dispatches", "list_dispatches", "cancel_dispatch"}


def _loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_context: dict[str, Any] | None = None,
    targets: tuple[str, ...] = TARGETS,
    runtime_cfg: dict[str, Any] | None = None,
    rctx: Any = None,
) -> tuple[Any, ScriptedRegistry]:
    use_hooks(monkeypatch)
    registry = ScriptedRegistry(tools={"probe": echo("probe output")})
    ls = make_loop_state(
        tmp_path, registry, rctx=rctx or make_rctx(tmp_path, task_context=task_context), runtime_cfg=runtime_cfg,
    )
    ls.allowed_dispatch_targets = set(targets)
    return ls, registry


def _run(ls: Any, *tool_calls: Any, final_step: bool = False) -> Any:
    return asyncio.run(_handle_tool_calls(ls, turn(*tool_calls), 1, final_step=final_step))


def _payload(ls: Any, call_id: str) -> dict[str, Any]:
    [message] = results_for(ls.messages, call_id)
    return json.loads(message["content"])


def _answer_order(ls: Any) -> list[str]:
    return [message["tool_call_id"] for message in ls.messages if message.get("role") == "tool"]


def _response(body: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=json.dumps(body, ensure_ascii=False), json=lambda: body)


def _inbound_answer(task_id: str = "child-1", session_id: str = "agent-session-1") -> SimpleNamespace:
    return _response({"session_id": session_id, "inbound_seq": 7, "accepted": True, "task_id": task_id})


def _async(target: str = "helper", instruction: str = "画一只猫", *, call_id: str = "a1", **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": instruction, **extra}, call_id=call_id)


def _wait(target: str, instruction: str = "look around", *, call_id: str, **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": instruction, "wait": True, **extra}, call_id=call_id)


def _await_call(*handles: str, call_id: str = "aw", **extra: Any) -> Any:
    return call("await_dispatches", {"handles": list(handles), **extra}, call_id=call_id)


def _running(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "node", "status": "running", "state": "running", "node_id": "scout",
        "instruction": "crawl the docs", "handle": H1, "child_task_id": "child-1",
    }
    entry.update(fields)
    return entry


def _render(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return _build_resume_messages({"type": "batch_results", "entries": list(entries)})


def _delegating_node(**fields: Any) -> Node:
    values: dict[str, Any] = {
        "id": "node-1", "type": "ai", "prompt": "Work carefully.", "tool_mode": "native",
        "tool_access": ToolAccess(mode="all"), "delegate_targets": ["scout"],
    }
    values.update(fields)
    return Node(**values)


class TestAsyncDispatchResult:
    def test_an_async_dispatch_answers_with_the_handle_of_the_task_it_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_inbound_answer()))

        action = _run(ls, _async(call_id="a1"))

        assert action is None
        payload = _payload(ls, "a1")
        assert payload["success"] is True and payload["status"] == "dispatched"
        assert payload["handle"] == "child-1@agent-session-1" and payload["session_id"] == "agent-session-1"
        assert "await_dispatches" in payload["message"] and "cancel_dispatch" in payload["message"]
        sent = ls.rctx.http.post.await_args
        assert sent.args[0] == f"{SUPERVISOR}/v1/inbound"
        assert sent.kwargs["json"]["dispatch_origin"]["caller_task_id"] == TASK_ID

    def test_an_empty_instruction_is_refused_before_anything_is_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_inbound_answer()))

        _run(ls, _async(instruction="   ", call_id="a1"))

        assert ls.rctx.http.post.await_count == 0
        payload = _payload(ls, "a1")
        assert payload["success"] is False and "instruction 不能为空" in payload["error"]

    def test_timeout_sec_on_an_async_dispatch_points_to_await_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_inbound_answer()))

        _run(ls, _async(call_id="a1", timeout_sec=30))

        payload = _payload(ls, "a1")
        assert payload["success"] is True and "await_dispatches" in payload["note"]
        assert "timeout_sec" not in ls.rctx.http.post.await_args.kwargs["json"]


class TestAsyncFork:
    def test_an_entry_task_forks_from_its_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rctx = make_rctx(tmp_path, session_id="branch-1", parent_session_id="main-1")
        ls, _registry = _loop(tmp_path, monkeypatch, rctx=rctx)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_inbound_answer()))

        _run(ls, _async(call_id="a1", context_mode="fork"))

        sent = ls.rctx.http.post.await_args.kwargs["json"]
        assert sent["dispatch_fork_from_session"] == "branch-1"
        assert sent["dispatch_origin"]["parent_session_id"] == "main-1"
        assert sent["dispatch_context_mode"] == "fork"

    def test_a_child_node_forks_from_its_own_child_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rctx = make_rctx(tmp_path, session_id="caller-branch", parent_session_id="main-1", child_session_id="child-s1")
        ls, _registry = _loop(tmp_path, monkeypatch, rctx=rctx)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_inbound_answer()))

        _run(ls, _async(call_id="a1", context_mode="fork"))

        assert ls.rctx.http.post.await_args.kwargs["json"]["dispatch_fork_from_session"] == "child-s1"

    @pytest.mark.parametrize("mode, expected", [("fork", ["flush", "post"]), ("fresh", ["post"])])
    def test_queued_writes_land_before_a_fork_is_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: list[str],
    ) -> None:
        order: list[str] = []

        async def flush(rctx: Any) -> None:
            order.append("flush")

        async def post(url: str, json: Any = None, timeout: Any = None) -> SimpleNamespace:
            order.append("post")
            return _inbound_answer()

        monkeypatch.setattr(pseudo_handlers, "flush_conversation_writes", flush)
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=post)

        _run(ls, _async(call_id="a1", context_mode=mode))

        assert order == expected


class TestWaitItemTimeout:
    @pytest.mark.parametrize("raw, expected", [
        (30, 30), (999999, 3600), (12.2, 13), ("45", 45), (None, None), ("", None),
    ])
    def test_a_wait_dispatch_carries_its_timeout_within_the_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any, expected: int | None,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        extra = {} if raw is None else {"timeout_sec": raw}

        action = _run(ls, _wait("scout", call_id="w1", **extra))

        assert action.action == ACTION_DISPATCH
        assert action.dispatch_batch[0].get("timeout_sec") == expected
        message = _payload(ls, "w1")["message"]
        assert (f"最多等 {expected} 秒" in message) is (expected is not None)

    @pytest.mark.parametrize("raw", ["soon", -5, 0, True])
    def test_a_bad_timeout_refuses_only_that_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: Any,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", "a", call_id="w1", timeout_sec=raw), _wait("helper", "b", call_id="w2"))

        assert [item["call_id"] for item in action.dispatch_batch] == ["w2"]
        assert "timeout_sec 要填正数秒数" in _payload(ls, "w1")["error"]
        assert answered_once(ls.messages)

    def test_the_limit_follows_the_runtime_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, runtime_cfg={"engine": {"dispatch": {"max_timeout_sec": 120}}})

        action = _run(ls, _wait("scout", call_id="w1", timeout_sec=999))

        assert action.dispatch_batch[0]["timeout_sec"] == 120


class TestAwaitDispatches:
    def test_await_suspends_the_task_once_the_round_is_done(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call("probe", {}, call_id="p"), _await_call(H1, H2, call_id="aw", mode="any", timeout_sec=90))

        assert action is not None and action.action == ACTION_DISPATCH
        assert action.dispatch_input == {
            AWAIT_KEY: {"handles": [H1, H2], "mode": "any", "timeout_sec": 90, "call_id": "aw"},
        }
        assert action.dispatch_batch == [] and action.target_node == ""
        assert registry.executed_names() == ["probe"]
        payload = _payload(ls, "aw")
        assert payload["success"] is True and payload["status"] == "waiting"
        assert "2 个异步委派" in payload["message"] and "任意一个结束" in payload["message"]
        assert "90 秒" in payload["message"] and "note" not in payload
        assert answered_once(ls.messages)
        assert [message.tool_call_id for message in stored(tmp_path) if message.role == "tool"] == ["p", "aw"]
        assert ls.rctx.events.of("handoff_progress")[-1]["message"] == "[node-1] 等待 2 个异步委派"

    def test_the_supervisor_reads_back_exactly_what_the_engine_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _await_call(H1, call_id="aw", timeout_sec=45))

        assert await_request(action.to_dict()) == AwaitRequest(handles=(H1,), mode="all", timeout_sec=45, call_id="aw")

    def test_handles_are_tidied_before_they_go_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call("await_dispatches", {"handles": [f" {H1} ", H1, "", None, H2]}, call_id="aw"))

        request = action.dispatch_input[AWAIT_KEY]
        assert request["handles"] == [H1, H2]
        assert request["mode"] == "all" and "timeout_sec" not in request
        assert "600 秒" in _payload(ls, "aw")["message"]

    def test_a_wait_beyond_the_limit_is_clamped_and_noted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _await_call(H1, call_id="aw", timeout_sec=999999))

        assert action.dispatch_input[AWAIT_KEY]["timeout_sec"] == 3600
        assert "3600 秒" in _payload(ls, "aw")["note"]

    def test_finish_in_the_same_round_is_not_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _await_call(H1, call_id="aw"), call("finish", {"text": "done"}, call_id="f"))

        assert action.action == ACTION_DISPATCH
        assert results_for(ls.messages, "f")[0]["content"] == (
            "finish 没有执行：同一轮调用了 await_dispatches，等到的结果在下一条消息回来；看完结果再单独调用 finish。"
        )
        assert _answer_order(ls) == ["aw", "f"]
        assert answered_once(ls.messages)

    def test_an_await_next_to_a_wait_dispatch_is_refused_and_the_wait_goes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _wait("scout", call_id="w1"), _await_call(H1, call_id="aw"))

        assert [item["call_id"] for item in action.dispatch_batch] == ["w1"]
        assert AWAIT_KEY not in action.dispatch_input
        payload = _payload(ls, "aw")
        assert payload["success"] is False and "wait=true" in payload["error"]
        assert _answer_order(ls) == ["w1", "aw"]
        assert answered_once(ls.messages)

    def test_only_the_first_await_of_a_round_counts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _await_call(H1, call_id="aw1"), _await_call(H2, call_id="aw2"))

        assert action.dispatch_input[AWAIT_KEY]["handles"] == [H1]
        assert "同一轮只能调用一次" in _payload(ls, "aw2")["error"]
        assert _answer_order(ls) == ["aw1", "aw2"]

    @pytest.mark.parametrize("arguments, fragment", [
        ({"handles": []}, "handles 不能为空"),
        ({"handles": 5}, "handles 要填句柄列表"),
        ({"handles": "not-a-handle"}, "格式不对"),
        ({"handles": [H1, "not-a-handle"]}, "格式不对"),
        ({"handles": [f"t{index}@s{index}" for index in range(17)]}, "一次最多等 16 个"),
        ({"handles": [H1], "mode": "some"}, "mode 只能是"),
        ({"handles": [H1], "timeout_sec": "soon"}, "timeout_sec 要填正数秒数"),
        ({"handles": [H1], "timeout_sec": -3}, "timeout_sec 要填正数秒数"),
    ])
    def test_bad_arguments_are_refused_without_suspending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], fragment: str,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, call("await_dispatches", arguments, call_id="aw"))

        assert action is None
        payload = _payload(ls, "aw")
        assert payload["success"] is False and fragment in payload["error"]
        assert answered_once(ls.messages)

    @pytest.mark.parametrize("task_context, targets, fragment", [
        ({"is_system_task": True}, TARGETS, "系统任务"),
        ({"route_hints": {"response_action": "short_reply"}}, TARGETS, "简短回应"),
        ({}, (), "没有委派目标"),
    ])
    def test_some_tasks_cannot_await(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        task_context: dict[str, Any], targets: tuple[str, ...], fragment: str,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, task_context=task_context, targets=targets)

        action = _run(ls, _await_call(H1, call_id="aw"))

        assert action is None
        assert fragment in _payload(ls, "aw")["error"]

    def test_a_new_user_message_stops_the_await(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.check_preempted = AsyncMock(return_value={"preempted": True, "message": "等等，先别等了"})

        action = _run(ls, _await_call(H1, call_id="aw"))

        assert action is None
        assert ls.preempt_inject_info["message"] == "等等，先别等了"
        assert "用户刚发来了新消息" in _payload(ls, "aw")["error"]
        assert answered_once(ls.messages)

    def test_a_cancelled_task_does_not_start_waiting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.check_cancelled = AsyncMock(return_value=True)

        action = _run(ls, _await_call(H1, call_id="aw"))

        assert action.action == ACTION_CANCELLED
        assert "已取消" in _payload(ls, "aw")["error"]

    def test_the_last_step_does_not_await(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)

        action = _run(ls, _await_call(H1, call_id="aw"), final_step=True)

        assert action is None
        assert results_for(ls.messages, "aw")[0]["content"] == _FINAL_STEP_REJECT_MESSAGE.format(tool="await_dispatches")


class TestListAndCancelTools:
    def test_list_relays_what_the_supervisor_lists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {
            "ok": True, "active": [{"handle": H1, "node_id": "scout", "status": "running"}], "finished": [],
            "active_total": 3, "finished_total": 0,
        }
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(get=AsyncMock(return_value=_response(body)))

        action = _run(ls, call("list_dispatches", {}, call_id="l"))

        assert action is None
        assert ls.rctx.http.get.await_args.args[0] == f"{SUPERVISOR}/v1/tasks/{TASK_ID}/dispatches"
        payload = _payload(ls, "l")
        assert payload["success"] is True and payload["active"] == body["active"] and payload["finished"] == []
        assert "一共 3 个" in payload["note"]

    def test_an_empty_list_says_there_is_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"ok": True, "active": [], "finished": [], "active_total": 0, "finished_total": 0}
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(get=AsyncMock(return_value=_response(body)))

        _run(ls, call("list_dispatches", {}, call_id="l"))

        assert _payload(ls, "l")["note"] == "这段对话里现在没有异步委派。"

    def test_a_supervisor_error_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(
            get=AsyncMock(return_value=_response({"detail": "caller task not found or not active"}, status=404)),
        )

        _run(ls, call("list_dispatches", {}, call_id="l"))

        assert _payload(ls, "l") == {
            "success": False, "error": "查询异步委派失败：caller task not found or not active",
        }

    @pytest.mark.parametrize("body, message", [
        (
            {"ok": True, "handle": H1, "node_id": "scout", "cancelled_count": 3, "stopping_count": 0, "status": "cancelled"},
            "已取消 scout 这个委派。它派出去还没结束的 2 个子任务一并取消。",
        ),
        (
            {"ok": True, "handle": H1, "node_id": "scout", "cancelled_count": 0, "stopping_count": 1, "status": "stopping"},
            "已通知 scout 停下：它会在手上这一步做完后结束，不再交回结果。",
        ),
        (
            {
                "ok": True, "handle": H1, "node_id": "scout", "cancelled_count": 1, "stopping_count": 0,
                "status": "finish", "already_finished": True,
            },
            "scout 这个委派已经结束了（finish），它本身不用取消。它派出去还没结束的 1 个子任务一并取消。",
        ),
    ])
    def test_cancel_reports_what_was_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], message: str,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=AsyncMock(return_value=_response(body)))

        _run(ls, call("cancel_dispatch", {"handle": f" {H1} "}, call_id="c"))

        posted = ls.rctx.http.post.await_args
        assert posted.args[0] == f"{SUPERVISOR}/v1/tasks/{TASK_ID}/dispatches/cancel"
        assert posted.kwargs["json"] == {"handle": H1}
        payload = _payload(ls, "c")
        assert payload["success"] is True and payload["handle"] == H1 and payload["message"] == message

    def test_a_handle_that_cannot_be_read_is_refused_before_asking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(post=AsyncMock())

        _run(ls, call("cancel_dispatch", {"handle": "garbage"}, call_id="c"))

        assert ls.rctx.http.post.await_count == 0
        assert "格式不对" in _payload(ls, "c")["error"]

    def test_a_refusal_from_the_supervisor_is_passed_on(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch)
        ls.rctx.http = SimpleNamespace(
            post=AsyncMock(return_value=_response({"ok": False, "reason": "not_found", "message": "找不到这个委派。"})),
        )

        _run(ls, call("cancel_dispatch", {"handle": H1}, call_id="c"))

        assert _payload(ls, "c") == {"success": False, "error": "找不到这个委派。"}

    def test_a_node_without_delegate_targets_has_nothing_to_list_or_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, targets=())
        ls.rctx.http = SimpleNamespace(get=AsyncMock(), post=AsyncMock())

        _run(ls, call("list_dispatches", {}, call_id="l"), call("cancel_dispatch", {"handle": H1}, call_id="c"))

        assert ls.rctx.http.get.await_count == 0 and ls.rctx.http.post.await_count == 0
        assert "没有委派目标" in _payload(ls, "l")["error"]
        assert "没有委派目标" in _payload(ls, "c")["error"]

    def test_a_short_reply_cannot_cancel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls, _registry = _loop(tmp_path, monkeypatch, task_context={"route_hints": {"response_action": "short_reply"}})
        ls.rctx.http = SimpleNamespace(post=AsyncMock())

        _run(ls, call("cancel_dispatch", {"handle": H1}, call_id="c"))

        assert ls.rctx.http.post.await_count == 0
        assert answered_once(ls.messages)


class TestTimeoutLimits:
    def test_the_defaults(self) -> None:
        assert DispatchTimeouts.from_config({}) == DispatchTimeouts(await_default=600, maximum=3600)

    def test_the_default_wait_never_exceeds_the_ceiling(self) -> None:
        timeouts = DispatchTimeouts.from_config({"engine": {"dispatch": {"await_timeout_sec": 900, "max_timeout_sec": 300}}})

        assert timeouts == DispatchTimeouts(await_default=300, maximum=300)

    def test_the_ceiling_has_a_floor_and_a_roof(self) -> None:
        low = DispatchTimeouts.from_config({"engine": {"dispatch": {"max_timeout_sec": 10}}})
        high = DispatchTimeouts.from_config({"engine": {"dispatch": {"max_timeout_sec": 10 ** 9}}})

        assert (low.maximum, high.maximum) == (60, 21600)

    def test_clamp(self) -> None:
        assert [DispatchTimeouts().clamp(value) for value in (1, 5, 600, 3600, 99999)] == [5, 5, 600, 3600, 3600]

    @pytest.mark.parametrize("raw, expected", [
        (30, 30), ("30", 30), (" 30 ", 30), (12.2, 13), (0, None), (-1, None), (True, None), ("", None),
        (None, None), ("soon", None), (float("inf"), None), (float("nan"), None),
    ])
    def test_parse_timeout_sec(self, raw: Any, expected: int | None) -> None:
        assert parse_timeout_sec(raw) == expected

    def test_the_supervisor_reads_a_request_leniently(self) -> None:
        action = {"dispatch_input": {AWAIT_KEY: {
            "handles": [H1, f" {H1}", H2, None, 5, True], "mode": "ANY", "timeout_sec": "40", "call_id": " aw ",
        }}}

        assert await_request(action) == AwaitRequest(handles=(H1, H2, "5"), mode="any", timeout_sec=40, call_id="aw")

    def test_an_odd_request_falls_back_to_waiting_for_all(self) -> None:
        handles = [f"t{index}@s{index}" for index in range(20)]

        request = await_request({"dispatch_input": {AWAIT_KEY: {"handles": handles, "mode": "weird", "timeout_sec": -1}}})

        assert request == AwaitRequest(handles=tuple(handles[:16]), mode="all", timeout_sec=None, call_id="")
        assert await_request({"dispatch_input": {"instruction": "x"}}) is None
        assert await_request({"dispatch_batch": [{"target": "scout"}]}) is None


class TestToolSpecs:
    def test_the_dispatch_tools_are_reserved_engine_pseudo_tools(self) -> None:
        names = [spec["function"]["name"] for spec in _dispatch_control_specs(allow_await=True)]

        assert names == ["await_dispatches", "list_dispatches", "cancel_dispatch"]
        assert set(names) <= PSEUDO_TOOL_NAMES
        assert all(is_pseudo_tool_name(name) for name in names)

    def test_system_tasks_are_not_offered_await(self) -> None:
        names = [spec["function"]["name"] for spec in _dispatch_control_specs(allow_await=False)]

        assert names == ["list_dispatches", "cancel_dispatch"]

    def test_cancel_needs_a_handle_and_list_needs_nothing(self) -> None:
        list_spec, cancel_spec = _dispatch_control_specs(allow_await=False)

        assert list_spec["function"]["parameters"]["properties"] == {}
        assert cancel_spec["function"]["parameters"]["required"] == ["handle"]

    def test_the_limits_shown_follow_the_runtime_config(self) -> None:
        timeouts = DispatchTimeouts.from_config({"engine": {"dispatch": {"await_timeout_sec": 90, "max_timeout_sec": 120}}})

        await_spec = _dispatch_control_specs(allow_await=True, timeouts=timeouts)[0]["function"]
        wait_spec = _dispatch_delegate_spec("scout", allow_wait=True, timeouts=timeouts)["function"]
        async_only = _dispatch_delegate_spec("scout", allow_wait=False)["function"]

        properties = await_spec["parameters"]["properties"]
        assert "90 秒" in properties["timeout_sec"]["description"]
        assert "5-120" in properties["timeout_sec"]["description"]
        assert properties["handles"]["maxItems"] == 16 and await_spec["parameters"]["required"] == ["handles"]
        assert properties["mode"]["enum"] == ["all", "any"]
        assert "5-120" in wait_spec["parameters"]["properties"]["timeout_sec"]["description"]
        assert "timeout_sec" not in async_only["parameters"]["properties"]


class TestWholeNode:
    def test_a_delegating_node_gets_the_three_tools(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert CONTROL_TOOLS <= set(run.tool_names(0))

    def test_a_system_task_gets_no_await(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node(),
            task_context={"is_system_task": True},
        )

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        names = set(run.tool_names(0))
        assert "await_dispatches" not in names
        assert {"list_dispatches", "cancel_dispatch"} <= names

    def test_a_node_without_delegate_targets_gets_none_of_them(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node(delegate_targets=[]),
        )

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert not CONTROL_TOOLS & set(run.tool_names(0))

    def test_an_await_ends_the_run_as_a_dispatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())

        action = run.run(turn(_await_call(H1, call_id="aw")))

        assert action.action == ACTION_DISPATCH
        assert action.dispatch_input == {AWAIT_KEY: {"handles": [H1], "mode": "all", "call_id": "aw"}}

    def test_what_an_await_brings_back_reaches_the_model_and_is_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_delegating_node())
        resume = {
            "type": "batch_results",
            "entries": [
                _running(timed_out=True, waited_sec=30),
                {"kind": "node", "status": "unavailable", "handle": "bad", "error": "句柄格式不对。"},
            ],
            "await": {"mode": "all", "timeout_sec": 30, "timed_out": True, "call_id": "aw"},
        }

        run.run(turn(call("finish", {"text": "还有一个在跑"}, call_id="f")), instruction="", resume_data=resume)

        contents = [str(message.get("content") or "") for message in run.requests[0]["messages"]]
        assert any(content.startswith("子节点 scout（指令：crawl the docs）等了 30 秒还没做完") for content in contents)
        assert "句柄 bad 拿不到结果：句柄格式不对。" in contents
        assert len([message for message in stored(tmp_path) if message.message_type == DISPATCH_RESULT_TYPE]) == 2


class TestResumeRendering:
    def test_a_dispatch_still_running_is_kept_for_the_caller(self) -> None:
        [message] = _render(_running())

        assert message["content"] == (
            "子节点 scout（指令：crawl the docs）还在运行。\n"
            "它的结果会留给你：之后再调用 await_dispatches 收取；本任务结束时还没收取的，会作为单独的消息交回。\n"
            f"句柄：{H1}"
        )
        assert message["_meta"] == {
            "message_type": DISPATCH_RESULT_TYPE, "child_node_id": "scout", "child_task_id": "child-1", "handle": H1,
        }

    def test_a_dispatch_that_outran_the_wait_says_how_long_was_waited(self) -> None:
        [message] = _render(_running(timed_out=True, waited_sec=30))

        assert message["content"].startswith("子节点 scout（指令：crawl the docs）等了 30 秒还没做完，仍在运行。")

    def test_a_wait_cut_short_by_the_task_lifetime_says_so(self) -> None:
        [message] = _render(_running(timed_out=True, waited_sec=1200, lifetime_capped=True))

        assert message["content"].startswith("子节点 scout（指令：crawl the docs）还没做完：本任务的总运行时长快到上限了")

    def test_a_handle_it_could_not_use_is_named_with_the_reason(self) -> None:
        [message] = _render({"kind": "node", "status": "unavailable", "handle": "bad", "error": "句柄格式不对。"})

        assert message["content"] == "句柄 bad 拿不到结果：句柄格式不对。"
        assert message["_meta"] == {"message_type": DISPATCH_RESULT_TYPE, "handle": "bad"}

    @pytest.mark.parametrize("where, note", [("inbound", "不要重复汇报"), ("join", "另一次 await_dispatches")])
    def test_a_result_delivered_earlier_says_where(self, where: str, note: str) -> None:
        [message] = _render({
            "kind": "node", "status": "finish", "node_id": "scout", "instruction": "crawl", "text": "two pages",
            "summary": "", "handle": H1, "delivered_earlier": where,
        })

        assert "two pages" in message["content"] and note in message["content"]
        assert message["content"].endswith(f"句柄：{H1}")

    def test_a_wait_item_that_timed_out_reads_as_a_failure_that_can_be_continued(self) -> None:
        [message] = _render({
            "kind": "node", "status": "fail", "node_id": "scout", "instruction": "crawl", "text": "", "summary": "",
            "error": "等待超时：30 秒内没有做完，已被停下。", "timed_out": True, "handle": "c1@child_x", "continuable": True,
        })

        assert message["content"].startswith("子节点 scout（指令：crawl）执行失败：等待超时：30 秒内没有做完，已被停下。")
        assert "continue_from" in message["content"]


class TestAsyncResultPrefix:
    @pytest.mark.parametrize("status, headline, label", [
        ("finish", "[Async task completed] Node scout finished.", "Result"),
        (
            "ask",
            "[Async task needs input] Node scout stopped to ask for more information and has ended. "
            "Dispatch it again with the missing details if the work should go on.",
            "Question",
        ),
        ("fail", "[Async task failed] Node scout failed and returned no result. Do not guess its output.", "Error"),
        ("", "[Async task completed] Node scout finished.", "Result"),
        ("exploded", "[Async task completed] Node scout finished.", "Result"),
    ])
    def test_the_prefix_follows_how_the_dispatch_ended(self, status: str, headline: str, label: str) -> None:
        assert _dispatch_result_headline({"inbound_child_status": status}, "scout") == (headline, label)

    def test_a_missing_node_name_still_reads(self) -> None:
        headline, _label = _dispatch_result_headline({"inbound_child_status": "fail"}, "")

        assert headline.startswith("[Async task failed] Node unknown failed")
