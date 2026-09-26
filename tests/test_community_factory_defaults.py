from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from supervisor.community.api import create_router
from supervisor.community.service import CommunityService, DEFAULT_SETTINGS, POLICY_DEFAULTS, SettingsConflict
from supervisor.feature_auth import FeatureActor


FACTORY = {
    "response_policy_enabled": False,
    "topic_enabled": True,
    "merge_window_sec": 8.0,
    "merge_max_wait_sec": 10.0,
    "reply_budget_per_minute": 6,
}
LEGACY = {
    "response_policy_enabled": False,
    "topic_enabled": False,
    "merge_window_sec": 0.0,
    "merge_max_wait_sec": 4.0,
    "reply_budget_per_minute": 6,
}
NO_TABLE = object()
NO_ROW = object()


@pytest.fixture()
def admin():
    return FeatureActor(owner="console:admin", is_admin=True, role="admin", channel="web")


def database_path(root):
    path = Path(root) / "data" / "community.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def database_snapshot(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0], tuple(db.iterdump())


def legacy_database(root, version, values=NO_TABLE, *, revision=7, groups=None):
    path = database_path(root)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE groups (
                scope TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}',
                guide TEXT NOT NULL DEFAULT '{}', quiet TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 0
            )
        """)
        for scope, settings in (groups or {}).items():
            raw = settings if isinstance(settings, str) else json.dumps(settings, indent=2)
            db.execute("INSERT INTO groups VALUES (?,?,?,?,?)", (
                scope, raw, '{"rules":"synthetic rules"}',
                '{"mode":"listen","expires_at":9999999999}', 9,
            ))
        if values is not NO_TABLE:
            db.execute("""
                CREATE TABLE conversation_defaults (
                    id INTEGER PRIMARY KEY CHECK (id=1), settings TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 0
                )
            """)
            if values is not NO_ROW:
                raw = values if isinstance(values, str) else json.dumps(values, indent=2)
                db.execute("INSERT INTO conversation_defaults VALUES (1,?,?)", (raw, revision))
        db.execute(f"PRAGMA user_version={version}")
    return path


@pytest.mark.parametrize("existing", ["missing", "empty", "unrelated_table"])
def test_fresh_database_inherits_factory_values_without_persisting_overrides(tmp_path, admin, existing):
    path = database_path(tmp_path)
    if existing != "missing":
        with sqlite3.connect(path) as db:
            if existing == "unrelated_table":
                db.execute("CREATE TABLE unrelated(value TEXT)")
                db.execute("INSERT INTO unrelated VALUES ('synthetic')")
    service = CommunityService(tmp_path)
    assert POLICY_DEFAULTS == FACTORY
    assert DEFAULT_SETTINGS == {**LEGACY, "welcome_enabled": False}
    assert service.defaults(admin) == {"settings": FACTORY, "values": {}, "revision": 0}
    for scope in ("qq_group:fresh", "qq_private:fresh"):
        state = service.state(replace(admin, scope=scope))
        assert state["settings"] == {**FACTORY, "welcome_enabled": False}
        assert state["defaults"] == FACTORY
        assert state["overrides"] == {} and state["revision"] == state["defaults_revision"] == 0
        assert set(state["inherited_fields"]) == set(FACTORY)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT settings,revision FROM conversation_defaults").fetchall() == [("{}", 0)]
        assert db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        if existing == "unrelated_table":
            assert db.execute("SELECT value FROM unrelated").fetchall() == [("synthetic",)]
    before = database_snapshot(path)
    assert CommunityService(tmp_path).defaults(admin) == service.defaults(admin)
    assert database_snapshot(path) == before


def test_fresh_api_returns_factory_values_for_defaults_group_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda request: None)
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(workspace_root=tmp_path)))
    with TestClient(app) as client:
        response = client.get("/v1/community/settings/defaults")
        assert response.status_code == 200
        assert response.json() == {"values": {}, "settings": FACTORY, "revision": 0}
        for scope in ("qq_group:api", "qq_private:api"):
            response = client.get("/v1/community/state", params={"scope": scope})
            assert response.status_code == 200
            state = response.json()
            assert state["settings"] == {**FACTORY, "welcome_enabled": False}
            assert state["overrides"] == {} and state["defaults_revision"] == 0


@pytest.mark.parametrize("scope", ["qq_group:topic", "qq_private:topic"])
def test_fresh_topic_tracking_uses_references_and_participants_without_configuration(tmp_path, admin, scope):
    service = CommunityService(tmp_path)
    first_actor = replace(admin, scope=scope, owner="synthetic:first", is_admin=False)
    second_actor = replace(first_actor, owner="synthetic:second")
    first = service.ingest(first_actor, {"message_id": "1", "text": "first topic"})
    second = service.ingest(second_actor, {"message_id": "2", "text": "other topic"})
    same_participant = service.ingest(first_actor, {"message_id": "3", "text": "continuation"})
    referenced = service.ingest(second_actor, {"message_id": "4", "reply_ref": "1", "text": "reply"})
    assert first["topic_id"] and first["topic_id"] != second["topic_id"]
    assert first["topic_id"] == same_participant["topic_id"] == referenced["topic_id"]
    assert {row["ref"] for row in referenced["context"]} == {"1", "3", "4"}
    assert service.state(replace(admin, scope=scope))["overrides"] == {}


@pytest.mark.parametrize("scope", [
    "web:console", "web:legacy", "legacy-web", "agent:child:qq_group:parent", "telegram:group",
    "qq_group:", "qq_private:", "qq_group: ", "qq_private: ",
])
def test_non_qq_scopes_keep_legacy_baseline_before_and_after_default_edits(tmp_path, admin, scope):
    service = CommunityService(tmp_path)
    actor = replace(admin, scope=scope)
    before = service.state(actor)
    assert before["settings"] == {**LEGACY, "welcome_enabled": False}
    assert before["defaults"] == LEGACY
    assert service.ingest(actor, {"message_id": "1", "text": "not a QQ topic"}) == {"topic_id": "", "context": []}
    service.update_defaults(admin, {
        "values": {"response_policy_enabled": True, "topic_enabled": False, "merge_window_sec": 2},
        "expected_revision": 0,
    })
    assert service.state(actor) == before


@pytest.mark.parametrize("version", [0, 1, 2])
@pytest.mark.parametrize("values", [
    pytest.param(NO_TABLE, id="missing-table"), pytest.param(NO_ROW, id="missing-row"),
    pytest.param({}, id="empty-map"), pytest.param({"topic_enabled": True}, id="sparse-topic"),
    pytest.param({"merge_max_wait_sec": 4}, id="sparse-max-without-window"),
    pytest.param({"response_policy_enabled": False, "merge_window_sec": 0, "merge_max_wait_sec": 0}, id="false-zero"),
    pytest.param({**LEGACY, "topic_enabled": True, "merge_window_sec": 2, "merge_max_wait_sec": 5}, id="full-map"),
])
def test_legacy_migration_freezes_effective_defaults_and_preserves_every_group_byte(tmp_path, admin, version, values):
    groups = {
        "qq_group:partial": {"merge_max_wait_sec": 4, "welcome_enabled": True},
        "qq_private:explicit": {"topic_enabled": False, "merge_window_sec": 0},
        "web:console": {"topic_enabled": True, "merge_window_sec": 0, "merge_max_wait_sec": 0},
    }
    path = legacy_database(tmp_path, version, values, groups=groups)
    with sqlite3.connect(path) as db:
        before_groups = db.execute("SELECT * FROM groups ORDER BY scope").fetchall()
        before_default = None if values is NO_TABLE else db.execute("SELECT * FROM conversation_defaults").fetchone()
    explicit = {} if values is NO_TABLE or values is NO_ROW else values
    expected = {**LEGACY, **explicit}
    revision = 0 if before_default is None else 7 + (set(explicit) != set(LEGACY))
    service = CommunityService(tmp_path)
    assert service.defaults(admin) == {"values": expected, "settings": expected, "revision": revision}
    for scope, local in groups.items():
        state = service.state(replace(admin, scope=scope))
        inherited = LEGACY if scope.startswith("web:") else expected
        assert state["settings"] == {**inherited, "welcome_enabled": False, **local}
        assert state["revision"] == 9
        assert state["defaults_revision"] == (0 if scope.startswith("web:") else revision)
        assert state["guide"] == {"rules": "synthetic rules"}
        assert state["quiet"] == {"mode": "listen", "expires_at": 9999999999}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT * FROM groups ORDER BY scope").fetchall() == before_groups
        if set(explicit) == set(LEGACY):
            assert db.execute("SELECT * FROM conversation_defaults").fetchone() == before_default
    before_restart = database_snapshot(path)
    assert CommunityService(tmp_path).defaults(admin) == service.defaults(admin)
    assert database_snapshot(path) == before_restart


@pytest.mark.parametrize("version", [1, 2])
def test_versioned_legacy_database_without_tables_is_not_treated_as_fresh(tmp_path, admin, version):
    with sqlite3.connect(database_path(tmp_path)) as db:
        db.execute(f"PRAGMA user_version={version}")
    assert CommunityService(tmp_path).defaults(admin) == {"values": LEGACY, "settings": LEGACY, "revision": 0}


@pytest.mark.parametrize("raw", [
    "{broken", "[]", '{"topic_enabled":"false"}', '{"merge_window_sec":"1.0"}',
    '{"merge_window_sec":5}', '{"unexpected":true}', '{"merge_max_wait_sec":null}',
])
@pytest.mark.parametrize("version", [0, 1, 2])
def test_invalid_legacy_defaults_fail_without_partial_schema_or_data_migration(tmp_path, raw, version):
    path = legacy_database(tmp_path, version, raw, groups={"qq_group:kept": {"topic_enabled": False}})
    before = database_snapshot(path)
    with pytest.raises(ValueError):
        CommunityService(tmp_path)
    assert database_snapshot(path) == before


@pytest.mark.parametrize("raw", ["{broken", "[]", '{"topic_enabled":"false"}', '{"merge_window_sec":5}'])
def test_damaged_legacy_group_remains_local_to_that_group_after_migration(tmp_path, admin, raw):
    path = legacy_database(tmp_path, 2, {}, groups={"qq_group:damaged": raw, "qq_group:healthy": {}})
    with sqlite3.connect(path) as db:
        before_groups = db.execute("SELECT * FROM groups ORDER BY scope").fetchall()
    service = CommunityService(tmp_path)
    assert service.state(replace(admin, scope="qq_group:healthy"))["settings"] == {**LEGACY, "welcome_enabled": False}
    with pytest.raises(ValueError):
        service.state(replace(admin, scope="qq_group:damaged"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM groups ORDER BY scope").fetchall() == before_groups
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("initial", ["fresh", "legacy"])
def test_initialization_failure_rolls_back_schema_values_and_version_together(tmp_path, monkeypatch, initial):
    path = legacy_database(tmp_path, 2, {}) if initial == "legacy" else database_path(tmp_path)
    before = database_snapshot(path)
    original_connect = sqlite3.connect
    statements = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(sql.upper().split())
            statements.append(normalized)
            if normalized == "PRAGMA USER_VERSION=3":
                raise RuntimeError("synthetic initialization failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FailingConnection)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(RuntimeError, match="synthetic initialization failure"):
            CommunityService(tmp_path)
    assert "PRAGMA USER_VERSION=3" in statements
    assert statements.index("BEGIN IMMEDIATE") < next(index for index, sql in enumerate(statements) if sql.startswith("CREATE TABLE"))
    assert any(sql.startswith("UPDATE CONVERSATION_DEFAULTS" if initial == "legacy" else "INSERT OR IGNORE INTO CONVERSATION_DEFAULTS") for sql in statements)
    assert database_snapshot(path) == before


def test_future_version_keeps_unknown_settings_and_does_not_rewrite_history(tmp_path):
    service = CommunityService(tmp_path)
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE conversation_defaults SET settings=?,revision=31 WHERE id=1", ('{ "future_policy": {"mode":"new"}, "topic_enabled": false }',))
        db.execute("INSERT INTO groups(scope,settings,revision) VALUES (?,?,?)", ("qq_group:future", '{"future_override":true}', 42))
        db.execute("PRAGMA user_version=99")
    before = database_snapshot(service.path)
    CommunityService(tmp_path)
    assert database_snapshot(service.path) == before


@pytest.mark.parametrize("initial", ["fresh", "legacy"])
def test_concurrent_initializers_agree_and_migrate_only_once(tmp_path, admin, initial):
    if initial == "legacy":
        legacy_database(tmp_path, 2, {"topic_enabled": True})
    barrier = Barrier(6)

    def initialize(_):
        barrier.wait(timeout=10)
        return CommunityService(tmp_path).defaults(admin)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(initialize, range(6)))
    expected = {**LEGACY, "topic_enabled": True} if initial == "legacy" else FACTORY
    revision = 8 if initial == "legacy" else 0
    assert results == [{"settings": expected, "values": expected if initial == "legacy" else {}, "revision": revision}] * 6
    assert database_snapshot(database_path(tmp_path))[0] == 3


def test_old_and_new_instances_keep_independent_values_across_migration_and_restart(tmp_path, admin):
    legacy_database(tmp_path / "old", 2, {"topic_enabled": False, "merge_window_sec": 0})
    old = CommunityService(tmp_path / "old")
    fresh = CommunityService(tmp_path / "fresh")
    assert old.defaults(admin)["settings"] == LEGACY
    assert fresh.defaults(admin)["settings"] == FACTORY
    fresh.update_defaults(admin, {"values": {"topic_enabled": False}, "expected_revision": 0})
    old.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 8})
    assert CommunityService(tmp_path / "old").defaults(admin)["settings"] == {**LEGACY, "topic_enabled": True}
    assert CommunityService(tmp_path / "fresh").defaults(admin)["settings"] == {**FACTORY, "topic_enabled": False}


def test_migration_revision_rejects_pre_upgrade_draft_without_losing_it(tmp_path, admin):
    legacy_database(tmp_path, 2, {"topic_enabled": False}, revision=4)
    service = CommunityService(tmp_path)
    before = database_snapshot(service.path)
    with pytest.raises(SettingsConflict):
        service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 4})
    assert database_snapshot(service.path) == before
    saved = service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 5})
    assert saved["revision"] == 6 and saved["settings"]["topic_enabled"] is True


def test_explicit_factory_value_is_still_an_override_with_its_own_revision(tmp_path, admin):
    service = CommunityService(tmp_path)
    saved = service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
    assert saved == {"settings": FACTORY, "values": {"topic_enabled": True}, "revision": 1}
    assert service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 1}) == saved
    reset = service.update_defaults(admin, {"reset_fields": ["topic_enabled"], "expected_revision": 1})
    assert reset == {"settings": FACTORY, "values": {}, "revision": 2}


def test_legacy_defaults_reset_adopts_factory_values_and_keeps_unrelated_group_data(tmp_path, admin):
    path = legacy_database(tmp_path, 2, {}, groups={"qq_group:kept": {"welcome_enabled": True}})
    service = CommunityService(tmp_path)
    group = replace(admin, scope="qq_group:kept")
    previous = service.state(group)
    reset = service.update_defaults(admin, {"reset_fields": list(FACTORY), "expected_revision": 8})
    assert reset == {"values": {}, "settings": FACTORY, "revision": 9}
    current = service.state(group)
    assert current["settings"] == {**FACTORY, "welcome_enabled": True}
    for field in ("revision", "overrides", "guide", "quiet"):
        assert current[field] == previous[field]
    before = database_snapshot(path)
    assert CommunityService(tmp_path).defaults(admin) == reset
    assert database_snapshot(path) == before


def test_factory_reset_conflicting_with_legacy_local_wait_is_atomic_and_recoverable(tmp_path, admin):
    path = legacy_database(tmp_path, 2, {}, groups={"qq_group:limited": {"merge_max_wait_sec": 4}})
    service = CommunityService(tmp_path)
    before = database_snapshot(path)
    with pytest.raises(ValueError, match="覆盖冲突"):
        service.update_defaults(admin, {"reset_fields": list(FACTORY), "expected_revision": 8})
    assert database_snapshot(path) == before
    actor = replace(admin, scope="qq_group:limited")
    cleared = service.update_overrides(actor, {"reset_fields": ["merge_max_wait_sec"], "expected_revision": 9, "expected_defaults_revision": 8})
    assert cleared["overrides"] == {} and cleared["revision"] == 10
    reset = service.update_defaults(admin, {"reset_fields": list(FACTORY), "expected_revision": 8})
    assert reset == {"values": {}, "settings": FACTORY, "revision": 9}
    assert service.state(actor)["settings"] == {**FACTORY, "welcome_enabled": False}


def test_single_field_reset_does_not_create_an_invalid_pair_in_migrated_defaults(tmp_path, admin):
    path = legacy_database(tmp_path, 2, {"merge_max_wait_sec": 4})
    service = CommunityService(tmp_path)
    before = database_snapshot(path)
    with pytest.raises(ValueError, match="最长等待"):
        service.update_defaults(admin, {"reset_fields": ["merge_window_sec"], "expected_revision": 8})
    assert database_snapshot(path) == before
    reset = service.update_defaults(admin, {"reset_fields": ["merge_window_sec", "merge_max_wait_sec"], "expected_revision": 8})
    assert reset["settings"] == {**LEGACY, "merge_window_sec": 8, "merge_max_wait_sec": 10}


class InitializationClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        assert delay > 0
        self.sleeps.append(delay)
        self.now += delay


class WalConnection:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.timeout = 15000
        self.timeouts = []
        self.wal_attempts = 0

    def execute(self, sql):
        if sql == "PRAGMA busy_timeout":
            return SimpleNamespace(fetchone=lambda: (self.timeout,))
        if sql.startswith("PRAGMA busy_timeout="):
            self.timeout = int(sql.partition("=")[2])
            self.timeouts.append(self.timeout)
            return None
        assert sql == "PRAGMA journal_mode=WAL"
        self.wal_attempts += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(fetchone=lambda: (outcome,))


def sqlite_error(code):
    error = sqlite3.OperationalError("synthetic SQLite failure")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | 256, sqlite3.SQLITE_LOCKED | 256])
def test_wal_lock_retry_is_bounded_and_restores_connection_timeout(monkeypatch, code):
    clock = InitializationClock()
    monkeypatch.setattr("supervisor.community.service.time", clock)
    db = WalConnection([sqlite_error(code), "wal"])
    CommunityService._enable_wal(db)
    assert db.wal_attempts == 2
    assert clock.sleeps == [0.01]
    assert db.timeout == 15000
    assert db.timeouts == [250, 250, 15000]


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CORRUPT])
def test_wal_non_lock_errors_are_not_retried(monkeypatch, code):
    clock = InitializationClock()
    monkeypatch.setattr("supervisor.community.service.time", clock)
    error = sqlite_error(code)
    db = WalConnection([error])
    with pytest.raises(sqlite3.OperationalError) as caught:
        CommunityService._enable_wal(db)
    assert caught.value is error
    assert db.wal_attempts == 1 and clock.sleeps == []
    assert db.timeout == 15000


def test_wal_mode_must_really_be_enabled(monkeypatch):
    clock = InitializationClock()
    monkeypatch.setattr("supervisor.community.service.time", clock)
    db = WalConnection(["delete"])
    with pytest.raises(sqlite3.OperationalError, match="启用 WAL"):
        CommunityService._enable_wal(db)
    assert db.wal_attempts == 1 and clock.sleeps == []
    assert db.timeout == 15000


@pytest.mark.parametrize("initial", ["fresh", "legacy"])
def test_wal_retry_expiry_leaves_schema_and_settings_unmodified(tmp_path, monkeypatch, initial):
    path = legacy_database(tmp_path, 2, {"merge_max_wait_sec": 4}) if initial == "legacy" else database_path(tmp_path)
    before = database_snapshot(path)
    original_connect = sqlite3.connect
    clock = InitializationClock()
    statements = []
    error = sqlite_error(sqlite3.SQLITE_BUSY)

    class BusyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            if sql == "PRAGMA journal_mode=WAL":
                raise error
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=BusyConnection)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        patch.setattr("supervisor.community.service.time", clock)
        with pytest.raises(sqlite3.OperationalError) as caught:
            CommunityService(tmp_path)
    assert caught.value is error
    assert clock.now == pytest.approx(15)
    assert statements.count("PRAGMA journal_mode=WAL") > 1
    assert not any(sql.startswith(("BEGIN", "CREATE", "INSERT", "UPDATE")) for sql in statements)
    assert statements[-1] == "PRAGMA busy_timeout=15000"
    assert database_snapshot(path) == before
