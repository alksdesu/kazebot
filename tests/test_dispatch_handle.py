"""委派句柄 task_id@session_id：拼出、解析、拒绝缺段和越界字符。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.dispatch_handle import DispatchHandle, format_handle, parse_handle  # noqa: E402

_TASK = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


class TestFormat:
    @pytest.mark.parametrize("session_id", ["child_9c2f", "branch_12", "6b0d4c0e-2f7a-4f69-9f5e-0d6c3f1a2b3c", "agent.s:1"])
    def test_a_task_id_and_a_session_id_make_a_handle(self, session_id: str) -> None:
        assert format_handle(_TASK, session_id) == f"{_TASK}@{session_id}"

    def test_surrounding_spaces_are_dropped(self) -> None:
        assert format_handle(f"  {_TASK} ", " child_a ") == f"{_TASK}@child_a"

    @pytest.mark.parametrize("task_id,session_id", [
        pytest.param("", "child_a", id="no-task"),
        pytest.param(_TASK, "", id="no-session"),
        pytest.param(None, None, id="none"),
        pytest.param("task id", "child_a", id="space-in-task"),
        pytest.param("task@x", "child_a", id="separator-in-task"),
        pytest.param(_TASK, "child@a", id="separator-in-session"),
        pytest.param(_TASK, "child/../a", id="slash-in-session"),
        pytest.param("-task", "child_a", id="task-starts-with-a-dash"),
        pytest.param(_TASK, ":child", id="session-starts-with-a-colon"),
        pytest.param("t" * 65, "child_a", id="task-too-long"),
        pytest.param(_TASK, "s" * 201, id="session-too-long"),
    ])
    def test_a_missing_or_odd_part_gives_nothing(self, task_id: Any, session_id: Any) -> None:
        assert format_handle(task_id, session_id) == ""

    def test_the_length_limits_themselves_are_fine(self) -> None:
        assert format_handle("t" * 64, "s" * 200) == f"{'t' * 64}@{'s' * 200}"


class TestParse:
    def test_a_formatted_handle_parses_back(self) -> None:
        handle = parse_handle(format_handle(_TASK, "child_a"))

        assert handle == DispatchHandle(task_id=_TASK, session_id="child_a")
        assert str(handle) == f"{_TASK}@child_a"

    def test_spaces_around_the_handle_and_its_parts_are_ignored(self) -> None:
        assert parse_handle(f"  {_TASK} @ child_a  ") == DispatchHandle(task_id=_TASK, session_id="child_a")

    @pytest.mark.parametrize("raw", [
        pytest.param(None, id="none"),
        pytest.param(42, id="number"),
        pytest.param(["t@s"], id="list"),
        pytest.param("", id="empty"),
        pytest.param(_TASK, id="no-separator"),
        pytest.param("@child_a", id="no-task"),
        pytest.param(f"{_TASK}@", id="no-session"),
        pytest.param(f"{_TASK}@child_a@more", id="two-separators"),
        pytest.param(f"{_TASK}@child a", id="space-in-session"),
    ])
    def test_anything_else_is_not_a_handle(self, raw: Any) -> None:
        assert parse_handle(raw) is None
