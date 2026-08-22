"""engine 看门狗。

engine 崩了 supervisor 照样活着、systemd 照样 active running —— 生产上这么潜伏过
三个半小时。这里盯的是重拉本身，以及「起不来时别把磁盘刷满」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.process_manager import ManagedProcess, ProcessManager, WorkerHealth  # noqa: E402


class FakePopen:
    def __init__(self, pid: int = 1000, alive: bool = True) -> None:
        self.pid = pid
        self.returncode: int | None = None if alive else 1
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def die(self, code: int = 1) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def pm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProcessManager:
    monkeypatch.setattr(ProcessManager, "_install_signal_handlers", lambda self: None)
    manager = ProcessManager(
        supervisor_url="http://127.0.0.1:8765",
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
        log_func=lambda msg: None,
    )
    manager.engine_workers = 2
    manager.spawned: list[str] = []

    def _fake_spawn(*, name: str, module: str, **kwargs) -> ManagedProcess:
        manager.spawned.append(name)
        import time
        return ManagedProcess(
            name=name, popen=FakePopen(pid=9000 + len(manager.spawned)),
            log_path=None, started_at=time.time(),
        )

    monkeypatch.setattr(manager, "_spawn", _fake_spawn)
    return manager


def crash(pm: ProcessManager, name: str = "engine-1") -> None:
    """崩一次并让它走完重拉：先巡检收尸，再把退避时钟推过去。"""
    for proc in pm.engines:
        if proc.name == name:
            proc.popen.die()
    pm._watchdog_tick()
    health = pm._health.get(name)
    if health and not health.given_up and not any(p.name == name for p in pm.engines):
        health.next_retry_at = 0.0
        pm._watchdog_tick()


class TestNaming:
    def test_a_gap_is_filled_by_name_not_by_count(self, pm: ProcessManager) -> None:
        # engine-1 死了而 engine-2 还活着时，数着补会再造一个 engine-2 出来，
        # 两个 worker 抢同一个 id 注册。
        pm.start_engine()
        assert pm.spawned == ["engine-1", "engine-2"]

        pm.engines[0].popen.die()
        pm.spawned.clear()
        pm.start_engine()

        assert pm.spawned == ["engine-1"]
        assert sorted(p.name for p in pm.engines) == ["engine-1", "engine-2"]

    def test_nothing_is_spawned_when_all_are_alive(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm.spawned.clear()
        pm.start_engine()
        assert pm.spawned == []


class TestRespawn:
    def test_a_dead_worker_comes_back(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm.engines[0].popen.die()
        pm.spawned.clear()

        pm._watchdog_tick()

        assert pm.spawned == ["engine-1"]
        assert pm.worker_health()["engine-1"]["respawns"] == 1

    def test_a_living_worker_is_left_alone(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm.spawned.clear()
        pm._watchdog_tick()
        assert pm.spawned == []

    def test_the_exit_code_and_tail_are_recorded(self, pm: ProcessManager, tmp_path: Path) -> None:
        pm.start_engine()
        log = tmp_path / "logs" / "engine-1.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("PermissionError: /opt/kazebot/tools/__init__.py\n", encoding="utf-8")
        pm.engines[0].log_path = log
        pm.engines[0].popen.die(13)

        pm._watchdog_tick()

        health = pm.worker_health()["engine-1"]
        assert health["last_exit_code"] == 13
        assert "PermissionError" in health["last_log"]


class TestBackoff:
    def _crash_no_retry(self, pm: ProcessManager, name: str = "engine-1") -> None:
        """只崩、只收尸，不把退避时钟推过去。"""
        for proc in pm.engines:
            if proc.name == name:
                proc.popen.die()
        pm._watchdog_tick()

    def test_the_first_crash_is_retried_at_once(self, pm: ProcessManager) -> None:
        # 偶发崩溃白等一秒没道理，退避是留给反复起不来的。
        pm.start_engine()
        pm.spawned.clear()
        self._crash_no_retry(pm)
        assert pm.spawned == ["engine-1"]

    def test_the_delay_doubles_from_the_second_crash(self, pm: ProcessManager) -> None:
        pm.start_engine()
        delays = []
        for _ in range(4):
            crash(pm)
            delays.append(pm._health["engine-1"].next_retry_at)
        gaps = [round(b - a, 3) for a, b in zip(delays, delays[1:])]
        assert gaps == sorted(gaps)
        assert gaps[-1] >= gaps[0] * 2

    def test_the_delay_is_capped(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=20)
        self._crash_no_retry(pm)
        assert pm.worker_health()["engine-1"]["retry_in_sec"] <= pm._BACKOFF_CAP_SEC

    def test_backoff_holds_the_respawn_back(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=3)
        self._crash_no_retry(pm)
        pm.spawned.clear()

        pm._watchdog_tick()  # 还在退避窗口里

        assert pm.spawned == []

    def test_a_worker_that_ran_a_long_time_starts_over(self, pm: ProcessManager) -> None:
        # 跑了几小时才退出跟「起来就崩」不是一回事，不该继承上一轮的退避。
        import time
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=4)
        for proc in pm.engines:
            if proc.name == "engine-1":
                proc.started_at = time.time() - 3600
                proc.popen.die()
        pm._watchdog_tick()

        assert pm._health["engine-1"].failures == 1


class TestGivingUp:
    def test_it_stops_after_enough_failures(self, pm: ProcessManager) -> None:
        pm.start_engine()
        for _ in range(pm._MAX_FAILURES):
            crash(pm)

        health = pm.worker_health()["engine-1"]
        assert health["given_up"] is True

        pm.spawned.clear()
        pm._watchdog_tick()
        assert pm.spawned == []

    def test_the_other_worker_is_unaffected(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=99, given_up=True)
        pm.spawned.clear()
        for proc in pm.engines:
            if proc.name == "engine-2":
                proc.popen.die()

        pm._watchdog_tick()

        assert pm.spawned == ["engine-2"]

    def test_a_manual_retry_clears_it(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=99, given_up=True, next_retry_at=1e12)
        for proc in list(pm.engines):
            if proc.name == "engine-1":
                pm.engines.remove(proc)

        pm.clear_given_up()
        pm.spawned.clear()
        pm._watchdog_tick()

        assert pm.spawned == ["engine-1"]

    def test_staying_up_resets_the_count(self, pm: ProcessManager) -> None:
        import time
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=3)
        for proc in pm.engines:
            if proc.name == "engine-1":
                proc.started_at = time.time() - pm._STABLE_SEC - 1

        pm._watchdog_tick()

        assert pm.worker_health()["engine-1"]["failures"] == 0


class TestNotFightingWithDeliberateStops:
    def test_stopping_on_purpose_is_not_a_crash(self, pm: ProcessManager) -> None:
        # stop_engine 把进程摘出列表，看门狗看不到它们，也就不会记成崩溃。
        pm.start_engine()
        pm.stop_engine()
        assert all(row["failures"] == 0 for row in pm.worker_health().values())

    def test_the_loop_stands_down_while_shutting_down(self, pm: ProcessManager) -> None:
        pm.start_engine()
        for proc in pm.engines:
            proc.popen.die()
        pm._stopped = True
        pm.spawned.clear()

        # _watchdog_loop 在 _stopped / _restarting_engine 时不进 tick。
        assert pm._stopped
        pm._restarting_engine = True
        assert pm._restarting_engine

    def test_restarting_clears_the_give_up_state(self, pm: ProcessManager) -> None:
        pm.start_engine()
        pm._health["engine-1"] = WorkerHealth(failures=99, given_up=True)

        pm.restart_engine()

        assert pm.worker_health()["engine-1"]["given_up"] is False
        assert pm.worker_health()["engine-1"]["failures"] == 0


class TestHealthReport:
    def test_every_configured_worker_shows_up(self, pm: ProcessManager) -> None:
        # 一个都没起来时也要列出来，否则界面上是一片空白而不是「两个都挂了」。
        rows = pm.worker_health()
        assert sorted(rows) == ["engine-1", "engine-2"]
        assert all(row["alive"] is False for row in rows.values())

    def test_a_running_worker_reports_pid_and_uptime(self, pm: ProcessManager) -> None:
        pm.start_engine()
        row = pm.worker_health()["engine-1"]
        assert row["alive"] is True
        assert row["pid"] > 0
        assert row["uptime_sec"] >= 0


class TestEvents:
    def test_a_death_and_a_respawn_are_both_reported(self, pm: ProcessManager) -> None:
        seen: list[tuple[str, dict]] = []
        pm._on_event = lambda kind, payload: seen.append((kind, payload))
        pm.start_engine()
        pm.engines[0].popen.die(13)

        pm._watchdog_tick()

        kinds = [kind for kind, _ in seen]
        assert "engine_died" in kinds
        died = next(p for k, p in seen if k == "engine_died")
        assert died["worker_id"] == "engine-1"
        assert died["exit_code"] == 13

    def test_giving_up_is_reported(self, pm: ProcessManager) -> None:
        seen: list[str] = []
        pm._on_event = lambda kind, payload: seen.append(kind)
        pm.start_engine()
        for _ in range(pm._MAX_FAILURES):
            crash(pm)

        assert "engine_gave_up" in seen

    def test_a_broken_event_sink_does_not_stop_the_respawn(self, pm: ProcessManager) -> None:
        # 记不上事件是小事，因此不重拉 engine 是大事。
        def _boom(kind: str, payload: dict) -> None:
            raise RuntimeError("eventlog 写不进去")

        pm._on_event = _boom
        pm.start_engine()
        pm.engines[0].popen.die()
        pm.spawned.clear()

        pm._watchdog_tick()

        assert pm.spawned == ["engine-1"]
