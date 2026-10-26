"""发给模型的请求视图：最近的工具结果在 token 预算内保留原文，更早的只留开头和结尾、保住落盘的 ref 行；
读过又重读或改写过的 read_file 结果换成一行说明，消息条数和调用配对永远不变。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.compact import LATEST_ROUND_FACTOR, TOOL_RESULT_VIEW_CHARS, bounded_tool_result_view  # noqa: E402
from engine.inference.llm_call import overflow_view, request_view, view_settings  # noqa: E402
from engine.inference.tool_format import (  # noqa: E402
    build_llm_messages,
    create_tool_formatter,
    repair_tool_result_pairing_with_stats,
)
from engine.tool_history import SUPERSEDED_READ_NOTE  # noqa: E402
from engine.tool_outputs import LATEST_ROUND_VIEW_CHARS, SavedOutput, excerpt_text, preview_text  # noqa: E402

_USER = {"role": "user", "content": "go"}


def _call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": dict(arguments or {})}


def _assistant(*calls: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant", "content": "", "tool_calls": list(calls),
        "_meta": {"tool_mode": "native", "message_type": "assistant"},
    }


def _result(call_id: str, name: str, content: str, status: str | None = "success") -> dict[str, Any]:
    meta: dict[str, Any] = {"tool_mode": "native", "message_type": "tool_result"}
    if status is not None:
        meta["tool_status"] = status
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content, "_meta": meta}


def _round(
    call_id: str, name: str, content: str, arguments: dict[str, Any] | None = None, status: str | None = "success",
) -> list[dict[str, Any]]:
    return [_assistant(_call(call_id, name, arguments)), _result(call_id, name, content, status)]


def _numbered(count: int, width: int = 40) -> str:
    return "\n".join(f"line-{index:05d} " + "x" * width for index in range(1, count + 1))


def _read_output(path: str, rest: str, rows: int = 3) -> str:
    body = "\n".join(f"{number:>4} | code {number}" for number in range(1, rows + 1))
    return f"── {path} ── {rest}\n{body}"


def _loop(tmp_path: Path, **fields: Any) -> SimpleNamespace:
    state = SimpleNamespace(
        runtime_cfg={}, compact_threshold=0, last_prompt_chars=0, last_prompt_tokens=None, task_digest=None,
        rctx=SimpleNamespace(workspace_root=tmp_path, task_id=""),
    )
    for key, value in fields.items():
        setattr(state, key, value)
    return state


class TestProtectionBudget:
    def test_results_inside_the_budget_stay_verbatim_and_the_boundary_is_inclusive(self) -> None:
        messages = [_USER]
        for number in range(3):
            messages += _round(f"c{number}", "probe", "a" * 3000)

        view, changed = bounded_tool_result_view(messages, protect_tokens=2000, chars_per_token=3.0)

        # 从最新往前：1000、2000（正好等于预算）保留，3000 超出
        assert view[6]["content"] == "a" * 3000
        assert view[4]["content"] == "a" * 3000
        assert view[2]["content"] != "a" * 3000
        assert len(view[2]["content"]) <= TOOL_RESULT_VIEW_CHARS
        assert changed == 1

    def test_protection_stops_at_the_first_result_over_budget(self) -> None:
        messages = [_USER, *_round("old", "probe", "b" * 5000), *_round("new", "probe", "c" * 30000)]

        view, _ = bounded_tool_result_view(messages, protect_tokens=2000, chars_per_token=3.0)

        # 更早那条单独算放得下，但最新那条已经超出，保护到此为止
        assert len(view[2]["content"]) <= TOOL_RESULT_VIEW_CHARS
        assert TOOL_RESULT_VIEW_CHARS < len(view[4]["content"]) <= LATEST_ROUND_VIEW_CHARS

    def test_the_estimate_follows_chars_per_token(self) -> None:
        messages = [_USER, *_round("old", "probe", "d" * 6000), *_round("new", "probe", "e" * 100)]

        loose, _ = bounded_tool_result_view(messages, protect_tokens=2000, chars_per_token=4.0)
        tight, _ = bounded_tool_result_view(messages, protect_tokens=2000, chars_per_token=1.0)

        assert loose[2]["content"] == "d" * 6000
        assert len(tight[2]["content"]) <= TOOL_RESULT_VIEW_CHARS

    def test_the_defaults_keep_the_old_bounds_and_never_touch_history(self) -> None:
        messages = [_USER, *_round("a", "probe", "x" * 5000), *_round("b", "probe", "y" * 20000)]

        view, changed = bounded_tool_result_view(messages)

        assert TOOL_RESULT_VIEW_CHARS * LATEST_ROUND_FACTOR == LATEST_ROUND_VIEW_CHARS
        assert len(view[2]["content"]) <= TOOL_RESULT_VIEW_CHARS
        assert TOOL_RESULT_VIEW_CHARS < len(view[4]["content"]) <= LATEST_ROUND_VIEW_CHARS
        assert changed == 2
        assert messages[2]["content"] == "x" * 5000
        assert messages[4]["content"] == "y" * 20000

    def test_the_latest_round_is_found_from_l1_tool_calls_and_legacy_meta_ids(self) -> None:
        legacy_result = 'Tool result for "probe":\n' + "z" * 6000
        messages = [
            _USER,
            *_round("a", "probe", "q" * 6000),
            {"role": "assistant", "content": "legacy round", "_meta": {"tool_call_ids": ["legacy-1"]}},
            {"role": "user", "content": legacy_result, "_meta": {"message_type": "tool_result"}},
            {"role": "assistant", "content": "thinking", "tool_calls": []},
        ]

        view, _ = bounded_tool_result_view(messages)

        assert view[4]["content"] == legacy_result
        assert len(view[2]["content"]) <= TOOL_RESULT_VIEW_CHARS

    def test_the_loop_state_settings_are_read_from_the_runtime_config(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, runtime_cfg={"engine": {"compact": {
            "tool_result_protect_tokens": 500, "supersede_reads": False,
        }}})

        settings = view_settings(loop)

        assert settings.protect_tokens == 500
        assert settings.supersede is False
        assert settings.chars_per_token == pytest.approx(3.0)
        assert settings.workspace_root == tmp_path

    @pytest.mark.parametrize("configured, threshold, expected", [
        (-5, 0, 0),
        (10_000_000, 0, 2_000_000),
        (40_000, 10_000, 4_000),
        (1_000, 10_000, 1_000),
    ])
    def test_the_protect_budget_is_clamped_and_capped_by_the_soft_threshold(
        self, tmp_path: Path, configured: int, threshold: int, expected: int,
    ) -> None:
        loop = _loop(
            tmp_path, compact_threshold=threshold,
            runtime_cfg={"engine": {"compact": {"tool_result_protect_tokens": configured}}},
        )

        assert view_settings(loop).protect_tokens == expected

    @pytest.mark.parametrize("chars, tokens, expected", [
        (12_000, 6_000, 2.0),
        (0, None, 3.0),
        (1_000, 100_000, 1.0),
    ])
    def test_chars_per_token_is_calibrated_from_real_usage(
        self, tmp_path: Path, chars: int, tokens: int | None, expected: float,
    ) -> None:
        loop = _loop(tmp_path, last_prompt_chars=chars, last_prompt_tokens=tokens)

        assert view_settings(loop).chars_per_token == pytest.approx(expected)

    def test_without_a_loop_state_the_request_view_keeps_the_fixed_bounds(self) -> None:
        messages = [_USER, *_round("a", "probe", "x" * 5000), *_round("b", "probe", "y" * 20000)]

        assert request_view(messages) == bounded_tool_result_view(messages)[0]


class TestHeadAndTailCut:
    def test_an_old_result_keeps_its_head_and_tail(self) -> None:
        text = _numbered(400)
        messages = [_USER, *_round("a", "probe", text), *_round("b", "probe", "ok")]

        cut = bounded_tool_result_view(messages)[0][2]["content"]

        assert cut.startswith("line-00001 ")
        assert cut.endswith(text.splitlines()[-1])
        assert "中间省略" in cut
        assert len(cut) <= TOOL_RESULT_VIEW_CHARS

    def test_both_ref_lines_survive_when_an_old_preview_is_cut(self) -> None:
        saved = SavedOutput(ref="to_" + "a" * 24, chars=99999, lines=2000)
        preview = preview_text(_numbered(2000), LATEST_ROUND_VIEW_CHARS, saved=saved)
        messages = [_USER, *_round("a", "probe", preview), *_round("b", "probe", "ok")]

        cut = bounded_tool_result_view(messages)[0][2]["content"]

        lines = preview.splitlines()
        assert lines[0] in cut and "ref=to_" in lines[0]
        assert lines[-1] in cut and "ref=to_" in lines[-1]
        assert len(cut) <= TOOL_RESULT_VIEW_CHARS

    def test_a_ref_line_in_the_middle_is_kept(self) -> None:
        ref_line = "[输出过长，完整内容已保存为 ref=to_" + "b" * 24 + "（共 50000 字符 / 900 行），下面只放开头和结尾]"
        lines = [f"head-{index:04d} " + "h" * 60 for index in range(60)]
        lines.append(ref_line)
        lines.extend(f"tail-{index:04d} " + "t" * 60 for index in range(60))
        text = "\n".join(lines)

        cut = excerpt_text(text, 2000)

        assert ref_line in cut
        assert cut.startswith("head-0000 ")
        assert cut.endswith(lines[-1])
        assert len(cut) <= 2000

    @pytest.mark.parametrize("budget", [300, 1000, 2000, 8000])
    def test_the_excerpt_stays_within_its_budget(self, budget: int) -> None:
        assert len(excerpt_text(_numbered(500), budget)) <= budget

    def test_short_text_is_returned_untouched(self) -> None:
        assert excerpt_text("short", 100) == "short"

    def test_a_tiny_budget_falls_back_to_a_head_cut(self) -> None:
        cut = excerpt_text("z" * 5000, 100)

        assert cut.startswith("zzz")
        assert len(cut) <= 100

    def test_a_ref_buried_in_one_long_line_is_kept_without_the_whole_line(self) -> None:
        ref = "ref=to_" + "f" * 24
        text = '{"data": "' + "j" * 15000 + f" {ref} " + "k" * 15000 + '"}'

        cut = excerpt_text(text, 2000)

        assert ref in cut
        assert len(cut) <= 2000

    def test_a_ref_line_is_kept_even_when_the_budget_cannot_hold_it(self) -> None:
        ref_line = "[完整输出共 50000 字符 / 900 行，已保存为 ref=to_" + "c" * 24 + "，可用 read_tool_output 按行读取或搜索]"

        cut = excerpt_text(ref_line + "\n" + "y" * 5000, 60)

        assert ref_line in cut


class TestSupersededReads:
    def test_a_reread_that_covers_the_old_range_supersedes_it(self) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("src/app.py", "文件共 300 行，显示 10-40 行"),
                    {"files": [{"path": "src/app.py", "startLine": 10, "endLine": 40}]}),
            *_round("r2", "read_file", _read_output("src/app.py", "共 300 行"), {"files": [{"path": "src/app.py"}]}),
        ]

        view, changed = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="src/app.py")
        assert view[4]["content"] == messages[4]["content"]
        assert changed == 1

    def test_a_narrower_reread_does_not_supersede(self) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("src/app.py", "共 300 行"), {"path": "src/app.py"}),
            *_round("r2", "read_file", _read_output("src/app.py", "文件共 300 行，显示 10-20 行"),
                    {"path": "src/app.py", "start_line": 10, "end_line": 20}),
        ]

        view, changed = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]
        assert changed == 0

    def test_a_truncated_default_read_is_covered_by_a_later_full_read(self) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file",
                    _read_output("big.py", "文件共 900 行，只显示了 1-500 行；继续读用 startLine=501"),
                    {"files": [{"path": "big.py"}]}),
            *_round("r2", "read_file", _read_output("big.py", "共 900 行"),
                    {"files": [{"path": "big.py", "startLine": 1}]}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="big.py")

    @pytest.mark.parametrize("tool, arguments", [
        ("write_file", {"path": "a.py", "content": "new"}),
        ("apply_diff", {"path": "a.py", "diffs": [{"search": "x", "replace": "y"}]}),
        ("edit_lines", {"path": "a.py", "edits": []}),
    ])
    def test_a_successful_rewrite_supersedes_earlier_reads(self, tool: str, arguments: dict[str, Any]) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("w1", tool, "ok", arguments),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="a.py")

    @pytest.mark.parametrize("status", ["error", "blocked", "cancelled", "async_started"])
    def test_a_rewrite_that_did_not_succeed_does_not_supersede(self, status: str) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("w1", "write_file", "ERROR: refused", {"path": "a.py", "content": "new"}, status=status),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]

    def test_a_rewrite_without_a_recorded_status_does_not_supersede(self) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("w1", "write_file", "ok", {"path": "a.py", "content": "new"}, status=None),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]

    def test_a_multi_file_read_waits_until_every_file_is_superseded(self) -> None:
        both = _read_output("a.py", "共 5 行") + "\n" + _read_output("b.py", "共 5 行")
        messages = [
            _USER,
            *_round("r1", "read_file", both, {"files": [{"path": "a.py"}, {"path": "b.py"}]}),
            *_round("w1", "write_file", "ok", {"path": "a.py", "content": "x"}),
        ]

        assert bounded_tool_result_view(messages, supersede=True)[0][2]["content"] == both

        messages += _round("w2", "apply_diff", "ok", {"path": "b.py", "diffs": []})
        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="a.py、b.py")

    def test_paths_are_compared_after_normalization(self, tmp_path: Path) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("./src/../src/mod.py", "共 3 行"), {"path": "./src/../src/mod.py"}),
            *_round("w1", "write_file", "ok", {"path": str(tmp_path / "src" / "mod.py"), "content": "x"}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True, workspace_root=tmp_path)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="./src/../src/mod.py")

    def test_a_search_replace_supersedes_the_files_it_lists(self) -> None:
        replaced = "2 files modified, 3 replacements\npkg/a.py: 2 replacements\npkg/b.py: 1 replacements"
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("pkg/a.py", "共 8 行"), {"path": "pkg/a.py"}),
            *_round("r2", "read_file", _read_output("pkg/c.py", "共 8 行"), {"path": "pkg/c.py"}),
            *_round("s1", "search_in_files", replaced, {"query": "old", "replace": "new", "mode": "replace", "path": "pkg"}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="pkg/a.py")
        assert view[4]["content"] == messages[4]["content"]

    def test_a_plain_search_supersedes_nothing(self) -> None:
        listing = "2 files modified, 3 replacements\npkg/a.py: 2 replacements"
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("pkg/a.py", "共 8 行"), {"path": "pkg/a.py"}),
            *_round("s1", "search_in_files", listing, {"query": "old", "path": "pkg"}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]

    @pytest.mark.parametrize("arguments, superseded", [
        ({"paths": ["a.py"]}, True),
        ({"action": "undo", "paths": ["other.py"]}, False),
        ({}, False),
        ({"action": "list", "paths": ["a.py"]}, False),
    ])
    def test_undo_edits_supersedes_only_the_paths_it_names(self, arguments: dict[str, Any], superseded: bool) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("u1", "undo_edits", "restored", arguments),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert (view[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="a.py")) is superseded

    def test_a_reread_whose_header_is_missing_cannot_cover(self) -> None:
        spilled = "[输出过长，完整内容已保存为 ref=to_" + "e" * 24 + "（共 90000 字符 / 900 行），下面只放开头和结尾]\n   1 | x"
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("r2", "read_file", spilled, {"path": "a.py"}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]

    def test_reused_call_ids_resolve_to_the_nearest_call(self) -> None:
        messages = [
            _USER,
            *_round("call_0", "read_file", _read_output("a.py", "共 4 行"), {"path": "a.py"}),
            *_round("call_0", "read_file", _read_output("b.py", "共 4 行"), {"path": "b.py"}),
            *_round("call_0", "write_file", "ok", {"path": "b.py", "content": "x"}),
        ]

        view, _ = bounded_tool_result_view(messages, supersede=True)

        assert view[2]["content"] == messages[2]["content"]
        assert view[4]["content"] == SUPERSEDED_READ_NOTE.format(paths="b.py")

    def test_the_view_keeps_every_message_and_its_pairing(self) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("x1", "probe", "p" * 9000),
            *_round("w1", "write_file", "ok", {"path": "a.py", "content": "x"}),
            *_round("x2", "probe", "q" * 30000),
        ]

        view, changed = bounded_tool_result_view(messages, supersede=True, protect_tokens=100, chars_per_token=3.0)

        assert changed == 3
        assert len(view) == len(messages)
        for shown, original in zip(view, messages):
            for key in ("role", "tool_call_id", "name", "tool_calls"):
                assert shown.get(key) == original.get(key)
        _, stats = repair_tool_result_pairing_with_stats(view)
        assert stats["removed_orphan_tool_results"] == 0
        assert stats["fixed_orphan_tool_calls"] == 0
        replayed = build_llm_messages(view, create_tool_formatter("native"))
        assert [message.get("tool_call_id") for message in replayed if message.get("role") == "tool"] == [
            "r1", "x1", "w1", "x2",
        ]

    def test_the_switch_turns_superseding_off(self, tmp_path: Path) -> None:
        messages = [
            _USER,
            *_round("r1", "read_file", _read_output("a.py", "共 10 行"), {"path": "a.py"}),
            *_round("w1", "write_file", "ok", {"path": "a.py", "content": "x"}),
        ]

        on = request_view(messages, _loop(tmp_path))
        off = request_view(messages, _loop(tmp_path, runtime_cfg={"engine": {"compact": {"supersede_reads": False}}}))

        assert on[2]["content"] == SUPERSEDED_READ_NOTE.format(paths="a.py")
        assert off[2]["content"] == messages[2]["content"]


class TestOverflowView:
    def test_the_retry_after_an_overflow_cuts_the_latest_round_but_keeps_its_refs(self, tmp_path: Path) -> None:
        saved = SavedOutput(ref="to_" + "d" * 24, chars=1, lines=1)
        preview = preview_text(_numbered(2000), LATEST_ROUND_VIEW_CHARS, saved=saved)
        messages = [_USER, *_round("a", "probe", preview)]
        loop = _loop(tmp_path)

        normal = request_view(messages, loop)
        retry = overflow_view(messages, loop)

        assert normal[2]["content"] == preview
        assert len(retry[2]["content"]) <= TOOL_RESULT_VIEW_CHARS
        assert preview.splitlines()[0] in retry[2]["content"]
        assert preview.splitlines()[-1] in retry[2]["content"]
