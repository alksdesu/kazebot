"""迁移会话键时各个库一起改写：会话注册表、定时任务、提醒、群协作、资料、执行计划。

每个库都用它自己的读写接口造数据、读结果：迁移和库的主人对格式的理解必须是同一份。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.scheduler as scheduler  # noqa: E402
from engine.conversation_migration import ConversationRekey, RekeyConflict, RekeyCount  # noqa: E402
from engine.materials.service import MaterialService  # noqa: E402
from engine.materials.storage import materials_index_path, rekey_materials  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor  # noqa: E402
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor._helpers import SessionInfo, _now  # noqa: E402
from supervisor.community.service import CommunityService, community_path, rekey_community  # noqa: E402
from supervisor.conversation_stores import (  # noqa: E402
    CONVERSATION_STORES,
    ConversationStoreError,
    conversation_stores,
    format_store_counts,
    rekey_conversation_stores,
    store_counts_payload,
)
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.execution import ExecutionService  # noqa: E402
from supervisor.execution.models import PlanDraft  # noqa: E402
from supervisor.execution.store import plans_path, rekey_plans  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.reminders import ReminderService  # noqa: E402
from supervisor.reminders.service import rekey_reminders, reminders_path  # noqa: E402
from supervisor.scheduler import (  # noqa: E402
    load_schedules,
    rekey_schedules,
    save_schedules,
    schedules_lock,
    schedules_path,
    update_schedules,
)
from supervisor.session_store import SessionStore, rekey_session_registry, session_registry_path  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

OLD = "qq_group:" + "a" * 24
NEW = "qq_group:" + "b" * 24
# 跨平台绑定：渠道名跟着换。
MOVED = "qqbot_group:" + "c" * 24
OTHER = "qq_group:" + "d" * 24
OLD_BOT = "10001"
NEW_BOT = "20002"
# 登记过的另一个平台：绑定过去时平台 id 也跟着换。
MATRIX = PlatformDescriptor(
    id="matrix", display_name="Matrix", short_name="MX", channels=(ChannelDescriptor("mx_room", CHANNEL_GROUP),),
)
BOUND = "mx_room:" + "e" * 24


@pytest.fixture
def matrix():
    register_platform(MATRIX)
    try:
        yield MATRIX
    finally:
        unregister_platform(MATRIX.id)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "stores-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    nodes = tmp_path / "config/nodes"
    nodes.mkdir(parents=True)
    (nodes / "test.entry.yaml").write_text(
        "id: test.entry\ntype: ai\ntool_access:\n  mode: allowlist\n  allow: [read_file, write_file]\n", encoding="utf-8",
    )
    supervisor_state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="stores"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    supervisor_state._defer_post_work_enqueue = True
    monkeypatch.setattr(supervisor_state, "_default_entry_node", lambda: "test.entry")
    return supervisor_state


def _member(scope: str = OLD, bot: str = OLD_BOT, channel: str = "qq_group", **fields) -> FeatureActor:
    return FeatureActor(scope=scope, owner="user:alice", bot_scope=bot, channel=channel, user_id="30003", **fields)


def _manager(scope: str = OLD, **fields) -> FeatureActor:
    return FeatureActor(
        scope=scope, owner="user:owner", is_admin=True, role="admin", channel="qq_group", bot_scope=OLD_BOT, **fields,
    )


def _session(store: SessionStore, session_id: str, key: str, channel: str = "qq_group") -> None:
    now = _now()
    store.on_session_created(
        SessionInfo(session_id=session_id, channel=channel, conversation_key=key, created_at=now, updated_at=now),
    )


def _registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path, sql: str, *args) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


class TestConversationRekey:
    def test_pairs_that_change_nothing_are_dropped(self) -> None:
        rekey = ConversationRekey({OLD: OLD, "": NEW, OTHER: ""})
        assert not rekey
        assert dict(rekey.keys) == {}

    def test_a_chain_is_refused(self) -> None:
        # a→b、b→c：先改哪条，结果都取决于顺序。
        with pytest.raises(ValueError):
            ConversationRekey({OLD: NEW, NEW: MOVED})

    def test_two_conversations_are_never_merged_into_one(self) -> None:
        with pytest.raises(ValueError):
            ConversationRekey({OLD: NEW, OTHER: NEW})

    def test_a_channel_follows_only_when_it_named_the_old_conversation(self) -> None:
        rekey = ConversationRekey({OLD: MOVED})
        assert rekey.channel(OLD, MOVED, "qq_group") == "qqbot_group"
        assert rekey.channel(OLD, MOVED, "web") == "web"
        assert rekey.channel(OLD, MOVED, None) is None

    def test_a_platform_id_follows_only_into_another_platform(self, matrix: PlatformDescriptor) -> None:
        rekey = ConversationRekey({OLD: BOUND})
        assert rekey.platform(OLD, BOUND, "qq") == "matrix"
        # 历史别名也是旧平台。
        assert rekey.platform(OLD, BOUND, " OneBot ") == "matrix"
        assert rekey.platform(OLD, BOUND, "qq_group") == "qq_group"
        assert rekey.platform(OLD, BOUND, "web") == "web"
        assert rekey.platform(OLD, BOUND, None) is None

    def test_a_platform_id_stays_inside_one_platform_and_beside_unknown_keys(self, matrix: PlatformDescriptor) -> None:
        assert ConversationRekey({OLD: NEW}).platform(OLD, NEW, "onebot") == "onebot"
        assert ConversationRekey({OLD: MOVED}).platform(OLD, MOVED, "qq") == "qq"
        assert ConversationRekey({MOVED: BOUND}).platform(MOVED, BOUND, "qqbot") == "qqbot"

    def test_only_the_listed_accounts_are_renamed(self) -> None:
        rekey = ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT, "same": "same"})
        assert rekey.bot_scope(OLD_BOT) == NEW_BOT
        assert rekey.bot_scope("30003") == "30003"
        assert dict(rekey.bot_scopes) == {OLD_BOT: NEW_BOT}

    def test_rewrite_touches_only_the_fields_it_is_told_about(self) -> None:
        rekey = ConversationRekey({OLD: MOVED, OTHER: NEW}, bot_scopes={OLD_BOT: NEW_BOT})
        record = {"scope": OLD, "route": OTHER, "untouched": OLD, "channel": "qq_group", "bot_scope": OLD_BOT}
        rekey.rewrite(record, OLD, MOVED, keys=("scope", "route"), channels=("channel",), bot_scopes=("bot_scope",))
        assert record == {
            "scope": MOVED, "route": NEW, "untouched": OLD, "channel": "qqbot_group", "bot_scope": NEW_BOT,
        }

    def test_rewrite_runs_the_platform_rule_only_on_the_listed_fields(self, matrix: PlatformDescriptor) -> None:
        record = {"platform": "qq", "created_platform": "qq"}
        ConversationRekey({OLD: BOUND}).rewrite(record, OLD, BOUND, platforms=("platform",))
        assert record == {"platform": "matrix", "created_platform": "qq"}


class TestSessionRegistry:
    def _store(self, tmp_path: Path) -> tuple[SessionStore, Path]:
        path = session_registry_path(tmp_path)
        store = SessionStore(path)
        _session(store, "s-old", OLD)
        store.on_child_session_created("c-old", "s-old", "helper", "ctx")
        _session(store, "s-other", OTHER)
        return store, path

    def test_rows_move_and_the_store_loads_them_under_the_new_key(self, tmp_path: Path) -> None:
        _store, path = self._store(tmp_path)

        count = rekey_session_registry(path, ConversationRekey({OLD: MOVED}), apply=True)

        sessions, conversation_map, child_map, _ = SessionStore(path).load()
        assert count == RekeyCount(2, 0)
        assert conversation_map[MOVED] == "s-old" and OLD not in conversation_map
        assert sessions["s-old"].channel == "qqbot_group"
        assert conversation_map[OTHER] == "s-other"
        child = _registry(path)["c-old"]
        assert child["conversation_key"] == MOVED and child["channel"] == "internal"
        assert child_map[("s-old", "helper", "ctx")] == "c-old"

    def test_an_active_session_already_on_the_new_key_is_reset(self, tmp_path: Path) -> None:
        store, path = self._store(tmp_path)
        _session(store, "s-new", NEW)

        count = rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=True)

        _sessions, conversation_map, _, _ = SessionStore(path).load()
        assert count == RekeyCount(2, 1)
        assert conversation_map[NEW] == "s-old"
        assert _registry(path)["s-new"]["reset"] is True

    def test_a_source_with_only_reset_sessions_leaves_the_target_active(self, tmp_path: Path) -> None:
        store, path = self._store(tmp_path)
        store.on_session_reset("s-old")
        _session(store, "s-new", NEW)

        count = rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=True)

        _sessions, conversation_map, _, _ = SessionStore(path).load()
        assert count.replaced == 0
        assert conversation_map[NEW] == "s-new"
        assert _registry(path)["s-old"]["conversation_key"] == NEW

    def test_legacy_dispatch_keys_carry_the_parent_key_along(self, tmp_path: Path) -> None:
        path = session_registry_path(tmp_path)
        store = SessionStore(path)
        for session_id, key in (
            ("d-tail", f"agent:helper:{OLD}"),
            ("d-context", f"agent:helper:ctx:{OLD}"),
            ("d-fresh", f"agent:helper:{OLD}:0f0f"),
            ("d-other", f"agent:helper:{OTHER}"),
        ):
            _session(store, session_id, key)

        count = rekey_session_registry(path, ConversationRekey({OLD: MOVED}), apply=True)

        rows = _registry(path)
        assert count.moved == 3
        assert {sid: row["conversation_key"] for sid, row in rows.items()} == {
            "d-tail": f"agent:helper:{MOVED}",
            "d-context": f"agent:helper:ctx:{MOVED}",
            "d-fresh": f"agent:helper:{MOVED}:0f0f",
            "d-other": f"agent:helper:{OTHER}",
        }
        assert rows["d-tail"]["channel"] == "qqbot_group" and rows["d-other"]["channel"] == "qq_group"

    def test_a_preview_counts_the_same_rows_and_writes_nothing(self, tmp_path: Path) -> None:
        store, path = self._store(tmp_path)
        _session(store, "s-new", NEW)
        before = path.read_bytes()

        preview = rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=False)

        assert path.read_bytes() == before
        assert preview == rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=True)

    def test_an_unreadable_registry_stops_the_rekey(self, tmp_path: Path) -> None:
        path = session_registry_path(tmp_path)
        path.parent.mkdir(parents=True)
        for broken in ("{not json", "[1, 2]"):
            path.write_text(broken, encoding="utf-8")
            with pytest.raises(ValueError):
                rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=True)
            assert path.read_text(encoding="utf-8") == broken

    def test_a_missing_registry_stays_missing(self, tmp_path: Path) -> None:
        path = session_registry_path(tmp_path)
        assert rekey_session_registry(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount()
        assert not path.exists()

    def test_the_live_store_rekeys_what_it_holds_in_memory(self, tmp_path: Path) -> None:
        # 在线迁移时内存才是权威：盘上没刷下去的改动也得跟着留下。
        store, path = self._store(tmp_path)
        touched = datetime(2026, 1, 2, tzinfo=timezone.utc)
        store.touch_updated_at("s-old", touched)

        assert store.rekey_conversations(ConversationRekey({OLD: NEW}), apply=False) == RekeyCount(2, 0)
        assert store.get_entry("s-old")["conversation_key"] == OLD

        count = store.rekey_conversations(ConversationRekey({OLD: NEW}), apply=True)

        row = _registry(path)["s-old"]
        assert count == RekeyCount(2, 0)
        assert row["conversation_key"] == NEW and row["updated_at"] == touched.isoformat()

    def test_the_live_store_refuses_to_pretend_when_the_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store, _path = self._store(tmp_path)
        monkeypatch.setattr(store, "flush_snapshot", lambda snapshot: False)
        with pytest.raises(OSError):
            store.rekey_conversations(ConversationRekey({OLD: NEW}), apply=True)

    def test_the_supervisor_rebuilds_its_maps_after_an_online_rekey(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq_group", conversation_key=OLD)

        count = state.rekey_session_registry(ConversationRekey({OLD: NEW}), apply=True)

        assert count.moved == 1
        assert state.conversation_map.get(NEW) == session_id and OLD not in state.conversation_map
        assert state.sessions[session_id].conversation_key == NEW


class TestSchedules:
    def _seed(self, tmp_path: Path) -> Path:
        save_schedules(tmp_path, [
            {"id": "morning", "cron": "0 9 * * *", "text": "早", "conversation_key": OLD},
            {"id": "evening", "cron": "0 21 * * *", "text": "晚", "conversation_key": OTHER},
        ])
        return schedules_path(tmp_path)

    def test_schedules_follow_their_conversation(self, tmp_path: Path) -> None:
        path = self._seed(tmp_path)

        count = rekey_schedules(path, ConversationRekey({OLD: NEW}), apply=True)

        assert count == RekeyCount(1)
        assert {item["id"]: item["conversation_key"] for item in load_schedules(tmp_path)} == {
            "morning": NEW, "evening": OTHER,
        }

    def test_a_preview_leaves_the_file_alone(self, tmp_path: Path) -> None:
        path = self._seed(tmp_path)
        before = path.read_bytes()
        assert rekey_schedules(path, ConversationRekey({OLD: NEW}), apply=False) == RekeyCount(1)
        assert path.read_bytes() == before

    def test_an_unreadable_file_is_never_written_back_as_empty(self, tmp_path: Path) -> None:
        path = schedules_path(tmp_path)
        path.parent.mkdir(parents=True)
        for broken in ("schedules: [unclosed\n", "- just\n- a list\n", "schedules: nope\n"):
            path.write_text(broken, encoding="utf-8")
            with pytest.raises((ValueError, yaml.YAMLError)):
                update_schedules(tmp_path, lambda items: items + [{"id": "x"}])
            assert path.read_text(encoding="utf-8") == broken
            # 只读的调用方照旧当成没有定时任务，调度线程不至于崩掉。
            assert load_schedules(tmp_path) == []

    def test_a_failed_write_keeps_the_old_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = self._seed(tmp_path)
        before = path.read_bytes()

        def refuse(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(scheduler.os, "replace", refuse)
        with pytest.raises(OSError):
            update_schedules(tmp_path, lambda items: items + [{"id": "x"}])

        assert path.read_bytes() == before
        assert not list(path.parent.glob("*.tmp"))

    def test_an_unchanged_list_is_not_rewritten(self, tmp_path: Path) -> None:
        path = self._seed(tmp_path)
        before = path.stat().st_mtime_ns
        time.sleep(0.02)
        update_schedules(tmp_path, lambda items: items)
        assert path.stat().st_mtime_ns == before

    def test_threads_do_not_lose_each_others_updates(self, tmp_path: Path) -> None:
        def append(tag: str) -> None:
            for index in range(10):
                update_schedules(tmp_path, lambda items, i=index: items + [{"id": f"{tag}-{i}"}])

        workers = [threading.Thread(target=append, args=(f"t{n}",)) for n in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert len({item["id"] for item in load_schedules(tmp_path)}) == 60

    def test_processes_do_not_lose_each_others_updates(self, tmp_path: Path) -> None:
        # supervisor、离线迁移脚本各是一个进程，进程内的锁管不到对方。
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from supervisor.scheduler import update_schedules\n"
            "root, tag = Path(sys.argv[2]), sys.argv[3]\n"
            "for index in range(15):\n"
            "    update_schedules(root, lambda items, i=index: items + [{'id': f'{tag}-{i}'}])\n"
        )
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(_ROOT), str(tmp_path), f"p{n}"])
            for n in range(3)
        ]
        assert [worker.wait(timeout=120) for worker in workers] == [0, 0, 0]
        assert len({item["id"] for item in load_schedules(tmp_path)}) == 45

    def test_the_console_waits_for_the_schedules_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from fastapi import FastAPI

        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "stores-test")
        monkeypatch.setattr(admin_api, "_admin_token", "")
        app = FastAPI()
        app.include_router(admin_api.create_admin_router(workspace_root=tmp_path), prefix="/v1/admin/config")
        path = self._seed(tmp_path)
        done = threading.Event()

        def put() -> None:
            async def go() -> None:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                    response = await http.put(
                        "/v1/admin/config/schedules/raw", json={"content": "schedules: []\n"},
                        headers={"Authorization": "Bearer stores-test"},
                    )
                    assert response.status_code == 200

            asyncio.run(go())
            done.set()

        with schedules_lock(tmp_path):
            writer = threading.Thread(target=put)
            writer.start()
            assert not done.wait(0.5)
            assert load_schedules(tmp_path)
        writer.join(timeout=30)
        assert done.is_set()
        assert path.read_text(encoding="utf-8") == "schedules: []\n"


class TestReminders:
    def _seed(self, state: SupervisorState) -> dict:
        service = ReminderService(state)
        try:
            actor = _member()
            reminder = service.create(actor, "交材料", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
            service.tick(datetime.fromisoformat(reminder["due_at"]) + timedelta(seconds=1))
            current = service.get(FeatureActor(is_admin=True), reminder["id"])
            service.bind_delivery_message(current["delivery_id"], OLD, OLD_BOT, "m-delivered")
            service.act(_member(message_id="m-snooze"), reminder["id"], "snooze", current["revision"], minutes=5)
            return reminder
        finally:
            service.close()

    def test_a_reminder_moves_with_its_conversation_and_account(self, state: SupervisorState) -> None:
        reminder = self._seed(state)
        path = reminders_path(state.workspace_root)

        count = rekey_reminders(path, ConversationRekey({OLD: MOVED}, bot_scopes={OLD_BOT: NEW_BOT}), apply=True)

        service = ReminderService(state)
        try:
            moved_actor = _member(scope=MOVED, bot=NEW_BOT, channel="qqbot_group")
            [listed] = service.list(moved_actor)
            assert listed["id"] == reminder["id"]
            assert (listed["scope"], listed["channel"], listed["bot_scope"]) == (MOVED, "qqbot_group", NEW_BOT)
            assert service.list(_member()) == []
            # 引用那条已经发出去的提醒回复，在新会话、新号上照样认得出。
            context = service.reply_context(_member(scope=MOVED, bot=NEW_BOT, channel="qqbot_group"), "m-delivered")
            assert context["matched"] and context["authorized"]
        finally:
            service.close()
        [(result,)] = _rows(path, "SELECT result FROM reminder_actions")
        assert json.loads(result)["scope"] == MOVED
        assert count == RekeyCount(3, 0)

    def test_the_source_binding_wins_over_a_stale_target_one(self, state: SupervisorState) -> None:
        self._seed(state)
        path = reminders_path(state.workspace_root)
        db = sqlite3.connect(path)
        with db:
            db.execute(
                "INSERT INTO reminder_message_bindings VALUES (?,?,?,?,?,?,?)",
                (NEW, NEW_BOT, "m-delivered", "R-stale", 1, "reminder:R-stale:1", "2026-01-01T00:00:00+00:00"),
            )
        db.close()

        count = rekey_reminders(path, ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT}), apply=True)

        assert count.replaced == 1
        [(reminder_id,)] = _rows(path, "SELECT reminder_id FROM reminder_message_bindings WHERE scope=?", NEW)
        assert reminder_id != "R-stale"

    def test_a_preview_changes_nothing(self, state: SupervisorState) -> None:
        self._seed(state)
        path = reminders_path(state.workspace_root)
        rekey = ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT})

        preview = rekey_reminders(path, rekey, apply=False)

        assert _rows(path, "SELECT scope FROM reminders") == [(OLD,)]
        assert preview == rekey_reminders(path, rekey, apply=True)

    def test_a_missing_database_is_not_created(self, tmp_path: Path) -> None:
        path = reminders_path(tmp_path)
        assert rekey_reminders(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount()
        assert not path.exists()


class TestCommunity:
    def test_group_settings_and_activity_move_and_replace_the_target_settings(self, tmp_path: Path) -> None:
        service = CommunityService(tmp_path)
        service.update_settings(_manager(), {"reply_budget_per_minute": 9})
        service.set_quiet(_manager(), "listen", 600)
        activity = service.create_activity(_manager(), {"kind": "poll", "title": "吃什么", "options": ["面", "饭"]})
        service.record_decision(_manager(message_id="m1"), {"action": "ignore"})
        service.update_settings(_manager(scope=NEW), {"reply_budget_per_minute": 3})

        count = rekey_community(community_path(tmp_path), ConversationRekey({OLD: NEW}), apply=True)

        moved = CommunityService(tmp_path)
        state = moved.state(_manager(scope=NEW))
        assert state["settings"]["reply_budget_per_minute"] == 9
        assert state["quiet"]["mode"] == "listen"
        assert [item["id"] for item in moved.activities(_manager(scope=NEW))] == [activity["id"]]
        assert [item["message_id"] for item in moved.decisions(_manager(scope=NEW))] == ["m1"]
        assert moved.activities(_manager()) == []
        assert count.replaced == 1

    def test_a_preview_changes_nothing(self, tmp_path: Path) -> None:
        service = CommunityService(tmp_path)
        service.update_settings(_manager(), {"reply_budget_per_minute": 9})
        path = community_path(tmp_path)

        preview = rekey_community(path, ConversationRekey({OLD: NEW}), apply=False)

        assert _rows(path, "SELECT scope FROM groups") == [(OLD,)]
        assert preview == rekey_community(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount(1)

    def test_a_missing_database_is_not_created(self, tmp_path: Path) -> None:
        path = community_path(tmp_path)
        assert rekey_community(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount()
        assert not path.exists()


class TestMaterials:
    def _actor(self, scope: str = OLD, bot: str = OLD_BOT) -> dict:
        return {"scope": scope, "owner": "user:alice", "bot_scope": bot, "is_admin": True, "role": "admin", "message_id": "m1"}

    def test_materials_journal_and_settings_move_to_the_new_conversation_and_account(self, tmp_path: Path) -> None:
        service = MaterialService(tmp_path)
        try:
            service.store.save_settings(self._actor(), {"journal_enabled": True})
            service.record_message(self._actor(), {"message_id": "m1", "text": "原话", "timestamp": time.time()})
            note = service.store.create("note", self._actor(), {"title": "笔记"})

            count = rekey_materials(
                materials_index_path(tmp_path), ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT}), apply=True,
            )

            moved = self._actor(NEW, NEW_BOT)
            [listed] = service.store.list("note", moved)
            assert listed["id"] == note["id"] and (listed["scope"], listed["bot_scope"]) == (NEW, NEW_BOT)
            assert service.store.settings(moved)["journal_enabled"] is True
            assert service.get_message(moved, "m1")["text"] == "原话"
            assert service.store.list("note", self._actor()) == []
            assert count == RekeyCount(3, 0)
        finally:
            service.close()

    def test_a_pending_image_operation_lands_in_the_new_conversation(self, tmp_path: Path) -> None:
        from PIL import Image

        service = MaterialService(tmp_path)
        try:
            origin = {**self._actor(), "channel": "qq_group", "task_id": "origin"}
            operation = service.begin_image_operation(origin, {"tool_name": "gpt_image_2", "tool_call_id": "call1"})

            rekey_materials(
                materials_index_path(tmp_path), ConversationRekey({OLD: MOVED}, bot_scopes={OLD_BOT: NEW_BOT}), apply=True,
            )

            image = tmp_path / "data" / "attachments" / "gpt_image" / "generated.png"
            image.parent.mkdir(parents=True)
            Image.new("RGB", (64, 48), "red").save(image)
            result = service.complete_image_operation(operation["id"], "origin", {
                "tool_name": "gpt_image_2", "tool_call_id": "call1",
                "attachments": [{"type": "image", "path": image.relative_to(tmp_path).as_posix(), "name": image.name}],
            })

            moved = self._actor(MOVED, NEW_BOT)
            [version] = result["versions"]
            assert service.store.get(version["id"], moved, kind="version")["scope"] == MOVED
            assert service.store.list("version", self._actor()) == []
            bound = service.store.get(operation["id"], moved, kind="image_operation")["bound_actor"]
            assert (bound["scope"], bound["channel"], bound["bot_scope"]) == (MOVED, "qqbot_group", NEW_BOT)
        finally:
            service.close()

    def test_the_same_generated_version_on_both_sides_stops_everything(self, tmp_path: Path) -> None:
        service = MaterialService(tmp_path)
        try:
            for actor in (self._actor(), self._actor(NEW, NEW_BOT)):
                service.store.create("version", actor, {"metadata": {"generation_key": "g-1"}})
            service.store.create("note", self._actor(), {"title": "笔记"})

            with pytest.raises(RekeyConflict):
                rekey_materials(
                    materials_index_path(tmp_path), ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT}),
                    apply=True,
                )

            assert len(service.store.list("note", self._actor())) == 1
        finally:
            service.close()

    def test_a_missing_database_is_not_created(self, tmp_path: Path) -> None:
        path = materials_index_path(tmp_path)
        assert rekey_materials(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount()
        assert not path.exists()


class TestExecutionPlans:
    def test_a_plan_moves_with_its_actor_context_and_revisions(self, state: SupervisorState) -> None:
        state.execution = service = ExecutionService(state)
        try:
            plan = service.create(_member(role="group_admin"), PlanDraft(goal="总结", inputs=[{"kind": "text", "value": "资料"}]))
        finally:
            service.close()
        path = plans_path(state.workspace_root)

        count = rekey_plans(path, ConversationRekey({OLD: MOVED}, bot_scopes={OLD_BOT: NEW_BOT}), apply=True)

        state.execution = service = ExecutionService(state)
        try:
            body = service.store.get(plan["id"])
            assert (body["scope"], body["channel"], body["bot_scope"]) == (MOVED, "qqbot_group", NEW_BOT)
            binding = body["actor_binding"]
            assert (binding["scope"], binding["channel"], binding["bot_scope"]) == (MOVED, "qqbot_group", NEW_BOT)
            context = body["task_context"]
            assert (context["conversation_key"], context["channel"]) == (MOVED, "qqbot_group")
            assert context["platform_auth"]["bot_scope"] == NEW_BOT
            assert [item["id"] for item in service.store.list(MOVED, bot_scope=NEW_BOT)] == [plan["id"]]
        finally:
            service.close()
        assert {json.loads(raw)["scope"] for (raw,) in _rows(path, "SELECT body FROM revisions")} == {MOVED}
        assert count.moved == 2

    def test_nested_context_keys_follow_the_conversation(self) -> None:
        from supervisor.execution.store import _rekey_plan_body

        body = json.dumps({
            "scope": OLD, "channel": "qq_group", "bot_scope": OLD_BOT,
            "actor_binding": {"scope": OLD, "channel": "qq_group", "bot_scope": OLD_BOT, "owner": "user:alice"},
            "task_context": {
                "conversation_key": OLD, "route_conversation_key": OLD, "parent_conversation_key": OLD,
                "channel": "qq_group",
                "platform_auth": {"platform": "qq", "group_scope": OLD, "bot_scope": OLD_BOT, "user_id": "30003"},
            },
        })

        plan = json.loads(_rekey_plan_body(body, ConversationRekey({OLD: MOVED}, bot_scopes={OLD_BOT: NEW_BOT}), OLD, MOVED))

        context = plan["task_context"]
        assert (context["route_conversation_key"], context["parent_conversation_key"]) == (MOVED, MOVED)
        # 新键认不出是哪个平台，平台 id 原样；群管理身份认的会话键要换。
        assert context["platform_auth"] == {"platform": "qq", "group_scope": MOVED, "bot_scope": NEW_BOT, "user_id": "30003"}

    @pytest.mark.parametrize("recorded, expected", [("qq", "matrix"), ("onebot", "matrix"), ("qq_group", "mx_room")])
    def test_the_platform_follows_a_binding_into_another_platform(
        self, matrix: PlatformDescriptor, recorded: str, expected: str,
    ) -> None:
        # 任务里建的计划抄的是适配器报的平台 id，没有任务时记的是渠道名。
        from supervisor.execution.store import _rekey_plan_body

        body = json.dumps({
            "scope": OLD, "channel": "qq_group",
            "task_context": {
                "conversation_key": OLD, "channel": "qq_group", "platform_auth": {"platform": recorded, "group_scope": OLD},
            },
        })

        plan = json.loads(_rekey_plan_body(body, ConversationRekey({OLD: BOUND}), OLD, BOUND))

        assert (plan["scope"], plan["channel"], plan["task_context"]["channel"]) == (BOUND, "mx_room", "mx_room")
        assert plan["task_context"]["platform_auth"] == {"platform": expected, "group_scope": BOUND}

    def test_a_missing_database_is_not_created(self, tmp_path: Path) -> None:
        path = plans_path(tmp_path)
        assert rekey_plans(path, ConversationRekey({OLD: NEW}), apply=True) == RekeyCount()
        assert not path.exists()


class TestInventory:
    def _seed_every_store(self, state: SupervisorState) -> None:
        workspace = state.workspace_root
        state.get_or_create_session(channel="qq_group", conversation_key=OLD)
        save_schedules(workspace, [{"id": "morning", "cron": "0 9 * * *", "text": "早", "conversation_key": OLD}])
        reminders = ReminderService(state)
        reminders.create(_member(), "交材料", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        reminders.close()
        CommunityService(workspace).update_settings(_manager(), {"reply_budget_per_minute": 9})
        materials = MaterialService(workspace)
        materials.store.create("note", {"scope": OLD, "owner": "user:alice", "bot_scope": OLD_BOT}, {"title": "笔记"})
        materials.close()
        execution = ExecutionService(state)
        execution.create(_member(), PlanDraft(goal="总结", inputs=[{"kind": "text", "value": "资料"}]))
        execution.close()

    def test_every_store_is_listed_once_and_only_the_registry_is_history(self) -> None:
        names = [store.name for store in CONVERSATION_STORES]
        assert len(names) == len(set(names))
        assert [store.name for store in CONVERSATION_STORES if store.history] == ["sessions"]
        assert "sessions" not in {store.name for store in conversation_stores(history=False)}

    def test_a_preview_matches_what_apply_does_and_a_second_run_does_nothing(self, state: SupervisorState) -> None:
        self._seed_every_store(state)
        rekey = ConversationRekey({OLD: NEW}, bot_scopes={OLD_BOT: NEW_BOT})

        preview = rekey_conversation_stores(state.workspace_root, rekey, apply=False)
        applied = rekey_conversation_stores(state.workspace_root, rekey, apply=True)
        again = rekey_conversation_stores(state.workspace_root, rekey, apply=True)

        assert preview == applied
        assert all(applied[store.name].moved for store in CONVERSATION_STORES)
        assert not any(again.values())

    def test_history_can_be_left_behind(self, state: SupervisorState) -> None:
        self._seed_every_store(state)
        registry = session_registry_path(state.workspace_root)
        before = registry.read_bytes()

        results = rekey_conversation_stores(state.workspace_root, ConversationRekey({OLD: NEW}), apply=True, history=False)

        assert "sessions" not in results and results["schedules"].moved == 1
        assert registry.read_bytes() == before

    def test_the_backup_holds_every_store_as_it_was(self, state: SupervisorState) -> None:
        self._seed_every_store(state)
        backup = state.workspace_root / "backup"
        # 开着的连接让刚提交的内容停在 WAL 里、没合并进主文件：直接拷文件就会漏掉这一条。
        materials = MaterialService(state.workspace_root)
        try:
            materials.store.create("note", {"scope": OLD, "owner": "user:bob", "bot_scope": OLD_BOT}, {"title": "刚写的"})
            rekey_conversation_stores(state.workspace_root, ConversationRekey({OLD: NEW}), apply=True, backup_dir=backup)
        finally:
            materials.close()

        assert len(_rows(backup / "data/materials/index.sqlite3", "SELECT id FROM objects WHERE scope=?", OLD)) == 2
        assert OLD in (backup / "data" / "sessions.json").read_text(encoding="utf-8")
        assert OLD in (backup / "data" / "schedules.yaml").read_text(encoding="utf-8")
        for relative, table in (
            ("data/reminders/reminders.sqlite3", "reminders"),
            ("data/community.sqlite3", "groups"),
            ("data/materials/index.sqlite3", "objects"),
            ("data/execution/execution.sqlite3", "plans"),
        ):
            assert _rows(backup / relative, f"SELECT DISTINCT scope FROM {table}") == [(OLD,)], relative

    def test_a_live_store_stands_in_for_its_file(self, state: SupervisorState) -> None:
        self._seed_every_store(state)
        registry = session_registry_path(state.workspace_root)
        before = registry.read_bytes()
        calls: list[tuple[ConversationRekey, bool]] = []

        def live(rekey: ConversationRekey, *, apply: bool) -> RekeyCount:
            calls.append((rekey, apply))
            return RekeyCount(7)

        rekey = ConversationRekey({OLD: NEW})
        results = rekey_conversation_stores(state.workspace_root, rekey, apply=True, live={"sessions": live})

        assert results["sessions"] == RekeyCount(7)
        assert calls == [(rekey, True)]
        assert registry.read_bytes() == before

    def test_a_broken_store_names_itself_and_stops_before_the_rest(self, state: SupervisorState) -> None:
        self._seed_every_store(state)
        session_registry_path(state.workspace_root).write_text("{not json", encoding="utf-8")

        with pytest.raises(ConversationStoreError, match="会话注册表"):
            rekey_conversation_stores(state.workspace_root, ConversationRekey({OLD: NEW}), apply=True)

        assert load_schedules(state.workspace_root)[0]["conversation_key"] == OLD

    def test_a_conflict_is_reported_as_it_is(self, tmp_path: Path) -> None:
        service = MaterialService(tmp_path)
        try:
            for scope in (OLD, NEW):
                service.store.create(
                    "version", {"scope": scope, "owner": "user:alice", "bot_scope": OLD_BOT},
                    {"metadata": {"generation_key": "g-1"}},
                )
        finally:
            service.close()
        with pytest.raises(RekeyConflict):
            rekey_conversation_stores(tmp_path, ConversationRekey({OLD: NEW}), apply=False)

    def test_nothing_is_created_for_stores_that_do_not_exist(self, tmp_path: Path) -> None:
        results = rekey_conversation_stores(tmp_path, ConversationRekey({OLD: NEW}), apply=True, backup_dir=tmp_path / "b")
        assert not any(results.values())
        assert not (tmp_path / "data").exists() and not (tmp_path / "b").exists()

    def test_the_counts_read_well(self) -> None:
        counts = {"sessions": RekeyCount(3, 1), "schedules": RekeyCount(2), "reminders": RekeyCount()}
        assert format_store_counts(counts) == "会话注册表 3 行（顶掉目标 1 行）；定时任务 2 行"
        assert format_store_counts({"reminders": RekeyCount()}) == "无"
        assert store_counts_payload(counts)["sessions"] == {"label": "会话注册表", "moved": 3, "replaced": 1}
