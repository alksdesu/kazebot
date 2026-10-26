"""plan、批量派发替节点跑的独立工具任务按发起节点的设置执行：节点开关和记忆去向跟节点自己调用时一样。"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import runner  # noqa: E402
from toolbox.hashline import hashline_enabled  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

# 包的 __init__ 把 search_in_files 这个名字换成了函数，模块得从 sys.modules 里拿
search_module = importlib.import_module("toolbox.builtins.search_in_files")


class _Response:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"ok": True, "cancelled": False}


class _Http:
    async def post(self, url: str, **_kwargs: Any) -> _Response:
        return _Response()

    async def get(self, url: str, **_kwargs: Any) -> _Response:
        return _Response()


class _CapturingRegistry:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.contexts.append(ctx)
        return {"ok": True, "data": {"result": "done"}}

    def get_spec(self, name: str) -> None:
        return None


def _node(root: Path, node_id: str, **fields: Any) -> None:
    nodes = root / "config" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    data = {"id": node_id, "type": "ai", "prompt": "test", "tool_access": {"mode": "all"}, **fields}
    (nodes / f"{node_id}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _run(
    root: Path, registry: Any, tool_name: str, arguments: dict[str, Any], task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = {"task_id": "task-1", "tool_name": tool_name, "input": {
        "origin_node_id": "origin", "arguments": arguments, "task_context": task_context or {},
    }}
    return asyncio.run(runner._run_tool_task(
        http=_Http(), sup_url="http://sup", ws_root=root, registry=registry, task=task,  # type: ignore[arg-type]
        worker_id="worker-1", session_id="session", session_generation=1, task_id="task-1",
    ))


class TestTheToolContext:
    def test_it_carries_the_origin_node_settings_and_memory_scope(self, tmp_path: Path) -> None:
        _node(tmp_path, "origin", search_replace=False, edit_mode="hashline", memory_book="notes")
        registry = _CapturingRegistry()

        result = _run(tmp_path, registry, "demo", {}, {
            "conversation_key": "qq_group:plan",
            "route_conversation_key": "qq_group:route",
            "parent_conversation_key": "qq_group:parent",
            "memory_hints": {"subjects": ["alice"]},
        })

        assert result["action"] == "finish"
        [ctx] = registry.contexts
        assert ctx.node_id == "demo"
        assert ctx._node_id == "origin"
        assert ctx._node_extra["search_replace"] is False
        assert ctx._node_extra["memory_book"] == "notes"
        assert hashline_enabled(ctx)
        assert ctx._memory_subjects == ["alice"]
        assert (ctx._route_conversation_key, ctx._parent_conversation_key) == ("qq_group:route", "qq_group:parent")

    def test_a_node_without_settings_keeps_the_defaults(self, tmp_path: Path) -> None:
        _node(tmp_path, "origin")
        registry = _CapturingRegistry()

        _run(tmp_path, registry, "demo", {})

        [ctx] = registry.contexts
        assert "search_replace" not in ctx._node_extra
        assert not hashline_enabled(ctx)
        assert ctx._memory_subjects == []
        assert (ctx._route_conversation_key, ctx._parent_conversation_key) == ("", "")

    def test_a_refused_origin_node_never_reaches_the_tool(self, tmp_path: Path) -> None:
        _node(tmp_path, "origin", tool_access={"mode": "allowlist", "allow": ["read_file"]})
        registry = _CapturingRegistry()

        result = _run(tmp_path, registry, "demo", {})

        assert result["action"] == "error"
        assert registry.contexts == []


class TestSearchReplaceSwitch:
    def test_a_plan_step_cannot_replace_for_a_node_that_forbids_it(self, tmp_path: Path) -> None:
        _node(tmp_path, "origin", search_replace=False)
        (tmp_path / "notes.txt").write_text("old text\n", encoding="utf-8")
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")

        result = _run(tmp_path, registry, "search_in_files", {"query": "old", "mode": "replace", "replace": "new"})

        assert result["result"]["tool_ok"] is False
        assert search_module.REPLACE_DISABLED_MESSAGE in result["result"]["text"]
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "old text\n"
