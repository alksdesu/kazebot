"""按会话分道的投递：同一会话严格按提交顺序一条条执行，不同会话互不等待。"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Generic, Hashable, TypeVar

logger = logging.getLogger(__name__)

Job = TypeVar("Job")


class DeliveryLanes(Generic[Job]):
    """每条有活的道一个 worker，道清空即退出，所以 worker 数量跟着积压的会话数走。

    同一个 key 在排队或执行期间不会再被收下：重放扫描和实时接收可能同时递来同一行。
    """

    def __init__(self, run: Callable[[Job], Awaitable[Any]]) -> None:
        self._run = run
        self._queues: dict[str, deque[tuple[Hashable, Job]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._keys: set[Hashable] = set()

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    @property
    def busy(self) -> bool:
        return bool(self._keys)

    def submit(self, lane: str, key: Hashable, job: Job) -> bool:
        """排到这条道的队尾；返回是否收下。"""
        if key in self._keys:
            return False
        self._keys.add(key)
        self._queues.setdefault(lane, deque()).append((key, job))
        worker = self._workers.get(lane)
        if worker is None or worker.done():
            self._workers[lane] = asyncio.create_task(self._drain(lane))
        return True

    async def _drain(self, lane: str) -> None:
        queue = self._queues[lane]
        try:
            while queue:
                key, job = queue[0]
                try:
                    await self._run(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("delivery lane job failed: lane=%s key=%s", lane, key, exc_info=True)
                finally:
                    queue.popleft()
                    self._keys.discard(key)
        finally:
            # 被取消时队里剩下的只是内存里的引用，持久化的行还在，下次重放扫描会再递进来。
            for key, _ in queue:
                self._keys.discard(key)
            queue.clear()
            if self._queues.get(lane) is queue:
                del self._queues[lane]
            if self._workers.get(lane) is asyncio.current_task():
                del self._workers[lane]

    def _running(self) -> list[asyncio.Task[None]]:
        return [worker for worker in self._workers.values() if not worker.done()]

    async def join(self) -> None:
        """等到所有道都清空。"""
        while running := self._running():
            await asyncio.wait(running)

    async def close(self) -> None:
        running = self._running()
        for worker in running:
            worker.cancel()
        if running:
            await asyncio.wait(running)
