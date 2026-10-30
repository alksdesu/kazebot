"""TaskRecord 的 summary 只放轮摘要，L2 snip 只拿它顶替原文；旧记录来路不明的 summary 降级成 outcome。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from engine.task_record import (
    TASK_RECORD_VERSION,
    TaskRecord,
    load_task_records,
    parse_record_timestamp,
    recent_task_ids,
    record_timestamp,
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


def _finished(**extra: object) -> TaskRecord:
    return TaskRecord(
        task_id="t", session_id="s", node_id="n", action="finish", token_usage={"prompt_tokens": 9},
        completed_at="2026-10-27T08:00:00+00:00", model="deepseek-v4", provider="deepseek", channel="ds-main",
        **extra,
    )


def test_completion_time_and_serving_provider_round_trip() -> None:
    data = _finished().to_dict()

    assert (data["completed_at"], data["model"], data["provider"], data["channel"]) == (
        "2026-10-27T08:00:00+00:00", "deepseek-v4", "deepseek", "ds-main",
    )
    loaded = TaskRecord.from_dict(data)
    assert (loaded.completed_at, loaded.model, loaded.provider, loaded.channel) == (
        "2026-10-27T08:00:00+00:00", "deepseek-v4", "deepseek", "ds-main",
    )
    assert loaded.version == TASK_RECORD_VERSION


def test_empty_completion_fields_are_left_out() -> None:
    data = TaskRecord(task_id="t", session_id="s", node_id="n").to_dict()

    assert not {"completed_at", "model", "provider", "channel"} & data.keys()


def test_records_written_before_the_completion_fields_still_load(tmp_path: Path) -> None:
    path = tmp_path / "data" / "transcripts" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"task_id": "old", "session_id": "s", "node_id": "n", "token_usage": {"prompt_tokens": 5}, "version": 2}\n',
        encoding="utf-8",
    )

    [loaded] = load_task_records(tmp_path, "s")

    assert loaded.token_usage == {"prompt_tokens": 5}
    assert (loaded.completed_at, loaded.model, loaded.provider, loaded.channel) == ("", "", "", "")
    assert not {"completed_at", "model", "provider", "channel"} & loaded.to_dict().keys()


def test_a_legacy_summary_still_downgrades_next_to_the_new_fields() -> None:
    loaded = TaskRecord.from_dict({
        "task_id": "t", "session_id": "s", "node_id": "n", "summary": "我调用了三个工具",
        "completed_at": "2026-10-27T08:00:00+00:00", "model": "m",
    })

    assert loaded.summary == "" and loaded.outcome == "我调用了三个工具"
    assert loaded.completed_at == "2026-10-27T08:00:00+00:00" and loaded.model == "m"


def test_falsy_completion_fields_read_back_as_empty() -> None:
    loaded = TaskRecord.from_dict({"task_id": "t", "session_id": "s", "node_id": "n", "model": None, "channel": 0})

    assert (loaded.model, loaded.channel) == ("", "")


def test_record_timestamp_is_utc_and_reads_back() -> None:
    before = datetime.now(timezone.utc)
    stamp = record_timestamp()
    parsed = parse_record_timestamp(stamp)

    assert parsed is not None and parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= datetime.now(timezone.utc)


def test_parse_record_timestamp_treats_a_naive_time_as_utc_and_rejects_garbage() -> None:
    assert parse_record_timestamp("2026-10-27T08:00:00") == datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    assert parse_record_timestamp("2026-10-27T16:00:00+08:00") == datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    assert parse_record_timestamp("2026-10-27T08:00:00Z") == datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    for garbage in ("", "   ", "yesterday", None, 1730000000):
        assert parse_record_timestamp(garbage) is None


def test_the_turn_summary_copy_keeps_the_completion_fields(tmp_path: Path) -> None:
    from supervisor.task_router import TaskRouterMixin

    write_task_record(tmp_path, _finished())
    summarizer = SimpleNamespace(
        result={"action": "finish", "result": {"text": "这一轮读了三个文件，改了配置里的超时设置，又跑了一遍检查，确认生效。" * 2}},
        input={"_target_task_id": "t", "_target_session_id": "s"},
    )

    TaskRouterMixin._apply_turn_summary_result_locked(SimpleNamespace(workspace_root=tmp_path), summarizer)

    original, copy = load_task_records(tmp_path, "s")
    assert copy.summary and not original.summary
    assert (copy.completed_at, copy.model, copy.provider, copy.channel) == (
        original.completed_at, original.model, original.provider, original.channel,
    )


def test_a_record_appended_after_a_torn_last_line_is_still_read(tmp_path: Path) -> None:
    write_task_record(tmp_path, TaskRecord(task_id="first", session_id="s", node_id="n"))
    with (tmp_path / "data" / "transcripts" / "s.jsonl").open("ab") as stream:
        stream.write(b'{"task_id": "torn", "session_id": "s", "no')

    write_task_record(tmp_path, TaskRecord(task_id="second", session_id="s", node_id="n"))

    assert [record.task_id for record in load_task_records(tmp_path, "s")] == ["first", "second"]
