"""节点 YAML 的 selectable：写了就以它为准列不列进入口列表，没写照委派关系推断。

switch_node 的枚举、终端界面和控制台节点表读出同一个结论；它只管列不列，节点照样能当入口。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from engine.node import entry_node_problem, load_node  # noqa: E402
from engine.runner import _discover_switchable_nodes  # noqa: E402
from shell.tui.services.node_scanner import find_root_nodes, scan_nodes  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 挡住仓库自带的 defaults/nodes，节点表里只剩用例自己写的
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)
    config_layout.nodes_dir(tmp_path).mkdir(parents=True)
    return tmp_path


def _node(workspace: Path, name: str, body: str = "") -> None:
    path = config_layout.nodes_dir(workspace) / f"{name}.yaml"
    path.write_text(f"type: ai\nprompt: hi\n{body}", encoding="utf-8")


def _switchable(workspace: Path) -> list[str]:
    return [row["id"] for row in _discover_switchable_nodes(workspace, "someone.else")]


def _terminal(workspace: Path) -> list[str]:
    return [node.id for node in find_root_nodes(scan_nodes(workspace))]


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-node-selectable"),
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


class TestListing:
    def test_a_delegated_node_marked_selectable_is_still_listed(self, workspace: Path) -> None:
        _node(workspace, "boss", "delegate_targets: [coder, reviewer]\n")
        _node(workspace, "coder", "selectable: true\ndelegate_targets: [reviewer]\n")
        _node(workspace, "reviewer")

        assert _switchable(workspace) == _terminal(workspace) == ["boss", "coder"]

    def test_a_root_marked_not_selectable_is_left_out_but_still_takes_conversations(self, workspace: Path) -> None:
        _node(workspace, "main")
        _node(workspace, "vision", "selectable: false\n")

        assert _switchable(workspace) == _terminal(workspace) == ["main"]
        vision = load_node(workspace, "vision")
        assert vision is not None and entry_node_problem(vision) == ""

    def test_without_it_the_delegation_rule_still_decides(self, workspace: Path) -> None:
        _node(workspace, "boss", "delegate_targets: [worker]\n")
        _node(workspace, "worker")

        assert _switchable(workspace) == _terminal(workspace) == ["boss"]

    def test_when_every_node_is_delegated_the_fallback_still_leaves_out_the_hidden_ones(self, workspace: Path) -> None:
        _node(workspace, "a", "delegate_targets: [b]\n")
        _node(workspace, "b", "delegate_targets: [a]\n")
        _node(workspace, "hidden", "selectable: false\n")

        assert _switchable(workspace) == _terminal(workspace) == ["a", "b"]

    def test_the_current_node_is_never_offered_to_itself(self, workspace: Path) -> None:
        _node(workspace, "main", "delegate_targets: [coder]\n")
        _node(workspace, "coder", "selectable: true\n")

        assert [row["id"] for row in _discover_switchable_nodes(workspace, "coder")] == ["main"]


class TestStoredValues:
    @pytest.mark.parametrize("raw,expected", [
        ('"false"', False),
        ("no", False),
        ('"yes"', True),
        ("true", True),
        ('"maybe"', None),
        ("null", None),
    ])
    def test_the_engine_the_terminal_and_the_console_read_the_same_value(
        self, workspace: Path, client: Any, raw: str, expected: bool | None,
    ) -> None:
        _node(workspace, "boss", "delegate_targets: [worker]\n")
        _node(workspace, "worker", f"selectable: {raw}\n")

        worker = load_node(workspace, "worker")
        rows = {row["id"]: row for row in client("GET", "/v1/admin/config/nodes", headers=_AUTH).json()}
        listed = expected is True

        assert worker is not None and worker.selectable is expected
        assert rows["worker"]["selectable"] is expected
        assert rows["boss"]["selectable"] is None
        assert ("worker" in _switchable(workspace)) is listed
        assert ("worker" in _terminal(workspace)) is listed


class TestConsoleNodeTable:
    def test_delegate_targets_come_back_the_way_the_engine_reads_them(self, workspace: Path, client: Any) -> None:
        # 网页按这份算入口下拉：旧 id 要换成现在的，模板、示例和重复的不算，还没建的节点照留
        _node(workspace, "boss", "delegate_targets: [old.coder, coder, _helper.template, scout.example, ghost, coder]\n")
        _node(workspace, "coder", "aliases: [old.coder]\n")

        rows = {row["id"]: row for row in client("GET", "/v1/admin/config/nodes", headers=_AUTH).json()}

        assert rows["boss"]["delegate_targets"] == ["coder", "ghost"]
        assert load_node(workspace, "boss").delegate_targets == ["coder", "ghost"]
