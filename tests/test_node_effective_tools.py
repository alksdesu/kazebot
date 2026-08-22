"""节点授权界面读的那份「勾了还剩下哪些能用」。

勾中不等于能用：名字写错了注入时是静默忽略的，界面拿不到这份回答就只能显示成
「已授权」，运营者会以为权限已经给出去了。
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
_BASE = "/v1/admin/config/nodes"

_GUARDED_TOOL = '''SPEC = {
    "name": "guarded_tool",
    "description": "declares a guard",
    "input_schema": {"type": "object", "properties": {}},
    "guard": {"operation": "execute_command"},
}
'''

_LOOSE_TOOL = '''SPEC = {
    "name": "loose_tool",
    "description": "no guard at all",
    "input_schema": {"type": "object", "properties": {}},
}
'''


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "guarded_tool.py").write_text(_GUARDED_TOOL, encoding="utf-8")
    (tmp_path / "tools" / "loose_tool.py").write_text(_LOOSE_TOOL, encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-effective-tools"),
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

    # 认证失败计数是模块级的，不清干净会让后面跑的用例撞上 429。
    admin_api._auth_failures.clear()
    yield call
    admin_api._auth_failures.clear()


def _write_node(workspace: Path, node_id: str, body: str) -> None:
    (workspace / "config" / "nodes" / f"{node_id}.yaml").write_text(body, encoding="utf-8")


def test_lists_the_tools_the_node_was_granted(client, workspace: Path) -> None:
    _write_node(workspace, "qq.orchestrator", """
id: qq.orchestrator
type: ai
tool_access:
  mode: allowlist
  allow:
    - read_file
    - guarded_tool
""")

    resp = client("GET", f"{_BASE}/qq.orchestrator/effective-tools", headers=_AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "allowlist"
    assert [row["name"] for row in body["tools"]] == ["read_file", "guarded_tool"]


def test_flags_a_name_that_is_not_registered(client, workspace: Path) -> None:
    _write_node(workspace, "qq.orchestrator", """
id: qq.orchestrator
tool_access:
  mode: allowlist
  allow:
    - read_file
    - ghost_tool
""")

    rows = {r["name"]: r for r in client(
        "GET", f"{_BASE}/qq.orchestrator/effective-tools", headers=_AUTH,
    ).json()["tools"]}

    assert rows["read_file"]["registered"] is True
    assert rows["ghost_tool"]["registered"] is False


def test_only_external_scripts_get_a_verdict_on_guard(client, workspace: Path) -> None:
    # 内置工具走不走 request_guard 得看源码，不猜就是不猜 —— null 不能变成 false。
    _write_node(workspace, "qq.orchestrator", """
id: qq.orchestrator
tool_access:
  mode: allowlist
  allow:
    - read_file
    - guarded_tool
    - loose_tool
""")

    rows = {r["name"]: r for r in client(
        "GET", f"{_BASE}/qq.orchestrator/effective-tools", headers=_AUTH,
    ).json()["tools"]}

    assert rows["read_file"]["external"] is False
    assert rows["read_file"]["guarded"] is None
    assert rows["guarded_tool"]["guarded"] is True
    assert rows["loose_tool"]["guarded"] is False


def test_a_wide_open_node_reports_what_it_can_call_not_what_it_cannot(client, workspace: Path) -> None:
    # 只注解 deny 名单的话，权限最宽的那些节点在界面上一条警告都看不到 ——
    # 而「没声明 guard 的外部脚本」正是要在它们身上提醒的。
    _write_node(workspace, "bootstrap.executor", """
id: bootstrap.executor
tool_access:
  mode: all
  deny:
    - execute_command
""")

    body = client("GET", f"{_BASE}/bootstrap.executor/effective-tools", headers=_AUTH).json()
    names = [row["name"] for row in body["tools"]]

    assert "execute_command" not in names
    assert "read_file" in names
    assert {"guarded_tool", "loose_tool"} <= set(names)
    assert next(r for r in body["tools"] if r["name"] == "loose_tool")["guarded"] is False


def test_a_typo_in_the_deny_list_is_called_out(client, workspace: Path) -> None:
    # 禁令写错等于没禁，而界面上那一行看着和真禁掉了一模一样。
    _write_node(workspace, "bootstrap.executor", """
id: bootstrap.executor
tool_access:
  mode: all
  deny:
    - execute_commnad
""")

    body = client("GET", f"{_BASE}/bootstrap.executor/effective-tools", headers=_AUTH).json()

    assert body["dead_names"] == ["execute_commnad"]
    assert "execute_command" in [row["name"] for row in body["tools"]]


def test_a_typo_in_the_allowlist_is_called_out_too(client, workspace: Path) -> None:
    _write_node(workspace, "qq.orchestrator", """
id: qq.orchestrator
tool_access:
  mode: allowlist
  allow:
    - read_file
    - ghost_tool
""")

    body = client("GET", f"{_BASE}/qq.orchestrator/effective-tools", headers=_AUTH).json()

    assert body["dead_names"] == ["ghost_tool"]


def test_accepts_the_string_shorthand(client, workspace: Path) -> None:
    _write_node(workspace, "qq.intent", "id: qq.intent\ntool_access: none\n")

    body = client("GET", f"{_BASE}/qq.intent/effective-tools", headers=_AUTH).json()

    assert body["mode"] == "none"
    assert body["tools"] == []


def test_unknown_node_is_a_404(client) -> None:
    resp = client("GET", f"{_BASE}/nope/effective-tools", headers=_AUTH)

    assert resp.status_code == 404


def test_refuses_to_step_out_of_the_nodes_directory(client, workspace: Path) -> None:
    (workspace / "config" / "secrets.yaml").write_text("tool_access: all\n", encoding="utf-8")

    resp = client("GET", f"{_BASE}/..%2Fsecrets/effective-tools", headers=_AUTH)

    assert resp.status_code in (400, 404)


def test_requires_the_admin_token(client, workspace: Path) -> None:
    _write_node(workspace, "qq.intent", "id: qq.intent\ntool_access: none\n")

    assert client("GET", f"{_BASE}/qq.intent/effective-tools").status_code == 401
