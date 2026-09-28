"""全局页标出平台兜底：只认全局的键全局没写时，各平台实际用的是自己的老位置或 env，控制台得报出来。

fallbacks 报的是「谁在用什么」，central 报的是统一跑的功能实际开不开；两者出自同一份 published 快照。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.platform_api as platform_api  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import (  # noqa: E402
    BOOL,
    GLOBAL_ONLY,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_LEGACY,
    SOURCE_PLATFORM,
    LiveConfig,
)
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, STATE_LIVE, ChannelDescriptor, PlatformDescriptor  # noqa: E402
from platforms.registry import (  # noqa: E402
    PLATFORM_ENV,
    platform_config_path,
    platform_state_path,
    register_platform,
    unregister_platform,
)
from supervisor.feature_config_api import features_state  # noqa: E402
from supervisor.sticker_tagging import AUTO_TAG, StickerTaggingService  # noqa: E402

_LIBRARY = "sticker_library_limit"
_TAGGING_LOGGER = "supervisor.sticker_tagging"

# 没有热载配置文件的平台，用来看通称文案。
PAPERLESS = PlatformDescriptor(
    id="paperless",
    display_name="Paperless",
    short_name="PL",
    channels=(ChannelDescriptor("paperless_room", CHANNEL_GROUP),),
)
VAULT = PlatformDescriptor(
    id="vault",
    display_name="保险库",
    short_name="Vault",
    channels=(ChannelDescriptor("vault_room", CHANNEL_GROUP),),
    config_path="config/vault.yaml",
    state_files={STATE_LIVE: "vault_live.json"},
)


@pytest.fixture(autouse=True)
def _workspace_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局文件和 qq.yaml 都落在 tmp_path 下，不受开发机上的路径覆盖影响。"""
    monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
    monkeypatch.delenv(QQ_NAPCAT.config_env, raising=False)
    for env_name in QQ_NAPCAT.state_env.values():
        monkeypatch.delenv(env_name, raising=False)


@pytest.fixture
def config(tmp_path: Path) -> LiveConfig:
    return features.new_features_config(tmp_path)


@pytest.fixture
def vault() -> Iterator[PlatformDescriptor]:
    register_platform(VAULT)
    try:
        yield VAULT
    finally:
        unregister_platform(VAULT.id)


@pytest.fixture
def paperless() -> Iterator[PlatformDescriptor]:
    register_platform(PAPERLESS)
    try:
        yield PAPERLESS
    finally:
        unregister_platform(PAPERLESS.id)


def _published(**pairs: tuple[Any, str]) -> dict[str, Any]:
    """一个 bot 公布的生效快照，每项给「值, 来源」。"""
    return {
        "values": {name: value for name, (value, _) in pairs.items()},
        "sources": {name: source for name, (_, source) in pairs.items()},
    }


def _write_global(config: LiveConfig, text: str) -> None:
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text(text, encoding="utf-8")
    config.invalidate()


def _publish(root: Path, platform: PlatformDescriptor, **pairs: tuple[Any, str]) -> None:
    path = platform_state_path(platform, STATE_LIVE, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"published_at": time.time(), **_published(**pairs)}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestGlobalFallbacks:
    """全局文件没写的只认全局的键，各平台眼下用着的兜底。"""

    def test_the_old_line_and_the_env_are_both_collected(self, config: LiveConfig) -> None:
        published = {
            "qq": _published(sticker_library_limit=(500, SOURCE_LEGACY)),
            "vault": _published(sticker_pending_limit=(50, SOURCE_ENV)),
        }

        assert features.global_fallbacks(config, published) == {
            _LIBRARY: (features.PlatformFallback("qq", SOURCE_LEGACY, 500),),
            "sticker_pending_limit": (features.PlatformFallback("vault", SOURCE_ENV, 50),),
        }

    @pytest.mark.parametrize("source", [SOURCE_DEFAULT, SOURCE_GLOBAL, SOURCE_PLATFORM])
    def test_a_value_nobody_wrote_by_hand_is_not_a_fallback(self, config: LiveConfig, source: str) -> None:
        # 默认值和全局值都不是平台那边亲手写的；只认全局的键也轮不到平台层。
        published = {"qq": _published(sticker_library_limit=(500, source))}

        assert features.global_fallbacks(config, published) == {}

    def test_an_old_bot_without_sources_is_not_guessed_at(self, config: LiveConfig) -> None:
        published = {"qq": {"values": {_LIBRARY: 500}}}

        assert features.global_fallbacks(config, published) == {}

    def test_a_key_the_global_file_sets_is_not_reported(self, config: LiveConfig) -> None:
        # 全局写了就是它说了算，平台那行早已不起作用，页面上没什么要标的。
        _write_global(config, "extensions:\n  sticker:\n    library_limit: 700\n")
        published = {"qq": _published(sticker_library_limit=(500, SOURCE_LEGACY))}

        assert features.global_fallbacks(config, published) == {}

    def test_the_other_keys_are_still_reported(self, config: LiveConfig) -> None:
        _write_global(config, "extensions:\n  sticker:\n    library_limit: 700\n")
        published = {
            "qq": _published(sticker_library_limit=(500, SOURCE_LEGACY), sticker_auto_tag=(False, SOURCE_ENV)),
        }

        assert set(features.global_fallbacks(config, published)) == {AUTO_TAG}

    def test_a_key_platforms_may_override_is_never_reported(self, config: LiveConfig) -> None:
        # 平台文件写这种键就是这个平台单独覆盖，env 也只管它自己，谈不上替全局兜底。
        published = {"qq": _published(enable_echo=(True, SOURCE_ENV))}

        assert features.global_fallbacks(config, published) == {}

    def test_every_platform_gets_its_own_row(self, config: LiveConfig) -> None:
        published = {
            "qq": _published(sticker_library_limit=(500, SOURCE_LEGACY)),
            "vault": _published(sticker_library_limit=(800, SOURCE_ENV)),
        }

        assert features.global_fallbacks(config, published)[_LIBRARY] == (
            features.PlatformFallback("qq", SOURCE_LEGACY, 500),
            features.PlatformFallback("vault", SOURCE_ENV, 800),
        )

    def test_a_published_value_goes_through_the_key_parser(self, config: LiveConfig) -> None:
        published = {"qq": _published(sticker_library_limit=("500", SOURCE_LEGACY))}

        assert features.global_fallbacks(config, published)[_LIBRARY][0].value == 500

    def test_nothing_published_means_nothing_to_report(self, config: LiveConfig) -> None:
        assert features.global_fallbacks(config, {}) == {}

    def test_it_needs_the_global_layer(self, config: LiveConfig, tmp_path: Path) -> None:
        # 平台那层算出来的是这一个平台的生效值，拿它当「全局写没写」的判据会把兜底当成全局设置。
        platform = LiveConfig((features.adopt(_LIBRARY),), path=lambda: tmp_path / "chat.yaml", defaults=config)

        with pytest.raises(ValueError, match="全局那层"):
            features.global_fallbacks(platform, {})


class TestCentralSwitchRegistry:
    def test_every_central_switch_is_a_global_only_toggle(self) -> None:
        """统一跑的开关必须只认全局文件：平台各写各的话，「实际开不开」就不是一个结论。"""
        for name in sorted(features.CENTRAL_SWITCHES):
            key = features.FEATURE_KEYS_BY_NAME[name]
            assert (key.coerce, key.layers) == (BOOL, GLOBAL_ONLY), name

    def test_an_unregistered_global_only_toggle_is_refused(self, config: LiveConfig) -> None:
        # 入库开关同样是只认全局的布尔键，但它由各平台自己跑，没有「全系统开不开」这回事。
        with pytest.raises(ValueError, match="CENTRAL_SWITCHES"):
            features.central_switch("sticker_auto_accept", config, {})

    def test_an_unregistered_name_is_refused_before_it_is_checked_for_being_a_switch(
        self, config: LiveConfig,
    ) -> None:
        with pytest.raises(ValueError, match="CENTRAL_SWITCHES"):
            features.central_switch("sticker_pending_limit", config, {})

    def test_the_global_layer_is_checked_first(self, config: LiveConfig, tmp_path: Path) -> None:
        platform = LiveConfig((features.adopt(_LIBRARY),), path=lambda: tmp_path / "chat.yaml", defaults=config)

        with pytest.raises(ValueError, match="全局那层"):
            features.central_switch("sticker_pending_limit", platform, {})


class TestFeaturesState:
    """/v1/features/config/state 多报的两项：谁在用什么兜底，统一跑的功能实际开不开。"""

    def test_it_names_the_platform_the_file_and_the_value(self, tmp_path: Path, config: LiveConfig) -> None:
        _publish(
            tmp_path, QQ_NAPCAT,
            sticker_library_limit=(500, SOURCE_LEGACY), sticker_auto_tag=(False, SOURCE_LEGACY),
        )

        body = features_state(tmp_path, config)

        qq_file = platform_config_path(QQ_NAPCAT, tmp_path).name
        assert body["fallbacks"][_LIBRARY] == [{
            "platform": QQ_NAPCAT.id,
            "display_name": QQ_NAPCAT.display_name,
            "source": SOURCE_LEGACY,
            "file": qq_file,
            "value": 500,
        }]
        assert body["fallbacks"][AUTO_TAG] == [{
            "platform": QQ_NAPCAT.id,
            "display_name": QQ_NAPCAT.display_name,
            "source": SOURCE_LEGACY,
            "file": qq_file,
            "value": False,
        }]

    def test_the_central_switch_follows_the_platform_that_turned_it_off(
        self, tmp_path: Path, config: LiveConfig,
    ) -> None:
        # 页面上这一项显示的是默认值「开」，实际是 QQ 那边关着的。
        _publish(tmp_path, QQ_NAPCAT, sticker_auto_tag=(False, SOURCE_LEGACY))

        assert features_state(tmp_path, config)["central"] == {AUTO_TAG: {"enabled": False, "set_globally": False}}

    def test_an_env_fallback_has_no_file(self, tmp_path: Path, config: LiveConfig) -> None:
        _publish(tmp_path, QQ_NAPCAT, sticker_library_limit=(500, SOURCE_ENV))

        assert features_state(tmp_path, config)["fallbacks"][_LIBRARY][0] == {
            "platform": QQ_NAPCAT.id,
            "display_name": QQ_NAPCAT.display_name,
            "source": SOURCE_ENV,
            "file": "",
            "value": 500,
        }

    def test_writing_the_global_file_clears_the_report_and_takes_the_switch_over(
        self, tmp_path: Path, config: LiveConfig,
    ) -> None:
        _publish(
            tmp_path, QQ_NAPCAT,
            sticker_library_limit=(500, SOURCE_LEGACY), sticker_auto_tag=(False, SOURCE_LEGACY),
        )
        _write_global(config, "extensions:\n  sticker:\n    library_limit: 700\n    auto_tag: true\n")

        body = features_state(tmp_path, config)

        assert body["fallbacks"] == {}
        assert body["central"] == {AUTO_TAG: {"enabled": True, "set_globally": True}}

    def test_nothing_published_reports_nothing_and_the_default_switch(
        self, tmp_path: Path, config: LiveConfig,
    ) -> None:
        body = features_state(tmp_path, config)

        assert body["fallbacks"] == {}
        assert body["central"] == {AUTO_TAG: {"enabled": True, "set_globally": False}}

    def test_every_central_switch_is_covered(self, tmp_path: Path, config: LiveConfig) -> None:
        assert set(features_state(tmp_path, config)["central"]) == features.CENTRAL_SWITCHES


class TestConfigFileName:
    def test_a_platform_with_a_config_file_gives_its_name(self, tmp_path: Path) -> None:
        assert platform_api.config_file_name(tmp_path, QQ_NAPCAT) == platform_config_path(QQ_NAPCAT, tmp_path).name

    def test_it_follows_the_path_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(QQ_NAPCAT.config_env, "config/elsewhere.yaml")

        assert platform_api.config_file_name(tmp_path, QQ_NAPCAT) == "elsewhere.yaml"

    def test_a_platform_without_a_hot_reloaded_config_gets_the_generic_name(self, tmp_path: Path) -> None:
        # 说「去改它的配置文件」总比报一个不存在的文件名强，也不能让日志在这里崩掉。
        assert platform_api.config_file_name(tmp_path, PAPERLESS) == "配置文件"

    def test_it_takes_the_generic_name_when_the_path_cannot_be_worked_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(platform_api, "platform_config_path", lambda *_args, **_kwargs: None)

        assert platform_api.config_file_name(tmp_path, QQ_NAPCAT) == "配置文件"


class TestTaggingLogWording:
    """打标日志改用 config_file_name 之后，两种兜底的文案逐字不变。"""

    def _messages(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [record.getMessage() for record in caplog.records if record.name == _TAGGING_LOGGER]

    def _run(self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture) -> list[str]:
        service = StickerTaggingService(tmp_path, config)
        with caplog.at_level(logging.INFO, logger=_TAGGING_LOGGER):
            service._tagger_config()
        return self._messages(caplog)

    def test_an_old_line_that_turns_it_off(
        self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _publish(tmp_path, QQ_NAPCAT, sticker_auto_tag=(False, SOURCE_LEGACY))

        assert self._run(tmp_path, config, caplog) == [
            "表情包自动打标关着：features.yaml 没写 extensions.sticker.auto_tag，"
            "按QQ · NapCat 的 qq.yaml 老位置（关）。要打标就在控制台表情包图库页打开，写进全局设置",
        ]

    def test_an_env_that_turns_it_off(
        self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _publish(tmp_path, QQ_NAPCAT, sticker_auto_tag=(False, SOURCE_ENV))

        assert self._run(tmp_path, config, caplog) == [
            "表情包自动打标关着：features.yaml 没写 extensions.sticker.auto_tag，"
            "按QQ · NapCat 的环境变量（关）。要打标就在控制台表情包图库页打开，写进全局设置",
        ]

    def test_an_old_line_that_leaves_it_on(
        self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _publish(tmp_path, QQ_NAPCAT, sticker_auto_tag=(True, SOURCE_LEGACY))

        assert self._run(tmp_path, config, caplog) == [
            "表情包自动打标开着：features.yaml 没写 extensions.sticker.auto_tag，"
            "按QQ · NapCat 的 qq.yaml 老位置（开）",
        ]

    def test_only_the_platform_this_instance_runs_is_named(
        self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture, vault: PlatformDescriptor,
    ) -> None:
        # 换过平台的实例盘上还留着旧平台的快照，报出来就是叫人去改一份早已没人读的配置。
        _publish(tmp_path, QQ_NAPCAT, sticker_auto_tag=(False, SOURCE_ENV))
        _publish(tmp_path, vault, sticker_auto_tag=(False, SOURCE_LEGACY))

        assert self._run(tmp_path, config, caplog) == [
            "表情包自动打标关着：features.yaml 没写 extensions.sticker.auto_tag，"
            "按QQ · NapCat 的环境变量（关）。要打标就在控制台表情包图库页打开，写进全局设置",
        ]

    def test_a_platform_without_a_config_file_is_named_generically(
        self, tmp_path: Path, config: LiveConfig, caplog: pytest.LogCaptureFixture,
        paperless: PlatformDescriptor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 没有配置文件的平台公布不出老位置兜底，这一句只兜住「真碰上了也别把日志打崩」。
        monkeypatch.setenv(PLATFORM_ENV, paperless.id)
        _publish(tmp_path, paperless, sticker_auto_tag=(False, SOURCE_LEGACY))

        assert self._run(tmp_path, config, caplog) == [
            "表情包自动打标关着：features.yaml 没写 extensions.sticker.auto_tag，"
            "按Paperless 的 配置文件 老位置（关）。要打标就在控制台表情包图库页打开，写进全局设置",
        ]
