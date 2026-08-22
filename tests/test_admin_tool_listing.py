"""管理端列外部工具的口径。

显示一套遍历、执行另一套，结果是 drawtools/ 和 stocktool/ 里六个真在被调用的工具
在界面上根本不存在，白名单里勾着的名字也对不上号。这里把两边钉成同一份。
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
from toolbox.registry import extract_tool_spec, iter_external_tool_files  # noqa: E402

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


def _names(client) -> list[str]:
    return [row["name"] for row in client("GET", _BASE, headers=_AUTH).json()]


def test_it_reaches_into_subdirectories(client) -> None:
    assert "pkg_entry" in _names(client)


def test_it_uses_the_spec_name_not_the_file_stem(client) -> None:
    rows = {row["name"]: row for row in client("GET", _BASE, headers=_AUTH).json()}

    assert "spec_name" in rows
    assert "renamed" not in rows
    assert rows["spec_name"]["file"] == "renamed.py"


def test_it_skips_package_internals_and_disabled_scripts(client) -> None:
    names = _names(client)

    assert names == ["flat_tool", "pkg_entry", "spec_name"]


def test_it_still_carries_description_and_timeout(client) -> None:
    rows = {row["name"]: row for row in client("GET", _BASE, headers=_AUTH).json()}

    assert rows["flat_tool"]["description"] == "flat_tool does things"
    assert rows["flat_tool"]["timeout_sec"] == 45
    assert rows["flat_tool"]["has_spec"] is True


def test_a_top_level_script_with_a_broken_spec_stays_listed(client, workspace: Path) -> None:
    # 列表是唯一能打开编辑器的入口，SPEC 写坏的脚本掉出列表就再也修不回来。
    (workspace / "tools" / "typo.py").write_text("SPEC = {oops\n", encoding="utf-8")

    rows = {row["name"]: row for row in client("GET", _BASE, headers=_AUTH).json()}

    assert rows["typo"]["has_spec"] is False


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


def test_the_endpoint_and_the_registry_see_the_same_tools() -> None:
    """真实 tools/ 目录上跑一遍：端点口径和注册表口径的差集必须为空。"""
    registered = set()
    for py in iter_external_tool_files(_REPO / "tools"):
        spec, _timeout = extract_tool_spec(py)
        name = spec.get("name") if isinstance(spec, dict) else None
        if isinstance(name, str) and name.strip():
            registered.add(name.strip())

    router = admin_api.create_admin_router(_REPO)
    listed = {row["name"] for route in router.routes
              if getattr(route, "path", "") == "/tools" and "GET" in getattr(route, "methods", set())
              for row in route.endpoint()}

    assert registered - listed == set()
    assert listed - registered == set()
