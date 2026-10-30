"""工具进度行的拼法和拆法：引擎按 tool_step 拼，QQ 适配器按同一处拆出工具名，摘要里的路径、关键词、命令不算。"""
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

from _engine_loop_fixture import ScriptedRegistry, call, echo, make_loop_state, real_calls, use_hooks  # noqa: E402
from engine.inference.ai_step import _execute_real_tools  # noqa: E402
from engine.tool_step import (  # noqa: E402
    progress_tool_names,
    tool_batch_progress,
    tool_result_progress,
    tool_start_progress,
)


class TestRoundTrip:
    def test_batch_names_every_tool_it_shows(self) -> None:
        line = tool_batch_progress("qq.orchestrator", ["read_file", "web_search"], 2)

        assert line == "[qq.orchestrator] 执行 2 个工具：read_file、web_search"
        assert progress_tool_names(line) == ("read_file", "web_search")

    def test_a_long_batch_shows_the_first_five(self) -> None:
        names = [f"tool_{index}" for index in range(7)]
        line = tool_batch_progress("node-1", names, 7)

        assert line.endswith(" 等 7 个工具")
        assert progress_tool_names(line) == tuple(names[:5])

    def test_a_batch_without_names_names_no_tool(self) -> None:
        line = tool_batch_progress("node-1", ["", ""], 2)

        assert line == "[node-1] 执行 2 个工具"
        assert progress_tool_names(line) == ()

    def test_a_start_line_names_its_tool(self) -> None:
        assert progress_tool_names(tool_start_progress("tool", "web_search")) == ("web_search",)

    @pytest.mark.parametrize(("tool_name", "summary"), [
        ("read_file", "已读取 tools/web_search.py"),
        ("search_in_files", '搜索 "WEB_SEARCH_PROVIDER" in tools (3 结果)'),
        ("execute_command", "命令完成 (rc=0): grep -n x_search tools/exa_search.py"),
        ("dispatch:qq.search", "委派 qq.search: 查一下 web_search: 怎么配"),
        ("web_search", ""),
    ])
    def test_a_result_line_names_only_its_tool(self, tool_name: str, summary: str) -> None:
        line = tool_result_progress("qq.orchestrator", tool_name, summary)

        assert progress_tool_names(line) == (tool_name,)

    @pytest.mark.parametrize("line", [
        "",
        "▶ 节点启动: qq.orchestrator",
        "⏳ 等待审批: write_file",
        "✅ 自动放行: web_search",
        "⚠️ 上下文压缩失败",
        "[qq.orchestrator] 等待 2 个子节点：qq.search、draw.novelai_planner",
        "[qq.orchestrator] 等待 1 个异步委派",
    ])
    def test_lines_that_are_not_tool_progress_name_no_tool(self, line: str) -> None:
        assert progress_tool_names(line) == ()


class TestEngineLines:
    """引擎这一轮真发出去的进度行，拆出来正好是这一轮调的工具。"""

    @staticmethod
    def _progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: dict[str, Any], *calls: Any) -> list[str]:
        use_hooks(monkeypatch)
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools=tools))
        asyncio.run(_execute_real_tools(ls, real_calls(*calls), step=1))
        return [payload["message"] for payload in ls.rctx.events.of("handoff_progress")]

    def test_reading_the_search_tool_source_names_no_search_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def read_source(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"totalCount": 1, "successCount": 1, "results": [{"path": args["path"]}]}}

        async def grep_source(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"count": 3}}

        lines = self._progress(
            tmp_path, monkeypatch, {"read_file": read_source, "search_in_files": grep_source},
            call("read_file", {"path": "tools/web_search.py"}, call_id="r"),
            call("search_in_files", {"query": "WEB_SEARCH_PROVIDER", "path": "tools"}, call_id="s"),
        )

        assert "tools/web_search.py" in lines[1] and "WEB_SEARCH_PROVIDER" in lines[2]
        assert [progress_tool_names(line) for line in lines] == [
            ("read_file", "search_in_files"), ("read_file",), ("search_in_files",),
        ]

    def test_a_search_call_is_named_in_each_line(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lines = self._progress(
            tmp_path, monkeypatch, {"web_search": echo("金价 3000")},
            call("web_search", {"query": "今日金价"}, call_id="w"),
        )

        assert [progress_tool_names(line) for line in lines] == [("web_search",), ("web_search",)]
