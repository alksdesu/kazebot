"""读后改前的戳：supervisor 按任务、会话两层记账，按内容比；写盘前在跨进程锁里再比一次，挡住别的进程插进来的改动。

回显原文：本会话经读权限看过的这一版直接放行；没看过就只问一次读策略（不送审），auto 才放行。
engine 这一侧向 supervisor 认领文件、登记读戳的几个函数，supervisor 连不上时一律不写。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _lock_holder import WAIT_SEC, LockHolder  # noqa: E402
from toolbox import edit_journal, file_state  # noqa: E402
from toolbox.file_state import (  # noqa: E402
    ConcurrentEdit,
    EditBlocked,
    EditGrant,
    Entry,
    Gate,
    StampBook,
    commit_locked,
    edit_lock,
    edit_lock_path,
    gate_of,
    stamp_of,
    take_snapshot,
)

# 没被挡住的话这点时间足够进到锁里
_BLOCKED_FOR = 0.5


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def entry(data: bytes = b"x\n", *, revealable: bool = True, mtime_ns: int = 1) -> Entry:
    return Entry(stamp_of(data, mtime_ns), revealable)


class TestTheSessionKey:
    def test_the_context_archive_session_wins(self) -> None:
        ctx = SimpleNamespace(session_id="branch_1", _context_archive_session_id="main-session")

        assert file_state.session_key(ctx) == "main-session"

    def test_falls_back_to_the_session_id(self) -> None:
        assert file_state.session_key(SimpleNamespace(session_id="sess-9")) == "sess-9"

    def test_a_context_without_either_still_has_a_key(self) -> None:
        # 测试和离线脚本里的上下文可能连 session_id 都没有，不能因此崩掉
        assert file_state.session_key(SimpleNamespace()) == ""


class TestThePathKey:
    def test_another_spelling_of_the_same_file_has_the_same_key(self, tmp_path: Path) -> None:
        assert file_state.path_key(tmp_path / "sub" / ".." / "a.txt") == file_state.path_key(tmp_path / "a.txt")

    @pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
    def test_case_does_not_matter_on_windows(self, tmp_path: Path) -> None:
        assert file_state.path_key(tmp_path / "A.TXT") == file_state.path_key(tmp_path / "a.txt")


class TestGateOf:
    def test_a_file_never_read_is_unread(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")

        gate = gate_of(None, take_snapshot(path))

        assert gate.unread and not gate.stale and not gate.revealable

    def test_the_same_content_is_fresh_and_revealable(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        snapshot = take_snapshot(path)

        gate = gate_of(Entry(snapshot.stamp, True), snapshot)  # type: ignore[arg-type]

        assert not gate.unread and not gate.stale and gate.revealable

    def test_changed_content_is_stale(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        seen = Entry(take_snapshot(path).stamp, True)  # type: ignore[arg-type]
        path.write_bytes(b"y\n")

        gate = gate_of(seen, take_snapshot(path))

        assert gate.stale and not gate.revealable
        assert gate.stale_message == file_state.STALE_MESSAGE

    def test_touching_a_file_without_changing_it_is_not_a_change(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        seen = Entry(take_snapshot(path).stamp, True)  # type: ignore[arg-type]
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

        assert not gate_of(seen, take_snapshot(path)).stale

    def test_a_deleted_file_is_vanished(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        seen = Entry(take_snapshot(path).stamp, True)  # type: ignore[arg-type]
        path.unlink()

        gate = gate_of(seen, take_snapshot(path))

        assert gate.vanished and not gate.stale and not gate.revealable

    def test_an_entry_written_without_read_permission_is_not_revealable(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        stamp = take_snapshot(path).stamp

        assert not gate_of(Entry(stamp, False), take_snapshot(path)).revealable  # type: ignore[arg-type]
        assert gate_of(Entry(stamp, True), take_snapshot(path)).revealable  # type: ignore[arg-type]

    def test_a_foreign_version_is_stale_even_when_the_content_matches(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        snapshot = take_snapshot(path)

        gate = gate_of(Entry(snapshot.stamp, True), snapshot, foreign=True)  # type: ignore[arg-type]

        assert gate.stale and gate.foreign and not gate.revealable
        assert gate.stale_message == file_state.FOREIGN_MESSAGE

    def test_a_foreign_version_of_a_deleted_file_is_vanished(self, tmp_path: Path) -> None:
        gate = gate_of(entry(), take_snapshot(tmp_path / "absent.txt"), foreign=True)

        assert gate.vanished and gate.foreign and not gate.stale

    def test_a_grant_carries_the_foreign_flag_into_its_gate(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        snapshot = take_snapshot(path)
        seen = Entry(snapshot.stamp, True)  # type: ignore[arg-type]

        assert not EditGrant("k", seen).gate(snapshot).stale
        assert EditGrant("k", seen, foreign=True).gate(snapshot).stale
        assert EditGrant("k").gate(snapshot).unread


class TestTheWireFormat:
    def test_a_stamp_survives_the_round_trip(self) -> None:
        stamp = stamp_of(b"data\n", 123)

        assert file_state.stamp_from_wire(file_state.stamp_to_wire(stamp)) == stamp

    def test_an_entry_survives_the_round_trip(self) -> None:
        for revealable in (True, False):
            seen = entry(revealable=revealable)

            assert file_state.entry_from_wire(file_state.entry_to_wire(seen)) == seen

    def test_no_entry_is_none_both_ways(self) -> None:
        assert file_state.entry_to_wire(None) is None
        assert file_state.entry_from_wire(None) is None

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "stamp",
            {},
            {"mtime_ns": True, "size": 1, "sha256": "a" * 64},
            {"mtime_ns": 1, "size": -1, "sha256": "a" * 64},
            {"mtime_ns": 1.5, "size": 1, "sha256": "a" * 64},
            {"mtime_ns": "1", "size": 1, "sha256": "a" * 64},
            {"mtime_ns": 1, "size": 1, "sha256": "a" * 63},
            {"mtime_ns": 1, "size": 1, "sha256": "A" * 64},
            {"mtime_ns": 1, "size": 1, "sha256": "g" * 64},
        ],
    )
    def test_a_malformed_stamp_is_rejected(self, raw: Any) -> None:
        assert file_state.stamp_from_wire(raw) is None
        assert file_state.entry_from_wire(raw) is None


class TestTheStampBook:
    def test_a_task_sees_its_own_reading_first(self) -> None:
        book = StampBook()
        book.record("s", "", "k", entry(b"session\n"), now=1)
        book.record("s", "t1", "k", entry(b"mine\n"), now=2)

        found = book.lookup("s", "t1", "k", started_at=0)

        assert found.entry == entry(b"mine\n") and found.layer == "task" and not found.foreign

    def test_a_parallel_task_does_not_see_another_task_s_reading(self) -> None:
        book = StampBook()
        book.record("s", "t1", "k", entry(b"v1\n"), now=1)

        found = book.lookup("s", "t2", "k", started_at=0)

        assert found.entry is None and found.layer == ""

    def test_a_reading_merges_into_the_session_when_its_task_ends(self) -> None:
        book = StampBook()
        book.record("s", "t1", "k", entry(b"v1\n"), now=1)

        assert book.merge_task("t1", at=2) == 1

        later = book.lookup("s", "t2", "k", started_at=3)
        assert later.entry == entry(b"v1\n") and later.layer == "session" and not later.foreign
        assert book.task_ids() == [] and len(book) == 1

    def test_a_merge_after_a_task_started_is_foreign_to_it(self) -> None:
        book = StampBook()
        book.record("s", "t2", "k", entry(b"v2\n"), now=5)
        book.merge_task("t2", at=6)

        running = book.lookup("s", "t1", "k", started_at=4)
        anonymous = book.lookup("s", "", "k", started_at=None)

        assert running.entry == entry(b"v2\n") and running.foreign
        assert anonymous.entry == entry(b"v2\n") and not anonymous.foreign

    def test_a_task_s_own_reading_wins_over_a_foreign_merge(self) -> None:
        # 并行的另一个任务把新版本并进会话层，不会刷新本任务读到的旧版本：本任务下次改时按旧版本比，判为过期
        book = StampBook()
        book.record("s", "t1", "k", entry(b"old\n"), now=1)
        book.record("s", "t2", "k", entry(b"new\n"), now=2)
        book.merge_task("t2", at=3)

        found = book.lookup("s", "t1", "k", started_at=0)

        assert found.entry == entry(b"old\n") and found.layer == "task"

    def test_merging_keeps_the_later_reading(self) -> None:
        book = StampBook()
        book.record("s", "t-late", "k", entry(b"late\n"), now=5)
        book.record("s", "t-early", "k", entry(b"early\n"), now=3)
        book.merge_task("t-late", at=6)

        book.merge_task("t-early", at=7)

        assert book.lookup("s", "", "k", started_at=None).entry == entry(b"late\n")

    def test_a_reading_recorded_after_its_task_ended_is_not_dated_back(self) -> None:
        # 回收之后 worker 还记进来的读戳：并入时刻取它记下的时刻，期间开始的任务要把它当 foreign
        book = StampBook()
        book.record("s", "t-reaped", "k", entry(b"v\n"), now=10)
        book.merge_task("t-reaped", at=5)

        assert book.lookup("s", "t-new", "k", started_at=7).foreign
        assert not book.lookup("s", "t-newer", "k", started_at=11).foreign

    def test_a_reading_without_a_task_goes_straight_to_the_session(self) -> None:
        book = StampBook()

        assert book.record("s", "", "k", entry(), now=4) == "session"
        assert book.task_ids() == []
        assert book.lookup("s", "t1", "k", started_at=3).foreign
        assert not book.lookup("s", "t1", "k", started_at=5).foreign

    def test_sessions_are_kept_apart(self) -> None:
        book = StampBook()
        book.record("one", "", "k", entry(), now=1)
        book.record("one", "t1", "k2", entry(), now=1)

        assert book.lookup("two", "", "k", started_at=None).entry is None
        assert book.lookup("two", "t1", "k2", started_at=None).entry is None

    def test_merging_an_unknown_task_does_nothing(self) -> None:
        assert StampBook().merge_task("nobody", at=1) == 0

    def test_a_task_layer_drops_its_least_recently_recorded_file(self) -> None:
        book = StampBook(max_task_entries=2)
        for number, key in enumerate(("a", "b", "c")):
            book.record("s", "t1", key, entry(), now=number)

        assert book.task_entries("t1") == 2
        assert book.lookup("s", "t1", "a", started_at=0).entry is None
        assert book.lookup("s", "t1", "c", started_at=0).entry is not None

    def test_the_least_recently_active_task_layer_is_dropped(self) -> None:
        book = StampBook(max_task_layers=2)
        book.record("s", "t1", "k", entry(), now=1)
        book.record("s", "t2", "k", entry(), now=2)
        book.record("s", "t1", "k2", entry(), now=3)
        book.record("s", "t3", "k", entry(), now=4)

        assert sorted(book.task_ids()) == ["t1", "t3"]

    def test_the_session_layer_drops_its_least_recently_used_entry(self) -> None:
        book = StampBook(max_session_entries=2)
        book.record("s", "", "a", entry(), now=1)
        book.record("s", "", "b", entry(), now=2)
        assert book.lookup("s", "", "a", started_at=None).entry is not None
        book.record("s", "", "c", entry(), now=3)

        assert len(book) == 2
        assert book.lookup("s", "", "b", started_at=None).entry is None
        assert book.lookup("s", "", "a", started_at=None).entry is not None


class TestTakeSnapshot:
    def test_a_missing_file_does_not_exist(self, tmp_path: Path) -> None:
        snapshot = take_snapshot(tmp_path / "absent.txt")

        assert not snapshot.exists and snapshot.stamp is None and snapshot.data == b""

    def test_the_stamp_covers_the_whole_file(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")

        snapshot = take_snapshot(path)

        assert snapshot.data == b"one\ntwo\n"
        assert snapshot.stamp is not None
        assert snapshot.stamp.size == 8
        assert snapshot.stamp.sha256 == stamp_of(b"one\ntwo\n", 0).sha256


class TestCommit:
    def test_a_file_changed_underneath_is_not_overwritten(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"mine\n")
        expected = take_snapshot(path)
        path.write_bytes(b"someone else\n")

        with edit_lock(path, tmp_path), pytest.raises(ConcurrentEdit):
            commit_locked(path, b"new\n", expected=expected)

        assert path.read_bytes() == b"someone else\n"

    def test_an_unchanged_file_is_replaced_and_the_new_stamp_returned(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"old\n")
        expected = take_snapshot(path)

        with edit_lock(path, tmp_path):
            stamp = commit_locked(path, b"new\n", expected=expected)

        assert path.read_bytes() == b"new\n"
        assert stamp.sha256 == stamp_of(b"new\n", 0).sha256 and stamp.size == 4

    def test_a_missing_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.txt"
        expected = take_snapshot(path)

        with edit_lock(path, tmp_path):
            commit_locked(path, b"hello\n", expected=expected)

        assert path.read_bytes() == b"hello\n"

    def test_a_file_that_appeared_meanwhile_is_not_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.txt"
        expected = take_snapshot(path)
        path.write_bytes(b"created elsewhere\n")

        with edit_lock(path, tmp_path), pytest.raises(ConcurrentEdit):
            commit_locked(path, b"hello\n", expected=expected)

        assert path.read_bytes() == b"created elsewhere\n"


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


def _in_thread(body: Callable[[], Any]) -> tuple[threading.Event, dict[str, Any]]:
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = body()
        except Exception as exc:
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done, outcome


class TestTheEditLock:
    def test_the_lock_file_lives_where_the_edit_tools_refuse_to_write(self, tmp_path: Path) -> None:
        lock = edit_lock_path(tmp_path / "src" / "a.py", tmp_path)

        assert edit_journal.inside_journal(tmp_path, lock)
        assert lock.parent == edit_journal.journal_root(tmp_path) / file_state.LOCK_SUBDIR
        assert lock.name.startswith("edit-") and lock.suffix == ".lock"

    def test_another_spelling_of_the_same_file_takes_the_same_lock(self, tmp_path: Path) -> None:
        assert edit_lock_path(tmp_path / "x" / ".." / "a.txt", tmp_path) == edit_lock_path(tmp_path / "a.txt", tmp_path)

    def test_files_share_a_bounded_set_of_lock_files(self, tmp_path: Path) -> None:
        locks = {edit_lock_path(tmp_path / f"f{number}.txt", tmp_path) for number in range(1000)}

        assert 1 < len(locks) <= 64

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_taking_the_lock_creates_the_journal_directory_private(self, tmp_path: Path) -> None:
        with edit_lock(tmp_path / "a.txt", tmp_path):
            pass

        assert edit_journal.journal_root(tmp_path).stat().st_mode & 0o777 == file_state.PRIVATE_DIR_MODE

    def test_another_process_holding_the_lock_makes_the_edit_wait(self, tmp_path: Path, hold) -> None:
        path = seed(tmp_path / "a.txt", b"one\n")
        holder = hold(edit_lock_path(path, tmp_path))
        entered = threading.Event()

        def body() -> None:
            with edit_lock(path, tmp_path):
                entered.set()

        done, outcome = _in_thread(body)

        assert not entered.wait(_BLOCKED_FOR)
        holder.release()
        assert done.wait(WAIT_SEC) and entered.is_set() and "error" not in outcome

    def test_a_change_made_while_another_process_holds_the_lock_is_not_overwritten(self, tmp_path: Path, hold) -> None:
        path = seed(tmp_path / "a.txt", b"mine\n")
        expected = take_snapshot(path)
        holder = hold(edit_lock_path(path, tmp_path))

        def body() -> file_state.Stamp:
            with edit_lock(path, tmp_path):
                return commit_locked(path, b"new\n", expected=expected)

        done, outcome = _in_thread(body)
        assert not done.wait(_BLOCKED_FOR)
        # 拿着锁的那个进程在它的锁里改了文件
        path.write_bytes(b"theirs\n")
        holder.release()

        assert done.wait(WAIT_SEC)
        assert isinstance(outcome.get("error"), ConcurrentEdit)
        assert path.read_bytes() == b"theirs\n"

    def test_a_lock_on_another_stripe_does_not_hold_up_this_file(self, tmp_path: Path, hold) -> None:
        path = seed(tmp_path / "a.txt", b"one\n")
        mine = edit_lock_path(path, tmp_path)
        other = next(
            candidate
            for candidate in (tmp_path / f"other-{number}.txt" for number in range(1000))
            if edit_lock_path(candidate, tmp_path) != mine
        )
        hold(edit_lock_path(other, tmp_path))
        expected = take_snapshot(path)

        done, outcome = _in_thread(lambda: _commit_under_lock(path, tmp_path, expected))

        assert done.wait(WAIT_SEC) and "error" not in outcome
        assert path.read_bytes() == b"new\n"


def _commit_under_lock(path: Path, workspace: Path, expected: file_state.FileSnapshot) -> file_state.Stamp:
    with edit_lock(path, workspace):
        return commit_locked(path, b"new\n", expected=expected)


class _Client:
    """只认 /v1/file_edits 那几个方法的上下文替身：按预设回复或抛错，记下每次调用。"""

    def __init__(
        self,
        *,
        task_id: str = "task-1",
        session: str = "sess-1",
        replies: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.task_id = task_id
        self.session_id = session
        self.replies = dict(replies or {})
        self.error = error
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def _answer(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.replies.get(name, {"ok": True})

    async def begin_file_edit(self, path: str, *, display: str, session: str) -> Any:
        return await self._answer("begin", path, display=display, session=session)

    async def record_file_stamp(self, path: str, *, session: str, stamp: dict[str, Any], revealable: bool) -> Any:
        return await self._answer("record", path, session=session, stamp=stamp, revealable=revealable)

    async def release_file_claim(self, path: str) -> Any:
        return await self._answer("release", path)

    async def file_edit_scope(self) -> Any:
        return await self._answer("scope")

    def names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestBeginEdit:
    def test_a_granted_edit_carries_the_stamp_and_the_journal_session(self, tmp_path: Path) -> None:
        seen = entry(b"v1\n")
        client = _Client(replies={"begin": {
            "ok": True, "new_claim": True, "journal_session": "root-sess",
            "stamp": file_state.entry_to_wire(seen), "stamp_layer": "task", "foreign": False,
        }})

        grant = run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        assert grant == EditGrant(file_state.path_key(tmp_path / "a.txt"), seen, False, "root-sess", True)
        [(name, args, kwargs)] = client.calls
        assert name == "begin" and args == (file_state.path_key(tmp_path / "a.txt"),)
        assert kwargs == {"display": "a.txt", "session": "sess-1"}

    def test_a_foreign_stamp_is_passed_on(self, tmp_path: Path) -> None:
        client = _Client(replies={"begin": {"ok": True, "stamp": file_state.entry_to_wire(entry()), "foreign": True}})

        grant = run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        assert grant.foreign and not grant.new_claim and grant.journal_session == ""

    def test_a_file_another_task_is_editing_is_refused_with_that_task_named(self, tmp_path: Path) -> None:
        client = _Client(replies={"begin": {"ok": False, "reason": "claimed", "holder": {
            "task_id": "task-9", "node_id": "bootstrap.coder", "status": "suspended",
            "session_id": "sess-9", "path": "a.txt", "since": "2026-10-27T08:00:00+00:00",
        }}})

        with pytest.raises(EditBlocked) as caught:
            run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        blocked = caught.value
        assert "task-9" in blocked.message and "bootstrap.coder" in blocked.message and "挂起等子任务" in blocked.message
        assert blocked.data["claimed"] is True and not blocked.unavailable
        assert blocked.data["claimedBy"] == {
            "taskId": "task-9", "nodeId": "bootstrap.coder", "status": "suspended",
            "since": "2026-10-27T08:00:00+00:00", "sessionId": "sess-9",
        }

    def test_a_holder_with_missing_details_still_reads(self, tmp_path: Path) -> None:
        client = _Client(replies={"begin": {"ok": False, "reason": "claimed", "holder": "garbage"}})

        with pytest.raises(EditBlocked) as caught:
            run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        assert "状态不详" in caught.value.message and caught.value.data["claimedBy"]["taskId"] == "?"

    @pytest.mark.parametrize(
        "reply,needle",
        [
            ({"ok": False, "reason": "task_ended", "status": "cancelled"}, "cancelled"),
            ({"ok": False, "reason": "task_unknown"}, "找不到本任务"),
            ({"ok": False, "reason": "odd"}, "odd"),
            ({"ok": False}, "没有说明原因"),
            ("not a mapping", "没有说明原因"),
            ({"ok": "yes"}, "没有说明原因"),
        ],
    )
    def test_other_refusals_are_explained_and_nothing_is_written(self, tmp_path: Path, reply: Any, needle: str) -> None:
        client = _Client(replies={"begin": reply})

        with pytest.raises(EditBlocked) as caught:
            run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        assert needle in caught.value.message and "没有写入" in caught.value.message
        assert not caught.value.unavailable

    def test_an_unreachable_supervisor_blocks_the_edit(self, tmp_path: Path) -> None:
        client = _Client(error=ConnectionError("refused"))

        with pytest.raises(EditBlocked) as caught:
            run(file_state.begin_edit(client, tmp_path / "a.txt", "a.txt"))

        assert caught.value.unavailable
        assert caught.value.message.startswith(file_state.UNAVAILABLE_MESSAGE) and "refused" in caught.value.message

    def test_a_context_without_the_supervisor_interface_blocks_the_edit(self, tmp_path: Path) -> None:
        with pytest.raises(EditBlocked) as caught:
            run(file_state.begin_edit(SimpleNamespace(session_id="s"), tmp_path / "a.txt", "a.txt"))

        assert caught.value.unavailable


class TestRecordingAndReleasing:
    def test_a_reading_is_recorded_under_the_session_key(self, tmp_path: Path) -> None:
        client = _Client()
        stamp = stamp_of(b"x\n", 7)

        assert run(file_state.record_stamp(client, tmp_path / "a.txt", stamp, revealable=True)) is True

        [(name, args, kwargs)] = client.calls
        assert name == "record" and args == (file_state.path_key(tmp_path / "a.txt"),)
        assert kwargs == {"session": "sess-1", "stamp": file_state.stamp_to_wire(stamp), "revealable": True}

    @pytest.mark.parametrize("client", [
        _Client(error=ConnectionError("down")),
        _Client(replies={"record": {"ok": False}}),
        _Client(replies={"record": None}),
        SimpleNamespace(session_id="s"),
    ])
    def test_a_reading_that_cannot_be_recorded_reports_false(self, tmp_path: Path, client: Any) -> None:
        assert run(file_state.record_stamp(client, tmp_path / "a.txt", stamp_of(b"x", 1), revealable=True)) is False

    def test_finishing_records_the_new_version_and_keeps_the_claim(self) -> None:
        client = _Client()
        grant = EditGrant("key", new_claim=True)

        assert run(file_state.finish_edit(client, grant, stamp_of(b"new", 1), revealable=False)) is True

        assert client.names() == ["record"]
        assert client.calls[0][2]["revealable"] is False

    def test_finishing_a_deletion_records_nothing(self) -> None:
        client = _Client()

        assert run(file_state.finish_edit(client, EditGrant("key", new_claim=True), None, revealable=False)) is True
        assert client.calls == []

    def test_abandoning_returns_only_a_claim_taken_this_time(self) -> None:
        fresh, held = _Client(), _Client()

        run(file_state.abandon_edit(fresh, EditGrant("key", new_claim=True)))
        run(file_state.abandon_edit(held, EditGrant("key", new_claim=False)))

        assert fresh.calls == [("release", ("key",), {})]
        assert held.calls == []

    def test_abandoning_never_raises(self) -> None:
        run(file_state.abandon_edit(_Client(error=ConnectionError("down")), EditGrant("key", new_claim=True)))
        run(file_state.abandon_edit(SimpleNamespace(), EditGrant("key", new_claim=True)))


class TestJournalSession:
    def test_without_a_task_the_own_session_is_used_without_asking(self) -> None:
        client = _Client(task_id="")

        assert run(file_state.journal_session(client)) == "sess-1"
        assert client.calls == []

    def test_the_supervisor_names_the_root_session(self) -> None:
        client = _Client(replies={"scope": {"ok": True, "journal_session": "root-sess"}})

        assert run(file_state.journal_session(client)) == "root-sess"

    @pytest.mark.parametrize("reply", [{"ok": True, "journal_session": ""}, {"ok": False, "reason": "task_unknown"}, None])
    def test_no_answer_falls_back_to_the_own_session(self, reply: Any) -> None:
        assert run(file_state.journal_session(_Client(replies={"scope": reply}))) == "sess-1"

    def test_an_unreachable_supervisor_is_an_error(self) -> None:
        with pytest.raises(EditBlocked) as caught:
            run(file_state.journal_session(_Client(error=ConnectionError("down"))))

        assert caught.value.unavailable and "连不上 supervisor" in caught.value.message

    def test_a_context_without_the_supervisor_interface_is_an_error(self) -> None:
        with pytest.raises(EditBlocked):
            run(file_state.journal_session(SimpleNamespace(session_id="s", task_id="t")))


class _Evaluator:
    """只认 evaluate_op 的上下文替身：按预设回结论，记下问过什么；request_op 一被调用就算错。"""

    def __init__(self, level: str = "auto", *, error: Exception | None = None) -> None:
        self.level = level
        self.error = error
        self.asked: list[tuple[str, dict]] = []

    async def evaluate_op(self, op: str, parameters: dict) -> dict:
        self.asked.append((op, dict(parameters)))
        if self.error is not None:
            raise self.error
        return {"safety_level": self.level, "reason": "test"}

    async def request_op(self, op: str, parameters: dict) -> dict:
        raise AssertionError("回显只问结论，不能送审")


def reveal(ctx: object, gate: Gate, path: str = "notes/a.txt") -> bool:
    return asyncio.run(file_state.reveal_allowed(ctx, gate, path))


class TestRevealAllowed:
    def test_a_version_seen_through_read_file_is_revealed_without_asking(self) -> None:
        ctx = _Evaluator("deny")

        assert reveal(ctx, Gate(Entry(stamp_of(b"x", 1), True))) is True
        assert ctx.asked == []

    def test_an_unread_file_is_revealed_when_reading_it_is_auto(self) -> None:
        ctx = _Evaluator("auto")

        assert reveal(ctx, Gate(None), "src/app.py") is True
        assert ctx.asked == [("read_file", {"path": "src/app.py"})]

    @pytest.mark.parametrize("level", ["approval_required", "deny", "", "unknown"])
    def test_an_unread_file_stays_hidden_unless_the_answer_is_auto(self, level: str) -> None:
        ctx = _Evaluator(level)

        assert reveal(ctx, Gate(None)) is False
        assert len(ctx.asked) == 1

    def test_an_entry_written_without_a_read_asks_the_policy(self) -> None:
        assert reveal(_Evaluator("approval_required"), Gate(Entry(stamp_of(b"x", 1), False))) is False
        assert reveal(_Evaluator("auto"), Gate(Entry(stamp_of(b"x", 1), False))) is True

    def test_a_stale_or_vanished_version_needs_the_policy(self) -> None:
        seen = Entry(stamp_of(b"x", 1), True)

        assert reveal(_Evaluator("deny"), Gate(seen, stale=True)) is False
        assert reveal(_Evaluator("deny"), Gate(seen, vanished=True)) is False
        assert reveal(_Evaluator("auto"), Gate(seen, stale=True)) is True

    def test_a_foreign_version_needs_the_policy(self) -> None:
        seen = Entry(stamp_of(b"x", 1), True)

        assert reveal(_Evaluator("deny"), Gate(seen, stale=True, foreign=True)) is False

    def test_a_failed_policy_check_hides_the_text(self) -> None:
        assert reveal(_Evaluator(error=RuntimeError("supervisor down")), Gate(None)) is False

    def test_a_context_that_cannot_ask_hides_the_text(self) -> None:
        assert reveal(SimpleNamespace(), Gate(None)) is False

    def test_an_answer_that_is_not_a_mapping_hides_the_text(self) -> None:
        class Odd(_Evaluator):
            async def evaluate_op(self, op: str, parameters: dict) -> dict:
                return "auto"  # type: ignore[return-value]

        assert reveal(Odd(), Gate(None)) is False
