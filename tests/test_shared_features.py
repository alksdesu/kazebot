"""全平台共用的功能设置：声明只写一份，平台只补自己的 env 兜底，模板和声明逐项对得上。"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _yaml_paths import leaf_paths  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import (  # noqa: E402
    GLOBAL_ONLY,
    LAYER_GLOBAL,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_LEGACY,
    LiveConfig,
)
from adapters._shared.yaml_loader import load_yaml  # noqa: E402

_EXAMPLE = _ROOT / "config" / "features.example.yaml"
_STICKER_LIBRARY = frozenset({
    "sticker_auto_tag", "sticker_auto_accept", "sticker_pending_limit", "sticker_library_limit",
    "sticker_pending_ttl_days",
})


class TestDeclarations:
    def test_every_key_can_be_set_globally(self) -> None:
        assert [key.name for key in features.FEATURE_KEYS if LAYER_GLOBAL not in key.layers] == []

    def test_no_key_reads_env(self) -> None:
        """env 兜底是各平台早就有的老设置，挂在全局声明上就分不清是哪个平台的。"""
        offenders = [key.name for key in features.FEATURE_KEYS if key.env or key.derive_default is not None]

        assert offenders == []

    def test_only_the_sticker_library_is_global_only(self) -> None:
        assert {key.name for key in features.FEATURE_KEYS if key.layers == GLOBAL_ONLY} == _STICKER_LIBRARY

    def test_the_global_only_paths_follow_the_declarations(self) -> None:
        assert features.GLOBAL_ONLY_PATHS == {features.FEATURE_KEYS_BY_NAME[name].path for name in _STICKER_LIBRARY}

    def test_adopting_keeps_everything_but_env(self) -> None:
        adopted = features.adopt("enable_echo", env=("SOME_PLATFORM_ECHO",))

        assert adopted.env == ("SOME_PLATFORM_ECHO",)
        assert dataclasses.replace(adopted, env=()) == features.FEATURE_KEYS_BY_NAME["enable_echo"]

    def test_adopting_an_unknown_name_fails_loudly(self) -> None:
        """平台键表在导入时就调它：拼错名字应当起不来，而不是少一个键。"""
        with pytest.raises(KeyError):
            features.adopt("enable_ecoh")


def _published(value: Any, source: str, name: str = "sticker_auto_tag") -> dict[str, Any]:
    return {"values": {name: value}, "sources": {name: source}}


class TestCentralSwitch:
    """打标这类全系统只跑一份的功能：全局写了听全局，没写认各平台亲手写的兜底，都没有才用默认值。"""

    _DEFAULT_ON = features.CentralSwitch(enabled=True, set_globally=False)

    @pytest.fixture
    def config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LiveConfig:
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
        return features.new_features_config(tmp_path)

    @staticmethod
    def _write_global(config: LiveConfig, text: str) -> None:
        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text(text, encoding="utf-8")
        config.invalidate()

    def test_the_global_file_wins_over_every_fallback(self, config: LiveConfig) -> None:
        self._write_global(config, "extensions:\n  sticker:\n    auto_tag: false\n")

        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published(True, SOURCE_ENV)})

        assert switch == features.CentralSwitch(enabled=False, set_globally=True)

    def test_a_platform_env_decides_while_the_global_file_is_silent(self, config: LiveConfig) -> None:
        # 关过打标的部署升级之后不能悄悄重新花钱。
        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published(False, SOURCE_ENV)})

        assert switch == features.CentralSwitch(
            enabled=False, set_globally=False, fallbacks=(features.PlatformFallback("qq", SOURCE_ENV, False),),
        )
        assert switch.fallbacks[0].from_env is True

    def test_the_old_platform_line_counts_too(self, config: LiveConfig) -> None:
        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published(False, SOURCE_LEGACY)})

        assert switch.enabled is False
        assert switch.fallbacks[0].from_env is False

    def test_a_platform_default_is_not_a_setting(self, config: LiveConfig) -> None:
        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published(False, SOURCE_DEFAULT)})

        assert switch == self._DEFAULT_ON

    def test_what_a_bot_saw_in_the_global_file_is_not_a_fallback(self, config: LiveConfig) -> None:
        # 全局文件以这里读到的为准；bot 读到的是另一份或旧的一份，不该反过来顶掉它。
        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published(False, SOURCE_GLOBAL)})

        assert switch == self._DEFAULT_ON

    def test_an_old_bot_without_sources_is_not_guessed_at(self, config: LiveConfig) -> None:
        published = {"qq": {"values": {"sticker_auto_tag": False}}}

        assert features.central_switch("sticker_auto_tag", config, published) == self._DEFAULT_ON

    @pytest.mark.parametrize("state", [
        {"values": "oops", "sources": {"sticker_auto_tag": SOURCE_ENV}},
        {"values": {"sticker_auto_tag": False}, "sources": [SOURCE_ENV]},
        {"values": {}, "sources": {"sticker_auto_tag": SOURCE_ENV}},
        {},
    ])
    def test_a_malformed_publication_is_skipped(self, config: LiveConfig, state: dict[str, Any]) -> None:
        assert features.central_switch("sticker_auto_tag", config, {"qq": state}) == self._DEFAULT_ON

    def test_one_platform_left_on_keeps_it_running(self, config: LiveConfig) -> None:
        # 以前每个 bot 各跑一份：只要还有一个开着，图库就有人打。
        published = {"qq": _published(False, SOURCE_ENV), "vault": _published(True, SOURCE_LEGACY)}

        switch = features.central_switch("sticker_auto_tag", config, published)

        assert switch.enabled is True
        assert {fallback.platform for fallback in switch.fallbacks} == {"qq", "vault"}

    def test_every_platform_off_turns_it_off(self, config: LiveConfig) -> None:
        published = {"qq": _published(False, SOURCE_ENV), "vault": _published(False, SOURCE_LEGACY)}

        assert features.central_switch("sticker_auto_tag", config, published).enabled is False

    def test_nothing_published_means_the_default(self, config: LiveConfig) -> None:
        assert features.central_switch("sticker_auto_tag", config, {}) == self._DEFAULT_ON

    def test_a_published_value_goes_through_the_key_parser(self, config: LiveConfig) -> None:
        switch = features.central_switch("sticker_auto_tag", config, {"qq": _published("off", SOURCE_ENV)})

        assert switch.fallbacks[0].value is False
        assert switch.enabled is False

    def test_a_global_change_is_seen_on_the_next_call(self, config: LiveConfig) -> None:
        published = {"qq": _published(False, SOURCE_ENV)}
        assert features.central_switch("sticker_auto_tag", config, published).enabled is False

        self._write_global(config, "extensions:\n  sticker:\n    auto_tag: true\n")

        assert features.central_switch("sticker_auto_tag", config, published) == features.CentralSwitch(
            enabled=True, set_globally=True,
        )

    def test_it_needs_the_global_layer(self, config: LiveConfig, tmp_path: Path) -> None:
        platform = LiveConfig(
            (features.adopt("sticker_auto_tag"),), path=lambda: tmp_path / "chat.yaml", defaults=config,
        )

        with pytest.raises(ValueError):
            features.central_switch("sticker_auto_tag", platform, {})

    def test_it_only_takes_switches(self, config: LiveConfig) -> None:
        with pytest.raises(ValueError):
            features.central_switch("sticker_pending_limit", config, {})

    def test_a_key_platforms_may_override_has_no_fallbacks(self, config: LiveConfig) -> None:
        # 平台文件写这种键就是平台覆盖，env 也只管那一个平台，谈不上替全局兜底。
        with pytest.raises(ValueError):
            features.platform_fallbacks("enable_echo", {"qq": _published(True, SOURCE_ENV, "enable_echo")})
        with pytest.raises(ValueError):
            features.central_switch("enable_echo", config, {})

    def test_fallbacks_are_parsed_for_any_global_only_key(self) -> None:
        published = {"qq": _published("50", SOURCE_LEGACY, "sticker_pending_limit")}

        assert features.platform_fallbacks("sticker_pending_limit", published) == (
            features.PlatformFallback("qq", SOURCE_LEGACY, 50),
        )


class TestLocation:
    def test_the_file_sits_under_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)

        assert features.features_config_path(tmp_path) == tmp_path / "config" / "features.yaml"

    def test_the_env_override_is_read_on_every_lookup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
        config = features.new_features_config(tmp_path)
        assert config.path() == tmp_path / "config" / "features.yaml"

        monkeypatch.setenv(features.FEATURES_CONFIG_ENV, str(tmp_path / "elsewhere.yaml"))

        assert config.path() == tmp_path / "elsewhere.yaml"


class TestExampleFileStaysInSync:
    """features.example.yaml 是通用键唯一的键位说明，和声明表脱钩就等于文档骗人。"""

    def _example(self) -> dict[str, Any]:
        loaded = load_yaml(_EXAMPLE.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def test_the_example_lists_exactly_the_declared_keys(self) -> None:
        leaves = set(leaf_paths(self._example())) - {"version"}

        assert sorted(leaves) == sorted(key.path for key in features.FEATURE_KEYS)

    def test_the_example_values_are_the_declared_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """示例文件必须就是当前默认行为，否则复制一份就等于悄悄改了配置。"""
        monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
        config = features.new_features_config(tmp_path)
        defaults = dict(config.current())

        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text(_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        config.invalidate()

        assert dict(config.current()) == defaults
        sources = config.state_payload()["sources"]
        # 三态开关的示例值是 null，本来就等于没写。
        unset = {key.name for key in features.FEATURE_KEYS if key.default is None}
        assert {name for name, source in sources.items() if source != SOURCE_GLOBAL} == unset
