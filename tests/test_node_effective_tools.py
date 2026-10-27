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
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
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
    # 最后一行是引擎自己加的超长输出读取工具，白名单里没写也能调
    assert [row["name"] for row in body["tools"]] == ["read_file", "guarded_tool", "read_tool_output"]
    assert [bool(row["automatic"]) for row in body["tools"]] == [False, False, True]


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


@pytest.mark.parametrize(("body", "problem"), [
    ("type: ai\napi_key: sk-SECRET-node: x\ntool_access: all\n", "YAML 读不出来：第 2 行第 24 列：mapping values are not allowed here"),
    ("type: robot\ntool_access: all\n", "type 写的是 robot，只认 ai、tool"),
])
def test_a_node_file_that_is_not_a_usable_definition_is_a_409_with_the_reason(
    client, workspace: Path, body: str, problem: str,
) -> None:
    # 按空表算会报成「一个工具都没给」，人会去改授权；该修的是这份文件。
    _write_node(workspace, "qq.intent", body)

    resp = client("GET", f"{_BASE}/qq.intent/effective-tools", headers=_AUTH)

    assert resp.status_code == 409
    assert resp.json()["detail"] == f"config/nodes/qq.intent.yaml 不是能用的节点定义：{problem}"
    assert "sk-SECRET" not in resp.text


def test_refuses_to_step_out_of_the_nodes_directory(client, workspace: Path) -> None:
    (workspace / "config" / "secrets.yaml").write_text("tool_access: all\n", encoding="utf-8")

    resp = client("GET", f"{_BASE}/..%2Fsecrets/effective-tools", headers=_AUTH)

    assert resp.status_code in (400, 404)


def test_requires_the_admin_token(client, workspace: Path) -> None:
    _write_node(workspace, "qq.intent", "id: qq.intent\ntool_access: none\n")

    assert client("GET", f"{_BASE}/qq.intent/effective-tools").status_code == 401


def test_a_tool_needing_another_edit_mode_is_marked_as_dropped(client, workspace: Path) -> None:
    # edit_lines 只给 edit_mode: hashline 的节点；勾了却没开模式的，构建工具表时会被摘掉
    _write_node(workspace, "plain.coder", """
id: plain.coder
type: ai
tool_access:
  mode: allowlist
  allow: [edit_lines, read_file]
""")
    _write_node(workspace, "hash.coder", """
id: hash.coder
type: ai
edit_mode: hashline
tool_access:
  mode: allowlist
  allow: [edit_lines, read_file]
""")

    plain = {row["name"]: row for row in client("GET", f"{_BASE}/plain.coder/effective-tools", headers=_AUTH).json()["tools"]}
    hashed = {row["name"]: row for row in client("GET", f"{_BASE}/hash.coder/effective-tools", headers=_AUTH).json()["tools"]}

    assert "hashline" in plain["edit_lines"]["gated"]
    assert plain["read_file"]["gated"] == ""
    assert hashed["edit_lines"]["gated"] == ""



def _rows(client, node_id: str) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in client("GET", f"{_BASE}/{node_id}/effective-tools", headers=_AUTH).json()["tools"]}


_ISOLATABLE_CODER = "id: w.coder\ntype: ai\nisolation: worktree\ntool_access: all\n"


def test_the_merge_tool_comes_with_a_delegate_target_that_opts_into_isolation(client, workspace: Path) -> None:
    # 没勾也能调：委派目标写了 isolation: worktree，引擎就给合回工具
    _write_node(workspace, "w.coder", _ISOLATABLE_CODER)
    _write_node(workspace, "w.lead", """
id: w.lead
type: ai
delegate_targets: [w.coder]
tool_access:
  mode: allowlist
  allow: [read_file]
""")

    rows = _rows(client, "w.lead")

    assert rows["read_file"]["automatic"] == ""
    assert rows["merge_worktree"]["gated"] == ""
    assert "w.coder" in rows["merge_worktree"]["automatic"] and "isolation: worktree" in rows["merge_worktree"]["automatic"]
    assert rows["read_tool_output"]["gated"] == "" and rows["read_tool_output"]["automatic"]


@pytest.mark.parametrize("node_id", ["_w.lead.template", "w.lead.example"])
def test_a_template_reports_the_merge_tool_a_copy_of_it_would_get(client, workspace: Path, node_id: str) -> None:
    # 模板、示例按 id 找不到，这一页照样解析它那份文件：照着它复制出去的节点拿到的就是这里报的
    _write_node(workspace, "w.coder", _ISOLATABLE_CODER)
    _write_node(workspace, node_id, """
type: ai
delegate_targets: [w.coder]
tool_access:
  mode: allowlist
  allow: [read_file]
""")

    rows = _rows(client, node_id)

    assert rows["read_file"]["gated"] == ""
    assert "w.coder" in rows["merge_worktree"]["automatic"]


def test_the_merge_tool_does_not_follow_the_checkbox(client, workspace: Path) -> None:
    _write_node(workspace, "w.coder", _ISOLATABLE_CODER)
    _write_node(workspace, "w.wide", "id: w.wide\ntype: ai\ntool_access: all\n")
    _write_node(workspace, "w.listed", """
id: w.listed
type: ai
tool_access:
  mode: allowlist
  allow: [read_file, merge_worktree]
""")
    _write_node(workspace, "w.denied", """
id: w.denied
type: ai
delegate_targets: [w.coder]
tool_access:
  mode: all
  deny: [merge_worktree]
""")
    _write_node(workspace, "w.both", """
id: w.both
type: ai
delegate_targets: [w.coder]
tool_access:
  mode: allowlist
  allow: [read_file, merge_worktree]
  deny: [merge_worktree]
""")

    # 全给或者写进白名单，没有能隔离的委派目标照样不给
    for node_id in ("w.wide", "w.listed"):
        merge = _rows(client, node_id)["merge_worktree"]
        assert merge["automatic"] == "" and "没有写 isolation: worktree" in merge["gated"], node_id
    assert "merge_worktree" not in _rows(client, "w.denied")
    both = _rows(client, "w.both")["merge_worktree"]
    assert both["automatic"] == "" and "deny" in both["gated"]


@pytest.mark.parametrize("allow", [
    pytest.param("[ghost_tool]", id="only-unregistered"),
    pytest.param("[edit_lines]", id="only-dropped"),
])
def test_no_reader_for_a_node_left_without_tools(client, workspace: Path, allow: str) -> None:
    _write_node(workspace, "w.empty", f"id: w.empty\ntype: ai\ntool_access:\n  mode: allowlist\n  allow: {allow}\n")

    assert "read_tool_output" not in _rows(client, "w.empty")
