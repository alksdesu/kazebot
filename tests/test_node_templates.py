"""模板和示例节点文件（_ 开头、带 .example）只给人复制、参考：运行期按 id 一律找不到，点名时说清楚为什么。

按 id 加载要是不看这条规则：委派清单里写上 qq.orchestrator.example 就真派过去跑示例，
新装实例的网页入口默认选中 _persistent_agent.template，用户的消息全由模板处理。
"""
from __future__ import annotations

import asyncio
import logging
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
from engine import runner  # noqa: E402
from engine.node import (  # noqa: E402
    canonical_node_id,
    example_base_name,
    example_node_name,
    known_node_ids,
    load_node,
    load_node_file,
    missing_node_reason,
    node_file,
    node_files,
    template_node_file,
    template_node_name,
    template_node_reason,
    template_reference_warnings,
)
from engine.runner import _collect_node_info, _discover_switchable_nodes  # noqa: E402
from shell.tui.services.node_scanner import find_root_nodes, scan_nodes  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-node-templates-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_FOLDERS = {
    "system": Path("engine") / "system_nodes",
    "config": Path("config") / "nodes",
    "defaults": Path("defaults") / "nodes",
}


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 仓库自带的 defaults/nodes 里就有模板和示例，不挡住会混进这个假工作区。
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)
    for folder in _FOLDERS.values():
        (tmp_path / folder).mkdir(parents=True)
    return tmp_path


def _node(workspace: Path, filename: str, *, folder: str = "config", **fields: Any) -> Path:
    path = workspace / _FOLDERS[folder] / f"{filename}.yaml"
    path.write_text(yaml.safe_dump({"type": "ai", **fields}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _raw(workspace: Path, filename: str, text: str) -> Path:
    path = workspace / _FOLDERS["config"] / f"{filename}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestTheNamingRule:
    @pytest.mark.parametrize("name, template", [
        ("_persistent_agent.template", True),
        ("qq.orchestrator.example", True),
        # Windows、macOS 上改了大小写的名字照样打开示例文件
        ("qq.orchestrator.EXAMPLE", True),
        ("qq.orchestrator.Example", True),
        ("qq.orchestrator", False),
        ("bootstrap.coder", False),
        # _example 结尾的不是示例：运行期照样当节点加载，节点文件页的标签也得跟着
        ("helper_example", False),
    ])
    def test_templates_start_with_an_underscore_and_examples_carry_dot_example(self, name: str, template: bool) -> None:
        assert template_node_name(name) is template

    @pytest.mark.parametrize("name, example, base", [
        ("qq.orchestrator.example", True, "qq.orchestrator"),
        ("_persona.EXAMPLE", True, "_persona"),
        ("_persistent_agent.template", False, "_persistent_agent.template"),
        ("helper_example", False, "helper_example"),
    ])
    def test_an_example_and_the_name_it_stands_for(self, name: str, example: bool, base: str) -> None:
        assert example_node_name(name) is example
        assert example_base_name(name) == base
        assert not example_node_name(example_base_name(name))


class TestLookupSkipsTemplates:
    @pytest.mark.parametrize("folder", ["config", "defaults"])
    @pytest.mark.parametrize("name", ["_agent.template", "qq.orchestrator.example"])
    def test_a_template_or_example_is_not_a_node_by_its_file_name(self, workspace: Path, folder: str, name: str) -> None:
        _node(workspace, name, folder=folder, id="qq.orchestrator")

        assert node_file(workspace, name) is None
        assert load_node(workspace, name) is None
        assert canonical_node_id(workspace, name) == name
        assert known_node_ids(workspace, name) == (name,)

    @pytest.mark.parametrize("asked", ["qq.orchestrator.EXAMPLE", "qq.orchestrator.Example"])
    def test_changing_the_case_does_not_get_around_it(self, workspace: Path, asked: str) -> None:
        _node(workspace, "qq.orchestrator.example")

        assert node_file(workspace, asked) is None
        assert load_node(workspace, asked) is None

    def test_an_old_id_shaped_like_a_template_finds_nothing(self, workspace: Path) -> None:
        _node(workspace, "chat.helper", id="chat.helper", aliases=["helper.example", "_helper", "helper.v1"])

        assert node_file(workspace, "helper.example") is None
        assert node_file(workspace, "_helper") is None
        assert load_node(workspace, "chat.helper").aliases == ["helper.v1"]
        assert known_node_ids(workspace, "chat.helper") == ("chat.helper", "helper.v1")

    def test_the_listing_follows_the_same_rule(self, workspace: Path) -> None:
        _node(workspace, "_agent.template", folder="defaults")
        _node(workspace, "alpha.example", folder="defaults")
        _node(workspace, "beta.EXAMPLE")
        alpha = _node(workspace, "alpha", folder="defaults")
        helper = _node(workspace, "helper_example")

        found = node_files(workspace)

        assert set(found) == {alpha, helper}
        assert all(not template_node_name(path.stem) for path in found)

    def test_the_file_itself_can_still_be_read_as_a_draft(self, workspace: Path) -> None:
        path = _node(workspace, "_agent.template", folder="defaults", name="长期 Agent", persistent=True)
        _node(workspace, "chat.helper")

        assert template_node_file(workspace, "_agent.template") == path
        assert template_node_file(workspace, "chat.helper") is None
        node = load_node_file(workspace, path)
        assert node is not None
        assert (node.name, node.persistent) == ("长期 Agent", True)


class TestDelegation:
    def test_templates_are_dropped_from_delegate_targets_and_logged_once(
        self, workspace: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _node(workspace, "chat.web_search", id="chat.web_search")
        _node(workspace, "qq.orchestrator.example", folder="defaults")
        _node(workspace, "_agent.template", folder="defaults")
        _node(
            workspace, "qq.orchestrator", id="qq.orchestrator",
            delegate_targets=["chat.web_search", "qq.orchestrator.example", "draw.not_installed", "_agent.template"],
        )

        with caplog.at_level(logging.WARNING, logger="engine.node"):
            first = load_node(workspace, "qq.orchestrator")
            second = load_node(workspace, "qq.orchestrator")

        # 还没装上的节点照留：配置分批上线时它很快就会出现
        assert first.delegate_targets == second.delegate_targets == ["chat.web_search", "draw.not_installed"]
        assert [record.getMessage() for record in caplog.records if "delegate_targets" in record.getMessage()] == [
            "qq.orchestrator.yaml lists qq.orchestrator.example in delegate_targets; "
            "templates and examples are never dispatched to, so it is left out",
            "qq.orchestrator.yaml lists _agent.template in delegate_targets; "
            "templates and examples are never dispatched to, so it is left out",
        ]

    def test_no_dispatch_tool_is_built_for_a_template(self, workspace: Path) -> None:
        _node(workspace, "chat.web_search", id="chat.web_search")
        _node(workspace, "qq.orchestrator.example", folder="defaults", id="qq.orchestrator")
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", delegate_targets=["chat.web_search", "qq.orchestrator.EXAMPLE"])

        node = load_node(workspace, "qq.orchestrator")

        assert [info["id"] for info in _collect_node_info(workspace, node.delegate_targets)] == ["chat.web_search"]

    def test_a_template_is_never_offered_as_a_switch_target(self, workspace: Path) -> None:
        _node(workspace, "_agent.template", folder="defaults", name="模板")
        _node(workspace, "scout.example", folder="defaults", name="示例")
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", name="主入口")

        listed = _discover_switchable_nodes(workspace, "chat.vision")

        assert [row["id"] for row in listed] == ["qq.orchestrator"]

    def test_nodes_that_do_not_take_conversations_are_never_offered(self, workspace: Path) -> None:
        # 系统节点没人委派，不挡的话个个都算根节点，模型的 switch_node 枚举里就会有 system.compactor
        _node(workspace, "system.compactor", folder="system", name="压缩")
        _node(workspace, "judge.intent", internal=True, name="判定")
        _node(workspace, "tools.runner", type="tool", name="工具")
        _node(workspace, "qq.orchestrator", id="qq.orchestrator", name="主入口")

        listed = _discover_switchable_nodes(workspace, "chat.vision")

        assert [row["id"] for row in listed] == ["qq.orchestrator"]
        assert [node.id for node in find_root_nodes(scan_nodes(workspace))] == ["qq.orchestrator"]

    def test_saving_a_definition_that_names_a_template_warns(self) -> None:
        warnings = template_reference_warnings({
            "delegate_targets": ["chat.web_search", "qq.orchestrator.example", "qq.orchestrator.example", "  "],
            "aliases": ["helper.v1", "_helper"],
        })

        assert warnings == [
            "delegate_targets 里的 qq.orchestrator.example 是模板或示例，不会派发过去",
            "aliases 里的 _helper 是模板或示例的名字，按它找不到这个节点",
        ]
        assert template_reference_warnings({"delegate_targets": "qq.orchestrator.example"}) == []


class TestTheReasonIsSpelledOut:
    def test_an_example_with_a_file_says_where_it_is_and_what_to_do(self, workspace: Path) -> None:
        _node(workspace, "qq.orchestrator.example", folder="defaults")

        reason = missing_node_reason(workspace, "qq.orchestrator.example")

        assert reason == template_node_reason(workspace, "qq.orchestrator.example")
        assert "qq.orchestrator.example 是模板或示例" in reason
        assert "defaults/nodes/qq.orchestrator.example.yaml" in reason
        assert "config/nodes/" in reason

    def test_a_template_name_without_a_file_names_the_rule(self, workspace: Path) -> None:
        reason = missing_node_reason(workspace, "_ghost")

        assert "_ghost 是模板或示例的名字" in reason and "不能以 _ 开头" in reason

    @pytest.mark.parametrize("text, problem", [
        ("type: [ai\n", "YAML 读不出来"),
        ("- one\n- two\n", "YAML 读不出来"),
        ("kind: skill\ntype: ai\n", "kind 写的是 skill"),
        ("type: robot\n", "type 写的是 robot"),
    ], ids=["broken-yaml", "not-a-mapping", "wrong-kind", "wrong-type"])
    def test_a_file_that_is_there_but_not_a_node_is_not_reported_missing(
        self, workspace: Path, text: str, problem: str,
    ) -> None:
        _raw(workspace, "scout", text)

        reason = missing_node_reason(workspace, "scout")

        assert load_node(workspace, "scout") is None
        assert "节点未找到" not in reason
        assert "config/nodes/scout.yaml" in reason and problem in reason

    def test_a_node_that_is_nowhere_is_still_not_found(self, workspace: Path) -> None:
        assert missing_node_reason(workspace, "nobody") == "节点未找到：nobody"
        assert template_node_reason(workspace, "nobody") == ""


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
        self.calls: list[str] = []

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.calls.append(name)
        return {"ok": True, "data": {"result": "done"}}

    def get_spec(self, name: str) -> None:
        return None


class TestTheRunnerSaysWhy:
    def test_an_entry_task_on_an_example_fails_before_touching_the_conversation(self, workspace: Path) -> None:
        _node(workspace, "qq.orchestrator.example", folder="defaults", id="qq.orchestrator", tool_access="all")

        result = asyncio.run(runner._run_node_task(
            http=None, llm_http=None, sup_url="http://unused", ws_root=workspace, registry=None,  # type: ignore[arg-type]
            task={"task_id": "t-1", "node_id": "qq.orchestrator.example", "input": {}},
            worker_id="worker-1", session_id="s-1", session_generation=1,
            api_key="", base_url="", default_model="",
        ))

        assert result["action"] == "fail"
        assert "qq.orchestrator.example 是模板或示例" in result["error"]
        assert not (workspace / "data" / "conversations").exists()

    def test_an_entry_task_on_a_broken_node_says_the_file_is_broken(self, workspace: Path) -> None:
        _raw(workspace, "scout", "type: [ai\n")

        result = asyncio.run(runner._run_node_task(
            http=None, llm_http=None, sup_url="http://unused", ws_root=workspace, registry=None,  # type: ignore[arg-type]
            task={"task_id": "t-2", "node_id": "scout", "input": {}},
            worker_id="worker-1", session_id="s-1", session_generation=1,
            api_key="", base_url="", default_model="",
        ))

        assert result["action"] == "fail"
        assert "config/nodes/scout.yaml" in result["error"] and "节点未找到" not in result["error"]

    @pytest.mark.parametrize("origin, why", [
        ("_agent.template", "发起节点用不了：_agent.template 是模板或示例"),
        ("", "没有指定发起节点"),
    ], ids=["template", "unspecified"])
    def test_a_tool_task_from_a_template_never_reaches_the_tool(self, workspace: Path, origin: str, why: str) -> None:
        _node(workspace, "_agent.template", folder="defaults", tool_access="all")
        registry = _CapturingRegistry()

        result = asyncio.run(runner._run_tool_task(
            http=_Http(), sup_url="http://sup", ws_root=workspace, registry=registry,  # type: ignore[arg-type]
            task={"task_id": "t-3", "tool_name": "demo", "input": {"origin_node_id": origin, "arguments": {}}},
            worker_id="worker-1", session_id="s-1", session_generation=1, task_id="t-3",
        ))

        assert result["action"] == "error"
        assert result["summary"].startswith(f"拒绝执行：{why}")
        assert registry.calls == []


@pytest.fixture
def supervisor_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-node-templates"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    admin_api._auth_failures.clear()
    yield state, call
    admin_api._auth_failures.clear()


class TestTheSwitchEndpoint:
    @pytest.mark.parametrize("target", ["qq.orchestrator.example", "qq.orchestrator.EXAMPLE", "_persistent_agent.template"])
    def test_switching_to_a_template_is_refused_with_the_reason(self, supervisor_app: Any, target: str) -> None:
        state, call = supervisor_app
        session_id = state.get_or_create_session(channel="chat", conversation_key="chat:templates")

        response = call("POST", f"/v1/sessions/{session_id}/switch_node", json={"target_node_id": target})

        assert response.status_code == 400
        assert f"{target} 是模板或示例" in response.json()["detail"]
        assert session_id not in state.session_entry_overrides
        assert state.sessions[session_id].entry_node_id == ""

    def test_an_unknown_session_is_a_404_not_an_ok_false(self, supervisor_app: Any) -> None:
        # engine 只看状态码：200 加 ok:false 会被当成切换成功，模型就此结束本轮
        _state, call = supervisor_app

        response = call("POST", "/v1/sessions/no-such-session/switch_node", json={"target_node_id": "chat.vision"})

        assert response.status_code == 404
        assert response.json()["detail"] == "session not found"

    def test_a_real_node_still_switches(self, supervisor_app: Any) -> None:
        state, call = supervisor_app
        session_id = state.get_or_create_session(channel="chat", conversation_key="chat:templates")

        response = call("POST", f"/v1/sessions/{session_id}/switch_node", json={"target_node_id": "chat.vision"})

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert state.session_entry_overrides[session_id] == "chat.vision"

    @pytest.mark.parametrize("target, why", [
        ("nobody", "节点未找到：nobody"),
        ("system.compactor", "system.compactor 是系统节点"),
        ("judge.intent", "judge.intent 是 internal 节点"),
        ("tools.runner", "tools.runner 是工具节点"),
    ], ids=["missing", "system", "internal", "tool"])
    def test_switching_to_a_node_that_cannot_take_the_conversation_is_refused(
        self, supervisor_app: Any, target: str, why: str,
    ) -> None:
        # 切过去之后这个会话的每条消息都由它接：不当场拒，之后每条都会失败
        state, call = supervisor_app
        root = state.workspace_root
        (root / "engine" / "system_nodes").mkdir(parents=True)
        (root / "engine" / "system_nodes" / "system.compactor.yaml").write_text("type: ai\n", encoding="utf-8")
        (root / "config" / "nodes" / "judge.intent.yaml").write_text("type: ai\ninternal: true\n", encoding="utf-8")
        (root / "config" / "nodes" / "tools.runner.yaml").write_text("type: tool\n", encoding="utf-8")
        session_id = state.get_or_create_session(channel="chat", conversation_key="chat:entry")

        response = call("POST", f"/v1/sessions/{session_id}/switch_node", json={"target_node_id": target})

        assert response.status_code == 400
        assert why in response.json()["detail"]
        assert session_id not in state.session_entry_overrides
        assert state.sessions[session_id].entry_node_id == ""

    def test_an_old_id_is_kept_as_the_current_one_and_the_default_is_reported(self, supervisor_app: Any) -> None:
        state, call = supervisor_app
        (state.workspace_root / "config" / "nodes" / "chat.helper.yaml").write_text(
            "type: ai\naliases: [helper.v1]\n", encoding="utf-8",
        )
        session_id = state.get_or_create_session(channel="chat", conversation_key="chat:alias")

        response = call("POST", f"/v1/sessions/{session_id}/switch_node", json={"target_node_id": "helper.v1"})
        active = call("GET", f"/v1/sessions/{session_id}/active_node")

        assert response.status_code == 200, response.text
        assert state.session_entry_overrides[session_id] == "chat.helper"
        # 网页切完拿它记默认节点，缺了会被记成空串
        assert response.json()["default_node_id"] == active.json()["default_node_id"] != ""
