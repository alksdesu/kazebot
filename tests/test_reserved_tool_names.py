"""外部工具不能顶掉内置工具和引擎伪工具的名字。

重名不会报错，只会让模型在同一份工具列表里看到两个同名条目，然后调到哪个全看字典顺序。
判定集中在 ToolRegistry.reserved_name_reason：内置以注册表快照为准（插件启动时注册的也算），
伪工具名单在 toolbox/tool_names.py，两条路都得有人盯着。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.tool_names import (  # noqa: E402
    DISPATCH_TOOL_PREFIX,
    PSEUDO_TOOL_NAMES,
    TOOL_NAME_RE,
    is_engine_owned_name,
    is_pseudo_tool_name,
)

_BUILTIN_REASON = "taken by a builtin tool"
_PSEUDO_REASON = "taken by an engine pseudo tool"


@pytest.fixture()
def registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")


def test_the_pseudo_tool_roster_is_the_one_the_engine_injects() -> None:
    assert PSEUDO_TOOL_NAMES == frozenset(
        {"finish", "ask", "reply", "switch_node", "compact_context", "preempt_task"}
    )
    assert DISPATCH_TOOL_PREFIX == "dispatch_to_"


@pytest.mark.parametrize("name", sorted(PSEUDO_TOOL_NAMES))
def test_every_pseudo_name_is_recognised(name: str) -> None:
    assert is_pseudo_tool_name(name) is True
    assert is_engine_owned_name(name) is True


def test_a_dispatch_name_needs_a_target_to_be_routable() -> None:
    assert is_pseudo_tool_name("dispatch_to_scout") is True
    assert is_pseudo_tool_name("dispatch_to_") is False
    assert is_pseudo_tool_name("dispatch_to_   ") is False
    # 路由不了，但外部工具照样不能叫这个名字
    assert is_engine_owned_name("dispatch_to_") is True


@pytest.mark.parametrize("name", ["", "   ", "finisher", "my_finish", "dispatch", "read_file"])
def test_unrelated_names_are_not_pseudo_tools(name: str) -> None:
    assert is_pseudo_tool_name(name) is False
    assert is_engine_owned_name(name) is False


def test_pseudo_name_matching_ignores_surrounding_whitespace() -> None:
    assert is_pseudo_tool_name("  finish  ") is True
    assert is_engine_owned_name(" dispatch_to_x ") is True


@pytest.mark.parametrize("name", ["a", "_x", "read_file", "A" * 64])
def test_tool_name_pattern_accepts_plain_identifiers(name: str) -> None:
    assert TOOL_NAME_RE.fullmatch(name) is not None


@pytest.mark.parametrize("name", ["", "1tool", "a-b", "a.b", "A" * 65, "读文件"])
def test_tool_name_pattern_rejects_the_rest(name: str) -> None:
    assert TOOL_NAME_RE.fullmatch(name) is None


@pytest.mark.parametrize("name", ["read_file", "cancel_active_tasks", "get_context_window", "apply_diff"])
def test_core_builtin_names_are_reserved(registry: ToolRegistry, name: str) -> None:
    assert registry.reserved_name_reason(name) == _BUILTIN_REASON


@pytest.mark.parametrize("name", ["create_reminder", "draft_execution_plan"])
def test_names_from_register_tools_modules_are_reserved(registry: ToolRegistry, name: str) -> None:
    assert registry.reserved_name_reason(name) == _BUILTIN_REASON


def test_a_tool_registered_after_startup_becomes_reserved(registry: ToolRegistry) -> None:
    async def _noop(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "data": {"result": ""}}

    assert registry.reserved_name_reason("save_memory") is None

    registry.register_builtin_tool("save_memory", "plugin tool", {"type": "object"}, _noop)

    assert registry.reserved_name_reason("save_memory") == _BUILTIN_REASON


@pytest.mark.parametrize("name", sorted(PSEUDO_TOOL_NAMES) + ["dispatch_to_", "dispatch_to_scout"])
def test_engine_owned_names_are_reserved(registry: ToolRegistry, name: str) -> None:
    assert registry.reserved_name_reason(name) == _PSEUDO_REASON


@pytest.mark.parametrize("name", ["weather_lookup", "", "   ", "my_dispatch_to_x"])
def test_a_free_name_has_no_reason(registry: ToolRegistry, name: str) -> None:
    assert registry.reserved_name_reason(name) is None


def test_every_builtin_spec_name_is_reserved_including_plugin_tools() -> None:
    """按 engine 启动的那条路把插件工具也注册进来，再回头问注册表每个名字是不是都占着。

    插件工具走 register_builtin_tool 后进快照，漏了它就意味着 tools/save_memory.py 能悄悄顶掉记忆工具。
    """
    from engine.builtin.loader import auto_discover_and_register

    class _NullHooks:
        def register(self, *args: Any, **kwargs: Any) -> None: ...
        def register_plugin_meta(self, *args: Any, **kwargs: Any) -> None: ...

    registry = ToolRegistry(workspace_root=_ROOT, tools_dir=_ROOT / "tools")
    auto_discover_and_register(_NullHooks(), tool_registry=registry)

    names = [str(spec["name"]) for spec in registry.builtin_specs()]
    assert "save_memory" in names, "插件工具没进内置快照，下面这一条就白测了"
    assert [name for name in names if registry.reserved_name_reason(name) is None] == []


_TOOL_TEMPLATE = '''SPEC = {{
    "name": "{name}",
    "description": "external tool",
    "input_schema": {{"type": "object", "properties": {{}}, "required": []}},
}}

if __name__ == "__main__":
    pass
'''


def _write_external_tool(tools_dir: Path, filename: str, name: str) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / filename).write_text(_TOOL_TEMPLATE.format(name=name), encoding="utf-8")


def test_reload_skips_reserved_names_and_keeps_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    tools_dir = tmp_path / "tools"
    _write_external_tool(tools_dir, "finish.py", "finish")
    _write_external_tool(tools_dir, "read_file.py", "read_file")
    _write_external_tool(tools_dir, "weather_lookup.py", "weather_lookup")
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir, load_external=False)

    caplog.set_level(logging.WARNING, logger="toolbox.registry")
    registry.reload()

    assert registry.get_spec("weather_lookup")["description"] == "external tool"
    # 被跳过的名字仍然指向内置实现，不是外部脚本
    assert registry.get_spec("read_file")["description"] != "external tool"
    assert registry.get_spec("finish") is None

    warnings = [r.getMessage() for r in caplog.records if r.name == "toolbox.registry"]
    assert any("finish.py" in m and _PSEUDO_REASON in m for m in warnings), warnings
    assert any("read_file.py" in m and _BUILTIN_REASON in m for m in warnings), warnings
    assert not any("weather_lookup" in m for m in warnings), warnings


def _ctx(registry: ToolRegistry, workspace_root: Path) -> ToolContext:
    return ToolContext(
        supervisor_url="http://127.0.0.1:0",
        session_id="s-1",
        run_id="r-1",
        worker_id="w-1",
        workspace_root=workspace_root,
        http=None,
        registry=registry,
    )


def _create_tool(registry: ToolRegistry, workspace_root: Path, name: str) -> dict[str, Any]:
    from toolbox.builtins.tool_manage import create_or_update_tool

    return asyncio.run(create_or_update_tool(
        {"name": name, "description": "d", "input_schema": {"type": "object"}, "script": "output({})"},
        _ctx(registry, workspace_root),
    ))


@pytest.mark.parametrize("name, reason", [
    ("read_file", _BUILTIN_REASON),
    ("create_reminder", _BUILTIN_REASON),
    ("finish", _PSEUDO_REASON),
    ("dispatch_to_scout", _PSEUDO_REASON),
    ("dispatch_to_", _PSEUDO_REASON),
])
def test_create_or_update_tool_rejects_reserved_names(
    tmp_path: Path, registry: ToolRegistry, name: str, reason: str,
) -> None:
    result = _create_tool(registry, tmp_path, name)

    assert result["ok"] is False
    assert result["error"] == f"reserved tool name: {name} ({reason})"
    assert not (tmp_path / "tools" / f"{name}.py").exists()


def test_create_or_update_tool_still_rejects_malformed_names(
    tmp_path: Path, registry: ToolRegistry,
) -> None:
    assert _create_tool(registry, tmp_path, "1bad")["error"].startswith("invalid tool name")
    assert _create_tool(registry, tmp_path, "")["error"] == "empty tool name"


def test_create_or_update_tool_consults_the_live_registry(
    tmp_path: Path, registry: ToolRegistry,
) -> None:
    """名字占没占用按调用时的注册表算，插件启动后注册的工具也拦得住。"""
    async def _noop(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "data": {"result": ""}}

    registry.register_builtin_tool("save_memory", "plugin tool", {"type": "object"}, _noop)

    result = _create_tool(registry, tmp_path, "save_memory")

    assert result["error"] == f"reserved tool name: save_memory ({_BUILTIN_REASON})"


def test_the_meta_tools_shim_is_gone() -> None:
    with pytest.raises(ImportError):
        import toolbox.meta_tools  # noqa: F401


@pytest.mark.parametrize("attr", ["RESERVED_TOOL_NAMES", "_RESERVED_TOOL_NAMES", "list_active_tasks", "SKILL_NAME_RE"])
def test_toolbox_builtins_no_longer_owns_these(attr: str) -> None:
    import toolbox.builtins as builtins_pkg

    assert not hasattr(builtins_pkg, attr)


def test_skill_name_pattern_now_lives_with_the_skill_tools() -> None:
    from engine.builtin.knowledge_inject import SKILL_NAME_RE

    assert SKILL_NAME_RE.fullmatch("my-skill") is not None
    assert SKILL_NAME_RE.fullmatch("-nope") is None


def test_list_active_tasks_is_gone_everywhere() -> None:
    import toolbox.builtins.tasks as tasks_module
    from supervisor.execution.models import READ_ONLY_TOOLS

    assert not hasattr(tasks_module, "list_active_tasks")
    assert "list_active_tasks" not in READ_ONLY_TOOLS
    assert READ_ONLY_TOOLS == frozenset({"read_file", "list_dir", "search_in_files", "get_context_window"})


def test_no_registry_still_advertises_list_active_tasks(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")

    assert registry.get_spec("list_active_tasks") is None


def test_pseudo_tools_module_defers_naming_to_tool_names() -> None:
    """名单只能有一份；pseudo_tools 再抄一份就会和注册表的拒收规则慢慢对不上。"""
    import engine.inference.pseudo_tools as pseudo_tools
    from engine.inference import ai_step

    assert not hasattr(pseudo_tools, "_PSEUDO_TOOL_NAMES")
    assert not hasattr(pseudo_tools, "_is_pseudo_tool_name")
    assert pseudo_tools.DISPATCH_TOOL_PREFIX is DISPATCH_TOOL_PREFIX
    assert ai_step.is_pseudo_tool_name is is_pseudo_tool_name
