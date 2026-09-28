"""落盘换行符：已有的文件跟原文件走，新文件照内容原样写。

Path.write_text 默认按平台转换换行，Windows 上会把整份 LF 文件改成 CRLF，
Linux 上（读进来时 CRLF 已被通用换行归一）会把整份 CRLF 文件改成 LF —— 两边都是
一次保存改掉整个文件的每一行。断言一律按字节，两个平台上结论必须一样。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.text_files import CRLF, LF, newline_of, rewrite_text, write_text_as  # noqa: E402


class TestWhatNewlineAFileUses:
    def test_a_file_that_is_not_there_yet_has_no_newline(self, tmp_path: Path) -> None:
        assert newline_of(tmp_path / "absent.yaml") is None

    def test_lf_stays_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "lf.yaml"
        path.write_bytes(b"a: 1\nb: 2\n")

        assert newline_of(path) == LF

    def test_crlf_is_recognised(self, tmp_path: Path) -> None:
        path = tmp_path / "crlf.yaml"
        path.write_bytes(b"a: 1\r\nb: 2\r\n")

        assert newline_of(path) == CRLF

    def test_one_crlf_anywhere_makes_it_a_crlf_file(self, tmp_path: Path) -> None:
        # 混合的文件按 CRLF 写回：编辑器存的文件通常整份是 CRLF，混合多半是被
        # 某次 LF 写入破坏过，归成 CRLF 是把它修回去，归成 LF 是把破坏做实。
        path = tmp_path / "mixed.yaml"
        path.write_bytes(b"a: 1\nb: 2\r\nc: 3\n")

        assert newline_of(path) == CRLF

    def test_an_empty_file_counts_as_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_bytes(b"")

        assert newline_of(path) == LF

    def test_a_single_line_without_a_trailing_newline_counts_as_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "oneline.yaml"
        path.write_bytes(b"a: 1")

        assert newline_of(path) == LF

    def test_a_lone_cr_is_not_crlf(self, tmp_path: Path) -> None:
        # 孤立的 \r 是正文里的控制字符，不是行尾。
        path = tmp_path / "cr.yaml"
        path.write_bytes(b"a: on\rward\n")

        assert newline_of(path) == LF


class TestWritingWithAGivenNewline:
    def test_none_writes_the_content_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1\nb: 2\n", None)

        assert path.read_bytes() == b"a: 1\nb: 2\n"

    def test_none_keeps_crlf_that_the_content_itself_carries(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1\r\nb: 2\r\n", None)

        assert path.read_bytes() == b"a: 1\r\nb: 2\r\n"

    def test_none_keeps_mixed_content_exactly_as_given(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a\r\nb\nc", None)

        assert path.read_bytes() == b"a\r\nb\nc"

    def test_lf_flattens_crlf_in_the_content(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1\r\nb: 2\r\n", LF)

        assert path.read_bytes() == b"a: 1\nb: 2\n"

    def test_crlf_converts_every_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1\nb: 2\n", CRLF)

        assert path.read_bytes() == b"a: 1\r\nb: 2\r\n"

    def test_crlf_content_does_not_grow_a_second_cr(self, tmp_path: Path) -> None:
        # 直接 replace("\n", "\r\n") 会把已有的 \r\n 变成 \r\r\n，整份文件多出一轮空行。
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1\r\nb: 2\r\n", CRLF)

        assert path.read_bytes() == b"a: 1\r\nb: 2\r\n"

    def test_crlf_normalises_mixed_content_to_one_style(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a\r\nb\nc\r\n", CRLF)

        assert path.read_bytes() == b"a\r\nb\r\nc\r\n"

    def test_a_lone_cr_in_the_content_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: on\rward\n", CRLF)

        assert path.read_bytes() == b"a: on\rward\r\n"

    def test_content_without_any_newline_lands_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "a: 1", CRLF)

        assert path.read_bytes() == b"a: 1"

    def test_non_ascii_lands_as_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"

        write_text_as(path, "名: 你好\n", CRLF)

        assert path.read_bytes() == "名: 你好\r\n".encode("utf-8")

    def test_writing_replaces_the_previous_content_whole(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        path.write_bytes(b"old: 1\r\nold: 2\r\nold: 3\r\n")

        write_text_as(path, "new: 1\n", LF)

        assert path.read_bytes() == b"new: 1\n"


class TestRewritingAnExistingFile:
    def test_an_lf_file_stays_lf_even_when_the_content_has_crlf(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: 1\nb: 2\n")

        rewrite_text(path, "a: 9\r\nb: 8\r\n")

        assert path.read_bytes() == b"a: 9\nb: 8\n"

    def test_a_crlf_file_stays_crlf_even_when_the_content_has_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: 1\r\nb: 2\r\n")

        rewrite_text(path, "a: 9\nb: 8\n")

        assert path.read_bytes() == b"a: 9\r\nb: 8\r\n"

    def test_a_crlf_file_stays_crlf_when_the_content_already_matches(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: 1\r\n")

        rewrite_text(path, "a: 9\r\nb: 8\r\n")

        assert path.read_bytes() == b"a: 9\r\nb: 8\r\n"

    def test_a_new_file_takes_the_content_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.yaml"

        rewrite_text(path, "a: 1\nb: 2\n")

        assert path.read_bytes() == b"a: 1\nb: 2\n"

    def test_a_new_file_keeps_crlf_the_content_brought_along(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.yaml"

        rewrite_text(path, "a: 1\r\nb: 2\r\n")

        assert path.read_bytes() == b"a: 1\r\nb: 2\r\n"

    def test_an_empty_file_is_written_as_lf(self, tmp_path: Path) -> None:
        path = tmp_path / "placeholder.yaml"
        path.write_bytes(b"")

        rewrite_text(path, "a: 1\r\n")

        assert path.read_bytes() == b"a: 1\n"

    @pytest.mark.parametrize("seed", [b"a: 1\n", b"a: 1\r\n"])
    def test_rewriting_twice_does_not_drift(self, tmp_path: Path, seed: bytes) -> None:
        # 每次保存都把换行再转一遍，错一次就会越滚越多（\r\r\n、\r\r\r\n…）。
        path = tmp_path / "config.yaml"
        path.write_bytes(seed)

        rewrite_text(path, "a: 2\n")
        first = path.read_bytes()
        rewrite_text(path, "a: 2\n")

        assert path.read_bytes() == first
        assert b"\r\r" not in first


class TestTheByteCountThatComesBack:
    """返回的是磁盘上的字节数，不是入参内容的长度 —— 换行换过之后两者对不上。"""

    @pytest.mark.parametrize(
        "seed,content",
        [
            (b"a: 1\n", "a: 1\nb: 2\n"),
            (b"a: 1\r\n", "a: 1\nb: 2\n"),
            (b"a: 1\n", "a: 1\r\nb: 2\r\n"),
            (b"a: 1\r\n", "a: 1\r\nb: 2\r\n"),
            (b"a: 1\n", "名: 你好\n说: 好\n"),
            (b"a: 1\r\n", "名: 你好\n说: 好\n"),
        ],
    )
    def test_rewriting_an_existing_file_reports_the_size_on_disk(
        self, tmp_path: Path, seed: bytes, content: str,
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(seed)

        written = rewrite_text(path, content)

        assert written == path.stat().st_size

    @pytest.mark.parametrize("content", ["a: 1\nb: 2\n", "a: 1\r\nb: 2\r\n", "名: 你好\n"])
    def test_a_new_file_reports_the_size_on_disk(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "fresh.yaml"

        written = rewrite_text(path, content)

        assert written == path.stat().st_size

    @pytest.mark.parametrize("newline", [None, LF, CRLF])
    def test_writing_with_a_given_newline_reports_the_size_on_disk(
        self, tmp_path: Path, newline: str | None,
    ) -> None:
        path = tmp_path / "out.yaml"

        written = write_text_as(path, "名: 你好\r\n说: 好\n", newline)

        assert written == path.stat().st_size

    def test_converting_to_crlf_reports_more_than_the_content_length(self, tmp_path: Path) -> None:
        # 按入参内容算就会少报一个字节一行，覆盖 CRLF 文件时报的数和磁盘上的对不上。
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: 1\r\n")
        content = "a: 1\nb: 2\nc: 3\n"

        written = rewrite_text(path, content)

        assert written == path.stat().st_size == len(content.encode("utf-8")) + 3

    def test_flattening_to_lf_reports_less_than_the_content_length(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(b"a: 1\n")
        content = "a: 1\r\nb: 2\r\n"

        written = rewrite_text(path, content)

        assert written == path.stat().st_size == len(content.encode("utf-8")) - 2

    def test_non_ascii_is_counted_in_bytes_not_characters(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.yaml"

        written = rewrite_text(path, "你好\n")

        assert written == path.stat().st_size == 7

    def test_an_empty_write_reports_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"

        assert rewrite_text(path, "") == path.stat().st_size == 0


class TestRewritingStaysByteIdenticalToTheOldWriter:
    @pytest.mark.parametrize("seed", [b"a: 1\n", b"a: 1\r\n", None])
    @pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n", "a\r\nb\nc", "你好\n", ""])
    def test_the_bytes_match_an_explicit_utf8_encode(
        self, tmp_path: Path, seed: bytes | None, content: str,
    ) -> None:
        # 内部从 write_text(newline="") 换成了 write_bytes，落盘字节必须一模一样。
        path = tmp_path / "config.yaml"
        if seed is not None:
            path.write_bytes(seed)
        expected_newline = None if seed is None else (CRLF if b"\r\n" in seed else LF)
        expected = content
        if expected_newline is not None:
            expected = content.replace(CRLF, LF)
            if expected_newline != LF:
                expected = expected.replace(LF, CRLF)

        rewrite_text(path, content)

        assert path.read_bytes() == expected.encode("utf-8")
