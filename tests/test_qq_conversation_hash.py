"""会话键摘要的盐决策与离线迁移。

无盐 SHA256 的输入只有「前缀 + 十位数字」，能被枚举反推真实群号/QQ 号；
默认给干净工作区加盐，存量部署钉在无盐、加盐迁移交离线脚本。
"""
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


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ch = _load("_qq_conversation_hash", "adapters/onebot/conversation_hash.py")
migrate = _load("_qq_migrate_conversation_hash", "deploy/migrate_qq_conversation_hash.py")

_SECRET = "0123456789abcdef" * 4  # 64 hex chars


def _resolve(tmp_path: Path, *, env_secret: str = "", route_state: str = "state.json"):
    return ch.resolve_secret(
        env_secret=env_secret,
        secret_file=tmp_path / "data" / "onebot_conversation_hash_secret",
        workspace=tmp_path,
        route_state_file=tmp_path / "data" / route_state,
    )


class TestResolveSecret:
    def test_an_explicit_secret_wins_and_writes_no_file(self, tmp_path: Path) -> None:
        secret, salted = _resolve(tmp_path, env_secret="explicit-value")

        assert (secret, salted) == ("explicit-value", True)
        assert not (tmp_path / "data" / "onebot_conversation_hash_secret").exists()

    def test_a_clean_workspace_mints_and_pins_a_secret(self, tmp_path: Path) -> None:
        secret, salted = _resolve(tmp_path)

        secret_file = tmp_path / "data" / "onebot_conversation_hash_secret"
        assert salted is True
        assert ch._SECRET_RE.fullmatch(secret)
        assert secret_file.read_text(encoding="utf-8").strip() == secret

    def test_the_pinned_secret_is_reused_next_start(self, tmp_path: Path) -> None:
        first, _ = _resolve(tmp_path)
        second, salted = _resolve(tmp_path)

        assert first == second
        assert salted is True

    def test_an_existing_route_state_pins_the_legacy_mode(self, tmp_path: Path) -> None:
        state = tmp_path / "data" / "onebot_plugin_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"real_conversation_keys": {}}), encoding="utf-8")

        secret, salted = _resolve(tmp_path, route_state="onebot_plugin_state.json")

        assert (secret, salted) == ("", False)
        secret_file = tmp_path / "data" / "onebot_conversation_hash_secret"
        assert secret_file.read_text(encoding="utf-8").strip() == ch.LEGACY_MARKER

    def test_an_existing_memory_namespace_also_pins_legacy(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)

        secret, salted = _resolve(tmp_path)

        assert (secret, salted) == ("", False)
        secret_file = tmp_path / "data" / "onebot_conversation_hash_secret"
        assert secret_file.read_text(encoding="utf-8").strip() == ch.LEGACY_MARKER

    def test_a_corrupt_secret_file_degrades_instead_of_rotating(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "data" / "onebot_conversation_hash_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("xx", encoding="utf-8")

        secret, salted = _resolve(tmp_path)

        assert (secret, salted) == ("", False)
        # 密钥文件绝不能被改写：一旦重生成，现存记忆目录全成孤儿。
        assert secret_file.read_text(encoding="utf-8") == "xx"


class TestDigest:
    def test_the_legacy_digest_stays_byte_identical(self) -> None:
        assert ch.digest("qq_group:123456789", "") == "bc3f12b0621298e191a49fa7"
        assert ch.digest("qq_group:123456789", "") == hashlib.sha256(
            b"qq_group:123456789").hexdigest()[:24]

    def test_a_salted_digest_is_not_the_bare_sha256(self) -> None:
        assert ch.digest("qq_group:123456789", _SECRET) != ch.digest("qq_group:123456789", "")

    def test_the_salted_digest_is_stable_for_one_secret(self) -> None:
        assert ch.digest("qq_group:123456789", _SECRET) == ch.digest("qq_group:123456789", _SECRET)


def _seed_legacy_workspace(tmp_path: Path, real: str = "qq_group:123456789") -> dict:
    """造一个无盐存量工作区，返回旧摘要衍生的各处名字。"""
    old_stable = f"qq_group:{ch.digest(real, '')}"
    old_ns = "conv_" + hashlib.sha256(old_stable.encode("utf-8")).hexdigest()[:24]
    data = tmp_path / "data"
    (data / "memory" / old_ns).mkdir(parents=True)
    (data / "memory" / old_ns / "book.yaml").write_text("book: x\n", encoding="utf-8")
    (data / "memory" / ".hit_cache.json").write_text(
        json.dumps({f"{old_ns}/m1": "2020-01-01T00:00:00+00:00", "flat_id": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (data / "onebot_plugin_state.json").write_text(
        json.dumps({"version": 1, "real_conversation_keys": {old_stable: real},
                    "session_targets": {}}),
        encoding="utf-8",
    )
    (data / "sessions.json").write_text(json.dumps({"sentinel": old_stable}), encoding="utf-8")
    (data / "onebot_conversation_hash_secret").write_text(ch.LEGACY_MARKER, encoding="utf-8")
    return {"real": real, "old_stable": old_stable, "old_ns": old_ns}


class TestMigration:
    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        seed = _seed_legacy_workspace(tmp_path)
        data = tmp_path / "data"

        report = migrate.run_migration(workspace=tmp_path, apply=False, secret=_SECRET)

        assert report.changed_renames  # 有待迁移会话
        assert (data / "memory" / seed["old_ns"]).is_dir()
        assert (data / "onebot_conversation_hash_secret").read_text(encoding="utf-8") == ch.LEGACY_MARKER
        state = json.loads((data / "onebot_plugin_state.json").read_text(encoding="utf-8"))
        assert seed["old_stable"] in state["real_conversation_keys"]

    def test_apply_renames_dir_hitcache_and_route_and_writes_secret(self, tmp_path: Path) -> None:
        seed = _seed_legacy_workspace(tmp_path)
        data = tmp_path / "data"
        new_stable = f"qq_group:{ch.digest(seed['real'], _SECRET)}"
        new_ns = "conv_" + hashlib.sha256(new_stable.encode("utf-8")).hexdigest()[:24]

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert (data / "memory" / new_ns).is_dir()
        assert not (data / "memory" / seed["old_ns"]).exists()
        cache = json.loads((data / "memory" / ".hit_cache.json").read_text(encoding="utf-8"))
        assert f"{new_ns}/m1" in cache
        assert f"{seed['old_ns']}/m1" not in cache
        assert cache["flat_id"]  # 扁平 key 不动
        state = json.loads((data / "onebot_plugin_state.json").read_text(encoding="utf-8"))
        assert new_stable in state["real_conversation_keys"]
        assert seed["old_stable"] not in state["real_conversation_keys"]
        assert (data / "onebot_conversation_hash_secret").read_text(encoding="utf-8") == _SECRET
        assert report.secret_written is True
        assert report.backup_dir is not None and report.backup_dir.is_dir()

    def test_apply_never_touches_supervisor_sessions(self, tmp_path: Path) -> None:
        seed = _seed_legacy_workspace(tmp_path)
        sessions = tmp_path / "data" / "sessions.json"
        before = sessions.read_text(encoding="utf-8")

        migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert sessions.read_text(encoding="utf-8") == before

    def test_an_unmapped_namespace_dir_is_left_alone_and_reported(self, tmp_path: Path) -> None:
        _seed_legacy_workspace(tmp_path)
        orphan = tmp_path / "data" / "memory" / "conv_000000000000000000000000"
        orphan.mkdir()

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert orphan.is_dir()
        assert "conv_000000000000000000000000" in report.unknown_conv_dirs

    def test_a_second_run_is_idempotent(self, tmp_path: Path) -> None:
        _seed_legacy_workspace(tmp_path)
        migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        # 第二次不带 secret：复用已写入的密钥，应不再改任何东西。
        report = migrate.run_migration(workspace=tmp_path, apply=True)

        assert report.moved_dirs == []
        assert report.route_keys_changed == 0
