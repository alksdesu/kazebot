"""在场时的安静等待：非显式消息不逐条问模型，群里安静下来或等满上限，只把最后一条交出去。

轮次和时刻记在 PresenceState 里，试听按同一份规则推演；这里只管运行时的计时与唤醒。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from platforms.ids import normalize_id

from .trigger_policy import PresenceState, QuietWait

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    cycle: int
    latest: asyncio.Future
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    settled: bool = False
    task: asyncio.Task | None = None


def _resolve(future: asyncio.Future, value: QuietWait | None) -> None:
    # 等着的那条可能已经被取消（适配器关停时会取消 handler）
    if not future.done():
        future.set_result(value)


class QuietWaitScheduler:
    """每个群同时最多一轮。等着的是每条消息自己的 handler：到点后还要在它的 matcher 里接着处理。"""

    def __init__(self, presence: PresenceState, *, clock: Callable[[], float] = time.time) -> None:
        self._presence = presence
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    def waiting_groups(self) -> frozenset[str]:
        return frozenset(self._pending)

    async def join(
        self,
        group_id: Any,
        *,
        wait_sec: float,
        max_wait_sec: float,
        now: float | None = None,
    ) -> QuietWait | None:
        """登记一条消息并等到这一轮结束。

        到点时它还是最后一条就拿到这一轮（count 是合并了几条），被后来的盖过、被作废都拿到 None。
        """
        gid = normalize_id(group_id)
        at = self._clock() if now is None else now
        previous = self._presence.quiet(gid)
        wait = self._presence.join_quiet(gid, at, wait_sec=wait_sec, max_wait_sec=max_wait_sec)
        pending = self._pending.get(gid)
        if pending is not None and pending.cycle != wait.cycle:
            # 上一轮已经到点，只是计时还没来得及收：先交给它的最后一条，再开这一轮。
            due = previous if previous is not None and previous.cycle == pending.cycle else None
            self._settle(gid, pending, due)
            pending = None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if pending is None:
            pending = _Pending(cycle=wait.cycle, latest=future)
            self._pending[gid] = pending
            pending.task = asyncio.create_task(self._run(gid, pending))
        else:
            _resolve(pending.latest, None)
            pending.latest = future
            pending.wakeup.set()
        return await future

    def cancel(self, group_id: Any) -> None:
        """作废这个群正在等的一轮，在等的那条拿到 None。没有在等的就什么也不做。"""
        gid = normalize_id(group_id)
        self._presence.cancel_quiet(gid)
        pending = self._pending.get(gid)
        if pending is not None:
            self._settle(gid, pending, None)

    def cancel_all(self) -> None:
        for gid in list(self._pending):
            self.cancel(gid)

    def _settle(self, gid: str, pending: _Pending, wait: QuietWait | None) -> None:
        if pending.settled:
            return
        pending.settled = True
        _resolve(pending.latest, wait)
        pending.wakeup.set()
        if self._pending.get(gid) is pending:
            del self._pending[gid]

    async def _run(self, gid: str, pending: _Pending) -> None:
        try:
            while not pending.settled:
                wait = self._presence.quiet(gid)
                if wait is None or wait.cycle != pending.cycle:
                    self._settle(gid, pending, None)
                    return
                remaining = wait.deadline - self._clock()
                if remaining <= 0:
                    self._settle(gid, pending, self._presence.close_quiet(gid, pending.cycle))
                    return
                pending.wakeup.clear()
                try:
                    await asyncio.wait_for(pending.wakeup.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            self._settle(gid, pending, None)
            raise
        except Exception:
            # 计时坏了也不能让那条消息永远挂着：按被盖过处理，它已经记进群历史了。
            logger.exception("quiet wait timer failed for group %s", gid)
            self._settle(gid, pending, None)
