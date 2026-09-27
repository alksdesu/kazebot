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
    HOOK_CONSOLE_SCHEMA,
    HOOK_TRIGGER_POLICY,
    STATE_ALIASES,
    STATE_KINDS,
    STATE_LIVE,
    STATE_ROUTES,
    ChannelDescriptor,
    PlatformDescriptor,
)
from platforms.registry import (  # noqa: E402
    STANDARD_STATE_FILES,
    alias_numbering_path,
    all_platforms,
    conversation_channel,
    features_module,
    find_platform,
    get_platform,
    is_platform_conversation,
    platform_config_path,
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
        with pytest.raises(LookupError):
            platform_hook(telegram, HOOK_TRIGGER_POLICY)

    def test_the_console_hook_is_the_qq_layout(self) -> None:
        from adapters.onebot import console_layout

        assert platform_hook(QQ_NAPCAT, HOOK_CONSOLE_SCHEMA) is console_layout

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

    def test_account_scope_is_tracked_per_platform(self, tmp_path: Path, telegram: PlatformDescriptor) -> None:
        from supervisor.conversation_labels import account_scope

        live = platform_state_path(telegram, STATE_LIVE, tmp_path)
        live.parent.mkdir(parents=True)
        live.write_text(json.dumps({"scope_conversation_keys": [_TG_GROUP]}), encoding="utf-8")
        accounts = account_scope(tmp_path)

        assert accounts.is_current(_TG_GROUP) is True
        assert accounts.is_current(_TG_PRIVATE) is False
        # QQ 没公布过归属，无从判断就一律算当前；内部入口的会话不参与归属。
        assert accounts.is_current(_QQ_GROUP) is True
        assert accounts.is_current("web:console") is True

    def test_memory_directories_are_labelled_from_its_own_state(self, tmp_path: Path, telegram: PlatformDescriptor) -> None:
        from supervisor.conversation_labels import describe_namespaces, memory_namespace

        platform_data_dir(telegram, tmp_path).mkdir(parents=True)
        platform_state_path(telegram, STATE_ROUTES, tmp_path).write_text(
            json.dumps({"real_conversation_keys": {_TG_GROUP: "tg_group:-100123"}}), encoding="utf-8",
        )
        platform_state_path(telegram, STATE_LIVE, tmp_path).write_text(
            json.dumps({"group_names": {"-100123": "猫猫群"}}), encoding="utf-8",
        )

        labels = describe_namespaces(tmp_path)

        assert labels[memory_namespace(_TG_GROUP)]["label"] == "猫猫群"
        assert labels[memory_namespace(_TG_GROUP)]["kind"] == "group"

    def test_aliases_keyed_by_bot_account_still_name_its_private_chats(
        self, tmp_path: Path, telegram: PlatformDescriptor,
    ) -> None:
        from supervisor.conversation_labels import describe_namespaces, memory_namespace

        platform_data_dir(telegram, tmp_path).mkdir(parents=True)
        platform_state_path(telegram, STATE_ROUTES, tmp_path).write_text(
            json.dumps({"real_conversation_keys": {_TG_PRIVATE: "tg_private:555"}}), encoding="utf-8",
        )
        platform_state_path(telegram, STATE_ALIASES, tmp_path).write_text(
            json.dumps({"users": {"bot1/555": {"alias": "UserC", "last_seen": 1}}}), encoding="utf-8",
        )

        label = describe_namespaces(tmp_path)[memory_namespace(_TG_PRIVATE)]

        assert label["alias"] == "UserC" and label["label"] == "UserC"

    def test_an_id_aliased_under_two_bot_accounts_gets_no_alias(
        self, tmp_path: Path, telegram: PlatformDescriptor,
    ) -> None:
        # 会话键里看不出是哪个号，挑一个就可能把别人的名字安到这段私聊上。
        from supervisor.conversation_labels import describe_namespaces, memory_namespace

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

        label = describe_namespaces(tmp_path)[memory_namespace(_TG_PRIVATE)]

        assert label["alias"] == "" and label["label"] == "私聊 555"
