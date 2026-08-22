"""从群消息攒表情包。

最要紧的是热路径开销：判定跑在每条群消息上，submit 里但凡碰一次磁盘都会拖慢整个 bot。
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from stickers import collect as sc
from stickers import store as ss


def _png(size: tuple[int, int] = (200, 200), colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def bench(tmp_path: Path):
    """一个工作区 + 一个收集器，配置可以整块换掉。"""
    incoming = tmp_path / "data" / "attachments" / "qq_group_x"
    incoming.mkdir(parents=True)
    holder = {"config": sc.CollectConfig(enabled=True)}
    store = ss.StickerStore(sc.store_path(tmp_path))
    collector = sc.StickerCollector(
        tmp_path, config=lambda: holder["config"], open_store=lambda: store,
    )

    def drop(data: bytes, name: str = "a.png") -> dict[str, str]:
        target = incoming / name
        target.write_bytes(data)
        return {
            "type": "image",
            "path": target.relative_to(tmp_path).as_posix(),
            "mime_type": "image/png",
            "name": name,
        }

    yield type("Bench", (), {
        "root": tmp_path, "store": store, "collector": collector,
        "holder": holder, "drop": staticmethod(drop),
    })
    store.close()


async def _drain(bench, attachments, *, group_id: int = 1, marked: bool = False) -> None:
    bench.collector.submit(attachments, group_id=group_id, user_key="u1", marked=marked)
    await bench.collector._queue.join()


# ── 热路径 ──

def test_submit_does_not_touch_disk(bench) -> None:
    # 入队之外的任何 IO 都会摊到每条群消息上。
    item = bench.drop(_png())
    (bench.root / item["path"]).unlink()
    assert bench.collector.submit([item], group_id=1, user_key="u", marked=False) == 1


def test_submit_ignores_non_images(bench) -> None:
    assert bench.collector.submit(
        [{"type": "file", "path": "data/x.zip"}], group_id=1, user_key="u", marked=False,
    ) == 0


def test_submit_ignores_pathless_entries(bench) -> None:
    assert bench.collector.submit(
        [{"type": "image", "path": ""}], group_id=1, user_key="u", marked=False,
    ) == 0


def test_disabled_collector_takes_nothing(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=False)
    assert bench.collector.submit(
        [bench.drop(_png())], group_id=1, user_key="u", marked=False,
    ) == 0


def test_group_allowlist_is_honoured(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, groups=(42,))
    item = bench.drop(_png())
    assert bench.collector.submit([item], group_id=1, user_key="u", marked=False) == 0
    assert bench.collector.submit([item], group_id=42, user_key="u", marked=False) == 1


def test_empty_allowlist_means_every_group(bench) -> None:
    assert bench.collector.submit(
        [bench.drop(_png())], group_id=999, user_key="u", marked=False,
    ) == 1


def test_full_queue_drops_instead_of_blocking(bench) -> None:
    # 队列满了必须立刻返回，堆积说明 worker 跟不上，等待只会连累消息处理。
    item = bench.drop(_png())
    for _ in range(sc._QUEUE_MAX + 10):
        bench.collector.submit([item], group_id=1, user_key="u", marked=False)
    assert bench.collector.dropped > 0


# ── 入库 ──

@pytest.mark.asyncio
async def test_marked_sticker_is_stored(bench) -> None:
    await _drain(bench, [bench.drop(_png())], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 1


@pytest.mark.asyncio
async def test_stored_file_is_copied_into_the_library_tree(bench) -> None:
    await _drain(bench, [bench.drop(_png())], marked=True)
    row = bench.store.browse()[0]
    assert row.rel_path.startswith("data/stickers/pending/")
    assert (bench.root / row.rel_path).is_file()


@pytest.mark.asyncio
async def test_shape_is_recorded(bench) -> None:
    await _drain(bench, [bench.drop(_png((320, 240)))], marked=True)
    row = bench.store.browse()[0]
    assert (row.width, row.height, row.fmt) == (320, 240, "png")


@pytest.mark.asyncio
async def test_identical_images_are_stored_once(bench) -> None:
    data = _png()
    await _drain(bench, [bench.drop(data, "a.png")], marked=True)
    await _drain(bench, [bench.drop(data, "b.png")], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 1


@pytest.mark.asyncio
async def test_discarded_images_never_come_back(bench) -> None:
    data = _png()
    await _drain(bench, [bench.drop(data, "a.png")], marked=True)
    bench.store.discard(bench.store.browse()[0].sha256)
    await _drain(bench, [bench.drop(data, "b.png")], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 0


@pytest.mark.asyncio
async def test_auto_accept_skips_the_pending_pool(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, auto_accept=True)
    await _drain(bench, [bench.drop(_png())], marked=True)
    row = bench.store.browse()[0]
    assert row.state == ss.STATE_LIBRARY
    assert row.rel_path.startswith("data/stickers/library/")


@pytest.mark.asyncio
async def test_oversized_images_are_skipped(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, max_bytes=10)
    await _drain(bench, [bench.drop(_png())], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 0


@pytest.mark.asyncio
async def test_missing_file_is_not_fatal(bench) -> None:
    item = bench.drop(_png())
    (bench.root / item["path"]).unlink()
    await _drain(bench, [item], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 0


# ── 过滤策略 ──

@pytest.mark.asyncio
async def test_loose_strategy_drops_screenshots(bench) -> None:
    await _drain(bench, [bench.drop(_png((1080, 2340)))], marked=False)
    assert bench.store.counts()[ss.STATE_PENDING] == 0


@pytest.mark.asyncio
async def test_loose_strategy_keeps_marked_screenshots(bench) -> None:
    # QQ 说它是表情包就别再拿尺寸否决。
    await _drain(bench, [bench.drop(_png((1080, 2340)))], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] == 1


@pytest.mark.asyncio
async def test_strict_strategy_needs_the_mark(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, strategy="strict")
    await _drain(bench, [bench.drop(_png())], marked=False)
    assert bench.store.counts()[ss.STATE_PENDING] == 0


@pytest.mark.asyncio
async def test_none_strategy_takes_screenshots_too(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, strategy="none")
    await _drain(bench, [bench.drop(_png((1080, 2340)))], marked=False)
    assert bench.store.counts()[ss.STATE_PENDING] == 1


# ── 配额 ──

@pytest.mark.asyncio
async def test_pending_limit_stops_intake(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, pending_limit=2)
    for index in range(4):
        await _drain(bench, [bench.drop(_png(colour=(index, 0, 0)), f"{index}.png")], marked=True)
    assert bench.store.counts()[ss.STATE_PENDING] <= 2


@pytest.mark.asyncio
async def test_sweep_discards_expired_pending(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, pending_ttl_sec=3600)
    await _drain(bench, [bench.drop(_png(colour=(1, 0, 0)), "a.png")], marked=True)
    first = bench.store.browse()[0]
    bench.store._db.execute("UPDATE stickers SET created_at=created_at-99999")

    bench.collector._last_sweep = 0.0
    await _drain(bench, [bench.drop(_png(colour=(2, 0, 0)), "b.png")], marked=True)
    assert bench.store.state_of(first.sha256) == ss.STATE_DISCARDED
    assert not (bench.root / first.rel_path).exists()


@pytest.mark.asyncio
async def test_zero_ttl_means_no_expiry(bench) -> None:
    # 三个上限都遵循"0 即关闭"，别把它读成"立刻全清"。
    bench.holder["config"] = sc.CollectConfig(
        enabled=True, pending_ttl_sec=0, pending_limit=0, library_limit=0,
    )
    await _drain(bench, [bench.drop(_png(colour=(1, 0, 0)), "a.png")], marked=True)
    first = bench.store.browse()[0]
    bench.store._db.execute("UPDATE stickers SET created_at=0")

    bench.collector._last_sweep = 0.0
    await _drain(bench, [bench.drop(_png(colour=(2, 0, 0)), "b.png")], marked=True)
    assert bench.store.state_of(first.sha256) == ss.STATE_PENDING


# ── 转正 ──

@pytest.mark.asyncio
async def test_accept_moves_the_file(bench) -> None:
    await _drain(bench, [bench.drop(_png())], marked=True)
    row = bench.store.browse()[0]
    assert bench.collector.accept(bench.store, row.sha256)
    moved = bench.store.get(row.sha256)
    assert moved.state == ss.STATE_LIBRARY
    assert moved.rel_path.startswith("data/stickers/library/")
    assert (bench.root / moved.rel_path).is_file()
    assert not (bench.root / row.rel_path).exists()


@pytest.mark.asyncio
async def test_accepting_a_library_item_is_refused(bench) -> None:
    bench.holder["config"] = sc.CollectConfig(enabled=True, auto_accept=True)
    await _drain(bench, [bench.drop(_png())], marked=True)
    row = bench.store.browse()[0]
    assert not bench.collector.accept(bench.store, row.sha256)


def test_accepting_an_unknown_digest_is_refused(bench) -> None:
    assert not bench.collector.accept(bench.store, "f" * 64)


# ── 关停 ──

@pytest.mark.asyncio
async def test_stop_cancels_the_worker(bench) -> None:
    bench.collector.start()
    await asyncio.sleep(0)
    await bench.collector.stop()
    assert bench.collector._task is None
