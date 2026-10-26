"""改文件的工具（write_file / apply_diff / edit_lines）怎样挡住彼此：认领、按任务分层的读戳、跨进程的写盘锁。

别的还没结束的任务正在改的文件改不了，结果里写明是哪个任务、哪个节点，也不为它弹审批卡；同一任务再改不挡，任务结束才放。
读戳按任务记：并行的任务互不刷新，任务结束并进会话层，下一个任务换了 worker 也看得到；本任务开始之后别的任务并进来的版本算过期。
supervisor 连不上时一律不写，读照常。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _file_edits_fake import FakeFileEdits, FakeSupervisor, SupervisorDown  # noqa: E402
from _lock_holder import WAIT_SEC, LockHolder  # noqa: E402
from toolbox import file_state  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.edit_lines import edit_lines  # noqa: E402
from toolbox.builtins.read_file import read_file  # noqa: E402
from toolbox.builtins.write_file import write_file  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.edit_journal import EditJournal  # noqa: E402
from toolbox.hashline import line_hash  # noqa: E402
from toolbox.text_files import decode_text, normalize_newlines, split_lines  # noqa: E402

_SESSION = "sess-guard"
_BLOCKED_FOR = 0.5


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：一个任务一个；节点开了 hashline，edit_lines 也能用。按 op 名给出预设的 safety_level。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        task: str = "task-1",
        session: str = _SESSION,
        worker: str = "worker-1",
        **decisions: str,
    ) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id=session,
            run_id="run-1",
            worker_id=worker,
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id=task,
        )
        self._node_extra = {"edit_mode": "hashline"}
        self.decisions = dict(decisions)
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.evaluated: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.evaluated.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return False


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read(ctx: _Ctx, path: str) -> dict[str, Any]:
    return asyncio.run(read_file({"files": [{"path": path}]}, ctx))


def diff(ctx: _Ctx, path: str, search: str = "two", replace: str = "TWO") -> dict[str, Any]:
    return asyncio.run(apply_diff({"path": path, "diffs": [{"search": search, "replace": replace}]}, ctx))


def write(ctx: _Ctx, path: str, content: str = "whole\n") -> dict[str, Any]:
    return asyncio.run(write_file({"path": path, "content": content}, ctx))


def lines(ctx: _Ctx, path: str, line_no: int = 2, content: str = "TWO") -> dict[str, Any]:
    current = split_lines(normalize_newlines(decode_text((ctx.workspace_root / path).read_bytes())))
    anchor = f"{line_no}:{line_hash(current[line_no - 1])}"
    return asyncio.run(edit_lines({"path": path, "edits": [{"op": "replace", "start": anchor, "content": content}]}, ctx))


# 都改第一行：别的用例先用 diff 把第二行改成了 TWO，这几个调用照样有东西可改
_TOOLS: dict[str, Callable[[_Ctx, str], dict[str, Any]]] = {
    "apply_diff": lambda ctx, path: diff(ctx, path, "one", "ONE"),
    "write_file": lambda ctx, path: write(ctx, path),
    "edit_lines": lambda ctx, path: lines(ctx, path, 1, "ONE"),
}


def holders(tmp_path: Path, name: str) -> list[str]:
    return FakeSupervisor.of(tmp_path).holders((tmp_path / name).resolve())


class TestClaims:
    @pytest.mark.parametrize("tool", sorted(_TOOLS))
    def test_a_file_another_running_task_is_editing_is_refused_with_that_task_named(
        self, tmp_path: Path, tool: str,
    ) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        owner = _Ctx(tmp_path, task="task-1")
        owner.supervisor.start("task-1", session=_SESSION, node="bootstrap.coder")
        assert diff(owner, "a.txt")["ok"] is True
        other = _Ctx(tmp_path, task="task-2")

        result = _TOOLS[tool](other, "a.txt")

        assert result["ok"] is False
        assert "task-1" in result["error"] and "bootstrap.coder" in result["error"]
        assert result["data"]["claimed"] is True
        assert result["data"]["claimedBy"]["taskId"] == "task-1"
        assert result["data"]["claimedBy"]["nodeId"] == "bootstrap.coder"
        assert path.read_bytes() == b"one\nTWO\n"
        # 认领在送审之前：注定写不了的文件不弹审批卡
        assert other.requested == []
        assert [task.task_id for task in EditJournal(tmp_path).tasks(_SESSION)] == ["task-1"]
        assert holders(tmp_path, "a.txt") == ["task-1"]

    def test_the_same_task_editing_again_is_not_blocked(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\nthree\n")
        ctx = _Ctx(tmp_path)

        assert diff(ctx, "a.txt")["ok"] is True
        assert diff(ctx, "a.txt", "three", "THREE")["ok"] is True
        assert lines(ctx, "a.txt", 1, "ONE")["ok"] is True
        assert write(ctx, "a.txt", "all new\n")["ok"] is True

        assert path.read_bytes() == b"all new\n"
        assert holders(tmp_path, "a.txt") == ["task-1"]

    def test_the_claim_is_released_when_the_task_ends(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        owner = _Ctx(tmp_path, task="task-1")
        diff(owner, "a.txt")
        other = _Ctx(tmp_path, task="task-2")
        assert diff(other, "a.txt", "one", "ONE")["data"]["claimed"] is True

        owner.supervisor.end("task-1")
        # 认领放了，但那次改动是在本任务开始之后并进会话的：先重读
        unread = diff(other, "a.txt", "one", "ONE")
        read(other, "a.txt")
        after_read = diff(other, "a.txt", "one", "ONE")

        assert unread["ok"] is False and unread["data"]["changedByOtherTask"] is True
        assert after_read["ok"] is True
        assert path.read_bytes() == b"ONE\nTWO\n"
        assert holders(tmp_path, "a.txt") == ["task-2"]

    def test_claims_hold_across_sessions(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")
        diff(_Ctx(tmp_path, task="task-1", session="sess-a"), "a.txt")

        result = diff(_Ctx(tmp_path, task="task-2", session="sess-b"), "a.txt", "one", "ONE")

        assert result["ok"] is False and result["data"]["claimedBy"]["taskId"] == "task-1"

    def test_other_files_stay_free(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")
        seed(tmp_path / "b.txt", b"one\ntwo\n")
        diff(_Ctx(tmp_path, task="task-1"), "a.txt")

        assert diff(_Ctx(tmp_path, task="task-2"), "b.txt")["ok"] is True

    def test_a_suspended_task_keeps_its_claim_and_the_child_it_waits_for_may_edit(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\nthree\n")
        fake = FakeSupervisor.of(tmp_path)
        fake.start("task-parent", session=_SESSION, node="bootstrap.coder")
        child_session = "agent:bootstrap.code_fixer:sess-guard"
        fake.start("task-child", session=child_session, node="bootstrap.code_fixer", caller="task-parent")
        fake.start("task-stranger", session="sess-other", node="qq.orchestrator")
        parent = _Ctx(tmp_path, task="task-parent")
        child = _Ctx(tmp_path, task="task-child", session=child_session)
        stranger = _Ctx(tmp_path, task="task-stranger", session="sess-other")
        diff(parent, "a.txt")

        fake.suspend("task-parent")
        nested = diff(child, "a.txt", "three", "THREE")
        while_child = diff(stranger, "a.txt", "one", "ONE")
        fake.end("task-child")
        while_suspended = diff(stranger, "a.txt", "one", "ONE")

        assert nested["ok"] is True
        assert while_child["ok"] is False and while_child["data"]["claimedBy"]["taskId"] == "task-child"
        assert while_suspended["ok"] is False
        assert while_suspended["data"]["claimedBy"]["taskId"] == "task-parent"
        assert "挂起等子任务" in while_suspended["error"]
        assert path.read_bytes() == b"one\nTWO\nTHREE\n"

        fake.resume("task-parent")
        read(parent, "a.txt")
        assert diff(parent, "a.txt", "one", "ONE")["ok"] is True
        assert holders(tmp_path, "a.txt") == ["task-parent"]

    def test_a_sibling_of_the_child_is_still_refused(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\nthree\n")
        fake = FakeSupervisor.of(tmp_path)
        fake.start("task-parent", session=_SESSION)
        fake.start("task-first", session="agent:a:sess-guard", caller="task-parent")
        fake.start("task-second", session="agent:b:sess-guard", caller="task-parent")
        diff(_Ctx(tmp_path, task="task-parent"), "a.txt")
        fake.suspend("task-parent")
        diff(_Ctx(tmp_path, task="task-first", session="agent:a:sess-guard"), "a.txt", "three", "THREE")

        result = diff(_Ctx(tmp_path, task="task-second", session="agent:b:sess-guard"), "a.txt", "one", "ONE")

        assert result["ok"] is False and result["data"]["claimedBy"]["taskId"] == "task-first"

    @pytest.mark.parametrize("tool", ["apply_diff", "write_file"])
    def test_a_denied_approval_returns_a_claim_taken_for_it(self, tmp_path: Path, tool: str) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        denied = _Ctx(tmp_path, task="task-1", write_file="deny")

        result = _TOOLS[tool](denied, "a.txt")

        assert result["ok"] is False and path.read_bytes() == b"one\ntwo\n"
        assert holders(tmp_path, "a.txt") == []
        assert diff(_Ctx(tmp_path, task="task-2"), "a.txt")["ok"] is True

    def test_a_diff_that_does_not_apply_returns_its_claim(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")

        assert diff(_Ctx(tmp_path), "a.txt", "absent", "x")["ok"] is False

        assert holders(tmp_path, "a.txt") == []

    def test_an_edit_that_changes_nothing_returns_its_claim(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")

        diff(_Ctx(tmp_path), "a.txt", "two", "two")

        assert holders(tmp_path, "a.txt") == []

    def test_a_failed_edit_keeps_a_claim_from_an_earlier_write(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        diff(ctx, "a.txt")

        assert diff(ctx, "a.txt", "absent", "x")["ok"] is False

        assert holders(tmp_path, "a.txt") == ["task-1"]

    def test_a_missing_file_or_a_directory_returns_the_claim(self, tmp_path: Path) -> None:
        (tmp_path / "folder").mkdir()
        ctx = _Ctx(tmp_path)

        assert diff(ctx, "missing.txt")["ok"] is False
        assert write(ctx, "folder")["ok"] is False

        assert holders(tmp_path, "missing.txt") == [] and holders(tmp_path, "folder") == []

    @pytest.mark.parametrize("tool", ["apply_diff", "write_file"])
    def test_a_path_outside_the_allowed_roots_is_refused_before_any_claim_or_approval(
        self, tmp_path: Path, tool: str,
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = seed(tmp_path / "outside.txt", b"one\ntwo\n")
        ctx = _Ctx(workspace)

        result = _TOOLS[tool](ctx, "../outside.txt")

        assert result["ok"] is False
        assert ctx.requested == []
        assert outside.read_bytes() == b"one\ntwo\n"
        assert FakeSupervisor.of(workspace).ledger.claims.paths() == []

    def test_a_task_the_supervisor_already_ended_cannot_write(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        ctx.supervisor.start("task-1", session=_SESSION)
        ctx.supervisor.end("task-1", status="failed")

        result = diff(ctx, "a.txt")

        assert result["ok"] is False and result["data"]["taskEnded"] is True
        assert path.read_bytes() == b"one\ntwo\n" and ctx.requested == []


class TestStampsPerTask:
    def test_the_next_task_on_another_worker_still_sees_the_stamps(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        reader = _Ctx(tmp_path, task="task-1", worker="worker-1")
        read(reader, "a.txt")
        reader.supervisor.end("task-1")
        # 读策略要审批：回显得靠会话层里那一版读过的戳，而不是再问一次读策略
        follower = _Ctx(tmp_path, task="task-2", worker="worker-2", read_file="approval_required")

        result = diff(follower, "a.txt")

        assert result["ok"] is True and result["data"]["echoed"] is True
        assert follower.evaluated == []
        assert path.read_bytes() == b"one\nTWO\n"

    def test_a_parallel_task_s_change_makes_an_older_read_stale(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        first = _Ctx(tmp_path, task="task-1")
        second = _Ctx(tmp_path, task="task-2")
        read(first, "a.txt")
        read(second, "a.txt")
        assert diff(second, "a.txt")["ok"] is True
        second.supervisor.end("task-2")

        result = diff(first, "a.txt", "one", "ONE")

        assert result["ok"] is False and result["data"]["stale"] is True
        assert result["error"] == file_state.STALE_MESSAGE
        assert "changedByOtherTask" not in result["data"]
        assert path.read_bytes() == b"one\nTWO\n"
        read(first, "a.txt")
        assert diff(first, "a.txt", "one", "ONE")["ok"] is True

    @pytest.mark.parametrize("tool", sorted(_TOOLS))
    def test_a_version_shared_after_the_task_started_counts_as_changed(self, tmp_path: Path, tool: str) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        fake = FakeSupervisor.of(tmp_path)
        fake.start("task-1", session=_SESSION)
        writer = _Ctx(tmp_path, task="task-2")
        read(writer, "a.txt")
        diff(writer, "a.txt")
        fake.end("task-2")
        late = _Ctx(tmp_path, task="task-1")

        result = _TOOLS[tool](late, "a.txt")

        assert result["ok"] is False
        assert result["error"] == file_state.FOREIGN_MESSAGE
        assert result["data"]["stale"] is True and result["data"]["changedByOtherTask"] is True
        assert path.read_bytes() == b"one\nTWO\n"
        read(late, "a.txt")
        assert _TOOLS[tool](late, "a.txt")["ok"] is True

    def test_a_task_that_started_after_the_change_uses_the_shared_version(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        writer = _Ctx(tmp_path, task="task-1")
        read(writer, "a.txt")
        diff(writer, "a.txt")
        writer.supervisor.end("task-1")
        later = _Ctx(tmp_path, task="task-2", read_file="approval_required")

        result = diff(later, "a.txt", "one", "ONE")

        assert result["ok"] is True and result["data"]["echoed"] is True
        assert path.read_bytes() == b"ONE\nTWO\n"

    def test_a_running_task_s_read_does_not_count_for_another_task(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")
        read(_Ctx(tmp_path, task="task-1"), "a.txt")
        other = _Ctx(tmp_path, task="task-2", read_file="approval_required")

        result = diff(other, "a.txt")

        # 没读过就改照样能改，只是回显要按读策略问；另一个任务读过不算本任务看过
        assert result["ok"] is True and result["data"]["echoed"] is False
        assert other.evaluated == [("read_file", {"path": "a.txt"})]

    def test_a_new_version_that_could_not_be_recorded_asks_for_a_new_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\nthree\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "a.txt")

        def refuse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise SupervisorDown("record lost")

        monkeypatch.setattr(ctx.supervisor, "record", refuse)
        first = diff(ctx, "a.txt")
        monkeypatch.undo()

        assert first["ok"] is True and first["data"]["stampRecorded"] is False
        assert file_state.STAMP_NOT_RECORDED_NOTE in first["data"]["result"]
        assert path.read_bytes() == b"one\nTWO\nthree\n"
        # 本任务名下还是读到的旧版本：再改会被判过期，重读之后才能接着改
        assert diff(ctx, "a.txt", "three", "THREE")["data"]["stale"] is True
        read(ctx, "a.txt")
        assert diff(ctx, "a.txt", "three", "THREE")["ok"] is True


class TestSupervisorUnreachable:
    @pytest.mark.parametrize("tool", sorted(_TOOLS))
    def test_edits_are_refused_and_nothing_is_written(self, tmp_path: Path, tool: str) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        ctx.supervisor.down = True

        result = _TOOLS[tool](ctx, "a.txt")

        assert result["ok"] is False and result["data"]["unavailable"] is True
        assert result["error"].startswith(file_state.UNAVAILABLE_MESSAGE)
        assert path.read_bytes() == b"one\ntwo\n"
        assert ctx.requested == []
        assert EditJournal(tmp_path).tasks(_SESSION) == []

    def test_a_new_file_is_not_created_either(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)
        ctx.supervisor.down = True

        assert write(ctx, "fresh.txt")["ok"] is False
        assert not (tmp_path / "fresh.txt").exists()

    def test_reading_still_works(self, tmp_path: Path) -> None:
        seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        ctx.supervisor.down = True

        result = read(ctx, "a.txt")

        assert result["ok"] is True and "two" in result["content"]


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


def _in_thread(body: Callable[[], dict[str, Any]]) -> tuple[threading.Event, dict[str, Any]]:
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = body()
        except Exception as exc:
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done, outcome


class TestAnotherProcessWritingTheSameFile:
    @pytest.mark.parametrize("tool", ["apply_diff", "write_file"])
    def test_the_edit_waits_for_the_lock_and_then_sees_the_change(self, tmp_path: Path, hold, tool: str) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "a.txt")
        holder = hold(file_state.edit_lock_path(path.resolve(), tmp_path))

        done, outcome = _in_thread(lambda: _TOOLS[tool](ctx, "a.txt"))
        assert not done.wait(_BLOCKED_FOR)
        assert path.read_bytes() == b"one\ntwo\n"
        # 拿着锁的那个进程在它的锁里改了文件
        path.write_bytes(b"one\ntwo\ntheirs\n")
        holder.release()

        assert done.wait(WAIT_SEC) and "error" not in outcome, outcome
        result = outcome["result"]
        assert result["ok"] is False and result["data"]["stale"] is True
        assert path.read_bytes() == b"one\ntwo\ntheirs\n"
        assert holders(tmp_path, "a.txt") == []

    def test_once_the_lock_is_free_an_unchanged_file_is_written(self, tmp_path: Path, hold) -> None:
        path = seed(tmp_path / "a.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "a.txt")
        holder = hold(file_state.edit_lock_path(path.resolve(), tmp_path))

        done, outcome = _in_thread(lambda: diff(ctx, "a.txt"))
        assert not done.wait(_BLOCKED_FOR)
        holder.release()

        assert done.wait(WAIT_SEC) and outcome["result"]["ok"] is True
        assert path.read_bytes() == b"one\nTWO\n"
