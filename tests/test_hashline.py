"""hashline 的纯函数：行哈希、锚点的格式化和解析、行级 edit 的冲突判断和应用。

锚点「行号:哈希」都指改动前的文件；哈希对不上就整体不改，按位置从后往前替换，前面的行号不受后面改动影响。
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.hashline import (  # noqa: E402
    Anchor,
    LineEdit,
    apply_line_edits,
    edit_mode,
    find_overlaps,
    format_anchor,
    hashline_enabled,
    hashline_prefix,
    line_hash,
    looks_hashline_prefixed,
    parse_anchor,
)
from toolbox.text_files import split_lines  # noqa: E402

_FIVE = "a\nb\nc\nd\ne\n"


def anchor(text: str, line_no: int) -> Anchor:
    return Anchor(line_no, line_hash(split_lines(text)[line_no - 1]))


def edit(index: int, op: str, start: Anchor, end: Anchor | None = None, lines: tuple[str, ...] = ()) -> LineEdit:
    return LineEdit(index, op, start, end, lines)


def at(line_no: int) -> Anchor:
    """只用来判冲突的锚点：find_overlaps 只看行号。"""
    return Anchor(line_no, "000")


def other_hash(value: str) -> str:
    return f"{(int(value, 16) + 1) % 4096:03x}"


class TestLineHash:
    @pytest.mark.parametrize("line", ["", "x", "    return value", "中文内容", "\t\tdeep"])
    def test_it_is_three_lowercase_hex_digits(self, line: str) -> None:
        assert re.fullmatch(r"[0-9a-f]{3}", line_hash(line))

    def test_it_is_computed_from_the_content_without_trailing_whitespace(self) -> None:
        expected = f"{zlib.crc32('    x = 1'.encode('utf-8')) & 0xFFF:03x}"

        assert line_hash("    x = 1") == expected
        assert line_hash("    x = 1   ") == expected
        assert line_hash("    x = 1\t") == expected
        assert line_hash("    x = 1\r") == expected

    def test_leading_indentation_is_part_of_the_content(self) -> None:
        assert line_hash("  x") == f"{zlib.crc32(b'  x') & 0xFFF:03x}"

    def test_the_same_content_always_hashes_the_same(self) -> None:
        assert line_hash("def f():") == line_hash("def f():")

    def test_different_lines_spread_over_the_hash_space(self) -> None:
        assert len({line_hash(f"line {index}") for index in range(50)}) > 40


class TestAnchors:
    def test_format_and_prefix(self) -> None:
        digest = line_hash("  return x")

        assert format_anchor(12, "  return x") == f"12:{digest}"
        assert hashline_prefix(12, "  return x") == f"12:{digest}|"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12:a3f", Anchor(12, "a3f")),
            (" 12 : A3F ", Anchor(12, "a3f")),
            ("12:a3f|", Anchor(12, "a3f")),
            ("12:a3f|    return x", Anchor(12, "a3f")),
            ("1:000", Anchor(1, "000")),
        ],
    )
    def test_parse_accepts_what_read_file_prints(self, raw: str, expected: Anchor) -> None:
        assert parse_anchor(raw) == expected

    @pytest.mark.parametrize("raw", ["12", "a3f", "12:a3", "12:a3f4", "x:a3f", "12:xyz", "", "12 a3f", "-1:a3f"])
    def test_parse_rejects_anything_else(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_anchor(raw)

    @pytest.mark.parametrize("raw", [None, 12, True, ["12:a3f"], {"line": 12}])
    def test_parse_rejects_non_strings(self, raw: object) -> None:
        with pytest.raises(ValueError):
            parse_anchor(raw)

    def test_line_numbers_start_at_one(self) -> None:
        with pytest.raises(ValueError, match="从 1 开始"):
            parse_anchor("0:a3f")

    def test_a_formatted_anchor_parses_back(self) -> None:
        assert parse_anchor(format_anchor(7, "value = 3")) == Anchor(7, line_hash("value = 3"))
        assert str(Anchor(3, "0ab")) == "3:0ab"


class TestEditMode:
    @pytest.mark.parametrize("value", ["hashline", " HashLine "])
    def test_hashline_is_read_from_the_node_extra(self, value: str) -> None:
        ctx = SimpleNamespace(_node_extra={"edit_mode": value})

        assert hashline_enabled(ctx) is True
        assert edit_mode(ctx) == "hashline"

    @pytest.mark.parametrize("extra", [{}, {"edit_mode": "lines"}, {"edit_mode": None}, None, "hashline"])
    def test_anything_else_is_the_default_mode(self, extra: object) -> None:
        assert hashline_enabled(SimpleNamespace(_node_extra=extra)) is False

    def test_a_context_without_node_settings_is_the_default_mode(self) -> None:
        assert hashline_enabled(SimpleNamespace()) is False


class TestCopiedPrefixes:
    def test_lines_copied_with_their_anchors_are_spotted(self) -> None:
        assert looks_hashline_prefixed(["1:abc|x = 1", "", "2:def|y = 2"]) is True

    @pytest.mark.parametrize("lines", [["1:abc|x", "plain"], [], ["", "  "], ["ratio 1:2"]])
    def test_ordinary_content_is_not(self, lines: list[str]) -> None:
        assert looks_hashline_prefixed(lines) is False


class TestOverlaps:
    def test_ranges_sharing_a_line_conflict(self) -> None:
        problems = find_overlaps([edit(0, "replace", at(3), at(5), ("x",)), edit(1, "delete", at(5), at(6))])

        assert [(first, second) for first, second, _reason in problems] == [(0, 1)]

    def test_adjacent_ranges_do_not(self) -> None:
        assert find_overlaps([edit(0, "replace", at(3), at(4), ("x",)), edit(1, "replace", at(5), at(6), ("y",))]) == []

    def test_two_inserts_at_the_same_place_conflict(self) -> None:
        problems = find_overlaps([edit(0, "insert_after", at(4), lines=("x",)), edit(1, "insert_before", at(5), lines=("y",))])

        assert [(first, second) for first, second, _reason in problems] == [(0, 1)]
        assert "同一个位置" in problems[0][2]

    def test_inserts_on_both_sides_of_one_line_do_not(self) -> None:
        assert find_overlaps([edit(0, "insert_after", at(4), lines=("x",)), edit(1, "insert_before", at(4), lines=("y",))]) == []

    @pytest.mark.parametrize("op,line", [("insert_after", 4), ("insert_before", 4), ("insert_after", 3), ("insert_before", 6)])
    def test_an_insert_inside_a_range_conflicts(self, op: str, line: int) -> None:
        problems = find_overlaps([edit(0, op, at(line), lines=("x",)), edit(1, "replace", at(3), at(6), ("y",))])

        assert [(first, second) for first, second, _reason in problems] == [(0, 1)]

    @pytest.mark.parametrize("op,line", [("insert_before", 3), ("insert_after", 6), ("insert_after", 2), ("insert_before", 7)])
    def test_an_insert_right_next_to_a_range_does_not(self, op: str, line: int) -> None:
        assert find_overlaps([edit(0, op, at(line), lines=("x",)), edit(1, "delete", at(3), at(6))]) == []


class TestApplyLineEdits:
    def test_replace_one_line(self) -> None:
        outcome = apply_line_edits(_FIVE, [edit(0, "replace", anchor(_FIVE, 2), lines=("B",))])

        assert outcome.ok and outcome.changed
        assert outcome.content == "a\nB\nc\nd\ne\n"
        assert outcome.spans == {0: (2, 2)}

    def test_replace_a_range_with_more_lines(self) -> None:
        outcome = apply_line_edits(_FIVE, [edit(0, "replace", anchor(_FIVE, 2), anchor(_FIVE, 3), ("x", "y", "z"))])

        assert outcome.content == "a\nx\ny\nz\nd\ne\n"
        assert outcome.spans == {0: (2, 4)}

    def test_delete_leaves_an_empty_span_where_the_lines_were(self) -> None:
        outcome = apply_line_edits(_FIVE, [edit(0, "delete", anchor(_FIVE, 2), anchor(_FIVE, 4))])

        assert outcome.content == "a\ne\n"
        assert outcome.spans == {0: (2, 1)}

    def test_insert_after_the_last_and_before_the_first_line(self) -> None:
        outcome = apply_line_edits(_FIVE, [
            edit(0, "insert_after", anchor(_FIVE, 5), lines=("f",)),
            edit(1, "insert_before", anchor(_FIVE, 1), lines=("0",)),
        ])

        assert outcome.content == "0\na\nb\nc\nd\ne\nf\n"
        assert outcome.spans == {1: (1, 1), 0: (7, 7)}

    def test_every_anchor_refers_to_the_file_before_the_call(self) -> None:
        outcome = apply_line_edits(_FIVE, [
            edit(0, "replace", anchor(_FIVE, 1), lines=("A",)),
            edit(1, "delete", anchor(_FIVE, 3)),
            edit(2, "insert_after", anchor(_FIVE, 5), lines=("f", "g")),
        ])

        assert outcome.content == "A\nb\nd\ne\nf\ng\n"
        assert outcome.spans == {0: (1, 1), 1: (3, 2), 2: (5, 6)}

    def test_inserts_right_before_and_after_a_replaced_range_stay_outside_it(self) -> None:
        outcome = apply_line_edits(_FIVE, [
            edit(0, "insert_before", anchor(_FIVE, 2), lines=("P",)),
            edit(1, "replace", anchor(_FIVE, 2), anchor(_FIVE, 3), ("X",)),
            edit(2, "insert_after", anchor(_FIVE, 3), lines=("Q",)),
        ])

        assert outcome.content == "a\nP\nX\nQ\nd\ne\n"
        assert outcome.spans == {0: (2, 2), 1: (3, 3), 2: (4, 4)}

    def test_a_file_without_a_final_newline_stays_that_way(self) -> None:
        outcome = apply_line_edits("a\nb", [edit(0, "insert_after", anchor("a\nb", 2), lines=("c",))])

        assert outcome.content == "a\nb\nc"

    def test_a_final_newline_survives_replacing_the_last_line(self) -> None:
        outcome = apply_line_edits("a\nb\n", [edit(0, "replace", anchor("a\nb\n", 2), lines=("B",))])

        assert outcome.content == "a\nB\n"

    def test_deleting_every_line_leaves_an_empty_file(self) -> None:
        outcome = apply_line_edits("a\nb\n", [edit(0, "delete", anchor("a\nb\n", 1), anchor("a\nb\n", 2))])

        assert outcome.content == ""

    def test_a_wrong_hash_changes_nothing_and_reports_the_current_one(self) -> None:
        good = anchor(_FIVE, 2)
        bad = Anchor(4, other_hash(line_hash("d")))

        outcome = apply_line_edits(_FIVE, [edit(0, "replace", good, lines=("B",)), edit(1, "delete", bad)])

        assert outcome.ok is False
        assert outcome.content == _FIVE and outcome.changed is False
        [mismatch] = outcome.mismatches
        assert (mismatch.index, mismatch.role, mismatch.actual) == (1, "start", line_hash("d"))

    def test_an_anchor_past_the_end_is_out_of_range(self) -> None:
        outcome = apply_line_edits(_FIVE, [edit(0, "replace", anchor(_FIVE, 4), Anchor(9, "abc"), ("x",))])

        [mismatch] = outcome.mismatches
        assert mismatch.role == "end" and mismatch.out_of_range
        assert outcome.total_lines == 5

    def test_an_empty_file_has_no_line_to_anchor_on(self) -> None:
        outcome = apply_line_edits("", [edit(0, "insert_after", Anchor(1, line_hash("")), lines=("x",))])

        assert outcome.ok is False and outcome.mismatches[0].out_of_range

    def test_an_identical_replacement_is_not_a_change(self) -> None:
        outcome = apply_line_edits(_FIVE, [edit(0, "replace", anchor(_FIVE, 2), lines=("b",))])

        assert outcome.ok is True and outcome.changed is False

    def test_trailing_whitespace_does_not_break_an_anchor(self) -> None:
        text = "keep   \nnext\n"

        outcome = apply_line_edits(text, [edit(0, "replace", Anchor(1, line_hash("keep")), lines=("kept",))])

        assert outcome.content == "kept\nnext\n"
