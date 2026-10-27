"""隔离副本的生命周期：三种派发都建得出 worktree，孙任务和独立工具任务继承，建不成当场失败；
收尾算 diff 交回调用方，continue_from 接着用同一个副本；放弃、取消、到期、重启后的孤儿都会被清理，
清理前先停常驻进程、拆链接。"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _worktree_fixture import (  # noqa: E402
    Env,
    branches,
    close_service,
    entries,
    env,  # noqa: F401
    git,
    head,
    item,
    make_repo,
    make_state,
    requires_git,
    tool_item,
    write,
    write_policy,
    write_runtime,
    wt_git,
)
from engine.project_root import (  # noqa: E402
    ISOLATION_CONTEXT_KEY,
    PROJECT_ORIGIN_KEY,
    PROJECT_ROOT_KEY,
    WORKTREE_KEY,
    task_project_root,
)
from supervisor.types import TaskStatus  # noqa: E402
from supervisor.worktrees import service as service_module  # noqa: E402
from supervisor.worktrees.keys import (  # noqa: E402
    CALLER_WORKTREE_KEY,
    WORKTREE_OUTCOME_KEY,
    WORKTREE_OWNER_KEY,
    WORKTREE_PENDING_KEY,
)
from supervisor.worktrees.registry import registry_path  # noqa: E402
from supervisor.worktrees.service import WorktreeService  # noqa: E402

pytestmark = requires_git


def _edit_worktree(path: Path) -> None:
    """子任务在副本里做的改动：改一个、删一个、新建一个、留一个未跟踪的、再提交一个，外加一个二进制文件。"""
    write(path / "app" / "main.py", "def main():\n    return 2\n")
    (path / "app" / "old.py").unlink()
    write(path / "app" / "new.py", "NEW = 1\n")
    write(path / "notes.txt", "scratch\n")
    write(path / "assets" / "logo.bin", b"\x00\x01\x02\x03")
    write(path / "README.md", "hello\nworld\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "child commit")


def _by_path(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in view["files"]}


class _FakeProcesses:
    """记下清理前停了哪些目录下的常驻进程，以及那时副本和链接还在不在。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool, bool, float]] = []

    def stop_under_root(self, root: Path | str, *, reason: str = "", wait_sec: float = 10.0) -> int:
        path = Path(root)
        self.calls.append((path, path.is_dir(), (path / "node_modules").exists(), wait_sec))
        return 0


class TestCreation:
    def test_a_wait_batch_item_runs_in_its_own_worktree(self, env: Env) -> None:
        caller = env.caller(env.session())
        [child] = env.wait_batch(caller, item(isolation="worktree"))
        env.wait()

        entry = env.entry(child)
        path = Path(entry["path"])
        assert entry["state"] == "ready"
        assert path.resolve() == env.root / wt_git.repo_key(env.ws) / child.task_id
        assert entry["branch"] == f"clonoth/{child.task_id}"
        assert entry["base"] == head(env.ws)
        assert entry["owners"] == [child.task_id]
        assert entry["requested_by"] == caller.task_id
        assert Path(entry["patch"]).resolve() == path.resolve().with_name(f"{child.task_id}.patch")
        assert Path(child.input[PROJECT_ROOT_KEY]).resolve() == path.resolve()
        assert Path(child.input[PROJECT_ORIGIN_KEY]).resolve() == env.ws.resolve()
        assert task_project_root(child.input, env.ws) == path.resolve()
        assert child.input[WORKTREE_KEY] == child.task_id
        assert child.input[WORKTREE_OWNER_KEY] is True
        assert child.input[WORKTREE_PENDING_KEY] is None
        isolation = child.input["task_context"][ISOLATION_CONTEXT_KEY]
        assert isolation["mode"] == "worktree" and isolation["worktree"] == child.task_id
        assert isolation["branch"] == entry["branch"] and isolation["base"] == entry["base"]
        assert Path(isolation["origin"]).resolve() == env.ws.resolve()
        assert isolation["inherited"] is False and isolation["reused"] is False
        assert isolation["workspace_repo"] is True
        assert isolation["env"] == {"origin_root": "CLONOTH_ORIGIN_ROOT", "origin_venv": "CLONOTH_ORIGIN_VENV"}
        assert (path / "README.md").read_text(encoding="utf-8") == "hello\n"
        assert f"clonoth/{child.task_id}" in branches(env.ws)

    def test_the_registry_is_written_to_data_worktrees_json(self, env: Env) -> None:
        child = env.isolated_child(env.caller(env.session()))

        data = json.loads(registry_path(env.ws).read_text(encoding="utf-8"))

        assert data["version"] == 1
        stored = data["worktrees"][child.task_id]
        assert stored["state"] == "ready" and stored["branch"] == f"clonoth/{child.task_id}"
        assert stored["repo_key"] == wt_git.repo_key(env.ws)

    def test_the_child_cannot_be_claimed_before_its_worktree_is_ready(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gate = threading.Event()
        original = WorktreeService._provision_outside_lock

        def held(self: WorktreeService, worktree_id: str, spec: dict[str, Any]) -> None:
            assert gate.wait(60)
            original(self, worktree_id, spec)

        monkeypatch.setattr(WorktreeService, "_provision_outside_lock", held)
        caller = env.caller(env.session())
        [child] = env.wait_batch(caller, item(isolation="worktree"))

        try:
            assert isinstance(child.input[WORKTREE_PENDING_KEY], dict)
            assert env.entry(child)["state"] == "creating"
            assert env.state.assign_next_task(worker_id="worker-9") is None
        finally:
            gate.set()
        env.wait()

        claimed = env.state.assign_next_task(worker_id="worker-9")
        assert claimed is not None and claimed["task_id"] == child.task_id
        assert claimed["input"][WORKTREE_KEY] == child.task_id

    def test_a_single_dispatch_runs_in_its_own_worktree(self, env: Env) -> None:
        caller = env.caller(env.session())

        child = env.dispatch_single(caller, isolation="worktree")
        env.wait()

        assert env.entry(child)["state"] == "ready"
        assert Path(child.input[PROJECT_ROOT_KEY]).resolve() == Path(env.entry(child)["path"]).resolve()
        assert child.input[WORKTREE_OWNER_KEY] is True

    def test_an_async_dispatch_runs_in_its_own_worktree(self, env: Env) -> None:
        sid = env.session()
        caller = env.caller(sid)

        child = env.async_child(sid, caller, isolation="worktree")
        env.wait()

        entry = env.entry(child)
        assert entry["state"] == "ready" and entry["requested_by"] == caller.task_id
        assert Path(child.input[PROJECT_ROOT_KEY]).resolve() == Path(entry["path"]).resolve()
        assert CALLER_WORKTREE_KEY not in child.input

    def test_without_isolation_nothing_changes(self, env: Env) -> None:
        sid = env.session()
        caller = env.caller(sid)

        [child] = env.wait_batch(caller, item())
        async_child = env.async_child(sid, env.caller(sid))

        for task in (child, async_child):
            for key in (PROJECT_ROOT_KEY, PROJECT_ORIGIN_KEY, WORKTREE_KEY, WORKTREE_OWNER_KEY, WORKTREE_PENDING_KEY):
                assert key not in task.input
            assert ISOLATION_CONTEXT_KEY not in (task.input.get("task_context") or {})
        assert env.service.registry.entries() == []
        assert not env.root.exists()


class TestInheritance:
    def test_grandchildren_and_tool_tasks_work_in_the_same_worktree(self, env: Env) -> None:
        child = env.isolated_child(env.caller(env.session()))

        grandchild, tool_task = env.wait_batch(child, item("b.node"), tool_item())

        for task in (grandchild, tool_task):
            assert task.input[WORKTREE_KEY] == child.task_id
            assert task.input[PROJECT_ROOT_KEY] == child.input[PROJECT_ROOT_KEY]
            assert task.input[PROJECT_ORIGIN_KEY] == child.input[PROJECT_ORIGIN_KEY]
            assert WORKTREE_OWNER_KEY not in task.input
            assert WORKTREE_PENDING_KEY not in task.input
            isolation = task.input["task_context"][ISOLATION_CONTEXT_KEY]
            assert isolation["worktree"] == child.task_id and isolation["inherited"] is True
        assert env.entry(child)["owners"] == [child.task_id]

    def test_a_task_already_in_a_worktree_cannot_ask_for_another(self, env: Env) -> None:
        sid = env.session()
        child = env.isolated_child(env.caller(sid))
        sibling = env.isolated_child(env.caller(sid))

        [nested] = env.wait_batch(child, item("b.node", isolation="worktree"))
        async_nested = env.async_child(sid, sibling, isolation="worktree", repo=str(env.ws))
        env.wait()

        for task, holder in ((nested, child), (async_nested, sibling)):
            assert task.status == TaskStatus.failed
            assert f"隔离副本 {holder.task_id} 里" in task.result["error"]
            assert "不能再要求隔离" in task.result["error"]
            assert WORKTREE_KEY not in task.input and WORKTREE_PENDING_KEY not in task.input
            assert env.entry(task) is None
        [entry] = entries(child)
        assert entry["status"] == "fail" and "不能再要求隔离" in entry["error"]
        assert {row["id"] for row in env.service.registry.entries()} == {child.task_id, sibling.task_id}


class TestRefusals:
    def test_a_repo_outside_the_workspace_and_extra_roots_is_refused_on_the_spot(self, env: Env) -> None:
        other = make_repo(env.tmp / "elsewhere")
        caller = env.caller(env.session())

        [child] = env.wait_batch(caller, item(isolation="worktree", repo=str(other)))

        assert child.status == TaskStatus.failed
        assert "extra_roots" in child.result["error"]
        [entry] = entries(caller)
        assert entry["status"] == "fail" and "extra_roots" in entry["error"]
        assert env.service.registry.get(child.task_id) is None
        assert not env.root.exists()

    def test_a_repo_that_does_not_exist_is_refused_on_the_spot(self, env: Env) -> None:
        caller = env.caller(env.session())

        [child] = env.wait_batch(caller, item(isolation="worktree", repo=str(env.tmp / "missing")))

        assert child.status == TaskStatus.failed and "不存在" in child.result["error"]

    def test_a_worktree_that_cannot_be_created_fails_the_child_and_tells_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLONOTH_INSTANCES_FILE", raising=False)
        ws = make_repo(tmp_path / "ws", commit=False)
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller = environment.caller(environment.session())

            [child] = environment.wait_batch(caller, item(isolation="worktree"))
            environment.wait()

            assert child.status == TaskStatus.failed
            assert "建隔离副本失败" in child.result["error"] and "还没有提交" in child.result["error"]
            assert child.input[WORKTREE_PENDING_KEY] is None
            assert PROJECT_ROOT_KEY not in child.input and WORKTREE_KEY not in child.input
            [entry] = entries(caller)
            assert entry["status"] == "fail" and "建隔离副本失败" in entry["error"]
            registered = environment.entry(child)
            assert registered["state"] == "failed" and "还没有提交" in registered["error"]
        finally:
            close_service(state)

    def test_a_repo_under_extra_roots_is_isolated_and_maps_back_to_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLONOTH_INSTANCES_FILE", raising=False)
        libs = tmp_path / "libs"
        external = make_repo(libs / "shared-lib", {"lib.py": "VALUE = 1\n"})
        ws = make_repo(tmp_path / "ws")
        write_policy(ws, extra_roots=[libs.as_posix()])
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller = environment.caller(environment.session())

            [child] = environment.wait_batch(caller, item(isolation="worktree", repo=str(external)))
            environment.wait()

            entry = environment.entry(child)
            assert entry["state"] == "ready" and entry["workspace_repo"] is False
            assert Path(entry["repo"]).resolve() == external.resolve()
            assert Path(entry["path"]).resolve().parent.name == wt_git.repo_key(external)
            assert Path(child.input[PROJECT_ORIGIN_KEY]).resolve() == external.resolve()
            assert (environment.worktree_of(child) / "lib.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        finally:
            close_service(state)


class TestOutcome:
    def test_the_diff_covers_committed_modified_deleted_added_and_untracked_files(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        _edit_worktree(env.worktree_of(child))

        env.finish(child)

        [entry] = entries(caller)
        view = entry["worktree"]
        files = _by_path(view)
        assert set(files) == {"README.md", "app/main.py", "app/old.py", "app/new.py", "notes.txt", "assets/logo.bin"}
        assert files["README.md"]["status"] == "M" and files["README.md"]["added"] == 1
        assert files["app/main.py"] == {"path": "app/main.py", "status": "M", "added": 1, "deleted": 1}
        assert files["app/old.py"]["status"] == "D" and files["app/old.py"]["deleted"] == 1
        assert files["app/new.py"]["status"] == "A" and files["notes.txt"]["status"] == "A"
        assert files["assets/logo.bin"] == {"path": "assets/logo.bin", "status": "A", "binary": True}
        assert view["file_count"] == 6 and view["state"] == "finished"
        assert view["handle"] and view["id"] == child.task_id
        assert view["merge_allowed"] is True
        patch = Path(view["patch"])
        assert patch.is_file() and b"b/app/new.py" in patch.read_bytes()
        assert "app/new.py" in view["diff"] and view["diff_truncated"] is False
        assert env.entry(child)["state"] == "finished"
        assert env.entry(child)["outcome"]["file_count"] == 6
        assert "diff" not in env.entry(child)["outcome"]
        assert child.input[WORKTREE_OUTCOME_KEY]["digest"] == view["digest"]

    def test_a_long_diff_is_cut_inline_and_points_at_the_patch(self, env: Env) -> None:
        write_runtime(env.ws, {"engine": {"worktree": {"inline_diff_chars": 200}}})
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "big.txt", "".join(f"line {index}\n" for index in range(400)))

        env.finish(child)

        view = entries(caller)[0]["worktree"]
        assert view["diff_truncated"] is True and len(view["diff"]) <= 200
        assert Path(view["patch"]).stat().st_size > 200

    def test_a_failed_child_also_hands_back_its_diff(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")

        env.fail(child, "ran out of steps")

        [entry] = entries(caller)
        assert entry["status"] == "fail"
        assert [item["path"] for item in entry["worktree"]["files"]] == ["app/new.py"]

    def test_a_single_dispatch_hands_its_diff_back_with_the_child_result(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.dispatch_single(caller, isolation="worktree")
        env.wait()
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")

        env.finish(child)

        resume = caller.input["resume_data"]
        assert resume["type"] == "child_result"
        assert resume["worktree"]["id"] == child.task_id
        assert [item["path"] for item in resume["worktree"]["files"]] == ["app/new.py"]

    def test_an_async_result_carries_the_diff_back_to_the_callers_session(self, env: Env) -> None:
        sid = env.session()
        caller = env.caller(sid)
        child = env.async_child(sid, caller, isolation="worktree")
        env.wait()
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")

        env.finish(child)

        [callback] = env.callbacks()
        carried = callback.input["inbound_child_worktree"]
        assert carried["id"] == child.task_id
        assert [item["path"] for item in carried["files"]] == ["app/new.py"]
        assert WORKTREE_KEY not in callback.input

    def test_a_child_without_changes_says_so(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)

        env.finish(child)

        view = entries(caller)[0]["worktree"]
        assert view["file_count"] == 0 and view["files"] == []
        assert env.entry(child)["state"] == "finished"

    def test_children_finishing_at_once_with_the_same_new_file_all_get_their_diff(self, env: Env) -> None:
        children = [env.isolated_child(env.caller(env.session())) for _ in range(3)]
        for child in children:
            write(env.worktree_of(child) / "pkg" / "__init__.py", "VERSION = 1\n")
        errors: list[BaseException] = []

        def finish(task: Any) -> None:
            try:
                env.finish(task)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=finish, args=(child,)) for child in children]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        assert errors == []
        for child in children:
            view = child.input[WORKTREE_OUTCOME_KEY]
            assert "error" not in view, view
            assert [item["path"] for item in view["files"]] == ["pkg/__init__.py"]


class TestContinuation:
    def test_continue_from_reuses_the_same_worktree(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")
        env.fail(child, "ran out of steps")
        [first] = entries(caller)
        assert first["continuable"] is True

        [second] = [
            task for task in env.wait_batch(caller, item(continue_from=first["handle"]))
            if task.task_id != child.task_id
        ]

        assert second.input[WORKTREE_KEY] == child.task_id
        assert second.input[WORKTREE_OWNER_KEY] is True
        assert WORKTREE_PENDING_KEY not in second.input
        assert second.input["task_context"][ISOLATION_CONTEXT_KEY]["reused"] is True
        entry = env.entry(child)
        assert entry["state"] == "ready" and entry["owners"] == [child.task_id, second.task_id]
        assert (env.worktree_of(second) / "app" / "new.py").read_text(encoding="utf-8") == "NEW = 1\n"

        write(env.worktree_of(second) / "app" / "more.py", "MORE = 1\n")
        env.finish(second)

        view = [entry for entry in entries(caller) if entry["child_task_id"] == second.task_id][0]["worktree"]
        assert {item["path"] for item in view["files"]} == {"app/new.py", "app/more.py"}
        assert env.entry(child)["state"] == "finished"

    def test_continue_from_after_the_worktree_is_gone_starts_a_new_one_from_head(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        env.fail(child, "ran out of steps")
        [first] = entries(caller)
        assert env.service.abandon(task_id=caller.task_id, handle=first["handle"])["ok"] is True
        env.service.housekeeping()
        assert env.entry(child)["state"] == "removed"

        [second] = [
            task for task in env.wait_batch(caller, item(continue_from=first["handle"]))
            if task.task_id != child.task_id
        ]
        env.wait()

        entry = env.entry(second)
        assert entry["state"] == "ready" and entry["previous"] == child.task_id
        assert entry["previous_state"] == "removed"
        assert second.input["task_context"][ISOLATION_CONTEXT_KEY]["previous"] == {
            "worktree": child.task_id, "state": "removed",
        }


class TestAbandonAndCleanup:
    def test_abandoning_removes_the_worktree_and_its_branch(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        path = env.worktree_of(child)
        write(path / "app" / "new.py", "NEW = 1\n")
        env.finish(child)
        handle = entries(caller)[0]["worktree"]["handle"]

        result = env.service.abandon(task_id=caller.task_id, handle=handle)
        assert result["ok"] is True and env.entry(child)["state"] == "abandoned"
        env.service.housekeeping()

        entry = env.entry(child)
        assert entry["state"] == "removed" and entry["cleanup_reason"] == "abandoned"
        assert not path.exists()
        assert f"clonoth/{child.task_id}" not in branches(env.ws)
        assert child.task_id not in git(env.ws, "worktree", "list")

    def test_a_worktree_still_in_use_cannot_be_abandoned(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        env.wait_batch(child, item("b.node"))

        result = env.service.abandon(task_id=caller.task_id, handle=child.task_id)

        assert result["ok"] is False and child.task_id in result["message"]
        assert env.entry(child)["state"] == "ready"

    def test_a_cancelled_child_is_abandoned_and_its_changes_are_kept_as_a_patch(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")

        with env.state._lock:
            env.state._cancel_task_locked(child, datetime.now(timezone.utc), force=True)
        assert env.entry(child)["state"] == "abandoned"
        assert env.entry(child)["cleanup_reason"] == "cancelled"
        env.service.housekeeping()

        entry = env.entry(child)
        assert entry["state"] == "removed" and entry["kept_patch"] is True
        assert b"b/app/new.py" in Path(entry["patch"]).read_bytes()
        assert not env.worktree_of(child).exists()

    def test_a_child_cancelled_while_its_worktree_is_being_built_never_gets_it(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gate = threading.Event()
        building = threading.Event()
        original = WorktreeService._provision_outside_lock

        def held(self: WorktreeService, worktree_id: str, spec: dict[str, Any]) -> None:
            building.set()
            assert gate.wait(60)
            original(self, worktree_id, spec)

        monkeypatch.setattr(WorktreeService, "_provision_outside_lock", held)
        caller = env.caller(env.session())
        [child] = env.wait_batch(caller, item(isolation="worktree"))
        # 后台线程真的进了建副本那一步再取消：取消得更早时它根本不会去建
        assert building.wait(60)

        with env.state._lock:
            env.state._cancel_task_locked(child, datetime.now(timezone.utc), force=True)
        gate.set()
        env.wait()

        assert env.entry(child)["state"] == "abandoned"
        assert child.input[WORKTREE_PENDING_KEY] is None and PROJECT_ROOT_KEY not in child.input
        env.service.housekeeping()
        entry = env.entry(child)
        assert entry["state"] == "removed"
        assert not Path(entry["path"]).exists()
        assert f"clonoth/{child.task_id}" not in branches(env.ws)

    def test_processes_are_stopped_and_links_removed_before_the_directory_goes(self, env: Env) -> None:
        write(env.ws / "node_modules" / "pkg" / "index.js", "module.exports = 1;\n")
        write_runtime(env.ws, {"engine": {"worktree": {"link_dirs": ["node_modules"]}}})
        processes = _FakeProcesses()
        env.state.processes = processes
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        path = env.worktree_of(child)
        assert env.entry(child)["links"] == ["node_modules"]
        assert wt_git.is_link(path / "node_modules")
        assert (path / "node_modules" / "pkg" / "index.js").is_file()
        write(path / "app" / "new.py", "NEW = 1\n")

        env.finish(child)
        view = entries(caller)[0]["worktree"]
        assert [item["path"] for item in view["files"]] == ["app/new.py"]
        env.service.abandon(task_id=caller.task_id, handle=view["handle"])
        env.service.housekeeping()

        [(stopped, existed, linked, wait_sec)] = processes.calls
        assert stopped.resolve() == path.resolve() and existed and linked and wait_sec > 0
        assert not path.exists()
        assert (env.ws / "node_modules" / "pkg" / "index.js").read_text(encoding="utf-8") == "module.exports = 1;\n"


class TestRetention:
    def test_an_unmerged_worktree_expires_after_the_grace_period(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")
        env.finish(child)

        env.service.housekeeping()
        assert env.entry(child)["state"] == "finished" and not env.entry(child).get("until")

        env.finish(caller)
        env.service.housekeeping()
        until = datetime.fromisoformat(env.entry(child)["until"])
        assert timedelta(hours=23) < until - datetime.now(timezone.utc) <= timedelta(hours=24)

        later = until + timedelta(minutes=1)
        monkeypatch.setattr(service_module, "_utcnow", lambda: later)
        env.service.housekeeping()

        entry = env.entry(child)
        assert entry["state"] == "removed" and entry["cleanup_reason"] == "expired"
        assert entry["kept_patch"] is True and Path(entry["patch"]).is_file()
        assert not env.worktree_of(child).exists()

    def test_the_grace_period_can_be_set_on_its_own(self, env: Env) -> None:
        write_runtime(env.ws, {"engine": {"worktree": {"retain_grace_hours": 2}}})
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        env.finish(child)
        env.finish(caller)

        env.service.housekeeping()

        until = datetime.fromisoformat(env.entry(child)["until"])
        assert timedelta(hours=1) < until - datetime.now(timezone.utc) <= timedelta(hours=2)

    def test_tombstones_and_their_patches_are_dropped_after_tombstone_days(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        write(env.worktree_of(child) / "app" / "new.py", "NEW = 1\n")
        env.finish(child)
        patch = Path(entries(caller)[0]["worktree"]["patch"])
        env.service.abandon(task_id=caller.task_id, handle=child.task_id)
        env.service.housekeeping()
        assert env.entry(child)["state"] == "removed" and patch.is_file()

        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(service_module, "_utcnow", lambda: later)
        env.service.housekeeping()

        assert env.entry(child) is None and not patch.exists()


class TestRestart:
    def test_orphans_are_cleaned_and_unregistered_worktrees_are_left_alone(self, env: Env) -> None:
        caller = env.caller(env.session())
        running, finished = env.wait_batch(caller, item(isolation="worktree"), item("b.node", isolation="worktree"))
        env.wait()
        write(env.worktree_of(running) / "app" / "new.py", "NEW = 1\n")
        env.finish(finished)
        stray = env.root / wt_git.repo_key(env.ws) / "not-registered"
        git(env.ws, "worktree", "add", "-q", "-b", "someone/else", str(stray), "HEAD")
        close_service(env.state)

        restarted = env.extra_state()
        service = WorktreeService(restarted)
        restarted.worktrees = service
        service.recover()

        assert service.registry.get(running.task_id)["state"] == "abandoned"
        assert service.registry.get(running.task_id)["cleanup_reason"] == "orphan"
        assert service.registry.get(finished.task_id)["state"] == "finished"
        assert service.registry.get(finished.task_id)["until"]
        service.housekeeping()

        orphan = service.registry.get(running.task_id)
        assert orphan["state"] == "removed" and orphan["kept_patch"] is True
        assert not Path(orphan["path"]).exists()
        assert Path(service.registry.get(finished.task_id)["path"]).is_dir()
        assert (stray / "README.md").is_file()
        assert "someone/else" in branches(env.ws)

    def test_a_merge_cut_short_by_a_restart_goes_back_to_waiting_with_a_note(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        env.finish(child)
        env.service.registry.transition(child.task_id, {"finished"}, "merging", merging_task=caller.task_id)
        close_service(env.state)

        restarted = env.extra_state()
        service = WorktreeService(restarted)
        restarted.worktrees = service
        service.recover()

        entry = service.registry.get(child.task_id)
        assert entry["state"] == "finished" and f"merge-{child.task_id}" in entry["note"]

    def test_an_entry_outside_the_configured_root_is_forgotten_but_not_touched(self, env: Env) -> None:
        child = env.isolated_child(env.caller(env.session()))
        path = env.worktree_of(child)
        close_service(env.state)
        write_runtime(env.ws, {"engine": {"worktree": {"root": str(env.tmp / "elsewhere-root")}}})

        restarted = env.extra_state()
        service = WorktreeService(restarted)
        restarted.worktrees = service
        service.recover()
        service.housekeeping()

        entry = service.registry.get(child.task_id)
        assert entry["state"] == "removed" and "没有清理" in entry["note"]
        assert path.is_dir()

    def test_a_requeued_owner_keeps_its_worktree(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)
        with env.state._lock:
            child.status = TaskStatus.pending
            child.worker_id = None

        env.service.housekeeping()

        assert env.entry(child)["state"] == "ready"
        assert env.worktree_of(child).is_dir()

    def test_a_corrupt_registry_is_set_aside(self, env: Env) -> None:
        path = registry_path(env.ws)
        write(path, "{not json")

        service = WorktreeService(env.state)

        assert service.registry.entries() == []
        assert "格式不对" in service.registry.load_error
        assert [item.name for item in path.parent.iterdir() if item.name.startswith("worktrees.json.corrupt-")]


def test_the_runtime_keys_have_their_documented_defaults(env: Env) -> None:
    settings = env.service.settings()

    assert settings.root == env.root and settings.root_problem == ""
    assert settings.retain_grace == timedelta(hours=24)
    assert settings.merge_instances == () and settings.link_dirs == ()
    assert settings.inline_diff_chars == 6000 and settings.tombstone_days == 7


def test_a_root_inside_the_workspace_is_refused(env: Env) -> None:
    write_runtime(env.ws, {"engine": {"worktree": {"root": "inner-worktrees"}}})
    caller = env.caller(env.session())

    [child] = env.wait_batch(caller, item(isolation="worktree"))
    env.wait()

    assert child.status == TaskStatus.failed and "engine.worktree.root" in child.result["error"]
    assert not (env.ws / "inner-worktrees").exists()


def test_link_dirs_only_take_plain_relative_paths(env: Env) -> None:
    write_runtime(env.ws, {"engine": {"worktree": {"link_dirs": [
        "node_modules", "../escape", "/abs", "C:/abs", ".git", "web/node_modules", "", "node_modules",
    ]}}})

    assert env.service.settings().link_dirs == ("node_modules", "web/node_modules")
