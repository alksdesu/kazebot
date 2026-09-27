"""两种重启必须真的分开：只重启 engine 不该把 supervisor 一起带走。

target='engine' 曾经被一个不可达的字面量拦住（`== "engine_DISABLED"` 配 Literal
["engine","all"]），于是「重启引擎」按钮实际执行的是全量重启 —— 连 supervisor、
web 连接和 TUI 一起死，而 os._exit(75) 只在外层 launcher 下才是重启。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
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
from supervisor.process_manager import ManagedProcess, ProcessManager, WorkerHealth  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_URL = "/v1/admin/restart"
_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
# 下面会把 time.sleep 打成空操作让 deferred 线程立刻跑完，等待本身还得用真的。
_REAL_SLEEP = time.sleep


class _Popen:
    pid = 4242

    def __init__(self) -> None:
        # None = 还活着。engine 重启后的健康检查读这个。
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


class _Proc:
    def __init__(self) -> None:
        self.popen = _Popen()


class _DeadPopen:
    pid = 4243

    def poll(self) -> int:
        # 起来就退：重启端点那轮健康检查读到的就是这个。
        return 1


class _DeadProc:
    def __init__(self) -> None:
        self.popen = _DeadPopen()


class _FakeProcessManager:
    """够重启端点用的最小替身，记录被调了哪些进程操作。"""

    def __init__(self) -> None:
        self.engines = [_Proc()]
        self._restarting_engine = False
        self._restart_pending = False
        self.engine_restart_lock = threading.Lock()
        self.calls: list[str] = []

    def stop_engine(self) -> None:
        self.calls.append("stop_engine")
        self.engines = []

    def start_engine(self) -> None:
        self.calls.append("start_engine")
        self.engines = [_Proc()]

    def clear_given_up(self) -> None:
        self.calls.append("clear_given_up")

    def stop_all(self) -> None:
        self.calls.append("stop_all")


@pytest.fixture(autouse=True)
def _fast_and_safe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    # 两条路径的 deferred 线程都靠 sleep 给 HTTP 响应让路，测试里不必真等。
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    # 重启会把工作区变量钉成 tmp_path：先经 monkeypatch 登记一次，别漏给后面的用例。
    for name in ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


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


def _event_texts(state: SupervisorState) -> list[str]:
    return [str((e.get("payload") or {}).get("text") or "") for e in state.eventlog.list_all_events()]


def _event_blob(state: SupervisorState) -> str:
    """事件类型和给用户看的文本拼一起，三条退出路径各自的痕迹都能在里面找到。"""
    return "\n".join(
        f"{event.get('type')} {(event.get('payload') or {}).get('text') or ''}"
        for event in state.eventlog.list_all_events()
    )


class _StillbornProcessManager(_FakeProcessManager):
    """新 engine 刚拉起来就退，重启端点走的是「失败」那条返回。"""

    def start_engine(self) -> None:
        self.calls.append("start_engine")
        self.engines = [_DeadProc()]


class _BrokenProcessManager(_FakeProcessManager):
    """停不掉旧 engine：整段重启在半路抛出去。"""

    def stop_engine(self) -> None:
        self.calls.append("stop_engine")
        raise OSError("旧 engine 停不掉")


class _BlockingProcessManager(_FakeProcessManager):
    """第一次重启卡在 stop_engine 上，直到测试放行。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.let_go = threading.Event()

    def stop_engine(self) -> None:
        self.calls.append("stop_engine")
        self.entered.set()
        self.let_go.wait(10)
        self.engines = []


def _break_orphan_sweep(state: SupervisorState) -> None:
    def _boom() -> int:
        raise RuntimeError("孤儿清理炸了")

    state.cancel_orphaned_tasks = _boom


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

    def test_it_clears_the_give_up_state_between_the_stop_and_the_start(self, tmp_path: Path) -> None:
        # 看门狗停手之后人工重启：不清就等于关掉了新 engine 的看门狗，它再崩没人拉。
        # 清在 start 之后也不行 —— 那一瞬间崩掉的进程照样补不回来。
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")

        assert pm.calls == ["stop_engine", "clear_given_up", "start_engine"]

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

    def test_a_stillborn_engine_still_clears_the_suppression_flag(self, tmp_path: Path) -> None:
        # 标志留在 True 的代价有两层：看门狗从此每轮直接 continue，engine 起不来的时候
        # 恰恰没人再去拉它；信号 handler 也会一直吞掉 SIGTERM/SIGINT，supervisor 只能被 kill -9。
        pm = _StillbornProcessManager()
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: any("重启失败" in text for text in _event_texts(state)))
        assert any("重启失败" in text for text in _event_texts(state))

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


class TestTheSuppressionFlagIsAlwaysReleased:
    """标志留在 True 的代价有两层：看门狗每轮直接 continue，SIGTERM/SIGINT 也一直被 handler 吞掉。

    抑制是给重启那几秒用的，三条退出路径里漏掉任何一条都等于把它变成永久的。
    """

    _PATHS = [
        pytest.param(_FakeProcessManager, "restart_completed", None, id="完成"),
        pytest.param(_StillbornProcessManager, "进程启动后立即退出", None, id="起来就退"),
        pytest.param(_BrokenProcessManager, "旧 engine 停不掉", None, id="半路抛异常"),
        pytest.param(_FakeProcessManager, "restart_completed", _break_orphan_sweep, id="清孤儿出错"),
    ]

    @pytest.mark.parametrize(("factory", "marker", "prepare"), _PATHS)
    def test_the_path_reports_and_then_releases_the_flag(
        self, tmp_path: Path, factory: Any, marker: str, prepare: Any,
    ) -> None:
        pm = factory()
        state, app = _make(tmp_path, pm)
        if prepare is not None:
            prepare(state)

        _post(app, {"target": "engine", "reason": "test"})
        # 标志一开始就是 False，不先等这条路真的走完，下面那句断言什么都证明不了。
        # 静默放弃同样是故障：界面上这次重启会一直停在「已排上」。
        _until(lambda: marker in _event_blob(state))
        assert marker in _event_blob(state), "这条路径没有留下任何交代"

        _until(lambda: not pm.engine_restart_lock.locked())
        assert pm._restarting_engine is False

    @pytest.mark.parametrize(("factory", "marker", "prepare"), _PATHS)
    def test_the_next_restart_can_get_in(
        self, tmp_path: Path, factory: Any, marker: str, prepare: Any,
    ) -> None:
        # 锁漏放一次，这台机器此后再也重启不了 engine —— 而且只回一句「已在进行中」。
        pm = factory()
        state, app = _make(tmp_path, pm)
        if prepare is not None:
            prepare(state)
        _post(app, {"target": "engine", "reason": "first"})
        _until(lambda: marker in _event_blob(state))
        _until(lambda: not pm.engine_restart_lock.locked())

        assert not pm.engine_restart_lock.locked()
        assert _post(app, {"target": "engine", "reason": "second"}).json()["scheduled"] is True
        _until(lambda: not pm.engine_restart_lock.locked())

    def test_a_failed_orphan_sweep_does_not_sink_the_restart(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # engine 已经起来了，清理出错不算重启失败：记进日志，照常报完成。
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)
        _break_orphan_sweep(state)

        with caplog.at_level(logging.ERROR, logger="supervisor.api"):
            _post(app, {"target": "engine", "reason": "test"})
            _until(lambda: "restart_completed" in _event_types(state))

        assert "restart_completed" in _event_types(state)
        assert any("清理孤儿 task 失败" in record.getMessage() for record in caplog.records)
        _until(lambda: not pm.engine_restart_lock.locked())
        assert pm._restarting_engine is False
        assert not pm.engine_restart_lock.locked()

    def test_the_failed_sweep_is_recorded_with_its_traceback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 只留一行「清理失败」而不带堆栈的话，排障时不知道是谁抛的。
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)
        _break_orphan_sweep(state)

        with caplog.at_level(logging.ERROR, logger="supervisor.api"):
            _post(app, {"target": "engine", "reason": "test"})
            _until(lambda: "restart_completed" in _event_types(state))

        swept = [r for r in caplog.records if "清理孤儿 task 失败" in r.getMessage()]
        assert swept and swept[0].exc_info is not None

    def test_no_orphan_cleanup_event_is_written_when_the_sweep_fails(
        self, tmp_path: Path,
    ) -> None:
        # 当成 0 个继续，就不该留下一条说清过的记录。
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)
        _break_orphan_sweep(state)

        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: "restart_completed" in _event_types(state))

        assert "orphan_cleanup" not in _event_types(state)


def _restart_failures(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("reason") or "")
        for event in state.eventlog.list_all_events()
        if event.get("type") == "restart_failed"
    ]


def _announcements(state: SupervisorState) -> list[str]:
    return [
        text for text in _event_texts(state) if "已触发" in text
    ]


class TestOnlyOneEngineRestartAtATime:
    """两个重启撞在一起，先收尾的那个会在另一个停起到一半时放开信号抑制。

    那一刻 engine 已经停了、新的还没起，SIGTERM 不再被吞 —— supervisor 跟着一起走。
    """

    @pytest.fixture
    def stuck(self, tmp_path: Path):
        pm = _BlockingProcessManager()
        state, app = _make(tmp_path, pm)
        assert _post(app, {"target": "engine", "reason": "first"}).json()["scheduled"] is True
        pm.entered.wait(10)

        yield pm, state, app

        pm.let_go.set()
        # 那个线程还要往 tmp_path 下的事件日志里写，别让它活过这个用例。
        _until(lambda: not pm.engine_restart_lock.locked())

    def test_the_second_request_is_turned_away(self, stuck: Any) -> None:
        _pm, _state, app = stuck

        response = _post(app, {"target": "engine", "reason": "second", "session_id": "s-2"})

        assert response.json()["scheduled"] is False
        assert response.json()["target"] == "engine"

    def test_the_refusal_says_why(self, stuck: Any) -> None:
        _pm, state, app = stuck

        _post(app, {"target": "engine", "reason": "second", "session_id": "s-2"})

        assert _restart_failures(state) == ["engine restart already in progress"]

    def test_the_refused_request_announces_nothing(self, stuck: Any) -> None:
        # 报了「已触发」又没触发，对面那条会话就在等一个永远不来的结果。
        _pm, state, app = stuck

        _post(app, {"target": "engine", "reason": "second", "session_id": "s-2"})

        assert _announcements(state) == []

    def test_the_refused_request_touches_no_process(self, stuck: Any) -> None:
        pm, _state, app = stuck

        _post(app, {"target": "engine", "reason": "second"})

        assert pm.calls == ["stop_engine"]

    def test_the_first_one_finishes_once_it_is_let_go(self, stuck: Any) -> None:
        pm, state, app = stuck
        _post(app, {"target": "engine", "reason": "second"})

        pm.let_go.set()
        _until(lambda: "restart_completed" in _event_types(state))

        assert "restart_completed" in _event_types(state)
        assert pm.calls == ["stop_engine", "clear_given_up", "start_engine"]

    def test_the_lock_is_free_again_afterwards(self, stuck: Any) -> None:
        pm, state, app = stuck
        _post(app, {"target": "engine", "reason": "second"})
        pm.let_go.set()
        _until(lambda: "restart_completed" in _event_types(state))
        _until(lambda: not pm.engine_restart_lock.locked())

        assert _post(app, {"target": "engine", "reason": "third"}).json()["scheduled"] is True
        _until(lambda: not pm.engine_restart_lock.locked())


class TestTheLockIsFreedWhenTheRestartNeverStarts:
    """锁在请求线程里拿，放在后台线程的 finally 里 —— 后台线程没起来就没人放。"""

    def test_a_failed_announcement_frees_it(self, tmp_path: Path) -> None:
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)
        real_append = state.eventlog.append

        def _append(*args: Any, **kwargs: Any):
            if kwargs.get("type_") == "outbound_message":
                raise RuntimeError("事件写不进去")
            return real_append(*args, **kwargs)

        state.eventlog.append = _append

        with pytest.raises(RuntimeError):
            _post(app, {"target": "engine", "reason": "test", "session_id": "s-1"})

        assert not pm.engine_restart_lock.locked()

    def test_a_failed_announcement_starts_nothing(self, tmp_path: Path) -> None:
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)
        real_append = state.eventlog.append

        def _append(*args: Any, **kwargs: Any):
            if kwargs.get("type_") == "outbound_message":
                raise RuntimeError("事件写不进去")
            return real_append(*args, **kwargs)

        state.eventlog.append = _append

        with pytest.raises(RuntimeError):
            _post(app, {"target": "engine", "reason": "test", "session_id": "s-1"})

        # 抑制标志是后台线程置的，没起来就不该有人置上它。
        assert pm._restarting_engine is False
        assert pm.calls == []

    def test_a_thread_that_will_not_start_frees_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import supervisor.api as api_module

        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        class _DeadThread:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        class _FakeThreading:
            Thread = _DeadThread

        monkeypatch.setattr(api_module, "threading", _FakeThreading)

        with pytest.raises(RuntimeError):
            _post(app, {"target": "engine", "reason": "test"})

        assert not pm.engine_restart_lock.locked()
        assert pm._restarting_engine is False


class TestTheRealProcessManagerComesBackUnderTheWatchdog:
    """替身只能证明调了哪几个方法，停手状态真的清没清要问记账本身。"""

    @pytest.fixture
    def pm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(ProcessManager, "_install_signal_handlers", lambda self: None)
        manager = ProcessManager(
            supervisor_url="http://127.0.0.1:8765",
            workspace_root=tmp_path,
            log_dir=tmp_path / "logs",
        )
        manager.engine_workers = 1

        def _fake_spawn(*, name: str, module: str, **_kwargs: Any) -> ManagedProcess:
            return ManagedProcess(
                name=name, popen=_Popen(), log_path=None, started_at=time.time(),
            )

        monkeypatch.setattr(manager, "_spawn", _fake_spawn)
        yield manager
        # atexit 注册的 _cleanup 会在解释器收摊时再动一次子进程。
        manager._stopped = True

    def test_a_worker_the_watchdog_gave_up_on_is_taken_back(
        self, tmp_path: Path, pm: ProcessManager,
    ) -> None:
        pm._health["engine-1"] = WorkerHealth(failures=5, given_up=True, next_retry_at=time.time() + 1e6)
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: "restart_completed" in _event_types(state))

        row = pm.worker_health()["engine-1"]
        assert row["given_up"] is False
        assert row["failures"] == 0

    def test_the_backoff_clock_is_reset_too(self, tmp_path: Path, pm: ProcessManager) -> None:
        # 退避留着的话，重启起来的 engine 一崩就要等到那个时间点才有人管。
        pm._health["engine-1"] = WorkerHealth(failures=5, given_up=True, next_retry_at=time.time() + 1e6)
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: "restart_completed" in _event_types(state))

        assert pm._health["engine-1"].next_retry_at == 0.0

    def test_the_engine_is_actually_running_afterwards(self, tmp_path: Path, pm: ProcessManager) -> None:
        pm._health["engine-1"] = WorkerHealth(failures=5, given_up=True)
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: "restart_completed" in _event_types(state))

        assert [p.name for p in pm.engines] == ["engine-1"]
        assert pm.worker_health()["engine-1"]["alive"] is True

    def test_the_watchdog_goes_back_to_patrolling(self, tmp_path: Path, pm: ProcessManager) -> None:
        # 重启完还压着抑制标志的话，_watchdog_loop 每轮都在开头 continue，
        # 这之后 engine 再崩就真的没人管了 —— 而人工重启本身就说明它正在反复崩。
        pm._health["engine-1"] = WorkerHealth(failures=5, given_up=True)
        state, app = _make(tmp_path, pm)
        _post(app, {"target": "engine", "reason": "test"})
        _until(lambda: "restart_completed" in _event_types(state))
        _until(lambda: pm._restarting_engine is False)

        assert not (pm._stopped or pm._restarting_engine), "巡检的跳过条件还成立"

        pm.engines[0].popen.returncode = 1
        pm._watchdog_tick()

        assert pm.worker_health()["engine-1"]["alive"] is True
        assert pm.worker_health()["engine-1"]["respawns"] == 1


class TestEngineRestartReloadsTheWorkspaceEnv:
    """控制台改过的密钥要先进 supervisor 的环境，新拉起的 engine 才继承得到。"""

    def test_it_reloads_the_env_of_this_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 不带路径的 load_dotenv 找到的是代码目录那份：多开时那是主实例的端口和密钥。
        import supervisor.api as api_module

        loaded: list[tuple[Any, bool]] = []
        monkeypatch.setattr(
            api_module, "load_workspace_env",
            lambda root, *, override=False: loaded.append((root, override)),
        )
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")

        # override=True：supervisor 的环境还停在服务启动那一刻，不盖就等于没改。
        assert loaded == [(state.workspace_root, True)]

    def test_it_pins_the_workspace_this_supervisor_runs_in(self, tmp_path: Path) -> None:
        # 从别的实例抄来的 .env 带着那个实例的根：不钉回来，这次重启拉起的 engine 就落到那边去了。
        other = tmp_path / "other"
        other.mkdir()
        (tmp_path / ".env").write_text(f"CLONOTH_WORKSPACE={other.as_posix()}\n", encoding="utf-8")
        pm = _FakeProcessManager()
        state, app = _make(tmp_path, pm)

        _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")

        assert os.environ["CLONOTH_WORKSPACE"] == str(Path(state.workspace_root).resolve())

    def test_a_failed_reload_does_not_cancel_the_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # .env 被删掉或正写到一半，重启本身不能跟着黄掉。
        import supervisor.api as api_module

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("no .env here")

        monkeypatch.setattr(api_module, "load_workspace_env", _boom)
        pm = _FakeProcessManager()
        _state, app = _make(tmp_path, pm)

        response = _post(app, {"target": "engine", "reason": "test"})
        _settle(pm, "start_engine")

        assert response.json()["scheduled"] is True
        assert "start_engine" in pm.calls


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
