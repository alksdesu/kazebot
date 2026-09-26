"""共享的热载配置引擎：状态跟着实例走，键表写错在构造时就报，派生值跟着钉住的输入重算。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.live_config import BOOL, WORD_LIST, LiveConfig, LiveKey, int_in  # noqa: E402

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
