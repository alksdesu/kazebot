"""控制台直接改的 YAML 文件：按点分路径取值；交上来的原文解析不了时只报位置和问题。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor import config_files  # noqa: E402
from supervisor.config_files import parse_mapping, read_raw, value_at, write_raw  # noqa: E402

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


class TestReadingTheFileOnDisk:
    def test_a_missing_file_is_not_taken_over_yet(self, tmp_path: Path) -> None:
        assert read_raw(tmp_path / "qq.yaml") == {"content": "", "exists": False, "hash": ""}

    def test_a_file_that_is_not_utf8_is_refused_with_the_reason(self, tmp_path: Path) -> None:
        # 以前解码异常没人接，控制台只拿到一句光秃秃的服务器错误
        path = tmp_path / "qq.yaml"
        path.write_bytes(b"queue:\n  workers: 2\n# \xff\n")

        with pytest.raises(HTTPException) as caught:
            read_raw(path)

        assert caught.value.status_code == 500
        assert caught.value.detail == "qq.yaml 读不了：不是 UTF-8 编码（第 23 个字节）"

    def test_a_file_that_cannot_be_read_is_refused_without_its_absolute_path(self, tmp_path: Path) -> None:
        path = tmp_path / "qq.yaml"
        path.mkdir()

        with pytest.raises(HTTPException) as caught:
            read_raw(path)

        assert caught.value.status_code == 500
        assert caught.value.detail.startswith("qq.yaml 读不了：")
        assert str(tmp_path) not in caught.value.detail

    def test_a_failed_write_says_why_without_the_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "qq.yaml"

        def refuse(target: Path, *_args: object, **_kwargs: object) -> str:
            raise PermissionError(13, "Permission denied", str(target))

        monkeypatch.setattr(config_files.config_layout, "write_config_text", refuse)

        with pytest.raises(HTTPException) as caught:
            write_raw(path, "queue: {}\n")

        assert caught.value.status_code == 500
        assert caught.value.detail == "写入 qq.yaml 失败，原文件没动：Permission denied"
