"""共享表情包库里几个进程同时登记同一张图。

输掉登记的一方不能把赢家的文件删掉，读图的一方也不能看到写了一半的图。收群图和图床同步走的是同一份落盘实现。
"""
from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers import collect as sc
from stickers import files as sf
from stickers import imagebed as ib
from stickers import store as ss


def _png(tag: bytes) -> bytes:
    """最小的 PNG 头：判定和取宽高只看文件头，后面拼上 tag 让每张图的哈希各不相同。"""
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
    return header + (64).to_bytes(4, "big") + (64).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + tag


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _temp_files(root: Path) -> list[Path]:
    base = sf.sticker_root(root)
    return [path for path in base.rglob("*") if path.name.endswith(".tmp")] if base.exists() else []


class _GatedStore:
    """登记前停住，等另一个进程先登记完再接着走，把交错顺序钉死。"""

    def __init__(self, store: ss.StickerStore, *, arrived: threading.Event, release: threading.Event) -> None:
        self._store = store
        self._arrived = arrived
        self._release = release

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def add(self, **fields):
        self._arrived.set()
        if not self._release.wait(10):
            raise TimeoutError("另一个进程一直没登记完")
        return self._store.add(**fields)


class _BeatenStore:
    """登记前一刻，另一个进程抢先登记了同一张图。winner 给出它登记时用的字段。"""

    def __init__(self, store: ss.StickerStore, winner) -> None:
        self._store = store
        self._winner = winner

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def add(self, **fields):
        self._store.add(**self._winner(fields))
        return self._store.add(**fields)


@pytest.fixture()
def library(tmp_path: Path):
    """同一个库文件开两个句柄，各算一个进程。"""
    first = ss.StickerStore(sf.store_path(tmp_path))
    second = ss.StickerStore(sf.store_path(tmp_path))
    yield tmp_path, first, second
    first.close()
    second.close()


def test_the_loser_keeps_the_winners_file_when_both_land_on_the_same_path(library) -> None:
    root, winner_store, loser_store = library
    payload = _png(b"same-path")
    digest = _digest(payload)
    location = f"{sf.PENDING_DIR}/{digest}.png"
    arrived, release = threading.Event(), threading.Event()
    gated = _GatedStore(loser_store, arrived=arrived, release=release)
    outcome: dict[str, object] = {}

    def loser() -> None:
        # 先动手的是输家：文件是它新建的，登记却晚了一步。
        outcome["row"] = sf.place(root, gated, payload, location=location, sha256=digest, source=ss.SOURCE_GROUP)

    thread = threading.Thread(target=loser)
    thread.start()
    try:
        assert arrived.wait(10)
        won = sf.place(root, winner_store, payload, location=location, sha256=digest, source=ss.SOURCE_GROUP)
    finally:
        release.set()
        thread.join(10)

    assert won is not None
    assert outcome["row"] is None
    target = root / won.rel_path
    assert target.read_bytes() == payload
    assert winner_store.get(digest).rel_path == f"data/stickers/{location}"
    assert _temp_files(root) == []


def test_a_loser_that_landed_elsewhere_only_removes_its_own_fresh_copy(library) -> None:
    root, winner_store, loser_store = library
    payload = _png(b"elsewhere")
    digest = _digest(payload)
    library_copy = sf.sticker_root(root) / sf.LIBRARY_DIR / f"{digest}.png"
    arrived, release = threading.Event(), threading.Event()
    gated = _GatedStore(loser_store, arrived=arrived, release=release)
    outcome: dict[str, object] = {}

    def loser() -> None:
        outcome["row"] = sf.place(
            root, gated, payload, location=f"{sf.PENDING_DIR}/{digest}.png", sha256=digest,
            source=ss.SOURCE_GROUP, state=ss.STATE_PENDING,
        )

    thread = threading.Thread(target=loser)
    thread.start()
    try:
        assert arrived.wait(10)
        won = sf.place(
            root, winner_store, payload, location=f"{sf.LIBRARY_DIR}/{digest}.png", sha256=digest,
            source=ss.SOURCE_GROUP, state=ss.STATE_LIBRARY,
        )
    finally:
        release.set()
        thread.join(10)

    assert won is not None and outcome["row"] is None
    assert library_copy.read_bytes() == payload
    assert not (sf.sticker_root(root) / sf.PENDING_DIR / f"{digest}.png").exists()


def test_a_loser_never_deletes_a_file_it_did_not_create(library) -> None:
    root, store, _second = library
    payload = _png(b"pre-existing")
    digest = _digest(payload)
    leftover = sf.sticker_root(root) / sf.PENDING_DIR / f"{digest}.png"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(payload)
    store.add(
        sha256=digest, rel_path=f"data/stickers/{sf.LIBRARY_DIR}/{digest}.png",
        source=ss.SOURCE_GROUP, state=ss.STATE_LIBRARY,
    )

    row = sf.place(root, store, payload, location=f"{sf.PENDING_DIR}/{digest}.png", sha256=digest,
                   source=ss.SOURCE_GROUP)

    assert row is None
    assert leftover.read_bytes() == payload


def test_a_write_that_fails_leaves_neither_a_half_image_nor_a_temp_file(
    library, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _second = library
    payload = _png(b"disk-full")
    digest = _digest(payload)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sf.os, "replace", boom)
    with pytest.raises(OSError):
        sf.place(root, store, payload, location=f"{sf.PENDING_DIR}/{digest}.png", sha256=digest,
                 source=ss.SOURCE_GROUP)
    monkeypatch.undo()

    pending = sf.sticker_root(root) / sf.PENDING_DIR
    assert list(pending.iterdir()) == []
    assert not store.known(digest)


def test_a_location_outside_the_image_folders_is_refused_before_anything_is_written(library) -> None:
    root, store, _second = library
    payload = _png(b"escape")
    digest = _digest(payload)
    for location in ("../evil.png", "stickers.sqlite3", f"{sf.BACKUP_DIR}/{digest}.png"):
        with pytest.raises(ValueError):
            sf.place(root, store, payload, location=location, sha256=digest, source=ss.SOURCE_GROUP)
    assert not store.known(digest)
    assert not (root / "data" / "evil.png").exists()
    assert not (sf.sticker_root(root) / sf.BACKUP_DIR).exists()


# ── 两个收图的入口都走这一份 ──

def _job(root: Path, payload: bytes, name: str) -> sc._Job:
    incoming = root / "data" / "attachments" / "room"
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / name
    path.write_bytes(payload)
    return sc._Job(path=path, marked=True, source="room:1", contributor="UserA")


def test_two_collectors_racing_on_one_image_keep_the_file(library) -> None:
    root, winner_store, loser_store = library
    payload = _png(b"group-race")
    digest = _digest(payload)
    arrived, release = threading.Event(), threading.Event()
    gated = _GatedStore(loser_store, arrived=arrived, release=release)
    config = sc.CollectConfig(enabled=True)
    loser = sc.StickerCollector(root, config=lambda: config, open_store=lambda: gated)
    winner = sc.StickerCollector(root, config=lambda: config, open_store=lambda: winner_store)

    thread = threading.Thread(target=loser._ingest, args=(_job(root, payload, "a.png"),))
    thread.start()
    try:
        assert arrived.wait(10)
        winner._ingest(_job(root, payload, "b.png"))
    finally:
        release.set()
        thread.join(10)

    row = winner_store.get(digest)
    assert row is not None
    assert (root / row.rel_path).read_bytes() == payload


def test_a_collector_beaten_to_the_same_path_keeps_the_file(library) -> None:
    root, store, _second = library
    payload = _png(b"beaten-collector")
    digest = _digest(payload)
    beaten = _BeatenStore(store, lambda fields: fields)
    collector = sc.StickerCollector(root, config=lambda: sc.CollectConfig(enabled=True), open_store=lambda: beaten)

    collector._ingest(_job(root, payload, "a.png"))

    row = store.get(digest)
    assert row is not None
    assert (root / row.rel_path).read_bytes() == payload


def test_an_imagebed_sync_beaten_to_the_same_path_keeps_the_file(library) -> None:
    root, store, _second = library
    payload = _png(b"beaten-imagebed")
    digest = _digest(payload)
    beaten = _BeatenStore(store, lambda fields: fields)
    cfg = ib.ImagebedConfig(
        endpoint="https://s3.example.com", bucket="memes", access_key_id="key", secret_access_key="secret",
    )

    class _Client:
        async def fetch(self, _obj):
            return payload

    outcome = asyncio.run(ib._ingest(_Client(), cfg, beaten, root, ib.RemoteObject(key="a.png", size=len(payload))))

    assert outcome == "known"
    row = store.get(digest)
    assert row.rel_path == f"data/stickers/{ib.relative_path(digest, 'png')}"
    assert (root / row.rel_path).read_bytes() == payload


# ── 真的两个进程 ──

def _place_from_process(root: str, payload: bytes, barrier, index: int) -> None:
    store = ss.StickerStore(sf.store_path(root))
    try:
        digest = _digest(payload)
        barrier.wait(60)
        row = sf.place(
            root, store, payload, location=f"{sf.PENDING_DIR}/{digest}.png", sha256=digest,
            source=ss.SOURCE_GROUP,
        )
        Path(root, f"outcome-{index}").write_text("row" if row is not None else "none", encoding="utf-8")
    finally:
        store.close()


def test_processes_registering_one_image_leave_exactly_one_intact_file(tmp_path: Path) -> None:
    payload = _png(b"processes") * 1024
    digest = _digest(payload)
    ss.StickerStore(sf.store_path(tmp_path)).close()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes = [
        context.Process(target=_place_from_process, args=(str(tmp_path), payload, barrier, index))
        for index in range(3)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            process.close()

    outcomes = sorted((tmp_path / f"outcome-{index}").read_text(encoding="utf-8") for index in range(3))
    assert outcomes == ["none", "none", "row"]
    store = ss.StickerStore(sf.store_path(tmp_path))
    try:
        row = store.get(digest)
        assert (tmp_path / row.rel_path).read_bytes() == payload
    finally:
        store.close()
    assert _temp_files(tmp_path) == []
