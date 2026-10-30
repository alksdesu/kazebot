"""还登记着的子会话 data_cleanup 一概不删，登记和历史由 supervisor 一起收；子会话 TTL 两边读同一个 engine.child_session.ttl_hours。

data_cleanup 只看文件修改时间：accumulate 的登记还在、历史却没了；挂起等孙任务的同步子任务文件闲置过了 TTL，恢复时历史是空的。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.child_session_retention as retention  # noqa: E402
import engine.data_cleanup as cleanup  # noqa: E402
import supervisor.state as supervisor_state  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from supervisor._helpers import _now  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_HOUR = 3600.0


def _row(session_id: str, mode: str, *, reset: bool = False, is_child: bool = True) -> dict[str, Any]:
    """和 SessionStore.on_child_session_created 写出的子会话行同一个形状。"""
    return {
        "session_id": session_id,
        "channel": "internal",
        "conversation_key": "qq_group:5edca2992de82f26297b3a68",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reset": reset,
        "is_child": is_child,
        "parent_session_id": "parent",
        "node_id": "helper.node",
        "context_key": "",
        "context_mode": mode,
        "last_active_at": "2026-01-01T00:00:00+00:00",
    }


def _conversation(data: Path, session_id: str, *, idle_hours: float) -> Path:
    path = data / "conversations" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"role": "user", "content": "x"}\n', encoding="utf-8")
    stamp = time.time() - idle_hours * _HOUR
    os.utime(path, (stamp, stamp))
    return path


def _registry(data: Path, rows: dict[str, Any]) -> None:
    (data / "sessions.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _child_ttl(workspace: Path, raw: str) -> None:
    config = workspace / "config" / "runtime.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"engine:\n  child_session:\n    ttl_hours: {raw}\n", encoding="utf-8")


@pytest.fixture()
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "conversations").mkdir(parents=True)
    monkeypatch.setattr(cleanup, "DATA_DIR", root)
    monkeypatch.setattr(cleanup, "DRY_RUN", False)
    return root


class TestRetentionRule:
    def test_an_unreset_accumulate_child_is_retained(self) -> None:
        assert retention.is_retained_child_row(_row("child_a", "accumulate")) is True

    def test_spaces_around_the_mode_are_ignored_like_the_supervisor_does(self) -> None:
        assert retention.is_retained_child_row(_row("child_a", " accumulate ")) is True

    @pytest.mark.parametrize("row", [
        pytest.param(_row("child_a", "fresh"), id="fresh"),
        pytest.param(_row("child_a", "fork"), id="fork"),
        pytest.param(_row("child_a", ""), id="legacy-without-mode"),
        pytest.param(_row("child_a", "accumulate", reset=True), id="reset-accumulate"),
        pytest.param(_row("child_a", "accumulate", is_child=False), id="not-a-child"),
        pytest.param({"session_id": "child_a", "is_child": True}, id="mode-missing"),
        pytest.param(None, id="none"),
        pytest.param("accumulate", id="string-row"),
        pytest.param(["accumulate"], id="list-row"),
    ])
    def test_everything_else_is_not(self, row: Any) -> None:
        assert retention.is_retained_child_row(row) is False

    @pytest.mark.parametrize("config,hours", [
        pytest.param({}, 24, id="unset"),
        pytest.param({"engine": None}, 24, id="engine-null"),
        pytest.param({"engine": {"child_session": "broken"}}, 24, id="section-not-a-mapping"),
        pytest.param({"engine": {"child_session": {"ttl_hours": 6}}}, 6, id="configured"),
        pytest.param({"engine": {"child_session": {"ttl_hours": "12"}}}, 12, id="quoted-number"),
        pytest.param({"engine": {"child_session": {"ttl_hours": 0}}}, 1, id="zero-clamps-to-one"),
        pytest.param({"engine": {"child_session": {"ttl_hours": -5}}}, 1, id="negative-clamps-to-one"),
        pytest.param({"engine": {"child_session": {"ttl_hours": "abc"}}}, 24, id="not-a-number"),
        pytest.param({"engine": {"child_session": {"ttl_hours": None}}}, 24, id="null"),
    ])
    def test_the_ttl_reads_like_the_lazy_expiry_always_did(self, config: dict[str, Any], hours: int) -> None:
        assert retention.child_session_ttl_hours(config) == hours

    def test_supervisor_and_cleanup_import_the_same_rule(self) -> None:
        # 各写一份就会再次出现一边留登记、一边删历史。
        assert supervisor_state.child_session_ttl_hours is retention.child_session_ttl_hours
        assert supervisor_state.is_retained_child_row is retention.is_retained_child_row
        assert cleanup.child_session_ttl_hours is retention.child_session_ttl_hours

    def test_cleanup_does_not_judge_registered_rows_itself(self) -> None:
        # 登记着的行该不该收要看有没有任务在用，只有 supervisor 看得到
        for name in ("is_retained_child_row", "is_held_row", "_retained_child_sessions"):
            assert not hasattr(cleanup, name), name


class TestChildConversationRetention:
    def test_an_idle_accumulate_child_keeps_its_history(self, data: Path, caplog) -> None:
        kept = _conversation(data, "child_acc", idle_hours=24 * 30)
        _registry(data, {"child_acc": _row("child_acc", "accumulate")})

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert kept.exists()
        assert any("kept 1 registered child sessions past the ttl" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("mode", ["fresh", "fork"])
    def test_registered_idle_fresh_and_fork_children_are_left_to_the_supervisor(self, data: Path, mode: str) -> None:
        # 挂起等孙任务的同步子任务，会话文件可以闲置过 TTL；登记着就由 supervisor 看有没有任务在用再收
        kept = _conversation(data, "child_tmp", idle_hours=25)
        _registry(data, {"child_tmp": _row("child_tmp", mode)})

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    def test_an_unregistered_child_is_deleted(self, data: Path) -> None:
        # dream、记忆提取这类系统子会话从不进登记表。
        gone = _conversation(data, "child_dream", idle_hours=25)
        kept = _conversation(data, "child_acc", idle_hours=25)
        _registry(data, {"child_acc": _row("child_acc", "accumulate")})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()
        assert kept.exists()

    def test_a_reset_accumulate_row_no_longer_protects_its_history(self, data: Path) -> None:
        gone = _conversation(data, "child_acc", idle_hours=25)
        _registry(data, {"child_acc": _row("child_acc", "accumulate", reset=True)})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()

    def test_a_registered_legacy_row_without_a_mode_is_left_to_the_supervisor_too(self, data: Path) -> None:
        # supervisor 的定期清理见到这种行就连登记带文件一起删，用不着这里抢先
        kept = _conversation(data, "child_old", idle_hours=25)
        _registry(data, {"child_old": _row("child_old", "")})

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    @pytest.mark.parametrize("mode", ["fresh", "fork", "accumulate"])
    def test_a_recent_child_is_kept_whatever_its_mode(self, data: Path, mode: str) -> None:
        kept = _conversation(data, "child_new", idle_hours=1)
        _registry(data, {"child_new": _row("child_new", mode)})

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    def test_a_row_protects_the_file_named_by_its_key_and_by_its_session_id(self, data: Path) -> None:
        by_key = _conversation(data, "child_key", idle_hours=25)
        by_field = _conversation(data, "child_field", idle_hours=25)
        _registry(data, {"child_key": _row("child_field", "accumulate")})

        cleanup.purge_expired_child_sessions()

        assert by_key.exists()
        assert by_field.exists()

    def test_an_empty_registry_protects_nothing(self, data: Path) -> None:
        gone = _conversation(data, "child_acc", idle_hours=25)
        _registry(data, {})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()

    def test_without_a_registry_children_go_by_age(self, data: Path) -> None:
        # 没有 sessions.json 说明还没有任何登记。
        gone = _conversation(data, "child_any", idle_hours=25)
        kept = _conversation(data, "child_new", idle_hours=1)
        branch = _conversation(data, "branch_7", idle_hours=25)

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()
        assert kept.exists()
        assert not branch.exists()

    @pytest.mark.parametrize("broken", [
        pytest.param("{ not json", id="corrupt"),
        pytest.param("", id="empty"),
        pytest.param('[{"session_id": "child_tmp", "is_child": true, "context_mode": "fresh"}]', id="list-root"),
        pytest.param('"sessions"', id="string-root"),
    ])
    def test_an_unreadable_registry_keeps_every_child_and_every_branch(
        self, data: Path, caplog, broken: str,
    ) -> None:
        # 认不出哪些分支还登记着，就分不清哪些还有没合并回去的历史
        children = [
            _conversation(data, "child_acc", idle_hours=24 * 30),
            _conversation(data, "child_tmp", idle_hours=25),
        ]
        branch = _conversation(data, "branch_7", idle_hours=25)
        (data / "sessions.json").write_text(broken, encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cleanup.purge_expired_child_sessions()

        assert all(path.exists() for path in children)
        assert branch.exists()
        assert any("sessions.json unreadable" in r.getMessage() for r in caplog.records)

    def test_a_registry_that_cannot_be_opened_counts_as_unreadable(self, data: Path) -> None:
        kept = _conversation(data, "child_tmp", idle_hours=25)
        (data / "sessions.json").mkdir()

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    def test_a_broken_registry_is_not_reported_when_nothing_has_expired(self, data: Path, caplog) -> None:
        kept = _conversation(data, "child_new", idle_hours=1)
        (data / "sessions.json").write_text("{ not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cleanup.purge_expired_child_sessions()

        assert kept.exists()
        assert not any("sessions.json unreadable" in r.getMessage() for r in caplog.records)

    def test_a_dry_run_reports_only_what_would_really_go(self, data: Path, monkeypatch, caplog) -> None:
        accumulate = _conversation(data, "child_acc", idle_hours=25)
        fresh = _conversation(data, "child_tmp", idle_hours=25)
        unregistered = _conversation(data, "child_dream", idle_hours=25)
        branch = _conversation(data, "branch_7", idle_hours=25)
        _registry(data, {
            "child_acc": _row("child_acc", "accumulate"),
            "child_tmp": _row("child_tmp", "fresh"),
        })
        monkeypatch.setattr(cleanup, "DRY_RUN", True)

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert accumulate.exists() and fresh.exists() and unregistered.exists() and branch.exists()
        reported = [r.getMessage() for r in caplog.records if "would delete file" in r.getMessage()]
        assert any("child_dream.jsonl" in message for message in reported)
        assert any("branch_7.jsonl" in message for message in reported)
        assert not any("child_acc.jsonl" in message for message in reported)
        assert not any("child_tmp.jsonl" in message for message in reported)


_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _held(session_id: str, mode: str = "fresh", **hold: Any) -> dict[str, Any]:
    """失败或到点收尾后留着的子会话行：retain 里至少有产出它的任务 id。"""
    row = _row(session_id, mode)
    row[retention.RETAIN_FIELD] = {"task_id": "task-child", "parent_task_id": "task-caller", "until": "", **hold}
    return row


def _branch_row(session_id: str, *, reset: bool = False) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "channel": "internal",
        "conversation_key": "",
        "reset": reset,
        "is_child": True,
        "parent_session_id": "parent",
        "node_id": "__entry_branch__",
        "context_mode": "branch",
    }


class TestHoldRule:
    @pytest.mark.parametrize("config,hours", [
        pytest.param({}, 24, id="unset"),
        pytest.param({"engine": {"child_session": {"retain_grace_hours": 6}}}, 6, id="configured"),
        pytest.param({"engine": {"child_session": {"retain_grace_hours": "12"}}}, 12, id="quoted-number"),
        pytest.param({"engine": {"child_session": {"retain_grace_hours": 0}}}, 1, id="zero-clamps-to-one"),
        pytest.param({"engine": {"child_session": {"retain_grace_hours": "abc"}}}, 24, id="not-a-number"),
    ])
    def test_the_grace_period_reads_like_the_ttl(self, config: dict[str, Any], hours: int) -> None:
        assert retention.retain_grace_hours(config) == hours

    @pytest.mark.parametrize("row", [
        pytest.param(None, id="none"),
        pytest.param(_row("child_a", "fresh"), id="no-hold"),
        pytest.param({**_held("child_a"), "reset": True}, id="reset"),
        pytest.param({**_row("child_a", "fresh"), "retain": "yes"}, id="not-a-mapping"),
        pytest.param({**_row("child_a", "fresh"), "retain": {"task_id": "  "}}, id="no-task-id"),
        pytest.param({**_row("child_a", "fresh"), "retain": None}, id="released"),
    ])
    def test_rows_without_a_usable_hold(self, row: Any) -> None:
        assert retention.retention_hold(row) is None
        assert retention.is_held_row(row, now=_NOW) is False
        assert retention.is_expired_hold(row, now=_NOW) is False

    def test_a_hold_is_returned_as_a_copy(self) -> None:
        row = _held("child_a", reason="failed")

        hold = retention.retention_hold(row)
        hold["task_id"] = "changed"

        assert row["retain"]["task_id"] == "task-child"

    def test_a_hold_whose_caller_has_not_ended_has_no_deadline(self) -> None:
        row = _held("child_a")

        assert retention.is_held_row(row, now=_NOW) is True
        assert retention.is_expired_hold(row, now=_NOW) is False

    def test_a_hold_lasts_until_its_deadline(self) -> None:
        later = _held("child_a", until=(_NOW + timedelta(minutes=1)).isoformat())
        due = _held("child_a", until=_NOW.isoformat())

        assert retention.is_held_row(later, now=_NOW) is True
        assert retention.is_expired_hold(later, now=_NOW) is False
        assert retention.is_held_row(due, now=_NOW) is False
        assert retention.is_expired_hold(due, now=_NOW) is True

    def test_a_deadline_without_a_zone_is_utc(self) -> None:
        row = _held("child_a", until="2026-03-01T11:59:00")

        assert retention.retention_until(row["retain"]) == _NOW - timedelta(minutes=1)
        assert retention.is_expired_hold(row, now=_NOW) is True

    def test_an_unreadable_deadline_keeps_the_hold(self) -> None:
        # 判不清就不删：留得久一点只占磁盘，删早了接着做时历史就没了
        row = _held("child_a", until="next tuesday")

        assert retention.retention_until(row["retain"]) is None
        assert retention.is_held_row(row, now=_NOW) is True
        assert retention.is_expired_hold(row, now=_NOW) is False


class TestHeldChildrenAndRegisteredBranches:
    @pytest.mark.parametrize("mode", ["fresh", "fork"])
    def test_a_held_child_outlives_the_ttl(self, data: Path, mode: str) -> None:
        kept = _conversation(data, "child_held", idle_hours=24 * 3)
        _registry(data, {"child_held": _held("child_held", mode)})

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    def test_a_held_child_is_kept_until_its_deadline(self, data: Path) -> None:
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        kept = _conversation(data, "child_held", idle_hours=25)
        _registry(data, {"child_held": _held("child_held", until=until)})

        cleanup.purge_expired_child_sessions()

        assert kept.exists()

    def test_an_expired_hold_is_left_to_the_supervisor(self, data: Path) -> None:
        # 过了保留期的行由 supervisor 的定期清理连登记带文件一起删；这一刻可能又被接着派出去了
        until = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        idle = _conversation(data, "child_held", idle_hours=25)
        recent = _conversation(data, "child_recent", idle_hours=1)
        _registry(data, {
            "child_held": _held("child_held", until=until),
            "child_recent": _held("child_recent", until=until),
        })

        cleanup.purge_expired_child_sessions()

        assert idle.exists()
        assert recent.exists()

    def test_a_reset_row_drops_its_hold(self, data: Path) -> None:
        gone = _conversation(data, "child_held", idle_hours=25)
        _registry(data, {"child_held": {**_held("child_held"), "reset": True}})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()

    def test_a_registered_branch_is_left_to_the_supervisor(self, data: Path, caplog) -> None:
        # 挂起等子任务、续跑都可能让分支闲置一天以上，它还没合并回父会话
        registered = _conversation(data, "branch_7", idle_hours=24 * 3)
        by_field = _conversation(data, "branch_8", idle_hours=24 * 3)
        reset = _conversation(data, "branch_9", idle_hours=25)
        unregistered = _conversation(data, "branch_10", idle_hours=25)
        _registry(data, {
            "branch_7": _branch_row("branch_7"),
            "renamed": _branch_row("branch_8"),
            "branch_9": _branch_row("branch_9", reset=True),
        })

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert registered.exists() and by_field.exists()
        assert not reset.exists()
        assert not unregistered.exists()
        assert any("and 2 registered branches" in r.getMessage() for r in caplog.records)

    def test_a_young_unregistered_branch_still_waits_for_its_age(self, data: Path) -> None:
        young = _conversation(data, "branch_young", idle_hours=3)
        _registry(data, {})

        cleanup.purge_expired_child_sessions()

        assert young.exists()

    def test_the_registry_is_read_when_only_branches_have_expired(self, data: Path) -> None:
        registered = _conversation(data, "branch_7", idle_hours=25)
        _registry(data, {"branch_7": _branch_row("branch_7")})

        cleanup.purge_expired_child_sessions()

        assert registered.exists()


class TestChildTtlInCleanup:
    @pytest.mark.parametrize("raw,idle_hours,deleted", [
        pytest.param("2", 3, True, id="short-ttl-expired"),
        pytest.param("2", 1, False, id="short-ttl-alive"),
        pytest.param("48", 25, False, id="long-ttl-outlives-the-old-24h"),
        pytest.param("48", 49, True, id="long-ttl-expired"),
    ])
    def test_unregistered_children_follow_the_configured_ttl(
        self, data: Path, raw: str, idle_hours: float, deleted: bool,
    ) -> None:
        _child_ttl(data.parent, raw)
        path = _conversation(data, "child_tmp", idle_hours=idle_hours)
        _registry(data, {})

        cleanup.purge_expired_child_sessions()

        assert path.exists() is not deleted

    @pytest.mark.parametrize("raw,effective_hours", [
        pytest.param("0", 1, id="zero-clamps-to-one"),
        pytest.param("-5", 1, id="negative-clamps-to-one"),
        pytest.param("abc", 24, id="not-a-number"),
        pytest.param("null", 24, id="null"),
    ])
    def test_an_odd_ttl_is_clamped_or_falls_back(self, data: Path, raw: str, effective_hours: int) -> None:
        _child_ttl(data.parent, raw)
        alive = _conversation(data, "child_alive", idle_hours=effective_hours - 0.5)
        gone = _conversation(data, "child_gone", idle_hours=effective_hours + 1)

        cleanup.purge_expired_child_sessions()

        assert alive.exists()
        assert not gone.exists()

    def test_the_summary_line_names_the_ttl_in_use(self, data: Path, caplog) -> None:
        _child_ttl(data.parent, "6")

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert any("ttl_hours=6" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("raw", ["2", "100"])
    def test_branches_ignore_the_child_ttl(self, data: Path, raw: str) -> None:
        _child_ttl(data.parent, raw)
        young_branch = _conversation(data, "branch_young", idle_hours=3)
        old_branch = _conversation(data, "branch_old", idle_hours=25)

        cleanup.purge_expired_child_sessions()

        assert young_branch.exists()
        assert not old_branch.exists()

    def test_a_long_ttl_keeps_a_child_the_branch_rule_would_drop(self, data: Path) -> None:
        _child_ttl(data.parent, "100")
        child = _conversation(data, "child_tmp", idle_hours=25)
        branch = _conversation(data, "branch_7", idle_hours=25)

        cleanup.purge_expired_child_sessions()

        assert child.exists()
        assert not branch.exists()

    def test_the_branch_age_is_its_own_constant(self) -> None:
        assert cleanup.BRANCH_CONVERSATION_MAX_AGE == 24 * 3600
        assert not hasattr(cleanup, "CHILD_SESSION_MAX_AGE")

    def test_an_unreadable_runtime_falls_back_to_the_default_ttl(self, data: Path, monkeypatch, caplog) -> None:
        def _boom(_workspace_root: Path) -> dict[str, Any]:
            raise RuntimeError("runtime layer exploded")

        monkeypatch.setattr(cleanup, "load_runtime_config", _boom)
        alive = _conversation(data, "child_alive", idle_hours=23)
        gone = _conversation(data, "child_gone", idle_hours=25)

        with caplog.at_level(logging.WARNING):
            cleanup.purge_expired_child_sessions()

        assert alive.exists()
        assert not gone.exists()
        assert any("falling back to the default 24h ttl" in r.getMessage() for r in caplog.records)

    def test_a_broken_runtime_yaml_falls_back_to_the_default_ttl(self, data: Path) -> None:
        config = data.parent / "config" / "runtime.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("engine: [\n", encoding="utf-8")
        alive = _conversation(data, "child_alive", idle_hours=23)
        gone = _conversation(data, "child_gone", idle_hours=25)

        cleanup.purge_expired_child_sessions()

        assert alive.exists()
        assert not gone.exists()


class TestSharedRegistryReader:
    def test_a_non_object_registry_stops_the_task_conversation_sweep_too(self, data: Path) -> None:
        # SessionStore 只认以 session_id 为键的对象；别的形状一律当读不懂，宁可这一轮不删。
        session_id = "ghost"
        path = _conversation(data, session_id, idle_hours=24 * 30)
        (data / "sessions.json").write_text(json.dumps([{
            "session_id": session_id,
            "conversation_key": conversation_namespace("qq_group:5edca2992de82f26297b3a68"),
        }]), encoding="utf-8")

        cleanup.purge_expired_task_conversations()

        assert path.exists()

    def test_a_missing_file_is_none_and_an_object_is_returned_as_is(self, data: Path) -> None:
        assert cleanup._load_session_registry() is None

        rows = {"child_a": _row("child_a", "accumulate")}
        _registry(data, rows)

        assert cleanup._load_session_registry() == rows


def _supervisor(data: Path) -> SupervisorState:
    workspace = data.parent
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(data / "events.jsonl", run_id="run-child-retention"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _supervisor_sweep(state: SupervisorState) -> None:
    with state._lock:
        state._cleanup_stale_sessions_locked()
    # 删文件放在锁外的后台线程里
    for thread in threading.enumerate():
        if thread.name == "stale-session-purge":
            thread.join(timeout=10)


def _idle_for(state: SupervisorState, session_id: str, hours: float) -> None:
    state._session_store.set_entry_field(session_id, "last_active_at", (_now() - timedelta(hours=hours)).isoformat())


def _child_task(task_id: str, child_session_id: str, status: TaskStatus) -> Task:
    moment = _now()
    return Task(
        task_id=task_id, session_id=child_session_id, kind=TaskKind.node, node_id="helper.node",
        caller_task_id="task-caller", status=status,
        input={"child_session_id": child_session_id, "context_mode": "fresh"},
        created_at=moment, updated_at=moment,
    )


class TestSupervisorAndCleanupAgree:
    def test_registry_rows_and_history_files_survive_or_go_together(self, data: Path) -> None:
        _child_ttl(data.parent, "6")
        state = _supervisor(data)
        parent = state.get_or_create_session(channel="test", conversation_key="test:child-retention")
        accumulate_sid, _ = state.get_or_create_child_session(parent, "scout.node", "case", "accumulate")
        fresh_sid, _ = state.get_or_create_child_session(parent, "helper.node", "case", "fresh")
        for session_id in (accumulate_sid, fresh_sid):
            _idle_for(state, session_id, 10)
        accumulate_file = _conversation(data, accumulate_sid, idle_hours=10)
        fresh_file = _conversation(data, fresh_sid, idle_hours=10)

        cleanup.purge_expired_child_sessions()

        assert accumulate_file.exists() and fresh_file.exists()

        _supervisor_sweep(state)

        registry = json.loads((data / "sessions.json").read_text(encoding="utf-8"))
        assert accumulate_file.exists() and accumulate_sid in registry
        assert not fresh_file.exists() and fresh_sid not in registry
        reused, is_new = state.get_or_create_child_session(parent, "scout.node", "case", "accumulate")
        assert (reused, is_new) == (accumulate_sid, False)

    def test_a_suspended_sync_child_outlives_the_ttl_on_both_sides(self, data: Path) -> None:
        # TTL 比任务总寿命短时，挂起等孙任务的 fresh 子任务文件会闲置过 TTL；恢复时历史不能是空的
        _child_ttl(data.parent, "1")
        state = _supervisor(data)
        parent = state.get_or_create_session(channel="test", conversation_key="test:suspended-child")
        child_sid, _ = state.get_or_create_child_session(parent, "helper.node", "case", "fresh")
        _idle_for(state, child_sid, 3)
        history = _conversation(data, child_sid, idle_hours=3)
        waiting = _child_task("task-child", child_sid, TaskStatus.suspended)
        with state._lock:
            state.tasks[waiting.task_id] = waiting

        cleanup.purge_expired_child_sessions()
        _supervisor_sweep(state)

        assert history.exists()
        assert state._session_store.get_entry(child_sid) is not None

        with state._lock:
            waiting.status = TaskStatus.completed
        cleanup.purge_expired_child_sessions()
        _supervisor_sweep(state)

        assert not history.exists()
        assert state._session_store.get_entry(child_sid) is None

    def test_an_expired_hold_goes_with_its_registry_row(self, data: Path) -> None:
        state = _supervisor(data)
        parent = state.get_or_create_session(channel="test", conversation_key="test:expired-hold")
        child_sid, _ = state.get_or_create_child_session(parent, "helper.node", "case", "fresh")
        until = (_now() - timedelta(minutes=5)).isoformat()
        state._session_store.set_entry_field(
            child_sid, retention.RETAIN_FIELD,
            {"task_id": "task-child", "parent_task_id": "task-caller", "until": until},
        )
        history = _conversation(data, child_sid, idle_hours=25)

        cleanup.purge_expired_child_sessions()

        assert history.exists()

        _supervisor_sweep(state)

        assert not history.exists()
        assert state._session_store.get_entry(child_sid) is None


class TestNoHardCodedChildTtl:
    _SUPERVISOR_SITES = ("_is_child_session_expired", "_reconcile_after_restart", "_cleanup_stale_sessions_locked")

    def _function(self, name: str) -> str:
        tree = ast.parse((_ROOT / "supervisor" / "state.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.unparse(node)
        raise AssertionError(f"supervisor/state.py 里找不到 {name}")

    @pytest.mark.parametrize("name", _SUPERVISOR_SITES)
    def test_each_supervisor_site_reads_the_shared_ttl(self, name: str) -> None:
        body = self._function(name)

        assert "_child_session_ttl()" in body
        assert "hours=24" not in body

    def test_the_shared_ttl_goes_through_the_retention_rule(self) -> None:
        assert "child_session_ttl_hours(" in self._function("_child_session_ttl")
