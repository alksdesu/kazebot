"""平台注册表，以及核心层按注册表认平台的那几处。

接第二个平台时核心层一行不改：用一个只存在于测试里的平台跑一遍，
群功能、账号归属、提醒署名、发起会话都要认得它。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_BRIDGE,
    CHANNEL_GROUP,
    CHANNEL_PRIVATE,
    HOOK_ACCOUNT_OPS,
    HOOK_CONSOLE_SCHEMA,
    HOOK_KNOWN_ACCOUNTS,
    HOOK_MAINTENANCE,
    HOOK_TRIGGER_POLICY,
    STATE_ALIASES,
    STATE_KINDS,
    STATE_LIVE,
    STATE_ROUTES,
    ChannelDescriptor,
    InstancePort,
    PlatformDescriptor,
)
from platforms.registry import (  # noqa: E402
    DEFAULT_PLATFORM_ID,
    PLATFORM_ENV,
    STANDARD_STATE_FILES,
    MissingPlatformHook,
    UnknownPlatform,
    alias_numbering_path,
    all_platforms,
    conversation_channel,
    current_platform,
    current_platform_id,
    features_module,
    find_platform,
    get_platform,
    is_platform_conversation,
    platform_config_path,
    platform_named,
    platform_data_dir,
    platform_hook,
    platform_profiles_path,
    platform_secret_file,
    platform_state_path,
    register_platform,
    required_config_path,
    split_conversation_key,
    stable_key_pattern,
    unregister_platform,
)

TELEGRAM = PlatformDescriptor(
    id="telegram",
    display_name="Telegram",
    short_name="TG",
    channels=(
        ChannelDescriptor("tg_group", CHANNEL_GROUP),
        ChannelDescriptor("tg_private", CHANNEL_PRIVATE),
    ),
)
_TG_GROUP = "tg_group:" + "a" * 24
_TG_PRIVATE = "tg_private:" + "b" * 24
_QQ_GROUP = "qq_group:" + "c" * 24


@pytest.fixture
def telegram():
    register_platform(TELEGRAM)
    try:
        yield TELEGRAM
    finally:
        unregister_platform(TELEGRAM.id)


@pytest.fixture
def broken_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """一个导入就抛 KeyError 的钩子模块，模拟钩子自己读配置读炸了。"""
    name = "clonoth_broken_hook_probe"
    (tmp_path / f"{name}.py").write_text("raise KeyError('napcat config')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield name
    finally:
        sys.modules.pop(name, None)


class TestLookups:
    def test_the_builtin_qq_platform_is_registered(self) -> None:
        assert get_platform("qq") is QQ_NAPCAT
        assert QQ_NAPCAT in all_platforms()

    @pytest.mark.parametrize("name", ["qq", "onebot", "onebot11", "qq_group", "qq_private", "qq_forward", "qq_unknown"])
    def test_every_name_of_qq_finds_it(self, name: str) -> None:
        assert find_platform(name) is QQ_NAPCAT

    def test_unknown_names_find_nothing(self) -> None:
        assert find_platform("telegram") is None
        assert find_platform("") is None
        with pytest.raises(KeyError):
            get_platform("telegram")

    @pytest.mark.parametrize("key,expected", [
        ("qq_group:abc", ("qq_group", "abc")),
        ("  QQ_Group:abc ", ("qq_group", "abc")),
        ("qq_group:", ("", "")),
        ("conv_abc", ("", "")),
        ("", ("", "")),
    ])
    def test_splitting_a_conversation_key(self, key: str, expected: tuple[str, str]) -> None:
        assert split_conversation_key(key) == expected

    def test_a_conversation_key_resolves_to_its_channel(self) -> None:
        platform, channel = conversation_channel(_QQ_GROUP)
        assert platform is QQ_NAPCAT and channel.kind == CHANNEL_GROUP

    @pytest.mark.parametrize("key,expected", [
        (_QQ_GROUP, True),
        ("qq_private:x", True),
        ("qq_forward:group:1", False),
        ("qq_unknown:x", False),
        ("web:x", False),
        ("qq_group:", False),
    ])
    def test_only_group_and_private_chats_are_conversations(self, key: str, expected: bool) -> None:
        assert is_platform_conversation(key) is expected

    def test_bridge_channels_can_be_asked_for_explicitly(self) -> None:
        assert is_platform_conversation("qq_forward:group:1", kinds={CHANNEL_BRIDGE})

    def test_the_stable_key_pattern_covers_hashed_conversation_channels(self) -> None:
        pattern = stable_key_pattern()
        assert pattern is not None
        assert pattern.search(f"agent:x:{_QQ_GROUP}:uuid").group(0) == _QQ_GROUP
        assert pattern.search("agent:x:qq_forward:" + "c" * 24) is None


class TestTheInstancePlatform:
    """一个实例只跑一个平台，运行期处处认 CLONOTH_PLATFORM 这一个值。"""

    @pytest.mark.parametrize("name", ["qq", "  QQ  ", "onebot", "onebot11"])
    def test_a_platform_id_or_a_historical_alias_names_it(self, name: str) -> None:
        assert platform_named(name) is QQ_NAPCAT

    @pytest.mark.parametrize("name", ["qq_group", "qq_private", "qq_forward", "qq_unknown"])
    def test_a_conversation_channel_is_not_a_platform_name(self, name: str) -> None:
        # find_platform 认通道名，这里不能跟着认：清单和 .env 里写的是平台 id。
        assert find_platform(name) is QQ_NAPCAT
        with pytest.raises(UnknownPlatform):
            platform_named(name)

    @pytest.mark.parametrize("name", ["nowhere", "", "   ", None, "telegram"])
    def test_a_name_nobody_registered_is_refused(self, name: object) -> None:
        with pytest.raises(UnknownPlatform):
            platform_named(name)

    def test_the_refusal_lists_what_it_does_know(self) -> None:
        with pytest.raises(UnknownPlatform, match="qq"):
            platform_named("nowhere")

    def test_a_platform_registered_later_is_nameable(self, telegram: PlatformDescriptor) -> None:
        assert platform_named("TELEGRAM") is telegram
        with pytest.raises(UnknownPlatform):
            platform_named("tg_group")

    def test_an_instance_that_does_not_say_runs_qq(self) -> None:
        # 改造前的实例 .env 里没有这一行，那时候能跑的只有 QQ。
        assert current_platform_id({}) == DEFAULT_PLATFORM_ID == "qq"
        assert current_platform({}) is QQ_NAPCAT

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_a_blank_declaration_is_the_same_as_not_saying(self, raw: str) -> None:
        assert current_platform({PLATFORM_ENV: raw}) is QQ_NAPCAT

    @pytest.mark.parametrize("raw", ["  QQ  ", "onebot", "onebot11"])
    def test_a_declaration_goes_through_the_same_naming(self, raw: str) -> None:
        assert current_platform_id({PLATFORM_ENV: raw}) == raw.strip().lower()
        assert current_platform({PLATFORM_ENV: raw}) is QQ_NAPCAT

    def test_the_declared_platform_is_the_one_registered_under_that_id(
        self, telegram: PlatformDescriptor,
    ) -> None:
        assert current_platform({PLATFORM_ENV: "telegram"}) is telegram

    @pytest.mark.parametrize("raw", ["nowhere", "qq_group", "telegram"])
    def test_a_declaration_nobody_registered_is_refused(self, raw: str) -> None:
        # 带着一个不存在的平台跑下去，会话、记忆和维护都会落到一个没人认的 id 下。
        assert current_platform_id({PLATFORM_ENV: raw}) == raw
        with pytest.raises(UnknownPlatform):
            current_platform({PLATFORM_ENV: raw})

    def test_without_an_environ_it_reads_the_process_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PLATFORM_ENV, raising=False)
        assert current_platform() is QQ_NAPCAT

        monkeypatch.setenv(PLATFORM_ENV, "nowhere")
        assert current_platform_id() == "nowhere"
        with pytest.raises(UnknownPlatform):
            current_platform()

    def test_it_is_a_lookup_error(self) -> None:
        # 老的按 LookupError/KeyError 兜的调用方不能因为换了类型漏掉。
        assert issubclass(UnknownPlatform, LookupError)


class TestRegistration:
    def test_a_registered_platform_is_found_by_all_its_names(self, telegram: PlatformDescriptor) -> None:
        assert find_platform("tg_group") is telegram
        assert get_platform("TELEGRAM") is telegram

    def test_unregistering_forgets_every_name(self) -> None:
        register_platform(TELEGRAM)
        unregister_platform("telegram")
        assert find_platform("tg_group") is None

    def test_a_name_claimed_by_another_platform_is_refused(self) -> None:
        clash = PlatformDescriptor(
            id="fake", display_name="Fake", short_name="F",
            channels=(ChannelDescriptor("qq_group", CHANNEL_GROUP),),
        )
        with pytest.raises(ValueError, match="qq_group"):
            register_platform(clash)
        assert find_platform("qq_group") is QQ_NAPCAT

    def test_registering_twice_needs_replace(self, telegram: PlatformDescriptor) -> None:
        with pytest.raises(ValueError):
            register_platform(telegram)
        register_platform(telegram, replace=True)

    def test_descriptors_reject_malformed_channels(self) -> None:
        with pytest.raises(ValueError):
            ChannelDescriptor("tg_group", "chatroom")
        with pytest.raises(ValueError):
            PlatformDescriptor(
                id="x", display_name="X", short_name="X",
                channels=(ChannelDescriptor("x", CHANNEL_GROUP), ChannelDescriptor("x", CHANNEL_PRIVATE)),
            )

    @pytest.mark.parametrize("platform_id", ["QQ", "qq-official", "../qq", "", "1qq"])
    def test_platform_ids_are_lower_snake_case(self, platform_id: str) -> None:
        # id 是 data/platforms/ 下的目录名，也是 data/config.yaml 里的键。
        with pytest.raises(ValueError):
            PlatformDescriptor(id=platform_id, display_name="X", short_name="X")

    def test_descriptors_reject_malformed_state_declarations(self) -> None:
        base = {"id": "x", "display_name": "X", "short_name": "X"}
        with pytest.raises(ValueError, match="unknown state"):
            PlatformDescriptor(**base, state_files={"cache": "x_cache.json"})
        with pytest.raises(ValueError):
            PlatformDescriptor(**base, state_files={STATE_LIVE: "../x_live.json"})
        with pytest.raises(ValueError, match="standard place"):
            PlatformDescriptor(**base, state_env={STATE_LIVE: "X_LIVE_FILE"})
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            PlatformDescriptor(**base, state_files={STATE_LIVE: "x_live.json"}, state_env={STATE_LIVE: "x_live"})

    @pytest.mark.parametrize("what", ["config", "profiles"])
    def test_descriptors_reject_malformed_movable_files(self, what: str) -> None:
        # 保护规则把这两个路径原样当 glob 用，写歪了等于没护住。
        base = {"id": "x", "display_name": "X", "short_name": "X"}
        with pytest.raises(ValueError):
            PlatformDescriptor(**base, **{f"{what}_path": "../x.yaml"})
        with pytest.raises(ValueError):
            PlatformDescriptor(**base, **{f"{what}_path": "/etc/x.yaml"})
        with pytest.raises(ValueError, match="does not have"):
            PlatformDescriptor(**base, **{f"{what}_env": "X_PATH"})
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            PlatformDescriptor(**base, **{f"{what}_path": "config/x.yaml", f"{what}_env": "x_path"})

    def test_the_public_view_leaves_out_deployment_details(self) -> None:
        public = QQ_NAPCAT.public_dict()
        assert public["id"] == "qq" and public["has_config"] is True
        assert "config_env" not in public and "adapters._shared.trigger_policy" not in json.dumps(public)
        assert "state_env" not in public and "ONEBOT_STATE_FILE" not in json.dumps(public)
        # 端口是部署细节，控制台按不上用场，露出去只是多告诉人一件事。
        assert "instance_ports" not in public and "8080" not in json.dumps(public)

    def test_a_platform_names_the_process_it_connects_to(self) -> None:
        assert QQ_NAPCAT.connection_peer == "NapCat"
        assert PlatformDescriptor(id="plain", display_name="Plain", short_name="P").connection_peer == ""

    def test_the_public_view_carries_the_connection_card(self) -> None:
        # 控制台照这三项提示人去服务器上查哪个进程、它该连着谁，原先 bot.py 是写死在文案里的。
        public = json.loads(json.dumps(QQ_NAPCAT.public_dict()))

        assert public["entrypoints"] == ["bot.py"]
        assert public["connection_peer"] == "NapCat"
        assert public["connection_hint"] == "检查 NapCat 登录与反向 WebSocket 地址"
        assert isinstance(QQ_NAPCAT.public_dict()["entrypoints"], list)

    def test_the_public_view_hands_out_its_own_list(self) -> None:
        # 接口每次调用都拿它去序列化，共用一份的话谁改了都写回声明里。
        public = QQ_NAPCAT.public_dict()
        public["entrypoints"].append("evil.py")

        assert QQ_NAPCAT.public_dict()["entrypoints"] == ["bot.py"]
        assert QQ_NAPCAT.entrypoints == ("bot.py",)

    def test_a_platform_that_declares_no_connection_card_says_so(self) -> None:
        public = PlatformDescriptor(id="plain", display_name="Plain", short_name="P").public_dict()

        assert (public["entrypoints"], public["connection_peer"], public["connection_hint"]) == ([], "", "")


class TestInstancePorts:
    def test_a_port_steps_once_per_index(self) -> None:
        port = InstancePort("bot", 8080, 10)
        assert (port.at(0), port.at(1), port.at(8)) == (8080, 8090, 8160)

    @pytest.mark.parametrize("name", ["Bot", "bot-2", "_bot", "2bot", "", "bot.web"])
    def test_port_names_are_lower_snake_case(self, name: str) -> None:
        # 端口名是 .env 里那几行的来源，也是清单和 shell 侧对账用的键。
        with pytest.raises(ValueError, match="lower_snake_case"):
            InstancePort(name, 8080, 10)

    @pytest.mark.parametrize("base,step", [(80, 10), (1024, 10), (65536, 10), (70000, 10), (8080, 0), (8080, -10)])
    def test_a_port_needs_an_unprivileged_base_and_a_forward_step(self, base: int, step: int) -> None:
        # 1024 以下要 root 才能监听，步长不前进的话所有实例都落在同一个号上。
        with pytest.raises(ValueError, match="positive step"):
            InstancePort("bot", base, step)

    def test_one_platform_cannot_claim_a_port_name_twice(self) -> None:
        with pytest.raises(ValueError, match="duplicate instance port names"):
            PlatformDescriptor(
                id="x", display_name="X", short_name="X",
                instance_ports=(InstancePort("bot", 8080, 10), InstancePort("bot", 9090, 10)),
            )

    def test_declared_ports_are_frozen_into_a_tuple(self) -> None:
        platform = PlatformDescriptor(
            id="x", display_name="X", short_name="X", instance_ports=[InstancePort("bot", 8080, 10)],
        )
        assert platform.instance_ports == (InstancePort("bot", 8080, 10),)

    def test_qq_keeps_the_single_instance_ports_at_index_zero(self) -> None:
        # 序号 0 必须等于改造前的单实例部署，已有部署才不用改端口。
        assert {port.name: port.at(0) for port in QQ_NAPCAT.instance_ports} == {
            "bot": 8080, "bridge": 8769, "napcat": 6099,
        }


class TestFilesAndHooks:
    def test_the_config_path_follows_the_descriptor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
        assert platform_config_path(QQ_NAPCAT, tmp_path) == tmp_path / "config" / "qq.yaml"

    def test_the_legacy_env_override_still_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_QQ_CONFIG_PATH", str(tmp_path / "elsewhere.yaml"))
        assert platform_config_path(QQ_NAPCAT, tmp_path) == tmp_path / "elsewhere.yaml"

    def test_a_relative_override_is_resolved_against_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 离线脚本不一定在工作区里跑，按 cwd 算会读到另一份。
        monkeypatch.setenv("CLONOTH_QQ_CONFIG_PATH", "config/alt.yaml")
        assert platform_config_path(QQ_NAPCAT, tmp_path) == tmp_path / "config" / "alt.yaml"

    def test_a_platform_without_config_says_so(self, tmp_path: Path, telegram: PlatformDescriptor) -> None:
        assert platform_config_path(telegram, tmp_path) is None
        with pytest.raises(LookupError):
            required_config_path(telegram, tmp_path)

    def test_qq_profiles_follow_the_descriptor_and_its_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLONOTH_QQ_USER_PROFILES_PATH", raising=False)
        assert platform_profiles_path(QQ_NAPCAT, tmp_path) == tmp_path / "config" / "qq_user_profiles.yaml"

        monkeypatch.setenv("CLONOTH_QQ_USER_PROFILES_PATH", "private/people.yaml")
        assert platform_profiles_path(QQ_NAPCAT, tmp_path) == tmp_path / "private" / "people.yaml"
        assert platform_profiles_path(QQ_NAPCAT, tmp_path, environ={}) == tmp_path / "config" / "qq_user_profiles.yaml"

    def test_a_platform_without_profiles_says_so(self, tmp_path: Path, telegram: PlatformDescriptor) -> None:
        assert platform_profiles_path(telegram, tmp_path) is None

    def test_the_alias_numbering_sits_beside_the_platform_directories(self, tmp_path: Path) -> None:
        path = alias_numbering_path(tmp_path)

        assert path == tmp_path / "data" / "platforms" / "alias_numbering.json"
        # 平台 id 不带点，不会有哪个平台的数据目录叫这个名字。
        with pytest.raises(ValueError):
            PlatformDescriptor(id=path.name, display_name="X", short_name="X")

    def test_qq_keeps_its_legacy_state_file_names(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ONEBOT_STATE_FILE", raising=False)
        assert platform_state_path(QQ_NAPCAT, STATE_LIVE, tmp_path) == tmp_path / "data" / "qq_live_state.json"
        assert platform_state_path(QQ_NAPCAT, STATE_ROUTES, tmp_path) == tmp_path / "data" / "onebot_plugin_state.json"

    def test_a_new_platform_keeps_its_state_in_its_own_directory(
        self, tmp_path: Path, telegram: PlatformDescriptor,
    ) -> None:
        home = tmp_path / "data" / "platforms" / "telegram"

        assert platform_data_dir(telegram, tmp_path) == home
        assert {kind: platform_state_path(telegram, kind, tmp_path) for kind in STATE_KINDS} == {
            kind: home / name for kind, name in STANDARD_STATE_FILES.items()
        }

    def test_every_state_kind_has_its_own_standard_file_name(self) -> None:
        assert set(STANDARD_STATE_FILES) == STATE_KINDS
        assert len(set(STANDARD_STATE_FILES.values())) == len(STANDARD_STATE_FILES)

    def test_an_unknown_state_kind_is_a_programming_error(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError):
            platform_state_path(QQ_NAPCAT, "nothing", tmp_path)

    def test_legacy_state_env_overrides_move_the_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # bot 认这两个变量；supervisor 和迁移脚本不认的话，设过它们的部署会读错文件。
        moved = tmp_path / "elsewhere" / "routes.json"
        monkeypatch.setenv("ONEBOT_STATE_FILE", str(moved))
        monkeypatch.setenv("ONEBOT_ANON_MAP_FILE", "private/aliases.json")

        assert platform_state_path(QQ_NAPCAT, STATE_ROUTES, tmp_path) == moved
        assert platform_state_path(QQ_NAPCAT, STATE_ALIASES, tmp_path) == tmp_path / "private" / "aliases.json"

    def test_an_explicit_environ_stands_in_for_the_process_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ONEBOT_STATE_FILE", str(tmp_path / "process.json"))
        default = tmp_path / "data" / "onebot_plugin_state.json"

        assert platform_state_path(QQ_NAPCAT, STATE_ROUTES, tmp_path, environ={}) == default
        assert platform_state_path(
            QQ_NAPCAT, STATE_ROUTES, tmp_path, environ={"ONEBOT_STATE_FILE": "x.json"},
        ) == tmp_path / "x.json"

    def test_generated_secret_files_follow_the_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", raising=False)
        default = tmp_path / "data" / "onebot_conversation_hash_secret"
        assert platform_secret_file(QQ_NAPCAT, "conversation_hash_secret", tmp_path) == default

        monkeypatch.setenv("ONEBOT_CONVERSATION_HASH_SECRET_FILE", "secrets/hash")
        assert platform_secret_file(QQ_NAPCAT, "conversation_hash_secret", tmp_path) == tmp_path / "secrets" / "hash"
        # 不自动生成的密钥给 None，不猜一个位置。
        assert platform_secret_file(QQ_NAPCAT, "access_token", tmp_path) is None
        with pytest.raises(KeyError):
            platform_secret_file(QQ_NAPCAT, "nothing", tmp_path)

    def test_the_trigger_hook_is_the_shared_module(self) -> None:
        from adapters._shared import trigger_policy

        assert platform_hook(QQ_NAPCAT, HOOK_TRIGGER_POLICY) is trigger_policy

    def test_a_missing_hook_is_a_lookup_error(self, telegram: PlatformDescriptor) -> None:
        # 独立的类型，但仍是 LookupError：照着老写法接的调用方不用跟着改。
        assert issubclass(MissingPlatformHook, LookupError)
        with pytest.raises(MissingPlatformHook):
            platform_hook(telegram, HOOK_TRIGGER_POLICY)
        with pytest.raises(LookupError):
            platform_hook(telegram, HOOK_TRIGGER_POLICY)

    def test_a_hook_module_that_breaks_on_import_is_not_a_missing_hook(self, broken_hook: str) -> None:
        """钩子模块自己抛的 KeyError 也是 LookupError，笼统地接住就把坏模块当成了没声明。"""
        platform = PlatformDescriptor(
            id="brokenhook", display_name="坏钩子", short_name="BH",
            channels=(ChannelDescriptor("bh_group", CHANNEL_GROUP),),
            hooks={HOOK_TRIGGER_POLICY: broken_hook},
        )

        with pytest.raises(KeyError) as caught:
            platform_hook(platform, HOOK_TRIGGER_POLICY)
        assert not isinstance(caught.value, MissingPlatformHook)

    def test_the_maintenance_hook_is_the_qq_module(self) -> None:
        from adapters.onebot import maintenance

        assert platform_hook(QQ_NAPCAT, HOOK_MAINTENANCE) is maintenance

    def test_the_console_hook_is_the_qq_layout(self) -> None:
        from adapters.onebot import console_layout

        assert platform_hook(QQ_NAPCAT, HOOK_CONSOLE_SCHEMA) is console_layout

    def test_the_account_hook_reads_the_napcat_config_names(self, tmp_path: Path) -> None:
        from adapters.onebot import accounts

        config = tmp_path / "napcat" / "config"
        config.mkdir(parents=True)
        for name in ("onebot11_20002.json", "onebot11_10001.json", "onebot11_webui.json", "webui.json"):
            (config / name).write_text("{}", encoding="utf-8")

        assert platform_hook(QQ_NAPCAT, HOOK_KNOWN_ACCOUNTS) is accounts
        assert accounts.known_accounts(tmp_path) == ["10001", "20002"]
        assert accounts.known_accounts(tmp_path / "elsewhere") == []

    def test_the_account_ops_hook_is_the_qq_implementation(self) -> None:
        from adapters.onebot import account_ops

        assert platform_hook(QQ_NAPCAT, HOOK_ACCOUNT_OPS) is account_ops
        assert callable(account_ops.account_ops)

    def test_the_shared_features_load_through_the_registry(self) -> None:
        from adapters._shared import features

        assert features_module() is features


class TestCoreRecognisesANewPlatform:
    def test_schedules_bind_to_the_originating_chat(self, telegram: PlatformDescriptor) -> None:
        from toolbox.builtins.schedules import _originating_conversation_key

        ctx = SimpleNamespace(_route_conversation_key="", _parent_conversation_key="", conversation_key=_TG_GROUP)
        assert _originating_conversation_key(ctx) == _TG_GROUP

    def test_group_roles_apply_in_its_group_chats(self, telegram: PlatformDescriptor) -> None:
        from supervisor.feature_auth import _is_group_scope

        assert _is_group_scope("tg_group", _TG_GROUP) is True
        assert _is_group_scope("tg_private", _TG_GROUP) is False
        assert _is_group_scope("tg_private", _TG_PRIVATE) is False

    def test_group_features_accept_its_groups(self, telegram: PlatformDescriptor) -> None:
        from supervisor.community.service import CommunityService

        actor = SimpleNamespace(scope=_TG_GROUP, require_scope=lambda scope: None)
        assert CommunityService._group_scope(actor) == _TG_GROUP
        assert CommunityService._inherits_defaults(_TG_PRIVATE) is True
        with pytest.raises(ValueError, match="群聊"):
            CommunityService._group_scope(SimpleNamespace(scope=_TG_PRIVATE, require_scope=lambda scope: None))

    def test_reminders_sign_with_its_short_name(self, telegram: PlatformDescriptor) -> None:
        from supervisor.feature_auth import FeatureActor
        from supervisor.reminders.service import _owner_label

        assert _owner_label(FeatureActor(channel="tg_group", user_id="7")) == "TG 7"
        assert _owner_label(FeatureActor(channel="qq_group", user_id="7")) == "QQ 7"
        assert _owner_label(FeatureActor(channel="web", is_admin=True)) == "控制台管理员"

    def test_account_scope_follows_the_platform_this_instance_runs(
        self, tmp_path: Path, telegram: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from supervisor.conversation_labels import account_scope

        monkeypatch.setenv(PLATFORM_ENV, telegram.id)
        live = platform_state_path(telegram, STATE_LIVE, tmp_path)
        live.parent.mkdir(parents=True)
        live.write_text(json.dumps({"scope_conversation_keys": [_TG_GROUP]}), encoding="utf-8")
        accounts = account_scope(tmp_path)

        assert accounts.platform == telegram.id
        assert accounts.is_current(_TG_GROUP) is True
        assert accounts.is_current(_TG_PRIVATE) is False
        # QQ 的会话是换平台之前留下的，这个实例已经不跑它；内部入口的会话不参与归属。
        assert accounts.is_current(_QQ_GROUP) is False
        assert accounts.is_current("web:console") is True

    def test_a_platform_that_never_published_claims_all_of_its_own(
        self, tmp_path: Path, telegram: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 密钥只有 bot 进程有，没公布过就无从判断，一律算当前；别的平台的照样不算。
        from supervisor.conversation_labels import account_scope

        monkeypatch.setenv(PLATFORM_ENV, telegram.id)
        accounts = account_scope(tmp_path)

        assert accounts.keys is None
        assert accounts.is_current(_TG_GROUP) is True
        assert accounts.is_current(_TG_PRIVATE) is True
        assert accounts.is_current(_QQ_GROUP) is False

    def test_the_other_platform_left_behind_state_does_not_count(
        self, tmp_path: Path, telegram: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 换过平台的实例盘上还留着旧平台的快照，读它等于拿早已不跑的 bot 说的话当真。
        from supervisor.conversation_labels import account_scope

        monkeypatch.delenv(PLATFORM_ENV, raising=False)
        live = platform_state_path(telegram, STATE_LIVE, tmp_path)
        live.parent.mkdir(parents=True)
        live.write_text(json.dumps({"scope_conversation_keys": [_TG_GROUP]}), encoding="utf-8")
        accounts = account_scope(tmp_path)

        assert accounts.platform == "qq" and accounts.keys is None
        assert accounts.is_current(_TG_GROUP) is False
        assert accounts.is_current(_QQ_GROUP) is True

    def test_memory_directories_are_labelled_from_its_own_state(self, tmp_path: Path, telegram: PlatformDescriptor) -> None:
        from engine.memory_namespace import conversation_namespace
        from supervisor.conversation_labels import describe_namespaces

        platform_data_dir(telegram, tmp_path).mkdir(parents=True)
        platform_state_path(telegram, STATE_ROUTES, tmp_path).write_text(
            json.dumps({"real_conversation_keys": {_TG_GROUP: "tg_group:-100123"}}), encoding="utf-8",
        )
        platform_state_path(telegram, STATE_LIVE, tmp_path).write_text(
            json.dumps({"group_names": {"-100123": "猫猫群"}}), encoding="utf-8",
        )

        labels = describe_namespaces(tmp_path)

        assert labels[conversation_namespace(_TG_GROUP)]["label"] == "猫猫群"
        assert labels[conversation_namespace(_TG_GROUP)]["kind"] == "group"

    def test_aliases_keyed_by_bot_account_still_name_its_private_chats(
        self, tmp_path: Path, telegram: PlatformDescriptor,
    ) -> None:
        from engine.memory_namespace import conversation_namespace
        from supervisor.conversation_labels import describe_namespaces

        platform_data_dir(telegram, tmp_path).mkdir(parents=True)
        platform_state_path(telegram, STATE_ROUTES, tmp_path).write_text(
            json.dumps({"real_conversation_keys": {_TG_PRIVATE: "tg_private:555"}}), encoding="utf-8",
        )
        platform_state_path(telegram, STATE_ALIASES, tmp_path).write_text(
            json.dumps({"users": {"bot1/555": {"alias": "UserC", "last_seen": 1}}}), encoding="utf-8",
        )

        label = describe_namespaces(tmp_path)[conversation_namespace(_TG_PRIVATE)]

        assert label["alias"] == "UserC" and label["label"] == "UserC"

    def test_an_id_aliased_under_two_bot_accounts_gets_no_alias(
        self, tmp_path: Path, telegram: PlatformDescriptor,
    ) -> None:
        # 会话键里看不出是哪个号，挑一个就可能把别人的名字安到这段私聊上。
        from engine.memory_namespace import conversation_namespace
        from supervisor.conversation_labels import describe_namespaces

        platform_data_dir(telegram, tmp_path).mkdir(parents=True)
        platform_state_path(telegram, STATE_ROUTES, tmp_path).write_text(
            json.dumps({"real_conversation_keys": {_TG_PRIVATE: "tg_private:555"}}), encoding="utf-8",
        )
        platform_state_path(telegram, STATE_ALIASES, tmp_path).write_text(
            json.dumps({"users": {
                "bot1/555": {"alias": "UserC", "last_seen": 1},
                "bot2/555": {"alias": "UserD", "last_seen": 1},
            }}),
            encoding="utf-8",
        )

        label = describe_namespaces(tmp_path)[conversation_namespace(_TG_PRIVATE)]

        assert label["alias"] == "" and label["label"] == "私聊 555"


class TestAWrongPlatformStopsTheProcess:
    """实例声明的平台不认识时宁可不起：带着一个没人认的 id 跑下去，会话、记忆和维护全落到空处。"""

    @pytest.fixture
    def supervisor_main(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """把 main() 前面那几步换成不落盘、不监听的替身，只留平台校验和端口检查。"""
        from supervisor import main as supervisor_main_module

        monkeypatch.setattr(supervisor_main_module, "process_workspace", lambda: tmp_path)
        monkeypatch.setattr(supervisor_main_module, "configure_supervisor_logging", lambda *_a, **_k: "info")
        monkeypatch.setattr(sys, "argv", ["supervisor"])
        monkeypatch.delenv(PLATFORM_ENV, raising=False)
        return supervisor_main_module

    @pytest.fixture
    def bot_entry(self, monkeypatch: pytest.MonkeyPatch):
        """按入口文件加载 bot.py：它不是包的一部分，import 不到。"""
        import importlib.util

        import dotenv

        # 真加载的话测试机上的 .env 会灌进 os.environ，后面所有用例都跟着变。
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_k: False)
        spec = importlib.util.spec_from_file_location("clonoth_bot_entry_probe", _ROOT / "bot.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop("clonoth_bot_entry_probe", None)

    def test_the_exit_code_is_the_sysexits_config_error(self) -> None:
        # 单元文件的 RestartPreventExitStatus 按它写死，改一处就要连带改另一处。
        from workspace import CONFIG_ERROR_EXIT_CODE

        assert CONFIG_ERROR_EXIT_CODE == 78

    def test_the_supervisor_refuses_to_start_on_an_unknown_platform(
        self, supervisor_main, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from workspace import CONFIG_ERROR_EXIT_CODE

        monkeypatch.setenv(PLATFORM_ENV, "nowhere")

        with pytest.raises(SystemExit) as exited:
            supervisor_main.main()

        assert exited.value.code == CONFIG_ERROR_EXIT_CODE

    def test_the_supervisor_checks_the_platform_before_it_touches_the_port(
        self, supervisor_main, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 校验排在端口检查之后的话，一个写错平台的实例会先去抢正在跑的那个的端口。
        def taken(*_args: object, **_kwargs: object) -> None:
            raise OSError("address already in use")

        monkeypatch.setattr(
            supervisor_main, "socket",
            SimpleNamespace(socket=taken, AF_INET=0, SOCK_STREAM=0, SOL_SOCKET=0, SO_REUSEADDR=0),
        )
        monkeypatch.setenv(PLATFORM_ENV, "nowhere")
        with pytest.raises(SystemExit) as refused:
            supervisor_main.main()

        monkeypatch.setenv(PLATFORM_ENV, "onebot")
        with pytest.raises(SystemExit) as allowed:
            supervisor_main.main()

        assert refused.value.code == 78
        assert allowed.value.code == supervisor_main.PORT_IN_USE_EXIT_CODE

    def test_the_qq_adapter_refuses_an_unknown_platform(
        self, bot_entry, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from workspace import CONFIG_ERROR_EXIT_CODE

        monkeypatch.setenv(PLATFORM_ENV, "nowhere")

        with pytest.raises(SystemExit) as exited:
            bot_entry._require_qq_instance()

        assert exited.value.code == CONFIG_ERROR_EXIT_CODE

    def test_the_qq_adapter_refuses_an_instance_that_runs_another_platform(
        self, bot_entry, telegram: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 两个平台的进程挂在同一个工作区，会话和记忆会混成一份。
        monkeypatch.setenv(PLATFORM_ENV, telegram.id)

        with pytest.raises(SystemExit) as exited:
            bot_entry._require_qq_instance()

        assert exited.value.code == 78

    @pytest.mark.parametrize("declared", [None, "qq", "  QQ  ", "onebot", "onebot11"])
    def test_the_qq_adapter_starts_on_a_qq_instance(
        self, bot_entry, declared: str | None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if declared is None:
            monkeypatch.delenv(PLATFORM_ENV, raising=False)
        else:
            monkeypatch.setenv(PLATFORM_ENV, declared)

        assert bot_entry._require_qq_instance() is None

    def test_the_qq_adapter_checks_before_it_waits_for_the_supervisor(self) -> None:
        # 排在等 supervisor 之后的话，一个起不来的实例要先干等三分钟才说原因。
        import ast

        tree = ast.parse((_ROOT / "bot.py").read_text(encoding="utf-8"))
        body = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        ).body
        names = [
            node.value.func.id for node in body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ]

        assert names and names[0] == "_require_qq_instance"

    @pytest.mark.parametrize("unit,expected", [
        ("kazebot.service", "98 78"),
        ("kazebot@.service", "98 78"),
        ("kazebot-qq.service", "78"),
        ("kazebot-qq@.service", "78"),
    ])
    def test_the_units_do_not_restart_on_a_config_error(self, unit: str, expected: str) -> None:
        # 反复拉起一个起不来的进程，只会把日志刷满，真正的原因翻不到。
        body = (_ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")

        assert f"RestartPreventExitStatus={expected}\n" in body
