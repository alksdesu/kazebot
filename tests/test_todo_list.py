"""节点可选的待办清单 update_todos：参数校验、结果回显和事件、挂起恢复后重建、每步一条动态提醒、收尾时带上没做完的项、系统任务不注入。"""
from __future__ import annotations

import asyncio
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

from _engine_loop_fixture import (  # noqa: E402
    TASK_ID,
    NodeRun,
    ScriptedRegistry,
    call,
    echo,
    make_loop_state,
    results_for,
    stored,
    turn,
    use_hooks,
)
from engine.builtin.todo_reminder import TODO_REMINDER_KEY, TodoReminder  # noqa: E402
from engine.hooks.types import HookContext  # noqa: E402
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.pseudo_tools import _update_todos_spec  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH  # noqa: E402
from engine.todo_list import (  # noqa: E402
    MAX_TODO_CONTENT_CHARS,
    MAX_TODO_ITEMS,
    TODO_TOOL_NAME,
    TODO_UPDATED_PREFIX,
    TodoListError,
    latest_todos_from_messages,
    parse_todo_items,
    render_update_result,
)
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.tool_names import PSEUDO_TOOL_NAMES, is_pseudo_tool_name  # noqa: E402

PLAN = [
    {"content": "读需求", "status": "completed"},
    {"content": "改代码", "status": "in_progress"},
    {"content": "补测试", "status": "pending"},
]


def _todo_call(items: Any, *, call_id: str) -> Any:
    return call(TODO_TOOL_NAME, {"items": items}, call_id=call_id)


class TestValidation:
    def test_a_good_list_is_normalised(self) -> None:
        items = parse_todo_items([{"content": "  读需求 ", "status": " Completed "}, {"content": "改代码", "status": "pending"}])

        assert items == [{"content": "读需求", "status": "completed"}, {"content": "改代码", "status": "pending"}]

    def test_an_empty_list_is_allowed(self) -> None:
        assert parse_todo_items([]) == []

    @pytest.mark.parametrize("raw", [None, "读需求", {"content": "x", "status": "pending"}, 3])
    def test_items_must_be_a_list(self, raw: Any) -> None:
        with pytest.raises(TodoListError, match="items 必须是数组"):
            parse_todo_items(raw)

    def test_the_list_length_is_capped(self) -> None:
        with pytest.raises(TodoListError, match=f"最多 {MAX_TODO_ITEMS} 项"):
            parse_todo_items([{"content": f"step {index}", "status": "pending"} for index in range(MAX_TODO_ITEMS + 1)])

    def test_the_cap_itself_is_fine(self) -> None:
        assert len(parse_todo_items([{"content": f"s{index}", "status": "pending"} for index in range(MAX_TODO_ITEMS)])) == MAX_TODO_ITEMS

    def test_every_bad_item_is_reported_at_once(self) -> None:
        with pytest.raises(TodoListError) as raised:
            parse_todo_items([
                {"content": "", "status": "pending"},
                {"content": "x" * (MAX_TODO_CONTENT_CHARS + 1), "status": "pending"},
                {"content": "ok", "status": "done"},
                "not an object",
            ])

        message = str(raised.value)
        assert "第 1 项缺少 content" in message
        assert f"第 2 项超过 {MAX_TODO_CONTENT_CHARS} 字" in message
        assert "第 3 项的 status 只能是 pending / in_progress / completed" in message
        assert "第 4 项不是对象" in message
        assert message.endswith("清单没有更新，改好后再传一次完整清单。")

    def test_the_content_limit_itself_is_fine(self) -> None:
        assert parse_todo_items([{"content": "x" * MAX_TODO_CONTENT_CHARS, "status": "pending"}])


class TestNames:
    def test_update_todos_is_a_reserved_pseudo_tool(self, tmp_path: Path) -> None:
        assert TODO_TOOL_NAME in PSEUDO_TOOL_NAMES
        assert is_pseudo_tool_name(TODO_TOOL_NAME)
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
        assert registry.reserved_name_reason(TODO_TOOL_NAME) == "taken by an engine pseudo tool"

    def test_the_spec_describes_the_full_list_contract(self) -> None:
        function = _update_todos_spec()["function"]

        assert function["name"] == TODO_TOOL_NAME
        item = function["parameters"]["properties"]["items"]["items"]
        assert item["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]
        assert item["required"] == ["content", "status"]
        assert "完整清单" in function["description"]


def _loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> Any:
    use_hooks(monkeypatch)
    ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"probe": echo()}))
    ls.todo_enabled = enabled
    return ls


class TestTool:
    def test_an_update_replaces_the_list_echoes_it_and_emits_an_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)

        action = asyncio.run(_handle_tool_calls(ls, turn(_todo_call(PLAN, call_id="t1")), 1))

        assert action is None
        assert ls.todos == PLAN
        content = results_for(ls.messages, "t1")[0]["content"]
        assert content == render_update_result(PLAN)
        assert content.splitlines() == [
            f"{TODO_UPDATED_PREFIX}（共 3 项：已完成 1，进行中 1，待办 1）",
            "1. [已完成] 读需求",
            "2. [进行中] 改代码",
            "3. [待办] 补测试",
        ]
        [event] = ls.rctx.events.of("todo_updated")
        assert event["items"] == PLAN and event["task_id"] == TASK_ID and event["node_id"] == "node-1"

    def test_an_invalid_update_keeps_the_old_list_and_emits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.todos = list(PLAN)

        asyncio.run(_handle_tool_calls(ls, turn(_todo_call([{"content": "x", "status": "maybe"}], call_id="t1")), 1))

        assert ls.todos == PLAN
        assert results_for(ls.messages, "t1")[0]["content"].startswith("Error: 第 1 项的 status")
        assert ls.rctx.events.of("todo_updated") == []

    def test_clearing_the_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.todos = list(PLAN)

        asyncio.run(_handle_tool_calls(ls, turn(_todo_call([], call_id="t1")), 1))

        assert ls.todos == []
        assert results_for(ls.messages, "t1")[0]["content"] == f"{TODO_UPDATED_PREFIX}：清单已清空。"

    def test_a_node_without_the_option_cannot_use_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch, enabled=False)

        asyncio.run(_handle_tool_calls(ls, turn(_todo_call(PLAN, call_id="t1")), 1))

        assert ls.todos == []
        assert "没有启用待办清单" in results_for(ls.messages, "t1")[0]["content"]


def _assistant(task_id: str, *calls: tuple[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "name": TODO_TOOL_NAME, "arguments": {"items": items}} for call_id, items in calls],
        "_meta": {"source_task_id": task_id},
    }


def _result(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": TODO_TOOL_NAME, "content": content}


class TestRestore:
    def test_the_latest_successful_call_of_this_task_wins(self) -> None:
        older = [{"content": "旧", "status": "pending"}]
        messages = [
            _assistant(TASK_ID, ("c1", older)),
            _result("c1", render_update_result(older)),
            _assistant(TASK_ID, ("c2", PLAN)),
            _result("c2", render_update_result(PLAN)),
        ]

        assert latest_todos_from_messages(messages, TASK_ID) == PLAN

    def test_refused_or_invalid_calls_are_skipped(self) -> None:
        messages = [
            _assistant(TASK_ID, ("c1", PLAN)),
            _result("c1", render_update_result(PLAN)),
            _assistant(TASK_ID, ("c2", [{"content": "x", "status": "pending"}])),
            _result("c2", "update_todos 没有执行：这是本任务允许的最后一步，只能调用 finish。"),
            _assistant(TASK_ID, ("c3", "not a list")),
        ]

        assert latest_todos_from_messages(messages, TASK_ID) == PLAN

    def test_calls_of_other_tasks_are_ignored(self) -> None:
        messages = [_assistant("another-task", ("c1", PLAN)), _result("c1", render_update_result(PLAN))]

        assert latest_todos_from_messages(messages, TASK_ID) is None

    def test_a_call_whose_result_is_missing_still_counts(self) -> None:
        assert latest_todos_from_messages([_assistant(TASK_ID, ("c1", PLAN))], TASK_ID) == PLAN

    def test_arguments_stored_as_json_text_are_read(self) -> None:
        message = _assistant(TASK_ID, ("c1", PLAN))
        message["tool_calls"][0]["arguments"] = '{"items": [{"content": "读需求", "status": "pending"}]}'

        assert latest_todos_from_messages([message], TASK_ID) == [{"content": "读需求", "status": "pending"}]


def _step(ls: Any, step: int = 0) -> Any:
    ctx = HookContext(
        messages=ls.messages, tools=[], node=ls.node, provider=None, rctx=ls.rctx, step=step,
        extra={"loop_state": ls, "step_count": 0},
    )
    return asyncio.run(TodoReminder().handle(ctx))


def _reminders(ls: Any) -> list[dict[str, Any]]:
    return [message for message in ls.messages if message.get(TODO_REMINDER_KEY)]


class TestReminder:
    def test_unfinished_items_get_one_dynamic_reminder_at_the_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.todos = list(PLAN)

        result = _step(ls)

        assert result is not None and result.modified is True
        [reminder] = _reminders(ls)
        assert ls.messages[-1] is reminder
        assert reminder["_dynamic"] is True and reminder["role"] == "user"
        assert "还有 2 项没完成" in reminder["content"]
        assert "2. [进行中] 改代码" in reminder["content"]

    def test_the_reminder_is_replaced_not_piled_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.todos = list(PLAN)
        _step(ls)
        ls.messages.append({"role": "assistant", "content": "working"})
        ls.todos = [dict(PLAN[0]), {"content": "改代码", "status": "completed"}, dict(PLAN[2])]

        _step(ls, step=1)

        [reminder] = _reminders(ls)
        assert ls.messages[-1] is reminder
        assert "还有 1 项没完成" in reminder["content"]

    def test_a_finished_list_drops_the_reminder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.todos = list(PLAN)
        _step(ls)
        ls.todos = [{**item, "status": "completed"} for item in PLAN]

        result = _step(ls, step=1)

        assert result is not None
        assert _reminders(ls) == []

    def test_nothing_happens_without_todos_or_without_the_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _loop(tmp_path, monkeypatch)
        assert _step(ls) is None
        disabled = _loop(tmp_path, monkeypatch, enabled=False)
        disabled.todos = list(PLAN)
        assert _step(disabled) is None
        assert _reminders(disabled) == []

    def test_the_list_is_rebuilt_once_after_a_resume(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _loop(tmp_path, monkeypatch)
        ls.messages.extend([_assistant(TASK_ID, ("c1", PLAN)), _result("c1", render_update_result(PLAN))])

        _step(ls)

        assert ls.todos == PLAN and ls.todos_restored is True
        assert len(_reminders(ls)) == 1
        ls.todos = []
        _step(ls, step=1)
        assert ls.todos == []

    def test_the_reminder_never_reaches_the_stored_history(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path,
            registry=ScriptedRegistry(tools={"probe": echo()}),
            node=Node(
                id="node-1", type="ai", prompt="Work.", tool_mode="native",
                tool_access=ToolAccess(mode="all"), extra={"todo": True},
            ),
            runtime_cfg={"engine": {"max_steps": 6}},
            handlers={"before_step": [TodoReminder()]},
        )

        run.run(
            turn(_todo_call(PLAN, call_id="t1")),
            turn(call("probe", {}, call_id="p")),
            turn(call("finish", {"text": "ok"}, call_id="f")),
        )

        assert not any(message.get(TODO_REMINDER_KEY) for message in run.requests[0]["messages"])
        assert sum(1 for message in run.requests[1]["messages"] if message.get(TODO_REMINDER_KEY)) == 1
        assert sum(1 for message in run.requests[2]["messages"] if message.get(TODO_REMINDER_KEY)) == 1
        assert not any("[待办提醒]" in str(message.content) for message in stored(tmp_path))


def _todo_node(**extra: Any) -> Node:
    return Node(
        id="node-1", type="ai", prompt="Work.", tool_mode="native",
        tool_access=ToolAccess(mode="all"), extra={"todo": True, **extra},
    )


class TestInjection:
    def test_a_node_that_opts_in_gets_the_tool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node())

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert TODO_TOOL_NAME in run.tool_names(0)

    @pytest.mark.parametrize("flag", [False, "no", None])
    def test_other_nodes_do_not(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: Any) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node(todo=flag))

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert TODO_TOOL_NAME not in run.tool_names(0)

    def test_system_tasks_do_not_get_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node(),
            task_context={"is_system_task": True},
        )

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert TODO_TOOL_NAME not in run.tool_names(0)
        assert run.registry.executed_names() == []

    def test_a_system_task_calling_it_anyway_is_told_no(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node(),
            task_context={"is_system_task": True},
            runtime_cfg={"engine": {"max_steps": 4}},
        )

        run.run(turn(_todo_call(PLAN, call_id="t1")), turn(call("finish", {"text": "ok"}, call_id="f")))

        refusal = [message for message in run.requests[1]["messages"] if message.get("tool_call_id") == "t1"]
        assert "没有启用待办清单" in refusal[0]["content"]


class TestWrapUp:
    def test_the_last_step_hint_lists_unfinished_todos(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(
            turn(_todo_call(PLAN, call_id="t1")),
            turn(call("probe", {}, call_id="p")),
            turn(call("finish", {"text": "做完一半"}, call_id="f")),
        )

        assert action.action == ACTION_FINISH
        hint = run.requests[2]["messages"][-1]["content"]
        assert hint.startswith("[SYSTEM] 这是本任务允许的最后一步")
        assert "还没完成的待办：\n1. [进行中] 改代码\n2. [待办] 补测试" in hint
        assert "读需求" not in hint

    def test_a_silent_last_step_reports_unfinished_todos(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(tools={"probe": echo()}), node=_todo_node(),
            runtime_cfg={"engine": {"max_steps": 3}},
        )

        action = run.run(
            turn(_todo_call(PLAN, call_id="t1")),
            turn(call("probe", {}, call_id="p")),
            turn(call("probe", {}, call_id="p2")),
        )

        assert action.action == ACTION_FINISH and action.result["step_limit_reached"] is True
        assert action.result["text"].startswith("任务还没有完成。已经执行过的工具：probe。")
        assert "还没完成的待办：\n1. [进行中] 改代码\n2. [待办] 补测试" in action.result["text"]
