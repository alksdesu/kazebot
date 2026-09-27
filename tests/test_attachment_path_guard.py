"""附件路径白名单：只认工作区 data/ 下的相对路径。

只比前缀的话 `data/../.env` 也以 "data/" 开头，一路解析回工作区根，多实例部署下还能横着摸到
别的实例。带 .. 段的一律拒绝，三个入口（子节点读附件、file:// 解析、模型填的委派附件路径）
共用这一道门。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.attachments as attachments  # noqa: E402
from engine.attachments import (  # noqa: E402
    _resolve_file_url,
    attachments_to_content_parts,
    is_allowed_attachment_path,
)
from engine.inference.pseudo_handlers import _paths_to_attachments  # noqa: E402


@pytest.mark.parametrize("rel", [
    "data/x.png",
    "/data/x",
    "data/attachments/sess/a.png",
    "data\\reports\\a.md",
])
def test_paths_under_data_are_allowed(rel: str) -> None:
    assert is_allowed_attachment_path(rel) is True


@pytest.mark.parametrize("rel", [
    "data",
    "data/",
    "/data",
    "data/../.env",
    "data/a/../../x",
    "data\\..\\x",
    "C:/data/x",
    "config/x",
    "../data/x",
    "",
    "   ",
    ".env",
])
def test_everything_else_is_refused(rel: str) -> None:
    assert is_allowed_attachment_path(rel) is False


def test_the_private_alias_is_gone() -> None:
    assert not hasattr(attachments, "_is_allowed_attachment_path")


def test_a_traversal_attachment_is_not_inlined(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("OPENAI_API_KEY=sk-real", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "note.txt").write_text("harmless note", encoding="utf-8")

    parts = attachments_to_content_parts(
        [
            {"type": "file", "path": "data/../secret.txt", "name": "secret.txt"},
            {"type": "file", "path": "data/note.txt", "name": "note.txt"},
        ],
        tmp_path,
    )

    assert len(parts) == 1
    assert "harmless note" in parts[0]["text"]
    assert "sk-real" not in str(parts)


def test_a_traversal_file_url_resolves_to_nothing(tmp_path: Path) -> None:
    (tmp_path / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    assert _resolve_file_url("file://data/../x.png", tmp_path) is None


def test_dispatch_attachment_paths_drop_traversal_but_keep_real_data_files(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("sk-real", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "report.md").write_text("# report", encoding="utf-8")

    result = _paths_to_attachments(
        ["data/../secret.txt", "config/qq.yaml", "data/report.md", "  "],
        tmp_path,
    )

    assert [att["path"] for att in result] == ["data/report.md"]
    assert result[0]["type"] == "file"
    assert result[0]["name"] == "report.md"


def test_dispatch_attachment_paths_still_skip_missing_files(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()

    assert _paths_to_attachments(["data/nope.md"], tmp_path) == []
