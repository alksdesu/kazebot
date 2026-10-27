"""常驻进程的输出日志：按行写、写满轮转、行号跨轮转不变；tail、since、search 三种分页，记录里的状态重启后接得上。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.tool_outputs import MAX_PATTERN_CHARS, MAX_SEARCH_HITS  # noqa: E402
from supervisor.processes.logs import (  # noqa: E402
    MAX_LINE_BYTES,
    ProcessLog,
    display_line,
    page_search,
    page_since,
    page_tail,
)

_BUDGET = 8000


def _log(tmp_path: Path, *, max_bytes: int = 1 << 20, keep: int = 2, **extra: object) -> ProcessLog:
    log = ProcessLog(tmp_path / "logs", "p_test", max_bytes=max_bytes, keep_files=keep, **extra)
    log.open()
    return log


def _numbered(first: int, last: int) -> bytes:
    """每行 7 字节：line01\\n 这样。"""
    return b"".join(f"line{index:02d}\n".encode() for index in range(first, last + 1))


def _rotated_three_times(tmp_path: Path) -> ProcessLog:
    """一段写满 25 字节就轮转、留两段旧的：写 14 行后第 1–4 行已经轮转掉了。"""
    log = _log(tmp_path, max_bytes=25, keep=2)
    log.write(_numbered(1, 14))
    return log


class TestWriting:
    def test_complete_lines_come_back_and_the_unfinished_one_waits(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        lines, rotated = log.write(b"alpha\nbeta\ngam")

        assert lines == ["alpha", "beta"] and rotated is False
        assert log.lines_done == 2 and log.has_partial and log.last_line == 3
        assert log.partial_text() == "gam"

        lines, _rotated = log.write(b"ma\n")

        assert lines == ["gamma"]
        assert not log.has_partial and log.last_line == 3
        assert log.active_path.read_bytes() == b"alpha\nbeta\ngamma\n"

    def test_writing_before_open_is_an_error(self, tmp_path: Path) -> None:
        log = ProcessLog(tmp_path, "p_test", max_bytes=1024, keep_files=1)

        with pytest.raises(OSError):
            log.write(b"x\n")

    def test_a_line_longer_than_the_cap_is_broken_in_two(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        lines, _rotated = log.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")

        assert lines == ["x" * MAX_LINE_BYTES, "x" * 10]
        assert log.lines_done == 2

    def test_the_break_never_splits_a_character(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        # 第一个字节之后全是两字节的 é：在 MAX_LINE_BYTES 处切会落在字符中间
        data = b"a" + "é".encode() * 40000 + b"\n"

        lines, _rotated = log.write(data)

        assert lines == ["a" + "é" * 32767, "é" * (40000 - 32767)]
        assert all("\ufffd" not in line for line in lines)

    def test_a_line_that_never_ends_is_broken_at_the_cap(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        assert log.write(b"y" * MAX_LINE_BYTES) == ([], False)
        lines, _rotated = log.write(b"z\n")

        assert lines == ["y" * MAX_LINE_BYTES, "z"]
        assert log.lines_done == 2 and not log.has_partial


class TestRotation:
    def test_line_numbers_survive_rotation(self, tmp_path: Path) -> None:
        log = _rotated_three_times(tmp_path)

        assert log.lines_done == 14
        assert log.first_lines == [5, 9, 13]
        assert (log.first_line, log.last_line) == (5, 14)
        assert [(path.name, first, last) for path, first, last in log.segments()] == [
            ("p_test.log.2", 5, 8), ("p_test.log.1", 9, 12), ("p_test.log", 13, 14),
        ]
        assert log.rotated_path(2).read_bytes() == _numbered(5, 8)
        assert not log.rotated_path(3).exists()

    def test_reading_from_the_start_says_what_was_rotated_out(self, tmp_path: Path) -> None:
        log = _rotated_three_times(tmp_path)

        page = page_since(log, 0, 200, _BUDGET)

        assert "第 1–4 行已经轮转掉了，从留存的第 5 行读起" in page.text
        assert (page.start_line, page.end_line, page.next_since, page.more) == (5, 14, 14, False)
        assert "   5 | line05" in page.text and "  14 | line14" in page.text
        assert "line04" not in page.text

    def test_a_range_spanning_segments_is_read_across_them(self, tmp_path: Path) -> None:
        log = _rotated_three_times(tmp_path)

        assert [number for number, _line in log.lines_between(7, 10)] == [7, 8, 9, 10]
        assert log.tail(3) == [(12, "line12"), (13, "line13"), (14, "line14")]

    def test_without_kept_segments_rotation_starts_the_file_over(self, tmp_path: Path) -> None:
        log = _log(tmp_path, max_bytes=10, keep=0)

        log.write(b"aaaaaaaaa\n")

        assert log.first_lines == [2] and log.last_line == 1
        assert "留存的行读不到了" in page_tail(log, 10, _BUDGET).text

        log.write(b"bb\n")

        page = page_tail(log, 10, _BUDGET)
        assert (page.start_line, page.end_line) == (2, 2) and "   2 | bb" in page.text
        assert not log.rotated_path(1).exists()


class TestDisplay:
    @pytest.mark.parametrize("raw, shown", [
        ("plain", "plain"),
        ("abc\r", "abc"),
        ("10%\r20%\r30%", "30%"),
        ("10%\r20%\r", "20%"),
        ("done\r\r", "done"),
        ("\r\r", ""),
    ])
    def test_carriage_returns_keep_what_a_terminal_would_show(self, raw: str, shown: str) -> None:
        assert display_line(raw) == shown

    def test_a_progress_bar_shows_its_last_state(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"step 1\r\nloading 10%\rloading 90%\n")

        page = page_tail(log, 10, _BUDGET)

        assert "   1 | step 1" in page.text
        assert "   2 | loading 90%" in page.text and "10%" not in page.text


class TestTail:
    def test_no_output_yet(self, tmp_path: Path) -> None:
        page = page_tail(_log(tmp_path), 10, _BUDGET)

        assert page.text == "还没有输出"
        assert (page.last_line, page.next_since, page.start_line) == (0, 0, None)

    def test_the_last_lines_and_where_to_continue(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(_numbered(1, 10))

        page = page_tail(log, 3, _BUDGET)

        assert (page.start_line, page.end_line, page.next_since) == (8, 10, 10)
        assert page.text.splitlines()[0] == "输出共 10 行（留存第 1–10 行），这次是第 8–10 行；之后有新输出用 since=10 接着读"

    def test_an_unfinished_line_is_shown_but_not_counted_as_read(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"one\ntwo\nthr")

        page = page_tail(log, 100, _BUDGET)

        assert (page.start_line, page.end_line, page.last_line) == (1, 3, 3)
        assert "最后一行还没写完" in page.text and "   3 | thr" in page.text
        assert page.next_since == 2

        log.write(b"ee\nfour\n")
        follow = page_since(log, page.next_since, 200, _BUDGET)

        assert "   3 | three" in follow.text and "   4 | four" in follow.text
        assert (follow.start_line, follow.end_line, follow.next_since) == (3, 4, 4)

    def test_the_budget_keeps_the_newest_lines(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"".join(f"{index:03d}".encode() + b"-" * 97 + b"\n" for index in range(1, 51)))

        page = page_tail(log, 100, 1000)

        # 每行显示 107 字符加换行：1000 字只放得下最后 9 行
        assert (page.start_line, page.end_line) == (42, 50)

    def test_a_huge_line_is_clipped_with_a_note(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"q" * 900 + b"\n")

        page = page_tail(log, 10, 1000)

        assert "…[本行共 900 字符，只显示前 500 字符" in page.text


class TestSince:
    def test_paging_forward_until_the_end(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"".join(f"{index:03d}".encode() + b"-" * 97 + b"\n" for index in range(1, 51)))

        first = page_since(log, 0, 200, 1000)

        assert (first.start_line, first.end_line, first.more, first.next_since) == (1, 9, True, 9)
        assert "后面还有，用 since=9 接着读" in first.text

        second = page_since(log, first.next_since, 200, 1000)

        assert second.start_line == 10

    def test_max_lines_limits_the_page(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(_numbered(1, 20))

        page = page_since(log, 5, 3, _BUDGET)

        assert (page.start_line, page.end_line, page.more, page.next_since) == (6, 8, True, 8)

    def test_nothing_new_yet(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(_numbered(1, 10))

        page = page_since(log, 10, 200, _BUDGET)

        assert page.text == "第 10 行之后还没有新输出（共 10 行）；稍后用 since=10 再读"
        assert (page.start_line, page.next_since, page.more) == (None, 10, False)

    def test_a_since_past_the_end_is_pulled_back(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(_numbered(1, 10))

        page = page_since(log, 99, 200, _BUDGET)

        assert page.next_since == 10


class TestSearch:
    @staticmethod
    def _twenty(tmp_path: Path) -> ProcessLog:
        log = _log(tmp_path)
        rows = [f"ok {index}" for index in range(1, 21)]
        rows[4] = "ERROR five"
        rows[14] = "ERROR fifteen"
        log.write("".join(f"{row}\n" for row in rows).encode())
        return log

    def test_hits_come_with_context_and_separators(self, tmp_path: Path) -> None:
        page = page_search(self._twenty(tmp_path), "ERROR", 0, _BUDGET)
        rows = page.text.splitlines()

        assert rows[0] == "第 1–20 行里按正则或子串 'ERROR' 命中 2 行，这次列出 2 行；没有更多命中，之后有新输出用 since=20 接着搜"
        assert "   5: ERROR five" in rows and "   3- ok 3" in rows and "   7- ok 7" in rows
        assert "--" in rows and "  15: ERROR fifteen" in rows
        assert (page.more, page.next_since) == (False, 20)

    def test_search_after_since(self, tmp_path: Path) -> None:
        page = page_search(self._twenty(tmp_path), "ERROR", 10, _BUDGET)

        assert "命中 1 行" in page.text and "ERROR five" not in page.text

    def test_a_regex_matches(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"exit code=0\nexit code=17\n")

        page = page_search(log, r"code=[1-9]\d*", 0, _BUDGET)

        assert "   2: exit code=17" in page.text and "命中 1 行" in page.text

    def test_an_invalid_regex_is_searched_as_text(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"value [unclosed here\nother\n")

        page = page_search(log, "[unclosed", 0, _BUDGET)

        assert "按子串" in page.text and "   1: value [unclosed here" in page.text

    def test_no_match(self, tmp_path: Path) -> None:
        page = page_search(self._twenty(tmp_path), "WARN", 0, _BUDGET)

        assert "第 1–20 行里没有匹配 'WARN' 的行" in page.text
        assert page.next_since == 20

    @pytest.mark.parametrize("pattern", ["", "x" * (MAX_PATTERN_CHARS + 1)])
    def test_empty_or_oversized_patterns_are_refused(self, tmp_path: Path, pattern: str) -> None:
        with pytest.raises(ValueError):
            page_search(self._twenty(tmp_path), pattern, 0, _BUDGET)

    def test_too_many_hits_continue_on_the_next_page(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write("".join(f"hit {index}\n" for index in range(1, 121)).encode())

        page = page_search(log, "hit", 0, _BUDGET)

        assert f"命中 120 行，这次列出 {MAX_SEARCH_HITS} 行；后面还有，用 since=50 接着搜" in page.text
        assert (page.more, page.next_since) == (True, 50)

        follow = page_search(log, "hit", page.next_since, _BUDGET)

        assert "第 51–120 行里" in follow.text and follow.next_since == 100


class TestRecord:
    def test_a_restored_log_reads_the_same_lines(self, tmp_path: Path) -> None:
        log = _rotated_three_times(tmp_path)
        log.close()

        restored = ProcessLog(log.directory, "p_test", max_bytes=25, keep_files=2, **log.record())

        assert restored.record() == {"first_lines": [5, 9, 13], "lines_done": 14, "partial": False}
        assert page_since(restored, 0, 200, _BUDGET).text == page_since(log, 0, 200, _BUDGET).text

    def test_an_unfinished_line_is_remembered(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write(b"x\ny")
        log.close()

        restored = ProcessLog(log.directory, "p_test", max_bytes=1024, keep_files=2, **log.record())

        assert restored.has_partial and restored.last_line == 2
        page = page_tail(restored, 10, _BUDGET)
        assert "   2 | y" in page.text and page.next_since == 1

    def test_fewer_kept_segments_after_a_restart_only_count_the_newest(self, tmp_path: Path) -> None:
        log = _rotated_three_times(tmp_path)
        log.close()

        restored = ProcessLog(log.directory, "p_test", max_bytes=25, keep_files=1, **log.record())

        assert restored.first_lines == [9, 13] and restored.first_line == 9
        assert [first for _path, first, _last in restored.segments()] == [9, 13]

    def test_paths_cover_every_kept_segment(self, tmp_path: Path) -> None:
        log = ProcessLog(tmp_path, "p_x", max_bytes=1024, keep_files=2)

        assert [path.name for path in log.paths()] == ["p_x.log", "p_x.log.1", "p_x.log.2"]
