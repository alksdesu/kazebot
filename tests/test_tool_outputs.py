"""超长工具输出落盘、预览、按 ref 读回，以及保留期和会话拆除时的清理。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import engine.data_cleanup as cleanup  # noqa: E402
import engine.tool_outputs as tool_outputs  # noqa: E402
from _engine_loop_fixture import (  # noqa: E402
    NodeRun,
    ScriptedRegistry,
    call,
    echo,
    make_loop_state,
    make_rctx,
    ok,
    real_calls,
    turn,
    use_hooks,
)
from engine.builtin import tool_output_reader  # noqa: E402
from engine.builtin.loader import _register_declared_tools, iter_plugin_tool_meta  # noqa: E402
from engine.builtin.tool_output_reader import read_tool_output  # noqa: E402
from engine.inference.ai_step import _execute_real_tools, _resume_data_for_context, _run_async_deliver  # noqa: E402
from engine.inference.loop_state import build_tool_context, compact_target_session_id, context_archive_session_id  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.tool_outputs import (  # noqa: E402
    LATEST_ROUND_VIEW_CHARS,
    MAX_PATTERN_CHARS,
    MAX_READ_LINES,
    MAX_SEARCH_HITS,
    READ_TOOL_OUTPUT,
    ToolOutputLimits,
    ToolOutputNotFound,
    ToolOutputStore,
    inline_tool_output,
    is_valid_ref,
    preview_text,
    read_lines,
    search_lines,
    session_dir_name,
    tool_output_limits,
)

_DAY = 24 * 3600
_LIMITS = ToolOutputLimits(max_inline_chars=32000, view_chars=8000, retention_days=7)


def _numbered(count: int, width: int = 40) -> str:
    return "\n".join(f"line-{index:05d} " + "x" * width for index in range(1, count + 1))


def _ref_of(preview: str) -> str:
    return preview.split("ref=", 1)[1][:27]


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * _DAY
    os.utime(path, (stamp, stamp))


def _reader_ctx(workspace: Path, session_id: str) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=workspace, session_id="branch_9", _context_archive_session_id=session_id)


class TestInlineToolOutput:
    def test_a_result_within_the_limit_is_left_alone(self, tmp_path: Path) -> None:
        text = _numbered(10)

        assert inline_tool_output(tmp_path, "s1", "demo", text, _LIMITS) == text
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_a_long_result_is_saved_whole_and_previewed(self, tmp_path: Path) -> None:
        text = _numbered(800)

        preview = inline_tool_output(tmp_path, "s1", "demo", text, _LIMITS)

        ref = _ref_of(preview)
        assert is_valid_ref(ref)
        assert ToolOutputStore(tmp_path).load("s1", ref) == text
        assert len(preview) <= _LIMITS.view_chars
        lines = text.splitlines()
        assert preview.endswith(
            f"[完整输出共 {len(text)} 字符 / {len(lines)} 行，已保存为 ref={ref}，可用 read_tool_output 按行读取或搜索]"
        )
        # ref 也在第一行：请求视图把较早的结果截到只剩开头时还能看到
        assert ref in preview.splitlines()[0]
        assert "line-00001" in preview and "line-00800" in preview

    def test_the_preview_cuts_on_line_boundaries_and_names_what_was_left_out(self, tmp_path: Path) -> None:
        text = _numbered(800)
        lines = text.splitlines()

        preview = inline_tool_output(tmp_path, "s1", "demo", text, _LIMITS)

        body = preview.splitlines()[1:-1]
        marker_index = next(index for index, row in enumerate(body) if row.startswith("[……中间省略第 "))
        head, tail = body[:marker_index], body[marker_index + 1:]
        assert head == lines[:len(head)]
        assert tail == lines[len(lines) - len(tail):]
        omitted = len(lines) - len(head) - len(tail)
        first, last = len(head) + 1, len(lines) - len(tail)
        # 省略的是原文里被拿掉的那一段：中间各行连同最后一行的换行；开头那段自带的换行算在开头里
        omitted_chars = len(text) - (len("\n".join(head)) + 1) - len("\n".join(tail))
        assert body[marker_index] == f"[……中间省略第 {first}–{last} 行，共 {omitted} 行、{omitted_chars} 字符……]"

    def test_the_head_gets_about_sixty_percent_of_the_preview(self, tmp_path: Path) -> None:
        preview = inline_tool_output(tmp_path, "s1", "demo", _numbered(800), _LIMITS)

        body = preview.splitlines()[1:-1]
        marker_index = next(index for index, row in enumerate(body) if row.startswith("[……"))
        head_chars = sum(len(row) + 1 for row in body[:marker_index])
        tail_chars = sum(len(row) + 1 for row in body[marker_index + 1:])
        assert 0.5 < head_chars / (head_chars + tail_chars) < 0.7

    def test_one_giant_line_is_cut_by_characters_instead_of_vanishing(self, tmp_path: Path) -> None:
        text = "a" * 20000 + "z" * 20000

        preview = inline_tool_output(tmp_path, "s1", "demo", text, _LIMITS)

        assert preview.count("a") > 2000 and preview.count("z") > 1500
        assert len(preview) <= _LIMITS.view_chars

    def test_read_tool_output_results_are_never_saved_again(self, tmp_path: Path) -> None:
        preview = inline_tool_output(tmp_path, "s1", READ_TOOL_OUTPUT, _numbered(800), _LIMITS)

        assert "ref=" not in preview
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_a_failed_save_still_bounds_the_context(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken_save(self: Any, session_id: str, text: str) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(ToolOutputStore, "save", broken_save)

        preview = inline_tool_output(tmp_path, "s1", "demo", _numbered(800), _LIMITS)

        assert "ref=" not in preview
        assert "没能保存" in preview
        assert len(preview) <= _LIMITS.view_chars

    def test_without_a_workspace_only_the_preview_is_kept(self) -> None:
        preview = inline_tool_output(None, "s1", "demo", _numbered(800), _LIMITS)

        assert "ref=" not in preview and "没能保存" in preview

    def test_an_empty_session_key_cannot_store_anything(self, tmp_path: Path) -> None:
        preview = inline_tool_output(tmp_path, "", "demo", _numbered(800), _LIMITS)

        assert "ref=" not in preview
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_preview_text_without_cutting_wraps_short_text(self) -> None:
        text = _numbered(3)
        saved = tool_outputs.SavedOutput(ref="to_" + "0" * 24, chars=len(text), lines=3)

        preview = preview_text(text, 8000, saved=saved)

        assert text in preview and "[……" not in preview


class TestLimits:
    def test_defaults(self) -> None:
        assert tool_output_limits({}) == ToolOutputLimits(32000, 8000, 7)

    def test_values_are_clamped(self) -> None:
        limits = tool_output_limits({"engine": {"tool_output": {
            "max_inline_chars": 10, "view_chars": 999999, "retention_days": 0,
        }}})

        assert limits.max_inline_chars == 2000
        assert limits.view_chars == 2000
        assert limits.retention_days == 1

    def test_the_view_never_exceeds_what_the_request_view_keeps_of_the_latest_round(self) -> None:
        limits = tool_output_limits({"engine": {"tool_output": {"max_inline_chars": 100000, "view_chars": 20000}}})

        assert LATEST_ROUND_VIEW_CHARS == 8000
        assert limits.view_chars == LATEST_ROUND_VIEW_CHARS


class TestRefsAndSessions:
    @pytest.mark.parametrize("ref", ["to_0123456789abcdef01234567", "to_" + "f" * 24])
    def test_valid_refs(self, ref: str) -> None:
        assert is_valid_ref(ref)

    @pytest.mark.parametrize("ref", [
        "", "to_", "to_0123456789ABCDEF01234567", "to_0123456789abcdef0123456",
        "to_0123456789abcdef012345678", "../to_0123456789abcdef01234567",
        "to_0123456789abcdef01234567.txt", "to_0123456789abcdef01234567\n", None, 123,
    ])
    def test_invalid_refs(self, ref: Any) -> None:
        assert not is_valid_ref(ref)

    def test_path_for_rejects_a_bad_ref(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ToolOutputStore(tmp_path).path_for("s1", "../../secret")

    def test_a_hostile_session_key_stays_inside_the_output_root(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path)

        directory = store.session_dir("../../etc/passwd")

        assert directory.parent == store.root
        assert "/" not in directory.name and "\\" not in directory.name and ".." not in directory.name

    def test_keys_that_sanitize_alike_get_different_directories(self) -> None:
        assert session_dir_name("a/b") != session_dir_name("a?b")
        assert session_dir_name("a/b") == session_dir_name("a/b")

    def test_an_empty_session_key_is_refused(self) -> None:
        with pytest.raises(ValueError):
            session_dir_name("  ")

    def test_another_session_cannot_read_the_output(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path)
        saved = store.save("s1", "secret")

        with pytest.raises(ToolOutputNotFound):
            store.load("s2", saved.ref)

    def test_an_unknown_ref_is_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ToolOutputNotFound):
            ToolOutputStore(tmp_path).load("s1", "to_" + "0" * 24)


class TestRetention:
    def test_an_expired_output_is_unreadable_and_purged(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        old = store.save("s1", "old")
        fresh = store.save("s1", "fresh")
        _age(store.path_for("s1", old.ref), days=8)

        with pytest.raises(ToolOutputNotFound):
            store.load("s1", old.ref)
        assert store.purge() == 1
        assert not store.path_for("s1", old.ref).exists()
        assert store.load("s1", fresh.ref) == "fresh"

    def test_dry_run_only_counts(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        old = store.save("s1", "old")
        _age(store.path_for("s1", old.ref), days=8)

        assert store.purge(dry_run=True) == 1
        assert store.path_for("s1", old.ref).exists()

    def test_an_emptied_session_directory_is_removed(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        old = store.save("s1", "old")
        _age(store.path_for("s1", old.ref), days=8)

        store.purge()

        assert not store.session_dir("s1").exists()

    def test_leftover_temporary_files_are_purged_too(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path, retention_days=7)
        directory = store.session_dir("s1")
        directory.mkdir(parents=True)
        leftover = directory / ".abc.tmp"
        leftover.write_text("half", encoding="utf-8")
        _age(leftover, days=8)

        assert store.purge() == 1
        assert not leftover.exists()

    def test_purge_session_removes_only_that_session(self, tmp_path: Path) -> None:
        store = ToolOutputStore(tmp_path)
        store.save("s1", "a")
        store.save("s1", "b")
        kept = store.save("s2", "c")

        assert store.purge_session("s1") == 2
        assert store.purge_session("s1") == 0
        assert not store.session_dir("s1").exists()
        assert store.load("s2", kept.ref) == "c"

    def test_the_cleanup_job_purges_expired_outputs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", False)
        store = ToolOutputStore(tmp_path, retention_days=7)
        old = store.save("s1", "old")
        fresh = store.save("s1", "fresh")
        _age(store.path_for("s1", old.ref), days=8)

        cleanup.purge_expired_tool_outputs()

        assert not store.path_for("s1", old.ref).exists()
        assert store.path_for("s1", fresh.ref).exists()

    def test_the_cleanup_job_respects_dry_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        store = ToolOutputStore(tmp_path, retention_days=7)
        old = store.save("s1", "old")
        _age(store.path_for("s1", old.ref), days=8)

        cleanup.purge_expired_tool_outputs()

        assert store.path_for("s1", old.ref).exists()

    def test_the_cleanup_job_is_scheduled(self) -> None:
        source = (_ROOT / "engine" / "data_cleanup.py").read_text(encoding="utf-8")

        assert "(purge_expired_tool_outputs, {})" in source

    def test_purging_a_session_removes_its_outputs(self, tmp_path: Path) -> None:
        from supervisor.eventlog import EventLog
        from supervisor.policy import PolicyEngine
        from supervisor.state import SupervisorState

        store = ToolOutputStore(tmp_path)
        gone = store.save("child_a", "erase")
        kept = store.save("child_b", "keep")
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-tool-outputs"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )

        state.purge_session_files("child_a")

        with pytest.raises(ToolOutputNotFound):
            store.load("child_a", gone.ref)
        assert store.load("child_b", kept.ref) == "keep"


class TestReadLines:
    def test_pages_carry_line_numbers_and_the_next_start(self) -> None:
        text = _numbered(500, width=5)

        page = read_lines(text, "to_x", start_line=1, max_lines=200, char_budget=100000)

        rows = page.text.splitlines()
        assert rows[0] == f"ref=to_x：共 500 行 {len(text)} 字符，本次第 1–200 行；下一页 start_line=201"
        assert rows[1] == "   1 | line-00001 xxxxx"
        assert len(rows) == 201
        assert (page.start_line, page.end_line, page.next_start_line, page.total_lines) == (1, 200, 201, 500)

    def test_the_last_page_says_it_reached_the_end(self) -> None:
        page = read_lines(_numbered(500, width=5), "to_x", start_line=401, max_lines=200, char_budget=100000)

        assert page.end_line == 500 and page.next_start_line is None
        assert page.text.splitlines()[0].endswith("已到末尾")

    def test_max_lines_is_capped(self) -> None:
        page = read_lines(_numbered(3000, width=1), "to_x", start_line=1, max_lines=999999, char_budget=10**7)

        assert page.end_line == MAX_READ_LINES

    def test_the_character_budget_ends_a_page_early(self) -> None:
        page = read_lines(_numbered(500, width=100), "to_x", start_line=1, max_lines=500, char_budget=2000)

        assert page.end_line < 500
        assert page.next_start_line == page.end_line + 1
        # 首行也算在预算里：超出的部分会被请求视图截掉，下一页的位置就对不上了
        assert len(page.text) <= 2000

    def test_a_start_past_the_end_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            read_lines(_numbered(10), "to_x", start_line=11, max_lines=10, char_budget=8000)

    def test_an_overlong_line_is_clipped_with_a_note(self) -> None:
        page = read_lines("y" * 10000, "to_x", start_line=1, max_lines=10, char_budget=8000)

        assert "本行共 10000 字符" in page.text
        assert len(page.text) < 8000

    def test_an_empty_output(self) -> None:
        assert read_lines("", "to_x", start_line=1, max_lines=10, char_budget=8000).total_lines == 0

    @pytest.mark.parametrize("budget", [1000, 8000])
    def test_a_full_page_including_its_first_line_fits_the_budget(self, budget: int) -> None:
        text = "\n".join(f"{index:05d} " + "y" * 300 for index in range(1, 400))

        page = read_lines(text, "to_" + "a" * 24, start_line=1, max_lines=MAX_READ_LINES, char_budget=budget)

        assert len(page.text) <= budget
        assert page.next_start_line == page.end_line + 1
        assert page.text.splitlines()[0].endswith(f"下一页 start_line={page.next_start_line}")

    def test_an_overlong_line_still_fits_a_small_budget(self) -> None:
        page = read_lines("z" * 50000, "to_" + "a" * 24, start_line=1, max_lines=10, char_budget=1000)

        assert len(page.text) <= 1000
        assert "本行共 50000 字符" in page.text


class TestSearchLines:
    def test_hits_come_with_line_numbers_and_context(self) -> None:
        text = "\n".join(["alpha", "beta", "gamma ERROR here", "delta", "epsilon", "zeta", "eta", "theta", "iota ERROR"])

        page = search_lines(text, "to_x", pattern="ERROR", start_line=1, char_budget=8000)

        rows = page.text.splitlines()
        assert rows[0].startswith("ref=to_x：第 1–9 行里按正则或子串 'ERROR' 命中 2 行，这次列出 2 行")
        assert "   3: gamma ERROR here" in rows
        assert "   1- alpha" in rows and "   5- epsilon" in rows
        assert "--" in rows
        assert "   9: iota ERROR" in rows

    def test_regex_patterns_work(self) -> None:
        page = search_lines("a1\nb22\nc333", "to_x", pattern=r"\d{3}", start_line=1, char_budget=8000)

        assert "   3: c333" in page.text.splitlines()

    def test_an_invalid_regex_falls_back_to_a_substring(self) -> None:
        page = search_lines("call foo(\nother", "to_x", pattern="foo(", start_line=1, char_budget=8000)

        assert "   1: call foo(" in page.text.splitlines()
        assert "按子串" in page.text.splitlines()[0]

    def test_no_hits_says_so(self) -> None:
        page = search_lines("a\nb", "to_x", pattern="zzz", start_line=1, char_budget=8000)

        assert "没有匹配" in page.text and page.next_start_line is None

    def test_too_many_hits_continue_from_a_start_line(self) -> None:
        text = "\n".join(f"hit {index}" for index in range(1, 201))

        page = search_lines(text, "to_x", pattern="hit", start_line=1, char_budget=10**6)

        assert f"这次列出 {MAX_SEARCH_HITS} 行" in page.text.splitlines()[0]
        assert page.next_start_line == MAX_SEARCH_HITS + 1
        follow = search_lines(text, "to_x", pattern="hit", start_line=page.next_start_line, char_budget=10**6)
        assert f"{MAX_SEARCH_HITS + 1:>4}: hit {MAX_SEARCH_HITS + 1}" in follow.text.splitlines()

    def test_an_overlong_pattern_is_refused(self) -> None:
        with pytest.raises(ValueError):
            search_lines("a", "to_x", pattern="p" * 501, start_line=1, char_budget=8000)

    @pytest.mark.parametrize("budget", [1000, 8000])
    def test_hits_on_long_lines_fit_the_budget(self, budget: int) -> None:
        text = "\n".join(("hit " if index % 3 == 0 else "") + "w" * 3000 for index in range(60))

        page = search_lines(text, "to_" + "a" * 24, pattern="hit", start_line=1, char_budget=budget)

        assert len(page.text) <= budget
        assert page.next_start_line is not None
        assert page.text.splitlines()[0].endswith(f"用 start_line={page.next_start_line} 接着搜")

    def test_a_hit_with_context_on_both_sides_fits_even_when_every_line_is_long(self) -> None:
        lines = ["w" * 3000 for _ in range(5)]
        lines[2] = "w" * 1500 + "needle" + "w" * 1500

        page = search_lines("\n".join(lines), "to_" + "a" * 24, pattern="needle", start_line=1, char_budget=8000)

        assert len(page.text) <= 8000
        assert len(page.text.splitlines()) == 6
        assert "needle" in page.text

    def test_a_long_pattern_is_shortened_in_the_first_line(self) -> None:
        pattern = "q" * MAX_PATTERN_CHARS
        text = "\n".join(pattern + str(index) for index in range(30))

        page = search_lines(text, "to_" + "a" * 24, pattern=pattern, start_line=1, char_budget=1000)

        header = page.text.splitlines()[0]
        assert "…" in header and len(header) < 300
        assert len(page.text) <= 1000


class TestReadToolOutputTool:
    def _saved(self, workspace: Path, session_id: str = "s1", lines: int = 500) -> tuple[str, str]:
        text = _numbered(lines, width=5)
        return ToolOutputStore(workspace).save(session_id, text).ref, text

    def test_it_reads_a_page_of_the_current_session(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path)

        result = asyncio.run(read_tool_output({"ref": ref, "start_line": 11, "max_lines": 5}, _reader_ctx(tmp_path, "s1")))

        assert result["ok"] is True
        data = result["data"]
        assert (data["start_line"], data["end_line"], data["next_start_line"], data["total_lines"]) == (11, 15, 16, 500)
        assert "  11 | line-00011 xxxxx" in data["result"]

    def test_it_searches(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path)

        result = asyncio.run(read_tool_output({"ref": ref, "pattern": "line-00250"}, _reader_ctx(tmp_path, "s1")))

        assert result["ok"] is True and " 250: line-00250 xxxxx" in result["data"]["result"]

    def test_it_reads_the_archive_session_not_the_branch(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path, session_id="parent")
        ctx = SimpleNamespace(workspace_root=tmp_path, session_id="branch_9", _context_archive_session_id="parent")

        assert asyncio.run(read_tool_output({"ref": ref}, ctx))["ok"] is True

    def test_another_sessions_ref_is_refused(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path, session_id="s1")

        result = asyncio.run(read_tool_output({"ref": ref}, _reader_ctx(tmp_path, "s2")))

        assert result["ok"] is False and "不属于本会话" in result["error"]

    @pytest.mark.parametrize("ref", ["", "../../etc/passwd", "to_123", None])
    def test_a_malformed_ref_is_refused(self, tmp_path: Path, ref: Any) -> None:
        result = asyncio.run(read_tool_output({"ref": ref}, _reader_ctx(tmp_path, "s1")))

        assert result["ok"] is False and "ref 格式不对" in result["error"]

    def test_a_non_integer_start_line_is_refused(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path)

        result = asyncio.run(read_tool_output({"ref": ref, "start_line": "abc"}, _reader_ctx(tmp_path, "s1")))

        assert result["ok"] is False and "start_line" in result["error"]

    def test_a_start_line_past_the_end_is_refused(self, tmp_path: Path) -> None:
        ref, _text = self._saved(tmp_path, lines=10)

        result = asyncio.run(read_tool_output({"ref": ref, "start_line": 50}, _reader_ctx(tmp_path, "s1")))

        assert result["ok"] is False and "超出范围" in result["error"]

    def test_without_a_session_nothing_is_read(self, tmp_path: Path) -> None:
        ctx = SimpleNamespace(workspace_root=tmp_path, session_id="", _context_archive_session_id="")

        result = asyncio.run(read_tool_output({"ref": "to_" + "0" * 24}, ctx))

        assert result["ok"] is False

    def test_the_tool_is_declared_by_a_builtin_plugin(self) -> None:
        declared = {item["name"]: item for item in iter_plugin_tool_meta()}

        assert declared[READ_TOOL_OUTPUT]["module"] == "tool_output_reader"
        assert declared[READ_TOOL_OUTPUT]["input_schema"]["required"] == ["ref"]

    def test_the_declaration_registers_into_a_tool_registry(self) -> None:
        registered: list[tuple[str, Any]] = []
        registry = SimpleNamespace(register_builtin_tool=lambda name, description, schema, func: registered.append((name, func)))

        _register_declared_tools("engine.builtin.tool_output_reader", tool_output_reader.PLUGIN_META, registry)

        assert registered == [(READ_TOOL_OUTPUT, read_tool_output)]


class _AsyncHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> Any:
        self.posts.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200)


class TestAsyncResults:
    def _deliver(self, workspace: Path | None, text: str) -> dict[str, Any]:
        http = _AsyncHttp()
        asyncio.run(_run_async_deliver(
            registry=SimpleNamespace(get_spec=lambda name: None),
            http=http,
            supervisor_url="http://supervisor",
            task_id="task-1",
            session_id="parent-session",
            tool_name="demo",
            tool_args={},
            async_tool_id="async-1",
            started_at=time.monotonic(),
            node_id="node-a",
            result={"ok": True, "data": {"result": text}},
            workspace_root=workspace,
        ))
        return http.posts[-1]["json"]

    def test_a_long_async_result_is_saved_under_the_delivery_session(self, tmp_path: Path) -> None:
        text = _numbered(800)

        payload = self._deliver(tmp_path, text)

        message = payload["message"]
        # 状态行原样留着：finish_guard 按这一行认异步结果
        assert message.startswith('✅ Async tool "demo" (id: async-1) completed in ')
        ref = _ref_of(message)
        assert ToolOutputStore(tmp_path).load("parent-session", ref) == text
        assert len(message) < 9000
        assert payload["success"] is True

    def test_a_short_async_result_is_unchanged(self, tmp_path: Path) -> None:
        payload = self._deliver(tmp_path, "done")

        assert payload["message"].endswith("Result:\ndone")
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_without_a_workspace_the_async_result_is_still_bounded(self) -> None:
        payload = self._deliver(None, _numbered(800))

        assert "ref=" not in payload["message"]
        assert len(payload["message"]) < 9000


def _resume_rctx(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=workspace, child_session_id="", parent_session_id="parent-1", session_id="branch-1")


class TestResumedToolResults:
    def test_a_long_batch_tool_result_is_saved_and_previewed(self, tmp_path: Path) -> None:
        text = _numbered(800)
        node_entry = {"kind": "node", "node_id": "helper", "status": "finish", "text": "x" * 40000}
        short_node = {"kind": "node", "node_id": "helper", "status": "finish", "text": "fine"}
        resume = {"type": "batch_results", "entries": [
            {"kind": "tool", "name": "probe", "status": "finish", "raw_inline": text},
            node_entry,
            {"kind": "tool", "name": "probe", "status": "finish", "raw_inline": "short"},
            short_node,
        ]}

        shown = asyncio.run(_resume_data_for_context(resume, _resume_rctx(tmp_path), {}))

        preview = shown["entries"][0]["raw_inline"]
        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(preview)) == text
        assert len(preview) <= LATEST_ROUND_VIEW_CHARS
        node_preview = shown["entries"][1]["text"]
        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(node_preview)) == "x" * 40000
        assert len(node_preview) <= LATEST_ROUND_VIEW_CHARS
        assert shown["entries"][2]["raw_inline"] == "short"
        assert shown["entries"][3] is short_node
        # 原数据还要收附件、留在循环状态里，不能被改掉
        assert resume["entries"][0]["raw_inline"] == text
        assert node_entry["text"] == "x" * 40000

    def test_a_long_node_result_lists_the_refs_its_preview_leaves_out(self, tmp_path: Path) -> None:
        refs = [f"to_{index:024x}" for index in (1, 2, 3)]
        lines = _numbered(800).split("\n")
        for ref, at in zip(refs, (5, 400, 795)):
            lines[at] += f" 详见 ref={ref}"
        text = "\n".join(lines)
        failure = "e" * 40000
        resume = {"type": "batch_results", "entries": [
            {"kind": "node", "node_id": "helper", "status": "finish", "text": text, "call_id": "w1"},
            {"kind": "node", "node_id": "helper", "status": "fail", "text": "", "error": failure, "call_id": "w2"},
        ]}

        shown = asyncio.run(_resume_data_for_context(resume, _resume_rctx(tmp_path), {}))

        finished, failed = shown["entries"]
        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(finished["text"])) == text
        assert f"ref={refs[0]}" in finished["text"] and f"ref={refs[2]}" in finished["text"]
        assert finished["text"].endswith(
            f"[全文里还提到这些存档，预览里没显示：ref={refs[1]}，同样用 read_tool_output 读]",
        )
        assert finished["call_id"] == "w1" and finished["status"] == "finish"
        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(failed["error"])) == failure
        assert failed["status"] == "fail" and failed["text"] == ""
        assert resume["entries"][0]["text"] == text

    def test_the_old_tool_results_shape_is_covered_too(self, tmp_path: Path) -> None:
        text = _numbered(800)
        resume = {"type": "tool_results", "tool_results": [{"name": "probe", "raw_inline": text}]}

        shown = asyncio.run(_resume_data_for_context(resume, _resume_rctx(tmp_path), {}))

        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(shown["tool_results"][0]["raw_inline"])) == text

    @pytest.mark.parametrize("resume", [
        {"type": "child_result", "result": {"text": "x" * 40000}},
        {"type": "batch_results", "entries": [{"kind": "tool", "name": "probe", "raw_inline": "short"}]},
    ])
    def test_anything_else_comes_back_as_the_same_object(self, tmp_path: Path, resume: dict[str, Any]) -> None:
        assert asyncio.run(_resume_data_for_context(resume, _resume_rctx(tmp_path), {})) is resume
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_a_resumed_batch_reaches_the_model_as_a_preview(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        text = _numbered(800)
        run = NodeRun(
            monkeypatch, tmp_path,
            registry=ScriptedRegistry(tools={"probe": echo()}),
            runtime_cfg={"engine": {"max_steps": 4}},
            rctx=make_rctx(tmp_path, session_id="branch-1", parent_session_id="parent-1"),
        )

        run.run(
            turn(call("finish", {"text": "ok"}, call_id="f")),
            instruction="",
            resume_data={"type": "batch_results", "entries": [
                {"kind": "tool", "name": "probe", "status": "finish", "raw_inline": text},
            ]},
        )

        resumed = [
            message for message in run.requests[0]["messages"]
            if str(message.get("content") or "").startswith('Tool result for "probe":')
        ]
        assert len(resumed) == 1
        assert ToolOutputStore(tmp_path).load("parent-1", _ref_of(resumed[0]["content"])) == text


class TestReaderAvailability:
    def _registry(self) -> ScriptedRegistry:
        return ScriptedRegistry(tools={"probe": echo()}, specs={READ_TOOL_OUTPUT: {}})

    def _run(self, monkeypatch: pytest.MonkeyPatch, workspace: Path, access: ToolAccess) -> NodeRun:
        node = Node(id="node-1", type="ai", prompt="Work.", tool_mode="native", tool_access=access)
        return NodeRun(monkeypatch, workspace, registry=self._registry(), node=node, runtime_cfg={"engine": {"max_steps": 4}})

    def test_a_node_with_any_real_tool_gets_the_reader_and_can_call_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = self._run(monkeypatch, tmp_path, ToolAccess(mode="allowlist", allow=["probe"]))

        run.run(
            turn(call(READ_TOOL_OUTPUT, {"ref": "to_" + "0" * 24}, call_id="r")),
            turn(call("finish", {"text": "ok"}, call_id="f")),
        )

        assert {"probe", READ_TOOL_OUTPUT} <= set(run.tool_names(0))
        assert run.registry.executed_names() == [READ_TOOL_OUTPUT]

    def test_denying_it_in_tool_access_does_not_take_it_away(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._run(monkeypatch, tmp_path, ToolAccess(mode="all", deny=[READ_TOOL_OUTPUT]))

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert READ_TOOL_OUTPUT in run.tool_names(0)

    def test_a_node_without_real_tools_does_not_get_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._run(monkeypatch, tmp_path, ToolAccess(mode="none"))

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert READ_TOOL_OUTPUT not in run.tool_names(0)

    def test_a_node_that_only_delegates_gets_it_for_its_childrens_long_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        node = Node(
            id="node-1", type="ai", prompt="Work.", tool_mode="native",
            tool_access=ToolAccess(mode="none"), delegate_targets=["scout"],
        )
        run = NodeRun(monkeypatch, tmp_path, registry=self._registry(), node=node, runtime_cfg={"engine": {"max_steps": 4}})

        run.run(turn(call("finish", {"text": "ok"}, call_id="f")))

        assert READ_TOOL_OUTPUT in run.tool_names(0)
        assert "probe" not in run.tool_names(0)


class TestSessionKey:
    @pytest.mark.parametrize("child, parent, session, expected", [
        ("child-1", "parent-1", "branch-1", "child-1"),
        ("", "parent-1", "branch-1", "parent-1"),
        ("", "", "main-1", "main-1"),
        ("  ", " parent-1 ", "branch-1", "parent-1"),
    ])
    def test_the_archive_session_key(self, child: str, parent: str, session: str, expected: str) -> None:
        rctx = SimpleNamespace(child_session_id=child, parent_session_id=parent, session_id=session)

        assert context_archive_session_id(rctx) == expected

    def test_the_spill_the_tool_context_and_the_reader_agree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        use_hooks(monkeypatch)
        text = _numbered(800)

        async def long_output(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return ok(text)

        rctx = make_rctx(tmp_path, session_id="branch-1", parent_session_id="parent-1", child_session_id="child-1")
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"probe": long_output}), rctx=rctx)

        asyncio.run(_execute_real_tools(ls, real_calls(call("probe", {}, call_id="p")), step=1))

        ref = _ref_of(ls.messages[-1]["content"])
        assert ToolOutputStore(tmp_path).load("child-1", ref) == text
        ctx = build_tool_context(ls)
        assert ctx._context_archive_session_id == compact_target_session_id(ls) == "child-1"
        page = asyncio.run(read_tool_output({"ref": ref, "start_line": 799}, ctx))
        assert page["ok"] is True and "line-00800" in page["data"]["result"]
