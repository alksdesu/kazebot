"""缓存命中率汇总的口径：命中 token / 输入 token，渠道没报缓存的「无数据」不进比例，单独计数。

几份 transcript 里的副本只算一次，挂起又恢复的同一任务各段相加；没有完成时间的老记录默认当基线，时间段只筛新记录。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.usage_report as usage_report  # noqa: E402
from engine.task_record import TaskRecord, write_task_record  # noqa: E402
from supervisor.task_router import BranchFinalizePlan, TaskRouterMixin  # noqa: E402
from supervisor.usage_report import (  # noqa: E402
    TranscriptUsageIndex,
    build_cache_report,
    create_usage_router,
    parse_query,
    session_parents,
)

_TOKEN = "test-usage-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_UNSET = object()
T1 = "2026-10-26T08:00:00+00:00"
T2 = "2026-10-26T09:00:00+00:00"
SUMMARY = "这一轮读了三个文件，改了配置里的超时设置，又跑了一遍检查，确认改动已经生效。" * 2


def _record(
    task_id: str,
    session_id: str = "P",
    *,
    node: str = "entry",
    prompt: int = 1000,
    hit: Any = _UNSET,
    at: str = "",
    model: str = "",
    provider: str = "",
    channel: str = "",
    child: str = "",
    step: int = 1,
) -> TaskRecord:
    usage: dict[str, int] = {"prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10}
    if hit is not _UNSET:
        usage["cache_read_tokens"] = hit
    return TaskRecord(
        task_id=task_id, session_id=session_id, node_id=node, action="finish", step_count=step,
        token_usage=usage, child_session_id=child, outcome="done",
        completed_at=at, model=model, provider=provider, channel=channel,
    )


def _write(ws: Path, *records: TaskRecord) -> None:
    for record in records:
        write_task_record(ws, record)


def _transcripts(ws: Path) -> Path:
    return ws / "data" / "transcripts"


def _report(ws: Path, *, sessions: dict[str, dict[str, Any]] | None = None, **query: str) -> dict[str, Any]:
    segments, scan = TranscriptUsageIndex(_transcripts(ws)).snapshot()
    report = build_cache_report(segments, parse_query(**query), parents=session_parents(sessions or {}))
    report["scan"].update(scan.to_dict())
    return report


def _row(report: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in report["rows"] if row["key"] == key)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


class TestHitRate:
    def test_hit_rate_is_cache_hits_over_input(self, ws: Path) -> None:
        _write(
            ws,
            _record("a", node="entry", prompt=1000, hit=600, at=T1),
            _record("b", node="entry", prompt=3000, hit=0, at=T2),
            _record("c", node="coder", prompt=2000, hit=1500, at=T2),
        )

        report = _report(ws, group_by="node")

        assert [row["key"] for row in report["rows"]] == ["entry", "coder"]
        assert _row(report, "entry")["current"] == {
            "tasks": 2, "prompt_tokens": 4000, "cache_read_tokens": 600, "miss_tokens": 3400,
            "hit_rate": pytest.approx(0.15), "no_data_tasks": 0, "no_data_prompt_tokens": 0,
        }
        assert report["totals"]["current"]["hit_rate"] == pytest.approx(2100 / 6000)
        assert report["totals"]["baseline"]["tasks"] == 0 and report["totals"]["baseline"]["hit_rate"] is None

    def test_a_relay_that_reports_more_hits_than_input_is_capped(self, ws: Path) -> None:
        _write(ws, _record("a", prompt=100, hit=150, at=T1))

        current = _report(ws, group_by="node")["totals"]["current"]

        assert (current["cache_read_tokens"], current["miss_tokens"], current["hit_rate"]) == (100, 0, 1.0)

    def test_records_without_usage_are_left_out_and_counted(self, ws: Path) -> None:
        _write(ws, _record("empty", prompt=0, hit=0, at=T1), _record("a", prompt=500, hit=100, at=T1))

        report = _report(ws, group_by="node")

        assert report["scan"]["without_usage"] == 1
        assert report["totals"]["current"]["tasks"] == 1

    def test_model_and_channel_rows_put_old_records_under_unknown(self, ws: Path) -> None:
        _write(
            ws,
            _record("old", prompt=1000, hit=200),
            _record("n1", prompt=1000, hit=500, at=T1, model="deepseek-v4", provider="deepseek", channel="ds-main"),
            _record("n2", prompt=1000, hit=100, at=T1, model="gemini-3", provider="gemini", channel="gm-relay"),
        )

        by_model = _report(ws, group_by="model")
        by_channel = _report(ws, group_by="channel")

        assert {row["key"] for row in by_model["rows"]} == {"", "deepseek-v4", "gemini-3"}
        assert _row(by_model, "")["baseline"]["prompt_tokens"] == 1000
        assert _row(by_model, "")["current"]["tasks"] == 0
        assert _row(by_model, "deepseek-v4")["current"]["cache_read_tokens"] == 500
        assert {row["key"] for row in by_channel["rows"]} == {"", "ds-main", "gm-relay"}
        assert _row(by_channel, "gm-relay")["current"]["hit_rate"] == pytest.approx(0.1)

    def test_session_rows_fold_child_runs_into_the_user_conversation(self, ws: Path) -> None:
        child_task = _record("child-task", "P", node="coder", prompt=2000, hit=1000, at=T1, child="child_x")
        _write(
            ws,
            _record("entry-task", "P", prompt=1000, hit=400, at=T1),
            child_task,
            replace(child_task, session_id="child_x"),
            _record("q-task", "Q", prompt=500, hit=0, at=T1),
        )
        sessions = {"P": {"conversation_key": "web:console"}, "child_x": {"parent_session_id": "P", "is_child": True}}

        report = _report(ws, group_by="session", sessions=sessions)
        orphaned = _report(ws, group_by="session")

        assert [row["key"] for row in report["rows"]] == ["P", "Q"]
        assert _row(report, "P")["current"]["prompt_tokens"] == 3000
        # sessions.json 里子会话的登记清掉了，写给父会话的那份副本照样认得出归属
        assert [row["key"] for row in orphaned["rows"]] == ["P", "Q"]

    def test_an_unmerged_branch_is_counted_under_its_parent(self, ws: Path) -> None:
        _write(ws, _record("t", "branch_5", prompt=1000, hit=100, at=T1))

        assert [row["key"] for row in _report(ws, group_by="session", sessions={
            "branch_5": {"parent_session_id": "P"},
        })["rows"]] == ["P"]
        assert [row["key"] for row in _report(ws, group_by="session")["rows"]] == ["branch_5"]


class TestWindowAndBaseline:
    def test_the_time_window_only_filters_new_records(self, ws: Path) -> None:
        _write(
            ws,
            _record("old", prompt=1000, hit=100),
            _record("early", prompt=1000, hit=100, at="2026-10-20T08:00:00+00:00"),
            _record("late", prompt=1000, hit=900, at="2026-10-26T08:00:00+00:00"),
        )

        recent = _report(ws, group_by="node", since="2026-10-25T00:00:00+00:00")
        earlier = _report(ws, group_by="node", until="2026-10-21T00:00:00+00:00")

        assert (recent["totals"]["current"]["tasks"], recent["totals"]["current"]["cache_read_tokens"]) == (1, 900)
        assert recent["window"] == {"since": "2026-10-25T00:00:00+00:00", "until": None}
        assert (earlier["totals"]["current"]["tasks"], earlier["totals"]["current"]["cache_read_tokens"]) == (1, 100)
        # 老记录没有完成时间，时间段筛不到，始终在基线里
        assert recent["totals"]["baseline"]["tasks"] == earlier["totals"]["baseline"]["tasks"] == 1

    def test_the_default_baseline_is_every_record_without_a_completion_time(self, ws: Path) -> None:
        _write(ws, _record("old-1", prompt=1000, hit=300), _record("old-2", "Q", prompt=1000, hit=500))
        _write(ws, _record("new", prompt=1000, hit=900, at=T1))

        report = _report(ws, group_by="node")

        assert report["baseline"] == {"mode": "legacy", "before": None}
        baseline = report["totals"]["baseline"]
        assert (baseline["tasks"], baseline["prompt_tokens"], baseline["cache_read_tokens"]) == (2, 2000, 800)
        assert report["totals"]["current"]["tasks"] == 1

    def test_a_cutoff_baseline_takes_new_records_before_it(self, ws: Path) -> None:
        _write(
            ws,
            _record("old", prompt=1000, hit=1000),
            _record("before", prompt=1000, hit=200, at="2026-10-20T08:00:00+00:00"),
            _record("after", prompt=1000, hit=800, at="2026-10-26T08:00:00+00:00"),
        )

        report = _report(ws, group_by="node", baseline_before="2026-10-25T00:00:00+00:00")

        assert report["baseline"] == {"mode": "before", "before": "2026-10-25T00:00:00+00:00"}
        # 没给起点时当前值从截止时间算起，两段不重叠
        assert report["window"]["since"] == "2026-10-25T00:00:00+00:00"
        baseline, current = report["totals"]["baseline"], report["totals"]["current"]
        assert (baseline["tasks"], baseline["cache_read_tokens"]) == (1, 200)
        assert (current["tasks"], current["cache_read_tokens"]) == (1, 800)

    def test_an_explicit_window_may_overlap_a_cutoff_baseline(self, ws: Path) -> None:
        _write(ws, _record("t", prompt=1000, hit=200, at="2026-10-20T08:00:00+00:00"))

        report = _report(
            ws, group_by="node", since="2026-10-19T00:00:00+00:00", baseline_before="2026-10-25T00:00:00+00:00",
        )

        assert report["totals"]["baseline"]["tasks"] == report["totals"]["current"]["tasks"] == 1


class TestNoData:
    def test_old_records_count_as_zero_where_the_session_reported_hits(self, ws: Path) -> None:
        _write(
            ws,
            _record("p1", "P", prompt=1000, hit=600),
            _record("p2", "P", prompt=1000),
            _record("q1", "Q", prompt=1000),
        )

        report = _report(ws, group_by="session")

        reported = _row(report, "P")["baseline"]
        assert (reported["tasks"], reported["prompt_tokens"], reported["cache_read_tokens"]) == (2, 2000, 600)
        assert reported["hit_rate"] == pytest.approx(0.3)
        silent = _row(report, "Q")["baseline"]
        assert silent["tasks"] == 0 and silent["hit_rate"] is None
        assert (silent["no_data_tasks"], silent["no_data_prompt_tokens"]) == (1, 1000)
        totals = report["totals"]["baseline"]
        assert (totals["tasks"], totals["prompt_tokens"], totals["no_data_tasks"]) == (2, 2000, 1)

    def test_a_zero_reported_after_the_upgrade_vouches_for_the_old_records(self, ws: Path) -> None:
        _write(
            ws,
            _record("p-old", "P", prompt=1000),
            _record("p-new", "P", prompt=1000, hit=0, at=T1, provider="gemini"),
            _record("q-old", "Q", prompt=1000),
            _record("q-new", "Q", prompt=1000, at=T1),
        )

        report = _report(ws, group_by="session")

        reported = _row(report, "P")
        assert (reported["baseline"]["tasks"], reported["baseline"]["no_data_tasks"]) == (1, 0)
        assert reported["baseline"]["hit_rate"] == 0.0 and reported["current"]["hit_rate"] == 0.0
        silent = _row(report, "Q")
        assert (silent["baseline"]["no_data_tasks"], silent["current"]["no_data_tasks"]) == (1, 1)
        assert silent["baseline"]["hit_rate"] is None and silent["current"]["hit_rate"] is None

    def test_new_records_without_the_field_are_no_data_not_zero(self, ws: Path) -> None:
        _write(
            ws,
            _record("old-hit", "P", prompt=500, hit=100),
            _record("n1", "P", prompt=1000, hit=500, at=T1),
            _record("n2", "P", prompt=3000, at=T1),
        )

        current = _row(_report(ws, group_by="session"), "P")["current"]

        assert (current["tasks"], current["prompt_tokens"], current["hit_rate"]) == (1, 1000, pytest.approx(0.5))
        assert (current["no_data_tasks"], current["no_data_prompt_tokens"]) == (1, 3000)

    def test_a_child_tasks_hits_do_not_vouch_for_the_parents_own_tasks(self, ws: Path) -> None:
        child_task = _record("c1", "P", node="coder", prompt=1000, hit=700, child="child_x")
        _write(ws, child_task, replace(child_task, session_id="child_x"), _record("p1", "P", prompt=1000))

        report = _report(ws, group_by="node")

        assert _row(report, "entry")["baseline"]["no_data_tasks"] == 1
        assert _row(report, "coder")["baseline"]["cache_read_tokens"] == 700

    def test_a_branch_run_is_vouched_for_by_its_parent_session(self, ws: Path) -> None:
        _write(ws, _record("p1", "P", prompt=1000, hit=300), _record("b1", "branch_9", prompt=1000))

        report = _report(ws, group_by="session", sessions={"branch_9": {"parent_session_id": "P"}})

        baseline = _row(report, "P")["baseline"]
        assert (baseline["tasks"], baseline["no_data_tasks"]) == (2, 0)


class TestCopies:
    def test_the_turn_summary_copy_counts_once(self, ws: Path) -> None:
        _write(ws, _record("t", "P", prompt=1000, hit=400, at=T1))
        summarizer = SimpleNamespace(
            result={"action": "finish", "result": {"text": SUMMARY}},
            input={"_target_task_id": "t", "_target_session_id": "P"},
        )

        TaskRouterMixin._apply_turn_summary_result_locked(SimpleNamespace(workspace_root=ws), summarizer)

        report = _report(ws, group_by="node")
        assert report["scan"]["records"] == 2 and report["scan"]["duplicates"] == 1
        assert (report["totals"]["current"]["tasks"], report["totals"]["current"]["prompt_tokens"]) == (1, 1000)

    def test_a_summary_written_back_onto_a_legacy_record_counts_once(self, ws: Path) -> None:
        path = _transcripts(ws) / "P.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "task_id": "v1", "session_id": "P", "node_id": "entry", "summary": "max_steps reached",
            "token_usage": {"prompt_tokens": 800, "cache_read_tokens": 200},
        }) + "\n", encoding="utf-8")
        summarizer = SimpleNamespace(
            result={"action": "finish", "result": {"text": SUMMARY}},
            input={"_target_task_id": "v1", "_target_session_id": "P"},
        )

        TaskRouterMixin._apply_turn_summary_result_locked(SimpleNamespace(workspace_root=ws), summarizer)

        report = _report(ws, group_by="node")
        assert report["scan"]["duplicates"] == 1
        assert report["totals"]["baseline"]["prompt_tokens"] == 800

    def test_a_branch_merged_on_completion_counts_once(self, ws: Path) -> None:
        _write(ws, _record("t", "branch_7", prompt=1000, hit=300, at=T1))
        leftover = (_transcripts(ws) / "branch_7.jsonl").read_bytes()

        merged = TaskRouterMixin._merge_branch_transcript_locked(SimpleNamespace(workspace_root=ws), "P", "branch_7")
        # 合并后删分支文件失败：两份都还在
        (_transcripts(ws) / "branch_7.jsonl").write_bytes(leftover)

        report = _report(ws, group_by="session", sessions={"branch_7": {"parent_session_id": "P"}})
        assert merged == 1
        assert [row["key"] for row in report["rows"]] == ["P"]
        assert report["scan"]["duplicates"] == 1
        assert (report["totals"]["current"]["tasks"], report["totals"]["current"]["prompt_tokens"]) == (1, 1000)

    def test_a_branch_merged_at_finalize_counts_once(self, ws: Path) -> None:
        _write(ws, _record("t", "branch_8", prompt=1000, hit=300, at=T1))
        router = SimpleNamespace(
            workspace_root=ws, tasks={},
            _session_store=SimpleNamespace(get_entry=lambda _session_id: None),
            purge_session_files=lambda _session_id: None,
        )
        plan = BranchFinalizePlan(
            identity="i", task_id="t", parent_session_id="P", branch_session_id="branch_8",
            base_count=0, merge=True, cleanup_session_ids=("branch_8",),
        )

        TaskRouterMixin._execute_branch_finalize_io_body(router, plan)

        assert (_transcripts(ws) / "P.jsonl").is_file() and (_transcripts(ws) / "branch_8.jsonl").is_file()
        report = _report(ws, group_by="session")
        assert [row["key"] for row in report["rows"]] == ["P"]
        assert report["scan"]["duplicates"] == 1
        assert report["totals"]["current"]["prompt_tokens"] == 1000

    def test_parent_and_child_copies_count_once(self, ws: Path) -> None:
        record = _record("t", "P", node="coder", prompt=2000, hit=1200, at=T1, child="child_x")
        _write(ws, record, replace(record, session_id="child_x"))

        report = _report(ws, group_by="node")

        assert report["scan"]["duplicates"] == 1
        assert _row(report, "coder")["current"]["prompt_tokens"] == 2000

    def test_the_segments_of_a_suspended_and_resumed_task_add_up(self, ws: Path) -> None:
        _write(
            ws,
            _record("t", "P", prompt=1000, hit=100, at=T1, step=2),
            _record("t", "P", prompt=3000, hit=2000, at=T2, step=5),
            _record("old", "P", prompt=500, hit=100, step=1),
            _record("old", "P", prompt=700, hit=300, step=3),
        )

        report = _report(ws, group_by="node")

        assert report["scan"]["duplicates"] == 0
        current, baseline = _row(report, "entry")["current"], _row(report, "entry")["baseline"]
        assert (current["tasks"], current["prompt_tokens"], current["cache_read_tokens"]) == (1, 4000, 2100)
        assert (baseline["tasks"], baseline["prompt_tokens"], baseline["cache_read_tokens"]) == (1, 1200, 400)


class TestScanning:
    def test_bad_lines_are_skipped_and_counted(self, ws: Path) -> None:
        path = _transcripts(ws) / "P.jsonl"
        path.parent.mkdir(parents=True)
        good = json.dumps(_record("ok", prompt=1000, hit=250, at=T1).to_dict(), ensure_ascii=False).encode("utf-8")
        bad_time = json.dumps({**_record("bad-time", at=T1).to_dict(), "completed_at": "not a time"}).encode()
        path.write_bytes(b"\n".join([
            b"{not json",
            b"[1, 2, 3]",
            json.dumps({"session_id": "P", "node_id": "entry"}).encode(),
            bad_time,
            b"\xff\xfe\x00garbage",
            good,
            b"",
            b'{"task_id": "half',
        ]))

        report = _report(ws, group_by="node")

        assert report["scan"]["bad_lines"] == 6
        assert report["scan"]["records"] == 1
        assert report["totals"]["current"]["cache_read_tokens"] == 250

    def test_an_untouched_transcript_is_not_parsed_again(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(ws, _record("a", "P", hit=1, at=T1), _record("b", "Q", hit=1, at=T1))
        index = TranscriptUsageIndex(_transcripts(ws))
        calls: list[str] = []
        original = usage_report.parse_transcript_file

        def counting(path: Path, stamp: tuple[int, int, int]) -> Any:
            calls.append(path.name)
            return original(path, stamp)

        monkeypatch.setattr(usage_report, "parse_transcript_file", counting)

        _segments, first = index.snapshot()
        assert sorted(calls) == ["P.jsonl", "Q.jsonl"] and first.reparsed == 2

        calls.clear()
        segments, second = index.snapshot()
        assert calls == [] and second.reparsed == 0
        assert second.files == 2 and len(segments) == 2

    def test_a_changed_transcript_is_parsed_again_and_a_deleted_one_drops_out(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(ws, _record("a", "P", hit=1, at=T1), _record("b", "Q", hit=1, at=T1))
        index = TranscriptUsageIndex(_transcripts(ws))
        index.snapshot()
        calls: list[str] = []
        original = usage_report.parse_transcript_file

        def counting(path: Path, stamp: tuple[int, int, int]) -> Any:
            calls.append(path.name)
            return original(path, stamp)

        monkeypatch.setattr(usage_report, "parse_transcript_file", counting)

        _write(ws, _record("c", "P", hit=2, at=T2))
        segments, stats = index.snapshot()
        assert calls == ["P.jsonl"] and stats.reparsed == 1
        assert sorted(segment.task_id for segment in segments) == ["a", "b", "c"]

        calls.clear()
        (_transcripts(ws) / "Q.jsonl").unlink()
        segments, stats = index.snapshot()
        assert calls == [] and stats.files == 1
        assert sorted(segment.task_id for segment in segments) == ["a", "c"]

    def test_a_missing_transcript_directory_is_an_empty_report(self, ws: Path) -> None:
        report = _report(ws, group_by="session")

        assert report["rows"] == [] and report["scan"]["files"] == 0
        assert report["totals"]["current"]["hit_rate"] is None


class TestQuery:
    @pytest.mark.parametrize("kwargs", [
        {"group_by": "user"},
        {"since": "yesterday"},
        {"until": "2026-13-01"},
        {"baseline_before": "soon"},
        {"since": "2026-10-27T08:00:00+00:00", "until": "2026-10-27T08:00:00+00:00"},
        {"since": "2026-10-27T09:00:00+00:00", "until": "2026-10-27T08:00:00+00:00"},
    ])
    def test_bad_queries_are_rejected(self, kwargs: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            parse_query(**kwargs)

    def test_naive_times_are_utc_and_the_cutoff_opens_the_window(self) -> None:
        query = parse_query(group_by="", baseline_before="2026-10-25T00:00:00")

        assert query.group_by == "session" and query.baseline_mode == "before"
        assert query.since == query.baseline_before == datetime(2026, 10, 25, tzinfo=timezone.utc)
        explicit = parse_query(since="2026-10-26T00:00:00Z", baseline_before="2026-10-25T00:00:00Z")
        assert explicit.since == datetime(2026, 10, 26, tzinfo=timezone.utc)
        assert parse_query().baseline_mode == "legacy"

    def test_session_parents_ignore_entries_without_a_parent(self) -> None:
        assert session_parents({
            "child_a": {"parent_session_id": "P"},
            "P": {"conversation_key": "web:console"},
            "weird": "not a mapping",  # type: ignore[dict-item]
            "blank": {"parent_session_id": "  "},
        }) == {"child_a": "P"}


@pytest.fixture
def api(ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_usage_router(ws), prefix="/v1/admin/usage")

    def call(path: str, *, headers: dict[str, str] | None = None, **params: str) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.get(
                    f"/v1/admin/usage{path}", headers=_AUTH if headers is None else headers, params=params,
                )

        return asyncio.run(go())

    return call


class TestEndpoint:
    def test_it_needs_the_admin_token(self, api: Any) -> None:
        assert api("/cache", headers={}).status_code == 401
        assert api("/cache", headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_bad_parameters_are_a_400(self, api: Any) -> None:
        response = api("/cache", group_by="user")

        assert response.status_code == 400 and "group_by" in response.json()["detail"]
        assert api("/cache", since="yesterday").status_code == 400

    def test_session_rows_carry_what_the_console_needs_to_name_them(self, ws: Path, api: Any) -> None:
        child_task = _record("c", "P", node="coder", prompt=2000, hit=1000, at=T1, child="child_x")
        _write(ws, _record("t", "P", prompt=1000, hit=500, at=T1), child_task, replace(child_task, session_id="child_x"))
        (ws / "data" / "sessions.json").write_text(json.dumps({
            "P": {"session_id": "P", "conversation_key": "web:console"},
            "child_x": {"session_id": "child_x", "parent_session_id": "P", "is_child": True},
        }), encoding="utf-8")

        response = api("/cache", group_by="session")

        assert response.status_code == 200
        body = response.json()
        [row] = body["rows"]
        assert row["key"] == "P" and row["current"]["prompt_tokens"] == 3000
        assert row["session"]["conversation_key"] == "web:console" and row["session"]["internal"] is False
        assert body["scan"]["duplicates"] == 1 and body["scan"]["files"] == 2
        assert set(body["scan"]) == {"files", "reparsed", "bad_lines", "records", "segments", "duplicates", "without_usage"}
        assert body["baseline"] == {"mode": "legacy", "before": None}

    def test_other_dimensions_have_no_session_block(self, ws: Path, api: Any) -> None:
        _write(ws, _record("t", "P", prompt=1000, hit=500, at=T1, model="m1"))

        body = api("/cache", group_by="model").json()

        assert [row["key"] for row in body["rows"]] == ["m1"] and "session" not in body["rows"][0]
