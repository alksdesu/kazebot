"""节点的身份是文件名，不是 yaml 里那个 id 字段。

engine 的 load_node 拿 node_id 直接拼 {node_id}.yaml 找文件，yaml 里的 id 它不读。
管理接口一度按 id 字段去重，于是 qq.orchestrator.example.yaml 顶掉了同名的
qq.orchestrator.yaml：列表显示示例文件的内容，保存却按文件名写回真文件——
在那个界面上点一次保存，主入口节点的授权就被示例文件的内容覆盖了。
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

_REAL = """id: qq.orchestrator
type: ai
tool_access:
  mode: allowlist
  allow:
    - read_file
    - write_file
    - execute_command
"""

# 示例文件也自称 qq.orchestrator —— 正是撞车的那一份。
_EXAMPLE = """id: qq.orchestrator
type: ai
tool_access:
  mode: allowlist
  allow:
    - read_file
"""

_SYSTEM = """id: system.compactor
type: ai
tool_access:
  mode: none
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    (tmp_path / "engine" / "system_nodes").mkdir(parents=True)
    (tmp_path / "config" / "nodes" / "qq.orchestrator.yaml").write_text(_REAL, encoding="utf-8")
    (tmp_path / "config" / "nodes" / "qq.orchestrator.example.yaml").write_text(_EXAMPLE, encoding="utf-8")
    (tmp_path / "engine" / "system_nodes" / "system.compactor.yaml").write_text(_SYSTEM, encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-node-identity"),
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


def _nodes(client) -> dict[str, dict]:
    return {row["id"]: row for row in client("GET", _BASE, headers=_AUTH).json()}


def test_a_sample_file_no_longer_hides_the_real_node(client) -> None:
    rows = _nodes(client)

    assert "qq.orchestrator" in rows
    assert "qq.orchestrator.example" in rows
    # 真节点那一份是三个工具的；示例只有一个。顶掉了就会看到 1。
    assert len(rows["qq.orchestrator"]["tool_access"]["allow"]) == 3


def test_what_the_list_shows_is_what_saving_writes_to(client, workspace: Path) -> None:
    """读一份、写另一份是最坏的情况：界面看着没问题，保存就把真配置覆盖了。"""
    listed = _nodes(client)["qq.orchestrator"]["tool_access"]["allow"]

    raw = client("GET", f"{_BASE}/qq.orchestrator/raw", headers=_AUTH).json()["content"]

    for name in listed:
        assert name in raw
    assert raw == (workspace / "config" / "nodes" / "qq.orchestrator.yaml").read_text(encoding="utf-8")


def test_sample_and_template_files_are_flagged_inactive(client) -> None:
    rows = _nodes(client)

    assert rows["qq.orchestrator"]["active"] is True
    assert rows["qq.orchestrator.example"]["active"] is False


def test_an_id_field_that_disagrees_with_the_filename_is_reported(client) -> None:
    # engine 认文件名，那个 id 字段改了也不换节点 —— 界面得说出来。
    rows = _nodes(client)

    assert rows["qq.orchestrator.example"]["declared_id"] == "qq.orchestrator"
    assert rows["qq.orchestrator"]["declared_id"] == ""


def test_a_system_node_opens_instead_of_404ing(client) -> None:
    resp = client("GET", f"{_BASE}/system.compactor/raw", headers=_AUTH)

    assert resp.status_code == 200
    assert "system.compactor" in resp.json()["content"]


def test_saving_a_system_node_is_refused_not_silently_shadowed(client, workspace: Path) -> None:
    # 落到 config/nodes/ 下的同名文件永远不会被加载，改完看着成功，实际没变。
    resp = client("PUT", f"{_BASE}/system.compactor/raw", headers=_AUTH, json={"content": "id: x\n"})

    assert resp.status_code == 409
    assert not (workspace / "config" / "nodes" / "system.compactor.yaml").exists()


def test_deleting_a_system_node_is_refused(client) -> None:
    assert client("DELETE", f"{_BASE}/system.compactor", headers=_AUTH).status_code == 409


def test_deleting_a_node_that_does_not_exist_is_a_404(client) -> None:
    assert client("DELETE", f"{_BASE}/nope", headers=_AUTH).status_code == 404


def test_effective_tools_reads_the_real_file_too(client) -> None:
    body = client("GET", f"{_BASE}/qq.orchestrator/effective-tools", headers=_AUTH).json()

    assert {row["name"] for row in body["tools"]} == {"read_file", "write_file", "execute_command"}
