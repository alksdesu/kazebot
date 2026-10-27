"""任务的项目根：文件工具、命令、项目说明和模型点名的附件按项目根找文件；编辑日志、锁文件、策略和运行配置仍在实例根下。

项目根来自任务 input 的保留键，没给或不合格就是工作区根；送给 supervisor 的路径写法和不带项目根时一样。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shlex
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_layout  # noqa: E402
from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, echo, make_loop_state, make_rctx, turn  # noqa: E402
from _file_edits_fake import FakeFileEdits  # noqa: E402
from clonoth_runtime import classify_path  # noqa: E402
from engine import runner  # noqa: E402
from engine.builtin import project_instructions  # noqa: E402
from engine.builtin.project_instructions import MESSAGE_KEY, ProjectInstructions, discover  # noqa: E402
from engine.context import RunContext  # noqa: E402
from engine.inference import attachment_access  # noqa: E402
from engine.inference.attachment_access import RefusedAttachment, screen_model_attachment_paths  # noqa: E402
from engine.inference.loop_state import build_tool_context  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.project_root import PROJECT_ROOT_KEY, task_project_root  # noqa: E402
from engine.protocol import TaskAction  # noqa: E402
from toolbox import file_state, syntax_check  # noqa: E402
from toolbox._common import resolve_and_classify, resolve_under_allowed_roots  # noqa: E402
from toolbox.builtins import (  # noqa: E402
    apply_diff,
    edit_lines,
    execute_command,
    glob_files,
    list_dir,
    read_file,
    search_in_files,
    undo_edits,
    write_file,
)
from toolbox.context import ToolContext, project_root_of  # noqa: E402
from toolbox.edit_journal import EditJournal  # noqa: E402
from toolbox.hashline import line_hash  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.text_files import decode_text, normalize_newlines, split_lines  # noqa: E402

SESSION = "sess-root"
_MISSING_REASON = "no such file in the workspace"
_KEY_LOGGER = "engine.project_root"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def seed(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 按字节写：write_text 在 Windows 上会把 \n 换成 \r\n
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def shell(*argv: str) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def use_runtime(root: Path, data: dict[str, Any]) -> None:
    seed(config_layout.runtime_path(root), yaml.safe_dump(data))


def use_policy(root: Path, data: dict[str, Any]) -> None:
    seed(config_layout.policy_path(root), yaml.safe_dump({"version": 1, **data}))


def first_line_anchor(path: Path) -> str:
    lines = split_lines(normalize_newlines(decode_text(path.read_bytes())))
    return f"1:{line_hash(lines[0])}"


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：送审一律 auto 并记下参数；认领和读戳交给实例根的 FakeSupervisor。"""

    def __init__(self, workspace_root: Path, project_root: Path | None = None, *, task: str = "task-1") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id=SESSION,
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            project_root=project_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id=task,
        )
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.evaluated: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": "auto", "reason": f"{op} decided by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.evaluated.append((op, dict(parameters)))
        return {"safety_level": "auto", "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return False


@pytest.fixture
def roots(tmp_path: Path) -> SimpleNamespace:
    """实例根和项目根是两个不相干的目录；测试往两边放同名文件，看得出读写落在了哪边。"""
    instance = (tmp_path / "instance").resolve()
    project = (tmp_path / "project").resolve()
    instance.mkdir()
    project.mkdir()
    return SimpleNamespace(instance=instance, project=project)


class TestTheTaskInputKey:
    def test_a_valid_directory_becomes_the_project_root(self, roots: SimpleNamespace) -> None:
        found = task_project_root({PROJECT_ROOT_KEY: str(roots.project)}, roots.instance)

        assert found == roots.project

    def test_the_path_is_resolved_and_trimmed(self, roots: SimpleNamespace) -> None:
        (roots.project / "sub").mkdir()
        raw = f"  {roots.project / 'sub' / '..'}  "

        assert task_project_root({PROJECT_ROOT_KEY: raw}, roots.instance) == roots.project

    def test_a_path_object_is_accepted(self, roots: SimpleNamespace) -> None:
        assert task_project_root({PROJECT_ROOT_KEY: roots.project}, roots.instance) == roots.project

    @pytest.mark.parametrize("task_input", [
        {}, {PROJECT_ROOT_KEY: None}, {PROJECT_ROOT_KEY: ""}, {PROJECT_ROOT_KEY: "   "}, None, ["not", "a", "mapping"],
    ])
    def test_without_the_key_it_is_the_workspace_root_and_nothing_is_logged(
        self, roots: SimpleNamespace, caplog: pytest.LogCaptureFixture, task_input: Any,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_KEY_LOGGER):
            found = task_project_root(task_input, roots.instance)

        assert found is roots.instance
        assert caplog.records == []

    @pytest.mark.parametrize("make_raw, reasons", [
        (lambda root: "relative/dir", ("not an absolute path",)),
        (lambda root: str(root / "missing"), ("no such directory",)),
        (lambda root: str(seed(root / "file.txt", "x")), ("not a directory",)),
        (lambda root: 7, ("not a path string but int",)),
        (lambda root: ["x"], ("not a path string but list",)),
        (lambda root: {"path": str(root)}, ("not a path string but dict",)),
        (lambda root: True, ("not a path string but bool",)),
        # 有的 Python 版本解析时就报错，有的照原样解析、之后查不到这个目录
        (lambda root: f"{root}{os.sep}bad\x00dir", ("unusable", "no such directory")),
    ])
    def test_an_unusable_value_falls_back_to_the_workspace_root_with_a_warning(
        self, roots: SimpleNamespace, caplog: pytest.LogCaptureFixture, make_raw: Any, reasons: tuple[str, ...],
    ) -> None:
        raw = make_raw(roots.project)

        with caplog.at_level(logging.WARNING, logger=_KEY_LOGGER):
            found = task_project_root({PROJECT_ROOT_KEY: raw}, roots.instance, task_id="task-9")

        assert found is roots.instance
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "task-9" in message and PROJECT_ROOT_KEY in message
        assert any(reason in message for reason in reasons), message

    def test_a_windows_drive_relative_or_rooted_path_is_not_absolute(self, roots: SimpleNamespace) -> None:
        raw = "/project" if os.name == "nt" else "project"

        assert task_project_root({PROJECT_ROOT_KEY: raw}, roots.instance) is roots.instance


class TestTheContexts:
    def test_a_tool_context_without_one_uses_the_workspace_root(self, tmp_path: Path) -> None:
        ctx = ToolContext(
            supervisor_url="", session_id="s", run_id="r", worker_id="w",
            workspace_root=tmp_path, http=None, registry=None,  # type: ignore[arg-type]
        )

        assert ctx.project_root is tmp_path
        assert project_root_of(ctx) is tmp_path

    def test_a_tool_context_keeps_the_one_it_was_given(self, roots: SimpleNamespace) -> None:
        ctx = _Ctx(roots.instance, roots.project)

        assert (ctx.workspace_root, ctx.project_root) == (roots.instance, roots.project)
        assert project_root_of(ctx) == roots.project

    def test_a_copy_for_background_work_keeps_it(self, roots: SimpleNamespace) -> None:
        ctx = ToolContext(
            supervisor_url="", session_id="s", run_id="r", worker_id="w",
            workspace_root=roots.instance, project_root=roots.project, http=None, registry=None,  # type: ignore[arg-type]
        )

        assert dataclasses.replace(ctx, tool_call_id="call-2").project_root == roots.project

    def test_a_run_context_without_one_uses_the_workspace_root(self, tmp_path: Path) -> None:
        rctx = RunContext(tmp_path, "http://sup", "session", "worker", AsyncMock(), AsyncMock(), task_id="t")

        assert rctx.project_root is tmp_path

    def test_a_stand_in_without_the_field_falls_back(self, tmp_path: Path) -> None:
        assert project_root_of(SimpleNamespace(workspace_root=tmp_path)) is tmp_path
        assert project_root_of(SimpleNamespace(workspace_root=tmp_path, project_root=None)) is tmp_path
        assert project_root_of(SimpleNamespace(workspace_root=tmp_path, project_root="")) is tmp_path

    def test_the_loop_hands_it_to_every_tool_context(self, roots: SimpleNamespace) -> None:
        rctx = make_rctx(roots.instance)
        rctx.project_root = roots.project
        ls = make_loop_state(roots.instance, ScriptedRegistry(), rctx=rctx)

        ctx = build_tool_context(ls, tool_call_id="call-1")

        assert ctx.workspace_root == roots.instance
        assert ctx.project_root == roots.project


class TestPathClassification:
    def test_without_a_project_root_nothing_changes(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "src" / "a.py", "x")
        outside = seed(roots.project / "b.py", "x")

        for raw in ("src/a.py", "../escape.txt", str(outside), "src/missing.py"):
            assert classify_path(roots.instance, [], raw, project_root=roots.instance) == classify_path(
                roots.instance, [], raw,
            )

    def test_relative_paths_resolve_under_the_project_root(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "src" / "a.py", "instance")
        target = seed(roots.project / "src" / "a.py", "project")

        resolved, display, external = classify_path(roots.instance, [], "src/a.py", project_root=roots.project)

        assert (resolved, display, external) == (target.resolve(), "src/a.py", False)

    def test_an_absolute_path_in_the_project_root_is_internal(self, roots: SimpleNamespace) -> None:
        target = seed(roots.project / "pkg" / "m.py", "x")

        assert classify_path(roots.instance, [], str(target), project_root=roots.project) == (
            target.resolve(), "pkg/m.py", False,
        )

    def test_workspace_files_keep_their_workspace_names(self, roots: SimpleNamespace) -> None:
        target = seed(roots.instance / "config" / "nodes" / "x.yaml", "x")

        assert classify_path(roots.instance, [], str(target), project_root=roots.project) == (
            target.resolve(), "config/nodes/x.yaml", False,
        )

    def test_a_project_root_inside_the_workspace_is_named_from_the_workspace(self, roots: SimpleNamespace) -> None:
        nested = roots.instance / "checkout"
        target = seed(nested / "a.py", "x")

        assert classify_path(roots.instance, [], "a.py", project_root=nested) == (
            target.resolve(), "checkout/a.py", False,
        )

    def test_leaving_the_project_root_for_the_workspace_is_a_workspace_path(self, roots: SimpleNamespace) -> None:
        target = seed(roots.instance / "notes.md", "x")

        assert classify_path(roots.instance, [], "../instance/notes.md", project_root=roots.project) == (
            target.resolve(), "notes.md", False,
        )

    def test_leaving_every_root_is_still_invalid(self, roots: SimpleNamespace) -> None:
        assert classify_path(roots.instance, [], "../elsewhere/x.py", project_root=roots.project) == (
            None, "path escapes workspace root", False,
        )

    def test_absolute_paths_elsewhere_are_external(self, roots: SimpleNamespace, tmp_path: Path) -> None:
        outside = seed(tmp_path / "elsewhere" / "x.py", "x")

        assert classify_path(roots.instance, [], str(outside), project_root=roots.project) == (
            outside.resolve(), outside.resolve().as_posix(), True,
        )

    def test_extra_roots_come_from_the_instance_policy(self, roots: SimpleNamespace, tmp_path: Path) -> None:
        trusted = seed(tmp_path / "trusted" / "lib.py", "x")
        decoy = seed(tmp_path / "decoy" / "lib.py", "x")
        use_policy(roots.instance, {"extra_roots": [str(trusted.parent)]})
        use_policy(roots.project, {"extra_roots": [str(decoy.parent)]})

        assert resolve_and_classify(roots.instance, str(trusted), project_root=roots.project) == (
            trusted.resolve(), False,
        )
        assert resolve_and_classify(roots.instance, str(decoy), project_root=roots.project) == (
            decoy.resolve(), True,
        )

    def test_resolve_under_allowed_roots_uses_the_project_root(self, roots: SimpleNamespace) -> None:
        target = seed(roots.project / "a.txt", "x")

        assert resolve_under_allowed_roots(roots.instance, "a.txt", project_root=roots.project) == target.resolve()
        assert resolve_under_allowed_roots(roots.instance, "a.txt") == (roots.instance / "a.txt").resolve()
        with pytest.raises(ValueError, match="path escapes workspace root"):
            resolve_under_allowed_roots(roots.instance, "../../nowhere.txt", project_root=roots.project)

    def test_the_command_path_is_project_relative(self, roots: SimpleNamespace, tmp_path: Path) -> None:
        inside = seed(roots.project / "src" / "app.py", "x").resolve()
        outside = seed(tmp_path / "elsewhere.py", "x").resolve()

        assert syntax_check.command_path(roots.project, inside) == "src/app.py"
        assert syntax_check.command_path(roots.project, outside) == outside.as_posix()


class TestFileToolsUseTheProjectRoot:
    @pytest.fixture
    def locks(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
        """记下每次取改文件的锁时给的 (文件, 工作区根)；锁照常去取。"""
        taken: list[tuple[Path, Path]] = []
        real_lock = file_state.edit_lock

        def spy(path: Path, workspace_root: Path | str) -> Any:
            taken.append((Path(path), Path(workspace_root)))
            return real_lock(path, workspace_root)

        monkeypatch.setattr(file_state, "edit_lock", spy)
        return taken

    def test_read_file_reads_the_project_copy(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "src" / "app.py", "instance copy\n")
        seed(roots.project / "src" / "app.py", "project copy\n")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(read_file({"files": [{"path": "src/app.py"}]}, ctx))

        assert result["ok"] is True
        assert "project copy" in result["data"]["result"]
        assert "instance copy" not in result["data"]["result"]

    def test_an_absolute_path_still_reaches_the_instance(self, roots: SimpleNamespace) -> None:
        target = seed(roots.instance / "notes.md", "instance notes\n")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(read_file({"files": [{"path": str(target)}]}, ctx))

        assert "instance notes" in result["data"]["result"]

    def test_an_image_copy_goes_to_the_instance_attachments(self, roots: SimpleNamespace) -> None:
        seed(roots.project / "img" / "dot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        ctx = _Ctx(roots.instance, roots.project)

        result = run(read_file({"files": [{"path": "img/dot.png"}]}, ctx))

        [attachment] = result["attachments"]
        assert attachment["path"].startswith("data/attachments/read_file/")
        assert (roots.instance / attachment["path"]).is_file()
        assert not (roots.project / "data").exists()

    def test_write_file_writes_into_the_project_and_journals_under_the_instance(
        self, roots: SimpleNamespace, locks: list[tuple[Path, Path]],
    ) -> None:
        target = seed(roots.project / "src" / "app.py", "old\n")
        ctx = _Ctx(roots.instance, roots.project)
        run(read_file({"files": [{"path": "src/app.py"}]}, ctx))

        result = run(write_file({"path": "src/app.py", "content": "new\n"}, ctx))

        assert result["ok"] is True, result
        assert target.read_bytes() == b"new\n"
        assert not (roots.instance / "src").exists()
        assert locks == [(target.resolve(), roots.instance)]
        assert file_state.edit_lock_path(target.resolve(), roots.instance).is_file()
        [task] = EditJournal(roots.instance).tasks(SESSION)
        assert [Path(entry.path) for entry in task.entries] == [target.resolve()]
        assert not (roots.project / "data").exists()

    def test_apply_diff_and_edit_lines_change_the_project_copy(
        self, roots: SimpleNamespace, locks: list[tuple[Path, Path]],
    ) -> None:
        bystander = seed(roots.instance / "src" / "app.py", "value = 1\nother = 1\n")
        target = seed(roots.project / "src" / "app.py", "value = 1\nother = 1\n")
        ctx = _Ctx(roots.instance, roots.project)
        run(read_file({"files": [{"path": "src/app.py"}]}, ctx))

        diffed = run(apply_diff({"path": "src/app.py", "diffs": [{"search": "value = 1", "replace": "value = 2"}]}, ctx))
        lined = run(edit_lines({
            "path": "src/app.py",
            "edits": [{"op": "replace", "start": first_line_anchor(target), "content": "value = 3"}],
        }, ctx))

        assert diffed["ok"] is True and lined["ok"] is True, (diffed, lined)
        assert target.read_bytes() == b"value = 3\nother = 1\n"
        assert bystander.read_bytes() == b"value = 1\nother = 1\n"
        assert locks == [(target.resolve(), roots.instance)] * 2
        [task] = EditJournal(roots.instance).tasks(SESSION)
        assert {Path(entry.path) for entry in task.entries} == {target.resolve()}
        assert not (roots.project / "data").exists()

    def test_the_journal_directory_of_the_instance_stays_off_limits(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "data" / "edit_journal" / "x.json", "{}")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(write_file({"path": str(roots.instance / "data" / "edit_journal" / "x.json"), "content": "[]"}, ctx))

        assert result["ok"] is False
        assert "data/edit_journal/" in result["error"]

    def test_undo_edits_puts_the_project_file_back(
        self, roots: SimpleNamespace, locks: list[tuple[Path, Path]],
    ) -> None:
        bystander = seed(roots.instance / "src" / "app.py", "value = 1\n")
        target = seed(roots.project / "src" / "app.py", "value = 1\n")
        ctx = _Ctx(roots.instance, roots.project)
        run(read_file({"files": [{"path": "src/app.py"}]}, ctx))
        run(apply_diff({"path": "src/app.py", "diffs": [{"search": "value = 1", "replace": "value = 2"}]}, ctx))
        assert target.read_bytes() == b"value = 2\n"

        result = run(undo_edits({"paths": ["src/app.py"]}, ctx))

        assert result["ok"] is True, result
        assert target.read_bytes() == b"value = 1\n"
        assert bystander.read_bytes() == b"value = 1\n"
        assert result["data"]["changedPaths"] == ["src/app.py"]
        [parameters] = [params for _op, params in ctx.requested if params.get("tool_name") == "undo_edits"]
        assert parameters["path"] == "src/app.py"
        assert {root for _path, root in locks} == {roots.instance}
        assert {path for path, _root in locks} == {target.resolve()}

    def test_undo_edits_finds_a_named_path_under_the_project_root(self, roots: SimpleNamespace) -> None:
        target = seed(roots.project / "src" / "app.py", "value = 1\n")
        ctx = _Ctx(roots.instance, roots.project)
        run(read_file({"files": [{"path": str(target)}]}, ctx))
        run(apply_diff({"path": str(target), "diffs": [{"search": "value = 1", "replace": "value = 2"}]}, ctx))

        # 改的时候写的是绝对路径，撤的时候按项目根相对路径点名，也认得出是同一个文件
        result = run(undo_edits({"paths": ["src/app.py"]}, ctx))

        assert result["ok"] is True, result
        assert target.read_bytes() == b"value = 1\n"
        assert result["data"]["restoredCount"] == 1

    def test_list_dir_lists_the_project(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "only_in_instance" / "x.txt", "x")
        seed(roots.project / "src" / "app.py", "x")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(list_dir({"paths": ["."]}, ctx))

        names = [entry["name"] for entry in result["data"]["results"][0]["entries"]]
        assert names == ["src/"]

    def test_glob_names_project_files_relative_to_the_project(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "src" / "instance_only.py", "x")
        seed(roots.project / "src" / "app.py", "x")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(glob_files({"pattern": "src/*.py"}, ctx))

        assert result["data"]["files"] == ["src/app.py"]

    def test_search_matches_the_pattern_against_project_relative_paths(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "src" / "app.py", "needle in the instance\n")
        seed(roots.project / "src" / "app.py", "needle in the project\n")
        seed(roots.project / "docs" / "notes.md", "needle in the docs\n")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(search_in_files({"query": "needle", "pattern": "src/*.py"}, ctx))

        assert [match["file"] for match in result["data"]["results"]] == ["src/app.py"]
        assert "in the project" in result["data"]["result"]
        assert "in the instance" not in result["data"]["result"]

    def test_search_replace_changes_the_project_copy(
        self, roots: SimpleNamespace, locks: list[tuple[Path, Path]],
    ) -> None:
        bystander = seed(roots.instance / "notes" / "todo.txt", "needle\n")
        target = seed(roots.project / "notes" / "todo.txt", "needle\n")
        ctx = _Ctx(roots.instance, roots.project)

        result = run(search_in_files({"query": "needle", "mode": "replace", "replace": "pin", "path": "notes"}, ctx))

        assert result["ok"] is True, result
        assert [entry["file"] for entry in result["data"]["results"]] == ["notes/todo.txt"]
        assert target.read_bytes() == b"pin\n"
        assert bystander.read_bytes() == b"needle\n"
        assert locks == [(target.resolve(), roots.instance)]
        [task] = EditJournal(roots.instance).tasks(SESSION)
        assert [Path(entry.path) for entry in task.entries] == [target.resolve()]
        assert not (roots.project / "data").exists()


class TestCommandsRunInTheProjectRoot:
    def test_execute_command_runs_in_the_project_root(self, roots: SimpleNamespace) -> None:
        ctx = _Ctx(roots.instance, roots.project)
        command = shell(sys.executable, "-c", "import os; print(os.getcwd())")

        result = run(execute_command({"command": command, "timeout_sec": 60}, ctx))

        assert result["ok"] is True
        assert os.path.samefile(result["data"]["output"].strip(), roots.project)

    def test_without_a_project_root_it_runs_in_the_workspace(self, roots: SimpleNamespace) -> None:
        ctx = _Ctx(roots.instance)
        command = shell(sys.executable, "-c", "import os; print(os.getcwd())")

        result = run(execute_command({"command": command, "timeout_sec": 60}, ctx))

        assert os.path.samefile(result["data"]["output"].strip(), roots.instance)

    def test_its_settings_still_come_from_the_instance(self, roots: SimpleNamespace) -> None:
        use_runtime(roots.instance, {"meta": {"execute_command": {"max_output_chars": 1500}}})
        use_runtime(roots.project, {"meta": {"execute_command": {"max_output_chars": 0}}})
        ctx = _Ctx(roots.instance, roots.project)
        command = shell(sys.executable, "-c", "print('x' * 10000)")

        result = run(execute_command({"command": command, "timeout_sec": 60}, ctx))

        assert len(result["data"]["output"]) <= 1500
        assert result["data"]["truncated"] is True

    def test_edit_checks_run_in_the_project_root_with_a_project_relative_path(self, roots: SimpleNamespace) -> None:
        code = "import os,sys; print(os.getcwd()); sys.exit(0 if os.path.isfile(sys.argv[1]) else 3)"
        use_runtime(roots.instance, {"edit_checks": {"commands": [
            {"glob": "*.py", "command": python_command(code) + " {path}", "timeout_sec": 60},
        ]}})
        use_runtime(roots.project, {"edit_checks": {"commands": [{"glob": "*.py", "command": "decoy-check {path}"}]}})
        ctx = _Ctx(roots.instance, roots.project)

        result = run(write_file({"path": "src/new.py", "content": "x = 1\n"}, ctx))

        [check] = result["data"]["externalChecks"]
        assert check["status"] == "passed", check
        assert os.path.samefile(check["output"].strip(), roots.project)
        [(op, parameters)] = [(op, params) for op, params in ctx.requested if op == "execute_command"]
        assert parameters["command"] == python_command(code) + " " + syntax_check.quote_path("src/new.py")


def _exercise(ctx: _Ctx, project: Path) -> dict[str, Any]:
    """同一串调用：读、改、写、按行改、列目录、glob、搜索、替换、撤销、跑命令。"""
    seed(project / "src" / "app.py", "value = 1\n")
    seed(project / "notes" / "todo.txt", "needle here\n")
    outcomes: dict[str, Any] = {}
    outcomes["read"] = run(read_file({"files": [{"path": "src/app.py"}]}, ctx))
    outcomes["diff"] = run(apply_diff({"path": "src/app.py", "diffs": [{"search": "value = 1", "replace": "value = 2"}]}, ctx))
    outcomes["write"] = run(write_file({"path": "src/new.py", "content": "x = 1\n"}, ctx))
    outcomes["lines"] = run(edit_lines({
        "path": "src/new.py",
        "edits": [{"op": "replace", "start": first_line_anchor(project / "src" / "new.py"), "content": "x = 2"}],
    }, ctx))
    outcomes["list"] = run(list_dir({"paths": ["src"]}, ctx))
    outcomes["glob"] = run(glob_files({"pattern": "*.py", "path": "src"}, ctx))
    outcomes["search"] = run(search_in_files({"query": "needle", "path": "notes"}, ctx))
    outcomes["replace"] = run(search_in_files({"query": "needle", "mode": "replace", "replace": "pin", "path": "notes"}, ctx))
    outcomes["undo"] = run(undo_edits({"paths": ["src/app.py"]}, ctx))
    outcomes["command"] = run(execute_command({"command": shell(sys.executable, "-c", "print(1)"), "timeout_sec": 60}, ctx))
    return outcomes


class TestApprovalParametersDoNotChange:
    def test_every_request_names_paths_the_way_it_did_without_a_project_root(self, tmp_path: Path) -> None:
        code = "import os,sys; sys.exit(0 if os.path.isfile(sys.argv[1]) else 3)"
        checks = {"edit_checks": {"commands": [{"glob": "*.py", "command": python_command(code) + " {path}"}]}}
        alone = tmp_path / "alone"
        instance = tmp_path / "instance"
        project = tmp_path / "project"
        for root in (alone, instance, project):
            root.mkdir()
        use_runtime(alone, checks)
        use_runtime(instance, checks)

        baseline = _Ctx(alone)
        split = _Ctx(instance, project)
        before = _exercise(baseline, alone)
        after = _exercise(split, project)

        assert all(outcome.get("ok") is not False for outcome in before.values()), before
        assert all(outcome.get("ok") is not False for outcome in after.values()), after
        assert split.requested == baseline.requested
        assert split.evaluated == baseline.evaluated
        assert [op for op, _params in split.requested].count("execute_command") == 4
        # 只比命中了哪些：按修改时间排的先后取决于文件系统的时间精度
        assert sorted(after["glob"]["data"]["files"]) == sorted(before["glob"]["data"]["files"]) == [
            "src/app.py", "src/new.py",
        ]
        assert after["search"]["data"]["results"] == before["search"]["data"]["results"]
        assert after["write"]["data"]["externalChecks"][0]["status"] == "passed"
        assert (project / "src" / "app.py").read_bytes() == b"value = 1\n"
        assert (project / "notes" / "todo.txt").read_bytes() == b"pin here\n"
        assert not (project / "data").exists()
        assert (instance / "data" / "edit_journal").is_dir()


class _Verdicts:
    """顶替 ToolContext.evaluate_op：read_file 一律 auto，记下问了哪些路径、用的哪个上下文。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.asked: list[str] = []
        self.contexts: list[ToolContext] = []
        verdicts = self

        async def evaluate_op(ctx: ToolContext, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
            verdicts.asked.append(parameters["path"])
            verdicts.contexts.append(ctx)
            return {"safety_level": "auto", "reason": "decided by the test"}

        monkeypatch.setattr(ToolContext, "evaluate_op", evaluate_op)


class TestProjectInstructions:
    @pytest.fixture(autouse=True)
    def _fresh_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(project_instructions, "_TASK_STATES", OrderedDict())

    def test_the_walk_happens_in_the_project_root(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "AGENTS.md", "instance rules")
        seed(roots.instance / "pkg" / "CLAUDE.md", "instance pkg rules")
        seed(roots.project / "AGENTS.md", "project rules")
        seed(roots.project / "pkg" / "CLAUDE.md", "pkg rules")

        direct, candidates = discover(roots.instance, [("pkg/mod.py", True)], set(), project_root=roots.project)

        assert direct == []
        assert [(item.display, item.directory) for item in candidates] == [("AGENTS.md", ""), ("pkg/CLAUDE.md", "pkg")]
        assert [item.path for item in candidates] == [roots.project / "AGENTS.md", roots.project / "pkg" / "CLAUDE.md"]

    def test_the_walk_stops_at_the_project_root(self, roots: SimpleNamespace) -> None:
        nested = roots.instance / "checkout"
        seed(roots.instance / "AGENTS.md", "above the project")
        seed(nested / "sub" / "CLAUDE.md", "sub rules")

        _direct, candidates = discover(roots.instance, [("sub/x.py", True)], set(), project_root=nested)

        assert [item.display for item in candidates] == ["sub/CLAUDE.md"]

    def test_workspace_files_outside_the_project_find_nothing(self, roots: SimpleNamespace) -> None:
        seed(roots.instance / "AGENTS.md", "instance rules")
        target = seed(roots.instance / "src" / "x.py", "x")

        assert discover(roots.instance, [(str(target), True)], set(), project_root=roots.project) == ([], [])

    def test_extra_roots_still_come_from_the_instance_policy(self, roots: SimpleNamespace, tmp_path: Path) -> None:
        extra = tmp_path / "code"
        use_policy(roots.instance, {"extra_roots": [str(extra)]})
        seed(extra / "AGENTS.md", "code root")

        _direct, candidates = discover(
            roots.instance, [(str(extra / "lib" / "mod.py"), True)], set(), project_root=roots.project,
        )

        assert [item.display for item in candidates] == [(extra / "AGENTS.md").resolve().as_posix()]

    def test_the_loop_loads_the_project_copy_and_asks_with_its_project_name(
        self, roots: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed(roots.instance / "AGENTS.md", "instance rules")
        seed(roots.project / "AGENTS.md", "project rules")
        verdicts = _Verdicts(monkeypatch)
        handler = ProjectInstructions()
        rctx = make_rctx(roots.instance)
        rctx.project_root = roots.project
        node = Node(id="node-1", type="ai", prompt="Work.", tool_mode="native", tool_access=ToolAccess(mode="all"))
        registry = ScriptedRegistry(tools={"read_file": echo("file text")})
        node_run = NodeRun(
            monkeypatch, roots.instance, registry=registry, node=node, rctx=rctx,
            runtime_cfg={"engine": {"max_steps": 8}},
            handlers={"after_tool_call": [handler.collect], "before_step": [handler.inject]},
        )

        node_run.run(
            turn(call("read_file", {"files": [{"path": "pkg/mod.py"}]}, call_id="r1")),
            turn(call("finish", {"text": "done"}, call_id="f")),
        )

        [message] = [item for item in node_run.requests[1]["messages"] if item.get(MESSAGE_KEY)]
        assert "project rules" in message["content"]
        assert "instance rules" not in message["content"]
        assert verdicts.asked == ["AGENTS.md"]
        assert [ctx.project_root for ctx in verdicts.contexts] == [roots.project]
        [(name, _arguments, tool_ctx)] = registry.executed
        assert name == "read_file" and tool_ctx.project_root == roots.project


class _Guard:
    """顶替 attachment_access 里的 request_guard：一律放行，记下送审参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, ctx: Any, op: str, parameters: dict[str, Any]) -> tuple[dict[str, Any], None]:
        self.calls.append({"op": op, **parameters})
        return {}, None

    @property
    def paths(self) -> list[str]:
        return [item["path"] for item in self.calls]


class TestModelNamedAttachments:
    @pytest.fixture
    def guard(self, monkeypatch: pytest.MonkeyPatch) -> _Guard:
        stub = _Guard()
        monkeypatch.setattr(attachment_access, "request_guard", stub)
        return stub

    @staticmethod
    def _screen(
        roots: SimpleNamespace, paths: list[str], *, split: bool = True,
        collected: list[dict[str, Any]] | None = None, data_dir_only: bool = False,
    ) -> tuple[list[str], list[RefusedAttachment]]:
        rctx = make_rctx(roots.instance)
        if split:
            rctx.project_root = roots.project
        ls = make_loop_state(roots.instance, ScriptedRegistry(), rctx=rctx)
        ls.collected_attachments.extend(collected or [])
        pseudo_call = SimpleNamespace(name="dispatch_to_scout" if data_dir_only else "finish", id="call-1")
        return run(screen_model_attachment_paths(ls, pseudo_call, paths, data_dir_only=data_dir_only))

    def test_a_relative_path_names_the_project_file(self, roots: SimpleNamespace, guard: _Guard) -> None:
        seed(roots.instance / "out" / "chart.png", "instance chart")
        chart = seed(roots.project / "out" / "chart.png", "project chart")

        kept, refused = self._screen(roots, ["out/chart.png"])

        # 下游按工作区根解析相对路径，交出去的是项目里那份的绝对路径
        assert kept == [chart.resolve().as_posix()]
        assert refused == []
        assert guard.calls == [{
            "op": "read_file", "path": "out/chart.png", "tool_name": "finish", "reason": "finish attaching out/chart.png",
        }]

    def test_without_a_project_root_the_path_is_handed_on_as_written(self, roots: SimpleNamespace, guard: _Guard) -> None:
        seed(roots.instance / "out" / "chart.png", "instance chart")

        kept, refused = self._screen(roots, ["out/chart.png"], split=False)

        assert (kept, refused) == (["out/chart.png"], [])
        assert guard.paths == ["out/chart.png"]

    def test_a_file_only_in_the_instance_is_not_a_project_file(self, roots: SimpleNamespace, guard: _Guard) -> None:
        seed(roots.instance / "notes" / "plan.md", "x")

        kept, refused = self._screen(roots, ["notes/plan.md"])

        assert kept == []
        assert refused == [RefusedAttachment("notes/plan.md", _MISSING_REASON)]
        assert guard.calls == []

    def test_handing_data_to_a_child_still_reads_the_instance(self, roots: SimpleNamespace, guard: _Guard) -> None:
        seed(roots.instance / "data" / "reports" / "q3.md", "x")

        kept, refused = self._screen(roots, ["data/reports/q3.md"], data_dir_only=True)

        assert (kept, refused) == (["data/reports/q3.md"], [])
        assert guard.paths == ["data/reports/q3.md"]

    def test_collected_attachments_are_unchanged(self, roots: SimpleNamespace, guard: _Guard) -> None:
        seed(roots.instance / "data" / "a.png", "received")
        seed(roots.project / "data" / "a.png", "a project file of the same name")
        collected = [{"path": "data/a.png"}]

        kept, refused = self._screen(roots, ["data/a.png", "./data/../data/a.png"], collected=collected)

        assert (kept, refused) == (["data/a.png", "./data/../data/a.png"], [])
        assert guard.calls == []


class _Response:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"ok": True, "cancelled": False}


class _Http:
    async def post(self, url: str, **_kwargs: Any) -> _Response:
        return _Response()

    async def get(self, url: str, **_kwargs: Any) -> _Response:
        return _Response()


class _CapturingRegistry:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.contexts.append(ctx)
        return {"ok": True, "data": {"result": "done"}}

    def get_spec(self, name: str) -> None:
        return None


class TestTheRunnerReadsTheKey:
    @staticmethod
    def _origin_node(root: Path) -> None:
        nodes = root / "config" / "nodes"
        nodes.mkdir(parents=True, exist_ok=True)
        data = {"id": "origin", "type": "ai", "prompt": "test", "tool_access": {"mode": "all"}}
        (nodes / "origin.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def _tool_task(self, root: Path, task_input: dict[str, Any]) -> Any:
        self._origin_node(root)
        registry = _CapturingRegistry()
        task = {"task_id": "task-1", "tool_name": "demo", "input": {
            "origin_node_id": "origin", "arguments": {}, "task_context": {}, **task_input,
        }}
        run(runner._run_tool_task(
            http=_Http(), sup_url="http://sup", ws_root=root, registry=registry, task=task,  # type: ignore[arg-type]
            worker_id="worker-1", session_id="session", session_generation=1, task_id="task-1",
        ))
        [ctx] = registry.contexts
        return ctx

    def test_a_tool_task_gets_the_project_root_from_its_input(self, roots: SimpleNamespace) -> None:
        ctx = self._tool_task(roots.instance, {PROJECT_ROOT_KEY: str(roots.project)})

        assert (ctx.workspace_root, ctx.project_root) == (roots.instance, roots.project)

    def test_a_tool_task_without_the_key_uses_the_workspace_root(self, roots: SimpleNamespace) -> None:
        ctx = self._tool_task(roots.instance, {})

        assert ctx.project_root == roots.instance

    def test_a_tool_task_with_a_bad_value_falls_back_and_warns(
        self, roots: SimpleNamespace, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_KEY_LOGGER):
            ctx = self._tool_task(roots.instance, {PROJECT_ROOT_KEY: "relative/checkout"})

        assert ctx.project_root == roots.instance
        assert any("task-1" in record.getMessage() for record in caplog.records)

    @staticmethod
    def _node_task(root: Path, monkeypatch: pytest.MonkeyPatch, task_input: dict[str, Any]) -> RunContext:
        seen: list[RunContext] = []
        provider = SimpleNamespace(model="main-model", name="deepseek")
        monkeypatch.setattr(runner, "load_node", lambda *_args: Node(id="entry", type="ai", prompt="test"))
        monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
        monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *_args, **_kwargs: provider)
        monkeypatch.setattr(runner, "install_event_bridge", lambda *_args, **_kwargs: None)

        async def run_ai_node(**kwargs: Any) -> TaskAction:
            seen.append(kwargs["rctx"])
            return TaskAction(action="finish", node_id="entry", result={"text": "done"})

        monkeypatch.setattr(runner, "run_ai_node", run_ai_node)
        task = {"task_id": "task-1", "node_id": "entry", "input": {"instruction": "hello", **task_input}}

        async def go() -> None:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
            async with httpx.AsyncClient(transport=transport) as http:
                await runner._run_node_task(
                    http=http, llm_http=http, sup_url="http://local", ws_root=root,
                    registry=ToolRegistry(workspace_root=root, tools_dir=root / "tools"),
                    task=task, worker_id="worker", session_id="parent", session_generation=1,
                    api_key="test", base_url="http://model.invalid", default_model="test",
                )

        run(go())
        [rctx] = seen
        return rctx

    def test_a_node_task_gets_the_project_root_from_its_input(
        self, roots: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rctx = self._node_task(roots.instance, monkeypatch, {PROJECT_ROOT_KEY: str(roots.project)})

        assert (rctx.workspace_root, rctx.project_root) == (roots.instance, roots.project)
        # 实例数据照旧落在实例根
        assert (roots.instance / "data").is_dir()
        assert not (roots.project / "data").exists()

    def test_a_node_task_without_the_key_uses_the_workspace_root(
        self, roots: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rctx = self._node_task(roots.instance, monkeypatch, {})

        assert rctx.project_root == roots.instance
