"""共享的热载配置引擎：状态跟着实例走，键表写错在构造时就报，派生值跟着钉住的输入重算。"""
from __future__ import annotations

import dataclasses
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.live_config import (  # noqa: E402
    BOOL,
    GLOBAL_AND_PLATFORM,
    GLOBAL_ONLY,
    LAYER_GLOBAL,
    LAYER_PLATFORM,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_LEGACY,
    SOURCE_PLATFORM,
    WORD_LIST,
    LiveConfig,
    LiveKey,
    int_in,
)

_KEYS = (
    LiveKey("enabled", "feature.enabled", BOOL, False),
    LiveKey("names", "feature.names", WORD_LIST, ""),
    LiveKey("limit", "feature.limit", int_in(min_value=1, max_value=10), 3),
)
_DERIVED = ("names_count",)


def _derive(values: dict[str, Any]) -> None:
    values["names_count"] = len(values["names"])


def _config(path: Path) -> LiveConfig:
    return LiveConfig(_KEYS, path=lambda: path, derive=_derive, derived_names=_DERIVED)


def _unused_path() -> Path:
    return Path("never-read.yaml")


def _write(config: LiveConfig, text: str) -> None:
    path = config.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    config.invalidate()


# 全局那层的声明。平台键表只能原样引用，自己补 env。
_SHARED_KEYS = (
    LiveKey("greeting", "chat.greeting", WORD_LIST, "", layers=GLOBAL_AND_PLATFORM),
    LiveKey("library", "chat.library", int_in(min_value=0), 100, layers=GLOBAL_ONLY),
)


def _adopt(name: str, env: tuple[str, ...] = ()) -> LiveKey:
    return dataclasses.replace(next(key for key in _SHARED_KEYS if key.name == name), env=env)


def _layers(tmp_path: Path) -> tuple[LiveConfig, LiveConfig]:
    shared = LiveConfig(_SHARED_KEYS, path=lambda: tmp_path / "features.yaml", layer=LAYER_GLOBAL)
    platform = LiveConfig(
        (*_KEYS, _adopt("greeting", env=("CHAT_GREETING",)), _adopt("library", env=("CHAT_LIBRARY",))),
        path=lambda: tmp_path / "chat.yaml",
        derive=_derive,
        derived_names=_DERIVED,
        defaults=shared,
    )
    return shared, platform


def _effective(config: LiveConfig, name: str) -> tuple[Any, str]:
    return getattr(config.live, name), config.state_payload()["sources"][name]


class TestInstanceState:
    """同一进程里可能有几个平台连接，各自一份配置文件，谁的缓存和快照都不能串到别人那里。"""

    def test_each_instance_reads_its_own_file(self, tmp_path: Path) -> None:
        first = _config(tmp_path / "first.yaml")
        second = _config(tmp_path / "second.yaml")
        _write(first, "feature:\n  limit: 7\n")
        _write(second, "feature:\n  limit: 2\n")

        assert (first.live.limit, second.live.limit) == (7, 2)

    def test_a_pin_stays_on_its_own_instance(self, tmp_path: Path) -> None:
        first = _config(tmp_path / "first.yaml")
        second = _config(tmp_path / "second.yaml")

        with first.pinned(limit=9):
            assert (first.live.limit, second.live.limit) == (9, 3)

    def test_refresh_pins_only_its_own_instance(self, tmp_path: Path) -> None:
        first = _config(tmp_path / "first.yaml")
        second = _config(tmp_path / "second.yaml")
        first.refresh()

        _write(first, "feature:\n  limit: 8\n")
        _write(second, "feature:\n  limit: 8\n")

        assert (first.live.limit, second.live.limit) == (3, 8)


class TestDeclarations:
    def test_a_duplicate_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate key name 'limit'"):
            LiveConfig((*_KEYS, LiveKey("limit", "other.limit", BOOL, False)), path=_unused_path)

    def test_a_duplicate_path_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate path 'feature.limit'"):
            LiveConfig((*_KEYS, LiveKey("ceiling", "feature.limit", BOOL, False)), path=_unused_path)

    def test_a_path_nested_under_another_key_is_rejected(self) -> None:
        # 写回时外层键是标量，内层键会把它压成字典，或者反过来被它压掉。
        with pytest.raises(ValueError, match="path 'feature.limit' is a prefix of 'feature.limit.hard'"):
            LiveConfig((*_KEYS, LiveKey("hard", "feature.limit.hard", BOOL, False)), path=_unused_path)

    def test_a_sibling_sharing_the_text_prefix_is_fine(self) -> None:
        LiveConfig((*_KEYS, LiveKey("limit_hard", "feature.limit_hard", BOOL, False)), path=_unused_path)

    def test_a_derived_name_cannot_shadow_a_key(self) -> None:
        with pytest.raises(ValueError, match="derived name 'limit' shadows a declared key"):
            LiveConfig(_KEYS, path=_unused_path, derived_names=("limit",))


class TestPinning:
    def test_derived_values_follow_a_pinned_input(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        with config.pinned(names=("a", "b")):
            assert config.live.names_count == 2
        assert config.live.names_count == 0

    def test_an_explicit_derived_value_wins(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        with config.pinned(names=("a", "b"), names_count=5):
            assert config.live.names_count == 5

    def test_an_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        with pytest.raises(KeyError, match="unknown live config keys"):
            with config.pinned(nmes=()):
                pass

    def test_reload_drops_the_pin_and_rereads(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")
        config.refresh()
        _write(config, "feature:\n  limit: 8\n")
        assert config.live.limit == 3

        assert config.reload()["limit"] == 8
        assert config.live.limit == 8


class TestLayerDeclarations:
    """层写错在构造时就报：全局文件不认 env，平台引用的全局键必须和全局声明逐项一致。"""

    def test_a_key_needs_at_least_one_layer(self) -> None:
        with pytest.raises(ValueError, match="key 'x' declares no layer"):
            LiveConfig((LiveKey("x", "a.x", BOOL, False, layers=frozenset()),), path=_unused_path)

    def test_an_unknown_layer_on_a_key_is_rejected(self) -> None:
        # 会话那一层还没做，先写上只会让人以为它生效了。
        with pytest.raises(ValueError, match=r"key 'x' declares unknown layers \['conversation'\]"):
            LiveConfig((LiveKey("x", "a.x", BOOL, False, layers=frozenset({"conversation"})),), path=_unused_path)

    def test_an_unknown_layer_for_the_config_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown layer 'conversation'"):
            LiveConfig(_KEYS, path=_unused_path, layer="conversation")

    def test_the_global_layer_takes_no_env(self) -> None:
        key = LiveKey("x", "a.x", BOOL, False, env=("X",), layers=GLOBAL_ONLY)

        with pytest.raises(ValueError, match="global key 'x' reads env"):
            LiveConfig((key,), path=_unused_path, layer=LAYER_GLOBAL)

    def test_the_global_layer_holds_only_keys_that_allow_it(self) -> None:
        with pytest.raises(ValueError, match="key 'enabled' cannot be set globally"):
            LiveConfig(_KEYS, path=_unused_path, layer=LAYER_GLOBAL)

    def test_the_global_layer_has_nothing_to_fall_back_on(self, tmp_path: Path) -> None:
        shared, _ = _layers(tmp_path)

        with pytest.raises(ValueError, match="nothing wider to fall back on"):
            LiveConfig(_SHARED_KEYS, path=_unused_path, layer=LAYER_GLOBAL, defaults=shared)

    def test_a_shared_key_on_a_platform_needs_the_global_layer(self) -> None:
        with pytest.raises(ValueError, match="key 'greeting' has a global layer that nobody declares"):
            LiveConfig((_adopt("greeting"),), path=_unused_path)

    def test_a_shared_key_cannot_drift_from_its_global_declaration(self, tmp_path: Path) -> None:
        shared, _ = _layers(tmp_path)
        drifted = dataclasses.replace(_adopt("greeting"), default="hi")

        with pytest.raises(ValueError, match="key 'greeting' drifted from its global declaration"):
            LiveConfig((drifted,), path=_unused_path, defaults=shared)

    def test_defaults_must_come_from_the_global_layer(self, tmp_path: Path) -> None:
        other = _config(tmp_path / "other.yaml")

        with pytest.raises(ValueError, match="defaults must come from the global layer"):
            LiveConfig(_KEYS, path=_unused_path, defaults=other)


class TestLayers:
    """全局默认 → 平台两层：窄层写了就盖住宽层，env 排在两份文件之后。"""

    def test_platform_beats_global_beats_env_beats_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared, platform = _layers(tmp_path)
        assert _effective(platform, "greeting") == ((), SOURCE_DEFAULT)

        monkeypatch.setenv("CHAT_GREETING", "hi")
        platform.invalidate()
        assert _effective(platform, "greeting") == (("hi",), SOURCE_ENV)

        _write(shared, "chat:\n  greeting: [hello]\n")
        assert _effective(platform, "greeting") == (("hello",), SOURCE_GLOBAL)

        _write(platform, "chat:\n  greeting: [yo]\n")
        assert _effective(platform, "greeting") == (("yo",), SOURCE_PLATFORM)

    def test_a_global_only_key_reads_the_old_platform_line_after_the_global_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared, platform = _layers(tmp_path)
        monkeypatch.setenv("CHAT_LIBRARY", "7")
        platform.invalidate()
        assert _effective(platform, "library") == (7, SOURCE_ENV)

        _write(platform, "chat:\n  library: 9\n")
        assert _effective(platform, "library") == (9, SOURCE_LEGACY)

        _write(shared, "chat:\n  library: 11\n")
        assert _effective(platform, "library") == (11, SOURCE_GLOBAL)

    def test_a_platform_only_key_never_reads_the_global_file(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)

        _write(shared, "feature:\n  limit: 9\n")

        assert platform.live.limit == 3

    def test_editing_the_global_file_reaches_the_platform_without_touching_it(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)
        assert platform.live.greeting == ()

        shared.path().write_text("chat:\n  greeting: [hello]\n", encoding="utf-8")

        assert platform.live.greeting == ("hello",)

    def test_a_rewrite_the_fingerprint_misses_still_reaches_the_platform(self, tmp_path: Path) -> None:
        """同一时刻写两次、大小又没变时指纹认不出来；全局那层失效一次，平台跟着重算。"""
        shared, platform = _layers(tmp_path)
        _write(shared, "chat:\n  library: 11\n")
        assert platform.live.library == 11
        before = shared.path().stat()

        shared.path().write_text("chat:\n  library: 22\n", encoding="utf-8")
        os.utime(shared.path(), ns=(before.st_atime_ns, before.st_mtime_ns))
        shared.invalidate()

        assert platform.live.library == 22

    def test_derived_values_follow_the_global_layer(self, tmp_path: Path) -> None:
        shared = LiveConfig(_SHARED_KEYS, path=lambda: tmp_path / "features.yaml", layer=LAYER_GLOBAL)
        platform = LiveConfig(
            (_adopt("greeting"),),
            path=lambda: tmp_path / "chat.yaml",
            derive=lambda values: values.update(greeting_count=len(values["greeting"])),
            derived_names=("greeting_count",),
            defaults=shared,
        )

        _write(shared, "chat:\n  greeting: [hello, hi]\n")

        assert platform.live.greeting_count == 2

    def test_a_broken_global_file_keeps_the_last_good_global_values(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)
        _write(shared, "chat:\n  greeting: [hello]\n")
        assert platform.live.greeting == ("hello",)

        _write(shared, "chat: [oops\n")

        assert platform.live.greeting == ("hello",)
        assert platform.state_payload()["defaults"]["parse_error"]

    def test_each_layer_only_writes_what_it_may_hold(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)

        with pytest.raises(KeyError, match=r"not settable in chat.yaml: \['library'\]"):
            platform.document_from({"library": 5})
        assert shared.document_from({"library": 5}) == {"chat": {"library": 5}}
        assert platform.document_from({"greeting": ["yo"]}) == {"chat": {"greeting": ["yo"]}}

    def test_the_platform_state_carries_layers_sources_and_the_global_file(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)

        state = platform.state_payload()

        assert state["layer"] == LAYER_PLATFORM
        assert state["layers"] == {
            "enabled": [LAYER_PLATFORM], "names": [LAYER_PLATFORM], "limit": [LAYER_PLATFORM],
            "greeting": [LAYER_GLOBAL, LAYER_PLATFORM], "library": [LAYER_GLOBAL],
        }
        assert set(state["sources"]) == set(state["layers"])
        assert state["defaults"]["config_path"] == str(tmp_path / "features.yaml")
        assert shared.state_payload()["layer"] == LAYER_GLOBAL
        assert "defaults" not in shared.state_payload()


class TestMisplacedGlobalKeys:
    """只准写在全局的键还留在平台文件里：提醒一次，状态变了再提醒。"""

    _LOGGER = "adapters._shared.live_config"

    def test_each_state_is_reported_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        shared, platform = _layers(tmp_path)
        _write(platform, "chat:\n  library: 9\n")

        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            for _ in range(3):
                platform.invalidate()
                assert platform.live.library == 9
            _write(shared, "chat:\n  library: 11\n")
            assert platform.live.library == 11

        assert caplog.text.count("chat.yaml still sets chat.library; it belongs in features.yaml") == 1
        assert caplog.text.count("the line in chat.yaml has no effect") == 1

    def test_removing_the_old_line_resets_the_reminder(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, platform = _layers(tmp_path)

        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            for text in ("chat:\n  library: 9\n", "{}\n", "chat:\n  library: 9\n"):
                _write(platform, text)
                platform.current()

        assert caplog.text.count("still sets chat.library") == 2


class TestReader:
    def test_a_write_points_at_the_platform_file(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        with pytest.raises(AttributeError, match="read-only; edit chat.yaml"):
            config.live.limit = 5

    def test_dir_lists_keys_and_derived_names(self, tmp_path: Path) -> None:
        assert set(dir(_config(tmp_path / "chat.yaml").live)) == {"enabled", "names", "limit", "names_count"}


class TestFailClosed:
    def test_a_broken_file_keeps_the_previous_values(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")
        _write(config, "feature:\n  limit: 6\n")
        assert config.live.limit == 6

        _write(config, "feature: [unclosed\n")

        assert config.live.limit == 6
        state = config.state_payload()
        assert state["parse_error"]
        assert state["loaded"] != state["file"]

    def test_the_error_names_the_platform_file(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config = _config(tmp_path / "chat.yaml")
        _write(config, "- just\n- a list\n")

        with caplog.at_level(logging.ERROR, logger="adapters._shared.live_config"):
            config.current()

        assert "chat.yaml is unusable" in caplog.text


class TestResolved:
    """值和来源一起取：只拿到值的话，分不清是运营者亲手写的还是默认值。"""

    def test_each_value_comes_with_where_it_was_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHAT_LIBRARY", "7")
        _shared, platform = _layers(tmp_path)
        _write(platform, "feature:\n  limit: 5\n")

        values, sources = platform.resolved()

        assert (values["limit"], sources["limit"]) == (5, SOURCE_PLATFORM)
        assert (values["library"], sources["library"]) == (7, SOURCE_ENV)
        assert (values["enabled"], sources["enabled"]) == (False, SOURCE_DEFAULT)

    def test_a_pinned_snapshot_stays_out_of_it(self, tmp_path: Path) -> None:
        # 钉住的只有值；混进来的话，值是钉的、来源却是文件的，两边对不上。
        config = _config(tmp_path / "chat.yaml")
        _write(config, "feature:\n  limit: 5\n")

        with config.pinned(limit=9):
            values, sources = config.resolved()

        assert (values["limit"], sources["limit"]) == (5, SOURCE_PLATFORM)

    def test_it_follows_the_file(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")
        assert config.resolved()[1]["limit"] == SOURCE_DEFAULT

        _write(config, "feature:\n  limit: 4\n")
        values, sources = config.resolved()

        assert (values["limit"], sources["limit"]) == (4, SOURCE_PLATFORM)

    def test_the_published_sources_are_these(self, tmp_path: Path) -> None:
        shared, platform = _layers(tmp_path)
        _write(shared, "chat:\n  library: 3\n")

        assert platform.state_payload()["sources"] == dict(platform.resolved()[1])
        assert platform.resolved()[1]["library"] == SOURCE_GLOBAL


class TestSaveAndPublish:
    def test_save_writes_the_nested_layout_and_reloads(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        config.save(config.document_from({"limit": 4, "names": ["x"]}))

        assert config.live.limit == 4
        assert config.raw_document() == {"feature": {"limit": 4, "names": ["x"]}}

    def test_a_non_mapping_document_is_refused_by_file_name(self, tmp_path: Path) -> None:
        config = _config(tmp_path / "chat.yaml")

        with pytest.raises(TypeError, match="chat.yaml must be a mapping"):
            config.save(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_list_keys_publish_their_item_rule(self, tmp_path: Path) -> None:
        rules = _config(tmp_path / "chat.yaml").state_payload()["input_rules"]

        assert rules == {"names": {"kind": "word", "trim": True}}

    def test_platform_extras_are_left_to_the_adapter(self, tmp_path: Path) -> None:
        # 能力清单这类只有部分平台才有的内容由适配器自己补，引擎不替它决定。
        assert "capabilities" not in _config(tmp_path / "chat.yaml").state_payload()
