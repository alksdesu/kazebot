"""同一轮里连着的只读工具并发执行：分组规则、并发上限、按原顺序记结果和事件、审批快照、取消。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
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
    SESSION_ID,
    ScriptedRegistry,
    call,
    cancelled,
    echo,
    failed,
    make_loop_state,
    ok,
    real_calls,
    use_hooks,
)
from engine.builtin.result import hook_result  # noqa: E402
from engine.inference import ai_step  # noqa: E402
from engine.inference.ai_step import PARALLEL_SAFE_TOOLS, _execute_real_tools, _execution_groups  # noqa: E402
from engine.protocol import ACTION_FAIL, TaskAction  # noqa: E402
from engine.tool_outputs import READ_TOOL_OUTPUT  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_hooks(monkeypatch: Any) -> None:
    # 别的测试跑过 run_ai_node 的话，全局 hook 表里会挂着内置 handler
    use_hooks(monkeypatch)


def _read(path: str, call_id: str) -> Any:
    return call("read_file", {"files": [{"path": path}]}, call_id=call_id)


def _tool_messages(ls: Any) -> list[dict[str, Any]]:
    return [message for message in ls.messages if message.get("role") == "tool"]


def _barrier_reader(started: list[str], expected: int, *, delays: dict[str, float] | None = None) -> Any:
    """等齐 expected 个调用都开跑了才返回；顺序执行的话第一个等不到，交回失败。"""

    async def tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        path = args["files"][0]["path"]
        started.append(path)
        for _ in range(300):
            if len(started) >= expected:
                break
            await asyncio.sleep(0.01)
        else:
            return failed(f"{path} ran alone")
        await asyncio.sleep((delays or {}).get(path, 0))
        return ok(f"content of {path}")

    return tool


class TestGrouping:
    def test_runs_of_read_only_calls_form_groups_and_everything_else_runs_alone(self, tmp_path: Path) -> None:
        registry = ScriptedRegistry(
            tools={name: echo() for name in ("read_file", "list_dir", "write_file", "search_in_files", "execute_command")},
            specs={
                "slow_reader": {"parallel_safe": True, "async_mode": True},
                "lookup": {"parallel_safe": True},
                "lookup_text_flag": {"parallel_safe": "yes"},
            },
        )
        ls = make_loop_state(tmp_path, registry)
        calls = real_calls(
            _read("a", "r1"),
            call("list_dir", {"paths": ["."]}, call_id="l1"),
            call("write_file", {"path": "x", "content": ""}, call_id="w1"),
            _read("b", "r2"),
            call("search_in_files", {"query": "a", "mode": "replace", "replace": "b"}, call_id="s1"),
            call("search_in_files", {"query": "a"}, call_id="s2"),
            _read("c", "r3"),
            call("slow_reader", {}, call_id="a1"),
            call("lookup", {}, call_id="p1"),
            call("lookup_text_flag", {}, call_id="p2"),
            call("execute_command", {"command": "ls"}, call_id="e1"),
        )

        groups = _execution_groups(ls, calls, 8)

        assert [(parallel, [item["id"] for item in group]) for parallel, group in groups] == [
            (True, ["r1", "l1"]),
            (False, ["w1"]),
            (False, ["r2"]),
            (False, ["s1"]),
            (True, ["s2", "r3"]),
            (False, ["a1"]),
            (True, ["p1", "p2"]),
            (False, ["e1"]),
        ]

    def test_a_concurrency_of_one_keeps_every_call_on_its_own(self, tmp_path: Path) -> None:
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": echo()}))
        calls = real_calls(*(_read(str(index), f"r{index}") for index in range(3)))

        assert _execution_groups(ls, calls, 1) == [(False, [item]) for item in calls]

    def test_the_builtin_read_only_set(self) -> None:
        assert PARALLEL_SAFE_TOOLS == {
            "read_file", "list_dir", "search_in_files", "glob", "get_context_window",
            "search_context_archive", "read_context_archive", READ_TOOL_OUTPUT,
        }


class TestConcurrency:
    def test_a_read_only_group_really_runs_at_the_same_time(self, tmp_path: Path) -> None:
        started: list[str] = []
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": _barrier_reader(started, 2)}))

        asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r1"), _read("b", "r2")), step=1))

        assert [message["content"] for message in _tool_messages(ls)] == ["content of a", "content of b"]

    @pytest.mark.parametrize("configured, peak", [(None, 5), (2, 2), (1, 1)])
    def test_the_concurrency_cap_limits_how_many_run_at_once(
        self, tmp_path: Path, configured: int | None, peak: int,
    ) -> None:
        state = {"running": 0, "peak": 0}

        async def tracked(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.05)
            state["running"] -= 1
            return ok(args["files"][0]["path"])

        runtime_cfg = {} if configured is None else {"engine": {"parallel_tools": {"max_concurrency": configured}}}
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": tracked}), runtime_cfg=runtime_cfg)
        calls = [_read(str(index), f"r{index}") for index in range(5)]

        asyncio.run(_execute_real_tools(ls, real_calls(*calls), step=1))

        assert state["peak"] == peak
        assert [message["content"] for message in _tool_messages(ls)] == ["0", "1", "2", "3", "4"]


class TestOrdering:
    def test_results_events_and_after_hooks_keep_the_model_order(self, tmp_path: Path, monkeypatch: Any) -> None:
        started: list[str] = []
        seen_after: list[str] = []

        class AfterRecorder:
            name = "after_recorder"
            priority = 0

            async def handle(self, ctx: Any) -> None:
                seen_after.append(ctx.tool_call.id)

        use_hooks(monkeypatch, {"after_tool_call": [AfterRecorder()]})
        # 先发起的最后跑完
        reader = _barrier_reader(started, 3, delays={"a": 0.15, "b": 0.05, "c": 0.0})
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": reader}))

        asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r-a"), _read("b", "r-b"), _read("c", "r-c")), step=1))

        messages = _tool_messages(ls)
        assert [message["tool_call_id"] for message in messages] == ["r-a", "r-b", "r-c"]
        assert [message["_meta"]["tool_status"] for message in messages] == ["success"] * 3
        lifecycle = [(kind, payload["tool_call_id"]) for kind, payload in ls.rctx.events.events
                     if kind in {"tool_call_start", "tool_call_end"}]
        assert lifecycle == [
            ("tool_call_start", "r-a"), ("tool_call_start", "r-b"), ("tool_call_start", "r-c"),
            ("tool_call_end", "r-a"), ("tool_call_end", "r-b"), ("tool_call_end", "r-c"),
        ]
        assert seen_after == ["r-a", "r-b", "r-c"]

    def test_success_and_failure_are_recorded_per_tool(self, tmp_path: Path) -> None:
        async def broken(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return failed("permission denied")

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": echo("text"), "list_dir": broken}))

        asyncio.run(_execute_real_tools(
            ls, real_calls(_read("a", "r1"), call("list_dir", {"paths": ["."]}, call_id="l1")), step=1,
        ))

        assert ls.succeeded_real_tools == {"read_file"}
        assert ls.failed_real_tools == {"list_dir"}
        assert [message["_meta"]["tool_status"] for message in _tool_messages(ls)] == ["success", "error"]

    def test_every_approval_hook_runs_in_order_before_any_call_of_the_group(
        self, tmp_path: Path, monkeypatch: Any,
    ) -> None:
        log: list[str] = []

        class HookRecorder:
            name = "hook_recorder"
            priority = 50

            async def handle(self, ctx: Any) -> None:
                if ctx.tool_call is not None:
                    log.append(f"hook:{ctx.tool_call.id}")

        async def reader(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            log.append(f"run:{ctx.tool_call_id}")
            return ok("x")

        use_hooks(monkeypatch, {"before_tool_call": [HookRecorder()]})
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": reader}))

        asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r-a"), _read("b", "r-b"), _read("c", "r-c")), step=1))

        assert log[:3] == ["hook:r-a", "hook:r-b", "hook:r-c"]
        assert sorted(log[3:]) == ["run:r-a", "run:r-b", "run:r-c"]

    def test_a_hook_action_inside_a_group_stops_before_anything_runs(self, tmp_path: Path, monkeypatch: Any) -> None:
        stop = TaskAction(action=ACTION_FAIL, node_id="node-1", error="stopped by policy")

        class StopOnB:
            name = "stop_on_b"
            priority = 50

            async def handle(self, ctx: Any) -> Any:
                if ctx.tool_call is not None and ctx.tool_call.id == "r-b":
                    return hook_result(action=stop)
                return None

        use_hooks(monkeypatch, {"before_tool_call": [StopOnB()]})
        registry = ScriptedRegistry(tools={"read_file": echo()})
        ls = make_loop_state(tmp_path, registry)

        action = asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r-a"), _read("b", "r-b")), step=1))

        assert action is stop
        assert registry.executed == []
        assert _tool_messages(ls) == []


class TestApprovalSnapshots:
    def test_each_parallel_call_gets_its_own_tool_context(self, tmp_path: Path) -> None:
        seen: dict[str, tuple[str, int, str]] = {}

        async def reader(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            path = args["files"][0]["path"]
            # 晚读的那个最容易读到被别人改掉的 tool_call_id
            await asyncio.sleep(0.05 if path == "a" else 0)
            seen[path] = (ctx.tool_call_id, id(ctx), getattr(ctx, "_context_archive_session_id", ""))
            return ok(path)

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"read_file": reader}))

        asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r-a"), _read("b", "r-b")), step=1))

        assert seen["a"][0] == "r-a" and seen["b"][0] == "r-b"
        assert seen["a"][1] != seen["b"][1]
        assert seen["a"][2] == seen["b"][2] == SESSION_ID

    def test_a_call_the_approval_hook_blocks_keeps_its_place(self, tmp_path: Path, monkeypatch: Any) -> None:
        class DenyB:
            name = "deny_b"
            priority = 50

            async def handle(self, ctx: Any) -> Any:
                if ctx.tool_call is not None and ctx.tool_call.id == "r-b":
                    return hook_result(block=True, error_message="审批被拒绝")
                return None

        use_hooks(monkeypatch, {"before_tool_call": [DenyB()]})
        registry = ScriptedRegistry(tools={"read_file": echo("read")})
        ls = make_loop_state(tmp_path, registry)

        asyncio.run(_execute_real_tools(ls, real_calls(_read("a", "r-a"), _read("b", "r-b"), _read("c", "r-c")), step=1))

        assert [
            (message["tool_call_id"], message["content"], message["_meta"]["tool_status"])
            for message in _tool_messages(ls)
        ] == [("r-a", "read", "success"), ("r-b", "审批被拒绝", "blocked"), ("r-c", "read", "success")]
        assert len(registry.executed) == 2
        assert [payload["tool_call_id"] for payload in ls.rctx.events.of("tool_call_start")] == ["r-a", "r-c"]


class TestCancellation:
    def test_cancellation_is_checked_before_each_group(self, tmp_path: Path) -> None:
        registry = ScriptedRegistry(tools={"read_file": echo("read"), "write_file": echo("written")})
        ls = make_loop_state(tmp_path, registry)
        ls.rctx.check_cancelled = AsyncMock(side_effect=[False, True])  # type: ignore[method-assign]

        asyncio.run(_execute_real_tools(ls, real_calls(
            _read("a", "r1"), _read("b", "r2"), call("write_file", {"path": "x", "content": ""}, call_id="w1"),
        ), step=1))

        assert registry.executed_names() == ["read_file", "read_file"]
        assert [message["tool_call_id"] for message in _tool_messages(ls)] == ["r1", "r2"]

    def test_a_cancelled_result_inside_a_group_is_kept_and_later_groups_are_skipped(self, tmp_path: Path) -> None:
        async def reader(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            path = args["files"][0]["path"]
            return cancelled() if path == "b" else ok(path)

        registry = ScriptedRegistry(tools={"read_file": reader, "write_file": echo("written")})
        ls = make_loop_state(tmp_path, registry)

        asyncio.run(_execute_real_tools(ls, real_calls(
            _read("a", "r-a"), _read("b", "r-b"), _read("c", "r-c"),
            call("write_file", {"path": "x", "content": ""}, call_id="w1"),
        ), step=1))

        assert [(message["tool_call_id"], message["_meta"]["tool_status"]) for message in _tool_messages(ls)] == [
            ("r-a", "success"), ("r-b", "cancelled"), ("r-c", "success"),
        ]
        assert "write_file" not in registry.executed_names()


class TestAsyncToolsStayOutOfGroups:
    def test_an_async_tool_between_readers_runs_on_its_own(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawned: list[Any] = []
        monkeypatch.setattr(ai_step, "_spawn_background", lambda coroutine, *, name: spawned.append(coroutine))
        registry = ScriptedRegistry(
            tools={"read_file": echo("read"), "draw": echo("drawn")},
            specs={"draw": {"async_mode": True, "parallel_safe": True}},
        )
        ls = make_loop_state(tmp_path, registry)
        try:
            asyncio.run(_execute_real_tools(ls, real_calls(
                _read("a", "r1"), call("draw", {"prompt": "cat"}, call_id="d1"), _read("b", "r2"),
            ), step=1))
        finally:
            for coroutine in spawned:
                coroutine.close()

        messages = _tool_messages(ls)
        assert [(message["tool_call_id"], message["_meta"]["tool_status"]) for message in messages] == [
            ("r1", "success"), ("d1", "async_started"), ("r2", "success"),
        ]
        assert messages[1]["content"].startswith('⏳ Async tool "draw" started')
        assert len(spawned) == 1
        assert registry.executed_names() == ["read_file", "read_file"]
        assert "draw" in ls.succeeded_real_tools
