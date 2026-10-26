"""accumulate 子会话的历史不按空闲时长删；子会话 TTL 在 supervisor 和 data_cleanup 读同一个 engine.child_session.ttl_hours。

supervisor 永久留着 accumulate 的登记，data_cleanup 却按修改时间删空闲满一天的 child_*.jsonl：登记还在、历史没了。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
import time
from datetime import timedelta
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
        assert cleanup.is_retained_child_row is retention.is_retained_child_row


class TestChildConversationRetention:
    def test_an_idle_accumulate_child_keeps_its_history(self, data: Path, caplog) -> None:
        kept = _conversation(data, "child_acc", idle_hours=24 * 30)
        _registry(data, {"child_acc": _row("child_acc", "accumulate")})

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert kept.exists()
        assert any("kept 1 child sessions past the ttl" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("mode", ["fresh", "fork"])
    def test_idle_fresh_and_fork_children_are_deleted(self, data: Path, mode: str) -> None:
        gone = _conversation(data, "child_tmp", idle_hours=25)
        _registry(data, {"child_tmp": _row("child_tmp", mode)})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()

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

    def test_a_legacy_row_without_a_mode_does_not_protect_either(self, data: Path) -> None:
        gone = _conversation(data, "child_old", idle_hours=25)
        _registry(data, {"child_old": _row("child_old", "")})

        cleanup.purge_expired_child_sessions()

        assert not gone.exists()

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
    def test_an_unreadable_registry_keeps_every_child_but_not_branches(
        self, data: Path, caplog, broken: str,
    ) -> None:
        children = [
            _conversation(data, "child_acc", idle_hours=24 * 30),
            _conversation(data, "child_tmp", idle_hours=25),
        ]
        branch = _conversation(data, "branch_7", idle_hours=25)
        (data / "sessions.json").write_text(broken, encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cleanup.purge_expired_child_sessions()

        assert all(path.exists() for path in children)
        assert not branch.exists()
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
        branch = _conversation(data, "branch_7", idle_hours=25)
        _registry(data, {
            "child_acc": _row("child_acc", "accumulate"),
            "child_tmp": _row("child_tmp", "fresh"),
        })
        monkeypatch.setattr(cleanup, "DRY_RUN", True)

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_child_sessions()

        assert accumulate.exists() and fresh.exists() and branch.exists()
        reported = [r.getMessage() for r in caplog.records if "would delete file" in r.getMessage()]
        assert any("child_tmp.jsonl" in message for message in reported)
        assert any("branch_7.jsonl" in message for message in reported)
        assert not any("child_acc.jsonl" in message for message in reported)


class TestChildTtlInCleanup:
    @pytest.mark.parametrize("raw,idle_hours,deleted", [
        pytest.param("2", 3, True, id="short-ttl-expired"),
        pytest.param("2", 1, False, id="short-ttl-alive"),
        pytest.param("48", 25, False, id="long-ttl-outlives-the-old-24h"),
        pytest.param("48", 49, True, id="long-ttl-expired"),
    ])
    def test_children_follow_the_configured_ttl(
        self, data: Path, raw: str, idle_hours: float, deleted: bool,
    ) -> None:
        _child_ttl(data.parent, raw)
        path = _conversation(data, "child_tmp", idle_hours=idle_hours)
        _registry(data, {"child_tmp": _row("child_tmp", "fresh")})

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


class TestSupervisorAndCleanupAgree:
    def test_registry_rows_and_history_files_survive_or_go_together(self, data: Path) -> None:
        workspace = data.parent
        _child_ttl(workspace, "6")
        state = SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(data / "events.jsonl", run_id="run-child-retention"),
            policy=PolicyEngine(workspace_root=workspace),
        )
        parent = state.get_or_create_session(channel="test", conversation_key="test:child-retention")
        accumulate_sid, _ = state.get_or_create_child_session(parent, "scout.node", "case", "accumulate")
        fresh_sid, _ = state.get_or_create_child_session(parent, "helper.node", "case", "fresh")
        idle_since = (_now() - timedelta(hours=10)).isoformat()
        for session_id in (accumulate_sid, fresh_sid):
            state._session_store.set_entry_field(session_id, "last_active_at", idle_since)
        accumulate_file = _conversation(data, accumulate_sid, idle_hours=10)
        fresh_file = _conversation(data, fresh_sid, idle_hours=10)

        cleanup.purge_expired_child_sessions()
        with state._lock:
            state._cleanup_stale_sessions_locked()

        registry = json.loads((data / "sessions.json").read_text(encoding="utf-8"))
        assert accumulate_file.exists() and accumulate_sid in registry
        assert not fresh_file.exists() and fresh_sid not in registry
        reused, is_new = state.get_or_create_child_session(parent, "scout.node", "case", "accumulate")
        assert (reused, is_new) == (accumulate_sid, False)


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
