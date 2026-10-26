"""读后改前的戳：按「会话 + 路径」记、按内容比，LRU 有上限；写盘前最后一刻再比一次，挡住别的进程插进来的改动。

回显原文只认本会话经读权限看过的这一版，不另发读策略请求。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox import file_state  # noqa: E402
from toolbox.file_state import (  # noqa: E402
    ConcurrentEdit,
    Entry,
    Gate,
    StampStore,
    commit_locked,
    edit_lock,
    inspect,
    recall,
    remember,
    stamp_of,
    take_snapshot,
)


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestTheSessionKey:
    def test_the_context_archive_session_wins(self) -> None:
        ctx = SimpleNamespace(session_id="branch_1", _context_archive_session_id="main-session")

        assert file_state.session_key(ctx) == "main-session"

    def test_falls_back_to_the_session_id(self) -> None:
        assert file_state.session_key(SimpleNamespace(session_id="sess-9")) == "sess-9"

    def test_a_context_without_either_still_has_a_key(self) -> None:
        # 测试和离线脚本里的上下文可能连 session_id 都没有，不能因此崩掉
        assert file_state.session_key(SimpleNamespace()) == ""


class TestTheStore:
    def test_the_least_recently_used_entry_is_evicted(self, tmp_path: Path) -> None:
        store = StampStore(max_entries=2)
        stamp = stamp_of(b"x", 1)
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"

        store.put("s", a, Entry(stamp, True))
        store.put("s", b, Entry(stamp, True))
        assert store.get("s", a) is not None
        store.put("s", c, Entry(stamp, True))

        assert len(store) == 2
        assert store.get("s", b) is None
        assert store.get("s", a) is not None and store.get("s", c) is not None

    def test_entries_are_kept_per_session(self, tmp_path: Path) -> None:
        store = StampStore()
        store.put("one", tmp_path / "f", Entry(stamp_of(b"x", 1), True))

        assert store.get("two", tmp_path / "f") is None


class TestInspect:
    def test_a_file_never_read_is_unread(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")

        gate = inspect("inspect-unread", path, take_snapshot(path))

        assert gate.unread and not gate.stale and not gate.revealable

    def test_the_same_content_is_fresh_and_revealable(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        remember("inspect-fresh", path, take_snapshot(path).stamp, revealable=True)  # type: ignore[arg-type]

        gate = inspect("inspect-fresh", path, take_snapshot(path))

        assert not gate.unread and not gate.stale and gate.revealable

    def test_changed_content_is_stale(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        remember("inspect-stale", path, take_snapshot(path).stamp, revealable=True)  # type: ignore[arg-type]
        path.write_bytes(b"y\n")

        gate = inspect("inspect-stale", path, take_snapshot(path))

        assert gate.stale and not gate.revealable

    def test_touching_a_file_without_changing_it_is_not_a_change(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        remember("inspect-touch", path, take_snapshot(path).stamp, revealable=True)  # type: ignore[arg-type]
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

        assert not inspect("inspect-touch", path, take_snapshot(path)).stale

    def test_a_deleted_file_is_vanished(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        remember("inspect-gone", path, take_snapshot(path).stamp, revealable=True)  # type: ignore[arg-type]
        path.unlink()

        gate = inspect("inspect-gone", path, take_snapshot(path))

        assert gate.vanished and not gate.stale and not gate.revealable

    def test_an_entry_written_without_read_permission_is_not_revealable(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"x\n")
        stamp = take_snapshot(path).stamp
        remember("inspect-hidden", path, stamp, revealable=False)  # type: ignore[arg-type]

        assert not inspect("inspect-hidden", path, take_snapshot(path)).revealable
        # 之后经 read_file 读到同一版才算看过
        remember("inspect-hidden", path, stamp, revealable=True)  # type: ignore[arg-type]
        assert recall("inspect-hidden", path) == Entry(stamp, True)  # type: ignore[arg-type]
        assert inspect("inspect-hidden", path, take_snapshot(path)).revealable


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

        with edit_lock(path), pytest.raises(ConcurrentEdit):
            commit_locked(path, b"new\n", expected=expected)

        assert path.read_bytes() == b"someone else\n"

    def test_an_unchanged_file_is_replaced_and_the_new_stamp_returned(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"old\n")
        expected = take_snapshot(path)

        with edit_lock(path):
            stamp = commit_locked(path, b"new\n", expected=expected)

        assert path.read_bytes() == b"new\n"
        assert stamp.sha256 == stamp_of(b"new\n", 0).sha256 and stamp.size == 4

    def test_a_missing_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.txt"
        expected = take_snapshot(path)

        with edit_lock(path):
            commit_locked(path, b"hello\n", expected=expected)

        assert path.read_bytes() == b"hello\n"

    def test_a_file_that_appeared_meanwhile_is_not_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.txt"
        expected = take_snapshot(path)
        path.write_bytes(b"created elsewhere\n")

        with edit_lock(path), pytest.raises(ConcurrentEdit):
            commit_locked(path, b"hello\n", expected=expected)

        assert path.read_bytes() == b"created elsewhere\n"


class TestRevealAllowed:
    def test_a_version_seen_through_read_file_is_revealed(self) -> None:
        assert file_state.reveal_allowed(Gate(Entry(stamp_of(b"x", 1), True))) is True

    def test_an_unread_file_is_not_revealed(self) -> None:
        assert file_state.reveal_allowed(Gate(None)) is False

    def test_an_entry_written_without_a_read_is_not_revealed(self) -> None:
        assert file_state.reveal_allowed(Gate(Entry(stamp_of(b"x", 1), False))) is False

    def test_a_stale_or_vanished_version_is_not_revealed(self) -> None:
        entry = Entry(stamp_of(b"x", 1), True)

        assert file_state.reveal_allowed(Gate(entry, stale=True)) is False
        assert file_state.reveal_allowed(Gate(entry, vanished=True)) is False
