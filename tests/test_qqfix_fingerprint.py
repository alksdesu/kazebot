"""配置文件指纹只有一个来源：config_layout.file_stamp；导出给核心比对的形状不变。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import platforms.live as platform_live  # noqa: E402
from adapters._shared.live_config import BOOL, LiveConfig, LiveKey  # noqa: E402

_SHAPE = {"exists", "mtime_ns", "size"}


def test_the_platform_fingerprint_is_built_on_file_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "qq.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    seen: list[Path] = []

    def stamp(target: Path) -> tuple[int, int, int]:
        seen.append(Path(target))
        return (11, 22, 33)

    monkeypatch.setattr(config_layout, "file_stamp", stamp)

    assert platform_live.file_fingerprint(path) == {"exists": True, "mtime_ns": 11, "size": 22}
    assert seen == [path]


def test_a_missing_file_keeps_the_zero_shape(tmp_path: Path) -> None:
    assert platform_live.file_fingerprint(tmp_path / "nope.yaml") == {"exists": False, "mtime_ns": 0, "size": 0}


def test_the_adapter_and_the_core_export_the_same_shape(tmp_path: Path) -> None:
    path = tmp_path / "qq.yaml"
    path.write_text("feature:\n  enabled: true\n", encoding="utf-8")
    config = LiveConfig((LiveKey("enabled", "feature.enabled", BOOL, False),), path=lambda: path)

    state = config.file_state()

    assert set(state["file"]) == _SHAPE and set(state["loaded"]) == _SHAPE
    assert state["file"] == platform_live.file_fingerprint(path)
    assert state["loaded"] == state["file"]


def test_the_stamp_converter_is_the_one_both_sides_use() -> None:
    assert platform_live.stamp_fingerprint(None) == {"exists": False, "mtime_ns": 0, "size": 0}
    assert platform_live.stamp_fingerprint((5, 6, 7)) == {"exists": True, "mtime_ns": 5, "size": 6}
