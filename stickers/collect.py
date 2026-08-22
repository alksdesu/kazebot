"""从群消息里攒表情包。

判定跑在每条群消息上，所以热路径只入队，落盘和查库全在后台单 worker 里做。
队列满了直接丢：少收一张图，好过把消息处理拖慢。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .filter import decide, marked_as_sticker, normalize_strategy, read_shape
from .store import (
    SOURCE_GROUP,
    STATE_LIBRARY,
    STATE_PENDING,
    Sticker,
    StickerStore,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

PENDING_DIR = "pending"
LIBRARY_DIR = "library"

# 队列只是给突发流量削峰的，攒太多说明 worker 跟不上，那时候丢比堆着好。
_QUEUE_MAX = 32
_HEADER_BYTES = 64 * 1024

_EXT_BY_FMT = {
    "png": ".png", "gif": ".gif", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp",
}


@dataclass(frozen=True)
class CollectConfig:
    enabled: bool = False
    strategy: str = "loose"
    groups: tuple[int, ...] = ()
    auto_accept: bool = False
    pending_limit: int = 200
    library_limit: int = 1000
    # 三个上限都是 0 表示不限制，跟配置里"留空即关闭"的惯例一致。
    pending_ttl_sec: int = 7 * 86400
    max_bytes: int = 4 * 1024 * 1024

    def covers(self, group_id: Any) -> bool:
        """留空表示所有已授权的群，不是一个都不收。"""
        if not self.groups:
            return True
        try:
            return int(group_id) in self.groups
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class _Job:
    path: Path
    marked: bool
    from_group: str
    from_user: str


def accept_pending(workspace_root: Path, store: StickerStore, sha256: str) -> bool:
    """待审转在库，文件同时从 pending 搬到 library。控制台和适配器共用这一份。"""
    row = store.get(sha256)
    if row is None or row.state != STATE_PENDING:
        return False
    source = Path(workspace_root) / row.rel_path
    target_dir = sticker_root(workspace_root) / LIBRARY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    try:
        if source.exists():
            shutil.move(str(source), str(target))
    except OSError:
        logger.warning("表情包转正搬文件失败: %s", row.rel_path, exc_info=True)
        return False
    return store.accept(sha256, rel_path=target.relative_to(Path(workspace_root)).as_posix())


def drop_sticker_file(workspace_root: Path, rel_path: str) -> None:
    if not rel_path:
        return
    try:
        (Path(workspace_root) / rel_path).unlink(missing_ok=True)
    except OSError:
        logger.debug("表情包文件删不掉: %s", rel_path)


def sticker_root(workspace_root: Path) -> Path:
    """多开时这个目录是指向共享库的软链，各实例看到同一份图。"""
    return Path(workspace_root) / "data" / "stickers"


def store_path(workspace_root: Path) -> Path:
    return sticker_root(workspace_root) / "stickers.sqlite3"


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def any_marked_segment(segments: Sequence[tuple[str, Mapping[str, Any]]]) -> bool:
    """这条消息里有没有被 QQ 标成表情包的段。附件层只认商城表情，收藏表情要看段。"""
    return any(marked_as_sticker(seg_type, data) for seg_type, data in segments)


class StickerCollector:
    def __init__(
        self,
        workspace_root: Path,
        *,
        config: Callable[[], CollectConfig],
        open_store: Callable[[], StickerStore | None] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self._config = config
        self._open_store = open_store or self._default_store
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task: asyncio.Task[None] | None = None
        self._store: StickerStore | None = None
        self._dropped = 0
        self._last_sweep = 0.0

    def _default_store(self) -> StickerStore | None:
        if self._store is None:
            try:
                self._store = StickerStore(store_path(self.workspace_root))
            except Exception:
                logger.warning("表情包库打不开，收集已停用", exc_info=True)
                return None
        return self._store

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的循环。入队照样算数，等下一次在循环里 submit 时再起 worker。
            # 先探再建：反过来会留下一个没人 await 的协程对象。
            self._task = None
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._store is not None:
            self._store.close()
            self._store = None

    @property
    def dropped(self) -> int:
        return self._dropped

    def submit(
        self,
        attachments: Sequence[Mapping[str, Any]],
        *,
        group_id: Any,
        user_key: str,
        marked: bool,
    ) -> int:
        """热路径入口。只做入队，不碰磁盘也不查库。"""
        config = self._config()
        if not config.enabled or not config.covers(group_id):
            return 0
        queued = 0
        for item in attachments:
            if str(item.get("type") or "") != "image":
                continue
            rel = str(item.get("path") or "").strip()
            if not rel:
                continue
            job = _Job(
                path=self.workspace_root / rel,
                # 附件层已经认出商城表情了，段级判定再补收藏表情那一类。
                marked=bool(item.get("sticker")) or marked,
                from_group=str(group_id or ""),
                from_user=str(user_key or ""),
            )
            try:
                self._queue.put_nowait(job)
                queued += 1
            except asyncio.QueueFull:
                self._dropped += 1
                break
        if queued:
            self.start()
        return queued

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await asyncio.to_thread(self._ingest, job)
            except Exception:
                logger.warning("表情包入库失败: %s", job.path.name, exc_info=True)
            finally:
                self._queue.task_done()

    def _ingest(self, job: _Job) -> None:
        config = self._config()
        store = self._open_store()
        if store is None or not config.enabled:
            return
        try:
            raw = job.path.read_bytes()
        except OSError:
            return
        if not raw or len(raw) > config.max_bytes:
            return

        shape = read_shape(raw[:_HEADER_BYTES])
        ok, why = decide(config.strategy, marked=job.marked, shape=shape)
        if not ok:
            logger.debug("表情包未收: %s (%s)", job.path.name, why)
            return

        sha = digest_of(raw)
        # 已弃的也算见过，否则黑名单挡不住同一张图被反复发进来。
        if store.known(sha):
            return

        self._sweep(store, config)
        state = STATE_LIBRARY if config.auto_accept else STATE_PENDING
        limit = config.library_limit if config.auto_accept else config.pending_limit
        counts = store.counts()
        if limit and counts.get(state, 0) >= limit:
            return

        fmt = shape.fmt if shape else ""
        target_dir = sticker_root(self.workspace_root) / (
            LIBRARY_DIR if state == STATE_LIBRARY else PENDING_DIR
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sha}{_EXT_BY_FMT.get(fmt, job.path.suffix or '.bin')}"
        if not target.exists():
            shutil.copyfile(job.path, target)

        row = store.add(
            sha256=sha,
            rel_path=target.relative_to(self.workspace_root).as_posix(),
            source=SOURCE_GROUP,
            state=state,
            width=shape.width if shape else 0,
            height=shape.height if shape else 0,
            animated=bool(shape and shape.animated),
            fmt=fmt,
            size=len(raw),
            from_group=job.from_group,
            from_user=job.from_user,
        )
        if row is None:
            # 抢先一步的并发写已经登记过了，刚复制的副本没人认领。
            target.unlink(missing_ok=True)

    def _sweep(self, store: StickerStore, config: CollectConfig) -> None:
        """过期待审和超额部分。每分钟最多一次，别让每张图都触发全表扫描。"""
        now = time.monotonic()
        if now - self._last_sweep < 60.0:
            return
        self._last_sweep = now
        stale: list[Sticker] = []
        if config.pending_ttl_sec > 0:
            stale.extend(store.expired_pending(ttl_sec=config.pending_ttl_sec))
        if config.pending_limit:
            stale.extend(store.overflow(STATE_PENDING, keep=config.pending_limit))
        if config.library_limit:
            stale.extend(store.overflow(STATE_LIBRARY, keep=config.library_limit))
        for row in stale:
            self.drop_file(row)
            store.discard(row.sha256)

    def drop_file(self, row: Sticker) -> None:
        drop_sticker_file(self.workspace_root, row.rel_path)

    def accept(self, store: StickerStore, sha256: str) -> bool:
        return accept_pending(self.workspace_root, store, sha256)
