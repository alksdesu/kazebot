"""search_in_files：rg 和 Python 两条路给出同样的结果，都认 .gitignore；扫描只按真读了的文件计量，遍历本身也有上限。

replace 模式在文件锁里比戳、记编辑日志、原子写入，写完记新戳；节点关掉 search_replace 时在任何送审之前就拒。
rg 在测试里用一个照参数挑文件的假 rg 顶替；装了真 rg 的机器上再拿真 rg 对一遍。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _file_edits_fake import FakeFileEdits, FakeSupervisor  # noqa: E402
from toolbox import edit_journal, file_state, ignore_rules  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.read_file import read_file  # noqa: E402
from toolbox.builtins.undo_edits import undo_edits  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

# 包的 __init__ 把 search_in_files 这个名字换成了函数，模块得从 sys.modules 里拿
sif = importlib.import_module("toolbox.builtins.search_in_files")
_collect_files = sif._collect_files
_match_pattern = sif._match_pattern

_BOM = b"\xef\xbb\xbf"


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：按 op 名给出预设的 safety_level，没写的一律 auto；记下每次送审。认领和读戳交给 FakeSupervisor。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        session: str = "sess-search",
        task: str = "task-1",
        node_extra: dict[str, Any] | None = None,
        **decisions: str,
    ) -> None:
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
        if node_extra is not None:
            self._node_extra = node_extra

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "approval_id": "ap-1", "reason": f"{op} by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} by the test"}

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        return {"status": "allowed"}

    async def check_cancelled(self) -> bool:
        return self.cancelled


def _write(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 一律按字节写：write_text 在 Windows 上会把 \n 换成 \r\n
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def _runtime(workspace: Path, **search: Any) -> None:
    path = config_layout.runtime_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"meta": {"search": search}}), encoding="utf-8")


def _search(ctx: _Ctx, **args: Any) -> dict[str, Any]:
    return asyncio.run(sif.search_in_files(args, ctx))


def _hits(result: dict[str, Any]) -> list[tuple[str, int, int]]:
    return [(m["file"], m["line"], m["column"]) for m in result["data"]["results"]]


def _tree(root: Path) -> Path:
    """一个 git 工作区：该搜的、被 .gitignore 挡的、跳过名单里的、二进制的、太大的各有一份，都含 needle。"""
    _write(root / ".git" / "HEAD", "needle\n")
    _write(root / ".gitignore", "*.tmp\nsecret/\n")
    _write(root / "src" / "a.py", "x = 'needle'\n")
    _write(root / "src" / "b.py", "NEEDLE\nother\n")
    _write(root / "src" / "sub" / "c.py", "first\nneedle needle\nlast\n")
    _write(root / "src" / "notes.txt", "a needle in text\n")
    _write(root / "src" / "crlf.py", b"one\r\nneedle\r\n")
    _write(root / "src" / "bom.py", _BOM + b"needle\n")
    _write(root / "src" / "cache.tmp", "needle\n")
    _write(root / "src" / "app.log", "needle\n")
    _write(root / "src" / "bundle.min.js", "needle\n")
    _write(root / "src" / "blob.py", b"needle\x00binary")
    _write(root / "src" / "huge.py", "needle\n" + "x" * 150_000)
    _write(root / "secret" / "s.py", "needle\n")
    _write(root / "node_modules" / "m.py", "needle\n")
    _write(root / "dist.backup-1" / "x.py", "needle\n")
    _write(root / "logs" / "app.py", "needle\n")
    _write(root / ".hidden" / "h.py", "needle\n")
    _runtime(root, max_file_size_bytes=100_000)
    return root


_DEFAULT_HITS = [
    (".hidden/h.py", 1, 1),
    ("src/a.py", 1, 6),
    ("src/b.py", 1, 1),
    ("src/bom.py", 1, 1),
    ("src/crlf.py", 2, 1),
    ("src/notes.txt", 1, 3),
    ("src/sub/c.py", 2, 1),
    ("src/sub/c.py", 2, 8),
]


class _FakeRipgrep:
    """顶替 rg：照 argv 挑出有命中的文件。故意不管 --glob 和 --type、把顺序倒过来，
    结果得靠 _ripgrep_survivor 和排序收拾成和 Python 那一路一模一样。"""

    def __init__(
        self,
        *,
        returncode: int | None = None,
        stderr: bytes = b"",
        timed_out: bool = False,
        overflow: bool = False,
        cancelled: bool = False,
        cut_last: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.overflow = overflow
        self.cancelled = cancelled
        self.cut_last = cut_last
        self.calls: list[list[str]] = []

    def files_for(self, argv: list[str]) -> list[str]:
        root = Path(argv[argv.index("--") + 1])
        query = argv[argv.index("--regexp") + 1]
        flags = re.MULTILINE | (re.IGNORECASE if "--ignore-case" in argv else 0)
        regex = re.compile(re.escape(query) if "--fixed-strings" in argv else query, flags)
        max_size = int(argv[argv.index("--max-filesize") + 1])
        found: list[str] = []
        for entry, _rel in ignore_rules.walk_files(root, gitignore="--no-ignore-vcs" not in argv):
            data = Path(entry.path).read_bytes()
            if len(data) > max_size or b"\x00" in data:
                continue
            if regex.search(data.decode("utf-8", "ignore").replace("\r\n", "\n")):
                found.append(entry.path)
        found.reverse()
        return found

    async def __call__(self, argv: list[str], cwd: Path, ctx: Any) -> Any:
        self.calls.append(list(argv))
        if self.cancelled:
            return sif._RipgrepRun(None, b"", b"", cancelled=True)
        found = self.files_for(argv)
        stdout = b"".join(os.fsencode(path) + b"\x00" for path in found)
        if self.cut_last and stdout:
            stdout = stdout[:-1] + b"/half/written"
        code = self.returncode if self.returncode is not None else (0 if found else 1)
        return sif._RipgrepRun(code, stdout, self.stderr, self.overflow, self.timed_out)


@pytest.fixture
def python_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sif, "_ripgrep_binary", lambda: None)


@pytest.fixture
def fake_rg(monkeypatch: pytest.MonkeyPatch) -> _FakeRipgrep:
    fake = _FakeRipgrep()
    monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
    monkeypatch.setattr(sif, "_run_ripgrep", fake)
    return fake


def _both(monkeypatch: pytest.MonkeyPatch, ctx: _Ctx, fake: _FakeRipgrep | None = None, **args: Any) -> tuple[dict, dict]:
    """同一组参数先走假 rg、再走 Python，返回 (rg 的结果, Python 的结果)。"""
    fake = fake or _FakeRipgrep()
    monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
    monkeypatch.setattr(sif, "_run_ripgrep", fake)
    via_rg = _search(ctx, **args)
    monkeypatch.setattr(sif, "_ripgrep_binary", lambda: None)
    via_python = _search(ctx, **args)
    return via_rg, via_python


class TestPatternMatching:
    def test_glob_matches_the_full_relative_path(self) -> None:
        assert _match_pattern("src/pkg/main.py", "src/**/*.py")
        assert not _match_pattern("tests/pkg/main.py", "src/**/*.py")
        assert _match_pattern("src/pkg/main.py", "**/*.py")

    def test_a_double_star_also_stands_for_no_directory_at_all(self) -> None:
        assert _match_pattern("src/main.py", "src/**/*.py")
        assert _match_pattern("src/a/b/main.py", "src/**/*.py")
        assert _match_pattern("main.py", "**/main.py")
        assert not _match_pattern("lib/main.py", "src/**/*.py")

    def test_a_pattern_without_slash_matches_the_file_name_at_any_depth(self) -> None:
        assert _match_pattern("engine/inference/ai_step.py", "*.py")
        assert _match_pattern("engine/inference/ai_step.py", "ai_*.py")
        assert not _match_pattern("engine/inference/ai_step.py", "*.js")

    def test_the_pattern_is_matched_against_the_workspace_relative_path(self) -> None:
        # path 指到 engine 时显示路径仍是 engine/...，pattern 也得按工作区写
        assert _match_pattern("engine/inference/ai_step.py", "engine/**/*.py")
        assert not _match_pattern("engine/inference/ai_step.py", "inference/*.py")


class TestTheCollector:
    def test_it_prunes_backups_databases_and_build_artifacts(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "dist.backup-2026").mkdir()
        (tmp_path / ".cache").mkdir()
        _write(tmp_path / "src" / "main.py", "needle")
        _write(tmp_path / "src" / "state.sqlite3", "needle")
        _write(tmp_path / "src" / "bundle.min.js", "needle")
        _write(tmp_path / "dist.backup-2026" / "old.py", "needle")
        _write(tmp_path / ".cache" / "old.py", "needle")

        files = list(_collect_files(tmp_path, "**/*.py", 100_000, tmp_path))

        assert [rel for _, rel in files] == ["src/main.py"]

    def test_the_walk_stops_at_its_cap_with_a_reason(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sif, "MAX_WALK_FILES", 3)
        for index in range(5):
            _write(tmp_path / f"f{index}.txt", "x")

        collector = _collect_files(tmp_path, "*.py", 100_000, tmp_path)

        assert list(collector) == []
        assert collector.limited == "遍历了 3 个文件就停了"

    def test_a_walk_within_the_cap_reports_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sif, "MAX_WALK_FILES", 3)
        for index in range(3):
            _write(tmp_path / f"f{index}.py", "x")

        collector = _collect_files(tmp_path, "*.py", 100_000, tmp_path)

        assert len(list(collector)) == 3
        assert collector.limited == ""


class TestScanLimits:
    def test_a_large_file_that_is_never_read_does_not_end_the_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None,
    ) -> None:
        # 排在前面、和 pattern 对不上的大文件，和超过大小上限的文件，都不读，也就不该算进读取字节数
        monkeypatch.setattr(sif, "_MAX_SCAN_BYTES", 1000)
        _write(tmp_path / "a_media.mp4", b"m" * 50_000)
        _write(tmp_path / "a_oversized.py", "needle\n" + "x" * 150_000)
        _write(tmp_path / "b.py", "needle\n")
        _runtime(tmp_path, max_file_size_bytes=100_000)

        result = _search(_Ctx(tmp_path), query="needle", pattern="*.py")

        assert _hits(result) == [("b.py", 1, 1)]
        assert "scanLimited" not in result["data"]

    def test_reading_stops_at_the_byte_cap_when_more_candidates_remain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None,
    ) -> None:
        monkeypatch.setattr(sif, "_MAX_SCAN_BYTES", 10)
        _write(tmp_path / "a.py", "needle " + "x" * 20 + "\n")
        _write(tmp_path / "b.py", "needle\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert [hit[0] for hit in _hits(result)] == ["a.py"]
        assert result["data"]["scanLimited"].startswith("读到 ")
        assert "结果可能不完整" in result["data"]["result"]

    def test_reading_stops_at_the_file_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None) -> None:
        monkeypatch.setattr(sif, "_MAX_CANDIDATE_FILES", 2)
        for name in ("a.py", "b.py", "c.py"):
            _write(tmp_path / name, "needle\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert [hit[0] for hit in _hits(result)] == ["a.py", "b.py"]
        assert result["data"]["scanLimited"] == "读了 2 个文件就停了"

    def test_reaching_the_cap_with_nothing_left_is_not_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None,
    ) -> None:
        monkeypatch.setattr(sif, "_MAX_CANDIDATE_FILES", 2)
        for name in ("a.py", "b.py"):
            _write(tmp_path / name, "needle\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert len(_hits(result)) == 2
        assert "scanLimited" not in result["data"]

    def test_binary_files_count_toward_the_read_caps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None,
    ) -> None:
        monkeypatch.setattr(sif, "_MAX_CANDIDATE_FILES", 1)
        _write(tmp_path / "a.py", b"needle\x00")
        _write(tmp_path / "b.py", "needle\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert _hits(result) == []
        assert result["data"]["scanLimited"] == "读了 1 个文件就停了"

    def test_the_walk_cap_reaches_the_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_only: None) -> None:
        monkeypatch.setattr(sif, "MAX_WALK_FILES", 2)
        for name in ("a.txt", "b.txt", "c.py"):
            _write(tmp_path / name, "needle\n")

        result = _search(_Ctx(tmp_path), query="needle", pattern="*.py")

        assert _hits(result) == []
        assert result["data"]["scanLimited"] == "遍历了 2 个文件就停了"
        assert "遍历了 2 个文件就停了" in result["data"]["result"]

    def test_the_read_caps_apply_to_the_ripgrep_path_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_rg: _FakeRipgrep,
    ) -> None:
        monkeypatch.setattr(sif, "_MAX_CANDIDATE_FILES", 1)
        for name in ("a.py", "b.py"):
            _write(tmp_path / name, "needle\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert result["data"]["searcher"] == "ripgrep"
        assert [hit[0] for hit in _hits(result)] == ["a.py"]
        assert result["data"]["scanLimited"] == "读了 1 个文件就停了"


class TestPythonSearch:
    def test_what_is_searched_and_what_is_skipped(self, tmp_path: Path, python_only: None) -> None:
        result = _search(_Ctx(_tree(tmp_path)), query="needle")

        assert result["ok"] is True
        assert result["data"]["searcher"] == "python"
        assert _hits(result) == _DEFAULT_HITS

    def test_include_ignored_reaches_gitignored_files_but_not_the_skip_list(self, tmp_path: Path, python_only: None) -> None:
        result = _search(_Ctx(_tree(tmp_path)), query="needle", includeIgnored=True)

        files = {hit[0] for hit in _hits(result)}
        assert {"src/cache.tmp", "secret/s.py"} <= files
        assert not files & {"node_modules/m.py", "logs/app.py", "dist.backup-1/x.py", ".git/HEAD", "src/app.log"}

    def test_the_snake_case_spelling_works_too(self, tmp_path: Path, python_only: None) -> None:
        result = _search(_Ctx(_tree(tmp_path)), query="needle", include_ignored=True)

        assert "src/cache.tmp" in {hit[0] for hit in _hits(result)}

    def test_no_match_in_a_work_tree_points_at_ignored_files(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / ".git" / "HEAD", "ref\n")
        _write(tmp_path / ".gitignore", "*.tmp\n")
        _write(tmp_path / "only.tmp", "rare\n")

        hidden = _search(_Ctx(tmp_path), query="rare")
        shown = _search(_Ctx(tmp_path), query="rare", includeIgnored=True)

        assert hidden["data"]["count"] == 0
        assert "includeIgnored=true" in hidden["data"]["result"]
        assert _hits(shown) == [("only.tmp", 1, 1)]

    def test_a_named_file_is_searched_even_when_it_would_be_skipped(self, tmp_path: Path, python_only: None) -> None:
        _tree(tmp_path)

        result = _search(_Ctx(tmp_path), query="needle", path="src/app.log")

        assert _hits(result) == [("src/app.log", 1, 1)]

    def test_search_is_case_insensitive(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.txt", "Needle NEEDLE needle\n")

        result = _search(_Ctx(tmp_path), query="nEEdle")

        assert [hit[2] for hit in _hits(result)] == [1, 8, 15]

    def test_literal_mode_does_not_treat_the_query_as_a_regex(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.txt", "a.b\naxb\n")

        literal = _search(_Ctx(tmp_path), query="a.b")
        regex = _search(_Ctx(tmp_path), query="a.b", isRegex=True)

        assert _hits(literal) == [("a.txt", 1, 1)]
        assert _hits(regex) == [("a.txt", 1, 1), ("a.txt", 2, 1)]

    def test_an_invalid_regex_is_reported(self, tmp_path: Path, python_only: None) -> None:
        result = _search(_Ctx(tmp_path), query="(unclosed", isRegex=True)

        assert result["ok"] is False
        assert "invalid regex" in result["error"]

    def test_max_results_truncates(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.txt", "needle\n" * 5)

        result = _search(_Ctx(tmp_path), query="needle", maxResults=2)

        assert result["data"]["count"] == 2
        assert result["data"]["truncated"] is True
        assert result["data"]["result"].startswith("2 results found (truncated):")

    def test_the_text_lists_location_match_and_context(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.py", "before\nneedle here\nafter\n")

        result = _search(_Ctx(tmp_path), query="needle")

        assert result["data"]["result"] == (
            "1 results found:\n\na.py:2 | needle\n  1: before\n  2: needle here\n  3: after"
        )

    def test_a_missing_path_is_an_error(self, tmp_path: Path, python_only: None) -> None:
        result = _search(_Ctx(tmp_path), query="x", path="nope")

        assert result["ok"] is False
        assert result["error"] == "path not found"

    def test_reading_inside_the_workspace_asks_no_approval(self, tmp_path: Path, python_only: None) -> None:
        # 工作区内的搜索照旧不送审（既有决定，换 rg 不改策略行为）
        _write(tmp_path / "a.txt", "needle\n")
        ctx = _Ctx(tmp_path)

        _search(ctx, query="needle")

        assert ctx.requested == []

    def test_an_external_path_goes_through_the_read_approval(self, tmp_path: Path, python_only: None) -> None:
        workspace = tmp_path / "ws"
        outside = tmp_path / "outside"
        workspace.mkdir()
        _write(outside / "x.txt", "needle\n")
        ctx = _Ctx(workspace, read_file="deny")

        result = _search(ctx, query="needle", path=str(outside))

        assert result["ok"] is False
        assert [op for op, _params in ctx.requested] == ["read_file"]
        assert ctx.requested[0][1]["tool_name"] == "search_in_files"


class TestRipgrepAgreesWithPython:
    @pytest.mark.parametrize("args", [
        {"query": "needle"},
        {"query": "NEEDLE"},
        {"query": "needle", "pattern": "*.py"},
        {"query": "needle", "pattern": "src/**/*.py"},
        {"query": "need(le)+", "isRegex": True},
        {"query": "needle", "includeIgnored": True},
        {"query": "needle", "path": "src"},
        {"query": "needle", "path": "src", "pattern": "*.txt"},
    ])
    def test_same_hits_same_text_same_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: dict) -> None:
        ctx = _Ctx(_tree(tmp_path))

        via_rg, via_python = _both(monkeypatch, ctx, **args)

        assert via_rg["data"]["searcher"] == "ripgrep"
        assert via_python["data"]["searcher"] == "python"
        assert via_rg["data"]["results"] == via_python["data"]["results"]
        assert via_rg["data"]["result"] == via_python["data"]["result"]
        assert via_rg["data"]["count"] == via_python["data"]["count"] > 0

    def test_the_default_search_finds_the_expected_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        via_rg, _via_python = _both(monkeypatch, _Ctx(_tree(tmp_path)), query="needle")

        assert _hits(via_rg) == _DEFAULT_HITS

    def test_a_named_file_never_starts_ripgrep(self, tmp_path: Path, fake_rg: _FakeRipgrep) -> None:
        _tree(tmp_path)

        result = _search(_Ctx(tmp_path), query="needle", path="src/sub/c.py")

        assert fake_rg.calls == []
        assert result["data"]["searcher"] == "python"
        assert len(_hits(result)) == 2

    @pytest.mark.skipif(shutil.which("rg") is None, reason="这台机器上没装 ripgrep")
    @pytest.mark.parametrize("args", [
        {"query": "needle"},
        {"query": "needle", "pattern": "*.py"},
        {"query": "need(le)+", "isRegex": True},
        {"query": "needle", "includeIgnored": True},
    ])
    def test_the_real_ripgrep_agrees(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: dict) -> None:
        ctx = _Ctx(_tree(tmp_path))

        via_rg = _search(ctx, **args)
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: None)
        via_python = _search(ctx, **args)

        assert via_rg["data"]["searcher"] == "ripgrep"
        assert via_rg["data"]["results"] == via_python["data"]["results"]


class TestRipgrepArguments:
    def _argv(self, tmp_path: Path, fake: _FakeRipgrep, **args: Any) -> list[str]:
        _search(_Ctx(tmp_path), **args)
        return fake.calls[-1]

    def test_the_flags_mirror_the_python_rules(self, tmp_path: Path, fake_rg: _FakeRipgrep) -> None:
        _runtime(tmp_path, max_file_size_bytes=123_456)

        argv = self._argv(tmp_path, fake_rg, query="needle")

        for flag in (
            "--files-with-matches", "--null", "--no-config", "--hidden", "--no-ignore-dot",
            "--no-ignore-exclude", "--no-ignore-global", "--multiline", "--crlf", "--ignore-case", "--fixed-strings",
        ):
            assert flag in argv, flag
        assert "--no-ignore-vcs" not in argv
        assert argv[argv.index("--max-filesize") + 1] == "123456"
        globs = {argv[i + 1] for i, item in enumerate(argv) if item == "--glob"}
        assert {f"!{name}/" for name in sif._SKIP_DIRS} <= globs
        assert {f"!*{ending}" for ending in sif._SKIP_FILE_ENDINGS} <= globs
        assert "!dist.backup*/" in globs
        assert argv[-4:] == ["--regexp", "needle", "--", str(tmp_path.resolve())]
        assert "--type" not in argv

    def test_regex_include_ignored_and_a_file_type(self, tmp_path: Path, fake_rg: _FakeRipgrep) -> None:
        argv = self._argv(tmp_path, fake_rg, query="a+b", isRegex=True, includeIgnored=True, pattern="src/**/*.py")

        assert "--fixed-strings" not in argv
        assert "--no-ignore-vcs" in argv
        assert argv[argv.index("--type-add") + 1] == f"{sif._RG_PATTERN_TYPE}:*.[pP][yY]"
        assert argv[argv.index("--type") + 1] == sif._RG_PATTERN_TYPE

    def test_replace_mode_asks_ripgrep_to_match_case(self, tmp_path: Path, fake_rg: _FakeRipgrep) -> None:
        _write(tmp_path / "a.txt", "old\n")

        argv = self._argv(tmp_path, fake_rg, query="old", mode="replace", replace="new")

        assert "--case-sensitive" in argv
        assert "--ignore-case" not in argv

    @pytest.mark.parametrize("pattern,expected", [
        ("*.py", "*.[pP][yY]"),
        ("src/**/*_test.go", "*_[tT][eE][sS][tT].[gG][oO]"),
        ("**/*", None),
        ("*", None),
        ("*.{py,js}", None),
        ("main.py", None),
    ])
    def test_only_simple_suffix_patterns_become_a_type_filter(self, pattern: str, expected: str | None) -> None:
        assert sif._basename_type_glob(pattern) == expected


class TestRipgrepTrouble:
    def test_a_regex_ripgrep_rejects_falls_back_to_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "a.txt", "needle\n")
        fake = _FakeRipgrep(returncode=2, stderr=b"regex parse error: look-around is not supported")

        via_rg, via_python = _both(monkeypatch, _Ctx(tmp_path), fake, query="need(?=le)", isRegex=True)

        assert via_rg["data"]["searcher"] == "python"
        assert via_rg["data"]["results"] == via_python["data"]["results"]
        assert _hits(via_rg) == [("a.txt", 1, 1)]

    def test_exit_code_two_without_a_message_keeps_the_results(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # --no-messages 收掉了个别读不了的文件的报错，退出码 2 而 stderr 为空
        _write(tmp_path / "a.txt", "needle\n")
        fake = _FakeRipgrep(returncode=2)

        via_rg, _via_python = _both(monkeypatch, _Ctx(tmp_path), fake, query="needle")

        assert via_rg["data"]["searcher"] == "ripgrep"
        assert _hits(via_rg) == [("a.txt", 1, 1)]

    def test_ripgrep_that_cannot_start_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "a.txt", "needle\n")
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")

        async def cannot_start(argv: list[str], cwd: Path, ctx: Any) -> None:
            return None

        monkeypatch.setattr(sif, "_run_ripgrep", cannot_start)

        result = _search(_Ctx(tmp_path), query="needle")

        assert result["data"]["searcher"] == "python"
        assert _hits(result) == [("a.txt", 1, 1)]

    def test_a_timed_out_ripgrep_keeps_what_it_found_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in ("a.txt", "b.txt"):
            _write(tmp_path / name, "needle\n")
        fake = _FakeRipgrep(timed_out=True, cut_last=True)
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
        monkeypatch.setattr(sif, "_run_ripgrep", fake)

        result = _search(_Ctx(tmp_path), query="needle")

        # 假 rg 倒着报：b.txt 完整，a.txt 那条被截成半截路径丢掉
        assert _hits(result) == [("b.txt", 1, 1)]
        assert "ripgrep 跑了" in result["data"]["scanLimited"]
        assert "结果可能不完整" in result["data"]["result"]

    def test_an_overflowing_listing_is_marked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "a.txt", "needle\n")
        fake = _FakeRipgrep(overflow=True)
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
        monkeypatch.setattr(sif, "_run_ripgrep", fake)

        result = _search(_Ctx(tmp_path), query="needle")

        assert result["data"]["scanLimited"] == "ripgrep 找到的文件太多，只用了前一部分"

    def test_a_cancelled_ripgrep_reports_cancellation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "a.txt", "needle\n")
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
        monkeypatch.setattr(sif, "_run_ripgrep", _FakeRipgrep(cancelled=True))

        result = _search(_Ctx(tmp_path), query="needle")

        assert result["ok"] is False
        assert result["cancelled"] is True

    @pytest.mark.parametrize("stdout,cut_short,expected", [
        (b"a\x00b\x00c", True, ["a", "b"]),
        (b"a\x00b\x00c", False, ["a", "b", "c"]),
        (b"a\x00b\x00", True, ["a", "b"]),
        (b"", True, []),
    ])
    def test_a_half_written_last_path_is_dropped(self, stdout: bytes, cut_short: bool, expected: list[str]) -> None:
        assert sif._parse_ripgrep_paths(stdout, cut_short=cut_short) == expected


def _read(ctx: _Ctx, path: str) -> None:
    result = asyncio.run(read_file({"files": [{"path": path}]}, ctx))
    assert result["ok"] is True, result


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.usefixtures("python_only")
class TestReplaceMode:
    def test_matching_files_are_rewritten_and_listed(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.txt", "old old\n")
        _write(tmp_path / "sub" / "b.txt", "old\n")
        _write(tmp_path / "c.txt", "nothing\n")

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert result["ok"] is True, result
        lines = result["data"]["result"].splitlines()
        # engine.tool_history 按这几行认出改了哪些文件，格式不能变
        assert lines[:3] == ["2 files modified, 3 replacements", "a.txt: 2 replacements", "sub/b.txt: 1 replacements"]
        assert (tmp_path / "a.txt").read_bytes() == b"new new\n"
        assert (tmp_path / "sub" / "b.txt").read_bytes() == b"new\n"
        assert (tmp_path / "c.txt").read_bytes() == b"nothing\n"

    def test_one_write_approval_covers_the_call(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.txt", "old\n")
        _write(tmp_path / "b.txt", "old\n")
        ctx = _Ctx(tmp_path)

        _search(ctx, query="old", mode="replace", replace="new")

        assert [op for op, _params in ctx.requested] == ["write_file"]

    def test_a_denied_approval_changes_nothing(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.txt", "old\n")

        result = _search(_Ctx(tmp_path, write_file="deny"), query="old", mode="replace", replace="new")

        assert result["ok"] is False
        assert (tmp_path / "a.txt").read_bytes() == b"old\n"

    def test_line_endings_and_bom_survive(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.txt", _BOM + b"keep old\r\nold too\r\n")

        _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert (tmp_path / "a.txt").read_bytes() == _BOM + b"keep new\r\nnew too\r\n"

    def test_the_change_is_journaled_and_undo_puts_it_back(self, tmp_path: Path) -> None:
        original_a = _BOM + b"x = old\r\n"
        original_b = b"old\n"
        _write(tmp_path / "a.py", original_a)
        _write(tmp_path / "b.txt", original_b)
        ctx = _Ctx(tmp_path)

        result = _search(ctx, query="old", mode="replace", replace="new")
        assert all(entry["undoable"] is True for entry in result["data"]["results"])
        assert "undo_edits" in result["data"]["result"]

        undone = asyncio.run(undo_edits({}, ctx))

        assert undone["ok"] is True, undone
        assert undone["data"]["restoredCount"] == 2
        assert (tmp_path / "a.py").read_bytes() == original_a
        assert (tmp_path / "b.txt").read_bytes() == original_b

    def test_the_new_version_is_remembered_so_the_next_edit_is_not_stale(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.txt", "alpha old\nbeta\n")
        ctx = _Ctx(tmp_path)
        _read(ctx, "a.txt")

        _search(ctx, query="old", mode="replace", replace="new")
        entry = ctx.supervisor.stamp(file_state.session_key(ctx), "task-1", path.resolve()).entry
        follow_up = asyncio.run(apply_diff({"path": "a.txt", "diffs": [{"search": "beta", "replace": "gamma"}]}, ctx))

        assert entry is not None and entry.stamp.sha256 == _sha(b"alpha new\nbeta\n")
        assert follow_up["ok"] is True, follow_up
        assert path.read_bytes() == b"alpha new\ngamma\n"

    def test_a_file_changed_after_it_was_read_is_skipped(self, tmp_path: Path) -> None:
        stale = _write(tmp_path / "a.txt", "old\n")
        fresh = _write(tmp_path / "b.txt", "old\n")
        ctx = _Ctx(tmp_path)
        _read(ctx, "a.txt")
        stale.write_bytes(b"old, edited elsewhere\n")

        result = _search(ctx, query="old", mode="replace", replace="new")

        assert stale.read_bytes() == b"old, edited elsewhere\n"
        assert fresh.read_bytes() == b"new\n"
        assert result["data"]["skipped"] == [{"file": "a.txt", "reason": file_state.STALE_MESSAGE}]
        assert result["data"]["filesModified"] == 1

    def test_files_inside_the_edit_journal_are_never_changed(self, tmp_path: Path) -> None:
        journal_file = _write(edit_journal.journal_root(tmp_path) / "planted.txt", "old\n")
        target = _write(tmp_path / "src" / "a.txt", "old\n")

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert journal_file.read_bytes() == b"old\n"
        assert target.read_bytes() == b"new\n"
        assert {"file": "data/edit_journal/planted.txt", "reason": edit_journal.JOURNAL_REFUSED_MESSAGE} in (
            result["data"]["skipped"]
        )

    def test_a_file_another_running_task_is_editing_is_skipped_and_the_task_named(self, tmp_path: Path) -> None:
        held = _write(tmp_path / "a.txt", "old\nmore\n")
        free = _write(tmp_path / "b.txt", "old\n")
        other = _Ctx(tmp_path, task="task-other")
        other.supervisor.start("task-other", node="bootstrap.coder")
        edited = asyncio.run(apply_diff({"path": "a.txt", "diffs": [{"search": "more", "replace": "MORE"}]}, other))
        assert edited["ok"] is True, edited

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert held.read_bytes() == b"old\nMORE\n"
        assert free.read_bytes() == b"new\n"
        [skip] = result["data"]["skipped"]
        assert skip["file"] == "a.txt"
        assert "task-other" in skip["reason"] and "bootstrap.coder" in skip["reason"]
        assert result["data"]["filesModified"] == 1
        assert FakeSupervisor.of(tmp_path).holders(held.resolve()) == ["task-other"]

    def test_the_file_is_free_again_once_that_task_ends(self, tmp_path: Path) -> None:
        held = _write(tmp_path / "a.txt", "old\nmore\n")
        other = _Ctx(tmp_path, task="task-other")
        other.supervisor.start("task-other", node="bootstrap.coder")
        asyncio.run(apply_diff({"path": "a.txt", "diffs": [{"search": "more", "replace": "MORE"}]}, other))
        other.supervisor.end("task-other")

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert result["data"]["filesModified"] == 1, result
        assert held.read_bytes() == b"new\nMORE\n"

    def test_an_unreachable_supervisor_stops_the_replace_before_writing(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt"):
            _write(tmp_path / name, "old\n")
        ctx = _Ctx(tmp_path)
        ctx.supervisor.down = True

        result = _search(ctx, query="old", mode="replace", replace="new")

        assert result["ok"] is False and result["data"]["unavailable"] is True
        assert "连不上 supervisor" in result["error"]
        assert (tmp_path / "a.txt").read_bytes() == b"old\n"
        assert (tmp_path / "b.txt").read_bytes() == b"old\n"
        # 第一个文件认领不了就停下，不再一个个去试
        assert len(result["data"]["skipped"]) == 1

    def test_a_syntax_problem_after_the_change_is_reported_not_rolled_back(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.py", "value = (old)\n")

        result = _search(_Ctx(tmp_path), query="(old)", mode="replace", replace="(old")

        assert path.read_bytes() == b"value = (old\n"
        assert "改后检查没通过" in result["data"]["result"]

    def test_replace_is_case_sensitive_while_search_is_not(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.txt", "Old old\n")

        found = _search(_Ctx(tmp_path), query="OLD")
        _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert found["data"]["count"] == 2
        assert path.read_bytes() == b"Old new\n"

    def test_max_files_limits_how_many_files_change(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            _write(tmp_path / name, "old\n")

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new", maxFiles=2)

        assert result["data"]["filesModified"] == 2
        assert (tmp_path / "c.txt").read_bytes() == b"old\n"

    def test_replace_is_required_and_an_empty_string_deletes(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.txt", "keep drop keep\n")
        ctx = _Ctx(tmp_path)

        missing = _search(ctx, query="drop ", mode="replace")
        assert missing["ok"] is False
        assert ctx.requested == []

        _search(ctx, query="drop ", mode="replace", replace="")
        assert path.read_bytes() == b"keep keep\n"

    @pytest.mark.parametrize("query,replace,source,expected", [
        (r"(\w+)@(\w+)", "$2 at $1", "me@host", "host at me"),
        (r"(?P<word>a+)", "<${word}>", "baaa", "b<aaa>"),
        (r"b+", "[$&]", "abbc", "a[bb]c"),
        (r"b+", "[$0]", "abbc", "a[bb]c"),
        (r"x", "$$", "axb", "a$b"),
        (r"(a)", r"\1\1", "a", "aa"),
        (r"(a)", r"\g<1>!", "a", "a!"),
        (r"(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)(k)(l)", "$12", "abcdefghijkl", "l"),
        (r"(a)", "$12", "a", "a2"),
        (r"a", "$x", "a", "$x"),
    ])
    def test_regex_templates(self, tmp_path: Path, query: str, replace: str, source: str, expected: str) -> None:
        path = _write(tmp_path / "a.txt", source)

        result = _search(_Ctx(tmp_path), query=query, mode="replace", replace=replace, isRegex=True)

        assert result["ok"] is True, result
        assert path.read_text(encoding="utf-8") == expected

    def test_literal_mode_inserts_the_replacement_verbatim(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.txt", "a.b axb\n")

        _search(_Ctx(tmp_path), query="a.b", mode="replace", replace=r"$1\n$$")

        assert path.read_bytes() == b"$1\\n$$ axb\n"

    def test_a_broken_template_is_refused_before_any_approval(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.txt", "a\n")
        ctx = _Ctx(tmp_path)

        result = _search(ctx, query="(a)", mode="replace", replace="$2", isRegex=True)

        assert result["ok"] is False
        assert "replace 写法不对" in result["error"]
        assert ctx.requested == []
        assert path.read_bytes() == b"a\n"

    def test_ripgrep_finds_the_same_files_to_change(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("a.txt", "sub/b.txt"):
            _write(tmp_path / name, "old\n")
        fake = _FakeRipgrep()
        monkeypatch.setattr(sif, "_ripgrep_binary", lambda: "rg")
        monkeypatch.setattr(sif, "_run_ripgrep", fake)

        result = _search(_Ctx(tmp_path), query="old", mode="replace", replace="new")

        assert result["data"]["searcher"] == "ripgrep"
        assert [entry["file"] for entry in result["data"]["results"]] == ["a.txt", "sub/b.txt"]


class TestTheSearchReplaceSwitch:
    @pytest.mark.parametrize("value", [False, "false", "no", 0, "off", "garbage", None])
    def test_a_node_that_switched_it_off_is_refused_before_any_approval(self, tmp_path: Path, value: Any) -> None:
        path = _write(tmp_path / "a.txt", "old\n")
        ctx = _Ctx(tmp_path, node_extra={"search_replace": value})

        result = _search(ctx, query="old", mode="replace", replace="new")

        assert result["ok"] is False
        assert result["refused"] == sif.REPLACE_SWITCH
        assert result["error"] == sif.REPLACE_DISABLED_MESSAGE
        assert ctx.requested == []
        assert path.read_bytes() == b"old\n"

    @pytest.mark.parametrize("node_extra", [{"search_replace": True}, {"search_replace": "yes"}, {}, None])
    def test_replace_works_when_the_switch_is_on_or_absent(self, tmp_path: Path, python_only: None, node_extra: Any) -> None:
        path = _write(tmp_path / "a.txt", "old\n")

        result = _search(_Ctx(tmp_path, node_extra=node_extra), query="old", mode="replace", replace="new")

        assert result["ok"] is True, result
        assert path.read_bytes() == b"new\n"

    def test_search_mode_still_works_with_the_switch_off(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.txt", "old\n")

        result = _search(_Ctx(tmp_path, node_extra={"search_replace": False}), query="old")

        assert result["ok"] is True
        assert result["data"]["count"] == 1


class TestLongLines:
    @pytest.mark.parametrize("length", [0, 1, sif._MAX_CONTEXT_LINE_CHARS - 1, sif._MAX_CONTEXT_LINE_CHARS])
    def test_a_line_within_the_cap_is_untouched(self, length: int) -> None:
        line = "a" * length

        assert sif._clip_line(line) is line

    @pytest.mark.parametrize("extra", [1, 2, len(sif._CLIPPED_MARK) - 1, len(sif._CLIPPED_MARK), 10_000])
    def test_a_longer_line_is_cut_to_the_cap_with_the_mark_included(self, extra: int) -> None:
        line = "".join(chr(0x4E00 + index % 300) for index in range(sif._MAX_CONTEXT_LINE_CHARS + extra))

        clipped = sif._clip_line(line)

        assert len(clipped) == sif._MAX_CONTEXT_LINE_CHARS
        assert len(clipped) < len(line)
        assert clipped.endswith(sif._CLIPPED_MARK)
        assert line.startswith(clipped[:-len(sif._CLIPPED_MARK)])

    def test_long_matches_and_context_lines_stay_within_the_cap(self, tmp_path: Path, python_only: None) -> None:
        _write(tmp_path / "a.txt", "p" * 2000 + "\n" + "x" * 3000 + " needle " + "y" * 3000 + "\nafter\n")

        result = _search(_Ctx(tmp_path), query=r"x+ needle y+", isRegex=True)

        [hit] = result["data"]["results"]
        assert len(hit["match"]) == sif._MAX_CONTEXT_LINE_CHARS
        context = [line.split(": ", 1)[1] for line in hit["context"].splitlines()]
        assert [len(line) for line in context] == [sif._MAX_CONTEXT_LINE_CHARS, sif._MAX_CONTEXT_LINE_CHARS, len("after")]

    def test_the_replace_listing_clips_long_matches_the_same_way(self, tmp_path: Path, python_only: None) -> None:
        path = _write(tmp_path / "a.txt", "k" * 3000 + "\n")

        result = _search(_Ctx(tmp_path), query="k+", mode="replace", replace="v", isRegex=True)

        [match] = result["data"]["matches"]
        assert len(match["match"]) == sif._MAX_CONTEXT_LINE_CHARS
        assert path.read_bytes() == b"v\n"
