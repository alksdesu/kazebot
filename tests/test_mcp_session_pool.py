"""MCP 会话池：按对话分作用域借出会话，占用时另开，空闲回收，出错丢弃，退役和退出时关闭；share_sessions 让所有对话共用。

建连层（mcp_runtime._connect，或者它下面的 SDK 传输和 ClientSession）一律换成假的，不起真实的 MCP 服务。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import yaml
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
import toolbox.registry as registry_module  # noqa: E402
from clonoth_runtime import SwitchValueError, switch_refusal  # noqa: E402
from toolbox.builtins.mcp_clients import create_or_update_mcp_client, delete_mcp_client, list_mcp_clients  # noqa: E402
from toolbox.builtins.tool_manage import reload_tools  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.redaction import HIDDEN  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.reload_seqs import ReloadSeqs  # noqa: E402

_SPEC = {"id": "fs", "transport": "stdio", "command": "fake-mcp-server", "args": ["--stdio"]}


def _text_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


class _FakeSession:
    def __init__(self, server: _FakeServer, index: int, plan: Any = None) -> None:
        self.server = server
        self.index = index
        self.plan = plan
        self.loop = asyncio.get_running_loop()
        self.requests: list[str] = []
        self.closed = False
        self.read_stream: Any = None
        self.write_stream: Any = None

    async def list_tools(self) -> Any:
        self.requests.append("tools/list")
        if self.server.list_handler is not None:
            await self.server.list_handler(self)
        return mcp_types.ListToolsResult(tools=list(self.server.tools))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.requests.append(name)
        if self.server.handler is not None:
            return await self.server.handler(self, name, dict(arguments or {}))
        return _text_result(f"{name}@{self.index}")


class _FakeServer:
    """替掉建连层：记下每次建连、关闭和收到的请求，可以让建连失败、让请求或列工具表卡住，或让关闭卡在 close_gate 上。"""

    def __init__(self) -> None:
        self.connects = 0
        self.closes = 0
        self.events: list[str] = []
        self.plans: list[Any] = []
        self.sessions: list[_FakeSession] = []
        self.fail_connects = 0
        self.tools: list[mcp_types.Tool] = []
        self.handler: Any = None
        self.list_handler: Any = None
        self.close_gate: asyncio.Event | None = None

    @asynccontextmanager
    async def connect(self, plan: Any):
        self.connects += 1
        self.plans.append(plan)
        if self.fail_connects > 0:
            self.fail_connects -= 1
            self.events.append("refused")
            raise ConnectionRefusedError("fake server refused the connection")
        session = _FakeSession(self, len(self.sessions), plan)
        self.sessions.append(session)
        self.events.append(f"open {session.index}")
        send, receive = anyio.create_memory_object_stream(0)
        session.read_stream, session.write_stream = receive, send
        try:
            yield mcp_runtime._Connection(session=session, read_stream=receive, write_stream=send)
        finally:
            if self.close_gate is not None:
                await self.close_gate.wait()
            session.closed = True
            self.closes += 1
            self.events.append(f"close {session.index}")
            send.close()
            receive.close()


def _recording_client_session(seen: dict[str, Any]) -> type:
    """替掉 SDK 的 ClientSession：记下构造参数和请求参数，initialize / list_tools / call_tool 直接应答。"""

    class _RecordingClientSession:
        def __init__(self, read_stream: Any, write_stream: Any, *args: Any, **kwargs: Any) -> None:
            seen["session_args"] = args
            seen["session_kwargs"] = kwargs

        async def __aenter__(self) -> _RecordingClientSession:
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            seen["exited"] = True

        async def initialize(self) -> None:
            seen["initialized"] = True

        async def list_tools(self) -> Any:
            return mcp_types.ListToolsResult(tools=[])

        async def call_tool(self, name: str, *args: Any, **kwargs: Any) -> Any:
            seen.setdefault("calls", []).append((name, args, kwargs))
            return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="ok")])

    return _RecordingClientSession


class _ApprovingCtx(ToolContext):
    """离线 ToolContext：策略一律放行。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    mcp_runtime.upsert_client(root, dict(_SPEC))
    return root


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    fake = _FakeServer()
    monkeypatch.setattr(mcp_runtime, "_connect", fake.connect)
    return fake


@pytest.fixture(autouse=True)
def _no_sessions_left_behind():
    yield
    # 测试自己开着没关的循环里留下的会话，按进程退出时的路子收掉，不带进下一个测试
    mcp_runtime._close_pools_at_exit()


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _call(ws: Path, name: str = "echo", arguments: dict[str, Any] | None = None, *, scope: str = "") -> Any:
    return mcp_runtime.call_tool(ws, "fs", name, arguments or {}, scope=scope)


def _client_pool(ws: Path, client_id: str = "fs") -> Any:
    """当前事件循环里这个 client 的会话池，只能在循环里调用。"""
    return mcp_runtime._current_pool().clients[mcp_runtime._pool_key(ws, client_id)]


def _ctx(session_id: str, *, parent_session_id: str = "") -> ToolContext:
    """engine 交给工具的上下文：入口分支上的任务 session_id 是分支，parent_session_id 是对话的主会话。"""
    return ToolContext(
        supervisor_url="http://unused",
        session_id=session_id,
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=Path("."),
        http=None,  # type: ignore[arg-type]
        registry=None,
        parent_session_id=parent_session_id,
        node_id="chat",
    )


def _blocking_handler(running: list[int], gate: asyncio.Event) -> Any:
    """记下请求落在哪个会话上，然后卡住，直到 gate 打开。"""

    async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
        running.append(session.index)
        await gate.wait()
        return _text_result(f"{name}@{session.index}")

    return handler


def _rewrite_client(ws: Path, **changes: Any) -> None:
    """直接改 config/mcp.yaml，好比在控制台里改：不经 upsert_client，也就没有主动退役。"""
    path = config_layout.mcp_path(ws)
    with config_layout.locked(path):
        cfg = mcp_runtime._load_raw_config(ws)
        cfg["clients"]["fs"].update(changes)
        mcp_runtime._save_raw_config(ws, cfg)


def _saved_client(ws: Path, client_id: str = "fs") -> dict[str, Any]:
    return yaml.safe_load(config_layout.mcp_path(ws).read_text(encoding="utf-8"))["clients"][client_id]


def _clip_limit() -> int:
    return inspect.signature(registry_module._clip_result).parameters["limit"].default


# --- 借出与复用 ---


def test_sequential_calls_reuse_one_session(ws: Path, server: _FakeServer) -> None:
    async def main() -> list[dict[str, Any]]:
        return [await _call(ws, "first"), await _call(ws, "second")]

    first, second = asyncio.run(main())

    assert server.connects == 1
    assert server.sessions[0].requests == ["first", "second"]
    assert (first["data"]["result"], second["data"]["result"]) == ("first@0", "second@0")


def test_a_stuck_call_only_holds_its_own_session(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
            if name == "slow":
                entered.set()
                await gate.wait()
            return _text_result(f"{name}@{session.index}")

        server.handler = handler
        slow = asyncio.create_task(_call(ws, "slow"))
        await entered.wait()

        fast = await _call(ws, "fast")

        assert server.connects == 2
        assert fast["data"]["result"] == "fast@1"
        gate.set()
        assert (await slow)["data"]["result"] == "slow@0"
        await _call(ws, "again")
        await _call(ws, "again")
        assert server.connects == 2
        assert server.closes == 0

    asyncio.run(main())


def test_an_idle_session_is_closed_after_the_idle_timeout(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_SESSION_IDLE_SEC", 0.05)

    async def main() -> None:
        await _call(ws)
        await _until(lambda: server.closes == 1)
        await _call(ws)
        assert server.connects == 2

    asyncio.run(main())


def test_reusing_a_session_restarts_its_idle_timer(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws)
        session = _client_pool(ws).sessions[0]
        first = session._idle_timer
        assert first is not None and not first.cancelled()

        await _call(ws)

        assert server.connects == 1
        # 借出时撤掉头一次放回时排的计时器，不撤的话会话从头一次用完算起就到点被关
        assert first.cancelled()
        second = session._idle_timer
        assert second is not None and second is not first
        assert not second.cancelled()

    asyncio.run(main())


def test_an_idle_session_whose_transport_closed_is_replaced(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws)
        server.sessions[0].write_stream.close()

        result = await _call(ws)

        assert result["data"]["result"] == "echo@1"
        assert server.connects == 2
        await _until(lambda: server.sessions[0].closed)

    asyncio.run(main())


def test_a_session_whose_transport_failed_while_idle_is_not_reused(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lost: list[anyio.Event] = []
    exited: list[int] = []

    class _EchoSession:
        def __init__(self, index: int) -> None:
            self.index = index

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
            return _text_result(f"{name}@{self.index}")

    @asynccontextmanager
    async def connect(plan: Any):
        index = len(lost)
        lost.append(anyio.Event())

        async def transport() -> None:
            await lost[index].wait()
            raise ConnectionResetError("idle transport lost")

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(transport)
                yield mcp_runtime._Connection(session=_EchoSession(index))
        finally:
            exited.append(index)

    monkeypatch.setattr(mcp_runtime, "_connect", connect)

    async def main() -> list[dict[str, Any]]:
        first = await _call(ws)
        lost[0].set()
        await _until(lambda: exited == [0])
        return [first, await _call(ws)]

    first, second = asyncio.run(main())

    assert (first["data"]["result"], second["data"]["result"]) == ("echo@0", "echo@1")


# --- 按对话隔离 ---


def test_the_scope_is_the_conversations_main_session() -> None:
    scope = mcp_runtime.session_scope

    assert scope(_ctx("sess-a")) == "sess-a"
    # 入口分支上的任务回落到对话的主会话
    assert scope(_ctx("branch_1", parent_session_id="sess-a")) == "sess-a"
    assert scope(_ctx(" sess-a ")) == "sess-a"
    # 拿不到会话 id 的都走共享作用域
    assert scope(_ctx("")) == ""
    assert scope(None) == ""
    assert scope(SimpleNamespace(node_id="chat")) == ""
    # 没有 route_session_id 的上下文按同样的先后读字段
    assert scope(SimpleNamespace(session_id="branch_2", parent_session_id="sess-b")) == "sess-b"
    assert scope(SimpleNamespace(session_id="sess-c")) == "sess-c"


def test_each_conversation_gets_its_own_sessions(ws: Path, server: _FakeServer) -> None:
    async def main() -> list[str]:
        results = []
        for name, scope in (("first", "conv-a"), ("first", "conv-b"), ("second", "conv-a"), ("second", "conv-b")):
            results.append((await _call(ws, name, scope=scope))["data"]["result"])
        assert sorted(session.scope for session in _client_pool(ws).sessions) == ["conv-a", "conv-b"]
        return results

    assert asyncio.run(main()) == ["first@0", "first@1", "second@0", "second@1"]
    assert server.connects == 2
    assert [session.requests for session in server.sessions] == [["first", "second"], ["first", "second"]]


def test_a_branch_task_reuses_its_conversations_session(ws: Path, server: _FakeServer) -> None:
    tool = registry_module._make_mcp_tool(ws, "fs", "echo")
    contexts = [
        _ctx("sess-a"),
        _ctx("branch_1", parent_session_id="sess-a"),
        _ctx("sess-b"),
        _ctx("branch_2", parent_session_id="sess-b"),
        _ctx("branch_3", parent_session_id="sess-a"),
    ]

    async def main() -> list[str]:
        results = [(await tool({}, ctx))["data"]["result"] for ctx in contexts]
        assert sorted(session.scope for session in _client_pool(ws).sessions) == ["sess-a", "sess-b"]
        return results

    assert asyncio.run(main()) == ["echo@0", "echo@0", "echo@1", "echo@1", "echo@0"]
    assert server.connects == 2


def test_the_startup_tool_listing_and_calls_without_a_conversation_share_one_session(
    ws: Path, server: _FakeServer,
) -> None:
    server.tools = [mcp_types.Tool(name="echo", inputSchema={"type": "object"})]
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> list[str]:
        assert await registry.load_mcp_tools() == 1
        results = []
        for ctx in (None, SimpleNamespace(node_id="chat"), _ctx(""), _ctx("sess-a")):
            result = await registry.execute(name="mcp_fs_echo", arguments={}, ctx=ctx)
            results.append(result["data"]["result"])
        return results

    assert asyncio.run(main()) == ["echo@0", "echo@0", "echo@0", "echo@1"]
    assert server.sessions[0].requests == ["tools/list", "echo", "echo", "echo"]
    assert server.sessions[1].requests == ["echo"]


def test_share_sessions_lets_every_conversation_use_the_same_sessions(ws: Path, server: _FakeServer) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": True})
    tool = registry_module._make_mcp_tool(ws, "fs", "echo")
    contexts = [_ctx("sess-a"), _ctx("sess-b"), _ctx("branch_1", parent_session_id="sess-c"), None]

    async def main() -> list[str]:
        results = [(await tool({}, ctx))["data"]["result"] for ctx in contexts]
        assert [session.scope for session in _client_pool(ws).sessions] == [""]
        return results

    assert asyncio.run(main()) == ["echo@0"] * 4
    assert server.connects == 1
    assert server.plans[0].share_sessions is True


def test_switching_share_sessions_retires_the_sessions_of_every_conversation(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")

        # 控制台改原文，不经 upsert_client：下一次借出时指纹对不上，两个对话的旧会话一起退役，关完才起共用的那个
        _rewrite_client(ws, share_sessions=True)
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")

        assert server.connects == 3
        assert set(server.events[2:4]) == {"close 0", "close 1"}
        assert server.events[4] == "open 2"
        assert server.sessions[2].requests == ["echo", "echo"]
        assert server.plans[2].share_sessions is True

        # 改回按对话隔离同样算配置变了
        mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": False})
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")

        assert server.connects == 5
        assert server.events[5:8] == ["close 2", "open 3", "open 4"]
        assert server.plans[3].share_sessions is False

    asyncio.run(main())


# --- 上限 ---


def test_past_the_per_client_limit_a_call_gets_a_session_closed_after_use(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_MAX_POOLED_SESSIONS_PER_CLIENT", 2)

    async def main() -> None:
        gate = asyncio.Event()
        running: list[int] = []

        async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
            running.append(session.index)
            await gate.wait()
            return _text_result(f"{name}@{session.index}")

        server.handler = handler
        calls = [asyncio.create_task(_call(ws, f"job{i}")) for i in range(3)]
        await _until(lambda: len(running) == 3)

        # 第三个调用没有排队等前两个空出来，直接另开了一个会话
        assert server.connects == 3
        gate.set()
        await asyncio.gather(*calls)
        await _until(lambda: server.closes == 1)
        assert [session.closed for session in server.sessions] == [False, False, True]

        server.handler = None
        await _call(ws, "later")
        assert server.connects == 3

    asyncio.run(main())


def test_a_conversation_past_its_own_limit_gets_a_session_closed_after_use(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_MAX_POOLED_SESSIONS_PER_SCOPE", 2)

    async def main() -> None:
        gate = asyncio.Event()
        running: list[int] = []
        server.handler = _blocking_handler(running, gate)

        busy = [asyncio.create_task(_call(ws, f"a{i}", scope="conv-a")) for i in range(3)]
        await _until(lambda: len(running) == 3)
        other = asyncio.create_task(_call(ws, "b0", scope="conv-b"))
        await _until(lambda: len(running) == 4)

        assert server.connects == 4
        gate.set()
        await asyncio.gather(*busy, other)
        # conv-a 的第三个会话超出本对话的上限，不入池，用完就关；conv-b 没到自己的上限，照常入池
        await _until(lambda: server.closes == 1)
        assert [session.closed for session in server.sessions] == [False, False, True, False]

        server.handler = None
        for scope in ("conv-a", "conv-a", "conv-b"):
            await _call(ws, scope=scope)
        assert server.connects == 4

    asyncio.run(main())


def test_the_client_wide_limit_closes_the_least_recently_used_idle_session_of_any_conversation(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_MAX_POOLED_SESSIONS_PER_CLIENT", 3)

    async def main() -> None:
        for scope in ("conv-a", "conv-b", "conv-c"):
            await _call(ws, scope=scope)
        # conv-a 又用了一次，最久没用的成了 conv-b
        await _call(ws, scope="conv-a")

        await _call(ws, scope="conv-d")
        await _until(lambda: server.closes == 1)
        assert [session.closed for session in server.sessions] == [False, True, False, False]

        await _call(ws, scope="conv-e")
        await _until(lambda: server.closes == 2)
        assert [session.closed for session in server.sessions] == [False, True, True, False, False]

        # 留下的三个还在池子里，接着复用
        for scope in ("conv-a", "conv-d", "conv-e"):
            await _call(ws, scope=scope)
        assert (server.connects, server.closes) == (5, 2)

    asyncio.run(main())


def test_with_every_session_busy_at_the_client_wide_limit_a_new_conversation_gets_a_one_off_session(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_MAX_POOLED_SESSIONS_PER_CLIENT", 2)

    async def main() -> None:
        gate = asyncio.Event()
        running: list[int] = []
        server.handler = _blocking_handler(running, gate)

        busy = [asyncio.create_task(_call(ws, scope=scope)) for scope in ("conv-a", "conv-b")]
        await _until(lambda: len(running) == 2)
        extra = asyncio.create_task(_call(ws, scope="conv-c"))
        await _until(lambda: len(running) == 3)

        # 别的对话的会话都在忙，一个也不能挤掉，另开的这个不入池
        assert (server.connects, server.closes) == (3, 0)
        gate.set()
        await asyncio.gather(*busy, extra)
        await _until(lambda: server.closes == 1)
        assert [session.closed for session in server.sessions] == [False, False, True]

        server.handler = None
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")
        assert server.connects == 3

    asyncio.run(main())


def test_a_dead_idle_session_gives_up_its_place_before_a_live_one_is_evicted(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "_MAX_POOLED_SESSIONS_PER_CLIENT", 2)

    async def main() -> None:
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")
        # conv-b 的服务端在空闲时退出了；最久没用的是 conv-a，但该让位的是死掉的这个
        server.sessions[1].write_stream.close()

        await _call(ws, scope="conv-c")

        await _until(lambda: server.sessions[1].closed)
        assert server.sessions[0].closed is False
        await _call(ws, scope="conv-a")
        assert server.connects == 3
        assert server.sessions[0].requests == ["echo", "echo"]

    asyncio.run(main())


# --- 出错与重试 ---


def test_a_session_that_fails_to_start_is_replaced_once(ws: Path, server: _FakeServer) -> None:
    server.fail_connects = 1

    result = asyncio.run(_call(ws))

    assert result["ok"] is True
    assert result["data"]["result"] == "echo@0"
    assert server.connects == 2
    assert server.events[:2] == ["refused", "open 0"]


def test_a_second_start_failure_is_raised_without_a_third_attempt(ws: Path, server: _FakeServer) -> None:
    server.fail_connects = 2

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(_call(ws))

    assert server.connects == 2
    assert server.sessions == []


def test_list_tools_also_retries_a_failed_start_once(ws: Path, server: _FakeServer) -> None:
    server.fail_connects = 1

    listed = asyncio.run(mcp_runtime.list_tools(ws, "fs"))

    assert listed == {"ok": True, "client_id": "fs", "tools": []}
    assert server.connects == 2


def test_an_error_after_the_request_went_out_is_not_retried_and_drops_the_session(
    ws: Path, server: _FakeServer,
) -> None:
    async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
        raise ConnectionResetError("server went away mid-call")

    async def main() -> None:
        server.handler = handler
        with pytest.raises(ConnectionResetError, match="mid-call"):
            await _call(ws, "write", {"path": "a.txt"})

        # 请求只发了一次，也没有换新会话重发
        assert server.connects == 1
        assert server.sessions[0].requests == ["write"]
        await _until(lambda: server.closes == 1)

        server.handler = None
        await _call(ws, "write", {"path": "a.txt"})
        assert server.connects == 2

    asyncio.run(main())


def test_an_error_answer_from_the_server_keeps_the_session(ws: Path, server: _FakeServer) -> None:
    async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
        if "colour" in arguments:
            raise McpError(mcp_types.ErrorData(code=mcp_types.INVALID_PARAMS, message="unknown argument: colour"))
        return _text_result(f"{name}@{session.index}")

    tool = registry_module._make_mcp_tool(ws, "fs", "paint")

    async def main() -> None:
        server.handler = handler
        refused = await tool({"colour": "red"}, _ctx("sess-a"))
        assert refused["ok"] is False
        assert "unknown argument: colour" in refused["error"]

        # 服务端回了应答，这次请求有始有终：不重发，会话也不丢，对话攒下的状态（比如浏览器里开着的页面）还在
        accepted = await tool({"color": "red"}, _ctx("sess-a"))
        assert accepted["data"]["result"] == "paint@0"
        assert server.sessions[0].requests == ["paint", "paint"]
        assert (server.connects, server.closes) == (1, 0)

    asyncio.run(main())


def test_a_connection_closed_error_drops_the_session(ws: Path, server: _FakeServer) -> None:
    async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
        raise McpError(mcp_types.ErrorData(code=mcp_types.CONNECTION_CLOSED, message="Connection closed"))

    async def main() -> None:
        server.handler = handler
        with pytest.raises(McpError, match="Connection closed"):
            await _call(ws, "write", {"path": "a.txt"})

        # 连接断了 SDK 报的也是 McpError，但这不是服务端的应答：不重发，会话丢掉
        assert server.sessions[0].requests == ["write"]
        await _until(lambda: server.closes == 1)
        server.handler = None
        await _call(ws, "write", {"path": "a.txt"})
        assert server.connects == 2

    asyncio.run(main())


def test_a_transport_that_dies_mid_call_fails_the_call_instead_of_hanging(
    ws: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    class _HangingSession:
        def __init__(self, lost: anyio.Event) -> None:
            self.lost = lost

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
            sent.append(name)
            self.lost.set()
            await anyio.sleep_forever()

    @asynccontextmanager
    async def dying_connect(plan: Any):
        lost = anyio.Event()

        async def transport() -> None:
            await lost.wait()
            raise ConnectionResetError("transport lost")

        async with anyio.create_task_group() as tg:
            tg.start_soon(transport)
            yield mcp_runtime._Connection(session=_HangingSession(lost))

    monkeypatch.setattr(mcp_runtime, "_connect", dying_connect)

    async def main() -> None:
        # wait_for 只防实现出错时测试挂死
        await asyncio.wait_for(_call(ws, "write"), timeout=10)

    with pytest.raises(ConnectionResetError, match="transport lost"):
        asyncio.run(main())
    assert sent == ["write"]


def test_a_cancelled_call_closes_its_session_instead_of_returning_it(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        entered = asyncio.Event()

        async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
            entered.set()
            await asyncio.Event().wait()

        server.handler = handler
        call = asyncio.create_task(_call(ws, "hang"))
        await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        await _until(lambda: server.closes == 1)
        server.handler = None
        await _call(ws, "next")
        assert server.connects == 2

    asyncio.run(main())


# --- 配置变化与退役 ---


def test_a_changed_client_config_closes_the_old_session_before_starting_a_new_one(
    ws: Path, server: _FakeServer,
) -> None:
    async def main() -> None:
        await _call(ws)
        _rewrite_client(ws, args=["--stdio", "--verbose"])
        await _call(ws)

    asyncio.run(main())

    assert server.connects == 2
    assert server.events[:3] == ["open 0", "close 0", "open 1"]
    assert server.plans[1].args == ("--stdio", "--verbose")


def test_a_rotated_secret_behind_an_env_reference_counts_as_a_config_change(
    ws: Path, server: _FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MCP_POOL_TOKEN", "first")
    mcp_runtime.upsert_client(ws, {**_SPEC, "env": {"TOKEN": "${FAKE_MCP_POOL_TOKEN}"}})

    async def main() -> None:
        await _call(ws)
        await _call(ws)
        monkeypatch.setenv("FAKE_MCP_POOL_TOKEN", "second")
        await _call(ws)

    asyncio.run(main())

    assert server.connects == 2
    assert dict(server.plans[0].env)["TOKEN"] == "first"
    assert dict(server.plans[1].env)["TOKEN"] == "second"
    assert server.events[:3] == ["open 0", "close 0", "open 1"]


def test_a_client_disabled_behind_the_pools_back_is_refused_and_its_sessions_closed(
    ws: Path, server: _FakeServer,
) -> None:
    async def main() -> None:
        await _call(ws)
        _rewrite_client(ws, enabled=False)
        with pytest.raises(RuntimeError, match="disabled"):
            await _call(ws)
        await _until(lambda: server.closes == 1)

    asyncio.run(main())

    assert server.connects == 1


def test_saving_a_client_retires_its_idle_sessions(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws)
        mcp_runtime.upsert_client(ws, dict(_SPEC))
        await _until(lambda: server.closes == 1)
        await _call(ws)
        assert server.connects == 2

    asyncio.run(main())


def test_deleting_a_client_closes_its_sessions(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws)
        assert mcp_runtime.delete_client(ws, "fs") is True
        await _until(lambda: server.closes == 1)
        with pytest.raises(ValueError, match="client not found"):
            await _call(ws)

    asyncio.run(main())

    assert server.connects == 1


def test_a_session_in_use_when_its_client_is_saved_finishes_the_call_then_closes(
    ws: Path, server: _FakeServer,
) -> None:
    async def main() -> None:
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
            entered.set()
            await gate.wait()
            return _text_result("done")

        server.handler = handler
        call = asyncio.create_task(_call(ws, "slow"))
        await entered.wait()

        mcp_runtime.upsert_client(ws, dict(_SPEC))
        await asyncio.sleep(0.05)
        assert server.closes == 0

        gate.set()
        assert (await call)["data"]["result"] == "done"
        await _until(lambda: server.closes == 1)
        server.handler = None
        await _call(ws)
        assert server.connects == 2

    asyncio.run(main())


def test_saving_a_client_retires_its_sessions_in_every_conversation(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def handler(session: _FakeSession, name: str, arguments: dict[str, Any]) -> Any:
            if name == "slow":
                entered.set()
                await gate.wait()
            return _text_result(f"{name}@{session.index}")

        server.handler = handler
        await mcp_runtime.list_tools(ws, "fs")
        await _call(ws, scope="conv-a")
        slow = asyncio.create_task(_call(ws, "slow", scope="conv-b"))
        await entered.wait()

        mcp_runtime.upsert_client(ws, dict(_SPEC))

        # 共享作用域和 conv-a 的空闲会话现在就关；conv-b 那个正在用，等这次调用结束
        await _until(lambda: server.sessions[0].closed and server.sessions[1].closed)
        assert server.sessions[2].closed is False
        gate.set()
        assert (await slow)["data"]["result"] == "slow@2"
        await _until(lambda: server.sessions[2].closed)

        for scope in ("", "conv-a", "conv-b"):
            await _call(ws, scope=scope)
        assert server.connects == 6

    asyncio.run(main())


def test_deleting_a_client_closes_its_sessions_in_every_conversation(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        for scope in ("", "conv-a", "conv-b"):
            await _call(ws, scope=scope)
        assert mcp_runtime.delete_client(ws, "fs") is True
        await _until(lambda: server.closes == 3)

    asyncio.run(main())

    assert server.connects == 3


def test_after_a_save_a_new_session_starts_only_once_the_retired_one_has_closed(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        await _call(ws, scope="conv-a")
        server.close_gate = asyncio.Event()
        mcp_runtime.upsert_client(ws, dict(_SPEC))

        call = asyncio.create_task(_call(ws, scope="conv-a"))
        await asyncio.sleep(0.05)
        # 旧进程还没退出，新的先不起：占着独占资源（浏览器配置目录之类）的服务新旧同时在会撞车
        assert server.connects == 1

        server.close_gate.set()
        assert (await call)["data"]["result"] == "echo@1"
        assert server.events == ["open 0", "close 0", "open 1"]

    asyncio.run(main())


def test_a_retired_session_stuck_closing_stops_holding_up_new_sessions_after_the_close_wait(
    ws: Path, server: _FakeServer,
) -> None:
    async def main() -> None:
        await _call(ws)
        server.close_gate = asyncio.Event()
        mcp_runtime.upsert_client(ws, dict(_SPEC))
        stuck = _client_pool(ws).sessions[0]
        # 这次关闭已经卡了一整个等待时长：之后的调用不再等它，不然每次借出都要干等一轮
        stuck.closing_since -= mcp_runtime._SESSION_CLOSE_WAIT_SEC

        result = await asyncio.wait_for(_call(ws), timeout=2)

        assert result["data"]["result"] == "echo@1"
        assert server.sessions[0].closed is False
        server.close_gate.set()
        await _until(lambda: server.sessions[0].closed)

    asyncio.run(main())


def test_saving_a_client_from_another_thread_retires_sessions_of_a_loop_running_elsewhere(
    ws: Path, server: _FakeServer,
) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(_call(ws), loop).result(10)
        assert server.closes == 0

        mcp_runtime.upsert_client(ws, dict(_SPEC))

        deadline = time.monotonic() + 5
        while server.closes == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.closes == 1
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def test_reloading_tools_retires_idle_sessions_and_lists_from_a_fresh_one(ws: Path, server: _FakeServer) -> None:
    server.tools = [mcp_types.Tool(name="ping", inputSchema={"type": "object"})]
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> dict[str, Any]:
        assert await registry.load_mcp_tools() == 1
        assert server.connects == 1
        return await reload_tools({}, SimpleNamespace(registry=registry))

    result = asyncio.run(main())

    assert result["ok"] is True
    assert result["data"]["mcp_tools"] == 1
    assert server.connects == 2
    assert server.events[:3] == ["open 0", "close 0", "open 1"]
    assert registry.get_spec("mcp_fs_ping") is not None


def test_reloading_tools_retires_the_sessions_of_every_conversation_and_client(
    ws: Path, server: _FakeServer,
) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> dict[str, Any]:
        await _call(ws, scope="conv-a")
        await mcp_runtime.call_tool(ws, "web", "echo", {}, scope="conv-b")
        return await reload_tools({}, SimpleNamespace(registry=registry))

    result = asyncio.run(main())

    assert result["ok"] is True
    # 两个 client、两个对话的旧会话都关完，才从新会话里取工具表
    assert set(server.events[2:4]) == {"close 0", "close 1"}
    assert server.events[4:6] == ["open 2", "open 3"]
    assert [plan.client_id for plan in server.plans[2:4]] == ["fs", "web"]


# --- 跟上 supervisor 的重载序号 ---


def _seqs(run_id: str = "run-a", *, scripts: int = 0, config: int = 0, reload: int = 0) -> ReloadSeqs:
    return ReloadSeqs(run_id=run_id, scripts=scripts, mcp_config=config, mcp_reload=reload)


def _rewrite_clients(ws: Path, edit: Any) -> None:
    """整份改 clients，好比在控制台、在共享组别的实例里改：不经 upsert_client / delete_client，也就没有主动退役。"""
    path = config_layout.mcp_path(ws)
    with config_layout.locked(path):
        cfg = mcp_runtime._load_raw_config(ws)
        edit(cfg["clients"])
        mcp_runtime._save_raw_config(ws, cfg)


def _break_config(ws: Path, text: str = "clients: [unclosed\n") -> None:
    config_layout.write_config_text(config_layout.mcp_path(ws), text)


def _plans_for(server: _FakeServer, client_id: str) -> list[Any]:
    return [plan for plan in server.plans if plan.client_id == client_id]


def _serve_ping(server: _FakeServer) -> None:
    server.tools = [mcp_types.Tool(name="ping", inputSchema={"type": "object"})]


def test_a_changed_client_is_relisted_alone_and_the_others_keep_their_sessions(ws: Path, server: _FakeServer) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        assert await registry.load_mcp_tools(seqs=_seqs()) == 2
        await mcp_runtime.call_tool(ws, "web", "echo", {}, scope="conv-a")
        await mcp_runtime.call_tool(ws, "web", "echo", {}, scope="conv-b")
        web_sessions = [session for session in server.sessions if session.plan.client_id == "web"]
        before = len(server.plans)

        _rewrite_client(ws, args=["--other"])

        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert [plan.client_id for plan in server.plans[before:]] == ["fs"]
        assert server.plans[-1].args == ("--other",)
        assert len(web_sessions) == 3
        assert not any(session.closed for session in web_sessions)
        assert registry.get_spec("mcp_fs_ping") is not None
        assert registry.get_spec("mcp_web_ping") is not None

    asyncio.run(main())


def test_changing_only_the_description_relists_nothing(ws: Path, server: _FakeServer) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        _rewrite_client(ws, description="只改了说明")

        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert server.connects == 1
        assert server.sessions[0].requests == ["tools/list"]
        assert server.sessions[0].closed is False

    asyncio.run(main())


@pytest.mark.parametrize("edit", [
    pytest.param(lambda clients: clients.pop("fs"), id="deleted"),
    pytest.param(lambda clients: clients["fs"].update(enabled=False), id="disabled"),
])
def test_a_deleted_or_disabled_client_loses_its_tools_and_its_sessions(
    ws: Path, server: _FakeServer, edit: Any,
) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        await _call(ws, scope="conv-a")
        await _call(ws, scope="conv-b")
        fs_sessions = [session for session in server.sessions if session.plan.client_id == "fs"]

        _rewrite_clients(ws, edit)

        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert registry.get_spec("mcp_fs_ping") is None
        assert registry.get_spec("mcp_web_ping") is not None
        # 不再有人调它，各对话里攥着的会话也得收掉，不能等闲置超时
        assert len(fs_sessions) == 3
        await _until(lambda: all(session.closed for session in fs_sessions))

    asyncio.run(main())


def test_a_new_client_is_listed_without_touching_the_existing_ones(ws: Path, server: _FakeServer) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        _rewrite_clients(ws, lambda clients: clients.update(web={**clients["fs"], "args": ["--web"]}))

        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert [plan.client_id for plan in server.plans] == ["fs", "web"]
        assert server.sessions[0].closed is False
        assert registry.get_spec("mcp_web_ping") is not None

    asyncio.run(main())


def test_an_explicit_reload_restarts_every_client(ws: Path, server: _FakeServer) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        await _call(ws, scope="conv-a")
        old = list(server.sessions)

        assert await registry.sync_mcp_tools(_seqs(scripts=1, reload=1)) is True
        assert all(session.closed for session in old)
        assert [plan.client_id for plan in server.plans[len(old):]] == ["fs", "web"]

    asyncio.run(main())


@pytest.mark.parametrize(("offered", "restarts"), [
    pytest.param(_seqs(config=2, reload=1), False, id="already-followed"),
    pytest.param(_seqs(config=1, reload=1), False, id="older"),
    pytest.param(_seqs("run-b", config=1), True, id="supervisor-restarted"),
])
def test_sequences_the_worker_may_already_have_followed(
    ws: Path, server: _FakeServer, offered: ReloadSeqs, restarts: bool,
) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs(config=2, reload=1))
        old = list(server.sessions)

        assert await registry.sync_mcp_tools(offered) is restarts
        # supervisor 重启过，序号从头数，比不出先后：只能全部重来
        assert server.connects == (2 if restarts else 1)
        assert all(session.closed for session in old) is restarts

    asyncio.run(main())


@pytest.mark.parametrize("broken", ["clients: [unclosed\n", "- a list\n", "version: abc\nclients: {}\n", "clients: 5\n"])
def test_a_file_broken_while_running_keeps_the_last_good_clients_until_it_is_fixed(
    ws: Path, server: _FakeServer, broken: str,
) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        _break_config(ws, broken)

        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert registry.get_spec("mcp_fs_ping") is not None
        assert server.sessions[0].closed is False
        called = await registry.execute(name="mcp_fs_ping", arguments={}, ctx=None)
        assert called["data"]["result"] == "ping@0"
        assert [client["id"] for client in mcp_runtime.list_clients(ws)] == ["fs"]
        assert registry.mcp_config_error().startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")

        # 修好时连同改了的启动参数一起写回：这一份照常跟上
        _rewrite_client(ws, args=["--fixed"])

        assert await registry.sync_mcp_tools(_seqs(config=2)) is True
        assert registry.mcp_config_error() == ""
        assert server.plans[-1].args == ("--fixed",)

    asyncio.run(main())


def test_a_file_broken_before_the_first_read_means_no_clients(tmp_path: Path, server: _FakeServer) -> None:
    root = tmp_path / "fresh"
    config_layout.write_config_text(config_layout.mcp_path(root), "clients: [unclosed\n")
    registry = ToolRegistry(workspace_root=root, tools_dir=root / "tools")

    assert asyncio.run(registry.load_mcp_tools()) == 0

    assert server.connects == 0
    assert mcp_runtime.config_error(root).startswith(f"{config_layout.MCP_FILE} 用不了，现在按没有客户端算")
    assert mcp_runtime.list_clients(root) == []


def test_a_client_whose_listing_hangs_times_out_without_holding_up_the_others(ws: Path, server: _FakeServer) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    _serve_ping(server)
    runtime = config_layout.runtime_path(ws)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("tools:\n  mcp:\n    list_timeout_sec: 1\n", encoding="utf-8")
    hanging = {"fs"}

    async def list_handler(session: _FakeSession) -> None:
        if session.plan.client_id in hanging:
            await asyncio.Event().wait()

    server.list_handler = list_handler
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        started = time.monotonic()
        assert await registry.load_mcp_tools(seqs=_seqs()) == 1
        assert time.monotonic() - started < 10
        assert registry.get_spec("mcp_fs_ping") is None
        assert registry.get_spec("mcp_web_ping") is not None
        await _until(lambda: all(session.closed for session in server.sessions if session.plan.client_id == "fs"))

        # 别的客户端的配置变了：没列成的这个配置没变，不跟着再等一轮超时
        _rewrite_clients(ws, lambda clients: clients["web"].update(description="别处改的"))
        assert await registry.sync_mcp_tools(_seqs(config=1)) is True
        assert len(_plans_for(server, "fs")) == 1

        # 显式重载才再试
        hanging.clear()
        assert await registry.sync_mcp_tools(_seqs(config=1, reload=1)) is True
        assert len(_plans_for(server, "fs")) == 2
        assert registry.get_spec("mcp_fs_ping") is not None

    asyncio.run(main())


def test_the_old_tools_stay_registered_while_a_client_is_relisted(ws: Path, server: _FakeServer) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> None:
        await registry.load_mcp_tools(seqs=_seqs())
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def list_handler(session: _FakeSession) -> None:
            entered.set()
            await gate.wait()

        server.list_handler = list_handler
        server.tools = [mcp_types.Tool(name="pong", inputSchema={"type": "object"})]
        _rewrite_client(ws, args=["--other"])
        sync = asyncio.create_task(registry.sync_mcp_tools(_seqs(config=1)))
        await entered.wait()

        # 重列途中开始的任务照样拿得到旧工具，不会碰上一张缺了这个客户端的工具表
        assert registry.get_spec("mcp_fs_ping") is not None
        assert registry.get_spec("mcp_fs_pong") is None

        gate.set()
        assert await sync is True
        assert registry.get_spec("mcp_fs_ping") is None
        assert registry.get_spec("mcp_fs_pong") is not None

    asyncio.run(main())


def test_two_syncs_at_once_relist_once(ws: Path, server: _FakeServer) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> list[bool]:
        await registry.load_mcp_tools(seqs=_seqs())
        _rewrite_client(ws, args=["--other"])
        return list(await asyncio.gather(
            registry.sync_mcp_tools(_seqs(config=1)), registry.sync_mcp_tools(_seqs(config=1)),
        ))

    assert sorted(asyncio.run(main())) == [False, True]
    assert len(_plans_for(server, "fs")) == 2


class _SupervisorReply:
    """reload_tools 发给 supervisor 的那一下：记下地址和请求体，回给定的状态码和应答体。"""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        self.posts: list[tuple[str, Any]] = []

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> SimpleNamespace:
        self.posts.append((url, json))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)


def _reload_ctx(registry: ToolRegistry, http: Any) -> SimpleNamespace:
    return SimpleNamespace(registry=registry, http=http, supervisor_url="http://supervisor")


def test_reload_tools_tells_the_supervisor_and_this_worker_does_not_restart_twice(
    ws: Path, server: _FakeServer,
) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")
    reply = _SupervisorReply(200, {"ok": True, **_seqs(scripts=1, reload=1).payload()})

    async def main() -> dict[str, Any]:
        await registry.load_mcp_tools(seqs=_seqs())
        result = await reload_tools({}, _reload_ctx(registry, reply))
        # 本 worker 的 follower 随后从 supervisor 拿到的是同一组序号
        assert await registry.sync_mcp_tools(_seqs(scripts=1, reload=1)) is False
        return result

    result = asyncio.run(main())

    assert result["ok"] is True
    assert result["data"]["other_workers_notified"] is True
    assert result["data"]["mcp_tools"] == 1
    assert reply.posts == [("http://supervisor/v1/tools/reload", {"mcp": True})]
    assert server.connects == 2
    assert server.events[:3] == ["open 0", "close 0", "open 1"]


def test_reload_tools_refused_by_the_supervisor_restarts_this_worker_and_says_the_others_were_not_told(
    ws: Path, server: _FakeServer,
) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")
    reply = _SupervisorReply(401, {"detail": "Unauthorized"})

    async def main() -> dict[str, Any]:
        await registry.load_mcp_tools(seqs=_seqs())
        return await reload_tools({}, _reload_ctx(registry, reply))

    result = asyncio.run(main())

    assert result["ok"] is True
    assert result["data"]["other_workers_notified"] is False
    assert "Unauthorized" in result["data"]["result"]
    assert server.events[:3] == ["open 0", "close 0", "open 1"]


def test_reload_tools_on_a_broken_file_rescans_the_scripts_only(ws: Path, server: _FakeServer) -> None:
    _serve_ping(server)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")
    reply = _SupervisorReply(200, {"ok": True, **_seqs(scripts=1).payload()})

    async def main() -> dict[str, Any]:
        await registry.load_mcp_tools(seqs=_seqs())
        _break_config(ws)
        result = await reload_tools({}, _reload_ctx(registry, reply))
        # 文件坏着重开也只是按旧配置再连一遍，白丢各对话的会话状态
        assert server.sessions[0].closed is False
        return result

    result = asyncio.run(main())

    assert result["ok"] is False
    assert f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的" in result["error"]
    assert result["data"]["mcp_tools"] == 1
    assert reply.posts == [("http://supervisor/v1/tools/reload", {"mcp": False})]
    assert server.connects == 1


class _RecordingCtx(_ApprovingCtx):
    """放行一切，同时记下送审过哪些操作。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.requested: list[str] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append(op)
        return {"safety_level": "auto"}


def test_saving_or_deleting_a_client_is_refused_before_approval_while_the_file_is_broken(ws: Path) -> None:
    path = config_layout.mcp_path(ws)
    _break_config(ws)
    before = path.read_bytes()
    ctx = _RecordingCtx(ws)

    saved = asyncio.run(create_or_update_mcp_client({**_SPEC, "id": "web"}, ctx))
    deleted = asyncio.run(delete_mcp_client({"id": "fs"}, ctx))

    for result in (saved, deleted):
        assert result["ok"] is False
        assert "先修好这份文件再改" in result["error"]
    # 存不进去的改动不送审，审批人不用批一份注定被拒的配置
    assert ctx.requested == []
    with pytest.raises(mcp_runtime.McpConfigError):
        mcp_runtime.upsert_client(ws, {**_SPEC, "id": "web"})
    with pytest.raises(mcp_runtime.McpConfigError):
        mcp_runtime.delete_client(ws, "fs")
    # 坏文件原样留着等人修，不会被整份盖成只剩一个客户端
    assert path.read_bytes() == before


class _DetailsCtx(_ApprovingCtx):
    """放行一切，同时记下送审的 details。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": "auto"}


def test_the_approval_shows_what_a_stdio_client_runs_with_its_secrets_hidden(ws: Path) -> None:
    # 保存后每个 worker 都起这条命令，审批人得看见命令、参数和环境；令牌却会跟着 details 进事件流、控制台和调试工具。
    ctx = _DetailsCtx(ws)
    spec = {
        "id": "gh", "transport": "stdio", "command": "npx",
        "args": ["-y", "server-github", "--token=ghp_abcdefghijklmnopqrst"],
        "env": {"GITHUB_TOKEN": "ghp_zyxwvutsrqponmlkjihg", "LD_PRELOAD": "/tmp/hook.so"},
    }

    saved = asyncio.run(create_or_update_mcp_client(spec, ctx))

    assert saved["ok"] is True
    [(op, details)] = ctx.requested
    assert op == "write_file"
    assert details["path"] == config_layout.MCP_FILE
    assert details["mcp_client"] == "gh"
    assert details["transport"] == "stdio"
    assert details["command"] == "npx"
    assert details["args"][:2] == ["-y", "server-github"]
    # 不像密钥的照原样给：LD_PRELOAD 这种正是审批人要看的
    assert details["env"] == {"GITHUB_TOKEN": HIDDEN, "LD_PRELOAD": "/tmp/hook.so"}
    shown = json.dumps(details, ensure_ascii=False)
    assert "abcdefghijklmnopqrst" not in shown
    assert "zyxwvutsrqponmlkjihg" not in shown
    stored = yaml.safe_load(config_layout.mcp_path(ws).read_text(encoding="utf-8"))["clients"]["gh"]
    assert stored["env"]["GITHUB_TOKEN"] == "ghp_zyxwvutsrqponmlkjihg"
    assert stored["args"][2] == "--token=ghp_abcdefghijklmnopqrst"


def test_the_approval_shows_where_an_http_client_connects_with_its_secrets_hidden(ws: Path) -> None:
    ctx = _DetailsCtx(ws)
    spec = {
        "id": "remote", "transport": "streamable_http",
        "url": "https://user:hunter2@mcp.example/mcp?api_key=abcdef123456",
        "headers": {"Authorization": "Bearer abcdef123456", "X-Trace": "on"},
    }

    saved = asyncio.run(create_or_update_mcp_client(spec, ctx))

    assert saved["ok"] is True
    [(_op, details)] = ctx.requested
    assert details["transport"] == "streamable_http"
    assert details["url"].startswith(f"https://{HIDDEN}@mcp.example/mcp?api_key=")
    assert details["headers"] == {"Authorization": HIDDEN, "X-Trace": "on"}
    shown = json.dumps(details, ensure_ascii=False)
    assert "hunter2" not in shown
    assert "abcdef123456" not in shown


def test_a_numeric_password_in_the_environment_is_hidden_too(ws: Path) -> None:
    # 纯数字的口令在模型给的 JSON 和 YAML 里都是数，照样不能进 details。
    ctx = _DetailsCtx(ws)
    spec = {
        "id": "db", "transport": "stdio", "command": "db-mcp",
        "env": {"DB_PASSWORD": 123456, "PGPASSWORD": 1234.5, "DB_PORT": 5432},
    }

    saved = asyncio.run(create_or_update_mcp_client(spec, ctx))

    assert saved["ok"] is True
    [(_op, details)] = ctx.requested
    assert details["env"] == {"DB_PASSWORD": HIDDEN, "PGPASSWORD": HIDDEN, "DB_PORT": "5432"}
    shown = json.dumps(details, ensure_ascii=False)
    assert "123456" not in shown
    assert "1234.5" not in shown
    stored = yaml.safe_load(config_layout.mcp_path(ws).read_text(encoding="utf-8"))["clients"]["db"]
    assert str(stored["env"]["DB_PASSWORD"]) == "123456"


@pytest.mark.parametrize("spec", [
    {"id": "fs2", "transport": "stdio"},
    {"id": "fs2", "transport": "stdio", "command": "   "},
    {"id": "remote", "transport": "sse"},
    {"id": "bad id!", "transport": "stdio", "command": "fake-mcp-server"},
    {"id": "fs2", "transport": "stdio", "command": "fake-mcp-server", "enabled": "maybe"},
])
def test_a_client_that_cannot_be_saved_is_refused_before_approval(ws: Path, spec: dict[str, Any]) -> None:
    # 审批人批了也存不进去的配置不送审，免得白审一次。
    path = config_layout.mcp_path(ws)
    before = path.read_bytes()
    ctx = _RecordingCtx(ws)

    result = asyncio.run(create_or_update_mcp_client(spec, ctx))

    assert result["ok"] is False
    assert ctx.requested == []
    assert path.read_bytes() == before


def test_listing_clients_on_a_broken_file_says_why_and_shows_the_ones_in_use(ws: Path) -> None:
    mcp_runtime.list_clients(ws)
    _break_config(ws)

    result = asyncio.run(list_mcp_clients({}, _ApprovingCtx(ws)))

    assert result["ok"] is True
    assert result["data"]["config_error"].startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")
    assert result["data"]["result"].startswith(f"{config_layout.MCP_FILE} 用不了")
    assert [client["id"] for client in result["data"]["clients"]] == ["fs"]


_BROKEN_ON_A_TOKEN = [
    pytest.param(
        "clients:\n  fs:\n    env:\n      API_TOKEN: sk-SECRET-mcp: x\n",
        "YAML 解析失败：第 4 行第 31 列：mapping values are not allowed here",
        id="broken-line",
    ),
    # 写错的 !!int 原来让 PyYAML 抛 ValueError，McpConfigError 接不住，读配置的地方全跟着炸。
    pytest.param(
        "clients:\n  fs:\n    env:\n      API_TOKEN: !!int sk-SECRET-mcp\n",
        "YAML 解析失败：第 4 行第 18 列：值不是合法的 int",
        id="bad-value",
    ),
]


@pytest.mark.parametrize("broken,reason", _BROKEN_ON_A_TOKEN)
def test_the_reason_handed_to_the_model_names_the_place_but_not_the_line(
    ws: Path, caplog: pytest.LogCaptureFixture, broken: str, reason: str,
) -> None:
    # 客户端的 env、headers 里写着令牌；列客户端的结果和拒绝改写的原因都交给模型，也进日志。
    mcp_runtime.list_clients(ws)
    with caplog.at_level(logging.WARNING, logger=mcp_runtime.__name__):
        _break_config(ws, broken)
        listed = asyncio.run(list_mcp_clients({}, _ApprovingCtx(ws)))
        saved = asyncio.run(create_or_update_mcp_client({**_SPEC, "id": "web"}, _RecordingCtx(ws)))

    assert listed["data"]["config_error"] == f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的：{reason}"
    assert saved["ok"] is False and reason in saved["error"]
    for text in (json.dumps(listed, ensure_ascii=False), json.dumps(saved, ensure_ascii=False), caplog.text):
        assert "sk-SECRET" not in text


@pytest.mark.parametrize("broken,reason", _BROKEN_ON_A_TOKEN)
def test_the_editor_check_leaves_no_content_on_the_exception_chain(broken: str, reason: str) -> None:
    # 控制台原文编辑器拿它判能不能存，接口层再包一层往上抛。
    with pytest.raises(mcp_runtime.McpConfigError) as caught:
        mcp_runtime.check_mcp_text(broken)

    assert str(caught.value) == reason
    for link in (caught.value.__cause__, caught.value.__context__):
        assert link is not None and "sk-SECRET" not in str(link)


# --- share_sessions 配置 ---


def test_saving_a_client_without_share_sessions_keeps_the_current_setting(ws: Path) -> None:
    assert mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": True})["share_sessions"] is True
    assert _saved_client(ws)["share_sessions"] is True

    # 没传就沿用文件里的值，别的字段照常按这次给的整份重写
    kept = mcp_runtime.upsert_client(ws, {**_SPEC, "description": "改过的说明"})
    assert kept["share_sessions"] is True
    assert _saved_client(ws)["share_sessions"] is True
    assert _saved_client(ws)["description"] == "改过的说明"
    assert mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": None})["share_sessions"] is True
    assert mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": ""})["share_sessions"] is True

    # 传了 false 就去掉这个键：文件里没有它就是按对话隔离
    dropped = mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": False})
    assert dropped["share_sessions"] is False
    assert "share_sessions" not in _saved_client(ws)

    assert mcp_runtime.upsert_client(ws, dict(_SPEC))["share_sessions"] is False
    assert "share_sessions" not in _saved_client(ws)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True), ("yes", True), ("On", True), ("1", True), (1, True),
        (False, False), ("no", False), ("off", False), (0, False),
    ],
)
def test_share_sessions_is_read_like_other_switches(ws: Path, value: Any, expected: bool) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": value})

    assert mcp_runtime.get_client_spec(ws, "fs")["share_sessions"] is expected
    # 只在为真时写进文件，写的是布尔值，不是传进来的原样
    assert _saved_client(ws).get("share_sessions") is (True if expected else None)


@pytest.mark.parametrize("value", ["sometimes", 2, ["true"]])
def test_an_unrecognised_share_sessions_is_refused_and_the_client_stays(ws: Path, value: Any) -> None:
    mcp_runtime.upsert_client(ws, {**_SPEC, "share_sessions": True})
    before = _saved_client(ws)

    with pytest.raises(SwitchValueError) as refused:
        mcp_runtime.upsert_client(ws, {**_SPEC, "description": "改过的说明", "share_sessions": value})

    assert str(refused.value) == switch_refusal("share_sessions", value)
    assert _saved_client(ws) == before


def test_list_clients_reports_share_sessions_as_written_by_hand(ws: Path) -> None:
    config_layout.mcp_path(ws).write_text(
        "version: 1\n"
        "clients:\n"
        "  shared:\n"
        "    command: fake-mcp-server\n"
        "    share_sessions: yes\n"
        "  plain:\n"
        "    command: fake-mcp-server\n"
        "  odd:\n"
        "    command: fake-mcp-server\n"
        "    share_sessions: sometimes\n",
        encoding="utf-8",
    )

    listed = {client["id"]: client["share_sessions"] for client in mcp_runtime.list_clients(ws)}

    assert listed == {"odd": False, "plain": False, "shared": True}
    assert mcp_runtime.get_client_spec(ws, "shared")["share_sessions"] is True


def test_the_client_management_tools_pass_share_sessions_through(ws: Path) -> None:
    ctx = _ApprovingCtx(ws)

    async def main() -> list[dict[str, Any]]:
        return [
            await create_or_update_mcp_client({**_SPEC, "share_sessions": True}, ctx),
            await create_or_update_mcp_client({**_SPEC, "description": "again"}, ctx),
            await list_mcp_clients({}, ctx),
        ]

    saved, kept, listed = asyncio.run(main())

    assert saved["ok"] is True
    assert saved["data"]["client"]["share_sessions"] is True
    assert kept["data"]["client"]["share_sessions"] is True
    assert [client["share_sessions"] for client in listed["data"]["clients"]] == [True]


def test_the_client_tool_schema_offers_share_sessions(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)
    spec = next(spec for spec in registry.builtin_specs() if spec["name"] == "create_or_update_mcp_client")

    share = spec["input_schema"]["properties"]["share_sessions"]
    assert share["type"] == "boolean"
    assert "conversation" in share["description"]
    assert "share_sessions" not in spec["input_schema"]["required"]


# --- 工具表与注解 ---


def test_list_tools_uses_the_pool_and_carries_annotations(ws: Path, server: _FakeServer) -> None:
    server.tools = [
        mcp_types.Tool(
            name="read_file",
            description="Read a file",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        mcp_types.Tool(name="write_file", description="Write a file", inputSchema={"type": "object"}),
    ]

    async def main() -> dict[str, Any]:
        listed = await mcp_runtime.list_tools(ws, "fs")
        await mcp_runtime.list_tools(ws, "fs")
        await _call(ws, "read_file")
        return listed

    listed = asyncio.run(main())

    assert server.connects == 1
    assert server.sessions[0].requests == ["tools/list", "tools/list", "read_file"]
    tools = {tool["name"]: tool for tool in listed["tools"]}
    # SDK 序列化出来的那些 None 提示不带出来
    assert tools["read_file"]["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
    assert tools["read_file"]["input_schema"]["properties"]["path"] == {"type": "string"}
    assert "annotations" not in tools["write_file"]


def test_the_registry_marks_only_read_only_mcp_tools_parallel_safe(
    ws: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_runtime, "list_clients", lambda root: [{"id": "fs", "enabled": True}])

    async def list_tools(root: Path, cid: str) -> dict[str, Any]:
        return {"ok": True, "client_id": cid, "tools": [
            {"name": "read", "annotations": {"readOnlyHint": True}},
            {"name": "write", "annotations": {"readOnlyHint": False, "destructiveHint": True}},
            {"name": "plain"},
            {"name": "hinted", "annotations": {"idempotentHint": True, "openWorldHint": False}},
        ]}

    monkeypatch.setattr(mcp_runtime, "list_tools", list_tools)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    assert asyncio.run(registry.load_mcp_tools()) == 4

    assert registry.get_spec("mcp_fs_read")["parallel_safe"] is True
    for name in ("mcp_fs_write", "mcp_fs_plain", "mcp_fs_hinted"):
        assert "parallel_safe" not in registry.get_spec(name)


def test_annotations_reach_the_registry_through_the_pooled_tool_list(ws: Path, server: _FakeServer) -> None:
    server.tools = [
        mcp_types.Tool(
            name="read_file",
            inputSchema={"type": "object"},
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        ),
        mcp_types.Tool(
            name="write_file",
            inputSchema={"type": "object"},
            annotations=mcp_types.ToolAnnotations(destructiveHint=True),
        ),
    ]
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    assert asyncio.run(registry.load_mcp_tools()) == 2

    assert registry.get_spec("mcp_fs_read_file")["parallel_safe"] is True
    assert "parallel_safe" not in registry.get_spec("mcp_fs_write_file")


# --- 结果截断 ---


def test_mcp_results_are_clipped_like_script_results(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_clip = registry_module._clip_result
    text = "x" * (_clip_limit() + 500)
    seen: list[dict[str, Any]] = []

    def spy(response: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(response)
        return real_clip(response, *args, **kwargs)

    response = {
        "ok": False,
        "data": {
            "result": text,
            "mcp_result": {"content": [{"type": "text", "text": text}, {"type": "text", "text": "short"}], "isError": True},
            "client_id": "fs",
            "tool_name": "dump",
            "attachments": [],
        },
        "error": text,
        "attachments": [],
    }

    async def call_tool(root: Path, cid: str, tool: str, args: dict[str, Any], *, scope: str = "") -> dict[str, Any]:
        return response

    monkeypatch.setattr(registry_module, "_clip_result", spy)
    monkeypatch.setattr(mcp_runtime, "call_tool", call_tool)

    result = asyncio.run(registry_module._make_mcp_tool(ws, "fs", "dump")({}, None))

    clipped_text = real_clip({"data": {"result": text}})["data"]["result"]
    assert seen and seen[0] is response
    assert len(clipped_text) < len(text)
    assert result["data"]["result"] == clipped_text
    # isError 时 error 和 mcp_result 里的文本块是同一份正文，工具结束事件会整个带上，跟着一起截
    assert result["error"] == clipped_text
    assert result["data"]["mcp_result"]["content"] == [
        {"type": "text", "text": clipped_text},
        {"type": "text", "text": "short"},
    ]
    assert result["data"]["mcp_result"]["isError"] is True
    assert response["data"]["result"] == text
    assert response["error"] == text


def test_a_short_mcp_result_is_returned_untouched(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "ok": True,
        "data": {"result": "small", "mcp_result": {"content": [{"type": "text", "text": "small"}]}, "attachments": []},
        "error": None,
        "attachments": [],
    }
    scopes: list[str] = []

    async def call_tool(root: Path, cid: str, tool: str, args: dict[str, Any], *, scope: str = "") -> dict[str, Any]:
        scopes.append(scope)
        return response

    monkeypatch.setattr(mcp_runtime, "call_tool", call_tool)

    result = asyncio.run(registry_module._make_mcp_tool(ws, "fs", "dump")({}, _ctx("branch_1", parent_session_id="sess-a")))

    assert result == response
    assert scopes == ["sess-a"]


def test_a_long_mcp_result_reaches_the_caller_clipped_through_execute(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "y" * (_clip_limit() + 10)
    monkeypatch.setattr(mcp_runtime, "list_clients", lambda root: [{"id": "fs", "enabled": True}])

    async def list_tools(root: Path, cid: str) -> dict[str, Any]:
        return {"ok": True, "client_id": cid, "tools": [{"name": "dump"}]}

    async def call_tool(root: Path, cid: str, tool: str, args: dict[str, Any], *, scope: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "data": {"result": text, "mcp_result": {"content": [{"type": "text", "text": text}]}, "attachments": []},
            "error": None,
            "attachments": [],
        }

    monkeypatch.setattr(mcp_runtime, "list_tools", list_tools)
    monkeypatch.setattr(mcp_runtime, "call_tool", call_tool)
    registry = ToolRegistry(workspace_root=ws, tools_dir=ws / "tools")

    async def main() -> dict[str, Any]:
        await registry.load_mcp_tools()
        return await registry.execute(name="mcp_fs_dump", arguments={}, ctx=SimpleNamespace(node_id="chat"))

    result = asyncio.run(main())

    assert result["ok"] is True
    assert result["error"] is None
    assert result["data"]["result"] == registry_module._clip_result({"data": {"result": text}})["data"]["result"]
    assert len(result["data"]["mcp_result"]["content"][0]["text"]) < len(text)


def test_an_mcp_call_that_raises_names_the_exception_and_the_client(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def call_tool(root: Path, cid: str, tool: str, args: dict[str, Any], *, scope: str = "") -> dict[str, Any]:
        raise anyio.ClosedResourceError()

    monkeypatch.setattr(mcp_runtime, "call_tool", call_tool)

    result = asyncio.run(registry_module._make_mcp_tool(ws, "fs", "dump")({}, None))

    assert result["ok"] is False
    # 这类异常没有消息文本，光给 str(e) 模型只看得到一句空的 ERROR
    assert result["error"] == "ClosedResourceError"
    assert (result["mcp_client"], result["mcp_tool"]) == ("fs", "dump")


# --- 事件循环与退出清理 ---


def test_a_session_is_never_handed_to_another_event_loop(ws: Path, server: _FakeServer) -> None:
    first_loop = asyncio.new_event_loop()
    try:
        first_loop.run_until_complete(_call(ws))

        asyncio.run(_call(ws))

        assert server.connects == 2
        assert server.sessions[0].loop is first_loop
        assert server.sessions[1].loop is not first_loop
        assert server.sessions[0].closed is False
        first_loop.run_until_complete(mcp_runtime.retire_sessions(ws))
        assert server.sessions[0].closed is True
    finally:
        first_loop.close()


def test_the_end_of_an_event_loop_closes_its_sessions(ws: Path, server: _FakeServer) -> None:
    asyncio.run(_call(ws))

    assert server.closes == 1


def test_exit_cleanup_closes_sessions_left_on_an_open_loop(ws: Path, server: _FakeServer) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_call(ws))
        assert server.closes == 0

        mcp_runtime._close_pools_at_exit()

        assert server.closes == 1
    finally:
        loop.close()


def test_exit_cleanup_closes_sessions_of_a_loop_running_in_another_thread(ws: Path, server: _FakeServer) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(_call(ws), loop).result(10)
        assert server.closes == 0

        mcp_runtime._close_pools_at_exit()

        assert server.closes == 1
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def test_a_request_queued_just_before_its_session_closes_is_failed_instead_of_sent(
    ws: Path, server: _FakeServer,
) -> None:
    async def late(client: Any) -> Any:
        return await client.call_tool("late", arguments={})

    async def main() -> None:
        await _call(ws, "first")
        pool = _client_pool(ws)
        session = pool._take_idle("", pool.sessions[0].plan.fingerprint)
        pending = asyncio.ensure_future(session.run(late))
        # 请求已经排进队、owner 还没轮到它的时候会话开始关，比如进程退出时收池子
        await asyncio.sleep(0)
        session.close()

        with pytest.raises(RuntimeError, match="was closed"):
            await pending
        assert server.sessions[0].requests == ["first"]

    asyncio.run(main())


def test_open_session_gives_a_one_off_session_outside_the_pool(ws: Path, server: _FakeServer) -> None:
    async def main() -> None:
        async with mcp_runtime.open_session(ws, "fs") as session:
            assert session is server.sessions[0]
        assert server.closes == 1
        await _call(ws)
        assert server.connects == 2

    asyncio.run(main())


# --- 传输层参数（不能改的既有决定） ---


def test_stdio_sessions_inherit_the_engine_environment_and_get_no_read_timeout(
    ws: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MCP_INHERITED", "from-the-engine")
    monkeypatch.setenv("FAKE_MCP_POOL_TOKEN", "secret-value")
    mcp_runtime.upsert_client(ws, {**_SPEC, "env": {"TOKEN": "${FAKE_MCP_POOL_TOKEN}", "MODE": "plain"}})
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params: Any):
        seen["params"] = params
        send, receive = anyio.create_memory_object_stream(0)
        try:
            yield receive, send
        finally:
            send.close()
            receive.close()

    monkeypatch.setattr(mcp_runtime, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_runtime, "ClientSession", _recording_client_session(seen))

    async def main() -> dict[str, Any]:
        await mcp_runtime.list_tools(ws, "fs")
        return await mcp_runtime.call_tool(ws, "fs", "ping", {"a": 1})

    result = asyncio.run(main())

    assert result["data"]["result"] == "ok"
    params = seen["params"]
    assert (params.command, params.args) == ("fake-mcp-server", ["--stdio"])
    # 子进程继承剥掉密钥后的 engine 环境，再叠上配置里解析好的变量
    assert params.env["FAKE_MCP_INHERITED"] == "from-the-engine"
    assert (params.env["TOKEN"], params.env["MODE"]) == ("secret-value", "plain")
    # 没有会话级读超时，单次调用也不带超时
    assert (seen["session_args"], seen["session_kwargs"]) == ((), {})
    assert seen["calls"] == [("ping", (), {"arguments": {"a": 1}})]
    assert seen["initialized"] is True
    assert seen["exited"] is True


def test_stdio_sessions_do_not_inherit_the_secrets_of_the_engine(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 服务端是第三方程序：管理令牌、模型 key、平台令牌和工具子进程一样剥掉，它要用的写进 mcp.yaml 的 env。
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "sk-vendor")
    monkeypatch.setenv("OTHERVENDOR_API_KEY", "sk-other")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "qq-token")
    monkeypatch.setenv("FAKE_MCP_PLAIN", "kept")
    mcp_runtime.upsert_client(ws, {**_SPEC, "env": {"SOMEVENDOR_API_KEY": "${SOMEVENDOR_API_KEY}"}})
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params: Any):
        seen["params"] = params
        send, receive = anyio.create_memory_object_stream(0)
        try:
            yield receive, send
        finally:
            send.close()
            receive.close()

    monkeypatch.setattr(mcp_runtime, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_runtime, "ClientSession", _recording_client_session(seen))

    asyncio.run(mcp_runtime.list_tools(ws, "fs"))

    env = seen["params"].env
    for name in ("CLONOTH_ADMIN_TOKEN", "OTHERVENDOR_API_KEY", "ONEBOT_ACCESS_TOKEN"):
        assert name not in env
    assert "admin-secret" not in env.values()
    assert env["FAKE_MCP_PLAIN"] == "kept"
    # 配置里点名要的照给
    assert env["SOMEVENDOR_API_KEY"] == "sk-vendor"


def test_a_streamable_http_client_made_for_headers_is_closed_with_its_session(
    ws: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MCP_POOL_TOKEN", "t0ken")
    mcp_runtime.upsert_client(ws, {
        "id": "web",
        "transport": "streamable_http",
        "url": "http://127.0.0.1:9/mcp",
        "headers": {"Authorization": "${FAKE_MCP_POOL_TOKEN}"},
    })
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_streamable_http_client(url: str, **kwargs: Any):
        seen["url"] = url
        seen["http_client"] = kwargs.get("http_client")
        send, receive = anyio.create_memory_object_stream(0)
        try:
            yield receive, send, lambda: None
        finally:
            send.close()
            receive.close()

    monkeypatch.setattr(mcp_runtime, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp_runtime, "ClientSession", _recording_client_session(seen))

    async def main() -> None:
        await mcp_runtime.list_tools(ws, "web")
        client = seen["http_client"]
        assert client.headers["Authorization"] == "t0ken"
        assert client.is_closed is False

        await mcp_runtime.retire_sessions(ws, "web")

        assert client.is_closed is True

    asyncio.run(main())

    assert seen["url"] == "http://127.0.0.1:9/mcp"
