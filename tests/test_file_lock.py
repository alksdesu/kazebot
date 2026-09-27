"""跨进程互斥锁：多开的几个 supervisor 靠它轮流改实例清单、合并分支、给共用的图库打标。"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _lock_holder import WAIT_SEC, LockHolder  # noqa: E402
from file_lock import exclusive_lock, try_exclusive_lock  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.instances import instances_file, save_instance  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_WAIT = WAIT_SEC


@pytest.fixture
def hold() -> Iterator[Callable[[Path], LockHolder]]:
    started: list[LockHolder] = []

    def start(path: Path) -> LockHolder:
        holder = LockHolder(path)
        started.append(holder)
        return holder

    yield start
    for holder in started:
        holder.close()


def _free(path: Path) -> bool:
    with try_exclusive_lock(path) as held:
        return held


def _eventually_free(path: Path) -> bool:
    # 进程被杀之后，Windows 放掉它的文件锁可能要慢一拍。
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if _free(path):
            return True
        time.sleep(0.05)
    return False


def _in_thread(body: Callable[[], None]) -> tuple[threading.Thread, threading.Event]:
    done = threading.Event()

    def run() -> None:
        body()
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done


class TestAcrossProcesses:
    def test_a_lock_held_by_another_process_is_not_handed_out(self, tmp_path: Path, hold) -> None:
        path = tmp_path / "library.lock"
        holder = hold(path)

        assert _free(path) is False

        holder.release()
        assert _free(path) is True

    def test_a_holder_that_dies_leaves_the_lock_free(self, tmp_path: Path, hold) -> None:
        # 这是落到文件锁上的全部意义：进程被杀了不会留下一把没人能解的锁。
        path = tmp_path / "library.lock"
        hold(path).kill()

        assert _eventually_free(path)

    def test_the_blocking_form_waits_for_the_other_process(self, tmp_path: Path, hold) -> None:
        path = tmp_path / "library.lock"
        holder = hold(path)

        def take() -> None:
            with exclusive_lock(path):
                pass

        thread, done = _in_thread(take)
        assert not done.wait(0.5)

        holder.release()
        assert done.wait(_WAIT)
        thread.join(_WAIT)


class TestWithinOneProcess:
    def test_threads_take_turns(self, tmp_path: Path) -> None:
        path = tmp_path / "library.lock"
        entered, leave = threading.Event(), threading.Event()

        def hold_it() -> None:
            with exclusive_lock(path):
                entered.set()
                leave.wait(_WAIT)

        thread, _done = _in_thread(hold_it)
        assert entered.wait(_WAIT)

        assert _free(path) is False
        # 拿不到的那次不能顺手把别人的进程锁放掉。
        assert _free(path) is False

        leave.set()
        thread.join(_WAIT)
        assert _free(path) is True

    def test_two_spellings_of_one_file_are_one_lock(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()

        with exclusive_lock(tmp_path / "library.lock"):
            assert _free(tmp_path / "sub" / ".." / "library.lock") is False

    def test_the_lock_is_released_when_the_body_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "library.lock"

        with pytest.raises(RuntimeError):
            with exclusive_lock(path):
                raise RuntimeError("boom")
        with pytest.raises(RuntimeError):
            with try_exclusive_lock(path) as held:
                assert held
                raise RuntimeError("boom")

        assert _free(path) is True

    def test_missing_directories_are_created(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "locks" / "library.lock"

        with try_exclusive_lock(path) as held:
            assert held
        assert path.is_file()


@pytest.mark.skipif(os.name != "nt", reason="只有 msvcrt 会自己放弃等待")
class TestMsvcrt:
    @staticmethod
    def _flaky(monkeypatch: pytest.MonkeyPatch, mode_name: str, *errors: OSError) -> list[int]:
        import msvcrt

        real, pending, seen = msvcrt.locking, list(errors), []
        mode = getattr(msvcrt, mode_name)

        def locking(fd: int, requested: int, nbytes: int) -> None:
            if requested == mode and pending:
                seen.append(requested)
                raise pending.pop(0)
            real(fd, requested, nbytes)

        monkeypatch.setattr(msvcrt, "locking", locking)
        return seen

    def test_the_blocking_form_keeps_waiting_after_msvcrt_gives_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # LK_LOCK 重试十秒就报 EDEADLOCK；阻塞锁要和 Linux 的 flock 一样接着等，不能把错误甩给调用方。
        gave_up = OSError(errno.EDEADLOCK, "Resource deadlock avoided")
        seen = self._flaky(monkeypatch, "LK_LOCK", gave_up, gave_up)

        with exclusive_lock(tmp_path / "library.lock"):
            pass

        assert len(seen) == 2

    def test_an_unexpected_failure_is_not_mistaken_for_a_held_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._flaky(monkeypatch, "LK_NBLCK", OSError(errno.EBADF, "Bad file descriptor"))

        with pytest.raises(OSError):
            with try_exclusive_lock(tmp_path / "library.lock"):
                pass

    def test_a_held_region_reads_as_not_acquired(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._flaky(monkeypatch, "LK_NBLCK", OSError(errno.EACCES, "Permission denied"))

        assert _free(tmp_path / "library.lock") is False
        assert _free(tmp_path / "library.lock") is True


class TestCallers:
    def test_the_instance_list_waits_for_another_supervisor(self, tmp_path: Path, hold) -> None:
        path = instances_file(tmp_path)
        holder = hold(path.with_name(f"{path.name}.lock"))

        thread, done = _in_thread(lambda: save_instance(tmp_path, instance_id="10001", label="a", idx=1))
        assert not done.wait(0.5)

        holder.release()
        assert done.wait(_WAIT)
        thread.join(_WAIT)
        assert "10001" in path.read_text(encoding="utf-8")

    def test_branch_merges_of_one_parent_take_turns(self, tmp_path: Path) -> None:
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-lock"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )
        entered, leave = threading.Event(), threading.Event()

        def merge_parent() -> None:
            with state._branch_parent_write_lock("parent"):
                entered.set()
                leave.wait(_WAIT)

        first, _first_done = _in_thread(merge_parent)
        assert entered.wait(_WAIT)

        def enter(session_id: str) -> Callable[[], None]:
            def body() -> None:
                with state._branch_parent_write_lock(session_id):
                    pass
            return body

        other, other_done = _in_thread(enter("other-parent"))
        same, same_done = _in_thread(enter("parent"))
        assert other_done.wait(_WAIT)
        assert not same_done.wait(0.5)

        leave.set()
        assert same_done.wait(_WAIT)
        for thread in (first, other, same):
            thread.join(_WAIT)
