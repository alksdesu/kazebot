"""节点的身份是文件名，不是 yaml 里那个 id 字段。

engine 的 load_node 拿 node_id 直接拼 {node_id}.yaml 找文件，yaml 里的 id 它不读。
管理接口一度按 id 字段去重，于是 qq.orchestrator.example.yaml 顶掉了同名的
qq.orchestrator.yaml：列表显示示例文件的内容，保存却按文件名写回真文件——
在那个界面上点一次保存，主入口节点的授权就被示例文件的内容覆盖了。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
from engine.node import (  # noqa: E402
    canonical_node_id,
    declared_id,
    internal_node,
    known_node_ids,
    load_node,
    template_reference_warnings,
)
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
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
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


def test_the_flag_follows_the_runtime_rule_in_any_case(client, workspace: Path) -> None:
    # Windows、macOS 上改了大小写的名字照样打开示例文件，运行期一样不当节点
    (workspace / "config" / "nodes" / "scout.EXAMPLE.yaml").write_text("type: ai\n", encoding="utf-8")
    (workspace / "config" / "nodes" / "_draft.yaml").write_text("type: ai\n", encoding="utf-8")
    (workspace / "config" / "nodes" / "helper_example.yaml").write_text("type: ai\n", encoding="utf-8")

    rows = _nodes(client)

    assert rows["scout.EXAMPLE"]["active"] is False
    assert rows["_draft"]["active"] is False
    assert rows["helper_example"]["active"] is True
    for node_id, row in rows.items():
        assert row["active"] is (load_node(workspace, node_id) is not None), node_id


@pytest.mark.parametrize("written, internal", [
    ("true", True), ('"yes"', True), ('"no"', False), ("false", False), (None, False),
], ids=["true", "yes", "no", "false", "unset"])
def test_the_list_says_which_nodes_are_internal(client, workspace: Path, written: str | None, internal: bool) -> None:
    # 网页的入口和切换列表按它过滤，和 switch_node 枚举、终端界面是同一个判定
    body = "type: ai\n" if written is None else f"type: ai\ninternal: {written}\n"
    (workspace / "config" / "nodes" / "chat.intent.yaml").write_text(body, encoding="utf-8")

    row = _nodes(client)["chat.intent"]

    assert row["internal"] is internal
    assert internal_node(load_node(workspace, "chat.intent")) is internal


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

    # read_tool_output 不在白名单里，引擎给有别的工具的节点自动加上
    assert {row["name"] for row in body["tools"]} == {"read_file", "write_file", "execute_command", "read_tool_output"}


@pytest.mark.parametrize("taken", ["system.compactor", "qq.vision"])
def test_creating_a_node_under_a_taken_id_is_refused(client, workspace: Path, taken: str) -> None:
    # 同名系统节点、把它声明成旧 id 的节点都排在新文件前面：建了看着成功，实际永远加载不到。
    (workspace / "config" / "nodes" / "chat.vision.yaml").write_text(
        "id: chat.vision\ntype: ai\naliases:\n  - qq.vision\n", encoding="utf-8",
    )

    resp = client("POST", _BASE, headers=_AUTH, json={"id": taken, "content": "type: ai\n"})

    assert resp.status_code == 409
    assert not (workspace / "config" / "nodes" / f"{taken}.yaml").exists()


def test_creating_a_node_under_a_free_id_still_works(client, workspace: Path) -> None:
    resp = client("POST", _BASE, headers=_AUTH, json={"id": "chat.helper", "content": "type: ai\n"})

    assert resp.status_code == 200
    assert (workspace / "config" / "nodes" / "chat.helper.yaml").read_text(encoding="utf-8") == "type: ai\n"


def test_hand_written_fields_come_back_the_way_the_engine_reads_them(client, workspace: Path) -> None:
    """留空的 model 原样给出去是 null，写成一个名字的 delegate_targets 会被拆成单个字母；engine 读的是规整后的值。"""
    (workspace / "config" / "nodes" / "sloppy.yaml").write_text(
        "type: ai\nname: 123\nmodel:\ndescription:\ndelegate_targets: helper\n", encoding="utf-8",
    )
    (workspace / "config" / "nodes" / "listed.yaml").write_text(
        "type: ai\ndelegate_targets:\n  - \" helper \"\n  - 1\n  - helper\n  - \"\"\n", encoding="utf-8",
    )

    rows = _nodes(client)

    assert rows["sloppy"]["name"] == "123"
    assert rows["sloppy"]["model"] == ""
    assert rows["sloppy"]["description"] == ""
    assert rows["sloppy"]["delegate_targets"] == []
    assert rows["listed"]["delegate_targets"] == ["helper"]


# 文件名是 probe.lead，yaml 里写的 id 却是 probe.lead.v2，委派名单里列的是文件名
_RENAMED = """id: probe.lead.v2
type: ai
delegate_targets:
  - probe.lead
"""


def _write_renamed(workspace: Path, content: str = _RENAMED) -> Path:
    path = workspace / "config" / "nodes" / "probe.lead.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_a_node_s_id_is_its_file_name_and_the_written_id_an_old_one(workspace: Path) -> None:
    _write_renamed(workspace)

    node = load_node(workspace, "probe.lead")
    by_old_id = load_node(workspace, "probe.lead.v2")

    assert node is not None and node.id == "probe.lead"
    assert by_old_id is not None and by_old_id.id == "probe.lead"
    assert node.aliases[0] == "probe.lead.v2"
    assert node.delegate_targets == ["probe.lead"]
    # 会话里存的入口、定时任务、技能的 node_ids 写着旧 id 的，都按它找回这个节点
    assert canonical_node_id(workspace, "probe.lead.v2") == "probe.lead"
    assert known_node_ids(workspace, "probe.lead") == ("probe.lead", "probe.lead.v2")


def test_the_written_id_is_warned_about_once_per_version_of_the_file(
    workspace: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write_renamed(workspace)

    def warned() -> int:
        return sum("declares id probe.lead.v2" in record.getMessage() for record in caplog.records)

    with caplog.at_level(logging.WARNING):
        load_node(workspace, "probe.lead")
        load_node(workspace, "probe.lead.v2")
        assert warned() == 1
        path.write_text(_RENAMED + "description: edited\n", encoding="utf-8")
        load_node(workspace, "probe.lead")

    assert warned() == 2


def test_a_written_id_that_names_another_file_stays_with_that_file(workspace: Path) -> None:
    nodes = workspace / "config" / "nodes"
    (nodes / "probe.a.yaml").write_text("id: probe.b\ntype: ai\n", encoding="utf-8")
    (nodes / "probe.b.yaml").write_text("type: ai\n", encoding="utf-8")

    assert load_node(workspace, "probe.a").id == "probe.a"
    assert load_node(workspace, "probe.b").id == "probe.b"
    assert known_node_ids(workspace, "probe.a") == ("probe.a",)
    assert canonical_node_id(workspace, "probe.b") == "probe.b"


def test_a_written_id_that_matches_the_file_name_is_no_old_id(workspace: Path) -> None:
    (workspace / "config" / "nodes" / "probe.same.yaml").write_text("id: probe.same\ntype: ai\n", encoding="utf-8")

    assert declared_id({"id": "probe.same"}, "probe.same") == ""
    assert known_node_ids(workspace, "probe.same") == ("probe.same",)


@pytest.mark.parametrize("raw", [None, "", "  ", True, ["probe.x"], {"id": "probe.x"}, "a/b", "..", "C:x"])
def test_an_id_that_is_not_a_node_name_is_ignored(raw: Any) -> None:
    assert declared_id({"id": raw}, "probe.lead") == ""


def test_a_template_name_written_as_the_id_is_reported_on_save() -> None:
    warnings = template_reference_warnings({"id": "_draft", "type": "ai"})

    assert "id 写的 _draft 是模板或示例的名字，按它找不到这个节点" in warnings


def test_the_console_lists_the_written_id_as_an_old_one_and_guards_it(client, workspace: Path) -> None:
    _write_renamed(workspace)

    row = _nodes(client)["probe.lead"]
    created = client("POST", _BASE, headers=_AUTH, json={"id": "probe.lead.v2", "content": "type: ai\n"})

    assert row["declared_id"] == "probe.lead.v2"
    assert row["aliases"] == ["probe.lead.v2"]
    assert created.status_code == 409
    assert not (workspace / "config" / "nodes" / "probe.lead.v2.yaml").exists()
