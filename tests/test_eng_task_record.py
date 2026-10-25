"""TaskRecord 的 summary 只放轮摘要，L2 snip 只拿它顶替原文；旧记录来路不明的 summary 降级成 outcome。"""
from __future__ import annotations

from pathlib import Path

from engine.task_record import (
    TASK_RECORD_VERSION,
    TaskRecord,
    load_task_records,
    recent_task_ids,
    snip_history,
    snip_summaries,
    write_task_record,
)


def test_new_records_keep_the_action_outcome_apart_from_the_turn_summary() -> None:
    data = TaskRecord(task_id="t", session_id="s", node_id="n", outcome="max_steps reached").to_dict()

    assert data["version"] == TASK_RECORD_VERSION and data["outcome"] == "max_steps reached"
    assert "summary" not in data
    loaded = TaskRecord.from_dict(data)
    assert loaded.summary == "" and loaded.outcome == "max_steps reached"


def test_a_legacy_summary_is_treated_as_the_action_outcome() -> None:
    loaded = TaskRecord.from_dict({"task_id": "t", "session_id": "s", "node_id": "n", "summary": "我调用了三个工具"})

    assert loaded.summary == "" and loaded.outcome == "我调用了三个工具"
    assert snip_summaries([loaded]) == {}


def test_a_turn_summary_written_back_onto_a_legacy_record_survives_a_reload(tmp_path: Path) -> None:
    legacy = TaskRecord.from_dict({"task_id": "t", "session_id": "s", "node_id": "n", "summary": "old outcome"})
    legacy.summary = "这一轮做了什么的摘要"

    write_task_record(tmp_path, legacy)

    assert snip_summaries(load_task_records(tmp_path, "s")) == {"t": "这一轮做了什么的摘要"}


def test_recent_tasks_are_counted_from_this_sessions_messages() -> None:
    messages = [{"role": "user", "content": "x", "_meta": {"source_task_id": task_id}} for task_id in ("a", "b", "c", "b")]
    messages.append({"role": "user", "content": "summary", "_meta": {"source_task_id": "compact_summary"}})

    assert recent_task_ids(messages, 2) == {"b", "c"}
    assert recent_task_ids(messages, 0) == set()


def test_child_task_records_do_not_crowd_out_recent_session_tasks() -> None:
    records = [TaskRecord(task_id=f"t{number}", session_id="s", node_id="n", summary=f"S{number}") for number in range(4)]
    records += [TaskRecord(task_id=f"child{number}", session_id="s", node_id="child", summary=f"C{number}") for number in range(5)]
    messages = [{"role": "user", "content": f"m{number}", "_meta": {"source_task_id": f"t{number}"}} for number in range(4)]

    _history, count, selected = snip_history(messages, records, keep_recent_tasks=3, max_snip=2)

    assert count == 1 and selected == {"t0"}
