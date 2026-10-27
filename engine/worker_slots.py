"""engine 进程手里的任务和并发名额：等人工审批的任务不占名额，连同它们在内的总数另有上限。

主循环据此决定还领不领、领任务时报几个空位；supervisor 拿这个空位判断能不能给后台道的任务。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from toolbox.approval_wait import report_approval_waits_to

logger = logging.getLogger(__name__)

DEFAULT_ACTIVE_FACTOR = 2
MAX_ACTIVE_CEILING = 64


def active_limit(max_workers: int, configured: int) -> int:
    """连同等审批的在内最多几个任务：没配（0）是 max_workers 的两倍，配得比 max_workers 小按 max_workers 算。"""
    workers = max(1, int(max_workers))
    limit = int(configured) if int(configured) > 0 else workers * DEFAULT_ACTIVE_FACTOR
    return max(workers, min(MAX_ACTIVE_CEILING, limit))


@dataclass(frozen=True)
class SlotCounts:
    running: int
    parked: int

    @property
    def total(self) -> int:
        return self.running + self.parked


class TaskSlot:
    """一个领到手的任务。它的工具在等审批时暂不占名额，等完再占回来；任务结束后迟到的报告一概不认。"""

    def __init__(self, owner: "WorkerSlots", task_id: str, lane: str) -> None:
        self._owner = owner
        self.task_id = task_id
        self.lane = lane
        self.approval_waits = 0
        self.released = False

    @property
    def parked(self) -> bool:
        return not self.released and self.approval_waits > 0

    def approval_wait_started(self) -> None:
        self._owner._approval_wait(self, 1)

    def approval_wait_ended(self) -> None:
        self._owner._approval_wait(self, -1)

    def release(self) -> None:
        self._owner._release(self)


class WorkerSlots:
    def __init__(self, *, max_workers: int, max_active: int = 0) -> None:
        self.max_workers = max(1, int(max_workers))
        self.max_active = active_limit(self.max_workers, max_active)
        self._slots: set[TaskSlot] = set()
        # 工具的审批等待可能落在别的线程派生出的上下文里，计数统一过一把锁
        self._lock = threading.Lock()

    def counts(self) -> SlotCounts:
        with self._lock:
            return self._counts_locked()

    def _counts_locked(self) -> SlotCounts:
        parked = sum(1 for slot in self._slots if slot.parked)
        return SlotCounts(running=len(self._slots) - parked, parked=parked)

    def free_slots(self) -> int:
        """现在还能再领几个。等审批的任务恢复后可能让在跑的超过 max_workers，这时是 0，不会把它卡住。"""
        counts = self.counts()
        return max(0, min(self.max_workers - counts.running, self.max_active - counts.total))

    def claim_params(self) -> dict[str, int]:
        """领任务时带给 supervisor 的空位情况。"""
        return {"free_slots": self.free_slots(), "capacity": self.max_workers}

    def start(self, work: Coroutine[Any, Any, Any], *, task_id: str, lane: str = "") -> "asyncio.Task[Any]":
        """在一个新的位置上跑 work：它和它派生的协程里的审批等待都记在这个位置上，跑完交还。"""
        slot = TaskSlot(self, task_id, lane)
        with self._lock:
            self._slots.add(slot)
        runner = _run_in_slot(slot, work)
        try:
            task = asyncio.create_task(runner)
        except BaseException:
            slot.release()
            runner.close()
            work.close()
            raise
        # 还没开始跑就被取消的任务不会走到 _run_in_slot 的 finally
        task.add_done_callback(lambda _done: slot.release())
        return task

    def _approval_wait(self, slot: TaskSlot, delta: int) -> None:
        with self._lock:
            if slot.released:
                return
            was_parked = slot.parked
            slot.approval_waits = max(0, slot.approval_waits + delta)
            now_parked = slot.parked
            counts = self._counts_locked()
        if was_parked != now_parked:
            logger.info(
                "task %s %s approval; running=%d parked=%d",
                slot.task_id[:12], "waiting for" if now_parked else "resumed after", counts.running, counts.parked,
            )

    def _release(self, slot: TaskSlot) -> None:
        with self._lock:
            if slot.released:
                return
            slot.released = True
            slot.approval_waits = 0
            self._slots.discard(slot)


async def _run_in_slot(slot: TaskSlot, work: Coroutine[Any, Any, Any]) -> Any:
    with report_approval_waits_to(slot):
        try:
            return await work
        finally:
            slot.release()
