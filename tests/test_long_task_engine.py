"""engine 一侧的长任务收尾：运行时长到点前的最后一步、挂起前用掉的步数接着数、续跑带来的待办、委派结果里的句柄和 continue_from。"""
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
    ToolFunc,
    call,
    echo,
    make_loop_state,
    make_rctx,
    ok,
    results_for,
    stored,
    turn,
    use_hooks,
)
from engine.inference import ai_step  # noqa: E402
from engine.inference.pseudo_handlers import (  # noqa: E402
    _handle_pseudo_dispatch,
    _resolve_dispatch_request,
    prepare_wait_dispatches,
)
from engine.inference.resume_builder import _build_resume_messages  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH  # noqa: E402
from engine.run_budget import (  # noqa: E402
    AUTO_CONTINUE_KEY,
    STEP_LIMIT_NOTICE,
    TIME_LIMIT_NOTICE,
    RunBudget,
)
from engine.runner import _carried_todos, _dispatch_handle_lines  # noqa: E402
from engine.todo_list import TODO_TOOL_NAME, carried_todo_items, render_update_result  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402

TIME_HINT = "本任务这一段的运行时间快到上限了"
STEP_HINT = "这是本任务允许的最后一步"
PLAN = [
    {"content": "读需求", "status": "completed"},
    {"content": "改代码", "status": "in_progress"},
    {"content": "补测试", "status": "pending"},
]


def _probe(index: int) -> ProviderResponse:
    return turn(call("probe", {"round": index}, call_id=f"p{index}"))


def _finish(text: str) -> ProviderResponse:
    return turn(call("finish", {"text": text}, call_id="f"))


def _ticking_probe(clock: list[float], seconds: float) -> ToolFunc:
    """每调一次让假时钟走 seconds 秒，模拟工具执行耗时。"""
    async def tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        clock[0] += seconds
        return ok("probe output")

    return tool


def _node(**extra: Any) -> Node:
    return Node(
        id="node-1", type="ai", prompt="Work carefully.", tool_mode="native",
        tool_access=ToolAccess(mode="all"), extra=dict(extra),
    )


def _timed_run(
    monkeypatch: Any, workspace: Path, *, max_steps: int = 10, task_context: dict[str, Any] | None = None,
) -> tuple[NodeRun, RunBudget]:
    clock = [0.0]
    run = NodeRun(
        monkeypatch, workspace,
        registry=ScriptedRegistry(tools={"probe": _ticking_probe(clock, 100.0)}),
        node=_node(),
        runtime_cfg={"engine": {"max_steps": max_steps}},
        task_context=task_context,
    )
    return run, RunBudget(deadline=150.0, started=0.0, clock=lambda: clock[0])


def _count(run: NodeRun, marker: str) -> list[int]:
    return [
        sum(1 for message in request["messages"] if marker in str(message.get("content")))
        for request in run.requests
    ]


class TestTimeWrapup:
    def test_the_step_that_finds_the_budget_spent_is_the_last_one(self, tmp_path: Path, monkeypatch: Any) -> None:
        run, budget = _timed_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _probe(1), _finish("查完两处，第三处没来得及"), run_budget=budget)

        assert action.action == ACTION_FINISH and action.result["text"] == "查完两处，第三处没来得及"
        assert action.result["time_limit_reached"] is True
        assert "step_limit_reached" not in action.result
        assert action.result["wrapup"] == {
            "reason": "time", "steps_used": 3, "max_steps": 10, "todos": [], "unfinished": 0,
        }
        assert _count(run, TIME_HINT) == [0, 0, 1]
        hint = run.requests[2]["messages"][-1]
        assert hint["content"].startswith("[SYSTEM] 本任务这一段的运行时间快到上限了（已运行约 3 分钟），这是最后一步。")
        assert run.tool_names(2) == ["finish"]
        assert "probe" in run.tool_names(1)
        assert not any(TIME_HINT in str(message.content) for message in stored(tmp_path))

    def test_a_disobedient_last_step_is_delivered_with_the_time_notice(self, tmp_path: Path, monkeypatch: Any) -> None:
        run, budget = _timed_run(monkeypatch, tmp_path)
        last = turn(call("probe", {"round": 2}, call_id="p2"), text="查完两处。")

        action = run.run(_probe(0), _probe(1), last, run_budget=budget)

        assert action.action == ACTION_FINISH
        assert action.result["text"] == f"查完两处。\n\n{TIME_LIMIT_NOTICE}"
        assert action.result["time_limit_reached"] is True and action.result["implicit_finish"] is True
        assert action.result["wrapup"]["reason"] == "time"
        assert run.registry.executed_names() == ["probe", "probe"]

    def test_a_silent_last_step_lists_what_ran_with_the_time_notice(self, tmp_path: Path, monkeypatch: Any) -> None:
        run, budget = _timed_run(monkeypatch, tmp_path)

        action = run.run(_probe(0), _probe(1), _probe(2), run_budget=budget)

        assert action.result["text"].startswith("任务还没有完成。已经执行过的工具：probe ×2。")
        assert action.result["text"].endswith(TIME_LIMIT_NOTICE)
        assert STEP_LIMIT_NOTICE not in action.result["text"]

    def test_a_budget_that_never_runs_out_changes_nothing(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node())
        budget = RunBudget(deadline=1e9, started=0.0, clock=lambda: 0.0)

        action = run.run(_probe(0), _finish("done"), run_budget=budget)

        assert action.result["text"] == "done"
        assert "time_limit_reached" not in action.result and "wrapup" not in action.result
        assert _count(run, TIME_HINT) == [0, 0]

    def test_system_tasks_ignore_the_budget(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(),
            task_context={"is_system_task": True},
        )
        spent = RunBudget(deadline=-1.0, started=0.0, clock=lambda: 0.0)

        action = run.run(_probe(0), _finish("done"), run_budget=spent)

        assert action.result["text"] == "done"
        assert "time_limit_reached" not in action.result
        assert _count(run, TIME_HINT) == [0, 0]
        assert run.registry.executed_names() == ["probe"]

    def test_nodes_too_short_to_wrap_up_ignore_the_budget(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(max_steps=2),
        )
        spent = RunBudget(deadline=-1.0, started=0.0, clock=lambda: 0.0)

        action = run.run(_probe(0), _finish("done"), run_budget=spent)

        assert action.result["text"] == "done"
        assert "time_limit_reached" not in action.result

    def test_the_time_hint_lists_unfinished_todos(self, tmp_path: Path, monkeypatch: Any) -> None:
        clock = [0.0]
        run = NodeRun(
            monkeypatch, tmp_path,
            registry=ScriptedRegistry(tools={"probe": _ticking_probe(clock, 200.0)}),
            node=_node(todo=True),
            runtime_cfg={"engine": {"max_steps": 10}},
        )
        budget = RunBudget(deadline=150.0, started=0.0, clock=lambda: clock[0])

        action = run.run(
            turn(call(TODO_TOOL_NAME, {"items": PLAN}, call_id="t1"), call("probe", {}, call_id="p0")),
            _finish("改了一半"),
            run_budget=budget,
        )

        hint = run.requests[1]["messages"][-1]["content"]
        assert hint.startswith(f"[SYSTEM] {TIME_HINT}")
        assert "还没完成的待办：\n1. [进行中] 改代码\n2. [待办] 补测试" in hint
        assert action.result["wrapup"]["todos"] == PLAN and action.result["wrapup"]["unfinished"] == 2


class TestStepsCarriedOver:
    def test_steps_used_before_a_suspend_count_toward_the_limit(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(_probe(1), _finish("接着做完了"), steps_used=1)

        assert len(run.requests) == 2
        assert [request["step"] for request in run.requests] == [1, 2]
        assert _count(run, STEP_HINT) == [0, 1]
        assert run.requests[1]["messages"][-1]["content"].startswith("[SYSTEM] 这是本任务允许的最后一步（已用 2/3 步）。")
        assert action.result["wrapup"]["steps_used"] == 3
        assert run.rctx.step_offset == 1 and run.rctx.completed_steps == 3

    def test_with_one_step_left_the_first_step_is_the_last(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(_finish("收尾"), steps_used=2)

        assert run.tool_names(0) == ["finish"]
        assert action.result["step_limit_reached"] is True
        assert action.result["wrapup"]["steps_used"] == 3

    @pytest.mark.parametrize("used", [3, 50])
    def test_a_limit_lowered_while_suspended_still_leaves_one_step(
        self, tmp_path: Path, monkeypatch: Any, used: int,
    ) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(_finish("收尾"), steps_used=used)

        assert action.action == ACTION_FINISH and action.result["step_limit_reached"] is True
        assert run.requests[0]["messages"][-1]["content"].startswith("[SYSTEM] 这是本任务允许的最后一步（已用 2/3 步）。")
        assert run.rctx.step_offset == 2


class TestCarriedTodos:
    def test_carried_todos_reach_the_hint_and_the_wrapup(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(todo=True),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(_probe(0), _probe(1), _finish("还差测试"), carried_todos=PLAN)

        hint = run.requests[2]["messages"][-1]["content"]
        assert "还没完成的待办：\n1. [进行中] 改代码\n2. [待办] 补测试" in hint
        assert action.result["wrapup"]["todos"] == PLAN
        assert action.result["wrapup"]["unfinished"] == 2

    def test_an_invalid_carried_list_is_dropped_whole(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(todo=True),
            runtime_cfg={"engine": {"max_steps": 3}},
        )
        broken = [{"content": "改代码", "status": "doing"}, {"content": "补测试", "status": "pending"}]

        action = run.run(_probe(0), _probe(1), _finish("还差测试"), carried_todos=broken)

        assert "还没完成的待办" not in run.requests[2]["messages"][-1]["content"]
        assert action.result["wrapup"]["todos"] == []

    def test_nodes_without_a_todo_list_ignore_them(self, tmp_path: Path, monkeypatch: Any) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(_probe(0), _probe(1), _finish("还差测试"), carried_todos=PLAN)

        assert action.result["wrapup"]["todos"] == []

    def test_the_task_s_own_list_wins_after_a_resume(self, tmp_path: Path) -> None:
        own = [{"content": "只剩这一项", "status": "in_progress"}]
        ls = make_loop_state(tmp_path, ScriptedRegistry(), messages=[
            {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "t1", "name": TODO_TOOL_NAME, "arguments": {"items": own}}],
                "_meta": {"source_task_id": TASK_ID},
            },
            {"role": "tool", "tool_call_id": "t1", "name": TODO_TOOL_NAME, "content": render_update_result(own)},
        ])

        ai_step._seed_carried_todos(ls, PLAN)

        assert ls.todos == own and ls.todos_restored is True

    def test_without_an_own_list_the_carried_one_is_used(self, tmp_path: Path) -> None:
        ls = make_loop_state(tmp_path, ScriptedRegistry())

        ai_step._seed_carried_todos(ls, PLAN)

        assert ls.todos == PLAN and ls.todos is not PLAN

    def test_a_carried_list_is_checked_like_update_todos(self) -> None:
        assert carried_todo_items([{"content": " 改代码 ", "status": "In_Progress"}]) == [
            {"content": "改代码", "status": "in_progress"},
        ]

    @pytest.mark.parametrize("raw", [None, [], "改代码", [{"content": "改代码", "status": "doing"}], [{"status": "pending"}]])
    def test_anything_unusable_carries_nothing(self, raw: Any) -> None:
        assert carried_todo_items(raw) == []


class TestRunnerHelpers:
    def test_carried_todos_come_from_the_chain(self) -> None:
        todos = _carried_todos({AUTO_CONTINUE_KEY: {"round": 2, "todos": [*PLAN, "not an item"]}})

        assert todos == PLAN
        assert todos[0] is not PLAN[0]

    @pytest.mark.parametrize("task_input", [
        pytest.param({}, id="no-chain"),
        pytest.param({AUTO_CONTINUE_KEY: {"round": 2}}, id="no-todos"),
        pytest.param({AUTO_CONTINUE_KEY: {"round": 2, "todos": []}}, id="empty"),
        pytest.param({AUTO_CONTINUE_KEY: {"round": 2, "todos": "改代码"}}, id="not-a-list"),
        pytest.param({AUTO_CONTINUE_KEY: "round 2"}, id="chain-not-a-dict"),
    ])
    def test_anything_else_carries_nothing(self, task_input: dict[str, Any]) -> None:
        assert _carried_todos(task_input) is None

    def test_no_handle_no_lines(self) -> None:
        assert _dispatch_handle_lines({"inbound_child_continuable": True}, "", "helper") == []

    def test_a_finished_child_only_shows_its_handle(self) -> None:
        assert _dispatch_handle_lines({}, "t1@child_a", "helper") == ["Handle: t1@child_a"]

    @pytest.mark.parametrize("wrapup,limit", [("time", "run time"), ("steps", "step")])
    def test_a_child_stopped_at_a_limit_says_how_to_continue(self, wrapup: str, limit: str) -> None:
        lines = _dispatch_handle_lines(
            {"inbound_child_continuable": True, "inbound_child_wrapup": wrapup}, "t1@child_a", "helper",
        )

        assert lines == [
            "Handle: t1@child_a",
            f"It stopped at its {limit} limit before finishing. To let it continue, dispatch to helper "
            "again with continue_from set to this handle.",
        ]

    def test_a_failed_child_kept_for_continuing_says_so(self) -> None:
        lines = _dispatch_handle_lines({"inbound_child_continuable": True}, "t1@child_a", "")

        assert lines[1] == (
            "To let it pick up where it stopped, dispatch to the same node again with continue_from set to this handle."
        )


HANDLE = "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0@child_9c2f"
KEPT_CHILD = {
    "ok": True, "session_id": "child_9c2f", "task_id": "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0",
    "kind": "child", "node_id": "scout", "context_mode": "accumulate",
}
KEPT_AGENT = {
    **KEPT_CHILD, "kind": "agent", "session_id": "s-agent", "context_mode": "fresh",
    "conversation_key": "agent:scout:qq_group:1:abc",
}


def _supervisor(*, status: int = 200, body: Any = None, error: Exception | None = None) -> SimpleNamespace:
    """顶替 rctx.http：GET 是 continuation 核对，POST 是异步委派的入站。"""
    response = SimpleNamespace(status_code=status, text="", json=lambda: body)
    get = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=response)
    accepted = SimpleNamespace(status_code=200, text="", json=lambda: {"session_id": "s-agent"})
    return SimpleNamespace(get=get, post=AsyncMock(return_value=accepted))


def _dispatching(tmp_path: Path, monkeypatch: Any, http: SimpleNamespace) -> Any:
    use_hooks(monkeypatch)
    ls = make_loop_state(
        tmp_path, ScriptedRegistry(tools={"probe": echo()}),
        rctx=make_rctx(tmp_path, task_context={"conversation_key": "qq_group:1", "channel": "qq"}),
    )
    ls.allowed_dispatch_targets = {"scout"}
    ls.rctx.http = http
    return ls


def _resolve(ls: Any, arguments: dict[str, Any], *, wait: bool) -> tuple[Any, str]:
    args = {"target": "scout", "instruction": "接着查", **arguments}
    return asyncio.run(_resolve_dispatch_request(ls, args, call("dispatch_to_scout", args, call_id="d1"), wait=wait))


class TestContinueFrom:
    def test_the_supervisor_checks_the_handle_and_the_child_keeps_its_own_mode(
        self, tmp_path: Path, monkeypatch: Any,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body=KEPT_CHILD))

        request, refusal = _resolve(
            ls, {"continue_from": f"  {HANDLE} ", "context_mode": "fork", "context_key": "topic"}, wait=True,
        )

        assert refusal == ""
        assert request.continue_from == HANDLE and request.continuation == KEPT_CHILD
        assert request.context_mode == "accumulate" and request.context_key == ""
        get = ls.rctx.http.get
        assert get.await_args.args[0] == "http://supervisor.invalid/v1/tasks/task-1/continuation"
        assert get.await_args.kwargs["params"] == {"handle": HANDLE, "target": "scout", "wait": "true"}

    @pytest.mark.parametrize("http,refusal", [
        pytest.param(_supervisor(body={"ok": False, "message": "子会话已经不在了"}), "子会话已经不在了", id="refused"),
        pytest.param(_supervisor(body={"ok": False}), "continue_from 被拒绝。", id="refused-without-a-reason"),
        pytest.param(
            _supervisor(status=500, body={"detail": "boom"}), "continue_from 核对失败：HTTP 500: boom", id="http-error",
        ),
        pytest.param(
            _supervisor(error=RuntimeError("down")), "continue_from 核对失败：RuntimeError: down", id="unreachable",
        ),
        pytest.param(_supervisor(body=["x"]), "continue_from 核对失败：supervisor 没有给出结果。", id="odd-body"),
    ])
    def test_a_handle_the_supervisor_does_not_accept_is_refused(
        self, tmp_path: Path, monkeypatch: Any, http: SimpleNamespace, refusal: str,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch, http)

        request, reason = _resolve(ls, {"continue_from": HANDLE}, wait=True)

        assert request is None and reason == refusal

    def test_a_malformed_handle_is_refused_without_asking(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body=KEPT_CHILD))

        request, reason = _resolve(ls, {"continue_from": "child_9c2f"}, wait=True)

        assert request is None
        assert reason == "continue_from 的句柄格式不对：要填委派结果里给的 task_id@session_id。"
        ls.rctx.http.get.assert_not_awaited()

    def test_without_continue_from_nothing_is_asked(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body=KEPT_CHILD))

        request, reason = _resolve(ls, {"context_key": "topic"}, wait=True)

        assert reason == "" and request.continue_from == "" and request.continuation == {}
        assert request.context_mode == "fresh" and request.context_key == "topic"
        ls.rctx.http.get.assert_not_awaited()

    def test_one_round_cannot_continue_the_same_child_twice(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body=KEPT_CHILD))
        calls = [
            call("dispatch_to_scout", {"instruction": "查 a", "wait": True, "continue_from": HANDLE}, call_id="w1"),
            call("dispatch_to_scout", {"instruction": "查 b", "wait": True, "continue_from": HANDLE}, call_id="w2"),
        ]

        plan = asyncio.run(prepare_wait_dispatches(ls, calls))

        [dispatch] = plan.items
        assert dispatch.item == {
            "kind": "node", "target": "scout", "instruction": "查 a", "context_mode": "accumulate",
            "continue_from": HANDLE, "call_id": "w1",
        }
        [(refused_call, text)] = plan.rejected
        assert refused_call.id == "w2"
        assert json.loads(text)["error"].startswith("没有派出：同一轮已经有一个委派用 continue_from 接着这个子 agent")

    def test_an_async_continuation_goes_into_the_kept_conversation(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body=KEPT_AGENT))
        args = {"target": "scout", "instruction": "接着查", "continue_from": HANDLE, "context_mode": "fork"}

        asyncio.run(_handle_pseudo_dispatch(ls, args, call("dispatch_to_scout", args, call_id="d1")))

        assert ls.rctx.http.get.await_args.kwargs["params"]["wait"] == "false"
        payload = ls.rctx.http.post.await_args.kwargs["json"]
        assert payload["conversation_key"] == "agent:scout:qq_group:1:abc"
        assert payload["dispatch_origin"]["continue_from"] == HANDLE
        assert payload["dispatch_context_mode"] == "fresh"
        assert "dispatch_fork_from_session" not in payload
        [result] = results_for(ls.messages, "d1")
        assert json.loads(result["content"])["success"] is True

    def test_an_async_continuation_without_a_conversation_is_not_sent(self, tmp_path: Path, monkeypatch: Any) -> None:
        ls = _dispatching(tmp_path, monkeypatch, _supervisor(body={**KEPT_AGENT, "conversation_key": ""}))
        args = {"target": "scout", "instruction": "接着查", "continue_from": HANDLE}

        asyncio.run(_handle_pseudo_dispatch(ls, args, call("dispatch_to_scout", args, call_id="d1")))

        ls.rctx.http.post.assert_not_awaited()
        [result] = results_for(ls.messages, "d1")
        assert json.loads(result["content"])["error"].startswith("continue_from 指向的子会话找不到会话键")


def _batch_entry(**fields: Any) -> dict[str, Any]:
    entry = {
        "kind": "node", "node_id": "scout", "status": "finish", "instruction": "查日志", "text": "查到一半",
        "summary": "", "call_id": "w1", "child_task_id": "t1", "child_session_id": "child_a",
    }
    entry.update(fields)
    return entry


class TestHandleRendering:
    def test_a_child_stopped_at_its_limit_is_offered_for_continuing(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [
            _batch_entry(handle="t1@child_a", continuable=True, wrapup="steps"),
        ]})

        assert message["content"].endswith(
            "句柄：t1@child_a\n它是到了步数上限停下的，还没做完；要它接着做，就再委派给 scout，带上 continue_from 填这个句柄。"
        )
        assert message["_meta"]["handle"] == "t1@child_a"

    def test_a_failed_child_kept_for_continuing_says_so(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [
            _batch_entry(status="fail", error="超时", text="", handle="t1@child_a", continuable=True),
        ]})

        assert message["content"].endswith(
            "句柄：t1@child_a\n要它从停下的地方接着做，就再委派给 scout，带上 continue_from 填这个句柄。"
        )

    def test_a_child_that_finished_only_shows_its_handle(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [
            _batch_entry(handle="t1@child_a", continuable=False),
        ]})

        assert message["content"].endswith("结果：\n查到一半\n句柄：t1@child_a")

    def test_entries_without_a_handle_render_as_before(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [_batch_entry()]})

        assert "句柄" not in message["content"] and "handle" not in message["_meta"]

    def test_a_single_child_result_carries_the_handle_too(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_result", "child_node_id": "scout", "result": {"text": "查到一半"},
            "handle": "t1@child_a", "continuable": True, "wrapup": "time",
        })

        assert message["content"] == "\n".join([
            "下游节点 scout 已完成。",
            "结果：",
            "查到一半",
            "句柄：t1@child_a",
            "它是到了运行时长上限停下的，还没做完；要它接着做，就再委派给 scout，带上 continue_from 填这个句柄。",
        ])

    def test_a_single_failed_child_carries_the_handle_too(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_failed", "child_node_id": "scout", "error": "超时",
            "handle": "t1@child_a", "continuable": True,
        })

        assert message["content"].startswith("下游节点 scout 执行失败：超时\n句柄：t1@child_a\n")

    def test_an_interrupted_task_is_told_to_check_before_going_on(self) -> None:
        [message] = _build_resume_messages({"type": "interrupted", "reason": "engine_crash"})

        assert message["role"] == "user"
        assert message["content"].startswith("[SYSTEM] 这个任务刚才被服务重启打断了，现在接着做。")
        assert message["_meta"] == {"message_type": "system"}
