"""glob：按文件名或路径模式找文件，新改的排前面；认 .gitignore、跳过依赖和构建目录，对搜索根报 read_file 判定。"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.context import ToolContext  # noqa: E402
from toolbox.ignore_rules import GlobError  # noqa: E402

# 包的 __init__ 把 glob_files 这个名字换成了函数，模块得从 sys.modules 里拿
glob_module = importlib.import_module("toolbox.builtins.glob_files")


class _Ctx(ToolContext):
    def __init__(self, workspace_root: Path, *, approval: str = "allowed", **decisions: str) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-glob",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id="task-1",
        )
        self.decisions = dict(decisions)
        self.approval = approval
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.approvals_waited = 0
        self.cancelled = False

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "approval_id": "ap-1", "reason": f"{op} by the test"}

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        self.approvals_waited += 1
        return {"status": self.approval}

    async def check_cancelled(self) -> bool:
        return self.cancelled


def _touch(path: Path, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _glob(ctx: _Ctx, **args: Any) -> dict[str, Any]:
    return asyncio.run(glob_module.glob_files(args, ctx))


class TestOrdering:
    def test_newest_first(self, tmp_path: Path) -> None:
        _touch(tmp_path / "old.py", mtime=1_700_000_000)
        _touch(tmp_path / "pkg" / "newest.py", mtime=1_700_000_300)
        _touch(tmp_path / "middle.py", mtime=1_700_000_100)

        result = _glob(_Ctx(tmp_path), pattern="*.py")

        assert result["ok"] is True
        assert result["data"]["files"] == ["pkg/newest.py", "middle.py", "old.py"]
        assert result["data"]["result"].splitlines() == [
            "3 files match '*.py' under ., newest first:", "pkg/newest.py", "middle.py", "old.py",
        ]

    def test_equal_times_keep_the_walk_order(self, tmp_path: Path) -> None:
        for rel in ("b.py", "a.py", "sub/c.py"):
            _touch(tmp_path / rel, mtime=1_700_000_000)

        result = _glob(_Ctx(tmp_path), pattern="*.py")

        assert result["data"]["files"] == ["a.py", "b.py", "sub/c.py"]


class TestLimits:
    def test_max_results_caps_what_is_shown_but_not_the_total(self, tmp_path: Path) -> None:
        for index in range(5):
            _touch(tmp_path / f"f{index}.py", mtime=1_700_000_000 + index)

        result = _glob(_Ctx(tmp_path), pattern="*.py", max_results=2)

        data = result["data"]
        assert data["files"] == ["f4.py", "f3.py"]
        assert (data["count"], data["total"], data["truncated"]) == (2, 5, True)
        assert "showing the first 2" in data["result"]

    def test_the_camel_case_spelling_works_too(self, tmp_path: Path) -> None:
        for index in range(3):
            _touch(tmp_path / f"f{index}.py")

        assert _glob(_Ctx(tmp_path), pattern="*.py", maxResults=1)["data"]["count"] == 1

    @pytest.mark.parametrize("raw,expected", [
        (None, glob_module.DEFAULT_MAX_RESULTS),
        ("", glob_module.DEFAULT_MAX_RESULTS),
        (True, glob_module.DEFAULT_MAX_RESULTS),
        (0, glob_module.DEFAULT_MAX_RESULTS),
        (-3, glob_module.DEFAULT_MAX_RESULTS),
        ("7", 7),
        (10**9, glob_module.MAX_RESULTS_LIMIT),
    ])
    def test_max_results_is_normalised(self, raw: Any, expected: int) -> None:
        assert glob_module._max_results(raw) == expected

    def test_a_non_numeric_max_results_is_an_error_before_any_approval(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path)

        result = _glob(ctx, pattern="*.py", max_results="many")

        assert result["ok"] is False
        assert ctx.requested == []

    def test_the_walk_stops_at_its_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(glob_module, "MAX_WALK_FILES", 3)
        for index in range(5):
            _touch(tmp_path / f"f{index}.py")

        result = _glob(_Ctx(tmp_path), pattern="*.py")

        assert result["data"]["total"] == 3
        assert result["data"]["scanLimited"] is True
        assert "走到 3 个文件就停了" in result["data"]["result"]


class TestWhatIsSkipped:
    def test_gitignore_and_default_ignored_directories_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_bytes(b"*.gen.py\nsecret/\n")
        for rel in ("src/app.py", "src/app.gen.py", "secret/key.py", "node_modules/pkg/index.py",
                    "build/out.py", ".venv/lib/site.py", "src/__pycache__/app.py"):
            _touch(tmp_path / rel)

        result = _glob(_Ctx(tmp_path), pattern="**/*.py")

        assert result["data"]["files"] == ["src/app.py"]

    def test_gitignore_has_no_effect_outside_a_work_tree(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_bytes(b"*.gen.py\n")
        _touch(tmp_path / "app.gen.py")

        assert _glob(_Ctx(tmp_path), pattern="*.py")["data"]["files"] == ["app.gen.py"]

    def test_pointing_path_into_an_ignored_directory_looks_inside(self, tmp_path: Path) -> None:
        _touch(tmp_path / "node_modules" / "pkg" / "index.js")

        result = _glob(_Ctx(tmp_path), pattern="*.js", path="node_modules")

        assert result["data"]["files"] == ["node_modules/pkg/index.js"]

    def test_no_match_explains_what_was_left_out(self, tmp_path: Path) -> None:
        _touch(tmp_path / "node_modules" / "index.js")

        result = _glob(_Ctx(tmp_path), pattern="*.js")

        assert result["ok"] is True
        assert result["data"]["files"] == []
        assert ".gitignore" in result["data"]["result"]

    def test_symbolic_links_are_not_followed(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        _touch(target / "hidden.py")
        workspace = tmp_path / "ws"
        _touch(workspace / "real.py")
        try:
            os.symlink(target, workspace / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("这台机器上建不了软链接")

        assert _glob(_Ctx(workspace), pattern="**/*.py")["data"]["files"] == ["real.py"]


class TestPatterns:
    @pytest.mark.parametrize("pattern,expected", [
        ("*.py", ["a.py", "src/b.py", "src/deep/c.py"]),
        ("src/*.py", ["src/b.py"]),
        ("src/**/*.py", ["src/b.py", "src/deep/c.py"]),
        ("**/c.py", ["src/deep/c.py"]),
        ("./src/*.py", ["src/b.py"]),
        ("src\\*.py", ["src/b.py"]),
        ("src/", ["src/b.py", "src/deep/c.py", "src/notes.md"]),
        ("*.{py,md}", ["a.py", "src/b.py", "src/deep/c.py", "src/notes.md"]),
        ("[ab].py", ["a.py", "src/b.py"]),
        ("?.py", ["a.py", "src/b.py", "src/deep/c.py"]),
    ])
    def test_pattern_forms(self, tmp_path: Path, pattern: str, expected: list[str]) -> None:
        for rel in ("a.py", "src/b.py", "src/deep/c.py", "src/notes.md"):
            _touch(tmp_path / rel, mtime=1_700_000_000)

        result = _glob(_Ctx(tmp_path), pattern=pattern)

        assert sorted(result["data"]["files"]) == expected

    def test_the_pattern_is_relative_to_path(self, tmp_path: Path) -> None:
        _touch(tmp_path / "src" / "deep" / "c.py")

        result = _glob(_Ctx(tmp_path), pattern="deep/*.py", path="src")

        assert result["data"]["files"] == ["src/deep/c.py"]

    @pytest.mark.parametrize("pattern", ["", "   ", "[unclosed", "{a,b", "/"])
    def test_a_bad_or_empty_pattern_is_refused_before_any_approval(self, tmp_path: Path, pattern: str) -> None:
        ctx = _Ctx(tmp_path)

        result = _glob(ctx, pattern=pattern)

        assert result["ok"] is False
        assert ctx.requested == []

    def test_the_matcher_raises_on_a_broken_glob(self) -> None:
        with pytest.raises(GlobError):
            glob_module.pattern_matcher("[oops")


class TestPolicy:
    def test_a_workspace_path_is_judged_like_read_file(self, tmp_path: Path) -> None:
        _touch(tmp_path / "src" / "a.py")
        ctx = _Ctx(tmp_path)

        _glob(ctx, pattern="*.py", path="src")

        assert ctx.requested == [("read_file", {"path": "src", "glob": "*.py"})]

    def test_a_denied_read_returns_nothing(self, tmp_path: Path) -> None:
        _touch(tmp_path / "data" / "secret.py")

        result = _glob(_Ctx(tmp_path, read_file="deny"), pattern="*.py", path="data")

        assert result["ok"] is False
        assert result["data"]["files"] == []
        assert "secret.py" not in result["data"]["result"]

    def test_an_approved_read_goes_ahead(self, tmp_path: Path) -> None:
        _touch(tmp_path / "data" / "a.py")
        ctx = _Ctx(tmp_path, read_file="approval_required")

        result = _glob(ctx, pattern="*.py", path="data")

        assert ctx.approvals_waited == 1
        assert result["data"]["files"] == ["data/a.py"]

    def test_a_rejected_approval_returns_nothing(self, tmp_path: Path) -> None:
        _touch(tmp_path / "data" / "a.py")

        result = _glob(_Ctx(tmp_path, read_file="approval_required", approval="denied"), pattern="*.py", path="data")

        assert result["ok"] is False
        assert result["data"]["files"] == []

    def test_a_cancelled_task_is_reported_as_cancelled(self, tmp_path: Path) -> None:
        _touch(tmp_path / "a.py")
        ctx = _Ctx(tmp_path)
        ctx.cancelled = True

        result = _glob(ctx, pattern="*.py")

        assert result["ok"] is False
        assert result["cancelled"] is True

    def test_an_external_path_goes_through_the_external_read_approval(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        _touch(outside / "x.py")
        ctx = _Ctx(workspace)

        result = _glob(ctx, pattern="*.py", path=str(outside))

        assert [op for op, _params in ctx.requested] == ["read_file"]
        assert ctx.requested[0][1]["tool_name"] == "glob"
        assert result["data"]["files"] == [(outside / "x.py").resolve().as_posix()]

    def test_the_check_comes_before_existence_is_revealed(self, tmp_path: Path) -> None:
        result = _glob(_Ctx(tmp_path, read_file="deny"), pattern="*.py", path="missing")

        assert result["ok"] is False
        assert result["error"] != "path not found"

    def test_a_missing_directory_and_a_file_are_errors(self, tmp_path: Path) -> None:
        _touch(tmp_path / "a.py")

        assert _glob(_Ctx(tmp_path), pattern="*.py", path="missing")["error"] == "path not found"
        assert _glob(_Ctx(tmp_path), pattern="*.py", path="a.py")["error"] == "Not a directory"
