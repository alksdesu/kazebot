"""控制台直接改的 YAML 文件：按点分路径取值。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.config_files import value_at  # noqa: E402

_DOCUMENT = {"extensions": {"sticker": {"auto_accept": False, "groups": [1, 2]}, "echo": None}, "mode": "all"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("mode", "all"),
        ("extensions.sticker.groups", [1, 2]),
        # false 是写了值，不能和「没写」混成一个。
        ("extensions.sticker.auto_accept", False),
        ("extensions.sticker", {"auto_accept": False, "groups": [1, 2]}),
    ],
)
def test_a_written_value_comes_back(path: str, expected: object) -> None:
    assert value_at(_DOCUMENT, path) == expected


@pytest.mark.parametrize(
    "path",
    ["missing", "extensions.sticker.missing", "extensions.echo.enabled", "mode.deeper", "extensions.sticker.groups.0"],
)
def test_a_missing_value_or_a_scalar_on_the_way_is_none(path: str) -> None:
    assert value_at(_DOCUMENT, path) is None


def test_a_document_that_is_not_a_mapping_has_nothing_in_it() -> None:
    assert value_at(["mode"], "mode") is None
    assert value_at(None, "mode") is None
