"""apply_diff 的多级匹配：每一级都要命中该命中的、放过不该命中的。

多处命中靠 start_line 消歧，消不了就报候选行；模糊匹配的范围远大于 search 时拒绝；找不到时给出最接近的片段。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox import edit_match  # noqa: E402
from toolbox.edit_match import (  # noqa: E402
    ANCHOR,
    BLANK_TRIMMED,
    EXACT,
    INDENTATION,
    LINE_TRIMMED,
    UNESCAPED,
    WHITESPACE,
    DiffRequest,
    Failure,
    Match,
    apply_diffs,
    find_match,
    line_range,
)


def apply(content: str, match: Match | Failure) -> str:
    assert isinstance(match, Match), match
    return content[:match.start] + match.replacement + content[match.end:]


def failure_of(result: Match | Failure) -> Failure:
    assert isinstance(result, Failure), result
    return result


class TestTheOrderOfLevels:
    def test_indentation_is_tried_before_the_looser_line_trimmed_level(self) -> None:
        # 缩进弹性的命中条件是逐行去首尾空白的子集：排在后面就永远轮不到
        assert edit_match.LEVELS.index(INDENTATION) < edit_match.LEVELS.index(LINE_TRIMMED)

    def test_every_level_appears_once(self) -> None:
        assert len(set(edit_match.LEVELS)) == len(edit_match.LEVELS) == 7


class TestExact:
    def test_a_unique_exact_match_is_used(self) -> None:
        content = "a = 1\nb = 2\nc = 3\n"

        match = find_match(content, "b = 2\n", "b = 20\n")

        assert isinstance(match, Match)
        assert match.level is EXACT and match.via is None
        assert apply(content, match) == "a = 1\nb = 20\nc = 3\n"

    def test_a_unique_match_ignores_a_stale_start_line(self) -> None:
        content = "a = 1\nb = 2\nc = 3\n"

        match = find_match(content, "c = 3", "c = 30", start_line=1)

        assert apply(content, match) == "a = 1\nb = 2\nc = 30\n"

    def test_several_matches_without_start_line_report_the_candidate_lines(self) -> None:
        content = "x = 1\ny = 2\nx = 1\n"

        failure = failure_of(find_match(content, "x = 1", "x = 9"))

        assert failure.reason == "ambiguous"
        assert failure.level is EXACT
        assert failure.candidate_lines == (1, 3)
        assert failure.total == 2
        assert "1、3" in failure.message

    def test_start_line_takes_the_first_match_at_or_after_it(self) -> None:
        content = "x = 1\ny = 2\nx = 1\n"

        match = find_match(content, "x = 1", "x = 9", start_line=2)

        assert apply(content, match) == "x = 1\ny = 2\nx = 9\n"
        assert isinstance(match, Match)
        assert any("start_line" in note for note in match.notes)

    def test_start_line_past_every_match_is_rejected_with_the_candidates(self) -> None:
        content = "x = 1\nx = 1\ny = 2\nz = 3\n"

        failure = failure_of(find_match(content, "x = 1", "x = 9", start_line=3))

        assert failure.reason == "no_match_after_start"
        assert failure.candidate_lines == (1, 2)

    def test_start_line_out_of_range_is_rejected(self) -> None:
        failure = failure_of(find_match("x = 1\nx = 1\n", "x = 1", "x = 9", start_line=99))

        assert failure.reason == "out_of_range"
        assert failure.candidate_lines == (1, 2)

    def test_crlf_in_search_and_replace_counts_as_lf(self) -> None:
        content = "a\nb\nc\n"

        match = find_match(content, "a\r\nb\r\n", "A\r\nB\r\n")

        assert isinstance(match, Match) and match.level is EXACT
        assert apply(content, match) == "A\nB\nc\n"

    def test_an_empty_search_is_rejected(self) -> None:
        assert failure_of(find_match("abc", "", "x")).reason == "empty_search"

    def test_a_search_that_matches_everywhere_is_refused(self) -> None:
        content = "a\n" * (edit_match.HIT_LIMIT + 5)

        failure = failure_of(find_match(content, "a", "b"))

        assert failure.reason == "too_many"


class TestIndentation:
    CONTENT = "def f():\n    if x:\n        return 1\n    return 2\n"

    def test_a_block_written_without_its_indentation_is_reindented_to_the_target(self) -> None:
        match = find_match(self.CONTENT, "if x:\n    return 1\n", "if x:\n    return 10\n")

        assert isinstance(match, Match) and match.level is INDENTATION
        assert apply(self.CONTENT, match) == "def f():\n    if x:\n        return 10\n    return 2\n"
        assert any("缩进重排" in note for note in match.notes)

    def test_an_over_indented_search_is_shifted_back(self) -> None:
        content = "items:\n  - a\n  - b\n"

        match = find_match(content, "    - a\n    - b\n", "    - a\n    - c\n")

        assert isinstance(match, Match) and match.level is INDENTATION
        assert apply(content, match) == "items:\n  - a\n  - c\n"

    def test_a_block_whose_relative_indentation_differs_is_not_an_indentation_match(self) -> None:
        content = "if a:\n    b()\nc()\n"

        match = find_match(content, "if a:\nb()\nc()\n", "if a:\nb2()\nc()\n")

        assert isinstance(match, Match)
        assert match.level is LINE_TRIMMED
        assert any("缩进和 search 不一致" in note for note in match.notes)
        # 缩进推断不出来时 replace 照原样写，不替模型猜
        assert apply(content, match) == "if a:\nb2()\nc()\n"


class TestLineTrimmed:
    def test_trailing_whitespace_differences_match_line_by_line(self) -> None:
        content = "a = 1   \nb = 2\t\nc = 3\n"

        match = find_match(content, "a = 1\nb = 2\n", "a = 10\nb = 20\n")

        assert isinstance(match, Match)
        assert match.level is LINE_TRIMMED
        assert match.notes == ()
        assert apply(content, match) == "a = 10\nb = 20\nc = 3\n"

    def test_different_text_inside_a_line_is_not_a_trimmed_match(self) -> None:
        failure = failure_of(find_match("a = 1\nb = 2\n", "a = 1\nb = 3\n", "x"))

        assert failure.reason == "not_found"


class TestWhitespace:
    def test_runs_of_whitespace_collapse_line_by_line(self) -> None:
        content = "def f(a,  b):\n    return a  +  b\n"

        match = find_match(content, "def f(a, b):\n    return a + b\n", "def f(a, b):\n    return a - b\n")

        assert isinstance(match, Match) and match.level is WHITESPACE
        assert apply(content, match) == "def f(a, b):\n    return a - b\n"

    def test_a_single_line_search_matches_inside_a_line(self) -> None:
        content = "x = compute(a,   b) + 1\n"

        match = find_match(content, "compute(a, b)", "compute(a, c)")

        assert isinstance(match, Match) and match.level is WHITESPACE
        assert apply(content, match) == "x = compute(a, c) + 1\n"

    def test_the_same_words_in_another_order_do_not_match(self) -> None:
        assert failure_of(find_match("x = compute(b, a)\n", "compute(a, b)", "y")).reason == "not_found"

    def test_a_single_line_search_does_not_reach_across_lines(self) -> None:
        assert failure_of(find_match("compute(a,\n    b)\n", "compute(a, b)", "y")).reason == "not_found"


class TestUnescaped:
    def test_double_escaped_newlines_and_quotes_are_read_as_real_characters(self) -> None:
        content = 'msg = "hi"\nprint(msg)\n'
        search = 'msg = \\"hi\\"\\nprint(msg)'
        replace = 'msg = \\"bye\\"\\nprint(msg)'

        match = find_match(content, search, replace)

        assert isinstance(match, Match)
        assert match.level is UNESCAPED and match.via is EXACT
        assert apply(content, match) == 'msg = "bye"\nprint(msg)\n'

    def test_escapes_that_really_are_in_the_file_stay_literal(self) -> None:
        content = 'print("a\\nb")\n'

        match = find_match(content, 'print("a\\nb")', 'print("a\\nc")')

        assert isinstance(match, Match) and match.level is EXACT
        assert apply(content, match) == 'print("a\\nc")\n'

    def test_unescaping_does_not_invent_a_match(self) -> None:
        assert failure_of(find_match("a\nb\n", "a\\nc", "x")).reason == "not_found"


class TestBlankTrimmed:
    CONTENT = "def a():\n    pass\ndef b():\n    pass\n"

    def test_blank_lines_around_the_search_are_ignored(self) -> None:
        match = find_match(self.CONTENT, "\n\ndef b():\n    pass\n\n\n", "\n\ndef b():\n    return 1\n\n\n")

        assert isinstance(match, Match)
        assert match.level is BLANK_TRIMMED and match.via is EXACT
        assert apply(self.CONTENT, match) == "def a():\n    pass\ndef b():\n    return 1\n"

    def test_a_blank_line_inside_the_search_is_not_trimmed_away(self) -> None:
        assert failure_of(find_match(self.CONTENT, "def b():\n\n    pass\n", "x")).reason == "not_found"


class TestAnchor:
    CONTENT = (
        "def total(items):\n"
        "    result = 0\n"
        "    for item in items:\n"
        "        result += item.price\n"
        "    return result\n"
    )

    def test_first_and_last_lines_anchor_a_block_whose_middle_drifted(self) -> None:
        search = (
            "def total(items):\n"
            "    result = 0\n"
            "    for item in items:\n"
            "        result += item.cost\n"
            "    return result\n"
        )
        replace = "def total(items):\n    return sum(item.price for item in items)\n"

        match = find_match(self.CONTENT, search, replace)

        assert isinstance(match, Match) and match.level is ANCHOR
        assert match.similarity is not None and match.similarity >= edit_match.ANCHOR_MIN_SIMILARITY
        assert apply(self.CONTENT, match) == "def total(items):\n    return sum(item.price for item in items)\n"

    def test_a_middle_that_is_mostly_different_is_not_anchored(self) -> None:
        search = (
            "def total(items):\n"
            "    unrelated = fetch_everything()\n"
            "    while True:\n"
            "        break_out_somehow()\n"
            "    return result\n"
        )

        assert failure_of(find_match(self.CONTENT, search, "x")).reason == "not_found"

    def test_a_block_much_longer_than_the_search_is_not_anchored(self) -> None:
        content = "begin\n" + "".join(f"    step{i}()\n" for i in range(10)) + "end\n"

        failure = failure_of(find_match(content, "begin\n    step0()\n    step1()\nend\n", "x"))

        assert failure.reason == "not_found"


class TestDisproportion:
    def test_an_anchor_block_far_larger_than_the_search_is_rejected(self) -> None:
        # 相似度只比每行开头一段：开头一样、后面拖着一大截的行能骗过相似度，靠范围比例拦下
        head = "x" * 130
        tail = "y" * 400
        content = f"start()\n    {head}{tail}\n    {head}{tail}\nfinish()\n"
        search = f"start()\n    {head}\n    {head}\nfinish()\n"

        failure = failure_of(find_match(content, search, "start()\nfinish()\n"))

        assert failure.reason == "disproportionate"
        assert "不成比例" in failure.message
        assert failure.level is ANCHOR


class TestAmbiguityAtFuzzyLevels:
    CONTENT = "class A:\n    def run(self):\n        pass\n\nclass B:\n    def run(self):\n        pass\n"

    def test_several_fuzzy_matches_report_the_candidates(self) -> None:
        failure = failure_of(find_match(self.CONTENT, "def run(self):\n    pass\n", "def run(self):\n    return 1\n"))

        assert failure.reason == "ambiguous"
        assert failure.level is INDENTATION
        assert failure.candidate_lines == (2, 6)

    def test_start_line_disambiguates_a_fuzzy_level(self) -> None:
        match = find_match(
            self.CONTENT, "def run(self):\n    pass\n", "def run(self):\n    return 1\n", start_line=5,
        )

        assert isinstance(match, Match) and match.level is INDENTATION
        edited = apply(self.CONTENT, match)
        assert edited == "class A:\n    def run(self):\n        pass\n\nclass B:\n    def run(self):\n        return 1\n"


class TestTheClosestCandidate:
    def test_not_found_points_at_the_most_similar_block(self) -> None:
        content = "alpha()\nbeta(1, 2)\ngamma()\ndelta()\n"

        failure = failure_of(find_match(content, "beta(1, 3)\ngamma()\n", "x"))

        assert failure.reason == "not_found"
        assert failure.closest is not None
        assert (failure.closest.first, failure.closest.last) == (1, 2)
        assert failure.closest.similarity > 0.8

    def test_nothing_alike_gives_no_candidate(self) -> None:
        failure = failure_of(find_match("one\ntwo\nthree\n", "completely-unrelated-text-here", "x"))

        assert failure.reason == "not_found"
        assert failure.closest is None


class TestApplyingSeveralDiffs:
    def test_later_diffs_see_earlier_ones_and_start_line_is_compensated(self) -> None:
        outcome = apply_diffs("a\nb\nx\nc\nx\n", [
            DiffRequest(0, "a\n", "a\na2\na3\n"),
            DiffRequest(1, "x\n", "y\n", start_line=5),
        ])

        assert outcome.ok and outcome.changed
        assert outcome.content == "a\na2\na3\nb\nx\nc\ny\n"

    def test_a_failed_diff_does_not_stop_the_next_ones_from_being_checked(self) -> None:
        outcome = apply_diffs("one\ntwo\nthree\n", [
            DiffRequest(0, "nope-nothing-like-this", "x"),
            DiffRequest(1, "two", "TWO"),
        ])

        assert not outcome.ok
        assert [item.index for item in outcome.failed] == [0]
        assert [item.index for item in outcome.applied] == [1]

    def test_candidate_lines_use_the_numbering_the_model_read(self) -> None:
        outcome = apply_diffs("top\nx = 1\nmid\nx = 1\n", [
            DiffRequest(0, "top\n", "top\nadded1\nadded2\n"),
            DiffRequest(1, "x = 1", "x = 2"),
        ])

        failure = outcome.failed[0].failure
        assert failure.reason == "ambiguous"
        # 内存里已经多了两行，报给模型的仍是它读文件时的行号
        assert failure.candidate_lines == (2, 4)
        assert outcome.failed[0].line_delta == 2

    def test_spans_follow_the_final_content(self) -> None:
        outcome = apply_diffs("a\nb\nc\nd\n", [
            DiffRequest(0, "c\n", "c1\nc2\n"),
            DiffRequest(1, "a\n", "A\nA2\n"),
        ])

        start, end = outcome.spans[0]
        assert outcome.content[start:end] == "c1\nc2\n"
        start, end = outcome.spans[1]
        assert outcome.content[start:end] == "A\nA2\n"

    def test_an_edit_inside_an_earlier_edit_merges_their_spans(self) -> None:
        outcome = apply_diffs("a\nb\nc\n", [
            DiffRequest(0, "b\n", "b1\nb2\nb3\n"),
            DiffRequest(1, "b2\n", "B2\n"),
        ])

        start, end = outcome.spans[0]
        assert outcome.content[start:end] == "b1\nB2\nb3\n"
        assert line_range(outcome.content, *outcome.spans[1]) == (3, 3)

    def test_identical_replacements_leave_the_content_unchanged(self) -> None:
        outcome = apply_diffs("a\nb\n", [DiffRequest(0, "a\n", "a\n")])

        assert outcome.ok
        assert not outcome.changed


class TestLineRange:
    @pytest.mark.parametrize(
        "content,start,end,expected",
        [
            ("a\nb\nc\n", 2, 4, (2, 2)),
            ("a\nb\nc\n", 0, 5, (1, 3)),
            ("a\nb\nc\n", 2, 2, (2, 2)),
            ("", 0, 0, (1, 1)),
        ],
    )
    def test_spans_map_to_lines(self, content: str, start: int, end: int, expected: tuple[int, int]) -> None:
        assert line_range(content, start, end) == expected
