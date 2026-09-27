"""配置分层的验收：同一项在全局、平台、会话三层的覆盖结果，旧写法照旧读得到，控制台标的来源和实际生效的一致。

全局和平台是两份 yaml，窄层盖宽层、env 排在两份文件之后；会话层在 data/community.sqlite3 里自成一套，
有自己的默认值，不和这两层合并。
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_live_config  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import (  # noqa: E402
    BOOL,
    LAYER_GLOBAL,
    LAYER_PLATFORM,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_LEGACY,
    SOURCE_PLATFORM,
    DerivedDefault,
    LiveKey,
    jsonable,
)
from adapters._shared.trigger_policy import LEGACY_ALL_MODES, LEGACY_PREFIX_MODES, TriggerConfig  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import STATE_LIVE  # noqa: E402
from platforms.registry import platform_config_path, platform_state_path  # noqa: E402
from supervisor.community.service import (  # noqa: E402
    DEFAULT_SETTINGS,
    POLICY_DEFAULTS,
    POLICY_FIELDS,
    CommunityService,
)
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.feature_config_api import features_state  # noqa: E402
from supervisor.sticker_tagging import AUTO_TAG, StickerTaggingService  # noqa: E402

_LIBRARY = "sticker_library_limit"


@pytest.fixture
def qq(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """真实的 QQ 两层配置：qq.yaml 是平台层，features.yaml 是全局层，都在 tmp_path 下。

    开发机上可能设着这些 env，留着会让「没写就是默认值」那一步测不准。
    """
    module = load_live_config(monkeypatch, tmp_path)
    for key in module.LIVE_KEYS:
        for env_name in key.env:
            monkeypatch.delenv(env_name, raising=False)
    # 去 Markdown 那两项的默认值由这个旧 env 推导，它不在任何键的 env 列表里。
    monkeypatch.delenv("ONEBOT_STRIP_MARKDOWN_STYLES", raising=False)
    module.invalidate()
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_platform(module: Any, text: str) -> None:
    _write(module.config_path(), text)
    module.invalidate()


def _write_global(module: Any, text: str) -> None:
    _write(module.features.path(), text)
    module.features.invalidate()


def _effective(module: Any, name: str) -> tuple[Any, str]:
    """生效值和控制台会标出来的来源。"""
    return getattr(module.live, name), module.state_payload()["sources"][name]


def _publish(module: Any, root: Path) -> None:
    """把这个 bot 的生效快照写成 live 状态文件，和适配器在线时公布的那份一样。"""
    path = platform_state_path(QQ_NAPCAT, STATE_LIVE, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"published_at": time.time(), **module.state_payload()}
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


class TestTwoFileLayers:
    """能写两层的键：平台 > 全局 > env > 默认。"""

    def test_each_layer_in_turn_takes_over(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _effective(qq, "echo_threshold") == (3, SOURCE_DEFAULT)

        monkeypatch.setenv("ONEBOT_ECHO_THRESHOLD", "4")
        qq.invalidate()
        assert _effective(qq, "echo_threshold") == (4, SOURCE_ENV)

        # env 排在两份文件之后：控制台改了全局值，不该被一行早就忘了的 env 顶掉。
        _write_global(qq, "extensions:\n  echo:\n    threshold: 5\n")
        assert _effective(qq, "echo_threshold") == (5, SOURCE_GLOBAL)

        _write_platform(qq, "extensions:\n  echo:\n    threshold: 6\n")
        assert _effective(qq, "echo_threshold") == (6, SOURCE_PLATFORM)

    def test_the_platform_only_overrides_what_it_writes(self, qq: Any) -> None:
        _write_global(qq, "extensions:\n  echo:\n    threshold: 5\n    max_text_length: 80\n")
        _write_platform(qq, "extensions:\n  echo:\n    threshold: 6\n")

        assert _effective(qq, "echo_threshold") == (6, SOURCE_PLATFORM)
        assert _effective(qq, "echo_max_length") == (80, SOURCE_GLOBAL)

    def test_a_platform_only_key_never_looks_at_the_global_file(self, qq: Any) -> None:
        # 群白名单是平台 ID，写进全局文件对哪个平台都不成立。
        _write_global(qq, "channels:\n  allowed_groups: [12345]\n")

        assert _effective(qq, "allowed_groups") == ((), SOURCE_DEFAULT)


class TestGlobalOnlyLayers:
    """只认全局的键：全局 > 平台文件老位置 > env > 默认。"""

    def test_each_layer_in_turn_takes_over(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _effective(qq, _LIBRARY) == (1000, SOURCE_DEFAULT)

        monkeypatch.setenv("ONEBOT_STICKER_LIBRARY_LIMIT", "300")
        qq.invalidate()
        assert _effective(qq, _LIBRARY) == (300, SOURCE_ENV)

        _write_platform(qq, "extensions:\n  sticker:\n    library_limit: 500\n")
        assert _effective(qq, _LIBRARY) == (500, SOURCE_LEGACY)

        _write_global(qq, "extensions:\n  sticker:\n    library_limit: 700\n")
        assert _effective(qq, _LIBRARY) == (700, SOURCE_GLOBAL)

    def test_a_global_file_that_leaves_it_out_keeps_the_old_line(self, qq: Any) -> None:
        # 升级之后全局文件里先只写了别的项，老位置上的这一行还得接着算数。
        _write_platform(qq, "extensions:\n  sticker:\n    library_limit: 500\n")
        _write_global(qq, "extensions:\n  sticker:\n    pending_limit: 50\n")

        assert _effective(qq, _LIBRARY) == (500, SOURCE_LEGACY)
        assert _effective(qq, "sticker_pending_limit") == (50, SOURCE_GLOBAL)


class TestLegacyReads:
    """升级前的写法照旧读得到。"""

    @pytest.mark.parametrize("mode", sorted(LEGACY_ALL_MODES))
    def test_the_old_group_mode_still_turns_on_the_unset_all_signal(self, qq: Any, mode: str) -> None:
        _write_platform(qq, f"trigger:\n  group_mode: {mode}\n")

        assert qq.live.signal_all is None
        assert TriggerConfig.from_live(qq.live).all_messages is True

    @pytest.mark.parametrize("mode", sorted(LEGACY_PREFIX_MODES))
    def test_the_old_group_mode_still_turns_on_the_unset_prefix_signal(self, qq: Any, mode: str) -> None:
        _write_platform(qq, f"trigger:\n  group_mode: {mode}\n")

        assert TriggerConfig.from_live(qq.live).prefix is True

    def test_the_default_mode_leaves_both_derived_signals_off(self, qq: Any) -> None:
        config = TriggerConfig.from_live(qq.live)

        assert (config.all_messages, config.prefix) == (False, False)
        # @ 与被回复在旧实现里共用 to_me，两者当时都是开着的。
        assert (config.at, config.reply) == (True, True)

    def test_an_explicit_signal_stops_following_the_old_mode(self, qq: Any) -> None:
        _write_platform(qq, "trigger:\n  group_mode: all\n  signals:\n    all: false\n")

        assert qq.live.signal_all is False
        assert TriggerConfig.from_live(qq.live).all_messages is False

    def test_the_global_file_can_turn_a_signal_on_for_every_platform(self, qq: Any) -> None:
        _write_global(qq, "trigger:\n  signals:\n    all: true\n")

        assert _effective(qq, "signal_all") == (True, SOURCE_GLOBAL)
        assert TriggerConfig.from_live(qq.live).all_messages is True

    @pytest.mark.parametrize(("raw", "expected"), [("0", (False, False)), ("1", (True, True))])
    def test_the_single_old_markdown_env_still_drives_both_switches(
        self, qq: Any, monkeypatch: pytest.MonkeyPatch, raw: str, expected: tuple[bool, bool],
    ) -> None:
        # 拆成两个开关之前只有这一个 env，两项都得跟着它；值是它给的，来源就得说是 env。
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", raw)
        qq.invalidate()

        assert (qq.live.strip_asterisk_styles, qq.live.strip_underscore_styles) == expected
        assert _effective(qq, "strip_asterisk_styles") == (expected[0], SOURCE_ENV)
        assert _effective(qq, "strip_underscore_styles") == (expected[1], SOURCE_ENV)

    def test_without_the_old_markdown_env_both_are_plain_defaults(self, qq: Any) -> None:
        assert _effective(qq, "strip_asterisk_styles") == (True, SOURCE_DEFAULT)
        assert _effective(qq, "strip_underscore_styles") == (False, SOURCE_DEFAULT)

    def test_an_empty_old_markdown_env_still_counts_as_set(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # 旧总开关按布尔读，空串就是「关」，不是「没配」。
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", "")
        qq.invalidate()

        assert _effective(qq, "strip_asterisk_styles") == (False, SOURCE_ENV)

    def test_the_new_switches_beat_the_old_markdown_env(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", "0")
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES", "1")
        qq.invalidate()

        assert _effective(qq, "strip_asterisk_styles") == (True, SOURCE_ENV)
        assert _effective(qq, "strip_underscore_styles") == (False, SOURCE_ENV)

    def test_the_file_beats_both_markdown_envs(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", "0")
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES", "0")
        _write_platform(qq, "output:\n  strip_asterisk_styles: true\n")

        assert _effective(qq, "strip_asterisk_styles") == (True, SOURCE_PLATFORM)

    @pytest.mark.parametrize(("listed", "expected"), [
        ("12345", False),
        ("12345,好友", True),
        ("[私聊只允许已经通过好友请求的人]", True),
    ])
    def test_the_old_private_whitelist_text_still_decides_friend_access(
        self, qq: Any, monkeypatch: pytest.MonkeyPatch, listed: str, expected: bool,
    ) -> None:
        # 以前没有这个开关，放行规则是从白名单文本里找「好友」二字推导出来的。
        monkeypatch.setenv("CLONOTH_ALLOWED_PRIVATE_USERS", listed)
        qq.invalidate()

        assert _effective(qq, "allow_private_friends") == (expected, SOURCE_ENV)

    @pytest.mark.parametrize("listed", ["", "   "])
    def test_an_empty_whitelist_env_is_not_a_setting(
        self, qq: Any, monkeypatch: pytest.MonkeyPatch, listed: str,
    ) -> None:
        # 名单空着时推导只是在复述内置默认，说成 env 会让人去翻一个没写东西的变量。
        monkeypatch.setenv("CLONOTH_ALLOWED_PRIVATE_USERS", listed)
        qq.invalidate()

        assert _effective(qq, "allow_private_friends") == (True, SOURCE_DEFAULT)

    def test_the_second_whitelist_env_name_counts_too(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_ALLOWED_PRIVATE_USERS", "12345")
        qq.invalidate()

        assert _effective(qq, "allow_private_friends") == (False, SOURCE_ENV)

    def test_the_switchs_own_env_beats_the_derivation(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_ALLOWED_PRIVATE_USERS", "12345")
        monkeypatch.setenv("ONEBOT_ALLOW_PRIVATE_FRIENDS", "1")
        qq.invalidate()

        assert _effective(qq, "allow_private_friends") == (True, SOURCE_ENV)

    def test_an_explicit_line_beats_the_whitelist_derivation(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_ALLOWED_PRIVATE_USERS", "12345")
        _write_platform(qq, "channels:\n  allow_private_friends: true\n")

        assert _effective(qq, "allow_private_friends") == (True, SOURCE_PLATFORM)


class TestDerivedDefault:
    """默认值本身从别的 env 推导出来的键：推导说它读到了 env，来源就报 env，控制台标的才和实际一致。"""

    _KEY = LiveKey("flag", "demo.flag", BOOL, False, env=("DEMO_FLAG",))

    def _with(self, value: Any, *, from_env: bool) -> LiveKey:
        return dataclasses.replace(self._KEY, derive_default=lambda: DerivedDefault(value, from_env=from_env))

    def test_a_derivation_that_read_an_env_reports_env(self) -> None:
        assert self._with(True, from_env=True).resolve({}) == (True, SOURCE_ENV)

    def test_a_derivation_that_read_nothing_reports_default(self) -> None:
        assert self._with(True, from_env=False).resolve({}) == (True, SOURCE_DEFAULT)

    def test_the_keys_own_env_comes_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEMO_FLAG", "0")

        assert self._with(True, from_env=True).resolve({}) == (False, SOURCE_ENV)

    def test_the_file_comes_first(self) -> None:
        documents = {LAYER_PLATFORM: {"demo": {"flag": False}}}

        assert self._with(True, from_env=True).resolve(documents) == (False, SOURCE_PLATFORM)

    def test_the_derived_value_goes_through_the_coercer(self) -> None:
        assert self._with("on", from_env=True).resolve({}) == (True, SOURCE_ENV)


class TestSessionLayer:
    """会话层：按群覆盖盖过会话默认，两份配置文件都够不着它。"""

    @pytest.fixture
    def service(self, tmp_path: Path) -> CommunityService:
        return CommunityService(tmp_path)

    @pytest.fixture
    def admin(self) -> FeatureActor:
        return FeatureActor(owner="console:admin", is_admin=True, role="admin", channel="web")

    @pytest.fixture
    def group(self, admin: FeatureActor) -> FeatureActor:
        return replace(admin, scope="qq_group:synthetic")

    def test_a_group_override_beats_the_session_default(
        self, service: CommunityService, admin: FeatureActor, group: FeatureActor,
    ) -> None:
        service.update_defaults(admin, {"values": {"topic_enabled": True}, "expected_revision": 0})
        service.update_overrides(group, {
            "values": {"topic_enabled": False}, "expected_revision": 0, "expected_defaults_revision": 1,
        })

        state = service.state(group)

        assert state["settings"]["topic_enabled"] is False
        assert state["defaults"]["topic_enabled"] is True
        assert state["overrides"] == {"topic_enabled": False}

    def test_a_group_without_an_override_follows_the_session_default(
        self, service: CommunityService, admin: FeatureActor, group: FeatureActor,
    ) -> None:
        service.update_defaults(admin, {"values": {"reply_budget_per_minute": 3}, "expected_revision": 0})

        state = service.state(group)

        assert state["settings"]["reply_budget_per_minute"] == 3
        assert "reply_budget_per_minute" in state["inherited_fields"]

    def test_the_session_layer_has_its_own_built_in_defaults(
        self, service: CommunityService, group: FeatureActor,
    ) -> None:
        assert service.state(group)["settings"] == {**DEFAULT_SETTINGS, **POLICY_DEFAULTS}

    def test_neither_config_file_reaches_it(
        self, service: CommunityService, group: FeatureActor, tmp_path: Path,
    ) -> None:
        before = service.state(group)

        _write(tmp_path / "config" / "features.yaml", "topic_enabled: false\nqueue:\n  workers: 9\n")
        _write(tmp_path / "config" / "qq.yaml", "topic_enabled: false\nqueue:\n  workers: 7\n")

        assert service.state(group) == before

    def test_no_session_setting_shares_a_name_with_a_hot_reloaded_key(self) -> None:
        """两套设置各有各的键空间。重名了「不合并」就只是碰巧，界面上也说不清改的是哪一个。"""
        assert set(DEFAULT_SETTINGS) & set(features.FEATURE_KEYS_BY_NAME) == set()
        assert set(POLICY_FIELDS) & {key.path for key in features.FEATURE_KEYS} == set()


class TestPublishedSourcesMatchReality:
    """控制台按 bot 公布的 sources 标来源，它必须和逐键取值算出来的一致。"""

    def _seed_every_source(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_ECHO_COOLDOWN_SEC", "45")
        # 旧总开关推出来的默认值也进来：推导那条分支同样要报得对。
        monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", "1")
        _write_global(qq, "extensions:\n  echo:\n    window_sec: 60\n")
        _write_platform(
            qq,
            "channels:\n  allowed_groups: [12345]\n"
            "extensions:\n  echo:\n    threshold: 6\n  sticker:\n    library_limit: 500\n",
        )

    def test_every_key_reports_where_it_was_actually_read(
        self, qq: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_every_source(qq, monkeypatch)
        documents = {LAYER_PLATFORM: qq.raw_document(), LAYER_GLOBAL: qq.features.raw_document()}

        payload = qq.state_payload()

        for key in qq.LIVE_KEYS:
            value, source = key.resolve(documents)
            assert payload["sources"][key.name] == source, key.name
            assert payload["values"][key.name] == jsonable(value), key.name
        # 推导出来的默认值跟着旧 env 走，报的也得是 env，不然控制台上它显示成「默认」。
        assert payload["sources"]["strip_asterisk_styles"] == SOURCE_ENV

    def test_all_five_sources_show_up(self, qq: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # 上一条逐键比对，只有每种来源都真的出现过才算比到。
        self._seed_every_source(qq, monkeypatch)

        assert set(qq.state_payload()["sources"].values()) == {
            SOURCE_PLATFORM, SOURCE_GLOBAL, SOURCE_LEGACY, SOURCE_ENV, SOURCE_DEFAULT,
        }


class TestConsoleFallbacksMatchTheBot:
    """全局页标出来的兜底，得和平台那边实际用的值、实际的来源、实际的打标开关对得上。"""

    def test_the_reported_fallback_is_what_the_platform_uses(
        self, qq: Any, tmp_path: Path,
    ) -> None:
        _write_platform(qq, "extensions:\n  sticker:\n    library_limit: 500\n    auto_tag: false\n")
        _publish(qq, tmp_path)
        config = features.new_features_config(tmp_path)

        body = features_state(tmp_path, config)

        published = qq.state_payload()
        for name in (_LIBRARY, AUTO_TAG):
            reported = body["fallbacks"][name][0]
            assert reported["platform"] == QQ_NAPCAT.id
            assert reported["value"] == getattr(qq.live, name)
            assert reported["source"] == published["sources"][name]
            assert reported["file"] == platform_config_path(QQ_NAPCAT, tmp_path).name

    def test_the_central_switch_agrees_with_the_tagging_service(self, qq: Any, tmp_path: Path) -> None:
        _write_platform(qq, "extensions:\n  sticker:\n    auto_tag: false\n")
        _publish(qq, tmp_path)
        config = features.new_features_config(tmp_path)

        switch = StickerTaggingService(tmp_path, config).switch()

        assert features_state(tmp_path, config)["central"][AUTO_TAG] == {
            "enabled": switch.enabled, "set_globally": switch.set_globally,
        }
        assert switch.enabled == qq.live.sticker_auto_tag

    def test_writing_the_global_file_lines_both_sides_back_up(self, qq: Any, tmp_path: Path) -> None:
        # 运营者照提示把设置挪进全局文件之后，页面上的值就是实际生效的值，没有要标的了。
        _write_platform(qq, "extensions:\n  sticker:\n    library_limit: 500\n    auto_tag: false\n")
        _write_global(qq, "extensions:\n  sticker:\n    library_limit: 700\n    auto_tag: true\n")
        _publish(qq, tmp_path)
        config = features.new_features_config(tmp_path)

        body = features_state(tmp_path, config)

        assert body["fallbacks"] == {}
        assert body["central"][AUTO_TAG] == {"enabled": True, "set_globally": True}
        assert body["state"]["values"][_LIBRARY] == qq.live.sticker_library_limit
