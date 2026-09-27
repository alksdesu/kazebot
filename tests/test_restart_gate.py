"""同一时刻只排得上一次重启。

两次重启撞在一起，先收尾的那次会在另一次停起到一半时放开信号抑制 —— 那一刻 engine
已经停了、新的还没起，SIGTERM 不再被吞。占位之外还得记下是哪一次、几点开始，
否则被拒的一方只能回一句「已在进行中」。
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.restart_gate import RestartGate  # noqa: E402
from supervisor.types import RestartInProgress  # noqa: E402


class TestClaiming:
    def test_the_first_one_gets_in(self) -> None:
        gate = RestartGate()

        assert gate.claim("engine") is None

    def test_what_got_in_is_written_down(self) -> None:
        gate = RestartGate()
        before = datetime.now(timezone.utc)

        gate.claim("engine")

        current = gate.current
        assert current is not None
        assert current.target == "engine"
        assert before <= current.since <= datetime.now(timezone.utc)

    def test_the_start_time_carries_its_timezone(self) -> None:
        # 不带时区的话控制台的 new Date() 按浏览器本地时区解释，显示出来差几个小时。
        gate = RestartGate()
        gate.claim("engine")

        assert gate.current.since.utcoffset() == timedelta(0)

    def test_nothing_is_claimed_before_the_first_one(self) -> None:
        assert RestartGate().current is None

    def test_a_second_one_is_handed_the_first(self) -> None:
        gate = RestartGate()
        gate.claim("engine")
        first = gate.current

        assert gate.claim("all") is first

    def test_a_second_one_does_not_overwrite_the_first(self) -> None:
        # 覆盖掉的话，被拒的这次会被报成正在跑的那次，真正卡住的是谁就查不出来了。
        gate = RestartGate()
        gate.claim("engine")
        first = gate.current

        gate.claim("all")

        assert gate.current is first
        assert gate.current.target == "engine"


class TestReleasing:
    def test_it_clears_what_was_claimed(self) -> None:
        gate = RestartGate()
        gate.claim("engine")

        gate.release()

        assert gate.current is None

    def test_the_next_one_can_get_in_afterwards(self) -> None:
        # 漏放一次，这台机器此后再也重启不了 engine。
        gate = RestartGate()
        gate.claim("engine")
        gate.release()

        assert gate.claim("all") is None
        assert gate.current.target == "all"

    def test_releasing_an_idle_gate_is_harmless(self) -> None:
        # 线程没起来那条路会在什么都没占的情况下调它。
        gate = RestartGate()

        gate.release()

        assert gate.current is None


class TestTwoRequestsAtOnce:
    def test_only_one_of_them_gets_in(self) -> None:
        gate = RestartGate()
        racers = 8
        ready = threading.Barrier(racers)
        outcomes: list[RestartInProgress | None] = []
        guard = threading.Lock()

        def _claim(index: int) -> None:
            ready.wait(10)
            taken = gate.claim(f"engine-{index}")
            with guard:
                outcomes.append(taken)

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert len(outcomes) == racers
        assert outcomes.count(None) == 1

    def test_everyone_turned_away_is_pointed_at_the_same_one(self) -> None:
        gate = RestartGate()
        racers = 8
        ready = threading.Barrier(racers)
        outcomes: list[RestartInProgress | None] = []
        guard = threading.Lock()

        def _claim(index: int) -> None:
            ready.wait(10)
            taken = gate.claim(f"engine-{index}")
            with guard:
                outcomes.append(taken)

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert [taken for taken in outcomes if taken is not None] == [gate.current] * (racers - 1)
