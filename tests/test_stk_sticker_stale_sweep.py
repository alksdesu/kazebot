"""表情包库里崩溃留下的半成品：写图和导出的临时文件、导入的暂存目录。

只按名字和年龄清；正在写的、正在导入的都在不停刷新 mtime，不能被当成垃圾。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers import collect as sc
from stickers import files as sf
from stickers import store as ss
from stickers import tagger as st

_OLD = time.time() - sf.STALE_AFTER_SEC - 600


def _make(path: Path, data: bytes = b"x", *, old: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if old:
        os.utime(path, (_OLD, _OLD))
    return path


def _age(path: Path) -> Path:
    os.utime(path, (_OLD, _OLD))
    return path


def _png(tag: bytes) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
    return header + (64).to_bytes(4, "big") + (64).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + tag


def test_only_old_crash_leftovers_are_removed(tmp_path: Path) -> None:
    root = sf.sticker_root(tmp_path)
    pending, library, backups = (root / name for name in (sf.PENDING_DIR, sf.LIBRARY_DIR, sf.BACKUP_DIR))
    image = _make(pending / f"{'a' * 64}.png", b"image")
    keep_hidden = _make(pending / ".keep")
    lock = _make(root / "stickers.sqlite3.tagging.lock", b"0")
    archive = _make(backups / "stickers-20261025-000000-000000.zip", b"zip")
    half_image = _make(pending / f".{'b' * 64}.png.123.abcdef.tmp")
    half_shard = _make(library / "cd" / f".{'c' * 64}.png.9.0a0b0c.tmp")
    half_export = _make(backups / ".stickers-20261025-000001-000000.zip.77.0a0b.tmp")
    fresh_temp = _make(pending / f".{'d' * 64}.png.5.aa.tmp", old=False)
    dead_import = root / ".import.deadbeef"
    _make(dead_import / ("e" * 64))
    _age(dead_import)
    busy_import = root / ".import.cafebabe"
    _make(busy_import / ("f" * 64), old=False)
    _age(busy_import)

    removed = sf.sweep_stale(tmp_path)

    assert set(removed) == {half_image, half_shard, half_export, dead_import}
    for path in (image, keep_hidden, lock, archive, fresh_temp, busy_import):
        assert path.exists(), path
    assert not dead_import.exists()


def test_a_missing_library_is_nothing_to_sweep(tmp_path: Path) -> None:
    assert sf.sweep_stale(tmp_path) == []


def test_the_sweep_never_follows_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    pending = sf.sticker_root(tmp_path) / sf.PENDING_DIR
    pending.mkdir(parents=True)
    try:
        (pending / ".evil.tmp").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器建不了软链")

    removed = sf.sweep_stale(tmp_path, now=time.time() + sf.STALE_AFTER_SEC + 600)

    assert removed == []
    assert outside.read_text(encoding="utf-8") == "keep"


def test_a_failed_export_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stickers import backup as sb

    store = ss.StickerStore(sf.store_path(tmp_path))
    try:
        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(sf.os, "replace", boom)
        with pytest.raises(OSError):
            sb.export_library(tmp_path)
        monkeypatch.undo()
    finally:
        store.close()

    assert list(sb.backup_dir(tmp_path).iterdir()) == []


def test_the_collector_sweeps_on_its_first_image_and_then_backs_off(tmp_path: Path) -> None:
    store = ss.StickerStore(sf.store_path(tmp_path))
    collector = sc.StickerCollector(
        tmp_path, config=lambda: sc.CollectConfig(enabled=True), open_store=lambda: store,
    )
    incoming = tmp_path / "data" / "attachments" / "room"
    pending = sf.sticker_root(tmp_path) / sf.PENDING_DIR
    first = _make(pending / ".first.png.1.aa.tmp")

    def job(name: str, data: bytes) -> sc._Job:
        return sc._Job(path=_make(incoming / name, data, old=False), marked=True, source="room:1", contributor="UserA")

    try:
        collector._ingest(job("a.png", _png(b"one")))
        assert not first.exists()

        second = _make(pending / ".second.png.1.bb.tmp")
        collector._ingest(job("b.png", _png(b"two")))
        assert second.exists()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_tagger_loop_sweeps_even_while_tagging_is_off(tmp_path: Path) -> None:
    tagger = st.StickerTagger(
        tmp_path, config=lambda: st.TaggerConfig(enabled=False), open_store=lambda: None,
    )
    pending = sf.sticker_root(tmp_path) / sf.PENDING_DIR
    first = _make(pending / ".first.png.1.aa.tmp")

    await tagger._tidy()
    assert not first.exists()

    second = _make(pending / ".second.png.1.bb.tmp")
    await tagger._tidy()
    assert second.exists()
