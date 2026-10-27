"""engine worker 跟上 supervisor 的工具重载序号。

每个 worker 每轮来问一次：脚本目录的序号变了当场重扫，config/mcp.yaml 或显式重载的序号变了在后台重列 MCP 工具。
端到端的几条里 supervisor 是真的，MCP 服务端的列工具表、关会话换成假的，按当时是哪个 worker 记下来。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.api as supervisor_api  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from engine.tool_reload import ToolReloadFollower, fetch_reload_seqs  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.tool_watch import McpConfigWatch, ToolTreeWatch  # noqa: E402
from toolbox.builtins.mcp_clients import create_or_update_mcp_client, delete_mcp_client  # noqa: E402
from toolbox.builtins.tool_manage import reload_tools  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.reload_seqs import ReloadSeqs  # noqa: E402

_WAIT = 5.0
_TOKEN = "test-tool-reload-token"
_URL = "http://supervisor"
_RUN = "run-a"


def _seqs(run_id: str = _RUN, *, scripts: int = 0, config: int = 0, reload: int = 0) -> ReloadSeqs:
    return ReloadSeqs(run_id=run_id, scripts=scripts, mcp_config=config, mcp_reload=reload)


def _answer(status: int, body: Any = None, *, unreadable: bool = False) -> SimpleNamespace:
    def json() -> Any:
        if unreadable:
            raise ValueError("not JSON")
        return body

    return SimpleNamespace(status_code=status, json=json)


async def _until(predicate: Callable[[], bool]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _settled(follower: ToolReloadFollower) -> None:
    """等 follower 在后台起的那次重列跑完。"""
    task = follower._mcp_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), _WAIT)


# --- 序号本身 ---


class TestTheSequences:
    def test_an_answer_reads_back_as_it_was_sent(self) -> None:
        seqs = _seqs(scripts=3, config=2, reload=1)

        assert ReloadSeqs.from_payload(seqs.payload()) == seqs
        assert ReloadSeqs.from_payload({"ok": True, **seqs.payload()}) == seqs

    def test_an_answer_with_only_the_script_counter_is_still_read(self) -> None:
        # 只发 seq 的 supervisor 也答得上来：run_id 和两个 MCP 序号按空串和 0 算
        assert ReloadSeqs.from_payload({"seq": 4}) == ReloadSeqs(scripts=4)

    @pytest.mark.parametrize("payload", [
        None, "seq=1", [1], {}, {"seq": None}, {"seq": "many"}, {"seq": -1}, {"seq": True},
    ])
    def test_an_answer_without_a_usable_script_counter_is_not_read(self, payload: Any) -> None:
        assert ReloadSeqs.from_payload(payload) is None

    def test_unusable_mcp_counters_count_as_zero(self) -> None:
        payload = {"seq": 1, "mcp_config_seq": "x", "mcp_reload_seq": -2}

        assert ReloadSeqs.from_payload(payload) == ReloadSeqs(scripts=1)

    def test_what_counts_as_a_script_change(self) -> None:
        seen = _seqs(scripts=2, config=1)

        assert _seqs(scripts=2, config=5, reload=3).scripts_changed_since(seen) is False
        assert _seqs(scripts=3, config=1).scripts_changed_since(seen) is True
        assert _seqs("run-b", scripts=2, config=1).scripts_changed_since(seen) is True
        assert seen.scripts_changed_since(None) is True

    def test_what_counts_as_an_mcp_change(self) -> None:
        seen = _seqs(scripts=2, config=1, reload=1)

        assert _seqs(scripts=9, config=1, reload=1).mcp_changed_since(seen) is False
        assert _seqs(scripts=2, config=2, reload=1).mcp_changed_since(seen) is True
        assert _seqs(scripts=2, config=1, reload=2).mcp_changed_since(seen) is True
        assert _seqs("run-b", scripts=2, config=1, reload=1).mcp_changed_since(seen) is True
        assert seen.mcp_changed_since(None) is True

    @pytest.mark.parametrize(("offered", "covered", "reconnect"), [
        pytest.param(_seqs(config=2, reload=1), True, False, id="the-same"),
        pytest.param(_seqs(config=1, reload=0), True, False, id="older"),
        pytest.param(_seqs(config=3, reload=1), False, False, id="config-changed"),
        pytest.param(_seqs(config=2, reload=2), False, True, id="explicit-reload"),
        pytest.param(_seqs(config=1, reload=2), False, True, id="explicit-reload-with-an-older-config"),
        pytest.param(_seqs("run-b", config=2, reload=1), False, True, id="supervisor-restarted"),
    ])
    def test_how_far_a_worker_has_to_go_to_follow(
        self, offered: ReloadSeqs, covered: bool, reconnect: bool,
    ) -> None:
        applied = _seqs(config=2, reload=1)

        assert offered.mcp_covered_by(applied) is covered
        if not covered:
            # supervisor 重启过的那组比不出先后：中间可能漏了一次显式重载，只能全部重来
            assert offered.mcp_reconnect_after(applied) is reconnect

    def test_nothing_applied_yet_means_a_full_reconnect(self) -> None:
        assert _seqs().mcp_covered_by(None) is False
        assert _seqs().mcp_reconnect_after(None) is True

    def test_merging_keeps_the_newest_of_each_counter_within_one_run(self) -> None:
        ours = _seqs(scripts=3, config=1, reload=2)

        assert ours.merged_with(_seqs(scripts=1, config=4, reload=2)) == _seqs(scripts=3, config=4, reload=2)
        assert ours.merged_with(None) == ours
        assert ours.merged_with(_seqs("run-old", scripts=9, config=9, reload=9)) == ours


# --- follower：supervisor 和注册表都是假的 ---


class _Supervisor:
    """GET /v1/tools/reload-seq 的假应答：回 answer，answer 是异常就抛出来。"""

    def __init__(self, seqs: ReloadSeqs) -> None:
        self.answer: Any = _answer(200, seqs.payload())
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def serve(self, seqs: ReloadSeqs) -> None:
        self.answer = _answer(200, seqs.payload())

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.asked.append((url, kwargs))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _Registry:
    """记下重扫和重列；给了 gate 就让重列卡在上面，给了 reload_error / sync_error 就抛出来。"""

    def __init__(self) -> None:
        self.reloads = 0
        self.synced: list[ReloadSeqs] = []
        self.cancelled: list[ReloadSeqs] = []
        self.gate: asyncio.Event | None = None
        self.reload_error: Exception | None = None
        self.sync_error: Exception | None = None

    def reload(self) -> int:
        self.reloads += 1
        if self.reload_error is not None:
            raise self.reload_error
        return 40 + self.reloads

    async def sync_mcp_tools(self, seqs: ReloadSeqs) -> bool:
        self.synced.append(seqs)
        if self.sync_error is not None:
            raise self.sync_error
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.append(seqs)
                raise
        return True

    def mcp_tool_count(self) -> int:
        return 2


class TestTheFollower:
    def test_the_sequences_it_started_from_change_nothing(self) -> None:
        async def main() -> None:
            seen = _seqs(scripts=2, config=1)
            registry, supervisor = _Registry(), _Supervisor(seen)
            follower = ToolReloadFollower(registry, supervisor, _URL, seen=seen)

            for _ in range(3):
                assert await follower.poll() is None
            await _settled(follower)

            assert (registry.reloads, registry.synced) == (0, [])
            assert follower._mcp_task is None
            url, kwargs = supervisor.asked[0]
            assert url == f"{_URL}/v1/tools/reload-seq"
            # 每轮主循环都问：supervisor 半天不回时宁可这一轮不跟，也不能拖住领任务
            assert kwargs["timeout"] > 0

        asyncio.run(main())

    def test_a_script_change_is_rescanned_on_the_spot_and_once(self) -> None:
        async def main() -> None:
            registry = _Registry()
            follower = ToolReloadFollower(registry, _Supervisor(_seqs(scripts=2)), _URL, seen=_seqs(scripts=1))

            assert await follower.poll() == 41
            assert await follower.poll() is None
            await _settled(follower)

            assert registry.reloads == 1
            assert registry.synced == []

        asyncio.run(main())

    @pytest.mark.parametrize("latest", [
        pytest.param(_seqs(config=1), id="config-file-changed"),
        pytest.param(_seqs(scripts=1, reload=1), id="explicit-reload"),
    ])
    def test_an_mcp_change_is_relisted_in_the_background_once(self, latest: ReloadSeqs) -> None:
        async def main() -> None:
            registry = _Registry()
            follower = ToolReloadFollower(registry, _Supervisor(latest), _URL, seen=_seqs())

            await follower.poll()
            await _settled(follower)
            await follower.poll()
            await _settled(follower)

            assert registry.synced == [latest]

        asyncio.run(main())

    def test_polling_never_waits_for_a_relist_and_only_one_runs_at_a_time(self) -> None:
        async def main() -> None:
            registry, supervisor = _Registry(), _Supervisor(_seqs(config=1))
            registry.gate = asyncio.Event()
            follower = ToolReloadFollower(registry, supervisor, _URL, seen=_seqs())

            await asyncio.wait_for(follower.poll(), _WAIT)
            await _until(lambda: registry.synced == [_seqs(config=1)])

            # 卡着的这次跑完之前又改了两次：不另起一个，只留下最新的那组；脚本照样当场重扫
            supervisor.serve(_seqs(config=2))
            assert await asyncio.wait_for(follower.poll(), _WAIT) is None
            supervisor.serve(_seqs(scripts=1, config=3))
            assert await asyncio.wait_for(follower.poll(), _WAIT) == 41
            await asyncio.sleep(0.05)
            assert registry.synced == [_seqs(config=1)]

            registry.gate.set()
            await _settled(follower)
            await follower.poll()
            await _settled(follower)
            await follower.poll()
            await _settled(follower)

            assert registry.synced == [_seqs(config=1), _seqs(scripts=1, config=3)]

        asyncio.run(main())

    def test_a_relist_still_waiting_starts_even_when_the_supervisor_stops_answering(self) -> None:
        async def main() -> None:
            registry, supervisor = _Registry(), _Supervisor(_seqs(config=1))
            registry.gate = asyncio.Event()
            follower = ToolReloadFollower(registry, supervisor, _URL, seen=_seqs())
            await follower.poll()
            await _until(lambda: len(registry.synced) == 1)
            supervisor.serve(_seqs(config=2))
            await follower.poll()

            registry.gate.set()
            await _settled(follower)
            supervisor.answer = httpx.ConnectError("supervisor restarting")
            assert await follower.poll() is None
            await _settled(follower)

            assert registry.synced == [_seqs(config=1), _seqs(config=2)]

        asyncio.run(main())

    @pytest.mark.parametrize("failure", [
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
        pytest.param(httpx.ReadTimeout("slow"), id="timed-out"),
        pytest.param(_answer(503, {"detail": "starting"}), id="error-status"),
        pytest.param(_answer(200, {"ok": True}), id="no-counter"),
        pytest.param(_answer(200, ["seq", 9]), id="not-a-mapping"),
        pytest.param(_answer(200, unreadable=True), id="not-json"),
    ])
    def test_an_unanswered_poll_keeps_what_it_had_seen(self, failure: Any) -> None:
        async def main() -> None:
            seen = _seqs(scripts=1, config=1)
            registry, supervisor = _Registry(), _Supervisor(seen)
            follower = ToolReloadFollower(registry, supervisor, _URL, seen=seen)
            supervisor.answer = failure

            assert await follower.poll() is None

            # 再问到时序号没变：中间那轮没问到，不等于要从头再来一遍
            supervisor.serve(seen)
            assert await follower.poll() is None
            await _settled(follower)
            assert (registry.reloads, registry.synced) == (0, [])

            supervisor.serve(_seqs(scripts=2, config=1))
            assert await follower.poll() == 41

        asyncio.run(main())

    def test_a_supervisor_restart_makes_it_follow_everything(self) -> None:
        async def main() -> None:
            registry = _Registry()
            restarted = _seqs("run-b")
            follower = ToolReloadFollower(
                registry, _Supervisor(restarted), _URL, seen=_seqs(scripts=5, config=3, reload=1),
            )

            # 序号从头数了，比大小比不出先后：脚本重扫，MCP 交给注册表按接不上的历史处理
            assert await follower.poll() == 41
            await _settled(follower)

            assert registry.synced == [restarted]

        asyncio.run(main())

    def test_without_a_starting_point_the_first_answer_is_followed_in_full(self) -> None:
        async def main() -> None:
            registry = _Registry()
            # 启动时没问到序号
            follower = ToolReloadFollower(registry, _Supervisor(_seqs()), _URL, seen=None)

            assert await follower.poll() == 41
            await _settled(follower)

            assert registry.synced == [_seqs()]

        asyncio.run(main())

    @pytest.mark.parametrize("failing", ["rescan", "relist", "relist-unsupported"])
    def test_a_failure_is_logged_once_and_waits_for_the_next_change(
        self, failing: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="engine.tool_reload")
        registry: Any = _Registry()
        if failing == "rescan":
            registry.reload_error = RuntimeError("script tree unreadable")
            latest = _seqs(scripts=1)
        elif failing == "relist":
            registry.sync_error = RuntimeError("MCP listing exploded")
            latest = _seqs(config=1)
        else:
            # 插件包过一层的注册表不一定有这个方法
            registry = SimpleNamespace(reload=lambda: 1)
            latest = _seqs(config=1)

        async def main() -> None:
            follower = ToolReloadFollower(registry, _Supervisor(latest), _URL, seen=_seqs())
            for _ in range(3):
                assert await follower.poll() is None
                await _settled(follower)

        asyncio.run(main())

        warnings = [record for record in caplog.records if record.name == "engine.tool_reload"]
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        assert warnings[0].levelno == logging.WARNING
        if failing == "rescan":
            assert registry.reloads == 1
        elif failing == "relist":
            assert registry.synced == [latest]

    def test_closing_cancels_a_running_relist_and_starts_no_other(self) -> None:
        async def main() -> None:
            registry, supervisor = _Registry(), _Supervisor(_seqs(config=1))
            registry.gate = asyncio.Event()
            follower = ToolReloadFollower(registry, supervisor, _URL, seen=_seqs())
            await follower.poll()
            await _until(lambda: registry.synced == [_seqs(config=1)])
            supervisor.serve(_seqs(config=2))
            await follower.poll()

            await follower.aclose()

            assert registry.cancelled == [_seqs(config=1)]
            assert follower._mcp_task is None
            await asyncio.sleep(0.05)
            assert registry.synced == [_seqs(config=1)]

        asyncio.run(main())

    def test_closing_with_nothing_running_is_fine(self) -> None:
        async def main() -> None:
            follower = ToolReloadFollower(_Registry(), _Supervisor(_seqs()), _URL, seen=_seqs())

            await follower.aclose()
            await follower.aclose()

        asyncio.run(main())


# --- worker_loop 怎么接上 follower ---


class TestTheWorkerLoop:
    def test_the_loop_follows_from_the_sequences_it_loaded_with_and_stops_a_running_relist_on_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from engine import runner

        payload = _seqs(scripts=3, config=1).payload()
        events: list[tuple[Any, ...]] = []
        gates: dict[str, asyncio.Event] = {}

        class FakeSupervisor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeSupervisor":
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def get(self, url: str, **kwargs: Any) -> SimpleNamespace:
                if url.endswith("/v1/tools/reload-seq"):
                    return _answer(200, dict(payload))
                return _answer(204, {})

            async def post(self, url: str, **kwargs: Any) -> SimpleNamespace:
                return _answer(200, {})

        class FakeTools:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def load_mcp_tools(self, *, seqs: Any = None) -> int:
                events.append(("load", seqs))
                return 0

            def reload(self) -> int:
                events.append(("reload",))
                return 7

            async def sync_mcp_tools(self, seqs: ReloadSeqs) -> bool:
                events.append(("sync", seqs))
                try:
                    await gates["relist"].wait()
                except asyncio.CancelledError:
                    events.append(("sync cancelled", seqs))
                    raise
                return True

            def mcp_tool_count(self) -> int:
                return 0

        async def ready(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        # 整个 httpx.AsyncClient 换成假的：连 supervisor 的、连模型的客户端都拿到它，谁也不出网
        monkeypatch.setattr(runner.httpx, "AsyncClient", FakeSupervisor)
        monkeypatch.setattr(runner, "ToolRegistry", FakeTools)
        monkeypatch.setattr(runner, "wait_supervisor", ready)
        monkeypatch.setattr(runner, "_register_engine", ready)
        monkeypatch.setattr(runner, "_flush_outbox", ready)
        monkeypatch.setattr(runner, "load_runtime_config", lambda _root: {"engine": {"poll_interval_sec": 0.1}})

        def count(kind: str) -> int:
            return sum(1 for event in events if event[0] == kind)

        async def scenario() -> None:
            gates["relist"] = asyncio.Event()
            worker = asyncio.create_task(runner.worker_loop(supervisor_url=_URL, workspace_root=tmp_path))
            try:
                await _until(lambda: count("load") == 1)
                # 加载拿的是加载之前问到的那组序号，follower 也从这组起跟
                assert events[0] == ("load", _seqs(scripts=3, config=1))
                await asyncio.sleep(0.35)
                assert events == [("load", _seqs(scripts=3, config=1))]

                payload["seq"] = 4
                await _until(lambda: count("reload") == 1)
                payload["mcp_config_seq"] = 2
                await _until(lambda: count("sync") == 1)
                assert events[-1] == ("sync", _seqs(scripts=4, config=2))

                # 重列卡着，主循环照样一轮轮问序号
                payload["seq"] = 5
                await _until(lambda: count("reload") == 2)
                assert count("sync") == 1
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

            assert ("sync cancelled", _seqs(scripts=4, config=2)) in events

        asyncio.run(scenario())


# --- 端到端：真的 supervisor，两个 worker 各有各的注册表 ---


_WORKER: contextvars.ContextVar[str] = contextvars.ContextVar("worker", default="")

_FS = {"transport": "stdio", "command": "fs-server"}
_WEB = {"transport": "stdio", "command": "web-server"}


class _FakeMcp:
    """替掉 mcp_runtime 的列工具表和退役会话，按当时是哪个 worker 记下来；hold 里的客户端列工具表时卡到放行。"""

    def __init__(self) -> None:
        self.listed: list[tuple[str, str]] = []
        self.retired: list[tuple[str, str | None]] = []
        self.hold: dict[str, asyncio.Event] = {}

    async def list_tools(self, workspace_root: Path, client_id: str, *, scope: str = "") -> dict[str, Any]:
        self.listed.append((_WORKER.get(), client_id))
        gate = self.hold.get(client_id)
        if gate is not None:
            await gate.wait()
        tool = {"name": "ping", "description": "", "input_schema": {"type": "object"}}
        return {"ok": True, "client_id": client_id, "tools": [tool]}

    async def retire_sessions(self, workspace_root: Path, client_id: str | None = None) -> None:
        self.retired.append((_WORKER.get(), client_id))

    def take(self) -> tuple[list[tuple[str, str]], list[tuple[str, str | None]]]:
        """到现在为止记下的，取走就清空。"""
        taken = (self.listed, self.retired)
        self.listed, self.retired = [], []
        return taken


class _Approving:
    """放行一切的工具上下文：mcp_clients 的工具只用到工作区、送审和查取消。"""

    def __init__(self, root: Path) -> None:
        self.workspace_root = root

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _write_clients(root: Path, **clients: dict[str, Any]) -> None:
    """整份写 config/mcp.yaml，好比在控制台或共享组别的实例里改：哪个 worker 都没收到通知。"""
    text = yaml.safe_dump({"version": 1, "clients": clients}, sort_keys=True)
    config_layout.write_config_text(config_layout.mcp_path(root), text)


def _break(root: Path) -> None:
    config_layout.write_config_text(config_layout.mcp_path(root), "clients: [unclosed\n")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    _write_clients(workspace, fs=_FS, web=_WEB)
    return workspace


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMcp:
    fake = _FakeMcp()
    monkeypatch.setattr(mcp_runtime, "list_tools", fake.list_tools)
    monkeypatch.setattr(mcp_runtime, "retire_sessions", fake.retire_sessions)
    return fake


@pytest.fixture
def supervisor_app(root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # 真的两个 watch，只是把扫描间隔压到 0：每次来问都重新看一眼
    monkeypatch.setattr(supervisor_api, "ToolTreeWatch", functools.partial(ToolTreeWatch, min_interval=0.0))
    monkeypatch.setattr(supervisor_api, "McpConfigWatch", functools.partial(McpConfigWatch, min_interval=0.0))
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    return create_app(
        state=SupervisorState(
            workspace_root=root,
            eventlog=EventLog(root / "data" / "events.jsonl", run_id=_RUN),
            policy=PolicyEngine(workspace_root=root),
        ),
        process_manager=None,
        config_store=ConfigStore(path=root / "config" / "models.yaml", workspace_root=root),
    )


@asynccontextmanager
async def _connected(app: Any):
    """engine 连 supervisor 的那种客户端：带着管理员令牌。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=_URL, headers={"Authorization": f"Bearer {_TOKEN}"},
    ) as http:
        yield http


class _Worker:
    """一个 engine worker 里和工具重载有关的那部分：自己的注册表、自己的 follower。"""

    def __init__(self, name: str, registry: ToolRegistry, follower: ToolReloadFollower, http: Any) -> None:
        self.name = name
        self.registry = registry
        self.follower = follower
        self.http = http

    @classmethod
    async def start(cls, name: str, root: Path, http: Any) -> _Worker:
        """照 worker_loop 的顺序：先问序号，再加载 MCP 工具，follower 从这组序号起跟。"""
        token = _WORKER.set(name)
        try:
            registry = ToolRegistry(workspace_root=root, tools_dir=root / "tools")
            seqs = await fetch_reload_seqs(http, _URL)
            await registry.load_mcp_tools(seqs=seqs)
        finally:
            _WORKER.reset(token)
        return cls(name, registry, ToolReloadFollower(registry, http, _URL, seen=seqs), http)

    async def poll(self) -> int | None:
        """主循环的一轮，连同它在后台起的重列一起等完。"""
        token = _WORKER.set(self.name)
        try:
            reloaded = await self.follower.poll()
        finally:
            _WORKER.reset(token)
        await _settled(self.follower)
        return reloaded

    async def run(
        self, tool: Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]], args: dict[str, Any], ctx: Any,
    ) -> dict[str, Any]:
        """在这个 worker 里调一个工具。"""
        token = _WORKER.set(self.name)
        try:
            return await tool(args, ctx)
        finally:
            _WORKER.reset(token)

    async def reload_tools(self) -> dict[str, Any]:
        ctx = SimpleNamespace(registry=self.registry, http=self.http, supervisor_url=_URL)
        return await self.run(reload_tools, {}, ctx)

    def mcp_tools(self) -> set[str]:
        return {spec["name"] for spec in self.registry.list_specs() if spec["name"].startswith("mcp_")}


class TestEveryWorkerFollows:
    def test_config_edits_and_an_explicit_reload_reach_every_worker_and_go_only_as_far_as_needed(
        self, root: Path, supervisor_app: Any, fake_mcp: _FakeMcp,
    ) -> None:
        async def main() -> None:
            async with _connected(supervisor_app) as http:
                w1 = await _Worker.start("w1", root, http)
                w2 = await _Worker.start("w2", root, http)
                assert fake_mcp.take() == (
                    [("w1", "fs"), ("w1", "web"), ("w2", "fs"), ("w2", "web")],
                    [("w1", None), ("w2", None)],
                )
                assert w1.mcp_tools() == w2.mcp_tools() == {"mcp_fs_ping", "mcp_web_ping"}

                for worker in (w1, w2, w1, w2):
                    assert await worker.poll() is None
                assert fake_mcp.take() == ([], [])

                # 控制台、共享组别的实例改了 fs 的启动参数：两个 worker 都只重列 fs，web 的会话不动
                _write_clients(root, fs={**_FS, "args": ["--other"]}, web=_WEB)
                assert await w1.poll() is None
                assert await w2.poll() is None
                assert fake_mcp.take() == ([("w1", "fs"), ("w2", "fs")], [])

                # 在 w1 上用工具加一个客户端：w2 没经手，照样列上
                saved = await w1.run(
                    create_or_update_mcp_client, {"id": "db", "transport": "stdio", "command": "db-server"},
                    _Approving(root),
                )
                assert saved["ok"] is True, saved
                await w1.poll()
                await w2.poll()
                assert fake_mcp.take() == ([("w1", "db"), ("w2", "db")], [])
                assert w1.mcp_tools() == w2.mcp_tools() == {"mcp_db_ping", "mcp_fs_ping", "mcp_web_ping"}

                # 在 w1 上 reload_tools：w1 当场全部重开，w2 跟着全部重开，w1 不再来第二遍
                result = await w1.reload_tools()
                assert result["ok"] is True, result
                assert result["data"]["other_workers_notified"] is True
                assert result["data"]["mcp_tools"] == 3
                assert fake_mcp.take() == ([("w1", "db"), ("w1", "fs"), ("w1", "web")], [("w1", None)])

                assert isinstance(await w1.poll(), int)
                assert fake_mcp.take() == ([], [])
                assert isinstance(await w2.poll(), int)
                assert fake_mcp.take() == ([("w2", "db"), ("w2", "fs"), ("w2", "web")], [("w2", None)])

                # 在 w2 上删掉 web：两边都摘掉它的工具，收掉它的会话
                deleted = await w2.run(delete_mcp_client, {"id": "web"}, _Approving(root))
                assert deleted["ok"] is True, deleted
                await w1.poll()
                await w2.poll()
                assert fake_mcp.take() == ([], [("w1", "web"), ("w2", "web")])
                assert w1.mcp_tools() == w2.mcp_tools() == {"mcp_db_ping", "mcp_fs_ping"}

        asyncio.run(main())

    def test_a_broken_file_keeps_every_worker_on_the_last_good_clients_until_it_is_fixed(
        self, root: Path, supervisor_app: Any, fake_mcp: _FakeMcp,
    ) -> None:
        async def main() -> None:
            async with _connected(supervisor_app) as http:
                w1 = await _Worker.start("w1", root, http)
                w2 = await _Worker.start("w2", root, http)
                fake_mcp.take()

                _break(root)
                assert await w1.poll() is None
                assert await w2.poll() is None
                assert fake_mcp.take() == ([], [])
                assert w1.mcp_tools() == w2.mcp_tools() == {"mcp_fs_ping", "mcp_web_ping"}
                assert w1.registry.mcp_config_error().startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")

                # 坏着的时候 reload_tools 只重扫脚本：重开也只是按旧配置再连一遍，白丢各对话的会话状态
                result = await w2.reload_tools()
                assert result["ok"] is False
                assert "沿用上一份能用的" in result["error"]
                assert isinstance(await w1.poll(), int)
                assert isinstance(await w2.poll(), int)
                assert fake_mcp.take() == ([], [])
                answered = ReloadSeqs.from_payload((await http.get(f"{_URL}/v1/tools/reload-seq")).json())
                assert answered is not None and answered.mcp_reload == 0

                # 修好时连同改了的 web 一起写回：两边都只重列 web
                _write_clients(root, fs=_FS, web={**_WEB, "args": ["--fixed"]})
                await w1.poll()
                await w2.poll()
                assert fake_mcp.take() == ([("w1", "web"), ("w2", "web")], [])
                assert w1.registry.mcp_config_error() == w2.registry.mcp_config_error() == ""

        asyncio.run(main())


class TestTheStartingSnapshot:
    def test_a_change_made_while_a_client_is_being_listed_is_picked_up_by_the_next_sync(
        self, root: Path, fake_mcp: _FakeMcp,
    ) -> None:
        registry = ToolRegistry(workspace_root=root, tools_dir=root / "tools")

        async def main() -> None:
            fake_mcp.hold["fs"] = asyncio.Event()
            loading = asyncio.create_task(registry.load_mcp_tools(seqs=_seqs()))
            await _until(lambda: bool(fake_mcp.listed))
            _write_clients(root, fs={**_FS, "args": ["--late"]}, web=_WEB)
            fake_mcp.hold.pop("fs").set()
            await asyncio.wait_for(loading, _WAIT)
            fake_mcp.take()

            # 记下的是开始列之前的启动参数：列的途中改的那一下，下一次对齐看得出来
            assert await registry.sync_mcp_tools(_seqs(config=1)) is True
            assert fake_mcp.take() == ([("", "fs")], [])

        asyncio.run(main())
