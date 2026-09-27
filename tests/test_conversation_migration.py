"""引擎侧的搬迁零件：新平台在自己的数据目录里迁移，跨平台绑定往目标平台的路由状态里补对照，
搬记忆目录前连同要改的路由状态一起备份。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.memory_hit_cache as hit_cache  # noqa: E402
from engine.conversation_migration import (  # noqa: E402
    MigrationPlan,
    Rename,
    apply_migration,
    memory_root,
    move_memory,
    plan_migration,
    record_route,
    route_state_file,
)
from engine.memory_namespace import conversation_namespace  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor  # noqa: E402

MATRIX = PlatformDescriptor(
    id="matrix", display_name="Matrix", short_name="MX", channels=(ChannelDescriptor("mx_room", CHANNEL_GROUP),),
)
SOURCE = "qq_group:" + "a" * 24
TARGET = "mx_room:" + "b" * 24
TARGET_REAL = "mx_room:!room:example.org"
OTHER = "mx_room:" + "c" * 24
OTHER_REAL = "mx_room:!other:example.org"


def _routes(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestRecordRoute:
    def test_a_missing_file_is_created_the_way_the_adapters_write_it(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)

        assert record_route(path, TARGET, TARGET_REAL, apply=True) is True

        assert _routes(path) == {"version": 1, "real_conversation_keys": {TARGET: TARGET_REAL}, "session_targets": {}}

    def test_what_is_already_there_stays(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True)
        targets = {"s1": {"conversation_key": OTHER, "room": "!other:example.org"}}
        path.write_text(json.dumps({
            "version": 1, "real_conversation_keys": {OTHER: OTHER_REAL}, "session_targets": targets,
        }), encoding="utf-8")

        record_route(path, TARGET, TARGET_REAL, apply=True)

        assert _routes(path) == {
            "version": 1, "real_conversation_keys": {OTHER: OTHER_REAL, TARGET: TARGET_REAL}, "session_targets": targets,
        }

    def test_the_same_pair_again_leaves_the_file_alone(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True)
        raw = json.dumps({"real_conversation_keys": {TARGET: TARGET_REAL}})
        path.write_text(raw, encoding="utf-8")

        assert record_route(path, TARGET, TARGET_REAL, apply=True) is False
        assert path.read_text(encoding="utf-8") == raw

    def test_a_preview_answers_without_writing(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)

        assert record_route(path, TARGET, TARGET_REAL, apply=False) is True
        assert not path.exists()

    def test_an_empty_entry_counts_as_missing(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"real_conversation_keys": {TARGET: ""}}), encoding="utf-8")

        assert record_route(path, TARGET, TARGET_REAL, apply=True) is True
        assert _routes(path)["real_conversation_keys"] == {TARGET: TARGET_REAL}

    def test_a_key_that_already_points_elsewhere_is_refused(self, tmp_path: Path) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True)
        raw = json.dumps({"real_conversation_keys": {TARGET: OTHER_REAL}})
        path.write_text(raw, encoding="utf-8")

        for apply in (False, True):
            with pytest.raises(ValueError):
                record_route(path, TARGET, TARGET_REAL, apply=apply)
        assert path.read_text(encoding="utf-8") == raw

    @pytest.mark.parametrize("raw", ["{not json", "[]", json.dumps({"real_conversation_keys": []})])
    def test_a_file_it_cannot_read_is_refused_not_replaced(self, tmp_path: Path, raw: str) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding="utf-8")

        with pytest.raises(ValueError):
            record_route(path, TARGET, TARGET_REAL, apply=True)
        assert path.read_text(encoding="utf-8") == raw

    @pytest.mark.parametrize("stable, real", [("", TARGET_REAL), (TARGET, " ")])
    def test_both_keys_are_required(self, tmp_path: Path, stable: str, real: str) -> None:
        path = route_state_file(tmp_path, MATRIX)

        with pytest.raises(ValueError):
            record_route(path, stable, real, apply=True)
        assert not path.exists()


class TestMoveMemory:
    def _plan(self, tmp_path: Path, *, dir_exists: bool = True) -> MigrationPlan:
        old_ns, new_ns = conversation_namespace(SOURCE), conversation_namespace(TARGET)
        return MigrationPlan(renames=[Rename(
            real="qq_group:12345", old_stable=SOURCE, new_stable=TARGET, old_ns=old_ns, new_ns=new_ns,
            dir_exists=dir_exists, target_exists=(memory_root(tmp_path) / new_ns).exists(),
        )])

    def test_memory_and_hits_move_and_the_backup_holds_the_routes_about_to_change(self, tmp_path: Path) -> None:
        old_ns, new_ns = conversation_namespace(SOURCE), conversation_namespace(TARGET)
        (memory_root(tmp_path) / old_ns).mkdir(parents=True)
        (memory_root(tmp_path) / old_ns / "book.yaml").write_text("entries: []\n", encoding="utf-8")
        hit_cache.hit_cache_path(tmp_path).write_text(json.dumps({f"{old_ns}/e1": "2026-01-01"}), encoding="utf-8")
        routes = route_state_file(tmp_path, MATRIX)
        routes.parent.mkdir(parents=True)
        routes.write_text(json.dumps({"real_conversation_keys": {OTHER: OTHER_REAL}}), encoding="utf-8")

        outcome = move_memory(workspace=tmp_path, platform=MATRIX, plan=self._plan(tmp_path))

        assert (memory_root(tmp_path) / new_ns / "book.yaml").is_file()
        assert not (memory_root(tmp_path) / old_ns).exists()
        assert hit_cache.read_hit_cache(tmp_path) == {f"{new_ns}/e1": "2026-01-01"}
        assert (outcome.moved_dirs, outcome.hit_keys_rewritten) == ([(old_ns, new_ns)], 1)
        assert (outcome.backup_dir / old_ns / "book.yaml").is_file()
        assert _routes(outcome.backup_dir / routes.name) == {"real_conversation_keys": {OTHER: OTHER_REAL}}
        # 路由状态留给调用方去改。
        assert outcome.route_keys_changed == 0
        assert _routes(routes) == {"real_conversation_keys": {OTHER: OTHER_REAL}}

    def test_the_caller_can_do_without_the_backup(self, tmp_path: Path) -> None:
        old_ns, new_ns = conversation_namespace(SOURCE), conversation_namespace(TARGET)
        (memory_root(tmp_path) / old_ns).mkdir(parents=True)

        outcome = move_memory(workspace=tmp_path, platform=MATRIX, plan=self._plan(tmp_path), make_backup=False)

        assert outcome.backup_dir is None
        assert (memory_root(tmp_path) / new_ns).is_dir()
        assert not list(tmp_path.glob("data/migration_backup_*"))

    def test_nothing_to_move_backs_up_nothing(self, tmp_path: Path) -> None:
        plan = MigrationPlan()

        outcome = move_memory(workspace=tmp_path, platform=MATRIX, plan=plan)

        assert outcome.backup_dir is None
        assert not list(tmp_path.glob("data/migration_backup_*"))

    def test_a_target_that_appears_after_the_preview_stops_before_the_backup(self, tmp_path: Path) -> None:
        old_ns, new_ns = conversation_namespace(SOURCE), conversation_namespace(TARGET)
        (memory_root(tmp_path) / old_ns).mkdir(parents=True)
        plan = self._plan(tmp_path)
        (memory_root(tmp_path) / new_ns).mkdir()

        with pytest.raises(RuntimeError):
            move_memory(workspace=tmp_path, platform=MATRIX, plan=plan)

        assert (memory_root(tmp_path) / old_ns).is_dir()
        assert not list(tmp_path.glob("data/migration_backup_*"))


class TestPlanMigration:
    def _routes(self, tmp_path: Path, mapping: dict[str, str]) -> None:
        path = route_state_file(tmp_path, MATRIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"real_conversation_keys": mapping}), encoding="utf-8")

    @pytest.mark.parametrize("answer", [None, ""], ids=["认不出来历", "算出空键"])
    def test_a_key_that_cannot_be_rekeyed_is_left_to_a_human(self, tmp_path: Path, answer: str | None) -> None:
        # 空键的目录名是空串，当成新键就把 data/memory 根目录本身当成了搬运目标。
        self._routes(tmp_path, {TARGET: TARGET_REAL})

        plan = plan_migration(workspace=tmp_path, platform=MATRIX, restable=lambda _old, _real: answer)

        assert [(item.old_stable, item.old_ns) for item in plan.unresolved] == [
            (TARGET, conversation_namespace(TARGET)),
        ]
        assert plan.renames == []

    def test_a_rekeyed_conversation_becomes_a_rename(self, tmp_path: Path) -> None:
        self._routes(tmp_path, {SOURCE: TARGET_REAL})

        plan = plan_migration(workspace=tmp_path, platform=MATRIX, restable=lambda _old, _real: TARGET)

        assert [(r.old_ns, r.new_ns) for r in plan.renames] == [
            (conversation_namespace(SOURCE), conversation_namespace(TARGET)),
        ]
        assert plan.unresolved == []


class TestApplyMigration:
    def test_a_new_platform_migrates_inside_its_own_data_dir(self, tmp_path: Path) -> None:
        renamed = "mx_room:" + "d" * 24
        routes = route_state_file(tmp_path, MATRIX)
        routes.parent.mkdir(parents=True)
        routes.write_text(json.dumps({
            "real_conversation_keys": {TARGET: TARGET_REAL}, "session_targets": {"s1": {"conversation_key": TARGET}},
        }), encoding="utf-8")
        (memory_root(tmp_path) / conversation_namespace(TARGET)).mkdir(parents=True)
        plan = MigrationPlan(renames=[Rename(
            real=TARGET_REAL, old_stable=TARGET, new_stable=renamed, old_ns=conversation_namespace(TARGET),
            new_ns=conversation_namespace(renamed), dir_exists=True, target_exists=False,
        )])

        outcome = apply_migration(workspace=tmp_path, platform=MATRIX, plan=plan, make_backup=False)

        assert _routes(routes) == {
            "real_conversation_keys": {renamed: TARGET_REAL}, "session_targets": {"s1": {"conversation_key": renamed}},
        }
        assert (outcome.route_keys_changed, outcome.backup_dir) == (2, None)
        assert (memory_root(tmp_path) / conversation_namespace(renamed)).is_dir()
        assert routes.parent == tmp_path / "data" / "platforms" / "matrix"
