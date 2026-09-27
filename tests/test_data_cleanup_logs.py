"""data/logs/ 的定期清理。

清理按 mtime 走，而 supervisor 和每个 worker 的日志文件是开着的：删掉之后进程还
握着句柄往那个已经不在目录里的 inode 写，看上去是日志突然停了，实际上一直在写。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.data_cleanup as cleanup  # noqa: E402

_DAY = 24 * 3600


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cleanup, "DRY_RUN", False)
    monkeypatch.setattr(cleanup, "LOG_MAX_AGE", 3 * _DAY)
    path = tmp_path / "data" / "logs"
    path.mkdir(parents=True)
    return path


def _aged(log_dir: Path, name: str, *, days: float) -> Path:
    path = log_dir / name
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - days * _DAY
    os.utime(path, (stamp, stamp))
    return path


def _left(log_dir: Path) -> list[str]:
    return sorted(p.name for p in log_dir.iterdir())


class TestTheOpenFilesSurvive:
    def test_the_supervisor_log_is_kept_whatever_its_age(self, log_dir: Path) -> None:
        # 按大小轮转的那份，mtime 只在写的时候动；安静一整天就够老到被删。
        _aged(log_dir, "supervisor.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["supervisor.log"]

    def test_the_cleanup_log_is_kept_too(self, log_dir: Path) -> None:
        # 这个脚本自己正往里写，删完下一行就写进一个没人看得到的文件。
        _aged(log_dir, "cleanup.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["cleanup.log"]

    def test_the_newest_spawn_log_of_a_series_is_kept(self, log_dir: Path) -> None:
        # ProcessManager 每拉起一次子进程就新开一份，最新那份多半是当前进程开着的。
        _aged(log_dir, "engine-1-20200101-000000.log", days=90)
        _aged(log_dir, "engine-1-20200102-000000.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["engine-1-20200102-000000.log"]

    def test_each_series_keeps_its_own_newest(self, log_dir: Path) -> None:
        for name in (
            "engine-1-20200101-000000.log", "engine-1-20200103-120000.log",
            "engine-2-20200101-000000.log", "engine-2-20200102-000000.log",
            "shell-tui-20200101-000000.log",
        ):
            _aged(log_dir, name, days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == [
            "engine-1-20200103-120000.log",
            "engine-2-20200102-000000.log",
            "shell-tui-20200101-000000.log",
        ]

    def test_the_time_of_day_decides_within_one_date(self, log_dir: Path) -> None:
        # 一天里重启十几次是常事，只比日期的话留下的是当天第一份。
        _aged(log_dir, "engine-1-20200101-000001.log", days=90)
        _aged(log_dir, "engine-1-20200101-235959.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["engine-1-20200101-235959.log"]


class TestEverythingElseGoesByAge:
    def test_a_rotated_supervisor_backup_expires(self, log_dir: Path) -> None:
        # 备份没人写，留着就是白占地方。
        _aged(log_dir, "supervisor.log", days=90)
        _aged(log_dir, "supervisor.1.log", days=90)
        _aged(log_dir, "supervisor.2.log", days=1)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["supervisor.2.log", "supervisor.log"]

    def test_an_unrecognised_name_is_deleted_when_old(self, log_dir: Path) -> None:
        _aged(log_dir, "whatever.log", days=90)
        _aged(log_dir, "notes.txt", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == []

    def test_an_unrecognised_name_is_kept_when_fresh(self, log_dir: Path) -> None:
        _aged(log_dir, "whatever.log", days=1)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["whatever.log"]

    def test_an_older_spawn_log_that_is_still_fresh_is_kept(self, log_dir: Path) -> None:
        # 「不是最新那份」只是不受保护，不等于立刻删。
        _aged(log_dir, "engine-1-20200101-000000.log", days=1)
        _aged(log_dir, "engine-1-20200102-000000.log", days=1)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["engine-1-20200101-000000.log", "engine-1-20200102-000000.log"]

    def test_a_name_that_only_looks_like_a_spawn_log_is_not_protected(self, log_dir: Path) -> None:
        # 位数不对就不是 ProcessManager 开的，没有「可能还开着」这回事。
        _aged(log_dir, "engine-1-2020011-000000.log", days=90)
        _aged(log_dir, "engine-1-20200101-00000.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == []


class TestTheSwitchesStillWork:
    def test_a_zero_max_age_disables_the_sweep(self, log_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "LOG_MAX_AGE", 0.0)
        _aged(log_dir, "whatever.log", days=90)

        cleanup.purge_old_logs()

        assert _left(log_dir) == ["whatever.log"]

    def test_a_dry_run_reports_instead_of_deleting(
        self, log_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        _aged(log_dir, "whatever.log", days=90)

        with caplog.at_level(logging.INFO):
            cleanup.purge_old_logs()

        assert _left(log_dir) == ["whatever.log"]
        assert any("would delete file" in r.getMessage() for r in caplog.records)

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "LOG_MAX_AGE", 3 * _DAY)

        cleanup.purge_old_logs()


class TestNewestSpawnLogs:
    def test_it_reports_one_name_per_series(self, log_dir: Path) -> None:
        for name in (
            "engine-1-20200101-000000.log", "engine-1-20200102-000000.log",
            "engine-2-20200101-000000.log",
        ):
            _aged(log_dir, name, days=1)

        assert cleanup._newest_spawn_logs(log_dir) == {
            "engine-1-20200102-000000.log", "engine-2-20200101-000000.log",
        }

    def test_the_rotating_logs_are_not_a_series(self, log_dir: Path) -> None:
        for name in ("supervisor.log", "supervisor.1.log", "cleanup.log"):
            _aged(log_dir, name, days=1)

        assert cleanup._newest_spawn_logs(log_dir) == set()

    def test_a_directory_named_like_one_is_ignored(self, log_dir: Path) -> None:
        (log_dir / "engine-1-20200103-000000.log").mkdir()
        _aged(log_dir, "engine-1-20200101-000000.log", days=1)

        assert cleanup._newest_spawn_logs(log_dir) == {"engine-1-20200101-000000.log"}
