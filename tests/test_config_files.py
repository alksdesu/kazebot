"""控制台直接改的 YAML 文件：按点分路径取值；交上来的原文解析不了时只报位置和问题。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.config_files import parse_mapping, value_at  # noqa: E402

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


@pytest.mark.parametrize(("content", "detail"), [
    ("api:\n  api_key: sk-SECRET-cf: x\n", "YAML 解析失败：第 2 行第 24 列：mapping values are not allowed here"),
    ("api:\n  api_key: !!int sk-SECRET-cf\n", "YAML 解析失败：第 2 行第 12 列：值不是合法的 int"),
], ids=["broken-line", "bad-value"])
def test_a_refusal_names_the_place_and_leaves_no_content_on_its_chain(content: str, detail: str) -> None:
    # 节点、绘图配置里写着密钥：detail 原样回给控制台，HTTPException 上还挂着原来的异常。
    with pytest.raises(HTTPException) as caught:
        parse_mapping(content)

    assert (caught.value.status_code, caught.value.detail) == (400, detail)
    for link in (caught.value.__cause__, caught.value.__context__):
        assert link is not None and "sk-SECRET" not in str(link)
