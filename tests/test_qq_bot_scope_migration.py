"""按 bot 账号隔离会话与记忆的契约，以及整体搬迁到另一个号的行为。"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import conversation_hash as ch  # noqa: E402
from supervisor._helpers import SessionInfo  # noqa: E402
from adapters._shared.active_account import ActiveAccountStore, normalize_account  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.conversation_stores import ConversationStoreError  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.reminders import ReminderService  # noqa: E402
from supervisor.scheduler import load_schedules, save_schedules  # noqa: E402
from supervisor.session_store import SessionStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from workspace import workspace_instance  # noqa: E402

_migrate_spec = importlib.util.spec_from_file_location(
    "_test_migrate_scope", _ROOT / "deploy" / "migrate_qq_bot_scope.py",
)
assert _migrate_spec and _migrate_spec.loader
migrate = importlib.util.module_from_spec(_migrate_spec)
sys.modules["_test_migrate_scope"] = migrate
_migrate_spec.loader.exec_module(migrate)

_SECRET = "a" * 64
_REAL = "qq_group:123456789"
_OTHER_REAL = "qq_group:987654321"
_OLD_BOT = "10000001"
_NEW_BOT = "10000002"
_QQ_ENVS = ("ONEBOT_CONVERSATION_HASH_SECRET", "ONEBOT_CONVERSATION_HASH_SECRET_FILE", "ONEBOT_STATE_FILE")


@pytest.fixture(autouse=True)
def _no_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 密钥和路由文件的位置都认 env，开发机上设着的话会读到别处去。先登记一次，命令行从 .env 灌进来的测完也撤掉。
    for name in (*_QQ_ENVS, "CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _store(workspace: Path) -> ActiveAccountStore:
    return ActiveAccountStore(QQ_NAPCAT, workspace)


def _ns(stable: str) -> str:
    return "conv_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _seed(tmp_path: Path, *, scope: str = "") -> dict:
    """造一个按 scope 作用域落盘的工作区。scope="" 就是加隔离之前的存量形态。"""
    stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=scope)}"
    ns = _ns(stable)
    data = tmp_path / "data"
    (data / "memory" / ns).mkdir(parents=True)
    (data / "memory" / ns / "book.yaml").write_text("book: x\n", encoding="utf-8")
    (data / "memory" / ".hit_cache.json").write_text(
        json.dumps({f"{ns}/m1": "2020-01-01T00:00:00+00:00", "flat": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (data / "onebot_plugin_state.json").write_text(
        json.dumps({
            "version": 1,
            "real_conversation_keys": {stable: _REAL},
            "session_targets": {"sid-1": {"type": "group", "group_id": 123456789,
                                          "conversation_key": stable}},
        }),
        encoding="utf-8",
    )
    # supervisor 的会话注册表就是 session_id 到条目的扁平映射，SessionStore 按这个形状读写。
    (data / "sessions.json").write_text(
        json.dumps({"sid-1": {
            "session_id": "sid-1", "conversation_key": stable, "channel": "qq_group",
            "created_at": "2026-01-01T00:00:00+00:00", "reset": False,
        }}),
        encoding="utf-8",
    )
    (data / "onebot_conversation_hash_secret").write_text(_SECRET, encoding="utf-8")
    (data / "attachments" / stable.replace(":", "_")).mkdir(parents=True)
    (data / "attachments" / stable.replace(":", "_") / "a.png").write_bytes(b"x")
    if scope:
        _store(tmp_path).save(scope)
    return {"stable": stable, "ns": ns}


def _add_route(tmp_path: Path, real: str, scope: str, *, memory: bool = True) -> dict:
    """往路由表里再添一个会话：换号之后旧号的键留着给在途回调用，新号一说话又添上新号的键。"""
    stable = f"{real.split(':', 1)[0]}:{ch.digest(real, _SECRET, bot_scope=scope)}"
    path = tmp_path / "data" / "onebot_plugin_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["real_conversation_keys"][stable] = real
    path.write_text(json.dumps(state), encoding="utf-8")
    ns = _ns(stable)
    if memory:
        (tmp_path / "data" / "memory" / ns).mkdir(parents=True)
        (tmp_path / "data" / "memory" / ns / f"{scope or 'unscoped'}.yaml").write_text("x: 1\n", encoding="utf-8")
    return {"stable": stable, "ns": ns}


def _routes(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "data" / "onebot_plugin_state.json").read_text(encoding="utf-8"))["real_conversation_keys"]


def _reminder(workspace: Path, scope: str, bot_scope: str) -> dict:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-reminder"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    service = ReminderService(state)
    try:
        actor = FeatureActor(scope=scope, owner="user:alice", bot_scope=bot_scope, channel="qq_group", user_id="30003")
        return service.create(actor, "交材料", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    finally:
        service.close()


def _stored_reminder(workspace: Path, reminder_id: str) -> dict:
    import sqlite3

    db = sqlite3.connect(workspace / "data" / "reminders" / "reminders.sqlite3")
    try:
        (body,) = db.execute("SELECT body FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    finally:
        db.close()
    return json.loads(body)


class TestScopedDigest:
    def test_no_scope_is_byte_identical_to_before_isolation(self) -> None:
        # 加账号隔离前的摘要输入就是裸 conversation_key，存量目录名必须一个字不差。
        assert ch.digest(_REAL, _SECRET) == ch.digest(_REAL, _SECRET, bot_scope="")

    def test_two_accounts_get_different_namespaces(self) -> None:
        a = ch.digest(_REAL, _SECRET, bot_scope=_OLD_BOT)
        b = ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)
        assert a != b
        assert _ns(f"qq_group:{a}") != _ns(f"qq_group:{b}")

    def test_switching_back_restores_the_old_namespace(self) -> None:
        # 「自动切换」靠的就是这个：换回旧号，摘要自己回到旧值，旧记忆原样可见。
        first = ch.digest(_REAL, _SECRET, bot_scope=_OLD_BOT)
        _ = ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)
        assert ch.digest(_REAL, _SECRET, bot_scope=_OLD_BOT) == first

    def test_a_non_numeric_self_id_is_refused_rather_than_hashed(self) -> None:
        # 拿到脏 self_id 就加作用域，等于把记忆分叉进一个再也算不回来的命名空间。
        assert normalize_account("not-a-qq", QQ_NAPCAT) == ""
        assert normalize_account(None, QQ_NAPCAT) == ""
        assert normalize_account(" 12345 ", QQ_NAPCAT) == "12345"


class TestScopeFile:
    def test_a_missing_file_reads_as_no_scope(self, tmp_path: Path) -> None:
        assert _store(tmp_path).load() == ""

    def test_a_corrupt_file_degrades_instead_of_guessing(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        _store(tmp_path).path.write_text("{not json", encoding="utf-8")
        assert _store(tmp_path).load() == ""

    def test_saving_reports_whether_the_account_actually_changed(self, tmp_path: Path) -> None:
        assert _store(tmp_path).save(_OLD_BOT) is True
        assert _store(tmp_path).save(_OLD_BOT) is False
        assert _store(tmp_path).save(_NEW_BOT) is True
        assert _store(tmp_path).load() == _NEW_BOT

    def test_an_empty_self_id_never_erases_a_known_scope(self, tmp_path: Path) -> None:
        _store(tmp_path).save(_OLD_BOT)
        assert _store(tmp_path).save("") is False
        assert _store(tmp_path).load() == _OLD_BOT


class TestScopeMigration:
    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=False,
        )
        assert report.changed_renames
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert _store(tmp_path).load() == ""

    def test_apply_moves_memory_hitcache_route_sessions_and_attachments(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        new_ns = _ns(new_stable)
        data = tmp_path / "data"

        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True,
        )

        assert (data / "memory" / new_ns / "book.yaml").is_file()
        assert not (data / "memory" / seed["ns"]).exists()
        cache = json.loads((data / "memory" / ".hit_cache.json").read_text(encoding="utf-8"))
        assert f"{new_ns}/m1" in cache and f"{seed['ns']}/m1" not in cache
        assert cache["flat"]  # 扁平 key 不动
        state = json.loads((data / "onebot_plugin_state.json").read_text(encoding="utf-8"))
        assert new_stable in state["real_conversation_keys"]
        assert state["session_targets"]["sid-1"]["conversation_key"] == new_stable
        sessions = json.loads((data / "sessions.json").read_text(encoding="utf-8"))
        assert sessions["sid-1"]["conversation_key"] == new_stable
        assert report.sessions_changed == 1
        assert (data / "attachments" / new_stable.replace(":", "_") / "a.png").is_file()
        assert _store(tmp_path).load() == _NEW_BOT
        assert report.backup_dir is not None and report.backup_dir.is_dir()

    def test_the_backup_holds_the_pre_migration_memory(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True,
        )
        assert (report.backup_dir / seed["ns"] / "book.yaml").is_file()
        backed_up = json.loads((report.backup_dir / "data" / "sessions.json").read_text(encoding="utf-8"))
        assert backed_up["sid-1"]["conversation_key"] == seed["stable"]

    def test_the_registry_the_supervisor_writes_follows_the_move(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        (tmp_path / "data" / "sessions.json").unlink()
        now = datetime.now(timezone.utc)
        SessionStore(tmp_path / "data" / "sessions.json").on_session_created(
            SessionInfo(session_id="sid-real", channel="qq_group", conversation_key=seed["stable"], created_at=now, updated_at=now),
        )

        migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True)

        _sessions, conversation_map, _, _ = SessionStore(tmp_path / "data" / "sessions.json").load()
        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        assert conversation_map == {new_stable: "sid-real"}

    def test_feature_data_follows_the_move_and_the_new_account(self, tmp_path: Path) -> None:
        # 加隔离之前没有账号，功能记录里的账号是工作区实例号，搬到新号名下得换成新号。
        seed = _seed(tmp_path)
        save_schedules(tmp_path, [{"id": "morning", "cron": "0 9 * * *", "text": "早", "conversation_key": seed["stable"]}])
        reminder = _reminder(tmp_path, seed["stable"], workspace_instance(tmp_path))

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True)

        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        assert load_schedules(tmp_path)[0]["conversation_key"] == new_stable
        stored = _stored_reminder(tmp_path, reminder["id"])
        assert (stored["scope"], stored["bot_scope"]) == (new_stable, _NEW_BOT)
        assert report.stores["schedules"].moved == 1 and report.stores["reminders"].moved == 1

    def test_feature_data_under_the_old_account_is_handed_to_the_new_one(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        reminder = _reminder(tmp_path, seed["stable"], _OLD_BOT)

        migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope=_OLD_BOT, apply=True)

        assert _stored_reminder(tmp_path, reminder["id"])["bot_scope"] == _NEW_BOT

    def test_a_dry_run_previews_the_stores_without_writing_them(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        before = (tmp_path / "data" / "sessions.json").read_bytes()

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=False)

        assert report.sessions_changed == 1
        assert (tmp_path / "data" / "sessions.json").read_bytes() == before
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()

    def test_a_store_that_cannot_be_read_stops_before_anything_moves(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        (tmp_path / "data" / "sessions.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(ConversationStoreError):
            migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True)

        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert _store(tmp_path).load() == ""

    def test_the_source_is_detected_when_the_recorded_account_disagrees(self, tmp_path: Path) -> None:
        # 存量数据没有作用域，而 bot 一登录就把当前号记下了，两者天然对不上。
        seed = _seed(tmp_path)
        _store(tmp_path).save(_OLD_BOT)
        assert migrate.detect_source_scope(tmp_path, _SECRET, target_scope=_NEW_BOT) == ""

        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, apply=True,
        )

        assert report.source_scope == ""
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_an_unrecognisable_source_refuses_to_guess(self, tmp_path: Path) -> None:
        _seed(tmp_path, scope="70000009")
        _store(tmp_path).path.write_text(json.dumps({"self_id": _OLD_BOT}), encoding="utf-8")
        assert migrate.detect_source_scope(tmp_path, _SECRET, target_scope=_NEW_BOT) is None
        with pytest.raises(migrate.ScopeMismatch):
            migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

    def test_a_wrong_source_account_aborts_before_touching_anything(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        with pytest.raises(migrate.ScopeMismatch):
            migrate.run_scope_migration(
                workspace=tmp_path, target_scope=_NEW_BOT, source_scope="999", apply=True,
            )
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert _store(tmp_path).load() == _OLD_BOT

    def test_a_second_run_is_idempotent(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True,
        )
        again = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope=_NEW_BOT, apply=True,
        )
        assert again.moved_dirs == []
        assert again.route_keys_changed == 0
        assert again.sessions_changed == 0

    def test_two_accounts_can_hand_the_data_back_and_forth(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        for source, target in ((_OLD_BOT, _NEW_BOT), (_NEW_BOT, _OLD_BOT)):
            migrate.run_scope_migration(
                workspace=tmp_path, target_scope=target, source_scope=source, apply=True,
            )
        memory = tmp_path / "data" / "memory"
        assert (memory / seed["ns"] / "book.yaml").is_file()
        assert _store(tmp_path).load() == _OLD_BOT
        assert sorted(p.name for p in memory.glob("conv_*")) == [seed["ns"]]

    def test_an_unmapped_namespace_dir_is_left_alone_and_reported(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        orphan = tmp_path / "data" / "memory" / "conv_000000000000000000000000"
        orphan.mkdir()
        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True,
        )
        assert orphan.is_dir()
        assert "conv_000000000000000000000000" in report.unknown_conv_dirs

    def test_a_non_numeric_target_is_refused(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        with pytest.raises(ValueError):
            migrate.run_scope_migration(
                workspace=tmp_path, target_scope="not-a-qq", source_scope="", apply=True,
            )

    def test_a_misspelt_source_is_not_taken_for_the_unscoped_data(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        with pytest.raises(ValueError):
            migrate.run_scope_migration(
                workspace=tmp_path, target_scope=_NEW_BOT, source_scope="1000O001", apply=True,
            )
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()


class TestMixedAccounts:
    def test_the_new_accounts_own_keys_do_not_block_the_move(self, tmp_path: Path) -> None:
        # 升级到账号隔离后 bot 一说话就按新号记键，路由表于是混着两种键；原来要求整表同源，这时一律拒绝。
        seed = _seed(tmp_path)
        _store(tmp_path).save(_NEW_BOT)
        theirs = _add_route(tmp_path, _OTHER_REAL, _NEW_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        memory = tmp_path / "data" / "memory"
        assert report.source_scope == "" and report.other_scopes == {}
        assert _routes(tmp_path) == {new_stable: _REAL, theirs["stable"]: _OTHER_REAL}
        assert (memory / _ns(new_stable) / "book.yaml").is_file() and (memory / theirs["ns"]).is_dir()
        assert not (memory / seed["ns"]).exists()

    def test_a_third_accounts_conversations_stay_behind(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        other = _add_route(tmp_path, _OTHER_REAL, _OLD_BOT)
        _store(tmp_path).save(_OLD_BOT)
        _store(tmp_path).save(_NEW_BOT)
        save_schedules(tmp_path, [{"id": "theirs", "cron": "0 9 * * *", "text": "早", "conversation_key": other["stable"]}])

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True)

        assert report.other_scopes == {_OLD_BOT: 1}
        assert _routes(tmp_path)[other["stable"]] == _OTHER_REAL
        assert (tmp_path / "data" / "memory" / other["ns"]).is_dir()
        assert load_schedules(tmp_path)[0]["conversation_key"] == other["stable"]
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_several_candidate_sources_are_not_guessed(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        other = _add_route(tmp_path, _OTHER_REAL, _OLD_BOT)
        _store(tmp_path).save(_OLD_BOT)

        with pytest.raises(migrate.AmbiguousSource) as caught:
            migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert caught.value.scopes == {"": 1, _OLD_BOT: 1}
        assert "<无作用域>" in str(caught.value) and _OLD_BOT in str(caught.value)
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert (tmp_path / "data" / "memory" / other["ns"]).is_dir()

    def test_a_group_both_accounts_remember_is_left_alone(self, tmp_path: Path) -> None:
        # 只跳过记忆目录、照样改路由的话，旧目录在路由表里再也查不到，以后哪次迁移都认领不回来。
        seed = _seed(tmp_path)
        theirs = _add_route(tmp_path, _REAL, _NEW_BOT)
        _store(tmp_path).save(_NEW_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        memory = tmp_path / "data" / "memory"
        assert [rename.old_stable for rename in report.blocked] == [seed["stable"]]
        assert report.moved_dirs == [] and report.sessions_changed == 0
        assert _routes(tmp_path) == {seed["stable"]: _REAL, theirs["stable"]: _REAL}
        assert (memory / seed["ns"] / "book.yaml").is_file()
        assert sorted(path.name for path in (memory / theirs["ns"]).iterdir()) == [f"{_NEW_BOT}.yaml"]
        sessions = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))
        assert sessions["sid-1"]["conversation_key"] == seed["stable"]

    def test_a_group_only_the_target_knows_by_key_takes_the_sources_memory(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        theirs = _add_route(tmp_path, _REAL, _NEW_BOT, memory=False)
        _store(tmp_path).save(_NEW_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert report.blocked == []
        assert _routes(tmp_path) == {theirs["stable"]: _REAL}
        assert (tmp_path / "data" / "memory" / theirs["ns"] / "book.yaml").is_file()

    def test_a_source_without_memory_still_moves_onto_a_target_that_remembers(self, tmp_path: Path) -> None:
        # 只有目标那边有记忆就没什么要合并的：会话和功能数据照样过去，目标的记忆原样不动。
        seed = _seed(tmp_path)
        shutil.rmtree(tmp_path / "data" / "memory" / seed["ns"])
        theirs = _add_route(tmp_path, _REAL, _NEW_BOT)
        _store(tmp_path).save(_NEW_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert report.blocked == [] and report.sessions_changed == 1
        assert _routes(tmp_path) == {theirs["stable"]: _REAL}
        assert sorted(path.name for path in (tmp_path / "data" / "memory" / theirs["ns"]).iterdir()) == [f"{_NEW_BOT}.yaml"]

    def test_running_again_without_a_source_finds_nothing_to_move(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        again = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert again.source_scope == _NEW_BOT
        assert again.moved_dirs == [] and again.route_keys_changed == 0

    def test_a_workspace_without_conversations_has_nothing_to_move(self, tmp_path: Path) -> None:
        _store(tmp_path).save(_OLD_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert report.source_scope == _OLD_BOT and report.renames == []
        assert _store(tmp_path).load() == _NEW_BOT

    def test_keys_already_under_an_unrecorded_target_count_as_the_targets(self, tmp_path: Path) -> None:
        # 迁给一个还没登录过的号（--no-adopt 那种），它名下的键也得认得出来，不然全算成认不出。
        seed = _seed(tmp_path)
        theirs = _add_route(tmp_path, _OTHER_REAL, _NEW_BOT)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        assert report.source_scope == "" and report.unresolved == []
        assert _routes(tmp_path)[theirs["stable"]] == _OTHER_REAL
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_the_previous_account_is_recognised_after_a_switch(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        _store(tmp_path).save(_NEW_BOT)

        assert migrate.detect_source_scope(tmp_path, _SECRET, target_scope=_NEW_BOT) == _OLD_BOT

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)
        assert report.source_scope == _OLD_BOT
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_an_account_napcat_remembers_is_recognised(self, tmp_path: Path) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        _store(tmp_path).path.write_text(json.dumps({"self_id": _NEW_BOT}), encoding="utf-8")
        assert migrate.detect_source_scope(tmp_path, _SECRET, target_scope=_NEW_BOT) is None

        config = tmp_path / "napcat" / "config"
        config.mkdir(parents=True)
        (config / f"onebot11_{_OLD_BOT}.json").write_text("{}", encoding="utf-8")

        assert migrate.detect_source_scope(tmp_path, _SECRET, target_scope=_NEW_BOT) == _OLD_BOT

    def test_an_explicit_source_outside_every_record_is_still_recognised(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope="70000009")
        _store(tmp_path).path.write_text(json.dumps({"self_id": _NEW_BOT}), encoding="utf-8")

        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="70000009", apply=True,
        )

        assert len(report.moved_dirs) == 1 and report.unresolved == []
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_keys_it_cannot_place_are_reported_and_left_alone(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        stray = _add_route(tmp_path, _OTHER_REAL, "70000009")

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True)

        assert [item.old_stable for item in report.unresolved] == [stray["stable"]]
        assert stray["ns"] not in report.unknown_conv_dirs
        assert _routes(tmp_path)[stray["stable"]] == _OTHER_REAL
        assert (tmp_path / "data" / "memory" / stray["ns"]).is_dir()


class TestDeploymentsThatMoveThings:
    """bot 认的密钥和文件位置，迁移也得认；认错了会把全部会话判成「认不出归属」。"""

    def _secret_moves_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "data" / "onebot_conversation_hash_secret").unlink()
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", _SECRET)

    def test_a_secret_configured_in_the_environment_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # deploy/bootstrap.sh 生成的部署就是这样：密钥在 .env 里，自动密钥文件不存在。
        _seed(tmp_path, scope=_OLD_BOT)
        self._secret_moves_to_env(tmp_path, monkeypatch)

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=False)

        assert report.source_scope == _OLD_BOT and len(report.changed_renames) == 1

    def test_a_secret_configured_in_data_config_is_used(self, tmp_path: Path) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        (tmp_path / "data" / "onebot_conversation_hash_secret").unlink()
        (tmp_path / "data" / "config.yaml").write_text(
            f"platforms:\n  qq:\n    conversation_hash_secret: {_SECRET}\n", encoding="utf-8",
        )

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=False)

        assert report.source_scope == _OLD_BOT

    def test_a_moved_secret_file_is_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        moved = tmp_path / "private" / "hash"
        moved.parent.mkdir()
        (tmp_path / "data" / "onebot_conversation_hash_secret").replace(moved)
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", "private/hash")

        report = migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=False)

        assert report.source_scope == _OLD_BOT

    def test_a_moved_route_state_is_migrated_where_it_lives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        moved = tmp_path / "private" / "routes.json"
        moved.parent.mkdir()
        (tmp_path / "data" / "onebot_plugin_state.json").replace(moved)
        monkeypatch.setenv("ONEBOT_STATE_FILE", str(moved))

        migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        assert new_stable in json.loads(moved.read_text(encoding="utf-8"))["real_conversation_keys"]
        assert not (tmp_path / "data" / "onebot_plugin_state.json").exists()

    def test_the_command_line_reads_the_workspace_env_file(self, tmp_path: Path) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        (tmp_path / "data" / "onebot_conversation_hash_secret").unlink()
        (tmp_path / ".env").write_text(f"ONEBOT_CONVERSATION_HASH_SECRET={_SECRET}\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key not in _QQ_ENVS}
        env["PYTHONIOENCODING"] = "utf-8"

        done = subprocess.run(
            [sys.executable, str(_ROOT / "deploy" / "migrate_qq_bot_scope.py"), "--workspace", str(tmp_path), "--to", _NEW_BOT],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, check=False,
        )

        assert done.returncode == 0, done.stderr
        assert f"源账号: {_OLD_BOT}" in done.stdout and "待搬迁会话数: 1" in done.stdout


class TestSupervisorScopeQuery:
    _TOKEN = "scope-admin-token"

    def _state(self, tmp_path: Path) -> SupervisorState:
        return SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-qq-scope"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )

    def _call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, url: str, *,
        state: SupervisorState | None = None, body: dict | None = None,
    ) -> httpx.Response:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", self._TOKEN)
        monkeypatch.setattr(admin_api, "_admin_token", "")
        app = create_app(
            state=state or self._state(tmp_path),
            process_manager=None,
            config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
        )

        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(
                    method, url, json=body, headers={"Authorization": f"Bearer {self._TOKEN}"},
                )

        return asyncio.run(go())

    def _get(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> httpx.Response:
        return self._call(tmp_path, monkeypatch, "GET", url)

    def test_the_script_reads_the_account_the_bot_recorded(self, tmp_path: Path) -> None:
        assert migrate.recorded_scope(tmp_path) == ""
        _store(tmp_path).save(_OLD_BOT)
        assert migrate.recorded_scope(tmp_path) == _OLD_BOT

    def test_the_console_sees_the_recorded_account(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _store(tmp_path).save(_OLD_BOT)

        response = self._get(tmp_path, monkeypatch, "/v1/admin/qq/scope")

        assert response.status_code == 200
        assert response.json() == {
            "current_scope": _OLD_BOT, "live_scope": "", "bot_alive": False, "preview": None, "ambiguous_sources": [],
        }

    def test_the_console_preview_works_when_the_secret_is_in_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(tmp_path, scope=_OLD_BOT)
        (tmp_path / "data" / "onebot_conversation_hash_secret").unlink()
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", _SECRET)

        response = self._get(tmp_path, monkeypatch, f"/v1/admin/qq/scope?target={_NEW_BOT}")

        assert response.status_code == 200, response.text
        assert response.json()["preview"]["source_scope"] == _OLD_BOT
        assert response.json()["preview"]["stores"]["sessions"] == {"label": "会话注册表", "moved": 1, "replaced": 0}

    def test_the_console_migration_rekeys_the_registry_the_supervisor_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 在线迁移时内存才是权威：先改文件再重读，中间任何一次落盘都会拿旧键盖回去，还没落盘的改动也会丢。
        seed = _seed(tmp_path, scope=_OLD_BOT)
        state = self._state(tmp_path)
        touched = datetime(2026, 3, 4, tzinfo=timezone.utc)
        state._session_store.touch_updated_at("sid-1", touched)

        response = self._call(
            tmp_path, monkeypatch, "POST", "/v1/admin/qq/scope/migrate", state=state,
            body={"target": _NEW_BOT, "source": _OLD_BOT},
        )

        assert response.status_code == 200, response.text
        new_stable = f"qq_group:{ch.digest(_REAL, _SECRET, bot_scope=_NEW_BOT)}"
        assert response.json()["sessions_changed"] == 1
        assert response.json()["stores"]["sessions"]["moved"] == 1
        assert state.conversation_map.get(new_stable) == "sid-1" and seed["stable"] not in state.conversation_map
        row = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))["sid-1"]
        assert row["conversation_key"] == new_stable and row["updated_at"] == touched.isoformat()

    def test_a_store_the_migration_cannot_read_is_a_server_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        (tmp_path / "data" / "schedules.yaml").write_text("schedules: [unclosed\n", encoding="utf-8")

        response = self._call(
            tmp_path, monkeypatch, "POST", "/v1/admin/qq/scope/migrate", body={"target": _NEW_BOT, "source": _OLD_BOT},
        )

        assert response.status_code == 500 and "定时任务" in response.json()["detail"]
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()

    def test_ambiguous_sources_are_listed_for_the_console_to_pick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(tmp_path)
        stray = _add_route(tmp_path, _OTHER_REAL, "70000009")
        _add_route(tmp_path, _OTHER_REAL, _OLD_BOT)
        _store(tmp_path).save(_OLD_BOT)

        listed = self._get(tmp_path, monkeypatch, f"/v1/admin/qq/scope?target={_NEW_BOT}")

        assert listed.status_code == 200, listed.text
        assert listed.json()["preview"] is None
        assert sorted(listed.json()["ambiguous_sources"], key=lambda row: row["scope"]) == [
            {"scope": "", "conversations": 1}, {"scope": _OLD_BOT, "conversations": 1},
        ]

        picked = self._get(tmp_path, monkeypatch, f"/v1/admin/qq/scope?target={_NEW_BOT}&source=")

        assert picked.status_code == 200, picked.text
        preview = picked.json()["preview"]
        assert preview["source_scope"] == "" and picked.json()["ambiguous_sources"] == []
        assert preview["other_scopes"] == [{"scope": _OLD_BOT, "conversations": 1}]
        assert preview["unresolved_namespaces"] == [stray["ns"]]
        assert len(preview["conversations"]) == 1

    def test_a_group_both_accounts_remember_is_shown_and_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed = _seed(tmp_path)
        theirs = _add_route(tmp_path, _REAL, _NEW_BOT)
        _store(tmp_path).save(_NEW_BOT)

        preview = self._get(tmp_path, monkeypatch, f"/v1/admin/qq/scope?target={_NEW_BOT}").json()["preview"]
        migrated = self._call(
            tmp_path, monkeypatch, "POST", "/v1/admin/qq/scope/migrate", body={"target": _NEW_BOT, "source": ""},
        )

        assert preview["conversations"] == [{
            "old_namespace": seed["ns"], "new_namespace": theirs["ns"], "has_memory": True, "blocked": True,
        }]
        assert migrated.status_code == 200, migrated.text
        assert migrated.json()["skipped"] == [{"old_namespace": seed["ns"], "new_namespace": theirs["ns"]}]
        assert migrated.json()["moved_memory_dirs"] == 0
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()

    def test_a_conversation_without_memory_is_not_blocked_by_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed = _seed(tmp_path)
        shutil.rmtree(tmp_path / "data" / "memory" / seed["ns"])
        theirs = _add_route(tmp_path, _REAL, _NEW_BOT)
        _store(tmp_path).save(_NEW_BOT)

        preview = self._get(tmp_path, monkeypatch, f"/v1/admin/qq/scope?target={_NEW_BOT}").json()["preview"]

        assert preview["conversations"] == [{
            "old_namespace": seed["ns"], "new_namespace": theirs["ns"], "has_memory": False, "blocked": False,
        }]

    def test_the_console_migration_passes_the_source_it_previewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed = _seed(tmp_path)
        _add_route(tmp_path, _OTHER_REAL, _OLD_BOT)
        _store(tmp_path).save(_OLD_BOT)

        refused = self._call(tmp_path, monkeypatch, "POST", "/v1/admin/qq/scope/migrate", body={"target": _NEW_BOT})
        migrated = self._call(
            tmp_path, monkeypatch, "POST", "/v1/admin/qq/scope/migrate", body={"target": _NEW_BOT, "source": _OLD_BOT},
        )

        assert refused.status_code == 409
        assert migrated.status_code == 200, migrated.text
        assert migrated.json()["source_scope"] == _OLD_BOT and migrated.json()["moved_memory_dirs"] == 1
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
