"""跨平台绑定：一个会话的长期记忆、短期上下文和功能数据挂到另一个平台的会话上，目标 bot 自己算出来的键能直接接着用。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.active_account import ActiveAccountStore  # noqa: E402
from adapters._shared.conversation_hash import ConversationKeys  # noqa: E402
from engine.conversation_migration import load_route_map, memory_root, namespace_dir, route_state_file  # noqa: E402
from engine.materials.service import MaterialService  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    ChannelDescriptor,
    PlatformDescriptor,
    SecretField,
)
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.session_store import session_registry_path  # noqa: E402
from workspace import workspace_instance  # noqa: E402

_spec = importlib.util.spec_from_file_location("_test_platform_binding", _ROOT / "deploy" / "migrate_platform_identity.py")
assert _spec and _spec.loader
bind = importlib.util.module_from_spec(_spec)
sys.modules["_test_platform_binding"] = bind
_spec.loader.exec_module(bind)

MATRIX = PlatformDescriptor(
    id="matrix",
    display_name="Matrix",
    short_name="MX",
    channels=(
        ChannelDescriptor("mx_room", CHANNEL_GROUP),
        ChannelDescriptor("mx_dm", CHANNEL_PRIVATE),
        ChannelDescriptor("mx_unknown", CHANNEL_OTHER),
    ),
    secrets=(
        SecretField("conversation_hash_secret", "会话键摘要密钥", file="data/platforms/matrix/conversation_hash_secret"),
    ),
)
QQ_SECRET = "a" * 64
MX_SECRET = "b" * 64
QQ_REAL = "qq_group:123456789"
MX_REAL = "mx_room:room123"
QQ_BOT = "10001"
QQ_OTHER_BOT = "10002"
MX_BOT = "mxbot"
_QQ_ENVS = ("ONEBOT_CONVERSATION_HASH_SECRET", "ONEBOT_CONVERSATION_HASH_SECRET_FILE", "ONEBOT_STATE_FILE")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    # 密钥和路由文件的位置都认 env，开发机上设着的话会读到别处去。先登记一次，命令行从 .env 灌进来的测完也撤掉。
    for name in (*_QQ_ENVS, "CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    register_platform(MATRIX)
    try:
        yield
    finally:
        unregister_platform(MATRIX.id)


def _qq_stable(real: str = QQ_REAL, account: str = QQ_BOT) -> str:
    return ConversationKeys(QQ_NAPCAT, QQ_SECRET).stable_key(real, bot_scope=account)


def _mx_stable(real: str = MX_REAL, account: str = MX_BOT) -> str:
    return ConversationKeys(MATRIX, MX_SECRET).stable_key(real, bot_scope=account)


def _add_route(path: Path, stable: str, real: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "real_conversation_keys": {}}
    data["real_conversation_keys"][stable] = real
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _actor(scope: str, bot: str) -> dict:
    return {"scope": scope, "owner": "user:alice", "bot_scope": bot, "is_admin": True, "role": "admin"}


def _seed(tmp_path: Path, *, account: str = QQ_BOT, memory: bool = True, features: bool = True) -> str:
    """QQ 那边一个群：路由表里有它，按 account 算的键下有长期记忆、会话和资料设置。返回它的稳定键。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "onebot_conversation_hash_secret").write_text(QQ_SECRET, encoding="utf-8")
    if account:
        ActiveAccountStore(QQ_NAPCAT, tmp_path).save(account)
    stable = _qq_stable(account=account)
    _add_route(route_state_file(tmp_path, QQ_NAPCAT), stable, QQ_REAL)
    if memory:
        (memory_root(tmp_path) / namespace_dir(stable)).mkdir(parents=True)
        (memory_root(tmp_path) / namespace_dir(stable) / "book.yaml").write_text("entries: []\n", encoding="utf-8")
    if features:
        session_registry_path(tmp_path).write_text(json.dumps({"sid-1": {
            "session_id": "sid-1", "conversation_key": stable, "channel": "qq_group",
            "created_at": "2026-01-01T00:00:00+00:00", "reset": False,
        }}), encoding="utf-8")
        service = MaterialService(tmp_path)
        try:
            # 加账号隔离之前的功能记录，账号一栏记的是工作区实例号。
            service.store.save_settings(_actor(stable, account or workspace_instance(tmp_path)), {"journal_enabled": True})
        finally:
            service.close()
    return stable


def _target_ready(tmp_path: Path, *, secret: bool = True, account: str = MX_BOT) -> None:
    """目标平台起过一次（钉住了密钥）、登录过（记下了账号）。"""
    if secret:
        path = tmp_path / "data" / "platforms" / "matrix" / "conversation_hash_secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MX_SECRET, encoding="utf-8")
    if account:
        ActiveAccountStore(MATRIX, tmp_path).save(account)


def _journal_enabled(tmp_path: Path, scope: str, bot: str) -> bool:
    service = MaterialService(tmp_path)
    try:
        return service.store.settings(_actor(scope, bot)).get("journal_enabled") is True
    finally:
        service.close()


def _session(tmp_path: Path) -> dict:
    [row] = json.loads(session_registry_path(tmp_path).read_text(encoding="utf-8")).values()
    return row


class TestBinding:
    def test_a_preview_reports_the_plan_and_changes_nothing(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        assert (report.source.stable, report.source.account) == (stable, QQ_BOT)
        assert (report.target.stable, report.target.account) == (_mx_stable(), MX_BOT)
        assert (report.source_memory, report.target_memory, report.route_recorded) == (True, False, True)
        assert (report.stores["sessions"].moved, report.stores["materials"].moved) == (1, 1)
        assert (memory_root(tmp_path) / namespace_dir(stable)).is_dir()
        assert not route_state_file(tmp_path, MATRIX).exists()
        assert _session(tmp_path)["conversation_key"] == stable
        assert _journal_enabled(tmp_path, stable, QQ_BOT)
        assert report.backup_dir is None

    def test_apply_moves_everything_onto_the_key_the_target_bot_derives(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path)
        target, elsewhere = _mx_stable(), _mx_stable("mx_room:elsewhere")
        _add_route(route_state_file(tmp_path, MATRIX), elsewhere, "mx_room:elsewhere")
        mx_routes = route_state_file(tmp_path, MATRIX).read_text(encoding="utf-8")
        qq_routes = route_state_file(tmp_path, QQ_NAPCAT).read_text(encoding="utf-8")

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (memory_root(tmp_path) / namespace_dir(target) / "book.yaml").is_file()
        assert not (memory_root(tmp_path) / namespace_dir(stable)).exists()
        assert load_route_map(route_state_file(tmp_path, MATRIX)) == {elsewhere: "mx_room:elsewhere", target: MX_REAL}
        assert (report.backup_dir / route_state_file(tmp_path, MATRIX).name).read_text(encoding="utf-8") == mx_routes
        # 源 bot 还在那个群里的话，之后说的话照常落在源会话名下。
        assert route_state_file(tmp_path, QQ_NAPCAT).read_text(encoding="utf-8") == qq_routes
        assert (_session(tmp_path)["conversation_key"], _session(tmp_path)["channel"]) == (target, "mx_room")
        assert _journal_enabled(tmp_path, target, MX_BOT)
        assert not _journal_enabled(tmp_path, stable, QQ_BOT)
        assert report.moved_dirs == [(namespace_dir(stable), namespace_dir(target))]
        assert (report.backup_dir / namespace_dir(stable) / "book.yaml").is_file()
        assert (report.backup_dir / "data" / "sessions.json").is_file()

    def test_legacy_unscoped_data_ends_up_under_the_target_account(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path, account="")
        _target_ready(tmp_path)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (report.source.stable, report.source.account) == (stable, "")
        assert _journal_enabled(tmp_path, _mx_stable(), MX_BOT)

    def test_legacy_unscoped_data_can_be_named_with_an_empty_account(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path, account="")
        route_state_file(tmp_path, QQ_NAPCAT).unlink()
        _target_ready(tmp_path)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, source_account="")

        assert (report.source.stable, report.source.account) == (stable, "")

    def test_a_target_that_only_has_memory_on_its_side_keeps_it(self, tmp_path: Path) -> None:
        _seed(tmp_path, memory=False)
        _target_ready(tmp_path)
        own = memory_root(tmp_path) / namespace_dir(_mx_stable())
        own.mkdir(parents=True)
        (own / "mine.yaml").write_text("x: 1\n", encoding="utf-8")

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (own / "mine.yaml").read_text(encoding="utf-8") == "x: 1\n"
        assert report.moved_dirs == []
        assert _session(tmp_path)["conversation_key"] == _mx_stable()

    def test_the_source_account_is_the_one_its_key_was_derived_under(self, tmp_path: Path) -> None:
        # 换号以后记下的是新号，这个群的数据还在旧号名下。
        stable = _seed(tmp_path)
        ActiveAccountStore(QQ_NAPCAT, tmp_path).save(QQ_OTHER_BOT)
        _target_ready(tmp_path)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (report.source.stable, report.source.account) == (stable, QQ_BOT)
        assert _journal_enabled(tmp_path, _mx_stable(), MX_BOT)

    def test_a_conversation_under_two_accounts_needs_a_pick(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        _add_route(route_state_file(tmp_path, QQ_NAPCAT), _qq_stable(account=QQ_OTHER_BOT), QQ_REAL)
        ActiveAccountStore(QQ_NAPCAT, tmp_path).save(QQ_OTHER_BOT)
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match=f"{QQ_BOT}、{QQ_OTHER_BOT}|{QQ_OTHER_BOT}、{QQ_BOT}"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, source_account=QQ_BOT)
        assert (report.source.stable, report.source.account) == (_qq_stable(), QQ_BOT)

    def test_a_conversation_missing_from_the_routes_is_named_by_account(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path)
        route_state_file(tmp_path, QQ_NAPCAT).unlink()
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match="--from-account"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        report = bind.run_binding(
            workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, source_account=QQ_BOT, apply=True,
        )
        assert report.moved_dirs == [(namespace_dir(stable), namespace_dir(_mx_stable()))]

    def test_a_key_from_an_account_nobody_remembers_is_named_by_account(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path, account="30003")
        ActiveAccountStore(QQ_NAPCAT, tmp_path).path.unlink()
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match="认不出"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, source_account="30003")
        assert report.source.stable == stable

    def test_route_entries_filed_under_another_channel_are_ignored(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path)
        _add_route(route_state_file(tmp_path, QQ_NAPCAT), "qq_private:" + stable.split(":", 1)[1], QQ_REAL)
        _target_ready(tmp_path)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        assert report.source.stable == stable

    def test_the_target_account_can_be_named_before_the_target_bot_logs_in(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path, account="")

        with pytest.raises(bind.BindingRefused, match="还没记下账号"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL)

        report = bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, target_account="mxbot2")
        assert (report.target.stable, report.target.account) == (_mx_stable(account="mxbot2"), "mxbot2")

    def test_a_second_run_finds_nothing_left_to_bind(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)
        bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        with pytest.raises(bind.BindingRefused, match="没有可以绑的"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)


class TestRefusals:
    @pytest.mark.parametrize("source, target, message", [
        ("tg_group:1", MX_REAL, "认不出是哪个平台"),
        ("qq_forward:1", MX_REAL, "不是群聊或私聊"),
        (QQ_REAL, "mx_unknown:1", "不是群聊或私聊"),
        (QQ_REAL, "mx_room:", "认不出是哪个平台"),
        (QQ_REAL, QQ_REAL, "同一个会话"),
    ])
    def test_keys_that_are_not_two_conversations_are_refused(
        self, tmp_path: Path, source: str, target: str, message: str,
    ) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match=message):
            bind.run_binding(workspace=tmp_path, source_real=source, target_real=target, apply=True)

        assert (memory_root(tmp_path) / namespace_dir(_qq_stable())).is_dir()

    def test_a_channel_whose_keys_are_not_digested_is_refused(self, tmp_path: Path) -> None:
        raw = PlatformDescriptor(
            id="rawchat", display_name="Raw", short_name="R",
            channels=(ChannelDescriptor("raw_room", CHANNEL_GROUP, hashed=False), ChannelDescriptor("raw_other", CHANNEL_OTHER)),
        )
        _seed(tmp_path)
        register_platform(raw)
        try:
            with pytest.raises(bind.BindingRefused, match="不经过摘要"):
                bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real="raw_room:1", apply=True)
        finally:
            unregister_platform(raw.id)

    def test_a_group_is_never_bound_to_a_private_chat(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match="群聊.*私聊"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real="mx_dm:alice", apply=True)

    def test_a_target_platform_that_never_started_is_refused(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path, secret=False)

        with pytest.raises(bind.BindingRefused, match="启动一次目标 bot"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (memory_root(tmp_path) / namespace_dir(_qq_stable())).is_dir()

    def test_a_source_platform_that_never_started_is_refused(self, tmp_path: Path) -> None:
        _seed(tmp_path)

        with pytest.raises(bind.BindingRefused, match="还没有会话数据可绑"):
            bind.run_binding(workspace=tmp_path, source_real=MX_REAL, target_real=QQ_REAL)

    @pytest.mark.parametrize("source_account, target_account, message", [
        (None, "", "不能是空的"),
        (None, "bad account", "不是 Matrix 的账号"),
        ("abc", None, "不是 QQ · NapCat 的账号"),
    ])
    def test_empty_or_malformed_accounts_are_refused(
        self, tmp_path: Path, source_account: str | None, target_account: str | None, message: str,
    ) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match=message):
            bind.run_binding(
                workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL,
                source_account=source_account, target_account=target_account, apply=True,
            )

    def test_memory_on_both_sides_is_refused(self, tmp_path: Path) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path)
        (memory_root(tmp_path) / namespace_dir(_mx_stable())).mkdir(parents=True)

        with pytest.raises(bind.BindingRefused, match="两边都有长期记忆"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (memory_root(tmp_path) / namespace_dir(stable) / "book.yaml").is_file()
        assert _session(tmp_path)["conversation_key"] == stable

    def test_a_source_with_nothing_to_bind_is_refused(self, tmp_path: Path) -> None:
        _seed(tmp_path, memory=False, features=False)
        _target_ready(tmp_path)

        with pytest.raises(bind.BindingRefused, match="没有可以绑的"):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert not route_state_file(tmp_path, MATRIX).exists()

    @pytest.mark.parametrize("routes", [
        json.dumps({"real_conversation_keys": {"__TARGET__": "mx_room:elsewhere"}}),
        "{not json",
    ])
    def test_a_target_route_state_it_cannot_extend_stops_everything(self, tmp_path: Path, routes: str) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path)
        path = route_state_file(tmp_path, MATRIX)
        path.write_text(routes.replace("__TARGET__", _mx_stable()), encoding="utf-8")

        with pytest.raises(bind.BindingRefused):
            bind.run_binding(workspace=tmp_path, source_real=QQ_REAL, target_real=MX_REAL, apply=True)

        assert (memory_root(tmp_path) / namespace_dir(stable) / "book.yaml").is_file()
        assert _session(tmp_path)["conversation_key"] == stable
        assert not list(tmp_path.glob("data/migration_backup_*"))


class TestCommandLine:
    def test_the_listing_shows_each_platform_without_its_secret(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path)

        assert bind.main(["conversations", "--workspace", str(tmp_path)]) == 0

        out = capsys.readouterr().out
        assert f"{QQ_REAL}  账号 {QQ_BOT}  有长期记忆  {stable}" in out
        assert "Matrix（matrix）：当前账号 mxbot" in out
        assert "路由表里还没有会话" in out
        assert "已钉住的文件" in out
        assert QQ_SECRET not in out and MX_SECRET not in out

    def test_the_listing_can_look_at_one_platform(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        assert bind.main(["conversations", "--platform", "matrix", "--workspace", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "Matrix（matrix）" in out and QQ_REAL not in out
        assert bind.main(["conversations", "--platform", "nowhere", "--workspace", str(tmp_path)]) == 2

    def test_a_platform_without_a_declared_secret_is_listed_with_the_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        bare = PlatformDescriptor(
            id="bare", display_name="Bare", short_name="B", channels=(ChannelDescriptor("bare_other", CHANNEL_OTHER),),
        )
        register_platform(bare)
        try:
            assert bind.main(["conversations", "--platform", "bare", "--workspace", str(tmp_path)]) == 0
        finally:
            unregister_platform(bare.id)
        assert "没有声明会话键摘要密钥" in capsys.readouterr().out

    def test_a_preview_exits_0_and_says_how_to_run_it(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        code = bind.main(["conversation", "--from", QQ_REAL, "--to", MX_REAL, "--workspace", str(tmp_path)])

        out = capsys.readouterr().out
        assert code == 0
        assert "[DRY-RUN]" in out and "将补上" in out and "在 bot 与 supervisor 都停止的状态下运行本脚本" in out
        assert "--to-account 一致" not in out

    def test_a_named_target_account_comes_with_a_warning(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path, account="")

        code = bind.main([
            "conversation", "--from", QQ_REAL, "--to", MX_REAL, "--to-account", "mxbot2", "--apply",
            "--workspace", str(tmp_path),
        ])

        out = capsys.readouterr().out
        assert code == 0
        assert "[APPLY]" in out and "已补上" in out and "备份目录" in out and "--to-account 一致" in out

    def test_an_unexpected_failure_exits_1_and_points_at_the_backup(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(tmp_path)
        _target_ready(tmp_path)

        def broken_move(**_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(bind, "move_memory", broken_move)

        code = bind.main(["conversation", "--from", QQ_REAL, "--to", MX_REAL, "--apply", "--workspace", str(tmp_path)])

        assert code == 1
        assert "从备份恢复" in capsys.readouterr().err

    def test_a_refusal_exits_2_and_touches_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        stable = _seed(tmp_path)
        _target_ready(tmp_path, secret=False)

        code = bind.main(["conversation", "--from", QQ_REAL, "--to", MX_REAL, "--apply", "--workspace", str(tmp_path)])

        assert code == 2
        assert "未改动任何文件" in capsys.readouterr().err
        assert (memory_root(tmp_path) / namespace_dir(stable)).is_dir()
