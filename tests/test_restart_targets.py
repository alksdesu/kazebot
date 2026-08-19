"""两种重启必须真的分开：只重启 engine 不该把 supervisor 一起带走。

target='engine' 曾经被一个不可达的字面量拦住（`== "engine_DISABLED"` 配 Literal
["engine","all"]），于是「重启引擎」按钮实际执行的是全量重启 —— 连 supervisor、
web 连接和 TUI 一起死，而 os._exit(75) 只在外层 launcher 下才是重启。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_URL = "/v1/admin/restart"
_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
# 下面会把 time.sleep 打成空操作让 deferred 线程立刻跑完，等待本身还得用真的。
_REAL_SLEEP = time.sleep


class _Popen:
    def poll(self) -> None:
        # None = 还活着。engine 重启后的健康检查读这个。
        return None


class _Proc:
    def __init__(self) -> None:
        self.popen = _Popen()


class _FakeProcessManager:
    """够重启端点用的最小替身，记录被调了哪些进程操作。"""

    def __init__(self) -> None:
        self.engines = [_Proc()]
        self._restarting_engine = False
        self._restart_pending = False
        self.calls: list[str] = []

    def stop_engine(self) -> None:
        self.calls.append("stop_engine")
        self.engines = []

    def start_engine(self) -> None:
        self.calls.append("start_engine")
        self.engines = [_Proc()]

    def stop_all(self) -> None:
        self.calls.append("stop_all")


@pytest.fixture(autouse=True)
def _fast_and_safe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    # 两条路径的 deferred 线程都靠 sleep 给 HTTP 响应让路，测试里不必真等。
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _make(tmp_path: Path, pm: Any):
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-restart"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state, process_manager=pm,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    return state, app


def _post(app, body: dict[str, Any]) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(_URL, json=body, headers=_AUTH)

    return asyncio.run(_run())


def _settle(pm: _FakeProcessManager, wanted: str, tries: int = 200) -> None:
    """重启在 daemon 线程里跑，等它落地。"""
    _until(lambda: wanted in pm.calls, tries)


def _until(done: Any, tries: int = 200) -> None:
    for _ in range(tries):
        if done():
            return
        _REAL_SLEEP(0.005)


def _event_types(state: SupervisorState) -> list[str]:
    return [str(e.get("type") or "") for e in state.eventlog.list_all_events()]


class TestEngineOnlyRestart:
    def test_it_reports_the_engine_target_and_not_all(self, tmp_path: Path) -> None:
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        response = _post(app, {"target": "engine", "reason": "test"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scheduled"] is True
        assert body["target"] == "engine"

    def test_it_cycles_only_the_engine_processes(self, tmp_path: Path) -> None:
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")

        assert "stop_engine" in pm.calls
        assert "start_engine" in pm.calls
        assert "stop_all" not in pm.calls

    def test_it_does_not_arm_the_full_restart_marker(self, tmp_path: Path) -> None:
        # data/restart_pending.json 是全量重启留给下次启动的信号，engine 路径不该写它。
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test", "session_id": "s-1"})
        _settle(pm, "start_engine")

        assert not (tmp_path / "data" / "restart_pending.json").exists()
        assert pm._restart_pending is False

    def test_it_clears_the_signal_suppression_flag(self, tmp_path: Path) -> None:
        # 标志留着不清，之后任何 SIGTERM 都会被 supervisor 静默吃掉。
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")
        _until(lambda: pm._restarting_engine is False)

        assert pm._restarting_engine is False

    def test_it_records_completion(self, tmp_path: Path) -> None:
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")
        _until(lambda: "restart_completed" in _event_types(state))

        types = _event_types(state)
        assert "restart_requested" in types
        assert "restart_completed" in types

    def test_without_a_process_manager_it_reports_failure(self, tmp_path: Path) -> None:
        # 假装排上了最糟：运营者以为重启了，其实什么都没发生。
        state, app = _make(tmp_path, None)

        response = _post(app, {"target": "engine", "reason": "test"})

        assert response.status_code == 200, response.text
        assert response.json()["scheduled"] is False
        assert "restart_failed" in _event_types(state)


class TestFullRestartStillWorks:
    def test_it_stops_everything_and_exits_with_the_relaunch_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)
        exits: list[int] = []
        import supervisor.api as api_module

        monkeypatch.setattr(api_module.os, "_exit", lambda code: exits.append(code))

        response = _post(app, {"target": "all", "reason": "test"})
        _settle(pm, "stop_all")
        _until(lambda: bool(exits))

        assert response.json()["target"] == "all"
        assert "stop_all" in pm.calls
        # 75 是外层 launcher 认的重启码；换成别的值就变成纯关机。
        assert exits == [75]

    def test_the_engine_target_no_longer_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)
        exits: list[int] = []
        import supervisor.api as api_module

        monkeypatch.setattr(api_module.os, "_exit", lambda code: exits.append(code))

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")
        # 全量那条路会再睡一轮才 os._exit，多等一会儿才能确认它真的没被走。
        for _ in range(60):
            _REAL_SLEEP(0.005)

        assert exits == []
        assert "stop_all" not in pm.calls
