"""工具结果消息里的 tool_status：序列化和旧数据兼容，以及同步、拦下、异步启动、转异步、准备失败、异步回传每条路径都填上。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
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
    call,
    cancelled,
    echo,
    failed,
    make_loop_state,
    ok,
    real_calls,
    results_for,
    stored,
    turn,
    use_hooks,
)
from engine import runner  # noqa: E402
from engine.builtin.result import hook_result  # noqa: E402
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.inference import ai_step  # noqa: E402
from engine.inference.ai_step import _execute_real_tools, _handle_tool_calls  # noqa: E402
from engine.inference.async_delivery import async_result_tool_status  # noqa: E402
from engine.inference.message_model import MessageMeta, get_message_meta  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.protocol import TaskAction  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_hooks(monkeypatch: Any) -> None:
    # 别的测试跑过 run_ai_node 的话，全局 hook 表里会挂着内置 handler
    use_hooks(monkeypatch)


class TestSerialization:
    def test_an_empty_status_is_not_written(self) -> None:
        assert "tool_status" not in MessageMeta(message_type="tool_result").to_dict()

    def test_a_status_is_written_and_read_back(self) -> None:
        data = MessageMeta(message_type="tool_result", tool_status="error").to_dict()

        assert data["tool_status"] == "error"
        assert MessageMeta.from_dict(data).tool_status == "error"

    @pytest.mark.parametrize("raw", [{}, {"message_type": "tool_result"}, {"tool_status": None}])
    def test_old_records_read_as_an_empty_status(self, raw: dict[str, Any]) -> None:
        assert MessageMeta.from_dict(raw).tool_status == ""
        assert get_message_meta({"role": "tool", "_meta": raw}).tool_status == ""


def _status_of(ls: Any, call_id: str) -> str:
    return results_for(ls.messages, call_id)[0]["_meta"].get("tool_status", "")


class TestExecutionPaths:
    @pytest.mark.parametrize("result, status", [(ok("fine"), "success"), (failed("broke"), "error"), (cancelled(), "cancelled")])
    def test_synchronous_results(self, tmp_path: Path, result: dict[str, Any], status: str) -> None:
        async def tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return result

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"probe": tool}))

        asyncio.run(_execute_real_tools(ls, real_calls(call("probe", {}, call_id="p")), step=1))

        assert _status_of(ls, "p") == status
        stored_result = [message for message in stored(tmp_path) if message.tool_call_id == "p"]
        assert stored_result[0].meta["tool_status"] == status

    def test_a_call_the_hook_blocks(self, tmp_path: Path, monkeypatch: Any) -> None:
        class Deny:
            name = "deny"
            priority = 50

            async def handle(self, ctx: Any) -> Any:
                return hook_result(skip_step=True, reason="需要审批") if ctx.tool_call is not None else None

        use_hooks(monkeypatch, {"before_tool_call": [Deny()]})
        registry = ScriptedRegistry(tools={"probe": echo()})
        ls = make_loop_state(tmp_path, registry)

        asyncio.run(_execute_real_tools(ls, real_calls(call("probe", {}, call_id="p")), step=1))

        assert _status_of(ls, "p") == "blocked"
        assert registry.executed == []

    def test_an_async_tool_that_started(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawned: list[Any] = []
        monkeypatch.setattr(ai_step, "_spawn_background", lambda coroutine, *, name: spawned.append(coroutine))
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"draw": echo()}, specs={"draw": {"async_mode": True}}))
        try:
            asyncio.run(_execute_real_tools(ls, real_calls(call("draw", {"prompt": "cat"}, call_id="d")), step=1))
        finally:
            for coroutine in spawned:
                coroutine.close()

        assert _status_of(ls, "d") == "async_started"
        end = ls.rctx.events.of("tool_call_end")
        assert [payload["status"] for payload in end] == ["async_started"]

    def test_an_async_tool_whose_preparation_failed(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawned: list[Any] = []
        monkeypatch.setattr(ai_step, "_spawn_background", lambda coroutine, *, name: spawned.append(coroutine))

        class PreparingRegistry(ScriptedRegistry):
            async def prepare(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
                raise RuntimeError("quota exhausted")

        ls = make_loop_state(tmp_path, PreparingRegistry(tools={"draw": echo()}, specs={"draw": {"async_mode": True}}))

        asyncio.run(_execute_real_tools(ls, real_calls(call("draw", {"prompt": "cat"}, call_id="d")), step=1))

        assert _status_of(ls, "d") == "error"
        assert results_for(ls.messages, "d")[0]["content"] == "工具准备失败，尚未执行：quota exhausted"
        assert ls.failed_real_tools == {"draw"}
        assert spawned == []

    def test_a_command_moved_to_the_background(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawned: list[Any] = []
        monkeypatch.setattr(ai_step, "_spawn_background", lambda coroutine, *, name: spawned.append(coroutine))

        async def slow_command(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            await asyncio.sleep(5)
            return ok("finished")

        runtime_cfg = {"meta": {"execute_command": {
            "async_upgrade": {"enabled": True, "threshold_sec": 0.05},
            "default_timeout_sec": 90,
        }}}
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"execute_command": slow_command}), runtime_cfg=runtime_cfg)
        try:
            asyncio.run(_execute_real_tools(
                ls, real_calls(call("execute_command", {"command": "make", "timeout_sec": 60}, call_id="e")), step=1,
            ))
        finally:
            for coroutine in spawned:
                coroutine.close()

        assert _status_of(ls, "e") == "async_started"
        assert results_for(ls.messages, "e")[0]["content"].startswith('⏳ Tool "execute_command" exceeded')
        assert len(spawned) == 1

    def test_calls_that_never_ran(self, tmp_path: Path, monkeypatch: Any) -> None:
        use_hooks(monkeypatch)
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"probe": echo()}), allowed={"probe"})
        response = turn(
            call("probe", {"_raw": '{"path": "a'}, call_id="bad"),
            call("other_tool", {}, call_id="other"),
            call("probe", {}, call_id="good"),
        )

        asyncio.run(_handle_tool_calls(ls, response, 1))

        assert _status_of(ls, "bad") == "error"
        assert _status_of(ls, "other") == "blocked"
        assert _status_of(ls, "good") == "success"

    def test_a_round_the_hook_blocks_marks_real_tools_only(self, tmp_path: Path, monkeypatch: Any) -> None:
        class BlockRound:
            name = "block_round"
            priority = 50

            async def handle(self, ctx: Any) -> Any:
                return hook_result(block=True, error_message="本轮不执行") if ctx.tool_call is None else None

        use_hooks(monkeypatch, {"before_tool_call": [BlockRound()]})
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"probe": echo()}))

        asyncio.run(_handle_tool_calls(ls, turn(call("probe", {}, call_id="p"), call("reply", {"text": "hi"}, call_id="r")), 1))

        assert _status_of(ls, "p") == "blocked"
        assert _status_of(ls, "r") == ""


class TestAsyncResultInbound:
    @pytest.mark.parametrize("hints, status", [
        ({"async_tool_success": True}, "success"),
        ({"async_tool_success": False, "async_tool_error": "timeout"}, "error"),
        ({"async_tool_success": "yes"}, ""),
        ({}, ""),
    ])
    def test_the_status_comes_from_the_route_hints(self, hints: dict[str, Any], status: str) -> None:
        assert async_result_tool_status({"route_hints": hints}) == status

    def test_a_context_without_hints_has_no_status(self) -> None:
        assert async_result_tool_status({}) == ""
        assert async_result_tool_status(None) == ""

    def test_the_inbound_message_in_the_loop_carries_the_status(self, tmp_path: Path, monkeypatch: Any) -> None:
        instruction = '❌ Async tool "draw" (id: a1) failed: timeout'
        run = NodeRun(
            monkeypatch, tmp_path,
            registry=ScriptedRegistry(tools={"probe": echo()}),
            node=Node(id="node-1", type="ai", prompt="Work.", tool_mode="native"),
            runtime_cfg={"engine": {"max_steps": 4}},
            task_context={"route_hints": {"async_tool_success": False, "async_tool_name": "draw"}},
        )

        run.run(turn(call("finish", {"text": "生图失败了"}, call_id="f")), instruction=instruction)

        inbound = [message for message in run.requests[0]["messages"] if message.get("content") == instruction]
        assert inbound and inbound[-1]["_meta"]["tool_status"] == "error"

    @pytest.mark.parametrize("hints, expected", [
        ({"async_tool_success": True}, "success"),
        ({"async_tool_success": False}, "error"),
        (None, None),
    ])
    def test_the_runner_stores_the_status_on_the_inbound_message(
        self, tmp_path: Path, monkeypatch: Any, hints: dict[str, Any] | None, expected: str | None,
    ) -> None:
        monkeypatch.setattr(runner, "load_node", lambda *args: Node(id="test", type="ai", prompt="test"))
        monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
        monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *args, **kwargs: SimpleNamespace())
        monkeypatch.setattr(runner, "install_event_bridge", lambda *args, **kwargs: None)
        monkeypatch.setattr(runner, "run_ai_node", AsyncMock(
            return_value=TaskAction(action="finish", node_id="test", result={"text": "done"}),
        ))
        task_context = {} if hints is None else {"route_hints": hints}
        task = {
            "task_id": "task", "node_id": "test",
            "input": {"instruction": '✅ Async tool "draw" (id: a1) completed in 3.0s.', "task_context": task_context},
        }

        async def run_task() -> None:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
            async with httpx.AsyncClient(transport=transport) as http:
                await runner._run_node_task(
                    http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
                    registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
                    task=task, worker_id="worker", session_id="session", session_generation=1,
                    api_key="test", base_url="http://model.invalid", default_model="test",
                )

        asyncio.run(run_task())

        inbound = ConversationStore(tmp_path / "data" / "conversations").load("session")[-1]
        assert inbound.role == "user"
        assert inbound.meta.get("tool_status") == expected
