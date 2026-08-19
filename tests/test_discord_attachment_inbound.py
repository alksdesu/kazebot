"""Discord 入站附件的类型/体积闸门：图片按魔数嗅探，超大附件读前就断。

与 QQ 侧共用 engine.attachments.sniff_image_mime，PDF 伪装成图片必须被拒、
不落盘；非图片文件不受图片嗅探影响。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._discord_harness import install_discord_stub  # noqa: E402

install_discord_stub()

_ROOT = _REPO_ROOT / "adapters" / "discord"
_PACKAGE_NAME = "_discord_attach_adapter"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE_NAME}.{name}" if name else _PACKAGE_NAME,
        path,
        submodule_search_locations=[str(_ROOT)] if not name else None,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if _PACKAGE_NAME not in sys.modules:
    _load("", _ROOT / "__init__.py")
    for _name in ("history", "messaging"):
        _load(_name, _ROOT / f"{_name}.py")

messaging = sys.modules[f"{_PACKAGE_NAME}.messaging"]

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_GIF = b"GIF89a" + b"\x00" * 64
_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 64


class _Att:
    def __init__(self, filename: str, content_type: str, data: bytes, size: int | None = None) -> None:
        self.filename = filename
        self.content_type = content_type
        self._data = data
        self.size = len(data) if size is None else size
        self.read_called = False

    async def read(self) -> bytes:
        self.read_called = True
        return self._data


def _message(atts: list[_Att]) -> Any:
    return SimpleNamespace(attachments=atts, reference=None, message_snapshots=None)


def _collect(rt: Any, atts: list[_Att], conv: str) -> list[dict[str, Any]]:
    return asyncio.run(messaging._collect_attachments(rt, _message(atts), conv))


def _saved(workspace: Path, conv: str) -> list[Path]:
    att_dir = workspace / "data" / "attachments" / conv.replace(":", "_")
    if not att_dir.exists():
        return []
    return [p for p in att_dir.rglob("*") if p.is_file()]


@pytest.fixture()
def rt(tmp_path: Path) -> Any:
    return SimpleNamespace(clonoth_client=None, workspace=tmp_path)


class TestDiscordInboundGate:
    def test_disguised_pdf_image_is_refused(self, rt, tmp_path: Path) -> None:
        att = _Att("pic.png", "image/png", _PDF)

        result = _collect(rt, [att], "disc_1")

        assert result == []
        assert att.read_called is True
        assert _saved(tmp_path, "disc_1") == []

    def test_oversized_attachment_is_skipped_before_read(self, rt, tmp_path: Path) -> None:
        att = _Att("big.png", "image/png", _PNG, size=messaging._ATTACHMENT_MAX_BYTES + 1)

        result = _collect(rt, [att], "disc_2")

        assert result == []
        assert att.read_called is False
        assert _saved(tmp_path, "disc_2") == []

    def test_valid_image_is_saved_with_sniffed_mime(self, rt, tmp_path: Path) -> None:
        att = _Att("photo.png", "image/png", _PNG)

        result = _collect(rt, [att], "disc_3")

        assert len(result) == 1
        assert result[0]["mime_type"] == "image/png"
        assert len(_saved(tmp_path, "disc_3")) == 1

    def test_mislabeled_but_real_image_uses_magic_mime(self, rt, tmp_path: Path) -> None:
        # content_type 说是 png，字节是 gif：嗅探把 mime 纠正成 image/gif。
        att = _Att("x.png", "image/png", _GIF)

        result = _collect(rt, [att], "disc_4")

        assert len(result) == 1
        assert result[0]["mime_type"] == "image/gif"

    def test_text_file_is_not_gated_by_image_sniffing(self, rt, tmp_path: Path) -> None:
        att = _Att("notes.txt", "text/plain", b"hello world")

        result = _collect(rt, [att], "disc_5")

        assert len(result) == 1
        assert result[0]["type"] == "file"
        assert len(_saved(tmp_path, "disc_5")) == 1
