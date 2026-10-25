"""表情包落盘位置的边界：库里登记的路径只认待审和在库两个图片目录。

data/stickers 下还有库文件、备份和锁文件，库里一行被改歪指过去时不能被当成表情包发出去。
"""
from __future__ import annotations

import hashlib
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

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from stickers.store import CAPTION_DONE, STATE_LIBRARY, StickerStore  # noqa: E402

_CONV = "qq_group:sticker-boundary"


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


@pytest.fixture()
def store(runtime: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StickerStore:
    path = tmp_path / "data" / "stickers" / "stickers.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = StickerStore(path)
    monkeypatch.setattr(runtime.emotes, "_sticker_store_handle", lambda: handle)
    yield handle
    handle.close()


def _register(store: StickerStore, name: str, rel_path: str, payload: bytes) -> Any:
    digest = hashlib.sha256(payload + rel_path.encode("utf-8")).hexdigest()
    store.add(sha256=digest, rel_path=rel_path, size=len(payload), source="test", state=STATE_LIBRARY, name=name)
    store.set_auto_tags(digest, ["大笑"], version=1)
    store.mark_caption(digest, CAPTION_DONE)
    return store.get(digest)


def _library_image(store: StickerStore, tmp_path: Path, name: str) -> Any:
    payload = b"GIF89a" + name.encode("utf-8")
    rel_path = f"data/stickers/library/{hashlib.sha256(payload).hexdigest()}.gif"
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return _register(store, name, rel_path, payload)


def test_a_row_pointing_at_the_library_database_is_never_sendable(runtime: Any, store: StickerStore) -> None:
    row = _register(store, "库文件", "data/stickers/stickers.sqlite3", b"")

    assert store.path.is_file() and store.path.stat().st_size > 0
    assert runtime.emotes._sticker_file_path(row) is None
    assert runtime.emotes._sticker_sendable(row) is False
    assert runtime.emotes._read_sticker(row) == b""


@pytest.mark.parametrize("rel_path", [
    "data/stickers/backups/stickers-backup.zip",
    "data/stickers/.tag.lock",
    "data/stickers/library/../stickers.sqlite3",
    "data/stickers/pending/../../stickers/stickers.sqlite3",
])
def test_anything_outside_the_image_folders_is_refused(
    runtime: Any, store: StickerStore, tmp_path: Path, rel_path: str,
) -> None:
    target = (tmp_path / rel_path).resolve()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"GIF89a-not-a-sticker")
    row = _register(store, f"越界{abs(hash(rel_path))}", rel_path, b"")

    assert runtime.emotes._sticker_file_path(row) is None


def test_the_roster_skips_the_database_row_but_keeps_real_images(
    runtime: Any, store: StickerStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(store, "库文件", "data/stickers/stickers.sqlite3", b"")
    real = _library_image(store, tmp_path, "正常")
    set_live_config(runtime, sticker_send_probability=1.0)
    monkeypatch.setattr(runtime.emotes.random, "random", lambda: 0.0)

    entries = runtime.emotes._sticker_prompt_entries(_CONV)

    assert runtime.emotes._sticker_file_path(real) == (tmp_path / real.rel_path).resolve()
    assert entries and all("库文件" not in entry for entry in entries)
    assert any(entry.startswith("正常") for entry in entries)
