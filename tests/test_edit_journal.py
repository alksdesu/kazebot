"""编辑日志：同一个任务里第一次改某个文件之前记下原文件，后面再改不覆盖；记不下来不拦改动，只说明不可撤销。

日志按会话、任务分目录存在 data/edit_journal/ 下，过了保留期由 data_cleanup 清掉，会话被拆掉时一起删。
"""
from __future__ import annotations

import gzip
import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import engine.data_cleanup as cleanup  # noqa: E402
from toolbox import edit_journal, file_state  # noqa: E402
from toolbox.edit_journal import (  # noqa: E402
    EditJournal,
    JournalCorrupt,
    JournalNote,
    JournalScope,
    PendingEdit,
    record_pre_image,
    retention_days_of,
)

_DAY = 24 * 3600


def scope(workspace: Path, *, session: str = "sess-j", task: str = "task-1") -> JournalScope:
    return JournalScope(workspace, session, task)


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def begin(target: JournalScope, path: Path, *, tool: str = "apply_diff", revealable: bool = True) -> PendingEdit:
    with file_state.edit_lock(path):
        snapshot = file_state.take_snapshot(path)
        return record_pre_image(target, path, snapshot, tool=tool, display=path.name, revealable=revealable)


def change(target: JournalScope, path: Path, data: bytes, *, tool: str = "apply_diff") -> JournalNote:
    """照写文件工具的顺序：锁里记原文件、写盘、记改后的版本。"""
    with file_state.edit_lock(path):
        snapshot = file_state.take_snapshot(path)
        pending = record_pre_image(target, path, snapshot, tool=tool, display=path.name, revealable=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = file_state.commit_locked(path, data, expected=snapshot)
        return pending.record_post_image(stamp)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def only_task(workspace: Path, session: str = "sess-j"):
    [task] = EditJournal(workspace).tasks(session)
    return task


def age(directory: Path, days: float) -> None:
    stamp = time.time() - days * _DAY
    for path in [*directory.rglob("*"), directory]:
        os.utime(path, (stamp, stamp))


class TestRecording:
    def test_the_first_change_saves_the_original(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "conf.yaml", b"v: 1\n")

        note = change(scope(tmp_path), path, b"v: 2\n")

        assert note.undoable is True and note.lines() == []
        task = only_task(tmp_path)
        [entry] = task.entries
        assert task.task_id == "task-1"
        assert entry.tool == "apply_diff" and entry.display == "conf.yaml" and entry.path == str(path)
        assert entry.pre.exists and entry.pre.sha256 == sha(b"v: 1\n") and entry.pre.size == 5
        assert entry.pre.revealable is True
        assert entry.post is not None and entry.post.sha256 == sha(b"v: 2\n")
        assert entry.writes == 1 and entry.undone is None
        assert EditJournal(tmp_path).load_pre_image("sess-j", "task-1", entry) == b"v: 1\n"

    def test_later_changes_in_the_same_task_keep_the_first_original(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "conf.yaml", b"v: 1\n")
        change(scope(tmp_path), path, b"v: 2\n")

        change(scope(tmp_path), path, b"v: 3\n")

        [entry] = only_task(tmp_path).entries
        assert entry.pre.sha256 == sha(b"v: 1\n")
        assert entry.post is not None and entry.post.sha256 == sha(b"v: 3\n")
        assert entry.writes == 2

    def test_another_task_keeps_its_own_original(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "conf.yaml", b"v: 1\n")
        change(scope(tmp_path, task="task-1"), path, b"v: 2\n")

        change(scope(tmp_path, task="task-2"), path, b"v: 3\n")

        tasks = {task.task_id: task for task in EditJournal(tmp_path).tasks("sess-j")}
        assert set(tasks) == {"task-1", "task-2"}
        assert tasks["task-2"].entries[0].pre.sha256 == sha(b"v: 2\n")

    def test_another_session_does_not_see_the_records(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "conf.yaml", b"v: 1\n")
        change(scope(tmp_path, session="sess-a"), path, b"v: 2\n")

        assert EditJournal(tmp_path).tasks("sess-b") == []

    def test_a_new_file_is_recorded_as_missing_with_the_directories_it_needs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "new.txt"

        change(scope(tmp_path), path, b"hello\n", tool="write_file")

        [entry] = only_task(tmp_path).entries
        assert entry.created and entry.pre.exists is False
        assert entry.pre.created_dirs == (str(tmp_path / "a" / "b"), str(tmp_path / "a"))
        assert entry.post is not None and entry.post.sha256 == sha(b"hello\n")

    def test_the_original_is_stored_compressed(self, tmp_path: Path) -> None:
        original = b"secret = 1\n" * 20
        path = seed(tmp_path / "conf.py", original)

        change(scope(tmp_path), path, b"secret = 2\n")

        [entry] = only_task(tmp_path).entries
        blob = EditJournal(tmp_path).task_dir("sess-j", "task-1") / entry.pre.blob
        stored = blob.read_bytes()
        assert stored[:2] == b"\x1f\x8b"
        assert b"secret" not in stored
        assert gzip.decompress(stored) == original

    def test_the_task_of_a_context_falls_back_to_its_run_id(self, tmp_path: Path) -> None:
        ctx = SimpleNamespace(workspace_root=tmp_path, session_id="sess-x", task_id="", run_id="run-9")

        assert JournalScope.of(ctx).task_id == "run-9"

    def test_the_session_is_the_file_state_session_key(self, tmp_path: Path) -> None:
        ctx = SimpleNamespace(
            workspace_root=tmp_path, session_id="branch_3", _context_archive_session_id="main", task_id="t", node_id="n",
        )

        target = JournalScope.of(ctx)

        assert (target.session, target.task_id, target.node_id) == ("main", "t", "n")

    def test_without_a_task_nothing_is_recorded(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"x\n")
        ctx = SimpleNamespace(workspace_root=tmp_path, session_id="sess-x", task_id="", run_id="")

        pending = begin(JournalScope.of(ctx), path)

        assert pending.note.undoable is False
        assert "任务号" in pending.note.message
        assert not (tmp_path / "data" / "edit_journal").exists()

    def test_hostile_session_and_task_ids_stay_inside_the_journal(self, tmp_path: Path) -> None:
        journal = EditJournal(tmp_path)

        directory = journal.task_dir("../../outside", "..\\..\\x")

        assert directory.resolve().is_relative_to(journal.root.resolve())


class TestLimits:
    def test_a_file_over_the_size_limit_is_not_saved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(edit_journal, "MAX_FILE_BYTES", 8)
        path = seed(tmp_path / "big.txt", b"0123456789abcdef\n")

        note = change(scope(tmp_path), path, b"small\n")

        assert note.undoable is False
        assert "太大" in note.message and "不可撤销" in note.message
        [entry] = only_task(tmp_path).entries
        assert entry.pre.skipped == edit_journal.SKIP_TOO_LARGE and entry.undoable is False
        task_dir = EditJournal(tmp_path).task_dir("sess-j", "task-1")
        assert sorted(item.name for item in task_dir.iterdir()) == ["manifest.json"]

    def test_a_file_too_large_at_first_stays_unrecorded_for_the_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 第一次没记下原文件，后面再记的只会是改到一半的版本
        monkeypatch.setattr(edit_journal, "MAX_FILE_BYTES", 8)
        path = seed(tmp_path / "big.txt", b"0123456789abcdef\n")
        change(scope(tmp_path), path, b"small\n")

        note = change(scope(tmp_path), path, b"tiny\n")

        assert note.undoable is False
        [entry] = only_task(tmp_path).entries
        assert entry.writes == 2

    def test_the_task_journal_limit_stops_saving_more(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        first = seed(tmp_path / "a.txt", b"alpha alpha alpha\n")
        second = seed(tmp_path / "b.txt", b"beta beta beta beta\n")
        packed = gzip.compress(first.read_bytes(), compresslevel=1, mtime=0)
        monkeypatch.setattr(edit_journal, "MAX_TASK_BYTES", len(packed))

        assert change(scope(tmp_path), first, b"A\n").undoable is True
        note = change(scope(tmp_path), second, b"B\n")

        assert note.undoable is False and "已满" in note.message
        entries = {entry.display: entry for entry in only_task(tmp_path).entries}
        assert entries["a.txt"].undoable is True
        assert entries["b.txt"].pre.skipped == edit_journal.SKIP_TASK_BYTES

    def test_the_number_of_files_per_task_is_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(edit_journal, "MAX_TASK_FILES", 1)
        first = seed(tmp_path / "a.txt", b"a\n")
        second = seed(tmp_path / "b.txt", b"b\n")
        change(scope(tmp_path), first, b"A\n")

        note = change(scope(tmp_path), second, b"B\n")

        assert note.undoable is False and "已达 1 个" in note.message
        assert [entry.display for entry in only_task(tmp_path).entries] == ["a.txt"]


class TestFailures:
    def test_a_failed_write_drops_the_entry_it_created(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")

        pending = begin(scope(tmp_path), path)
        pending.discard()

        assert EditJournal(tmp_path).tasks("sess-j") == []
        assert not EditJournal(tmp_path).task_dir("sess-j", "task-1").exists()

    def test_a_failed_write_keeps_an_entry_from_an_earlier_write(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")

        begin(scope(tmp_path), path).discard()

        [entry] = only_task(tmp_path).entries
        assert entry.pre.sha256 == sha(b"one\n") and entry.writes == 1

    def test_an_unwritable_journal_does_not_block_the_edit_and_says_so(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        blocker = seed(tmp_path / "data" / "edit_journal", b"not a directory")

        note = change(scope(tmp_path), path, b"two\n")

        assert path.read_bytes() == b"two\n"
        assert note.undoable is False and "编辑日志没能写入" in note.message

        # 日志恢复可写之后，这个任务也不能再把改到一半的版本当原文件记下来
        blocker.unlink()
        assert change(scope(tmp_path), path, b"three\n").undoable is False
        other = seed(tmp_path / "g.txt", b"g\n")
        assert change(scope(tmp_path), other, b"G\n").undoable is True

    def test_a_post_image_that_cannot_be_recorded_asks_for_force_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        pending = begin(scope(tmp_path), path)

        def broken_finish(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(EditJournal, "finish", broken_finish)
        note = pending.record_post_image(file_state.stamp_of(b"two\n", 0))

        assert note.undoable is False
        assert "force=true" in note.message and "disk full" in note.message

    def test_a_corrupt_manifest_is_left_out_of_listings(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        manifest = EditJournal(tmp_path).task_dir("sess-j", "task-1") / "manifest.json"
        manifest.write_text("{ not json", encoding="utf-8")

        assert EditJournal(tmp_path).tasks("sess-j") == []

    def test_a_corrupt_manifest_is_started_over_on_the_next_change(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        manifest = EditJournal(tmp_path).task_dir("sess-j", "task-1") / "manifest.json"
        manifest.write_text("[]", encoding="utf-8")

        assert change(scope(tmp_path), path, b"three\n").undoable is True
        [entry] = only_task(tmp_path).entries
        assert entry.pre.sha256 == sha(b"two\n")

    def test_a_tampered_original_is_refused(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        [entry] = only_task(tmp_path).entries
        blob = EditJournal(tmp_path).task_dir("sess-j", "task-1") / entry.pre.blob
        blob.write_bytes(gzip.compress(b"evi\n"))

        with pytest.raises(JournalCorrupt, match="哈希"):
            EditJournal(tmp_path).load_pre_image("sess-j", "task-1", entry)

    def test_an_original_bigger_than_recorded_is_not_inflated(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        [entry] = only_task(tmp_path).entries
        blob = EditJournal(tmp_path).task_dir("sess-j", "task-1") / entry.pre.blob
        blob.write_bytes(gzip.compress(b"\0" * 1_000_000))

        with pytest.raises(JournalCorrupt, match="大小"):
            EditJournal(tmp_path).load_pre_image("sess-j", "task-1", entry)

    def test_a_missing_original_is_reported(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        [entry] = only_task(tmp_path).entries
        (EditJournal(tmp_path).task_dir("sess-j", "task-1") / entry.pre.blob).unlink()

        with pytest.raises(JournalCorrupt, match="不见了"):
            EditJournal(tmp_path).load_pre_image("sess-j", "task-1", entry)


class TestRetention:
    def test_the_retention_comes_from_the_runtime_config(self, tmp_path: Path) -> None:
        assert EditJournal(tmp_path).retention_days == edit_journal.DEFAULT_RETENTION_DAYS
        runtime = config_layout.runtime_path(tmp_path)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(yaml.safe_dump({"edit_journal": {"retention_days": 5}}), encoding="utf-8")

        assert EditJournal(tmp_path).retention_days == 5

    def test_the_shipped_default_is_three_days(self) -> None:
        shipped = yaml.safe_load((_ROOT / "defaults" / "runtime.yaml").read_text(encoding="utf-8"))

        assert shipped["edit_journal"] == {"retention_days": 3}

    @pytest.mark.parametrize("raw,expected", [(0, 1), (-4, 1), ("x", 3), (None, 3), (99999, 3650)])
    def test_bad_values_are_clamped_or_defaulted(self, raw: object, expected: int) -> None:
        assert retention_days_of({"edit_journal": {"retention_days": raw}}) == expected

    def test_expired_task_journals_are_purged(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path, task="old"), path, b"two\n")
        change(scope(tmp_path, task="new"), path, b"three\n")
        journal = EditJournal(tmp_path, retention_days=3)
        age(journal.task_dir("sess-j", "old"), days=4)

        assert journal.purge() == 1

        assert not journal.task_dir("sess-j", "old").exists()
        assert [task.task_id for task in journal.tasks("sess-j")] == ["new"]

    def test_a_dry_run_only_counts(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        journal = EditJournal(tmp_path, retention_days=3)
        age(journal.task_dir("sess-j", "task-1"), days=4)

        assert journal.purge(dry_run=True) == 1
        assert journal.task_dir("sess-j", "task-1").exists()

    def test_an_emptied_session_directory_goes_but_the_locks_stay(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        journal = EditJournal(tmp_path, retention_days=3)
        age(journal.task_dir("sess-j", "task-1"), days=4)

        journal.purge()

        assert not journal.session_dir("sess-j").exists()
        assert (journal.root / ".locks").is_dir()

    def test_purge_session_removes_only_that_session(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path, session="s1", task="t1"), path, b"two\n")
        change(scope(tmp_path, session="s1", task="t2"), path, b"three\n")
        change(scope(tmp_path, session="s2", task="t3"), path, b"four\n")
        journal = EditJournal(tmp_path)

        assert journal.purge_session("s1") == 2
        assert journal.purge_session("s1") == 0
        assert journal.tasks("s1") == []
        assert [task.task_id for task in journal.tasks("s2")] == ["t3"]

    def test_the_cleanup_job_purges_expired_journals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", False)
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path, task="old"), path, b"two\n")
        change(scope(tmp_path, task="new"), path, b"three\n")
        age(EditJournal(tmp_path).task_dir("sess-j", "old"), days=4)

        cleanup.purge_expired_edit_journals()

        assert not EditJournal(tmp_path).task_dir("sess-j", "old").exists()
        assert EditJournal(tmp_path).task_dir("sess-j", "new").exists()

    def test_the_cleanup_job_respects_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path), path, b"two\n")
        age(EditJournal(tmp_path).task_dir("sess-j", "task-1"), days=4)

        cleanup.purge_expired_edit_journals()

        assert EditJournal(tmp_path).task_dir("sess-j", "task-1").exists()

    def test_the_cleanup_job_is_scheduled(self) -> None:
        source = (_ROOT / "engine" / "data_cleanup.py").read_text(encoding="utf-8")

        assert "(purge_expired_edit_journals, {})" in source

    def test_tearing_a_session_down_removes_its_journal(self, tmp_path: Path) -> None:
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState

        path = seed(tmp_path / "f.txt", b"one\n")
        change(scope(tmp_path, session="child_a"), path, b"two\n")
        change(scope(tmp_path, session="child_b"), path, b"three\n")
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-edit-journal"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )

        state.purge_session_files("child_a")

        assert EditJournal(tmp_path).tasks("child_a") == []
        assert len(EditJournal(tmp_path).tasks("child_b")) == 1
