"""脚本工具的两层：实例自己的 config/tools/ 盖在仓库 tools/ 上面。

同一相对路径实例那份算数，仓库那份原地不动；删掉覆盖就该露出仓库那份。
改造前自定义工具直接写在代码目录的 tools/ 里，搬家记下了它们，之后不能再把它们当成仓库自带的。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402
from supervisor.tool_catalog import external_entries, tool_catalog  # noqa: E402
from toolbox.builtins.tool_manage import create_or_update_tool  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import (  # noqa: E402
    ToolRegistry,
    custom_tool_python_path,
    external_tool_files,
)

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "/v1/admin/config"


def _spec(name: str, **extra: Any) -> str:
    body = {"name": name, "description": f"{name} tool", "input_schema": {"type": "object", "properties": {}}}
    body.update(extra)
    return f"SPEC = {body!r}\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


# 从仓库工具目录里 import 一个邻居模块，跑通就说明 PYTHONPATH 补对了。
_IMPORTING_TOOL = '''SPEC = {"name": "own", "description": "d", "input_schema": {"type": "object", "properties": {}}}

if __name__ == "__main__":
    import json, sys
    sys.stdin.read()
    from _helper import VALUE
    print(json.dumps({"ok": True, "data": {"result": VALUE}}))
'''


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "code"
    (root / config_layout.LEGACY_TOOLS_DIR).mkdir(parents=True)
    (root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def bundled(ws: Path) -> Path:
    """从工作区看过去的仓库工具目录。控制台和注册表都按这个位置找仓库那份。"""
    path = config_layout.workspace_tools_dir(ws)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def admin(ws: Path, code_root: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=ws), prefix=_BASE)

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"{_BASE}{url}", headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


class _Ctx(ToolContext):
    """离线 ToolContext：不连 supervisor，策略一律放行。"""

    def __init__(self, workspace_root: Path, registry: Any = None) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=registry,
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


class TestWhichFilesAreInPlay:
    def test_a_bundled_tool_with_no_override_is_bundled(self, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))

        found = external_tool_files(ws, bundled)

        assert [(item.rel, item.origin) for item in found] == [("weather.py", "bundled")]

    def test_a_file_only_in_the_instance_is_custom(self, ws: Path, bundled: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        found = external_tool_files(ws, bundled)

        assert [(item.rel, item.origin) for item in found] == [("own.py", "custom")]

    def test_the_same_relative_path_on_both_sides_is_an_override(self, ws: Path, bundled: Path) -> None:
        _write(bundled / "pack" / "entry.py", _spec("entry"))
        own = _write(config_layout.custom_tools_dir(ws) / "pack" / "entry.py", _spec("entry"))

        found = external_tool_files(ws, bundled)

        assert [(item.rel, item.origin) for item in found] == [("pack/entry.py", "override")]
        assert found[0].path == own

    def test_the_instance_files_come_first(self, ws: Path, bundled: Path) -> None:
        # 同名时先出现的那份算数，注册表和控制台都按这个顺序取。
        _write(bundled / "weather.py", _spec("weather"))
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        assert [item.origin for item in external_tool_files(ws, bundled)] == ["custom", "bundled"]

    def test_a_tool_left_in_the_code_directory_is_not_a_bundled_tool(self, ws: Path, bundled: Path) -> None:
        # 搬家时抄进 config/tools/ 的那几个，仓库目录里旧文件还在；当成仓库工具的话，删掉自己那份它又冒出来。
        _write(bundled / "legacy_tool.py", _spec("legacy_tool"))
        _write(ws / "data" / ".config_layout.json", json.dumps({"version": 1, "legacy_tools": ["legacy_tool.py"]}))

        assert external_tool_files(ws, bundled) == []

    def test_package_and_private_modules_are_not_tool_entries(self, ws: Path, bundled: Path) -> None:
        for name in ("__init__.py", "_helper.py", "old.disabled.py"):
            _write(bundled / name, _spec("x"))
        _write(bundled / "weather.py", _spec("weather"))

        assert [item.rel for item in external_tool_files(ws, bundled)] == ["weather.py"]


class TestTheImportPathHandedToCustomTools:
    def test_a_custom_tool_gets_the_bundled_neighbourhood(self, ws: Path, bundled: Path, code_root: Path) -> None:
        # 改过的 drawtools 入口还得 import 仓库那份 common，新写的工具照抄 from _channel import 也得能用。
        (bundled / "drawtools").mkdir(parents=True, exist_ok=True)
        _write(config_layout.custom_tools_dir(ws) / "drawtools" / "nai.py", _spec("nai"))
        item = external_tool_files(ws, bundled)[0]

        assert custom_tool_python_path(item, bundled) == (
            str(bundled / "drawtools"), str(bundled), str(code_root),
        )

    def test_a_bundled_tool_needs_nothing_extra(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))
        item = external_tool_files(ws, bundled)[0]

        assert custom_tool_python_path(item, bundled) == ()

    def test_directories_that_do_not_exist_are_left_out(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "pack" / "entry.py", _spec("entry"))
        item = external_tool_files(ws, bundled)[0]

        assert custom_tool_python_path(item, bundled) == (str(bundled), str(code_root))

    def test_the_running_subprocess_really_finds_the_bundled_module(self, ws: Path, bundled: Path) -> None:
        # 改过的工具落到 config/tools/ 之后，import 仓库那份邻居模块必须照样成立。
        _write(bundled / "_helper.py", 'VALUE = "from-bundled-sibling"\n')
        _write(config_layout.custom_tools_dir(ws) / "own.py", _IMPORTING_TOOL)
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        result = asyncio.run(registry.execute(name="own", arguments={}, ctx=_Ctx(ws)))

        assert result == {"ok": True, "data": {"result": "from-bundled-sibling"}}


class TestTheRegistry:
    def test_it_registers_both_layers(self, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        assert registry.get_spec("weather") is not None
        assert registry.get_spec("own") is not None

    def test_the_instance_copy_wins_when_both_declare_the_same_name(self, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather", description="bundled"))
        _write(config_layout.custom_tools_dir(ws) / "weather.py", _spec("weather", description="own"))

        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        assert registry.get_spec("weather")["description"] == "own"

    def test_two_scripts_claiming_one_name_do_not_both_register(self, ws: Path, bundled: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "a.py", _spec("twin", description="first"))
        _write(bundled / "z.py", _spec("twin", description="second"))

        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        assert registry.get_spec("twin")["description"] == "first"

    def test_a_name_taken_by_a_builtin_is_skipped(self, ws: Path, bundled: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "sneaky.py", _spec("write_file", description="hijack"))

        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        assert registry.get_spec("write_file")["description"] != "hijack"

    def test_reloading_creates_neither_directory(self, ws: Path, bundled: Path, tmp_path: Path) -> None:
        # 代码目录对多开实例只读，工作区里也不该凭空多出一个 tools 包。
        missing = tmp_path / "nothing-here"

        registry = ToolRegistry(workspace_root=ws, tools_dir=missing)
        registry.reload()

        assert not missing.exists()
        assert not config_layout.custom_tools_dir(ws).exists()

    def test_a_new_instance_file_shows_up_after_a_reload(self, ws: Path, bundled: Path) -> None:
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        registry.reload()

        assert registry.get_spec("own") is not None


class TestMcpToolsSurviveAScriptReload:
    def _clients(self, monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
        monkeypatch.setattr(
            mcp_runtime, "list_clients",
            lambda root: [{"id": cid, "enabled": True} for cid in ids],
        )

        async def list_tools(root: Path, cid: str) -> dict[str, Any]:
            return {"tools": [{"name": "ping", "description": f"from {cid}"}]}

        monkeypatch.setattr(mcp_runtime, "list_tools", list_tools)

    def test_a_script_reload_keeps_them(self, ws: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 热重载一次就把 MCP 工具全冲掉的话，加个脚本工具等于把 MCP 那批下线。
        self._clients(monkeypatch, "fs")
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)
        assert asyncio.run(registry.load_mcp_tools()) == 1

        registry.reload()

        assert registry.get_spec("mcp_fs_ping") is not None

    def test_a_client_that_disappeared_takes_its_tools_along(
        self, ws: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clients(monkeypatch, "fs")
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)
        asyncio.run(registry.load_mcp_tools())

        self._clients(monkeypatch)
        asyncio.run(registry.load_mcp_tools())

        assert registry.get_spec("mcp_fs_ping") is None

    def test_a_script_tool_is_not_dropped_by_an_mcp_rescan(
        self, ws: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clients(monkeypatch, "fs")
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        asyncio.run(registry.load_mcp_tools())

        assert registry.get_spec("own") is not None


class TestTheSupervisorSideCatalog:
    def test_each_entry_carries_its_origin_and_relative_path(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))
        _write(config_layout.custom_tools_dir(ws) / "weather.py", _spec("weather"))

        rows = {entry.name: entry for entry in external_entries(ws)}

        assert (rows["weather"].origin, rows["weather"].rel) == ("override", "weather.py")
        assert (rows["own"].origin, rows["own"].rel) == ("custom", "own.py")

    def test_the_file_is_named_the_way_the_workspace_sees_it(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))

        entry = next(entry for entry in external_entries(ws) if entry.name == "weather")

        assert entry.file == "tools/weather.py"

    def test_an_instance_copy_is_named_under_config(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        entry = next(entry for entry in external_entries(ws) if entry.name == "own")

        assert entry.file == "config/tools/own.py"

    def test_a_broken_spec_is_still_listed_so_it_can_be_repaired(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "broken.py", "SPEC = not a dict\n")

        entry = next(entry for entry in external_entries(ws) if entry.name == "broken")

        assert (entry.has_spec, entry.origin) == (False, "custom")

    def test_a_builtin_name_is_not_displaced_by_a_script(self, ws: Path, bundled: Path, code_root: Path) -> None:
        _write(config_layout.custom_tools_dir(ws) / "sneaky.py", _spec("write_file", description="hijack"))

        entry = next(entry for entry in tool_catalog(ws) if entry.name == "write_file")

        assert entry.source == "builtin"


class TestToolsWrittenByTheAgent:
    def test_create_or_update_tool_writes_the_instance_copy(self, ws: Path, bundled: Path, code_root: Path) -> None:
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        result = asyncio.run(create_or_update_tool(
            {"name": "own_tool", "script": "output({'ok': True})"}, _Ctx(ws, registry),
        ))

        assert result["ok"] is True, result
        assert result["data"]["path"] == "config/tools/own_tool.py"
        assert (config_layout.custom_tools_dir(ws) / "own_tool.py").is_file()

    def test_rewriting_a_bundled_tool_leaves_the_bundled_file_alone(
        self, ws: Path, bundled: Path, code_root: Path,
    ) -> None:
        original = _write(bundled / "weather.py", _spec("weather", description="bundled"))
        registry = ToolRegistry(workspace_root=ws, tools_dir=bundled)

        asyncio.run(create_or_update_tool(
            {"name": "weather", "script": "output({'ok': True})"}, _Ctx(ws, registry),
        ))

        assert original.read_text(encoding="utf-8") == _spec("weather", description="bundled")
        assert (config_layout.custom_tools_dir(ws) / "weather.py").is_file()


class TestMcpClientsAreWrittenToTheNewLocation:
    _SPEC = {"id": "fs", "transport": "stdio", "command": "echo", "args": ["hi"]}

    def test_a_new_client_creates_the_config_file(self, ws: Path) -> None:
        mcp_runtime.upsert_client(ws, self._SPEC)

        data = yaml.safe_load(config_layout.mcp_path(ws).read_text(encoding="utf-8"))
        assert data["clients"]["fs"]["command"] == "echo"
        assert not (ws / config_layout.LEGACY_MCP_FILE).exists()

    def test_deleting_rewrites_the_same_file(self, ws: Path) -> None:
        mcp_runtime.upsert_client(ws, self._SPEC)

        assert mcp_runtime.delete_client(ws, "fs") is True
        assert yaml.safe_load(config_layout.mcp_path(ws).read_text(encoding="utf-8"))["clients"] == {}

    def test_an_old_file_left_in_place_is_not_read(self, ws: Path) -> None:
        # 搬家只复制不删；再读旧文件就成了控制台改了新位置却不生效。
        _write(ws / config_layout.LEGACY_MCP_FILE, "version: 1\nclients:\n  stale:\n    transport: stdio\n")

        assert mcp_runtime.list_clients(ws) == []

    @pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 POSIX 权限位")
    def test_the_config_file_is_owner_only(self, ws: Path) -> None:
        # stdio 客户端的 env 里放着令牌。
        mcp_runtime.upsert_client(ws, self._SPEC)

        assert config_layout.mcp_path(ws).stat().st_mode & 0o777 == 0o600


class TestTheConsoleToolEndpoints:
    def test_the_listing_says_where_each_script_came_from(self, admin, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))
        _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        rows = {row["name"]: row for row in admin("GET", "/tools").json() if row["source"] == "external"}

        assert rows["weather"]["origin"] == "bundled"
        assert rows["own"]["origin"] == "custom"

    def test_reading_a_bundled_tool_returns_the_bundled_source(self, admin, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))

        body = admin("GET", "/tools/weather/raw").json()

        assert (body["origin"], body["path"]) == ("bundled", "tools/weather.py")

    def test_saving_a_bundled_tool_creates_an_instance_copy(self, admin, ws: Path, bundled: Path) -> None:
        original = _write(bundled / "weather.py", _spec("weather"))

        response = admin("PUT", "/tools/weather/raw", json={"content": _spec("weather", description="edited")})

        assert response.status_code == 200, response.text
        assert response.json()["path"] == "config/tools/weather.py"
        assert original.read_text(encoding="utf-8") == _spec("weather")
        assert admin("GET", "/tools/weather/raw").json()["origin"] == "override"

    def test_a_tool_in_a_subdirectory_keeps_its_relative_path(self, admin, ws: Path, bundled: Path) -> None:
        _write(bundled / "pack" / "entry.py", _spec("entry"))

        response = admin("PUT", "/tools/entry/raw", json={"content": _spec("entry", description="edited")})

        assert response.json()["path"] == "config/tools/pack/entry.py"

    def test_creating_a_tool_writes_the_instance_directory(self, admin, ws: Path, bundled: Path) -> None:
        response = admin("POST", "/tools", json={"id": "own", "content": _spec("own")})

        assert response.status_code == 200, response.text
        assert (config_layout.custom_tools_dir(ws) / "own.py").is_file()

    def test_creating_a_tool_whose_name_is_taken_is_refused(self, admin, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather"))

        assert admin("POST", "/tools", json={"id": "weather", "content": _spec("weather")}).status_code == 409

    def test_deleting_a_custom_tool_removes_it(self, admin, ws: Path, bundled: Path) -> None:
        own = _write(config_layout.custom_tools_dir(ws) / "own.py", _spec("own"))

        response = admin("DELETE", "/tools/own")

        assert response.json() == {"ok": True, "restored": False}
        assert not own.exists()

    def test_deleting_an_override_brings_the_bundled_script_back(self, admin, ws: Path, bundled: Path) -> None:
        _write(bundled / "weather.py", _spec("weather", description="bundled"))
        _write(config_layout.custom_tools_dir(ws) / "weather.py", _spec("weather", description="own"))

        response = admin("DELETE", "/tools/weather")

        assert response.json() == {"ok": True, "restored": True}
        assert admin("GET", "/tools/weather/raw").json()["origin"] == "bundled"

    def test_a_bundled_tool_that_was_never_changed_cannot_be_deleted(self, admin, ws: Path, bundled: Path) -> None:
        original = _write(bundled / "weather.py", _spec("weather"))

        assert admin("DELETE", "/tools/weather").status_code == 409
        assert original.is_file()

    def test_a_builtin_tool_has_no_source_to_edit(self, admin, ws: Path, bundled: Path) -> None:
        assert admin("GET", "/tools/write_file/raw").status_code == 409
