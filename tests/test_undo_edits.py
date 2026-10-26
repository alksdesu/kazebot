"""undo_edits：按任务把 write_file / apply_diff 改过的文件放回改之前的样子，新建的文件删掉。

文件之后又被改过的跳过并说明原因，force 才盖掉；每个文件写回之前都走 write_file 的审批；一处改动只能撤一次。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox import edit_journal  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.read_file import read_file  # noqa: E402
from toolbox.builtins.undo_edits import undo_edits  # noqa: E402
from toolbox.builtins.write_file import write_file  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_BOM = b"\xef\xbb\xbf"


class _Ctx(ToolContext):
    """离线 ToolContext：同一个会话里换 task 就是另一个任务；按 op 名给出预设的 safety_level，没写的一律 auto。"""

    def __init__(self, workspace_root: Path, *, task: str = "task-1", session: str = "sess-undo", **decisions: str) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id=session,
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id=task,
        )
        self.decisions = dict(decisions)
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.cancelled = False

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return self.cancelled

    def undo_requests(self) -> list[dict[str, Any]]:
        return [parameters for _op, parameters in self.requested if parameters.get("tool_name") == "undo_edits"]


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # 任务按最后写盘的时间排先后；Windows 上 time.time() 的粒度粗，连着两次写可能拿到同一个时间
    ticks = itertools.count(1_800_000_000)
    monkeypatch.setattr(edit_journal, "time", SimpleNamespace(time=lambda: float(next(ticks))))


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read(ctx: _Ctx, path: str) -> dict[str, Any]:
    return asyncio.run(read_file({"files": [{"path": path}]}, ctx))


def write(ctx: _Ctx, path: str, content: str) -> dict[str, Any]:
    return asyncio.run(write_file({"path": path, "content": content}, ctx))


def diff(ctx: _Ctx, path: str, search: str, replace: str) -> dict[str, Any]:
    result = asyncio.run(apply_diff({"path": path, "diffs": [{"search": search, "replace": replace}]}, ctx))
    assert result["ok"] is True, result
    return result


def undo(ctx: _Ctx, **args: Any) -> dict[str, Any]:
    return asyncio.run(undo_edits(args, ctx))


class TestUndo:
    def test_the_original_bytes_come_back(self, tmp_path: Path) -> None:
        original = _BOM + b"a: 1\r\nb: 2\r\n"
        path = seed(tmp_path / "conf.yaml", original)
        ctx = _Ctx(tmp_path)
        read(ctx, "conf.yaml")
        diff(ctx, "conf.yaml", "b: 2", "b: 20")
        assert path.read_bytes() != original

        result = undo(ctx)

        assert result["ok"] is True
        assert path.read_bytes() == original
        data = result["data"]
        assert data["taskId"] == "task-1" and data["restoredCount"] == 1
        assert data["changedPaths"] == ["conf.yaml"]
        assert "已恢复 conf.yaml" in data["result"]
        assert "当前任务" in data["result"]

    def test_each_file_goes_through_the_write_approval(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "notes.txt", b"old\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "notes.txt", "old", "new")
        ctx.decisions["write_file"] = "deny"

        result = undo(ctx)

        assert result["ok"] is False
        assert path.read_bytes() == b"new\n"
        assert "审批没有通过" in result["data"]["result"]
        [parameters] = ctx.undo_requests()
        assert parameters["path"] == "notes.txt"
        assert parameters["content_preview"].startswith("undo_edits: ")
        assert "notes.txt" in parameters["content_preview"] and "task-1" in parameters["content_preview"]
        assert parameters["content_len"] == 4

        # 被拒的那一项没有标成已撤销，批准之后照样能撤
        ctx.decisions["write_file"] = "auto"
        assert undo(ctx)["ok"] is True
        assert path.read_bytes() == b"old\n"

    def test_a_file_the_task_created_is_deleted_with_the_directories_made_for_it(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)
        assert write(ctx, "new/dir/f.txt", "x\n")["ok"] is True

        result = undo(ctx)

        assert result["ok"] is True and result["data"]["deletedCount"] == 1
        assert not (tmp_path / "new").exists()
        assert "已删除 new/dir/f.txt" in result["data"]["result"]
        [parameters] = ctx.undo_requests()
        assert "删除" in parameters["content_preview"]

    def test_a_directory_that_got_other_files_is_kept(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)
        write(ctx, "new/f.txt", "x\n")
        seed(tmp_path / "new" / "other.txt", b"keep\n")

        assert undo(ctx)["ok"] is True

        assert not (tmp_path / "new" / "f.txt").exists()
        assert (tmp_path / "new" / "other.txt").read_bytes() == b"keep\n"

    def test_a_file_changed_after_the_task_is_skipped(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "one", "two")
        path.write_bytes(b"someone else\n")

        result = undo(ctx)

        assert result["ok"] is False
        assert path.read_bytes() == b"someone else\n"
        text = result["data"]["result"]
        assert "又被改过" in text and "force=true" in text
        assert result["data"]["skippedCount"] == 1
        # 注定要跳过的文件不弹审批卡
        assert ctx.undo_requests() == []

    def test_force_restores_anyway(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "one", "two")
        path.write_bytes(b"someone else\n")

        result = undo(ctx, force=True)

        assert result["ok"] is True
        assert path.read_bytes() == b"one\n"
        assert "加了 force" in result["data"]["result"]
        assert result["data"]["files"][0]["forced"] is True
        [parameters] = ctx.undo_requests()
        assert "盖掉" in parameters["content_preview"]

    def test_a_file_already_back_to_the_original_needs_no_approval(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "one", "two")
        path.write_bytes(b"one\n")

        result = undo(ctx)

        assert result["ok"] is True
        assert "已经是改之前的内容" in result["data"]["result"]
        assert ctx.undo_requests() == []
        assert undo(ctx, task_id="task-1")["ok"] is False

    def test_a_created_file_that_is_already_gone_needs_nothing(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)
        write(ctx, "gone.txt", "x\n")
        (tmp_path / "gone.txt").unlink()

        result = undo(ctx)

        assert result["ok"] is True
        assert "已经不在了" in result["data"]["result"]
        assert ctx.undo_requests() == []

    def test_paths_limits_the_undo_to_those_files(self, tmp_path: Path) -> None:
        a = seed(tmp_path / "a.txt", b"a\n")
        b = seed(tmp_path / "b.txt", b"b\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "a.txt", "a", "A")
        diff(ctx, "b.txt", "b", "B")

        first = undo(ctx, paths=[str(tmp_path / "a.txt")])

        assert first["ok"] is True
        assert a.read_bytes() == b"a\n" and b.read_bytes() == b"B\n"
        # 同一个任务还有 b.txt 没撤，不带 task_id 时还是它
        second = undo(ctx)
        assert second["ok"] is True and second["data"]["taskId"] == "task-1"
        assert b.read_bytes() == b"b\n"

    def test_a_path_the_task_never_touched_is_reported(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"a\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "a.txt", "a", "A")

        result = undo(ctx, paths=["zzz.txt"])

        assert result["ok"] is False
        assert "没有改过 zzz.txt" in result["error"]

    def test_a_change_can_be_undone_only_once(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "one", "two")
        assert undo(ctx)["ok"] is True

        again = undo(ctx, task_id="task-1")
        latest = undo(ctx)

        assert again["ok"] is False and "都已经撤销过了" in again["error"]
        assert latest["ok"] is False and "已经撤销过" in latest["error"]
        assert path.read_bytes() == b"one\n"

    def test_the_most_recent_task_with_changes_left_goes_first(self, tmp_path: Path) -> None:
        a = seed(tmp_path / "a.txt", b"a1\n")
        b = seed(tmp_path / "b.txt", b"b1\n")
        later = _Ctx(tmp_path, task="task-3")
        diff(_Ctx(tmp_path, task="task-1"), "a.txt", "a1", "a2")
        diff(_Ctx(tmp_path, task="task-2"), "b.txt", "b1", "b2")

        first = undo(later)

        assert first["data"]["taskId"] == "task-2"
        assert b.read_bytes() == b"b1\n" and a.read_bytes() == b"a2\n"
        assert "当前任务" not in first["data"]["result"]
        second = undo(later)
        assert second["data"]["taskId"] == "task-1"
        assert a.read_bytes() == b"a1\n"

    def test_a_unique_prefix_of_the_task_id_is_enough(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"a\n")
        seed(tmp_path / "b.txt", b"b\n")
        diff(_Ctx(tmp_path, task="3f2a9c11-aaaa"), "a.txt", "a", "A")
        diff(_Ctx(tmp_path, task="3f2a9c11-bbbb"), "b.txt", "b", "B")
        ctx = _Ctx(tmp_path, task="77777777-cccc")

        ambiguous = undo(ctx, task_id="3f2a9c11")
        too_short = undo(ctx, task_id="3f2a")
        unique = undo(ctx, task_id="3f2a9c11-a")

        assert ambiguous["ok"] is False
        assert "3f2a9c11-aaaa" in ambiguous["error"] and "3f2a9c11-bbbb" in ambiguous["error"]
        assert too_short["ok"] is False and "没有任务 3f2a" in too_short["error"]
        assert unique["ok"] is True and unique["data"]["taskId"] == "3f2a9c11-aaaa"
        assert (tmp_path / "a.txt").read_bytes() == b"a\n"

    def test_the_task_of_another_session_is_not_found(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"a\n")
        diff(_Ctx(tmp_path, session="sess-other"), "a.txt", "a", "A")

        result = undo(_Ctx(tmp_path), task_id="task-1")

        assert result["ok"] is False
        assert (tmp_path / "a.txt").read_bytes() == b"A\n"

    def test_a_cancelled_task_stops_the_undo(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"old\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "old", "new")
        ctx.cancelled = True

        result = undo(ctx)

        assert result["ok"] is False and result["cancelled"] is True and result["data"]["cancelled"] is True
        assert path.read_bytes() == b"new\n"

    def test_the_restored_version_needs_no_new_read(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"line 1\nline 2\nline 3\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")
        diff(ctx, "f.txt", "line 2", "LINE 2")
        undo(ctx)

        again = diff(ctx, "f.txt", "line 3", "LINE 3")

        assert again["data"]["echoed"] is True
        assert (tmp_path / "f.txt").read_bytes() == b"line 1\nline 2\nLINE 3\n"

    def test_a_file_never_read_does_not_become_revealable_by_an_undo(self, tmp_path: Path) -> None:
        # 读策略要审批：回显只能靠本会话读过这一版，看的是撤销有没有把它错记成读过
        seed(tmp_path / "f.txt", b"a\nb\n")
        ctx = _Ctx(tmp_path, read_file="approval_required")
        diff(ctx, "f.txt", "a", "A")
        undo(ctx)

        again = diff(ctx, "f.txt", "b", "B")

        assert again["data"]["echoed"] is False

    def test_a_deleted_new_file_can_be_written_again(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)
        write(ctx, "fresh.txt", "1\n")
        undo(ctx)
        assert not (tmp_path / "fresh.txt").exists()

        assert write(ctx, "fresh.txt", "2\n")["ok"] is True
        assert undo(ctx)["ok"] is True
        assert not (tmp_path / "fresh.txt").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_permissions_survive_a_restore(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "run.sh", b"echo one\n")
        os.chmod(path, 0o750)
        ctx = _Ctx(tmp_path)
        diff(ctx, "run.sh", "one", "two")

        undo(ctx)

        assert path.stat().st_mode & 0o777 == 0o750

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_deleted_file_comes_back_with_its_permissions(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "secret.txt", b"one\n")
        os.chmod(path, 0o600)
        ctx = _Ctx(tmp_path)
        diff(ctx, "secret.txt", "one", "two")
        path.unlink()

        assert undo(ctx)["ok"] is False
        result = undo(ctx, force=True)

        assert result["ok"] is True
        assert path.read_bytes() == b"one\n"
        assert path.stat().st_mode & 0o777 == 0o600


class TestUnrecorded:
    def test_a_change_too_large_to_save_is_reported_and_cannot_be_undone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(edit_journal, "MAX_FILE_BYTES", 4)
        path = seed(tmp_path / "big.txt", b"0123456789\n")
        ctx = _Ctx(tmp_path)

        edited = diff(ctx, "big.txt", "0123", "abcd")

        assert edited["data"]["undoable"] is False
        assert "不可撤销" in edited["data"]["result"]
        result = undo(ctx)
        assert result["ok"] is False and "不可撤销" in result["error"]
        assert path.read_bytes() == b"abcd456789\n"

    def test_a_newer_task_with_nothing_undoable_is_named_when_passed_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(edit_journal, "MAX_FILE_BYTES", 6)
        small = seed(tmp_path / "small.txt", b"abc\n")
        seed(tmp_path / "big.txt", b"0123456789\n")
        diff(_Ctx(tmp_path, task="task-1"), "small.txt", "abc", "xyz")
        newer = _Ctx(tmp_path, task="task-2")
        diff(newer, "big.txt", "0123", "abcd")

        result = undo(newer)

        assert result["ok"] is True and result["data"]["taskId"] == "task-1"
        assert result["data"]["passedTasks"] == ["task-2"]
        assert "task-2" in result["data"]["result"] and "不可撤销" in result["data"]["result"]
        assert small.read_bytes() == b"abc\n"

    def test_edits_are_marked_undoable_in_their_results(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)

        written = write(ctx, "g.txt", "x\n")
        edited = diff(ctx, "f.txt", "one", "two")

        assert written["data"]["undoable"] is True and "undoNote" not in written["data"]
        assert edited["data"]["undoable"] is True and "不可撤销" not in edited["data"]["result"]


class TestTheJournalCannotBeForged:
    """编辑日志只由编辑工具自己记：模型能改它，就能让 undo_edits 把任意内容「恢复」进别的文件。"""

    def test_write_file_refuses_the_journal_directory_before_asking(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)

        result = write(ctx, "data/edit_journal/fake/manifest.json", "{}")

        assert result["ok"] is False and "data/edit_journal/" in result["error"]
        assert ctx.requested == []
        assert not (tmp_path / "data" / "edit_journal" / "fake").exists()

    def test_apply_diff_refuses_a_journal_file(self, tmp_path: Path) -> None:
        seed(tmp_path / "data" / "edit_journal" / "s" / "t" / "manifest.json", b'{"entries": []}\n')
        ctx = _Ctx(tmp_path)

        result = asyncio.run(apply_diff(
            {"path": "data/edit_journal/s/t/manifest.json", "diffs": [{"search": "[]", "replace": "[1]"}]}, ctx,
        ))

        assert result["ok"] is False and "data/edit_journal/" in result["error"]
        assert ctx.requested == []
        assert (tmp_path / "data" / "edit_journal" / "s" / "t" / "manifest.json").read_bytes() == b'{"entries": []}\n'

    def test_a_recorded_path_inside_the_journal_is_never_restored(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"one\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "f.txt", "one", "two")
        journal = edit_journal.EditJournal(tmp_path)
        task_dir = journal.task_dir(edit_journal.JournalScope.of(ctx).session, "task-1")
        manifest_path = task_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = journal.root / "planted.txt"
        manifest["entries"][0]["path"] = str(target)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = undo(ctx)

        assert result["ok"] is False
        assert "编辑日志目录" in result["data"]["result"]
        assert not target.exists()
        assert ctx.undo_requests() == []


class TestList:
    def test_list_shows_recent_tasks_their_files_and_what_was_undone(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"a\n")
        seed(tmp_path / "b.txt", b"b\n")
        second = _Ctx(tmp_path, task="task-2")
        diff(_Ctx(tmp_path, task="task-1"), "a.txt", "a", "A")
        write(second, "new.txt", "x\n")
        diff(second, "b.txt", "b", "B")
        diff(second, "b.txt", "B", "BB")
        undo(second, task_id="task-1")

        result = undo(second, action="list")

        assert result["ok"] is True
        data = result["data"]
        assert data["totalTasks"] == 2 and data["retentionDays"] == 3
        assert [row["taskId"] for row in data["tasks"]] == ["task-2", "task-1"]
        newest, oldest = data["tasks"]
        assert newest["current"] is True and newest["status"] == "未撤销"
        assert oldest["current"] is False and oldest["status"] == "已撤销"
        files = {row["path"]: row for row in newest["files"]}
        assert files["new.txt"]["created"] is True and files["b.txt"]["writes"] == 2
        text = data["result"]
        assert "任务 task-2" in text and "当前任务" in text and "任务 task-1" in text
        assert "new.txt（write_file 新建）未撤销" in text
        assert "b.txt（apply_diff，写了 2 次）未撤销" in text
        assert "a.txt（apply_diff）已撤销" in text
        assert ctx_free_of_approvals(second)

    def test_list_without_records(self, tmp_path: Path) -> None:
        result = undo(_Ctx(tmp_path), action="list")

        assert result["ok"] is True
        assert result["data"]["tasks"] == [] and "没有编辑记录" in result["data"]["result"]

    def test_undo_without_records_is_an_error(self, tmp_path: Path) -> None:
        result = undo(_Ctx(tmp_path))

        assert result["ok"] is False and "没有编辑记录" in result["error"]

    @pytest.mark.parametrize("args", [{"action": "redo"}, {"paths": "  "}, {"paths": [1, 2]}])
    def test_bad_arguments_are_refused(self, tmp_path: Path, args: dict[str, Any]) -> None:
        seed(tmp_path / "a.txt", b"a\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "a.txt", "a", "A")

        result = undo(ctx, **args)

        assert result["ok"] is False
        assert (tmp_path / "a.txt").read_bytes() == b"A\n"


def ctx_free_of_approvals(ctx: _Ctx) -> bool:
    """list 只读编辑日志，不该送审。"""
    before = len(ctx.undo_requests())
    undo(ctx, action="list")
    return len(ctx.undo_requests()) == before
