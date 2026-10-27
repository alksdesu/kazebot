"""脚本工具的 guard：送审的参数怎么拼，常量参数 fixed 写坏了怎么办。

脚本在子进程里跑，拿不到 ToolContext，注册表这层是唯一挂得上闸门的地方。fixed 让策略按脚本自己要读的那个
固定位置判，不随模型给的参数变；写坏了要么不登记，要么拒跑，不能当没写，按模型给的参数去判就判错了对象。
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
from toolbox.registry import ToolRegistry, _guard_parameters, _guard_problem, _script_guard_error  # noqa: E402

_GUARD = {"op": "read_file", "fixed": {"path": "data/.admin_token"}, "params": {"action": "action", "path": "path"}}

_TOOL = '''SPEC = {{
    "name": "{name}",
    "description": "guarded probe",
    "input_schema": {{"type": "object", "properties": {{}}, "required": []}},
    "guard": {guard!r},
}}

if __name__ == "__main__":
    import json, sys
    sys.stdin.read()
    print(json.dumps({{"ok": True, "data": {{"result": "ran"}}}}))
'''


def _write_tool(tools_dir: Path, name: str, guard: dict[str, Any]) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / f"{name}.py").write_text(_TOOL.format(name=name, guard=guard), encoding="utf-8")


class _Ctx(ToolContext):
    """离线 ToolContext：记下送审的参数，按预设 safety_level 回应。"""

    def __init__(self, workspace_root: Path, *, safety: str = "deny") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )
        self._safety = safety
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self._safety, "reason": "runtime data may contain secrets or private context"}

    async def check_cancelled(self) -> bool:
        return False


class TestGuardParameters:
    def test_the_constant_is_written_after_the_mapping_and_wins(self) -> None:
        assert _guard_parameters(_GUARD, "probe", {"action": "api", "path": "/v1/admin/state"}) == {
            "tool_name": "probe", "action": "api", "path": "data/.admin_token",
        }

    def test_an_argument_the_model_left_out_is_sent_empty(self) -> None:
        assert _guard_parameters(_GUARD, "probe", {}) == {
            "tool_name": "probe", "action": "", "path": "data/.admin_token",
        }

    def test_a_guard_without_constants_only_maps_the_arguments(self) -> None:
        guard = {"op": "read_file", "params": {"path": "file"}}

        assert _guard_parameters(guard, "probe", {"file": "notes.txt"}) == {"tool_name": "probe", "path": "notes.txt"}


class TestGuardProblem:
    @pytest.mark.parametrize("guard", [
        _GUARD,
        {"op": "read_file"},
        {"op": "read_file", "params": {"path": "file"}},
    ])
    def test_a_usable_guard_has_no_problem(self, guard: dict[str, Any]) -> None:
        assert _guard_problem(guard) == ""

    @pytest.mark.parametrize("fixed", [
        {},
        [],
        "data/.admin_token",
        ["path", "data/.admin_token"],
        {"": "data/x"},
        {" path": "data/x"},
        {1: "data/x"},
        {"path": ""},
        {"path": " data/x"},
        {"path": 1},
        {"path": ["data/x"]},
        # 这两个由引擎按调用填，常量写进去就能冒充别的工具、别人的身份
        {"tool_name": "other_tool"},
        {"platform_auth": "admin"},
    ])
    def test_an_unusable_constant_is_named(self, fixed: Any) -> None:
        assert _guard_problem({"op": "read_file", "fixed": fixed}).startswith("guard.fixed")


class TestRegistration:
    def test_a_script_with_an_unusable_constant_is_not_registered(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        tools_dir = tmp_path / "tools"
        _write_tool(tools_dir, "broken_probe", {"op": "read_file", "fixed": {"path": ""}})
        _write_tool(tools_dir, "good_probe", _GUARD)
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir, load_external=False)

        caplog.set_level(logging.WARNING, logger="toolbox.registry")
        registry.reload()

        assert registry.get_spec("broken_probe") is None
        assert registry.get_spec("good_probe")["description"] == "guarded probe"
        warnings = [record.getMessage() for record in caplog.records if record.name == "toolbox.registry"]
        assert any("broken_probe.py" in message and "guard.fixed.path" in message for message in warnings), warnings
        assert not any("good_probe" in message for message in warnings), warnings


class TestExecution:
    def test_the_policy_judges_the_constant_not_the_argument(self, tmp_path: Path) -> None:
        tools_dir = tmp_path / "tools"
        _write_tool(tools_dir, "good_probe", _GUARD)
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir)
        ctx = _Ctx(tmp_path, safety="deny")

        result = asyncio.run(registry.execute(
            name="good_probe", arguments={"action": "api", "path": "notes.txt"}, ctx=ctx,
        ))

        assert result["ok"] is False
        assert result["error"] == "runtime data may contain secrets or private context"
        assert ctx.requested == [
            ("read_file", {"tool_name": "good_probe", "action": "api", "path": "data/.admin_token"}),
        ]

    def test_an_allowed_call_still_runs_the_script(self, tmp_path: Path) -> None:
        tools_dir = tmp_path / "tools"
        _write_tool(tools_dir, "good_probe", _GUARD)
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir)
        ctx = _Ctx(tmp_path, safety="auto")

        result = asyncio.run(registry.execute(name="good_probe", arguments={"action": "api"}, ctx=ctx))

        assert result["ok"] is True, result
        assert result["data"]["result"] == "ran"
        assert len(ctx.requested) == 1

    def test_a_plugin_tool_with_an_unusable_constant_is_refused_without_asking(self, tmp_path: Path) -> None:
        # 插件工具注册时不查 guard，跑的时候走同一个闸门：写坏的常量在这里拒，不拿映射出来的参数去问。
        ctx = _Ctx(tmp_path, safety="auto")
        guard = {"op": "read_file", "fixed": {"tool_name": "other_tool"}, "params": {"path": "path"}}

        refused = asyncio.run(_script_guard_error(guard, "plugin_probe", {"path": "notes.txt"}, ctx))

        assert refused is not None
        assert refused["ok"] is False
        assert "plugin_probe declares an unusable guard" in refused["error"]
        assert ctx.requested == []
