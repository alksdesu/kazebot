"""常驻进程的平台层：Windows 和 Linux 两路都用替身走，不碰真进程。

停之前核对启动指纹，对不上就不停；Linux 上按环境标记把 setsid 出去的子孙也找回来。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.processes import osproc  # noqa: E402
from toolbox import process_tree  # noqa: E402

TERM = 15
KILL = 9
TAG = "inst/p_1"
# 比 Linux 的 pid_max 上限还大，不会撞上真进程
BASE = 9_100_000


class _Popen:
    def __init__(self, pid: int, *, running: bool = True) -> None:
        self.pid = pid
        self.running = running
        self.killed = False
        self.polled = 0

    def poll(self) -> int | None:
        self.polled += 1
        return None if self.running else 1

    def kill(self) -> None:
        self.killed = True
        self.running = False


class _FakeWin:
    """Win32 的替身：进程表是 pid → (创建时刻, 是否还在跑)，descendants 按 (根 pid, 根创建时刻) 查。"""

    def __init__(
        self,
        table: dict[int, tuple[int, bool]],
        children: dict[tuple[int, int], list[tuple[int, int]]] | None = None,
    ) -> None:
        self.table = dict(table)
        self.children = dict(children or {})
        self.verified: list[tuple[int, int]] = []

    @contextlib.contextmanager
    def handle(self, pid: int) -> Iterator[int | None]:
        yield pid if pid in self.table else None

    def created(self, handle: int) -> int:
        return self.table[handle][0]

    def alive(self, handle: int) -> bool:
        return self.table[handle][1]

    def query(self, pid: int) -> tuple[int, bool] | None:
        return self.table.get(pid)

    def descendants(self, root_pid: int, root_created: int) -> list[tuple[int, int]]:
        return list(self.children.get((root_pid, root_created), []))

    def kill_verified(self, pid: int, created: int) -> bool:
        self.verified.append((pid, created))
        return True


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    taskkilled: list[int] = []
    monkeypatch.setattr(osproc, "IS_WINDOWS", True)
    monkeypatch.setattr(osproc, "IS_LINUX", False)
    monkeypatch.setattr(osproc, "taskkill_tree", lambda pid: taskkilled.append(pid) is None)

    def install(win: _FakeWin) -> _FakeWin:
        monkeypatch.setattr(osproc, "win32", lambda: win)
        return win

    return SimpleNamespace(taskkilled=taskkilled, install=install)


class TestWindows:
    def test_stopping_kills_the_tree_and_the_orphans_by_creation_time(self, windows: SimpleNamespace) -> None:
        win = windows.install(_FakeWin({100: (5000, True)}, {(100, 5000): [(201, 5001), (202, 5002)]}))
        popen = _Popen(100)

        osproc.stop_tree(popen, tag=TAG, started="5000", grace_sec=1)

        assert windows.taskkilled == [100]
        assert win.verified == [(201, 5001), (202, 5002)]
        assert popen.killed is True

    def test_a_shell_that_already_exited_only_leaves_its_orphans_to_kill(self, windows: SimpleNamespace) -> None:
        win = windows.install(_FakeWin({100: (5000, False)}, {(100, 5000): [(201, 5001)]}))
        popen = _Popen(100, running=False)

        osproc.stop_tree(popen, tag=TAG, started="5000", grace_sec=1)

        assert windows.taskkilled == [] and popen.killed is False
        assert win.verified == [(201, 5001)]

    def test_without_a_fingerprint_only_the_tree_is_killed(self, windows: SimpleNamespace) -> None:
        win = windows.install(_FakeWin({100: (5000, True)}, {(100, 5000): [(201, 5001)]}))

        osproc.stop_tree(_Popen(100), tag=TAG, started="", grace_sec=1)

        assert windows.taskkilled == [100] and win.verified == []

    def test_a_leftover_whose_creation_time_matches_is_reaped(self, windows: SimpleNamespace) -> None:
        win = windows.install(_FakeWin({100: (5000, True)}, {(100, 5000): [(201, 5001)]}))

        assert osproc.reap(100, "5000", TAG, grace_sec=1) is True

        assert windows.taskkilled == [100] and win.verified == [(201, 5001)]

    def test_a_dead_but_still_held_root_only_has_its_orphans_reaped(self, windows: SimpleNamespace) -> None:
        win = windows.install(_FakeWin({100: (5000, False)}, {(100, 5000): [(201, 5001)]}))

        assert osproc.reap(100, "5000", TAG, grace_sec=1) is True

        assert windows.taskkilled == [] and win.verified == [(201, 5001)]

    @pytest.mark.parametrize("table, started", [
        pytest.param({100: (7777, True)}, "5000", id="pid_reused"),
        pytest.param({}, "5000", id="gone"),
        pytest.param({100: (5000, True)}, "not-a-filetime", id="bad_record"),
    ])
    def test_nothing_is_killed_unless_the_root_is_verified(
        self, windows: SimpleNamespace, table: dict[int, tuple[int, bool]], started: str,
    ) -> None:
        win = windows.install(_FakeWin(table, {(100, 5000): [(201, 5001)], (100, 7777): [(301, 7778)]}))

        assert osproc.reap(100, started, TAG, grace_sec=1) is False

        assert windows.taskkilled == [] and win.verified == []

    def test_the_fingerprint_is_the_creation_time(self, windows: SimpleNamespace) -> None:
        windows.install(_FakeWin({100: (5000, True), 101: (6000, False)}))

        assert osproc.fingerprint(100) == "5000"
        assert osproc.fingerprint(101) is None
        assert osproc.fingerprint(101, include_exited=True) == "6000"
        assert osproc.fingerprint(999) is None
        assert osproc.fingerprint(0) is None

    def test_environment_tags_are_not_read_off_linux(self, windows: SimpleNamespace) -> None:
        assert osproc.tagged_pids(lambda value: True) == []
        assert osproc.reap_instance("inst", grace_sec=1) == []


class TestSpawn:
    @pytest.fixture
    def popens(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
        calls: list[tuple[Any, dict[str, Any]]] = []
        monkeypatch.setattr(osproc.subprocess, "Popen", lambda command, **options: calls.append((command, options)))
        return calls

    def test_windows_gets_its_own_process_group_and_no_window(
        self, monkeypatch: pytest.MonkeyPatch, popens: list[tuple[Any, dict[str, Any]]], tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(osproc, "IS_WINDOWS", True)
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

        osproc.spawn("serve", cwd=tmp_path, env={"PATH": "x"}, tag=TAG)

        [(command, options)] = popens
        assert command == "serve" and options["shell"] is True and options["cwd"] == str(tmp_path)
        assert options["creationflags"] == 0x200 | 0x08000000 and "start_new_session" not in options
        assert options["env"] == {"PATH": "x", osproc.TAG_ENV: TAG}
        assert options["stdin"] is subprocess.DEVNULL and options["stdout"] is subprocess.PIPE
        assert options["stderr"] is subprocess.STDOUT

    def test_posix_starts_a_new_session(
        self, monkeypatch: pytest.MonkeyPatch, popens: list[tuple[Any, dict[str, Any]]], tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(osproc, "IS_WINDOWS", False)

        osproc.spawn("serve", cwd=tmp_path, env={osproc.TAG_ENV: "inherited/p_old"}, tag=TAG)

        [(_command, options)] = popens
        assert options["start_new_session"] is True and "creationflags" not in options
        assert options["env"][osproc.TAG_ENV] == TAG

    def test_the_instance_key_follows_the_workspace(self, tmp_path: Path) -> None:
        first = osproc.instance_key(tmp_path / "a")
        assert first == osproc.instance_key(tmp_path / "a") and len(first) == 12
        assert first != osproc.instance_key(tmp_path / "b")
        assert osproc.process_tag(first, "p_1") == f"{first}/p_1"


class _FakeLinux:
    """照 /proc 的样子在临时目录里摆进程，信号只记下来：TERM、KILL 让进程消失，stubborn 里的不理 TERM。"""

    def __init__(self, root: Path, boot_id: str = "boot-1") -> None:
        self.root = root
        self.groups: dict[int, int] = {}
        self.stubborn: set[int] = set()
        self.signals: list[tuple[str, int, int]] = []
        random_dir = root / "sys" / "kernel" / "random"
        random_dir.mkdir(parents=True)
        (random_dir / "boot_id").write_text(boot_id + "\n", encoding="ascii")

    def add(
        self, pid: int, *, pgrp: int | None = None, tag: str | None = None, state: str = "S",
        starttime: int = 1000, name: str = "python3",
    ) -> int:
        group = pgrp if pgrp is not None else pid
        directory = self.root / str(pid)
        directory.mkdir()
        fields = [state, "1", str(group), str(group), *(["0"] * 15), str(starttime), "0", "0"]
        (directory / "stat").write_bytes(f"{pid} ({name}) {' '.join(fields)}\n".encode())
        environ = [b"PATH=/usr/bin", b"HOME=/root"]
        if tag is not None:
            environ.append(f"{osproc.TAG_ENV}={tag}".encode())
        (directory / "environ").write_bytes(b"\0".join(environ) + b"\0")
        self.groups[pid] = group
        return pid

    def alive(self, pid: int) -> bool:
        return pid in self.groups

    def killpg(self, pgid: int, sig: int) -> None:
        members = [pid for pid, group in self.groups.items() if group == pgid]
        if not members:
            raise ProcessLookupError(pgid)
        self.signals.append(("group", pgid, sig))
        for pid in members:
            self._deliver(pid, sig)

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.groups:
            raise ProcessLookupError(pid)
        self.signals.append(("pid", pid, sig))
        self._deliver(pid, sig)

    def _deliver(self, pid: int, sig: int) -> None:
        if sig == 0 or (sig == TERM and pid in self.stubborn):
            return
        shutil.rmtree(self.root / str(pid), ignore_errors=True)
        self.groups.pop(pid, None)

    def sent(self, sig: int) -> list[tuple[str, int]]:
        return [(kind, target) for kind, target, number in self.signals if number == sig]


@pytest.fixture
def linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeLinux:
    fake = _FakeLinux(tmp_path / "proc")
    monkeypatch.setattr(osproc, "IS_WINDOWS", False)
    monkeypatch.setattr(osproc, "IS_LINUX", True)
    monkeypatch.setattr(osproc, "_PROC", fake.root)
    monkeypatch.setattr(osproc, "_POLL_SEC", 0.01)
    monkeypatch.setattr(osproc, "signal", SimpleNamespace(SIGTERM=TERM, SIGKILL=KILL))
    monkeypatch.setattr(osproc.os, "killpg", fake.killpg, raising=False)
    monkeypatch.setattr(osproc.os, "kill", fake.kill)
    return fake


class TestLinux:
    def test_the_fingerprint_is_boot_id_and_start_time(self, linux: _FakeLinux) -> None:
        pid = linux.add(BASE + 1, starttime=4242, name="my (weird) name")
        zombie = linux.add(BASE + 2, starttime=99, state="Z")

        assert osproc.fingerprint(pid) == "boot-1:4242"
        assert osproc.fingerprint(zombie) is None
        assert osproc.fingerprint(zombie, include_exited=True) == "boot-1:99"
        assert osproc.fingerprint(BASE + 3) is None

    def test_tagged_processes_are_found_by_their_environment(self, linux: _FakeLinux) -> None:
        first = linux.add(BASE + 1, tag="inst/p_1")
        second = linux.add(BASE + 2, tag="inst/p_2")
        linux.add(BASE + 3, tag="inst2/p_3")
        linux.add(BASE + 4)
        linux.add(os.getpid(), tag="inst/p_me")
        (linux.root / "self").mkdir()
        (linux.root / str(BASE + 5)).mkdir()

        assert sorted(osproc.tagged_pids(lambda value: value.startswith("inst/"))) == [first, second]

    def test_stopping_signals_the_group_and_what_escaped_it(self, linux: _FakeLinux) -> None:
        root = linux.add(BASE + 10, tag=TAG)
        member = linux.add(BASE + 11, pgrp=root, tag=TAG)
        escaped = linux.add(BASE + 20, tag=TAG)
        unrelated = linux.add(BASE + 30, tag="inst/p_other")
        popen = _Popen(root)

        osproc.stop_tree(popen, tag=TAG, started="boot-1:1000", grace_sec=0.5)

        assert linux.sent(TERM) == [("group", root), ("pid", escaped)]
        assert linux.sent(KILL) == []
        assert not any(linux.alive(pid) for pid in (root, member, escaped))
        assert linux.alive(unrelated) and popen.polled > 0

    def test_whatever_ignores_term_gets_kill_after_the_grace_period(self, linux: _FakeLinux) -> None:
        root = linux.add(BASE + 10, tag=TAG)
        member = linux.add(BASE + 11, pgrp=root, tag=TAG)
        escaped = linux.add(BASE + 20, tag=TAG)
        linux.stubborn |= {member, escaped}

        osproc.stop_tree(_Popen(root), tag=TAG, started="", grace_sec=0.2)

        assert linux.sent(KILL) == [("group", root), ("pid", escaped)]
        assert not linux.alive(member) and not linux.alive(escaped)

    def test_a_verified_leftover_group_is_reaped(self, linux: _FakeLinux) -> None:
        root = linux.add(BASE + 40, starttime=777, tag=TAG)
        member = linux.add(BASE + 41, pgrp=root, tag=TAG)

        assert osproc.reap(root, "boot-1:777", TAG, grace_sec=0.3) is True

        assert linux.sent(TERM) == [("group", root)]
        assert not linux.alive(root) and not linux.alive(member)

    @pytest.mark.parametrize("recorded", ["boot-1:999", "boot-0:1", ""])
    def test_a_pid_held_by_someone_else_is_left_alone(self, linux: _FakeLinux, recorded: str) -> None:
        stranger = linux.add(BASE + 50, starttime=1)

        assert osproc.reap(stranger, recorded, TAG, grace_sec=0.3) is False

        assert linux.signals == [] and linux.alive(stranger)

    def test_tagged_stragglers_of_a_dead_root_are_still_found(self, linux: _FakeLinux) -> None:
        straggler = linux.add(BASE + 61, pgrp=BASE + 60, tag=TAG)
        other = linux.add(BASE + 62, pgrp=BASE + 60, tag="inst/p_2")

        assert osproc.reap(BASE + 60, "boot-1:5", TAG, grace_sec=0.3) is True

        assert linux.sent(TERM) == [("pid", straggler)]
        assert not linux.alive(straggler) and linux.alive(other)

    def test_everything_tagged_for_the_instance_is_reaped(self, linux: _FakeLinux) -> None:
        first = linux.add(BASE + 70, tag="inst/p_1")
        second = linux.add(BASE + 71, tag="inst/p_2")
        neighbour = linux.add(BASE + 72, tag="inst2/p_3")
        untagged = linux.add(BASE + 73)
        linux.stubborn.add(second)

        assert sorted(osproc.reap_instance("inst", grace_sec=0.2)) == [first, second]

        assert ("pid", second) in linux.sent(KILL)
        assert not linux.alive(first) and not linux.alive(second)
        assert linux.alive(neighbour) and linux.alive(untagged)

    def test_no_tagged_process_means_nothing_to_do(self, linux: _FakeLinux) -> None:
        linux.add(BASE + 80)

        assert osproc.reap_instance("inst", grace_sec=0.2) == []
        assert linux.signals == []


class TestOtherPosix:
    def test_the_fingerprint_comes_from_ps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osproc, "IS_WINDOWS", False)
        monkeypatch.setattr(osproc, "IS_LINUX", False)
        answers = iter([
            SimpleNamespace(returncode=0, stdout="Tue Oct 27 10:00:00 2026\n"),
            SimpleNamespace(returncode=1, stdout=""),
        ])
        monkeypatch.setattr(osproc.subprocess, "run", lambda *args, **kwargs: next(answers))

        assert osproc.fingerprint(123) == "Tue Oct 27 10:00:00 2026"
        assert osproc.fingerprint(123) is None
        assert osproc.tagged_pids(lambda value: True) == []

    def test_ps_failing_to_run_gives_no_fingerprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osproc, "IS_WINDOWS", False)
        monkeypatch.setattr(osproc, "IS_LINUX", False)

        def missing(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("ps")

        monkeypatch.setattr(osproc.subprocess, "run", missing)

        assert osproc.fingerprint(123) is None


def _snapshot_win(snapshot: list[tuple[int, int]], table: dict[int, tuple[int, bool]]) -> process_tree.Win32:
    win = process_tree.Win32.__new__(process_tree.Win32)
    win.snapshot = lambda: list(snapshot)  # type: ignore[method-assign]
    win.query = lambda pid: table.get(pid)  # type: ignore[method-assign]
    return win


class TestDescendants:
    def test_children_and_grandchildren_are_found(self) -> None:
        win = _snapshot_win(
            [(101, 100), (102, 101), (103, 100), (104, 103), (105, 999), (106, 106), (0, 0)],
            {100: (1000, True), 101: (1001, True), 102: (1002, True), 103: (900, True), 104: (1004, True),
             105: (1005, True), 106: (1006, True)},
        )

        # 103 比根进程还早创建：只是父 pid 撞了号，它和它的孩子都不算
        assert sorted(win.descendants(100, 1000)) == [(101, 1001), (102, 1002)]

    def test_an_exited_child_still_leads_to_its_orphans(self) -> None:
        win = _snapshot_win([(101, 100), (102, 101)], {100: (1000, False), 101: (1001, False), 102: (1002, True)})

        assert win.descendants(100, 1000) == [(102, 1002)]

    def test_after_the_root_pid_is_reused_only_children_from_before_count(self) -> None:
        win = _snapshot_win([(110, 100), (111, 100)], {100: (5000, True), 110: (1500, True), 111: (6000, True)})

        assert win.descendants(100, 1000) == [(110, 1500)]

    def test_processes_that_cannot_be_opened_are_skipped(self) -> None:
        win = _snapshot_win([(101, 100), (102, 100)], {100: (1000, True), 102: (1002, True)})

        assert win.descendants(100, 1000) == [(102, 1002)]


class TestPinning:
    @pytest.fixture
    def taskkills(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        record = SimpleNamespace(pids=[], result=True)

        def fake(pid: int) -> bool:
            record.pids.append(pid)
            return record.result

        monkeypatch.setattr(process_tree, "taskkill_tree", fake)
        return record

    @staticmethod
    def _handles(alive: bool) -> SimpleNamespace:
        state = SimpleNamespace(alive=alive, closed=[], terminated=[])
        state.win = SimpleNamespace(
            alive=lambda handle: state.alive,
            created=lambda handle: 1000,
            terminate=lambda handle: state.terminated.append(handle) is None,
            close=lambda handle: state.closed.append(handle),
        )
        return state

    def test_a_running_process_is_killed_with_its_tree_and_released(self, taskkills: SimpleNamespace) -> None:
        handles = self._handles(alive=True)

        process_tree.PinnedProcess(handles.win, 42, 7).kill_tree_and_close()

        assert taskkills.pids == [42] and handles.terminated == [] and handles.closed == [7]

    def test_when_taskkill_fails_the_process_itself_is_terminated(self, taskkills: SimpleNamespace) -> None:
        taskkills.result = False
        handles = self._handles(alive=True)

        process_tree.PinnedProcess(handles.win, 42, 7).kill_tree_and_close()

        assert handles.terminated == [7] and handles.closed == [7]

    def test_an_exited_process_is_not_looked_up_by_pid(self, taskkills: SimpleNamespace) -> None:
        handles = self._handles(alive=False)
        pinned = process_tree.PinnedProcess(handles.win, 42, 7)

        pinned.kill_tree_and_close()
        pinned.close()

        assert taskkills.pids == [] and handles.terminated == [] and handles.closed == [7]
        assert pinned.alive() is False and pinned.created() is None

    def test_pin_needs_a_handle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opened = {42: 7}
        monkeypatch.setattr(process_tree, "win32", lambda: SimpleNamespace(open=lambda pid, terminate: opened.get(pid)))

        assert process_tree.pin(41) is None
        pinned = process_tree.pin(42)
        assert pinned is not None and pinned.pid == 42

    def test_a_verified_kill_checks_the_creation_time_while_holding_the_handle(
        self, taskkills: SimpleNamespace,
    ) -> None:
        table = {101: (1001, True), 102: (1002, False)}
        win = process_tree.Win32.__new__(process_tree.Win32)

        @contextlib.contextmanager
        def handle(pid: int) -> Iterator[int | None]:
            yield pid if pid in table else None

        win.handle = handle  # type: ignore[method-assign]
        win.created = lambda handle: table[handle][0]  # type: ignore[method-assign]
        win.alive = lambda handle: table[handle][1]  # type: ignore[method-assign]

        assert win.kill_verified(101, 1001) is True
        assert win.kill_verified(101, 9999) is False
        assert win.kill_verified(102, 1002) is False
        assert win.kill_verified(103, 1003) is False
        assert taskkills.pids == [101]


class TestTaskkill:
    def test_the_command_line_and_its_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        codes = iter([0, 128])

        def run(argv: list[str], **kwargs: Any) -> Any:
            calls.append(argv)
            return SimpleNamespace(returncode=next(codes))

        monkeypatch.setattr(process_tree.subprocess, "run", run)

        assert process_tree.taskkill_tree(123) is True
        assert process_tree.taskkill_tree(123) is False
        assert calls[0] == ["taskkill", "/T", "/F", "/PID", "123"]

    def test_a_missing_taskkill_counts_as_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(argv: list[str], **kwargs: Any) -> Any:
            raise FileNotFoundError("taskkill")

        monkeypatch.setattr(process_tree.subprocess, "run", run)

        assert process_tree.taskkill_tree(123) is False
