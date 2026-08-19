"""两个「配了但从来不跑」的维护任务。

signals.jsonl 的轮转和整个 data_cleanup 都只挂在 systemd timer 上，Windows 部署上
一次都没执行过 —— 日志无界增长，所有标称的保留策略全都没生效，而外部看不出区别。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.data_cleanup as cleanup
import engine.signals.bridge as bridge
from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES
from engine.signals.bus import SignalBus
from engine.signals.types import Signal


class TestSignalsRotation:
    @pytest.fixture(autouse=True)
    def _reset_bridge(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bridge, "_bridge_installed", False)
        monkeypatch.setattr(bridge, "_SIGNALS_LOG", None)
        monkeypatch.setattr(bridge, "_writes_since_rotate_check", 0)
        monkeypatch.setattr(bridge, "_bridge_patterns", None)
        monkeypatch.setattr(bridge, "_bridge_exclude_patterns", None)

    def _install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, max_bytes: int, every: int = 1):
        monkeypatch.setattr(bridge, "SIGNALS_MAX_BYTES", max_bytes)
        monkeypatch.setattr(bridge, "SIGNALS_BACKUPS", 2)
        monkeypatch.setattr(bridge, "_ROTATE_CHECK_EVERY", every)
        bus = SignalBus()
        bridge.install_event_bridge(bus, log_dir=tmp_path)
        return bus

    def test_the_active_log_rotates_once_it_is_too_big(self, tmp_path: Path, monkeypatch) -> None:
        bus = self._install(tmp_path, monkeypatch, max_bytes=200)

        for i in range(40):
            bus.emit(Signal(name="test.event", payload={"i": i, "pad": "x" * 40}))

        assert (tmp_path / "signals.jsonl.1").exists()
        # 轮转把 active 改名走了，下一条信号会重建它 —— 关键是它不再背着全部历史。
        bus.emit(Signal(name="test.event", payload={"i": "after"}))
        assert (tmp_path / "signals.jsonl").stat().st_size < 200

    def test_rotated_content_is_not_lost(self, tmp_path: Path, monkeypatch) -> None:
        bus = self._install(tmp_path, monkeypatch, max_bytes=200)

        for i in range(40):
            bus.emit(Signal(name="test.event", payload={"i": i, "pad": "x" * 40}))

        rotated = (tmp_path / "signals.jsonl.1").read_text(encoding="utf-8")
        assert "test.event" in rotated

    def test_no_rotation_below_the_threshold(self, tmp_path: Path, monkeypatch) -> None:
        bus = self._install(tmp_path, monkeypatch, max_bytes=10 * 1024 * 1024)

        bus.emit(Signal(name="test.event", payload={"i": 1}))

        assert not (tmp_path / "signals.jsonl.1").exists()

    def test_backups_are_capped(self, tmp_path: Path, monkeypatch) -> None:
        bus = self._install(tmp_path, monkeypatch, max_bytes=150)

        for i in range(200):
            bus.emit(Signal(name="test.event", payload={"i": i, "pad": "x" * 40}))

        assert not (tmp_path / "signals.jsonl.3").exists()

    def test_a_size_check_does_not_run_on_every_write(self, tmp_path: Path, monkeypatch) -> None:
        """signals 是高频写入路径，每条都 stat 一次不值。"""
        checks: list[int] = []
        monkeypatch.setattr(bridge, "SIGNALS_MAX_BYTES", 1)
        monkeypatch.setattr(bridge, "SIGNALS_BACKUPS", 2)
        monkeypatch.setattr(bridge, "_ROTATE_CHECK_EVERY", 10)
        monkeypatch.setattr(bridge, "rotate_event_log", lambda *a, **k: checks.append(1))
        bus = SignalBus()
        bridge.install_event_bridge(bus, log_dir=tmp_path)
        checks.clear()

        for i in range(9):
            bus.emit(Signal(name="test.event", payload={"i": i}))

        assert checks == []
        bus.emit(Signal(name="test.event", payload={"i": 9}))
        assert len(checks) == 1

    def test_install_checks_the_size_left_by_the_previous_process(self, tmp_path: Path, monkeypatch) -> None:
        # 重启前积累的体积不该等到再写 512 条才被发现。
        (tmp_path / "signals.jsonl").write_text("x" * 500, encoding="utf-8")

        self._install(tmp_path, monkeypatch, max_bytes=200, every=10_000)

        assert (tmp_path / "signals.jsonl.1").exists()

    def test_a_rotation_failure_is_visible_and_not_fatal(self, tmp_path: Path, monkeypatch, caplog) -> None:
        """信号写入的外层 except 只 log.debug，轮转一直失败会完全无声。

        文件会持续变大，而唯一的线索藏在默认不输出的日志级别里。
        """
        def _boom(*args: Any, **kwargs: Any):
            raise OSError("locked")

        monkeypatch.setattr(bridge, "SIGNALS_MAX_BYTES", 1)
        monkeypatch.setattr(bridge, "SIGNALS_BACKUPS", 2)
        monkeypatch.setattr(bridge, "_ROTATE_CHECK_EVERY", 1)
        monkeypatch.setattr(bridge, "rotate_event_log", _boom)
        bus = SignalBus()
        bridge.install_event_bridge(bus, log_dir=tmp_path)

        with caplog.at_level(logging.WARNING):
            bus.emit(Signal(name="test.event", payload={"i": 1}))
            bus.emit(Signal(name="test.event", payload={"i": 2}))

        assert any("rotation failed" in r.getMessage() for r in caplog.records)
        # 两条都要落盘：轮转坏了不等于信号丢了。
        assert (tmp_path / "signals.jsonl").read_text(encoding="utf-8").count("test.event") == 2

    def test_both_rotation_paths_share_one_threshold(self) -> None:
        """离线脚本和在线 bridge 取不同阈值就会互相踩掉备份编号。"""
        assert bridge.SIGNALS_MAX_BYTES == SIGNALS_MAX_BYTES
        assert bridge.SIGNALS_BACKUPS == SIGNALS_BACKUPS

    def test_the_offline_script_uses_the_locked_implementation(self) -> None:
        # 原来那份是裸 rename：没有锁、没有事务，和在线轮转并发时会丢备份。
        import ast

        tree = ast.parse((_ROOT / "engine/data_cleanup.py").read_text(encoding="utf-8"))
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "rotate_signals"
        )
        body = ast.unparse(fn)

        assert "rotate_event_log" in body
        assert ".rename(" not in body


class TestDryRun:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", False)

    def test_a_dry_run_reports_instead_of_deleting(self, tmp_path: Path, monkeypatch, caplog) -> None:
        target = tmp_path / "data" / "artifacts" / "old.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        import os as _os
        _os.utime(target, (0, 0))

        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        with caplog.at_level(logging.INFO):
            cleanup.purge_dir(target.parent, 3600, "artifacts")

        assert target.exists()
        assert any("would delete file" in r.getMessage() for r in caplog.records)

    def test_without_dry_run_it_really_deletes(self, tmp_path: Path) -> None:
        target = tmp_path / "data" / "artifacts" / "old.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        import os as _os
        _os.utime(target, (0, 0))

        cleanup.purge_dir(target.parent, 3600, "artifacts")

        assert not target.exists()

    def _book(self, tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
        book = tmp_path / "data" / "memory" / "conv_abc" / "profile.yaml"
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_text(
            yaml.safe_dump({"book": "profile", "entries": entries}, allow_unicode=True), encoding="utf-8",
        )
        return book

    def _old(self) -> str:
        return datetime.now(timezone.utc).replace(year=2020).isoformat()

    def _fresh(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def test_a_dry_run_keeps_an_expired_entry_in_a_surviving_book(self, tmp_path: Path, monkeypatch) -> None:
        """部分过期走的是「重写整本」，和「删空本」是两条不同的写路径。

        记忆是最不该被静默删掉的东西，两条路径都得受 dry-run 管。
        """
        book = self._book(tmp_path, [
            {"id": "m1", "content": "旧的", "source": "auto", "updated_at": self._old()},
            {"id": "m2", "content": "留下", "source": "auto", "updated_at": self._fresh()},
        ])

        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        cleanup.purge_expired_memory_entries()

        assert [e["id"] for e in yaml.safe_load(book.read_text(encoding="utf-8"))["entries"]] == ["m1", "m2"]

    def test_a_real_run_rewrites_a_surviving_book(self, tmp_path: Path) -> None:
        book = self._book(tmp_path, [
            {"id": "m1", "content": "旧的", "source": "auto", "updated_at": self._old()},
            {"id": "m2", "content": "留下", "source": "auto", "updated_at": self._fresh()},
        ])

        cleanup.purge_expired_memory_entries()

        assert [e["id"] for e in yaml.safe_load(book.read_text(encoding="utf-8"))["entries"]] == ["m2"]

    def test_a_dry_run_keeps_a_book_that_would_be_emptied(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, [
            {"id": "m1", "content": "旧的", "source": "auto", "updated_at": self._old()},
        ])

        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        cleanup.purge_expired_memory_entries()

        assert book.exists()

    def test_a_real_run_removes_an_emptied_book(self, tmp_path: Path) -> None:
        book = self._book(tmp_path, [
            {"id": "m1", "content": "旧的", "source": "auto", "updated_at": self._old()},
        ])

        cleanup.purge_expired_memory_entries()

        assert not book.exists()

    def test_the_flag_comes_from_the_command_line(self) -> None:
        import ast

        tree = ast.parse((_ROOT / "engine/data_cleanup.py").read_text(encoding="utf-8"))
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

        assert "--dry-run" in ast.unparse(fn)

    def test_every_destructive_call_goes_through_a_helper(self) -> None:
        """漏一处就是 dry-run 下仍然真删，而日志看着像什么都没干。"""
        import ast

        source = (_ROOT / "engine/data_cleanup.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = {"_unlink", "_rmdir", "_write_text"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name in allowed:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr in ("unlink", "rmdir"):
                    offenders.append(f"{node.name}: {ast.unparse(call)}")

        assert offenders == [], f"这些删除没走 dry-run 闸门: {offenders}"


class TestCleanupSchedule:
    def _handler(self):
        from engine.builtin.data_cleanup_schedule import DataCleanupScheduleHandler

        return DataCleanupScheduleHandler()

    def _ctx(self, tmp_path: Path, *, now: datetime, schedule_type: str = "data_cleanup") -> dict[str, Any]:
        return {
            "schedule_type": schedule_type,
            "workspace_root": tmp_path,
            "now": now,
            "now_key": now.strftime("%Y-%m-%d %H:%M"),
        }

    def _config(self, tmp_path: Path, **overrides: Any) -> None:
        cfg = {"maintenance": {"data_cleanup": {"enabled": True, "cron": "17 * * * *", "dry_run": True}}}
        cfg["maintenance"]["data_cleanup"].update(overrides)
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "runtime.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        import engine.builtin.data_cleanup_schedule as module

        calls: list[list[str]] = []

        class _Proc:
            pid = 4242

            def poll(self):
                return 0

        def _popen(args, **kwargs):
            calls.append(list(args))
            return _Proc()

        monkeypatch.setattr(module.subprocess, "Popen", _popen)
        return calls

    def test_it_spawns_at_the_cron_minute(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path)
        calls = self._spy(monkeypatch)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))

        assert len(calls) == 1
        assert calls[0][-1] == "--dry-run"

    def test_it_stays_quiet_off_the_cron_minute(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path)
        calls = self._spy(monkeypatch)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 18, tzinfo=timezone.utc)))

        assert calls == []

    def test_dry_run_false_drops_the_flag(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path, dry_run=False)
        calls = self._spy(monkeypatch)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))

        assert "--dry-run" not in calls[0]

    def test_dry_run_defaults_to_true(self, tmp_path: Path, monkeypatch) -> None:
        # 一个从来没跑过的清理脚本第一次上线，默认必须是只报告不删。
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "runtime.yaml").write_text(
            yaml.safe_dump({"maintenance": {"data_cleanup": {"cron": "17 * * * *"}}}), encoding="utf-8",
        )
        calls = self._spy(monkeypatch)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))

        assert calls and calls[0][-1] == "--dry-run"

    def test_disabling_it_stops_the_spawn(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path, enabled=False)
        calls = self._spy(monkeypatch)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))

        assert calls == []

    def test_another_schedule_type_is_ignored(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path)
        calls = self._spy(monkeypatch)

        self._handler().on_tick(
            self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc), schedule_type="dream"),
        )

        assert calls == []

    def test_the_same_minute_does_not_fire_twice(self, tmp_path: Path, monkeypatch) -> None:
        self._config(tmp_path)
        calls = self._spy(monkeypatch)
        handler = self._handler()
        ctx = self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc))

        handler.on_tick(ctx)
        handler.on_tick(ctx)

        assert len(calls) == 1

    def test_it_skips_while_a_previous_run_is_alive(self, tmp_path: Path, monkeypatch) -> None:
        """两个进程同时遍历同一批目录只会互相撞上「文件已被对方删掉」。"""
        import engine.builtin.data_cleanup_schedule as module

        self._config(tmp_path)
        calls: list[list[str]] = []

        class _Alive:
            pid = 1

            def poll(self):
                return None

        monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kw: (calls.append(list(args)), _Alive())[1])
        handler = self._handler()

        handler.on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))
        handler.on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 6, 17, tzinfo=timezone.utc)))

        assert len(calls) == 1

    def test_a_spawn_failure_does_not_raise(self, tmp_path: Path, monkeypatch) -> None:
        # 这个 handler 跑在 supervisor 的 state 锁里，抛出去会拖垮整个 scheduler tick。
        import engine.builtin.data_cleanup_schedule as module

        self._config(tmp_path)

        def _boom(*args: Any, **kwargs: Any):
            raise OSError("no exec")

        monkeypatch.setattr(module.subprocess, "Popen", _boom)

        self._handler().on_tick(self._ctx(tmp_path, now=datetime(2026, 8, 17, 5, 17, tzinfo=timezone.utc)))

    def test_the_scheduler_fires_this_schedule_type(self) -> None:
        """scheduler 只 fire "dream" 的话，这个 handler 永远等不到自己的 tick。"""
        source = (_ROOT / "supervisor/scheduler.py").read_text(encoding="utf-8")

        assert '"data_cleanup"' in source

    def test_the_handler_never_runs_cleanup_inline(self) -> None:
        """清理要遍历整个 data 目录，放进串行 tick 会阻塞 dream 和所有用户定时任务。"""
        import ast

        tree = ast.parse((_ROOT / "engine/builtin/data_cleanup_schedule.py").read_text(encoding="utf-8"))
        body = ast.unparse(tree)

        assert "subprocess.Popen" in body
        assert ".wait()" not in body
        assert "subprocess.run" not in body
