"""管理端列工具的口径。

显示一套遍历、执行另一套，结果是 drawtools/ 和 stocktool/ 里六个真在被调用的工具
在界面上根本不存在，白名单里勾着的名字也对不上号。这里把两边钉成同一份。

内置和插件工具同样要列出来：界面上看不见的工具，谁也管不到它。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "/v1/admin/config/tools"
_REPO = Path(__file__).resolve().parents[1]


def _script(name: str, *, timeout: int | None = None) -> str:
    body = (
        'SPEC = {\n'
        f'    "name": "{name}",\n'
        f'    "description": "{name} does things",\n'
        '    "input_schema": {"type": "object", "properties": {}},\n'
        '}\n'
    )
    return body + (f"TIMEOUT_SEC = {timeout}\n" if timeout is not None else "")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    (tools / "pkg").mkdir(parents=True)
    (tools / "flat_tool.py").write_text(_script("flat_tool", timeout=45), encoding="utf-8")
    (tools / "renamed.py").write_text(_script("spec_name"), encoding="utf-8")
    (tools / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tools / "pkg" / "_helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tools / "pkg" / "library.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tools / "pkg" / "entry.py").write_text(_script("pkg_entry"), encoding="utf-8")
    (tools / "retired.disabled.py").write_text(_script("retired"), encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-tool-listing"),
            policy=PolicyEngine(workspace_root=workspace),
        ),
        process_manager=None,
        config_store=ConfigStore(path=workspace / "data" / "config.yaml"),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(go())

    admin_api._auth_failures.clear()
    yield call
    admin_api._auth_failures.clear()


def _rows(client) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in client("GET", _BASE, headers=_AUTH).json()}


def _names(client, source: str | None = None) -> list[str]:
    rows = client("GET", _BASE, headers=_AUTH).json()
    return [row["name"] for row in rows if source is None or row["source"] == source]


def test_it_reaches_into_subdirectories(client) -> None:
    assert "pkg_entry" in _names(client)


def test_it_uses_the_spec_name_not_the_file_stem(client) -> None:
    rows = _rows(client)

    assert "spec_name" in rows
    assert "renamed" not in rows
    assert rows["spec_name"]["file"] == "tools/renamed.py"


def test_it_skips_package_internals_and_disabled_scripts(client) -> None:
    assert _names(client, "external") == ["flat_tool", "pkg_entry", "spec_name"]


def test_builtin_and_plugin_tools_are_listed_too(client) -> None:
    # 只列 tools/ 目录的话，界面上四十几个工具里只看得见二十个，剩下的谁也管不到。
    rows = _rows(client)

    assert rows["write_file"]["source"] == "builtin"
    assert rows["save_memory"]["source"] == "plugin"
    assert rows["flat_tool"]["source"] == "external"


def test_only_external_tools_are_editable(client) -> None:
    rows = _rows(client)

    assert rows["flat_tool"]["editable"] is True
    assert rows["write_file"]["editable"] is False
    assert rows["save_memory"]["editable"] is False
    # 没有源码文件就别报一个路径出去，否则编辑器打开只会看到「文件不存在」。
    assert "file" not in rows["write_file"]


def test_the_editor_refuses_a_builtin_instead_of_reporting_a_missing_file(client) -> None:
    resp = client("GET", f"{_BASE}/write_file/raw", headers=_AUTH)

    assert resp.status_code == 409
    assert "内置" in resp.json()["detail"]


def test_deleting_a_builtin_is_refused(client) -> None:
    resp = client("DELETE", f"{_BASE}/write_file", headers=_AUTH)

    assert resp.status_code == 409


def test_creating_a_name_a_builtin_already_owns_is_a_conflict(client) -> None:
    # 建了也没用：注册表里内置工具直接盖掉同名的外部脚本，文件白留在磁盘上。
    resp = client("POST", _BASE, headers=_AUTH, json={"id": "write_file", "content": _script("write_file")})

    assert resp.status_code == 409


def test_deleting_a_tool_that_does_not_exist_is_a_404(client) -> None:
    assert client("DELETE", f"{_BASE}/nope", headers=_AUTH).status_code == 404


def test_it_still_carries_description_and_timeout(client) -> None:
    rows = _rows(client)

    assert rows["flat_tool"]["description"] == "flat_tool does things"
    assert rows["flat_tool"]["timeout_sec"] == 45
    assert rows["flat_tool"]["has_spec"] is True


def test_a_top_level_script_with_a_broken_spec_stays_listed(client, workspace: Path) -> None:
    # 列表是唯一能打开编辑器的入口，SPEC 写坏的脚本掉出列表就再也修不回来。
    (workspace / "tools" / "typo.py").write_text("SPEC = {oops\n", encoding="utf-8")

    rows = _rows(client)

    assert rows["typo"]["has_spec"] is False
    assert rows["typo"]["editable"] is True


def test_the_editor_opens_a_tool_that_lives_in_a_subdirectory(client) -> None:
    resp = client("GET", f"{_BASE}/pkg_entry/raw", headers=_AUTH)

    assert resp.status_code == 200
    assert '"name": "pkg_entry"' in resp.json()["content"]


def test_saving_writes_back_to_the_file_the_name_came_from(client, workspace: Path) -> None:
    client("PUT", f"{_BASE}/spec_name/raw", headers=_AUTH, json={"content": _script("spec_name")})

    assert not (workspace / "tools" / "spec_name.py").exists()
    assert (workspace / "tools" / "renamed.py").read_text(encoding="utf-8").startswith("SPEC")


def test_deleting_removes_the_real_file_instead_of_reporting_a_silent_success(
    client, workspace: Path,
) -> None:
    assert client("DELETE", f"{_BASE}/pkg_entry", headers=_AUTH).json()["ok"] is True

    assert not (workspace / "tools" / "pkg" / "entry.py").exists()


def test_creating_a_name_a_subdirectory_already_owns_is_a_conflict(client) -> None:
    resp = client("POST", _BASE, headers=_AUTH, json={"id": "pkg_entry", "content": _script("pkg_entry")})

    assert resp.status_code == 409


def _listed_by_endpoint(repo: Path) -> set[str]:
    router = admin_api.create_admin_router(repo)
    return {row["name"] for route in router.routes
            if getattr(route, "path", "") == "/tools" and "GET" in getattr(route, "methods", set())
            for row in route.endpoint()}


def test_the_endpoint_and_the_registry_see_the_same_tools() -> None:
    """真实仓库上跑一遍：界面列出的，和 engine 真正能调的，必须是同一批。

    注册表活在 engine worker 进程里，管理接口够不着，只能照它的三个来源重建一遍。
    重建就会漂，所以这里把运行时那一份原样搭起来对一次。
    """
    from engine.builtin.loader import auto_discover_and_register
    from toolbox.registry import ToolRegistry

    class _NullHooks:
        def register(self, *args: Any, **kwargs: Any) -> None: ...
        def register_plugin_meta(self, *args: Any, **kwargs: Any) -> None: ...

    registry = ToolRegistry(workspace_root=_REPO, tools_dir=_REPO / "tools")
    auto_discover_and_register(_NullHooks(), tool_registry=registry)
    runtime = {spec["name"] for spec in registry.list_specs()}

    assert runtime == _listed_by_endpoint(_REPO)


def test_every_listed_tool_can_be_granted_to_a_node() -> None:
    """两个端点必须报同一批名字：授权页勾不到的工具，等于没人管得到它。"""
    router = admin_api.create_admin_router(_REPO)
    grantable = {name for route in router.routes
                 if getattr(route, "path", "") == "/all-tool-names"
                 for name in route.endpoint()}

    assert grantable == _listed_by_endpoint(_REPO)
