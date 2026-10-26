"""安静等待的运行时调度：每条消息的 handler 在这里等，到点只有最后一条拿到这一轮。

计时用真钟、只等几十毫秒；需要拨钟的那几条用手动钟，靠下一次登记或作废把计时叫醒。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.quiet_wait import QuietWaitScheduler  # noqa: E402
from adapters._shared.trigger_policy import PresenceState, QuietWait  # noqa: E402

GROUP = "70001"
WAIT = 0.05
CAP = 0.4
# 给计时留的余量：慢机器上一轮调度可能晚几十毫秒。
SLACK = 1.0


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _scheduler(clock=None) -> tuple[QuietWaitScheduler, PresenceState]:
    presence = PresenceState()
    if clock is None:
        return QuietWaitScheduler(presence), presence
    return QuietWaitScheduler(presence, clock=clock), presence


def test_a_lone_message_gets_its_turn_once_the_group_is_quiet() -> None:
    async def main() -> QuietWait | None:
        scheduler, presence = _scheduler()
        result = await asyncio.wait_for(scheduler.join(GROUP, wait_sec=WAIT, max_wait_sec=CAP), SLACK)
        assert presence.quiet(GROUP) is None
        assert scheduler.waiting_groups() == frozenset()
        return result

    result = asyncio.run(main())

    assert isinstance(result, QuietWait) and result.count == 1


def test_only_the_last_message_of_a_burst_gets_the_turn() -> None:
    async def main() -> list[QuietWait | None]:
        scheduler, _ = _scheduler()
        tasks = []
        for _ in range(3):
            tasks.append(asyncio.create_task(scheduler.join(GROUP, wait_sec=WAIT * 4, max_wait_sec=CAP * 4)))
            await asyncio.sleep(0.01)
        return list(await asyncio.wait_for(asyncio.gather(*tasks), SLACK * 2))

    first, second, last = asyncio.run(main())

    assert first is None and second is None
    assert isinstance(last, QuietWait) and last.count == 3


def test_the_cap_ends_a_burst_that_never_goes_quiet() -> None:
    async def main() -> list[QuietWait | None]:
        scheduler, _ = _scheduler()
        tasks = []
        # 每 30 毫秒来一条，安静窗口 80 毫秒永远等不到；最长 200 毫秒必须收一次。
        for _ in range(14):
            tasks.append(asyncio.create_task(scheduler.join(GROUP, wait_sec=0.08, max_wait_sec=0.2)))
            await asyncio.sleep(0.03)
        return list(await asyncio.wait_for(asyncio.gather(*tasks), SLACK * 2))

    results = asyncio.run(main())
    turns = [result for result in results if result is not None]

    assert len(turns) >= 2
    # 真钟是 time.time()，十亿量级的秒数相减只精确到 1e-7 左右
    assert all(turn.deadline - turn.started <= 0.2 + 1e-6 for turn in turns)
    assert sum(turn.count for turn in turns) == len(results)


def test_cancelling_releases_the_waiter_with_nothing() -> None:
    async def main() -> tuple[QuietWait | None, PresenceState, QuietWaitScheduler]:
        scheduler, presence = _scheduler()
        task = asyncio.create_task(scheduler.join(GROUP, wait_sec=5.0, max_wait_sec=20.0))
        await asyncio.sleep(0.01)
        scheduler.cancel(int(GROUP))
        return await asyncio.wait_for(task, SLACK), presence, scheduler

    result, presence, scheduler = asyncio.run(main())

    assert result is None
    assert presence.quiet(GROUP) is None
    assert scheduler.waiting_groups() == frozenset()


def test_cancelling_a_quiet_group_does_nothing() -> None:
    scheduler, presence = _scheduler()

    scheduler.cancel(GROUP)
    scheduler.cancel_all()

    assert presence.quiet(GROUP) is None


def test_cancel_all_releases_every_group() -> None:
    async def main() -> list[QuietWait | None]:
        scheduler, _ = _scheduler()
        tasks = [
            asyncio.create_task(scheduler.join(group, wait_sec=5.0, max_wait_sec=20.0))
            for group in ("70001", "70002", "70003")
        ]
        await asyncio.sleep(0.01)
        assert scheduler.waiting_groups() == frozenset({"70001", "70002", "70003"})
        scheduler.cancel_all()
        return list(await asyncio.wait_for(asyncio.gather(*tasks), SLACK))

    assert asyncio.run(main()) == [None, None, None]


def test_groups_wait_independently() -> None:
    async def main() -> list[QuietWait | None]:
        scheduler, _ = _scheduler()
        first = asyncio.create_task(scheduler.join("70001", wait_sec=WAIT, max_wait_sec=CAP))
        second = asyncio.create_task(scheduler.join("70002", wait_sec=WAIT, max_wait_sec=CAP))
        return list(await asyncio.wait_for(asyncio.gather(first, second), SLACK))

    first, second = asyncio.run(main())

    assert isinstance(first, QuietWait) and isinstance(second, QuietWait)
    assert first.cycle != second.cycle


def test_a_join_after_the_deadline_hands_the_old_turn_over_first() -> None:
    """计时还没来得及醒、下一条已经到了：上一轮先交给它的最后一条，这一条另起一轮。"""
    clock = _Clock(1000.0)

    async def main() -> tuple[QuietWait | None, QuietWait | None]:
        scheduler, _ = _scheduler(clock)
        old = asyncio.create_task(scheduler.join(GROUP, wait_sec=4.0, max_wait_sec=20.0))
        await asyncio.sleep(0.01)
        clock.now = 1010.0
        new = asyncio.create_task(scheduler.join(GROUP, wait_sec=4.0, max_wait_sec=20.0))
        handed = await asyncio.wait_for(old, SLACK)
        scheduler.cancel(GROUP)
        return handed, await asyncio.wait_for(new, SLACK)

    handed, new = asyncio.run(main())

    assert isinstance(handed, QuietWait)
    assert (handed.count, handed.started, handed.deadline) == (1, 1000.0, 1004.0)
    assert new is None


def test_a_waiter_cancelled_by_shutdown_does_not_jam_the_group() -> None:
    async def main() -> QuietWait | None:
        scheduler, _ = _scheduler()
        doomed = asyncio.create_task(scheduler.join(GROUP, wait_sec=WAIT, max_wait_sec=CAP))
        await asyncio.sleep(0.01)
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed
        await asyncio.sleep(WAIT * 3)
        assert scheduler.waiting_groups() == frozenset()
        return await asyncio.wait_for(scheduler.join(GROUP, wait_sec=WAIT, max_wait_sec=CAP), SLACK)

    result = asyncio.run(main())

    assert isinstance(result, QuietWait) and result.count == 1


def test_a_broken_timer_releases_the_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic timer failure")

    # PresenceState 用 __slots__，实例上换不了方法，只能换在类上。
    monkeypatch.setattr(PresenceState, "close_quiet", broken)

    async def main() -> QuietWait | None:
        scheduler, _ = _scheduler()
        return await asyncio.wait_for(scheduler.join(GROUP, wait_sec=WAIT, max_wait_sec=CAP), SLACK)

    assert asyncio.run(main()) is None


def test_the_turn_reports_how_many_messages_it_covers() -> None:
    clock = _Clock(500.0)

    async def main() -> list[QuietWait | None]:
        scheduler, _ = _scheduler(clock)
        tasks = []
        for offset in (0.0, 1.0, 2.0):
            clock.now = 500.0 + offset
            tasks.append(asyncio.create_task(scheduler.join(GROUP, wait_sec=4.0, max_wait_sec=20.0)))
            await asyncio.sleep(0.01)
        # 拨到最后一条的到点之后，再来一条把上一轮交出去。
        clock.now = 520.0
        tasks.append(asyncio.create_task(scheduler.join(GROUP, wait_sec=4.0, max_wait_sec=20.0)))
        await asyncio.sleep(0.01)
        scheduler.cancel(GROUP)
        return list(await asyncio.wait_for(asyncio.gather(*tasks), SLACK))

    results = asyncio.run(main())

    assert results[0] is None and results[1] is None and results[3] is None
    assert isinstance(results[2], QuietWait)
    assert (results[2].count, results[2].deadline) == (3, 506.0)
