"""整份写回文本文件：UTF-8 BOM 跟原文件走，写入走同目录临时文件替换，失败时原文件一个字节都不动。

断言一律按字节。换行符的规矩见 test_text_files.py。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox import text_files  # noqa: E402
from toolbox.text_files import (  # noqa: E402
    BOM,
    CRLF,
    LF,
    NEW_FILE,
    TextLayout,
    decode_text,
    encode_text,
    layout_of,
    normalize_newlines,
    replace_bytes,
    rewrite_text,
    split_lines,
)

_BOM = b"\xef\xbb\xbf"


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


class TestTheBom:
    def test_a_bom_file_keeps_its_bom_and_its_newline(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", _BOM + b"a\r\nb\r\n")

        rewrite_text(path, "x\ny\n")

        assert path.read_bytes() == _BOM + b"x\r\ny\r\n"

    def test_content_that_brings_its_own_bom_does_not_get_a_second_one(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", _BOM + b"a\n")

        rewrite_text(path, BOM + "x\n")

        assert path.read_bytes() == _BOM + b"x\n"

    def test_a_file_without_bom_does_not_gain_one(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"a\n")

        rewrite_text(path, BOM + "x\n")

        assert path.read_bytes() == b"x\n"

    def test_a_new_file_takes_the_content_verbatim(self, tmp_path: Path) -> None:
        rewrite_text(tmp_path / "with.txt", BOM + "x\n")
        rewrite_text(tmp_path / "without.txt", "x\n")

        assert (tmp_path / "with.txt").read_bytes() == _BOM + b"x\n"
        assert (tmp_path / "without.txt").read_bytes() == b"x\n"

    def test_rewriting_twice_does_not_pile_up_boms(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", _BOM + b"a\r\n")

        rewrite_text(path, "x\n")
        first = path.read_bytes()
        rewrite_text(path, "x\n")

        assert path.read_bytes() == first == _BOM + b"x\r\n"


class TestLayout:
    def test_a_missing_file_is_a_new_file(self, tmp_path: Path) -> None:
        assert layout_of(tmp_path / "absent.txt") == NEW_FILE

    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"a\nb\n", TextLayout(LF, False)),
            (b"a\r\nb\r\n", TextLayout(CRLF, False)),
            (_BOM + b"a\r\n", TextLayout(CRLF, True)),
            (b"", TextLayout(LF, False)),
        ],
    )
    def test_the_layout_comes_from_the_bytes(self, data: bytes, expected: TextLayout) -> None:
        assert TextLayout.of_bytes(data) == expected

    @pytest.mark.parametrize(
        "content,layout,expected",
        [
            ("a\nb\n", TextLayout(CRLF, True), _BOM + b"a\r\nb\r\n"),
            ("a\r\nb\n", TextLayout(LF, False), b"a\nb\n"),
            ("a\nb", NEW_FILE, b"a\nb"),
            (BOM + "a\n", NEW_FILE, _BOM + b"a\n"),
        ],
    )
    def test_encoding_follows_the_layout(self, content: str, layout: TextLayout, expected: bytes) -> None:
        assert encode_text(content, layout) == expected


class TestDecoding:
    def test_the_bom_is_not_part_of_the_text(self) -> None:
        assert decode_text(_BOM + "名\r\n".encode("utf-8")) == "名\r\n"

    def test_non_utf8_bytes_are_refused(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            decode_text(b"\xff\xfe\x00a")

    def test_a_lone_cr_is_text_not_a_line_break(self) -> None:
        assert normalize_newlines("a\r\nb\rc\n") == "a\nb\rc\n"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", []),
            ("a", ["a"]),
            ("a\n", ["a"]),
            ("a\n\n", ["a", ""]),
            ("a\x0cb\nc", ["a\x0cb", "c"]),
            ("a\u2028b\n", ["a\u2028b"]),
        ],
    )
    def test_lines_split_only_on_newline(self, text: str, expected: list[str]) -> None:
        # str.splitlines 还会在 \x0c、\u2028 之类字符上断行，和 apply_diff 数 \n 的行号对不上
        assert split_lines(text) == expected


class TestTheAtomicReplace:
    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.txt", b"old\n")

        written = replace_bytes(path, b"new\n")

        assert written == 4
        assert path.read_bytes() == b"new\n"
        assert leftovers(tmp_path) == []

    def test_a_failed_replace_leaves_the_original_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = seed(tmp_path / "a.txt", b"original\n")

        def broken_replace(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(text_files.os, "replace", broken_replace)

        with pytest.raises(OSError):
            rewrite_text(path, "new\n")

        assert path.read_bytes() == b"original\n"
        assert leftovers(tmp_path) == []

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_the_permissions_are_kept(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "a.sh", b"echo hi\n")
        os.chmod(path, 0o750)

        rewrite_text(path, "echo bye\n")

        assert path.stat().st_mode & 0o777 == 0o750

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_a_new_file_takes_the_given_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "node.yaml"

        replace_bytes(path, b"model: x\n", new_file_mode=0o600)

        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
    def test_an_existing_file_ignores_the_new_file_mode(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "node.yaml", b"old\n")
        os.chmod(path, 0o644)

        replace_bytes(path, b"new\n", new_file_mode=0o600)

        # 已有文件权限照旧，new_file_mode 只管新建
        assert path.stat().st_mode & 0o777 == 0o644

    def test_a_symlink_is_written_through_and_stays_a_link(self, tmp_path: Path) -> None:
        target = seed(tmp_path / "real.txt", b"old\n")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("this platform or account cannot create symlinks")

        rewrite_text(link, "new\n")

        assert link.is_symlink()
        assert target.read_bytes() == b"new\n"
