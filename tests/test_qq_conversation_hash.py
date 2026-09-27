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

from adapters._shared import conversation_hash as ch  # noqa: E402
from adapters._shared.active_account import ActiveAccountStore  # noqa: E402
from engine.conversation_migration import apply_migration, plan_migration  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    ID_STRING,
    ChannelDescriptor,
    PlatformDescriptor,
    SecretField,
)


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migrate = _load("_qq_migrate_conversation_hash", "deploy/migrate_qq_conversation_hash.py")

_SECRET = "0123456789abcdef" * 4  # 64 hex chars
_QQ_ENVS = ("ONEBOT_CONVERSATION_HASH_SECRET", "ONEBOT_CONVERSATION_HASH_SECRET_FILE", "ONEBOT_STATE_FILE")
_NEW = PlatformDescriptor(
    id="newchat", display_name="New Chat", short_name="New", id_shape=ID_STRING,
    channels=(ChannelDescriptor("new_room", CHANNEL_GROUP), ChannelDescriptor("new_unknown", CHANNEL_OTHER)),
    secrets=(SecretField(
        "conversation_hash_secret", "会话键摘要密钥", env="NEWCHAT_HASH_SECRET",
        file="data/platforms/newchat/conversation_hash_secret",
    ),),
)


@pytest.fixture(autouse=True)
def _no_deployment_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 密钥和路由文件的位置都认 env，开发机上设着的话会读到别处去。先登记一次，命令行从 .env 灌进来的测完也撤掉。
    for name in (*_QQ_ENVS, "NEWCHAT_HASH_SECRET", "CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _resolve(
    tmp_path: Path, *, explicit_secret: str = "", route_state: str = "state.json", allow_legacy: bool = True,
):
    return ch.resolve_secret(
        explicit_secret=explicit_secret,
        secret_file=tmp_path / "data" / "onebot_conversation_hash_secret",
        workspace=tmp_path,
        route_state_file=tmp_path / "data" / route_state,
        allow_legacy=allow_legacy,
    )


class TestResolveSecret:
    def test_an_explicit_secret_wins_and_writes_no_file(self, tmp_path: Path) -> None:
        secret, salted = _resolve(tmp_path, explicit_secret="explicit-value")

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

    def test_a_platform_without_legacy_data_never_pins_the_legacy_mode(self, tmp_path: Path) -> None:
        # 同一工作区里别的平台留下的记忆目录和路由不是它的存量，钉成无盐就永远不加盐了。
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)
        (tmp_path / "data" / "state.json").write_text("{}", encoding="utf-8")

        secret, salted = _resolve(tmp_path, allow_legacy=False)

        assert salted is True and ch._SECRET_RE.fullmatch(secret)

    def test_a_corrupt_secret_file_degrades_instead_of_rotating(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "data" / "onebot_conversation_hash_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("xx", encoding="utf-8")

        secret, salted = _resolve(tmp_path)

        assert (secret, salted) == ("", False)
        # 密钥文件绝不能被改写：一旦重生成，现存记忆目录全成孤儿。
        assert secret_file.read_text(encoding="utf-8") == "xx"


class TestPlatformSecret:
    def test_qq_reads_an_explicit_secret_from_data_config(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "config.yaml").write_text(
            f"platforms:\n  qq:\n    conversation_hash_secret: {_SECRET}\n", encoding="utf-8",
        )

        assert ch.platform_conversation_secret(QQ_NAPCAT, tmp_path) == (_SECRET, True)
        assert not (tmp_path / "data" / "onebot_conversation_hash_secret").exists()

    def test_qq_still_pins_legacy_over_its_own_old_state(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)

        assert ch.platform_conversation_secret(QQ_NAPCAT, tmp_path) == ("", False)

    def test_a_new_platform_mints_its_own_secret_next_to_qq_history(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)

        secret, salted = ch.platform_conversation_secret(_NEW, tmp_path)

        pinned = tmp_path / "data" / "platforms" / "newchat" / "conversation_hash_secret"
        assert salted is True and pinned.read_text(encoding="utf-8") == secret

    def test_the_pinned_file_follows_its_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", "private/hash")

        secret, _salted = ch.platform_conversation_secret(QQ_NAPCAT, tmp_path)

        assert (tmp_path / "private" / "hash").read_text(encoding="utf-8") == secret
        assert not (tmp_path / "data" / "onebot_conversation_hash_secret").exists()


class TestCurrentSecret:
    def test_an_explicit_secret_from_env_comes_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # bootstrap 生成的部署把密钥写在 .env 里，自动密钥文件根本不存在。
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", _SECRET)

        assert ch.current_secret(QQ_NAPCAT, tmp_path) == ch.CurrentSecret(_SECRET, ch.SECRET_EXPLICIT)

    def test_an_explicit_environ_stands_in_for_the_process_one(self, tmp_path: Path) -> None:
        current = ch.current_secret(QQ_NAPCAT, tmp_path, environ={"ONEBOT_CONVERSATION_HASH_SECRET": _SECRET})

        assert current.value == _SECRET

    def test_the_pinned_file_is_read_where_the_bot_keeps_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        moved = tmp_path / "private" / "hash"
        moved.parent.mkdir()
        moved.write_text(_SECRET, encoding="utf-8")
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", str(moved))

        assert ch.current_secret(QQ_NAPCAT, tmp_path) == ch.CurrentSecret(_SECRET, ch.SECRET_PINNED)

    def test_the_legacy_marker_reads_as_unsalted(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "onebot_conversation_hash_secret").write_text(ch.LEGACY_MARKER, encoding="utf-8")

        assert ch.current_secret(QQ_NAPCAT, tmp_path) == ch.CurrentSecret("", ch.SECRET_PINNED)

    def test_old_qq_state_without_a_file_reads_as_the_legacy_mode(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)

        current = ch.current_secret(QQ_NAPCAT, tmp_path)

        assert current == ch.CurrentSecret("", ch.SECRET_LEGACY) and current.settled
        assert not (tmp_path / "data" / "onebot_conversation_hash_secret").exists()

    def test_a_platform_that_never_started_is_unsettled(self, tmp_path: Path) -> None:
        (tmp_path / "data" / "memory" / "conv_deadbeef").mkdir(parents=True)

        current = ch.current_secret(_NEW, tmp_path)

        assert current == ch.CurrentSecret("", ch.SECRET_UNSETTLED) and not current.settled
        assert not (tmp_path / "data" / "platforms").exists()

    def test_a_platform_without_the_secret_declared_says_so(self, tmp_path: Path) -> None:
        bare = PlatformDescriptor(id="bare", display_name="Bare", short_name="B")

        with pytest.raises(LookupError):
            ch.current_secret(bare, tmp_path)


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
    (data / "sessions.json").write_text(
        json.dumps({"sid-1": {
            "session_id": "sid-1", "conversation_key": old_stable, "channel": "qq_group",
            "created_at": "2026-01-01T00:00:00+00:00", "reset": False,
        }}),
        encoding="utf-8",
    )
    (data / "onebot_conversation_hash_secret").write_text(ch.LEGACY_MARKER, encoding="utf-8")
    return {"real": real, "old_stable": old_stable, "old_ns": old_ns}


def _add_legacy_route(tmp_path: Path, real: str, *, scope: str, secret: str = "", memory: bool = True) -> dict:
    """往存量路由表里再添一个键：账号隔离之后算的键带着作用域，换号前的旧键也还留着。"""
    stable = f"{real.split(':', 1)[0]}:{ch.digest(real, secret, bot_scope=scope)}"
    path = tmp_path / "data" / "onebot_plugin_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["real_conversation_keys"][stable] = real
    path.write_text(json.dumps(state), encoding="utf-8")
    ns = _ns(stable)
    if memory:
        (tmp_path / "data" / "memory" / ns).mkdir(parents=True)
        (tmp_path / "data" / "memory" / ns / f"{scope or 'unscoped'}.yaml").write_text("x: 1\n", encoding="utf-8")
    return {"stable": stable, "ns": ns}


def _ns(stable: str) -> str:
    return "conv_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _routes(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "data" / "onebot_plugin_state.json").read_text(encoding="utf-8"))["real_conversation_keys"]


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

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert sessions.read_text(encoding="utf-8") == before
        assert seed["old_stable"] in before and "sessions" not in report.stores

    def test_feature_data_follows_the_new_digest(self, tmp_path: Path) -> None:
        # 短期上下文按既定做法丢一次，但定时任务、提醒到点要按新键找到会话，不跟着换就投不出去。
        from supervisor.scheduler import load_schedules, save_schedules

        seed = _seed_legacy_workspace(tmp_path)
        save_schedules(tmp_path, [{"id": "morning", "cron": "0 9 * * *", "text": "早", "conversation_key": seed["old_stable"]}])
        new_stable = f"qq_group:{ch.digest(seed['real'], _SECRET)}"

        preview = migrate.run_migration(workspace=tmp_path, apply=False, secret=_SECRET)
        assert preview.stores["schedules"].moved == 1
        assert load_schedules(tmp_path)[0]["conversation_key"] == seed["old_stable"]

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert load_schedules(tmp_path)[0]["conversation_key"] == new_stable
        assert report.stores["schedules"].moved == 1
        assert (report.backup_dir / "data" / "schedules.yaml").is_file()

    def test_an_unmapped_namespace_dir_is_left_alone_and_reported(self, tmp_path: Path) -> None:
        _seed_legacy_workspace(tmp_path)
        orphan = tmp_path / "data" / "memory" / "conv_000000000000000000000000"
        orphan.mkdir()

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert orphan.is_dir()
        assert "conv_000000000000000000000000" in report.unknown_conv_dirs

    def test_an_explicit_secret_is_the_target_and_no_file_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # bot 用的是配好的那把；另生成一把写进文件的话，迁完 bot 照样对不上。
        seed = _seed_legacy_workspace(tmp_path)
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", _SECRET)
        data = tmp_path / "data"

        report = migrate.run_migration(workspace=tmp_path, apply=True)

        new_stable = f"qq_group:{ch.digest(seed['real'], _SECRET)}"
        state = json.loads((data / "onebot_plugin_state.json").read_text(encoding="utf-8"))
        assert report.explicit_secret is True and report.target_secret == _SECRET
        assert new_stable in state["real_conversation_keys"]
        assert report.secret_written is False
        assert (data / "onebot_conversation_hash_secret").read_text(encoding="utf-8") == ch.LEGACY_MARKER

    def test_a_secret_argument_that_the_bot_would_ignore_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed = _seed_legacy_workspace(tmp_path)
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET", _SECRET)

        with pytest.raises(migrate.SecretConflict):
            migrate.run_migration(workspace=tmp_path, apply=True, secret="f" * 64)

        assert (tmp_path / "data" / "memory" / seed["old_ns"]).is_dir()
        assert migrate.run_migration(workspace=tmp_path, apply=False, secret=_SECRET).target_secret == _SECRET

    def test_the_command_line_takes_the_secret_from_the_workspace_env_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # deploy/bootstrap.sh 把密钥写进工作区 .env；命令行不灌它就会另生成一把，迁完 bot 对不上。
        seed = _seed_legacy_workspace(tmp_path)
        (tmp_path / ".env").write_text(f"ONEBOT_CONVERSATION_HASH_SECRET={_SECRET}\n", encoding="utf-8")

        refused = migrate.main(["--workspace", str(tmp_path), "--apply", "--secret", "f" * 64])

        assert refused == 2 and "密钥冲突" in capsys.readouterr().err
        assert (tmp_path / "data" / "memory" / seed["old_ns"]).is_dir()

        assert migrate.main(["--workspace", str(tmp_path), "--apply"]) == 0

        new_stable = f"qq_group:{ch.digest(seed['real'], _SECRET)}"
        state = json.loads((tmp_path / "data" / "onebot_plugin_state.json").read_text(encoding="utf-8"))
        assert new_stable in state["real_conversation_keys"]
        assert (tmp_path / "data" / "onebot_conversation_hash_secret").read_text(encoding="utf-8") == ch.LEGACY_MARKER

    def test_the_secret_file_is_written_where_the_bot_reads_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_legacy_workspace(tmp_path)
        moved = tmp_path / "private" / "hash"
        moved.parent.mkdir()
        moved.write_text(ch.LEGACY_MARKER, encoding="utf-8")
        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", str(moved))

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert report.secret_written is True
        assert moved.read_text(encoding="utf-8") == _SECRET
        assert (tmp_path / "data" / "onebot_conversation_hash_secret").read_text(encoding="utf-8") == ch.LEGACY_MARKER

    def test_a_second_run_is_idempotent(self, tmp_path: Path) -> None:
        _seed_legacy_workspace(tmp_path)
        migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        # 第二次不带 secret：复用已写入的密钥，应不再改任何东西。
        report = migrate.run_migration(workspace=tmp_path, apply=True)

        assert report.moved_dirs == []
        assert report.route_keys_changed == 0


class TestKeysUnderSeveralAccounts:
    def test_a_scoped_key_keeps_its_account(self, tmp_path: Path) -> None:
        # 重算时丢掉作用域，bot 登录后按账号算出来的键对不上，记忆照样成了孤儿。
        _seed_legacy_workspace(tmp_path)
        scoped = _add_legacy_route(tmp_path, "qq_group:987654321", scope="10001")
        ActiveAccountStore(QQ_NAPCAT, tmp_path).save("10001")

        migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        new_stable = f"qq_group:{ch.digest('qq_group:987654321', _SECRET, bot_scope='10001')}"
        assert _routes(tmp_path)[new_stable] == "qq_group:987654321"
        assert (tmp_path / "data" / "memory" / _ns(new_stable) / "10001.yaml").is_file()
        assert not (tmp_path / "data" / "memory" / scoped["ns"]).exists()

    def test_one_group_under_two_accounts_moves_as_two_conversations(self, tmp_path: Path) -> None:
        # 两个键都按无作用域重算会撞进同一个目录，后搬的那份被塞进先搬的里面。
        seed = _seed_legacy_workspace(tmp_path)
        _add_legacy_route(tmp_path, seed["real"], scope="10001")
        ActiveAccountStore(QQ_NAPCAT, tmp_path).save("10001")

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        unscoped = f"qq_group:{ch.digest(seed['real'], _SECRET)}"
        scoped = f"qq_group:{ch.digest(seed['real'], _SECRET, bot_scope='10001')}"
        memory = tmp_path / "data" / "memory"
        assert _routes(tmp_path) == {unscoped: seed["real"], scoped: seed["real"]}
        assert sorted(path.name for path in (memory / _ns(unscoped)).iterdir()) == ["book.yaml"]
        assert sorted(path.name for path in (memory / _ns(scoped)).iterdir()) == ["10001.yaml"]
        assert len(report.moved_dirs) == 2 and report.blocked == []

    def test_a_key_from_a_forgotten_account_waits_until_it_is_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_legacy_workspace(tmp_path)
        stray = _add_legacy_route(tmp_path, "qq_group:555", scope="10009")

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert [item.old_stable for item in report.unresolved] == [stray["stable"]]
        assert stray["ns"] not in report.unknown_conv_dirs
        assert _routes(tmp_path)[stray["stable"]] == "qq_group:555"
        assert (tmp_path / "data" / "memory" / stray["ns"]).is_dir()

        assert migrate.main(["--workspace", str(tmp_path), "--apply", "--account", "10009"]) == 0

        new_stable = f"qq_group:{ch.digest('qq_group:555', _SECRET, bot_scope='10009')}"
        assert _routes(tmp_path).get(new_stable) == "qq_group:555"
        assert (tmp_path / "data" / "memory" / _ns(new_stable) / "10009.yaml").is_file()
        assert "10009" in capsys.readouterr().out

    def test_a_run_interrupted_before_the_secret_was_written_can_be_repeated(self, tmp_path: Path) -> None:
        # 密钥最后才写：前面都改完了、密钥没写上，重跑时盘上的键已经是目标密钥算的，得认得出来。
        seed = _seed_legacy_workspace(tmp_path)
        migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)
        (tmp_path / "data" / "onebot_conversation_hash_secret").write_text(ch.LEGACY_MARKER, encoding="utf-8")

        again = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert again.unresolved == [] and again.changed_renames == []
        assert again.secret_written is True
        assert _routes(tmp_path) == {f"qq_group:{ch.digest(seed['real'], _SECRET)}": seed["real"]}

    def test_an_account_argument_must_be_a_qq_number(self, tmp_path: Path) -> None:
        _seed_legacy_workspace(tmp_path)

        with pytest.raises(SystemExit) as caught:
            migrate.main(["--workspace", str(tmp_path), "--apply", "--account", "1000l"])

        assert caught.value.code == 2

    def test_both_sides_with_memory_leave_the_conversation_alone(self, tmp_path: Path) -> None:
        from supervisor.scheduler import load_schedules, save_schedules

        seed = _seed_legacy_workspace(tmp_path)
        new_stable = f"qq_group:{ch.digest(seed['real'], _SECRET)}"
        (tmp_path / "data" / "memory" / _ns(new_stable)).mkdir()
        save_schedules(tmp_path, [{"id": "m", "cron": "0 9 * * *", "text": "早", "conversation_key": seed["old_stable"]}])

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert [rename.old_stable for rename in report.blocked] == [seed["old_stable"]]
        assert _routes(tmp_path) == {seed["old_stable"]: seed["real"]}
        assert (tmp_path / "data" / "memory" / seed["old_ns"] / "book.yaml").is_file()
        assert load_schedules(tmp_path)[0]["conversation_key"] == seed["old_stable"]

    def test_keys_that_would_collide_are_all_left_alone(self, tmp_path: Path) -> None:
        # 同一个群在无盐和旧密钥下各有一个键，换成第三把密钥就撞到一起，分不出该留谁。
        seed = _seed_legacy_workspace(tmp_path)
        previous = "1" * 64
        (tmp_path / "data" / "onebot_conversation_hash_secret").write_text(previous, encoding="utf-8")
        salted = _add_legacy_route(tmp_path, seed["real"], scope="", secret=previous)

        report = migrate.run_migration(workspace=tmp_path, apply=True, secret=_SECRET)

        assert sorted(rename.old_stable for rename in report.blocked) == sorted([seed["old_stable"], salted["stable"]])
        assert all(rename.collides for rename in report.blocked)
        assert _routes(tmp_path) == {seed["old_stable"]: seed["real"], salted["stable"]: seed["real"]}
        assert (tmp_path / "data" / "memory" / seed["old_ns"]).is_dir()
        assert (tmp_path / "data" / "memory" / salted["ns"]).is_dir()

    def test_a_target_that_appears_after_the_preview_stops_the_move(self, tmp_path: Path) -> None:
        seed = _seed_legacy_workspace(tmp_path)
        plan = plan_migration(
            workspace=tmp_path, platform=QQ_NAPCAT, restable=lambda old, real: ch.rekey(old, real, _SECRET),
        )
        (tmp_path / "data" / "memory" / plan.changed[0].new_ns).mkdir()

        with pytest.raises(RuntimeError):
            apply_migration(workspace=tmp_path, platform=QQ_NAPCAT, plan=plan)

        assert (tmp_path / "data" / "memory" / seed["old_ns"] / "book.yaml").is_file()
        assert _routes(tmp_path) == {seed["old_stable"]: seed["real"]}
        assert not list((tmp_path / "data").glob("migration_backup_*"))
