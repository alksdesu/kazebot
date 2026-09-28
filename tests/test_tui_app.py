"""TUI 真跑起来时，shell.tui.* 的每一项都要落到界面上。

用 Textual 的 headless pilot 驱动：supervisor 客户端和事件轮询换成桩，其余全是真组件，
主题、限高 CSS 变量、思考区开关、节点面板都按真实挂载路径检查。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from textual.css.query import NoMatches  # noqa: E402
from textual.widgets import Static  # noqa: E402

import config_layout  # noqa: E402
import shell.tui.app as app_module  # noqa: E402
from shell.tui.app import ClonothApp  # noqa: E402
from shell.tui.models import NodeCompleted, NodeStarted, StreamThinking  # noqa: E402
from shell.tui.services.event_poller import EventPoller  # noqa: E402
from shell.tui.settings import TuiSettings  # noqa: E402
from shell.tui.widgets.message_list import (  # noqa: E402
    CAPPED_CLASS,
    MAX_HEIGHT_VARIABLE,
    MessageList,
)
from shell.tui.widgets.node_panel import NodePanel  # noqa: E402
from shell.tui.widgets.thinking import ThinkingIndicator  # noqa: E402


class _StubSupervisor:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def wait_ready(self, timeout: float = 0.0) -> bool:
        return True

    async def fetch_model_name(self) -> str:
        return "stub-model"

    async def fetch_latest_global_seq(self) -> int:
        return 0

    async def close(self) -> None:
        self.closed = True


class _StubPoller:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.started: list[int] = []
        self.stopped_sessions = 0

    def start(self, global_seq: int = 0) -> None:
        self.started.append(global_seq)

    def start_session(self, *args: Any, **kwargs: Any) -> None: ...

    def stop_session(self) -> None:
        self.stopped_sessions += 1

    def stop(self) -> None: ...


@pytest.fixture()
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "SupervisorClient", _StubSupervisor)
    monkeypatch.setattr(app_module, "EventPoller", _StubPoller)
    # 仓库自带的 defaults/nodes 在代码目录下，不跟着工作区走：指到这里才是真的空工作区。
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)

    def _factory(settings: TuiSettings | None = None) -> ClonothApp:
        # 空工作区：节点扫描找不到入口，直接进聊天界面，不弹节点选择遮罩
        return ClonothApp(workspace_root=tmp_path, settings=settings or TuiSettings())

    return _factory


@pytest.mark.asyncio
async def test_a_known_theme_is_applied_on_mount(make_app) -> None:
    app = make_app(TuiSettings(theme="nord"))

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.theme == "nord"
        assert list(app._notifications) == []


@pytest.mark.asyncio
async def test_an_unknown_theme_keeps_the_default_and_warns(make_app) -> None:
    app = make_app(TuiSettings(theme="not-a-theme"))

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.theme == "textual-dark"
        notes = [(n.message, n.severity) for n in app._notifications]
        assert len(notes) == 1
        assert "not-a-theme" in notes[0][0]
        assert notes[0][1] == "warning"


@pytest.mark.asyncio
async def test_capped_bodies_carry_the_class_and_resolve_to_the_configured_height(make_app) -> None:
    app = make_app(TuiSettings(max_message_height=20))

    async with app.run_test() as pilot:
        await pilot.pause()
        message_list = app.query_one(MessageList)
        await message_list.add_user_message("hello")
        await message_list.add_assistant_message("plain reply")
        await pilot.pause()

        capped = list(message_list.query(f".{CAPPED_CLASS}"))
        assert {frozenset(widget.classes) for widget in capped} == {
            frozenset({CAPPED_CLASS, "user-msg"}),
            frozenset({CAPPED_CLASS, "assistant-msg"}),
        }
        assert [int(widget.styles.max_height.value) for widget in capped] == [20, 20]
        assert app.get_theme_variable_defaults()[MAX_HEIGHT_VARIABLE] == "20"


@pytest.mark.asyncio
async def test_height_zero_leaves_message_bodies_unbounded(make_app) -> None:
    app = make_app(TuiSettings(max_message_height=0))

    async with app.run_test() as pilot:
        await pilot.pause()
        message_list = app.query_one(MessageList)
        await message_list.add_user_message("hello")
        await pilot.pause()

        assert list(message_list.query(f".{CAPPED_CLASS}")) == []
        assert app.query_one(".user-msg").styles.max_height is None
        # 变量仍要是个合法长度，否则整张样式表解析失败
        assert app.get_theme_variable_defaults()[MAX_HEIGHT_VARIABLE] == "1"


@pytest.mark.asyncio
async def test_thinking_is_shown_when_enabled(make_app) -> None:
    app = make_app(TuiSettings(show_thinking=True))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(StreamThinking("在想事情"))
        await pilot.pause()

        assert app.query_one(ThinkingIndicator).has_class("visible") is True
        assert app._thinking_buf == "在想事情"


@pytest.mark.asyncio
async def test_thinking_stays_hidden_when_disabled(make_app) -> None:
    app = make_app(TuiSettings(show_thinking=False))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(StreamThinking("在想事情"))
        await pilot.pause()

        assert app.query_one(ThinkingIndicator).has_class("visible") is False
        assert app._thinking_buf == ""


@pytest.mark.asyncio
async def test_without_the_node_panel_the_message_list_stands_alone(make_app) -> None:
    app = make_app(TuiSettings(node_panel=False))

    async with app.run_test() as pilot:
        await pilot.pause()

        assert list(app.query(NodePanel)) == []
        with pytest.raises(NoMatches):
            app.query_one("#chat-row")
        assert app.query_one(MessageList) is not None


@pytest.mark.asyncio
async def test_the_node_panel_sits_next_to_the_message_list(make_app) -> None:
    app = make_app(TuiSettings(node_panel=True))

    async with app.run_test() as pilot:
        await pilot.pause()

        row = app.query_one("#chat-row")
        assert [type(child).__name__ for child in row.children] == ["MessageList", "NodePanel"]


@pytest.mark.asyncio
async def test_node_events_fill_the_panel_and_mark_failures(make_app) -> None:
    app = make_app(TuiSettings(node_panel=True))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(NodeStarted("Scout", "bootstrap.scout"))
        app.post_message(NodeStarted("Smith", "bootstrap.smith"))
        await pilot.pause()

        running = list(app.query_one(NodePanel).query(Static))
        assert [str(entry.content) for entry in running] == ["▶ Scout", "▶ Smith"]
        assert all(entry.has_class("node-running") for entry in running)

        app.post_message(NodeCompleted("Scout", "finish", "找到了", node_id="bootstrap.scout"))
        app.post_message(NodeCompleted("Smith", "fail", "挂了", node_id="bootstrap.smith"))
        await pilot.pause()

        entries = list(app.query_one(NodePanel).query(Static))
        # 完成是就地改写那一行，不是再追加一行
        assert entries == running
        assert [str(entry.content) for entry in entries] == [
            "✓ Scout → finish: 找到了",
            "✗ Smith → fail: 挂了",
        ]
        assert entries[0].has_class("node-completed") is True
        assert entries[0].has_class("node-running") is False
        assert entries[1].has_class("node-failed") is True
        assert entries[1].has_class("node-running") is False


@pytest.mark.asyncio
async def test_a_completion_without_a_start_still_gets_a_line(make_app) -> None:
    """启动事件可能被上一次 /clear 清掉；完成事件不能因此静默丢失。"""
    app = make_app(TuiSettings(node_panel=True))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(NodeCompleted("Scout", "finish", "x" * 80, node_id="bootstrap.scout"))
        await pilot.pause()

        entries = list(app.query_one(NodePanel).query(Static))
        assert len(entries) == 1
        assert str(entries[0].content) == f"✓ Scout → finish: {'x' * 40}"
        assert entries[0].has_class("node-completed") is True


@pytest.mark.asyncio
async def test_a_new_conversation_empties_the_panel(make_app) -> None:
    app = make_app(TuiSettings(node_panel=True))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(NodeStarted("Scout", "bootstrap.scout"))
        await pilot.pause()
        assert len(app.query_one(NodePanel).children) == 1

        app._start_new_conversation()
        await pilot.pause()

        assert list(app.query_one(NodePanel).children) == []
        assert list(app.query_one(MessageList).children) == []


def test_node_panel_does_not_shadow_the_textual_children_list() -> None:
    """Textual 用 DOMNode._nodes 存子组件，面板拿同名属性记条目会把挂载、查询和拆卸全打断。"""
    from textual._node_list import NodeList

    panel = NodePanel()

    assert isinstance(panel._nodes, NodeList)
    assert panel._entries == {}


def _parse(event: dict[str, Any]) -> Any:
    return EventPoller(client=None, app=None)._parse_event(event)


def test_node_completed_takes_its_outcome_from_action() -> None:
    """engine 写的是 action；读 outcome 会拿到空串，界面上每个节点都显示成 "→ :"。"""
    message = _parse({
        "type": "node_completed",
        "payload": {
            "node_id": "bootstrap.scout",
            "node_name": "Scout",
            "action": "finish",
            "summary": "找到了",
        },
    })

    assert isinstance(message, NodeCompleted)
    assert (message.name, message.outcome, message.summary, message.node_id) == (
        "Scout", "finish", "找到了", "bootstrap.scout",
    )


def test_node_completed_falls_back_to_the_node_id_for_its_label() -> None:
    message = _parse({"type": "node_completed", "payload": {"node_id": "bootstrap.scout"}})

    assert (message.name, message.outcome, message.node_id) == ("bootstrap.scout", "", "bootstrap.scout")


def test_a_stray_outcome_field_is_ignored() -> None:
    message = _parse({
        "type": "node_completed",
        "payload": {"node_id": "n", "action": "fail", "outcome": "finish"},
    })

    assert message.outcome == "fail"


def test_node_started_still_carries_name_and_id() -> None:
    message = _parse({"type": "node_started", "payload": {"node_id": "n", "node_name": "N"}})

    assert isinstance(message, NodeStarted)
    assert (message.name, message.node_id) == ("N", "n")


@pytest.mark.parametrize("module_path", [
    "shell/tui/services/event_poller.py",
    "shell/cli.py",
])
def test_no_node_completed_reader_still_looks_for_outcome(module_path: str) -> None:
    """两个消费端都要读 action；漏改一个就只有那一侧的节点行是空的。"""
    text = (_ROOT / module_path).read_text(encoding="utf-8")

    assert 'payload.get("outcome"' not in text
    assert 'payload.get("action"' in text
