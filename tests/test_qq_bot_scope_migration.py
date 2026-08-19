"""按 bot 账号隔离会话与记忆的契约，以及整体搬迁到另一个号的行为。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_test_{name}", _ROOT / "adapters" / "onebot" / f"{name}.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_test_{name}"] = module
    spec.loader.exec_module(module)
    return module


ch = _load("conversation_hash")
bot_scope = _load("bot_scope")

_migrate_spec = importlib.util.spec_from_file_location(
    "_test_migrate_scope", _ROOT / "deploy" / "migrate_qq_bot_scope.py",
)
assert _migrate_spec and _migrate_spec.loader
migrate = importlib.util.module_from_spec(_migrate_spec)
sys.modules["_test_migrate_scope"] = migrate
_migrate_spec.loader.exec_module(migrate)

_SECRET = "a" * 64
_REAL = "qq_group:123456789"
_OLD_BOT = "10000001"
_NEW_BOT = "10000002"


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
    (data / "sessions.json").write_text(
        json.dumps({"sessions": {"sid-1": {"conversation_key": stable, "channel": "qq_group"}}}),
        encoding="utf-8",
    )
    (data / "onebot_conversation_hash_secret").write_text(_SECRET, encoding="utf-8")
    (data / "attachments" / stable.replace(":", "_")).mkdir(parents=True)
    (data / "attachments" / stable.replace(":", "_") / "a.png").write_bytes(b"x")
    if scope:
        bot_scope.save_scope(tmp_path, scope)
    return {"stable": stable, "ns": ns}


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
        assert bot_scope.normalize("not-a-qq") == ""
        assert bot_scope.normalize(None) == ""
        assert bot_scope.normalize(" 12345 ") == "12345"


class TestScopeFile:
    def test_a_missing_file_reads_as_no_scope(self, tmp_path: Path) -> None:
        assert bot_scope.load_scope(tmp_path) == ""

    def test_a_corrupt_file_degrades_instead_of_guessing(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        bot_scope.scope_file(tmp_path).write_text("{not json", encoding="utf-8")
        assert bot_scope.load_scope(tmp_path) == ""

    def test_saving_reports_whether_the_account_actually_changed(self, tmp_path: Path) -> None:
        assert bot_scope.save_scope(tmp_path, _OLD_BOT) is True
        assert bot_scope.save_scope(tmp_path, _OLD_BOT) is False
        assert bot_scope.save_scope(tmp_path, _NEW_BOT) is True
        assert bot_scope.load_scope(tmp_path) == _NEW_BOT

    def test_an_empty_self_id_never_erases_a_known_scope(self, tmp_path: Path) -> None:
        bot_scope.save_scope(tmp_path, _OLD_BOT)
        assert bot_scope.save_scope(tmp_path, "") is False
        assert bot_scope.load_scope(tmp_path) == _OLD_BOT


class TestScopeMigration:
    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=False,
        )
        assert report.changed_renames
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert bot_scope.load_scope(tmp_path) == ""

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
        assert sessions["sessions"]["sid-1"]["conversation_key"] == new_stable
        assert (data / "attachments" / new_stable.replace(":", "_") / "a.png").is_file()
        assert bot_scope.load_scope(tmp_path) == _NEW_BOT
        assert report.backup_dir is not None and report.backup_dir.is_dir()

    def test_the_backup_holds_the_pre_migration_memory(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path)
        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, source_scope="", apply=True,
        )
        assert (report.backup_dir / seed["ns"] / "book.yaml").is_file()
        assert (report.backup_dir / "sessions.json").is_file()

    def test_the_source_is_detected_when_the_recorded_account_disagrees(self, tmp_path: Path) -> None:
        # 存量数据没有作用域，而 bot 一登录就把当前号记下了，两者天然对不上。
        seed = _seed(tmp_path)
        bot_scope.save_scope(tmp_path, _OLD_BOT)
        assert migrate.detect_source_scope(tmp_path, _SECRET) == ""

        report = migrate.run_scope_migration(
            workspace=tmp_path, target_scope=_NEW_BOT, apply=True,
        )

        assert report.source_scope == ""
        assert not (tmp_path / "data" / "memory" / seed["ns"]).exists()

    def test_an_unrecognisable_source_refuses_to_guess(self, tmp_path: Path) -> None:
        _seed(tmp_path, scope="70000009")
        bot_scope.save_scope(tmp_path, _OLD_BOT)
        assert migrate.detect_source_scope(tmp_path, _SECRET) is None
        with pytest.raises(migrate.ScopeMismatch):
            migrate.run_scope_migration(workspace=tmp_path, target_scope=_NEW_BOT, apply=True)

    def test_a_wrong_source_account_aborts_before_touching_anything(self, tmp_path: Path) -> None:
        seed = _seed(tmp_path, scope=_OLD_BOT)
        with pytest.raises(migrate.ScopeMismatch):
            migrate.run_scope_migration(
                workspace=tmp_path, target_scope=_NEW_BOT, source_scope="999", apply=True,
            )
        assert (tmp_path / "data" / "memory" / seed["ns"]).is_dir()
        assert bot_scope.load_scope(tmp_path) == _OLD_BOT

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
        assert bot_scope.load_scope(tmp_path) == _OLD_BOT
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
