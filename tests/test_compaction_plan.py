from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.compaction_plan import (
    build_compaction_plan,
    compressed_task_ids,
    fully_covered_task_ids,
    covered_indices,
    record_fingerprint,
    replace_covered,
)
from engine.conversation_store import Message


def test_late_records_prevent_marking_the_whole_task_compressed():
    records = [
        {"role": "user", "content": "first", "_meta": {"source_task_id": "old"}},
        {"role": "assistant", "content": "answer", "_meta": {"source_task_id": "old"}},
        {"role": "user", "content": "recent", "_meta": {"source_task_id": "recent"}},
        {"role": "assistant", "content": "late result", "_meta": {"source_task_id": "old"}},
    ]
    assert fully_covered_task_ids(records[:3], [0, 1]) == ["old"]
    assert fully_covered_task_ids(records, [0, 1]) == []


def message(index: int, task: str | None = None, **values: object) -> dict:
    return {
        "id": f"message-{index}",
        "role": "user",
        "content": f"消息 {index}",
        "source_task_id": f"task-{index}" if task is None else task,
        "created_at": f"2026-10-21T12:00:{index:02d}+08:00",
        **values,
    }


def selected(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


def test_complete_records_and_object_fingerprints_preserve_tool_metadata() -> None:
    first = Message.from_dict(message(
        1, "old", content=[{"type": "text", "text": "原文"}, {"type": "image_url", "image_url": {"url": "image.png"}}],
        meta={"provider": "gemini", "reasoning": "完整思考"},
    ))
    call = message(2, "old", role="assistant", tool_calls=[
        {"id": "call1", "function": {"name": "read_file", "arguments": '{"path":"原始.py"}'}},
    ])
    result = message(3, "old", role="tool", tool_call_id="call1", content="完整工具结果", name="read_file")
    current = message(4)
    plan, text = build_compaction_plan([first, call, result, current], keep_recent=1)
    assert selected(text) == [first.to_dict(), call, result]
    assert record_fingerprint(first) == record_fingerprint(first.to_dict())
    assert covered_indices([first, call, result, current], plan) == [0, 1, 2]


def test_summary_and_old_segments_share_the_same_exact_coverage() -> None:
    previous = message(0, "compact_summary", content="已有摘要")
    messages = [previous, message(1), message(2), message(3)]
    plan, text = build_compaction_plan(messages, keep_recent=1)
    assert selected(text) == messages[:3]
    assert plan["total_segments"] == 3
    assert plan["compressed_segments"] == 2
    summary = message(9, "compact_summary", content="新摘要")
    assert replace_covered(messages, plan, summary) == [summary, messages[-1]]


def test_interleaved_summary_keeps_original_chronological_order() -> None:
    messages = [message(1), message(0, "compact_summary"), message(2), message(3)]
    plan, text = build_compaction_plan(messages, keep_recent=1)
    assert selected(text) == messages[:3]
    assert covered_indices(messages, plan) == [0, 1, 2]


def test_system_dynamic_and_ephemeral_messages_are_retained_outside_coverage() -> None:
    messages = [
        message(0, role="system"), message(1), message(2, _dynamic=True),
        message(3, _ephemeral=True), Message.from_dict(message(4, ephemeral=True)), message(5),
    ]
    plan, text = build_compaction_plan(messages, keep_recent=1)
    assert selected(text) == [messages[1]]
    replacement = message(9, "compact_summary")
    assert replace_covered(messages, plan, replacement) == [messages[0], replacement, *messages[2:]]


def test_delayed_summary_preserves_appended_messages_without_recounting_segments() -> None:
    original = [message(i) for i in range(1, 5)]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    newer = [message(5), message(6), message(7)]
    summary = message(8, "compact_summary")
    assert replace_covered(original + newer, plan, summary) == [summary, original[-1], *newer]


def test_uncovered_tail_may_change_or_be_absent_in_a_branch() -> None:
    original = [message(i) for i in range(1, 5)]
    plan, _ = build_compaction_plan(original, keep_recent=2)
    changed = original[:2] + [message(9, "compact_summary")]
    assert covered_indices(changed, plan) == [0, 1]
    assert covered_indices(original[:2], plan) == [0, 1]


@pytest.mark.parametrize("change", ["missing", "text", "time", "metadata", "order", "insert"])
def test_modified_covered_snapshot_is_rejected(change: str) -> None:
    original = [message(i) for i in range(1, 5)]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    current = copy.deepcopy(original)
    if change == "missing":
        del current[1]
    elif change == "text":
        current[1]["content"] = "改写原文"
    elif change == "time":
        current[1]["created_at"] = "2026-10-21T00:00:00Z"
    elif change == "metadata":
        current[1]["meta"] = {"added": True}
    elif change == "order":
        current[0], current[1] = current[1], current[0]
    else:
        current.insert(1, message(9))
    assert covered_indices(current, plan) is None
    assert replace_covered(current, plan, message(8, "compact_summary")) is None


def test_removed_record_cannot_be_substituted_by_an_identical_appended_record() -> None:
    old = {"role": "user", "content": "相同内容", "source_task_id": "old"}
    original = [old, message(1), message(2)]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    assert covered_indices([*original[1:], old], plan) is None


def test_duplicate_records_are_each_covered_once_at_their_original_positions() -> None:
    old = message(1, "old")
    original = [old, copy.deepcopy(old), message(2)]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:2]
    assert covered_indices(original, plan) == [0, 1]
    assert covered_indices([old, original[-1]], plan) is None


@pytest.mark.parametrize("result_task", ["", "wrong-task"])
def test_tool_call_and_result_crossing_task_ids_stay_in_one_segment(result_task: str) -> None:
    call = message(2, "call-task", role="assistant", tool_calls=[{"id": "call1", "name": "read_file", "arguments": {}}])
    result = message(4, result_task, role="tool", tool_call_id="call1")
    original = [message(1), call, message(3, "intervening-task"), result]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:1]
    assert plan["total_segments"] == 2
    assert replace_covered(original, plan, message(8, "compact_summary"))[-3:] == original[-3:]
    plan, text = build_compaction_plan(original + [message(5)], keep_recent=1)
    assert selected(text) == original


def test_multiple_tool_calls_merge_all_crossed_boundaries() -> None:
    call = message(2, role="assistant", tool_calls=[{"id": "a"}, {"id": "b"}])
    original = [
        message(1), call, message(3, role="tool", tool_call_id="a"),
        message(4), message(5, role="tool", tool_call_id="b"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == [original[0]]
    assert plan["total_segments"] == 2


def test_unfinished_tool_call_is_retained_for_late_results() -> None:
    original = [message(1), message(2, role="assistant", tool_calls=[{"id": "pending"}]), message(3)]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == [original[0]]
    later = original + [message(4, role="tool", tool_call_id="pending")]
    assert replace_covered(later, plan, message(8, "compact_summary"))[1:] == later[1:]


def test_legacy_metadata_tool_ids_also_keep_pairs_together() -> None:
    original = [
        message(1), message(2, role="assistant", tool_calls=[], meta={"tool_call_ids": ["legacy"]}),
        message(3, role="tool", tool_call_id="legacy"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == [original[0]]


def test_size_limit_keeps_whole_segments_and_never_truncates_input() -> None:
    original = [message(1, content="a" * 200_000), message(2, content="b" * 120_000), message(3)]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert len(text) <= 300_000
    assert selected(text) == original[:1]
    assert plan["compressed_segments"] == 1
    assert replace_covered(original, plan, message(8, "compact_summary"))[1:] == original[1:]


def test_first_oversized_segment_prevents_skipping_into_newer_history() -> None:
    original = [message(1, content="a" * 300_001), message(2), message(3)]
    assert build_compaction_plan(original, keep_recent=1) == ({}, "")


def test_oversized_old_task_compacts_only_a_whole_message_prefix() -> None:
    original = [
        message(1, "old", content="a" * 150_000),
        message(2, "old", content="b" * 150_000),
        message(3, "old", content="旧任务结论"),
        message(4, "current"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:1]
    assert len(text) <= 300_000
    assert plan["partial_segment"] is True
    assert plan["full_task_ids"] == []
    assert replace_covered(original, plan, message(8, "compact_summary"))[1:] == original[1:]


def test_partial_old_task_preserves_complete_tool_dependency_closures() -> None:
    original = [
        message(1, "old", content="a" * 30_000),
        message(2, "old", role="assistant", tool_calls=[{"id": "a"}, {"id": "b"}]),
        message(3, "old", role="tool", tool_call_id="a", content="a" * 80_000),
        message(4, "old", role="tool", tool_call_id="b", content="b" * 80_000),
        message(5, "old", content="c" * 150_000),
        message(6, "current"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:4]
    assert plan["partial_segment"] is True
    assert plan["full_task_ids"] == []
    assert replace_covered(original, plan, message(8, "compact_summary"))[1:] == original[4:]


def test_partial_prefix_stops_before_tool_group_that_exceeds_remaining_budget() -> None:
    original = [
        message(1, "old", content="a" * 200_000),
        message(2, "old", role="assistant", tool_calls=[{"id": "a"}]),
        message(3, "old", role="tool", tool_call_id="a", content="b" * 150_000),
        message(4, "current"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:1]
    assert covered_indices(original, plan) == [0]


def test_oversized_first_tool_dependency_group_cannot_be_partially_deleted() -> None:
    original = [
        message(1, "old", role="assistant", tool_calls=[{"id": "a"}]),
        message(2, "old", role="tool", tool_call_id="a", content="b" * 300_000),
        message(3, "old", content="可压的小消息"),
        message(4, "current"),
    ]
    assert build_compaction_plan(original, keep_recent=1) == ({}, "")


def test_full_older_segments_are_selected_before_attempting_a_partial_task() -> None:
    original = [
        message(1, "older"),
        message(2, "old", content="a" * 150_000),
        message(3, "old", content="b" * 150_000),
        message(4, "current"),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:1]
    assert plan["partial_segment"] is False
    assert plan["full_task_ids"] == ["older"]


def test_partial_task_can_progress_over_multiple_compactions_without_losing_tail() -> None:
    original = [
        message(1, "old", content="a" * 150_000), message(2, "old", content="b" * 150_000),
        message(3, "old", content="c" * 150_000), message(4, "current"),
    ]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    assert plan["full_task_ids"] == []
    first = replace_covered(original, plan, message(8, "compact_summary", content="第一段摘要"))
    second_plan, text = build_compaction_plan(first, keep_recent=1)
    assert selected(text) == first[:2]
    assert second_plan["full_task_ids"] == []
    second = replace_covered(first, second_plan, message(9, "compact_summary", content="前两段摘要"))
    final_plan, text = build_compaction_plan(second, keep_recent=1)
    assert selected(text) == second[:2]
    assert final_plan["partial_segment"] is False
    assert final_plan["full_task_ids"] == ["old"]
    final = replace_covered(second, final_plan, message(10, "compact_summary", content="完整旧任务摘要"))
    assert final[-1] == original[-1]
    assert len(final) == 2


def test_prior_complete_ids_survive_but_discontinuous_uncovered_tasks_are_not_marked_complete() -> None:
    original = [
        message(0, "compact_summary", meta={"compressed_task_ids": ["previous", "resumed"]}),
        message(1, "resumed"),
        message(2, "done"),
        message(3, "resumed"),
    ]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    assert plan["full_task_ids"] == ["done", "previous"]


def test_partial_task_does_not_cut_the_current_task_even_if_it_alone_exceeds_limit() -> None:
    original = [
        message(1, "current", content="a" * 150_000),
        message(2, "current", content="b" * 150_000),
    ]
    assert build_compaction_plan(original, keep_recent=0) == ({}, "")


def test_size_budget_includes_previous_summary_and_record_metadata() -> None:
    original = [
        message(0, "compact_summary", content="摘要" * 120_000),
        message(1, meta={"reasoning": "b" * 100_000}),
        message(2),
    ]
    assert build_compaction_plan(original, keep_recent=1) == ({}, "")


def test_exact_character_limit_accepts_full_records() -> None:
    original = [message(1), message(2)]
    expected = json.dumps(original[0], ensure_ascii=False, default=str)
    assert build_compaction_plan(original, keep_recent=1, max_chars=len(expected))[1] == expected
    assert build_compaction_plan(original, keep_recent=1, max_chars=len(expected) - 1) == ({}, "")


def test_token_budget_retains_complete_recent_segments_and_always_the_active_one() -> None:
    original = [message(i, content="x" * 120) for i in range(1, 5)]
    plan, text = build_compaction_plan(original, keep_recent=100, keep_recent_tokens=80)
    assert selected(text) == original[:2]
    assert plan["compressed_segments"] == 2
    assert selected(build_compaction_plan(original, keep_recent_tokens=1)[1]) == original[:3]


def test_low_threshold_does_not_silently_compress_more_than_planned() -> None:
    original = [message(i, content="x" * 120) for i in range(1, 5)]
    plan, text = build_compaction_plan(original, keep_recent=2, threshold_tokens=1)
    assert selected(text) == original[:2]
    assert replace_covered(original, plan, message(8, "compact_summary"))[1:] == original[2:]


@pytest.mark.parametrize("keep_recent", [0, -1, 1, 6])
def test_at_least_one_real_segment_is_always_retained(keep_recent: int) -> None:
    original = [message(0, "compact_summary"), message(1)]
    assert build_compaction_plan(original, keep_recent=keep_recent) == ({}, "")


def test_anonymous_users_define_turns_but_assistant_and_tool_rows_do_not() -> None:
    original = [
        message(1, ""), message(2, "", role="assistant"), message(3, ""),
        message(4, "", role="assistant"), message(5, ""),
    ]
    plan, text = build_compaction_plan(original, keep_recent=1)
    assert selected(text) == original[:4]
    assert plan["total_segments"] == 3


@pytest.mark.parametrize("invalid", [
    {}, {"version": 1, "fingerprints": ["a"]}, {"version": True}, {"version": 2},
    {"version": 1, "fingerprints": [{}]}, {"version": 1, "fingerprints": []},
])
def test_unverifiable_legacy_or_malformed_plans_are_rejected(invalid: dict) -> None:
    assert covered_indices([message(1)], invalid) is None


@pytest.mark.parametrize("mutation", ["duplicate_index", "wrong_hash", "out_of_range", "negative", "boolean_index", "source_length"])
def test_indexed_plan_shape_is_validated(mutation: str) -> None:
    original = [message(1), message(2), message(3)]
    plan, _ = build_compaction_plan(original, keep_recent=1)
    if mutation == "duplicate_index":
        plan["indexed_records"][1]["index"] = 0
    elif mutation == "wrong_hash":
        plan["indexed_records"][0]["fingerprint"] = "different"
    elif mutation == "source_length":
        plan["source_length"] = 1
    else:
        plan["indexed_records"][0]["index"] = {"out_of_range": 9, "negative": -1, "boolean_index": False}[mutation]
    assert covered_indices(original, plan) is None


def test_compressed_task_ids_include_prior_summary_without_splitting_invalid_strings() -> None:
    original = [
        message(0, "compact_summary", meta={"compressed_task_ids": ["older", "oldest"]}),
        message(1, "old", meta={"compressed_task_ids": "invalid"}),
        message(2, "old", meta="invalid", _meta={"compressed_task_ids": ["another"]}),
    ]
    assert compressed_task_ids(original) == ["another", "old", "older", "oldest"]
