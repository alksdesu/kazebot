"""转到后台的工具：取消只看发起任务（被取消、失败、会话重置），不看领取，挂起再恢复不会把它杀掉；
它在后台等审批不算发起任务在等，转后台那一刻还没等完的审批交还给任务。占位文字说清结果交给谁。
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import time
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
    ScriptedRegistry,
    call,
    echo,
    make_loop_state,
    make_rctx,
    ok,
    real_calls,
    results_for,
    use_hooks,
)
from engine.inference import ai_step  # noqa: E402
from engine.inference.ai_step import _execute_real_tools  # noqa: E402
from engine.inference.pseudo_tools import _ASYNC_TOOL_HINT, _to_openai_tools  # noqa: E402
from toolbox import approval_wait  # noqa: E402
from toolbox.approval_wait import (  # noqa: E402
    ApprovalWaitRelay,
    relay_approval_waits,
    report_approval_waits_to,
    waiting_for_approval,
)
from toolbox.context import ToolContext  # noqa: E402

_DISPATCHER = "to the node that dispatched it"


class _Answer:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        return self.body

    def raise_for_status(self) -> None:
        return None


class _Http:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.gets: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> _Answer:
        self.gets.append((url, kwargs))
        answer = self.answers.pop(0) if self.answers else _Answer(200, {"cancelled": False})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ctx(tmp_path: Path, http: Any, *, detached: bool = True, **extra: Any) -> ToolContext:
    return ToolContext(
        supervisor_url="http://sup", session_id="branch-1", run_id="run-1", worker_id="worker-1",
        workspace_root=tmp_path, http=http, registry=None, task_id="task-1", parent_session_id="sess-1",
        claim_id="claim-1", detached=detached, **extra,
    )


class _Listener:
    def __init__(self) -> None:
        self.events: list[str] = []

    def approval_wait_started(self) -> None:
        self.events.append("start")

    def approval_wait_ended(self) -> None:
        self.events.append("end")


async def _noop() -> None:
    return None


class TestDetachedCancelProbe:
    def test_it_asks_about_the_task_and_its_session_not_the_claim(self, tmp_path: Path) -> None:
        http = _Http(_Answer(200, {"cancelled": False}))

        assert asyncio.run(_ctx(tmp_path, http).check_cancelled()) is False

        assert http.gets == [(
            "http://sup/v1/async_tools/task-1/cancelled", {"params": {"session_id": "sess-1"}, "timeout": 2.0},
        )]

    def test_answers_are_reused_for_a_little_while(self, tmp_path: Path) -> None:
        http = _Http()
        ctx = _ctx(tmp_path, http)

        async def probe_twice() -> list[bool]:
            return [await ctx.check_cancelled(), await ctx.check_cancelled()]

        assert asyncio.run(probe_twice()) == [False, False]
        assert len(http.gets) == 1

        ctx._detached_checked_at = float("-inf")
        asyncio.run(ctx.check_cancelled())

        assert len(http.gets) == 2

    def test_once_cancelled_it_stays_cancelled_without_asking_again(self, tmp_path: Path) -> None:
        http = _Http(_Answer(200, {"cancelled": True}))
        ctx = _ctx(tmp_path, http)

        assert asyncio.run(ctx.check_cancelled()) is True
        ctx._detached_checked_at = float("-inf")
        assert asyncio.run(ctx.check_cancelled()) is True

        assert len(http.gets) == 1

    @pytest.mark.parametrize("status", [404, 405])
    def test_an_older_supervisor_is_asked_by_task_without_the_claim(self, tmp_path: Path, status: int) -> None:
        http = _Http(_Answer(status, {"detail": "Not Found"}), _Answer(200, {"cancelled": True}))

        assert asyncio.run(_ctx(tmp_path, http).check_cancelled()) is True

        assert http.gets[1] == ("http://sup/v1/tasks/task-1/cancelled", {"timeout": 2.0})

    @pytest.mark.parametrize("answer", [OSError("supervisor down"), _Answer(500, {})])
    def test_an_unanswered_probe_does_not_stop_the_tool(self, tmp_path: Path, answer: Any) -> None:
        assert asyncio.run(_ctx(tmp_path, _Http(answer)).check_cancelled()) is False

    def test_a_tool_still_in_its_task_asks_with_its_claim(self, tmp_path: Path) -> None:
        http = _Http(_Answer(200, {"cancelled": True}))

        assert asyncio.run(_ctx(tmp_path, http, detached=False).check_cancelled()) is True

        assert http.gets == [(
            "http://sup/v1/tasks/task-1/cancelled", {"timeout": 2.0, "params": {"claim_id": "claim-1"}},
        )]

    def test_a_copy_keeps_the_mode_but_not_the_cached_answer(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, _Http())
        ctx._detached_cancelled = True
        ctx._detached_checked_at = time.monotonic()

        copied = dataclasses.replace(ctx)

        assert copied.detached is True
        assert copied._detached_cancelled is False and copied._detached_checked_at == float("-inf")

    @pytest.mark.parametrize("detached, renewals", [(True, 0), (False, 1)])
    def test_waiting_for_approval_in_the_background_does_not_renew_the_tasks_lease(
        self, tmp_path: Path, detached: bool, renewals: int,
    ) -> None:
        http = _Http(_Answer(200, {"status": "pending"}), _Answer(200, {"status": "allowed"}))
        ctx = _ctx(tmp_path, http, detached=detached, approval_poll_interval_sec=0.01)
        ctx.check_cancelled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        ctx.renew_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]

        approval = asyncio.run(ctx.wait_for_approval("ap-1"))

        assert approval["status"] == "allowed"
        assert ctx.renew_lease.await_count == renewals


class TestApprovalRelay:
    def test_waits_are_passed_on(self) -> None:
        listener = _Listener()
        relay = ApprovalWaitRelay(listener)

        relay.approval_wait_started()
        relay.approval_wait_ended()
        relay.approval_wait_ended()

        assert listener.events == ["start", "end"]

    def test_detaching_closes_the_open_waits_and_stops_passing_on(self) -> None:
        listener = _Listener()
        relay = ApprovalWaitRelay(listener)
        relay.approval_wait_started()
        relay.approval_wait_started()

        relay.detach()
        relay.detach()
        relay.approval_wait_started()
        relay.approval_wait_ended()

        assert listener.events == ["start", "start", "end", "end"]

    def test_a_relay_without_a_target_does_nothing(self) -> None:
        relay = ApprovalWaitRelay(None)

        relay.approval_wait_started()
        relay.approval_wait_ended()
        relay.detach()

    def test_coroutines_started_inside_report_through_the_relay(self) -> None:
        listener = _Listener()

        async def scenario() -> None:
            gate = asyncio.Event()

            async def waits() -> None:
                with waiting_for_approval():
                    await gate.wait()

            with report_approval_waits_to(listener):
                with relay_approval_waits() as relay:
                    task = asyncio.create_task(waits())
            await asyncio.sleep(0.01)
            relay.detach()
            gate.set()
            await task

        asyncio.run(scenario())

        assert listener.events == ["start", "end"]


def _upgrade_cfg(threshold: float = 0.05) -> dict[str, Any]:
    return {"meta": {"execute_command": {
        "async_upgrade": {"enabled": True, "threshold_sec": threshold}, "default_timeout_sec": 90,
    }}}


class TestAutoUpgrade:
    @pytest.fixture(autouse=True)
    def _hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_hooks(monkeypatch)

    @pytest.fixture
    def background(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        record = SimpleNamespace(spawned=[], delivered={})

        def spawn(coroutine: Any, *, name: str) -> None:
            record.spawned.append((name, approval_wait._LISTENER.get()))
            coroutine.close()

        def deliver(exec_task: Any, **kwargs: Any) -> Any:
            record.delivered.update(kwargs, exec_task=exec_task)
            return _noop()

        monkeypatch.setattr(ai_step, "_spawn_background", spawn)
        monkeypatch.setattr(ai_step, "_deliver_started_async_task", deliver)
        return record

    def test_a_command_moved_to_the_background_detaches_and_hands_back_its_approval_wait(
        self, tmp_path: Path, background: SimpleNamespace,
    ) -> None:
        seen: dict[str, Any] = {}

        async def waits_for_approval(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen["ctx"] = ctx
            with waiting_for_approval():
                await asyncio.sleep(5)
            return ok("finished")

        rctx = make_rctx(tmp_path, parent_session_id="parent-1")
        ls = make_loop_state(
            tmp_path, ScriptedRegistry(tools={"execute_command": waits_for_approval}), runtime_cfg=_upgrade_cfg(), rctx=rctx,
        )
        listener = _Listener()

        async def scenario() -> None:
            with report_approval_waits_to(listener):
                await _execute_real_tools(
                    ls, real_calls(call("execute_command", {"command": "make", "timeout_sec": 60}, call_id="e")), step=1,
                )

        asyncio.run(scenario())

        # 转后台之前在等审批算任务在等；转的那一刻报结束，之后后台再怎么等都不报
        assert listener.events == ["start", "end"]
        assert seen["ctx"].detached is True
        assert [listener_at_spawn for _name, listener_at_spawn in background.spawned] == [None]
        assert background.delivered["session_id"] == "parent-1" and background.delivered["task_id"] == "task-1"
        content = results_for(ls.messages, "e")[0]["content"]
        assert "auto-upgraded to async" in content and "also while this task waits for a sub-agent" in content
        assert _DISPATCHER in content and "preempt" not in content

    def test_a_command_that_finishes_in_time_stays_in_the_task(
        self, tmp_path: Path, background: SimpleNamespace,
    ) -> None:
        seen: dict[str, Any] = {}

        async def quick(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen["ctx"] = ctx
            with waiting_for_approval():
                await asyncio.sleep(0)
            return ok("finished")

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"execute_command": quick}), runtime_cfg=_upgrade_cfg(5))
        listener = _Listener()

        async def scenario() -> None:
            with report_approval_waits_to(listener):
                await _execute_real_tools(
                    ls, real_calls(call("execute_command", {"command": "make", "timeout_sec": 60}, call_id="e")), step=1,
                )

        asyncio.run(scenario())

        assert listener.events == ["start", "end"]
        assert seen["ctx"].detached is False and background.spawned == []
        assert results_for(ls.messages, "e")[0]["content"] == "finished"

    def test_an_interrupted_round_takes_the_command_down_with_it(self, tmp_path: Path) -> None:
        state: dict[str, bool] = {}

        async def slow(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            state["running"] = True
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return ok("never")

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"execute_command": slow}))
        tool_ctx = _ctx(tmp_path, _Http(), detached=False)
        rtc = {"id": "e", "name": "execute_command", "arguments": {"command": "make"}}

        async def scenario() -> None:
            outer = asyncio.create_task(ai_step._execute_with_async_upgrade(
                ls, rtc, tool_ctx, threshold=10.0, started_at=time.monotonic(),
            ))
            await asyncio.sleep(0.1)
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer
            await asyncio.sleep(0.05)

        asyncio.run(scenario())

        assert state == {"running": True, "cancelled": True}

    def test_an_async_mode_tool_runs_detached_and_its_waits_are_not_the_tasks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, background: SimpleNamespace,
    ) -> None:
        captured: dict[str, Any] = {}

        def run_async_tool(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return _noop()

        monkeypatch.setattr(ai_step, "_run_async_tool", run_async_tool)
        rctx = make_rctx(tmp_path, parent_session_id="parent-1")
        registry = ScriptedRegistry(tools={"draw": echo()}, specs={"draw": {"async_mode": True}})
        ls = make_loop_state(tmp_path, registry, rctx=rctx)
        listener = _Listener()

        async def scenario() -> None:
            with report_approval_waits_to(listener):
                await _execute_real_tools(ls, real_calls(call("draw", {"prompt": "cat"}, call_id="d")), step=1)

        asyncio.run(scenario())

        assert [listener_at_spawn for _name, listener_at_spawn in background.spawned] == [None]
        assert captured["tool_ctx"].detached is True and captured["session_id"] == "parent-1"
        content = results_for(ls.messages, "d")[0]["content"]
        assert content.startswith('⏳ Async tool "draw" started') and _DISPATCHER in content
        assert "preempt" not in content and listener.events == []

    def test_the_async_hint_says_where_the_result_goes(self) -> None:
        [tool] = _to_openai_tools([{
            "name": "draw", "description": "Draw.", "input_schema": {"type": "object"}, "async_mode": True,
        }])

        assert tool["function"]["description"] == "Draw." + _ASYNC_TOOL_HINT
        assert _DISPATCHER in _ASYNC_TOOL_HINT and "preempt" not in _ASYNC_TOOL_HINT


class TestUpgradeThreshold:
    @pytest.mark.parametrize("args, expected", [
        ({"timeout_sec": 70}, 56.0),
        ({"timeout_sec": 0}, 60.0),
        ({"timeout_sec": "abc"}, 60.0),
        ({}, 60.0),
        ({"timeout_sec": 30}, None),
        ({"timeout_sec": 10**9}, 60.0),
    ])
    def test_the_threshold_uses_the_clamped_timeout(self, args: dict[str, Any], expected: float | None) -> None:
        assert ai_step._execute_command_async_upgrade_threshold("execute_command", args, {}) == expected

    def test_the_instance_maximum_limits_how_late_it_moves(self) -> None:
        cfg = {"meta": {"execute_command": {"max_timeout_sec": 100, "async_upgrade": {"threshold_sec": 90}}}}

        assert ai_step._execute_command_async_upgrade_threshold("execute_command", {"timeout_sec": 10**9}, cfg) == 80.0

    def test_only_execute_command_and_only_when_enabled(self) -> None:
        disabled = {"meta": {"execute_command": {"async_upgrade": {"enabled": False}}}}

        assert ai_step._execute_command_async_upgrade_threshold("read_file", {}, {}) is None
        assert ai_step._execute_command_async_upgrade_threshold("execute_command", {}, disabled) is None


class _Posts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> Any:
        self.calls.append((url, json))
        return SimpleNamespace(status_code=200)


class _Registry:
    def get_spec(self, name: str) -> None:
        return None


def _deliver(result: Any) -> dict[str, Any]:
    http = _Posts()
    asyncio.run(ai_step._run_async_deliver(
        registry=_Registry(), http=http, supervisor_url="http://sup", task_id="task-1", session_id="sess-1",
        tool_name="execute_command", tool_args={}, async_tool_id="a1", started_at=time.monotonic(),
        node_id="bootstrap.coder", result=result,
    ))
    [(url, body)] = http.calls
    assert url == "http://sup/v1/async_tools/results"
    return body


class TestBackgroundResults:
    _TIMED_OUT = {
        "ok": False, "error": "timeout after 5.0s", "timed_out": True,
        "data": {
            "result": "ERROR: timeout after 5.0s; the command was killed. Output received before that:\nbuilding 3/7",
            "output": "building 3/7", "timed_out": True,
        },
    }

    def test_what_a_timed_out_command_printed_comes_back_with_the_failure(self) -> None:
        body = _deliver(self._TIMED_OUT)

        assert body["success"] is False and body["error"] == "timeout after 5.0s" and "cancelled" not in body
        assert "failed" in body["message"] and "Output received before that:\nbuilding 3/7" in body["message"]
        assert body["session_id"] == "sess-1" and body["task_id"] == "task-1"

    def test_a_cancelled_tool_is_flagged_so_the_supervisor_drops_it(self) -> None:
        body = _deliver({"ok": False, "error": "task cancelled", "cancelled": True, "data": {"cancelled": True}})

        assert body["cancelled"] is True and body["success"] is False

    @pytest.mark.parametrize("result, error, detail", [
        (_TIMED_OUT, "timeout after 5.0s", _TIMED_OUT["data"]["result"][len("ERROR: "):]),
        ({"ok": False, "error": "boom", "data": {"result": "ERROR: boom"}}, "boom", "boom"),
        ({"ok": False, "error": "boom", "data": {"result": "something else"}}, "boom", "boom"),
        ("not a dict", "boom", "boom"),
    ])
    def test_only_text_that_extends_the_error_is_added(self, result: Any, error: str, detail: str) -> None:
        assert ai_step._async_failure_detail(result, error) == detail
