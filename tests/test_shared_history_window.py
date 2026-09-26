"""共享层的消息窗口与清空门槛：编号、按水位让位、缺口记账、调容量、门槛判定。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Hashable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.history_window import ClearBarriers, HistoryWindows, PendingHistory  # noqa: E402

_CHANNEL = "room-1"


class _Knobs:
    """容量、软上限和水位都由调用方给；测试里手动拨。"""

    def __init__(self, *, capacity: int = 6, soft_cap: int = 3) -> None:
        self.capacity = capacity
        self.soft_cap = soft_cap
        self.marks: dict[Hashable, int] = {}

    def watermark(self, channel: Hashable) -> int:
        return self.marks.get(channel, -1)

    def windows(self) -> HistoryWindows:
        return HistoryWindows(
            capacity=lambda: self.capacity, soft_cap=lambda: self.soft_cap, watermark=self.watermark,
        )


def _seqs(windows: HistoryWindows, channel: Hashable = _CHANNEL) -> list[int]:
    return [line.seq for line in windows[channel]]


def _fill(windows: HistoryWindows, count: int, channel: Hashable = _CHANNEL) -> None:
    for index in range(count):
        windows.append(channel, f"line{index + 1}")


class TestNumbering:
    def test_each_channel_counts_from_one(self) -> None:
        windows = _Knobs().windows()

        assert [windows.append("a", "x"), windows.append("a", "y"), windows.append("b", "z")] == [1, 2, 1]
        assert windows.last_seq("a") == 2 and windows.last_seq("missing") == 0

    def test_numbering_survives_dropping_the_lines(self) -> None:
        # 对端的水位还按旧序号记着：序号归零的话，新行会被当成早已送达。
        windows = _Knobs().windows()
        windows.append(_CHANNEL, "a")

        windows.drop(_CHANNEL)

        assert _CHANNEL not in windows
        assert windows.append(_CHANNEL, "b") == 2

    def test_clearing_starts_over(self) -> None:
        windows = _Knobs(capacity=2, soft_cap=2).windows()
        _fill(windows, 4)

        windows.clear()

        assert len(windows) == 0 and windows.lost(_CHANNEL, -1) == 0
        assert windows.append(_CHANNEL, "again") == 1

    def test_a_new_window_takes_the_current_capacity(self) -> None:
        knobs = _Knobs(capacity=4)
        windows = knobs.windows()

        assert windows["fresh"].maxlen == 4
        assert windows.capacities() == [4]


class TestEviction:
    def test_delivered_lines_make_room_past_the_soft_cap(self) -> None:
        knobs = _Knobs(capacity=6, soft_cap=3)
        windows = knobs.windows()
        _fill(windows, 5)
        knobs.marks[_CHANNEL] = 5

        windows.append(_CHANNEL, "line6")

        assert _seqs(windows) == [4, 5, 6]
        assert windows.lost(_CHANNEL) == 0

    def test_undelivered_lines_keep_their_place_up_to_the_hard_cap(self) -> None:
        windows = _Knobs(capacity=6, soft_cap=3).windows()

        _fill(windows, 8)

        assert _seqs(windows) == [3, 4, 5, 6, 7, 8]
        assert windows.lost(_CHANNEL) == 2

    def test_a_capacity_of_zero_is_not_a_gap(self) -> None:
        windows = _Knobs(capacity=0, soft_cap=0).windows()

        _fill(windows, 3)

        assert _seqs(windows) == []
        assert windows.lost(_CHANNEL) == 0

    def test_the_first_overflow_is_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        windows = _Knobs(capacity=2, soft_cap=2).windows()

        with caplog.at_level(logging.WARNING, logger="adapters._shared.history_window"):
            _fill(windows, 5)

        assert len([record for record in caplog.records if "overflow" in record.getMessage()]) == 1


class TestPending:
    def test_only_lines_past_the_watermark_are_pending(self) -> None:
        knobs = _Knobs()
        windows = knobs.windows()
        _fill(windows, 4)
        knobs.marks[_CHANNEL] = 2

        pending = windows.pending(_CHANNEL)

        assert [line.seq for line in pending.lines] == [3, 4]
        assert (pending.watermark, pending.covered_seq, pending.lost) == (2, 4, 0)

    def test_nothing_new_keeps_the_watermark_in_place(self) -> None:
        knobs = _Knobs()
        windows = knobs.windows()
        _fill(windows, 2)
        knobs.marks[_CHANNEL] = 2

        assert windows.pending(_CHANNEL) == PendingHistory(watermark=2, lines=(), covered_seq=2, lost=0)

    def test_the_gap_is_counted_from_the_watermark(self) -> None:
        # 水位 -1 表示什么都没送出去，按 0 算，否则第一行会被多数一条。
        windows = _Knobs(capacity=3, soft_cap=3).windows()
        _fill(windows, 5)

        assert windows.lost(_CHANNEL, -1) == 2
        assert windows.lost(_CHANNEL, 1) == 1
        assert windows.gapped_channels() == [_CHANNEL]

    def test_forgetting_the_gap(self) -> None:
        windows = _Knobs(capacity=3, soft_cap=3).windows()
        _fill(windows, 5)

        windows.forget_gap(_CHANNEL)

        assert windows.lost(_CHANNEL, -1) == 0
        assert windows.gapped_channels() == []


class TestResize:
    def test_shrinking_records_the_undelivered_lines_it_drops(self) -> None:
        knobs = _Knobs(capacity=6, soft_cap=6)
        windows = knobs.windows()
        _fill(windows, 5)
        knobs.capacity = 3

        assert windows.resize() is True
        assert _seqs(windows) == [3, 4, 5]
        assert windows.lost(_CHANNEL) == 2
        assert windows.last_seq(_CHANNEL) == 5

    def test_shrinking_past_delivered_lines_is_not_a_gap(self) -> None:
        knobs = _Knobs(capacity=6, soft_cap=6)
        windows = knobs.windows()
        _fill(windows, 5)
        knobs.marks[_CHANNEL] = 5
        knobs.capacity = 3

        windows.resize()

        assert windows.lost(_CHANNEL) == 0

    def test_an_unchanged_capacity_keeps_the_same_window(self) -> None:
        windows = _Knobs().windows()
        window = windows[_CHANNEL]

        assert windows.resize() is False
        assert windows[_CHANNEL] is window


class TestClearBarriers:
    def test_the_barrier_lands_on_the_highest_seq_seen(self) -> None:
        barriers = ClearBarriers()
        barriers.note_inbound(40)
        barriers.note_inbound(12)
        barriers.note_inbound(None)

        barriers.raise_for(_CHANNEL)

        assert barriers.barrier(_CHANNEL) == 40

    def test_triggers_still_queued_raise_it_further(self) -> None:
        barriers = ClearBarriers()
        barriers.note_inbound(40)

        barriers.raise_for(_CHANNEL, [57, 3])

        assert barriers.barrier(_CHANNEL) == 57

    @pytest.mark.parametrize(("source_seq", "stale"), [(40, True), (12, True), (41, False), (0, False)])
    def test_deliveries_at_or_below_the_barrier_are_stale(self, source_seq: int, stale: bool) -> None:
        barriers = ClearBarriers()
        barriers.note_inbound(40)
        barriers.raise_for(_CHANNEL)

        assert barriers.is_stale(_CHANNEL, source_seq) is stale

    def test_a_channel_without_a_barrier_is_never_stale(self) -> None:
        assert ClearBarriers().is_stale(_CHANNEL, 5) is False

    def test_clearing_keeps_the_highest_seq_seen(self) -> None:
        barriers = ClearBarriers()
        barriers.note_inbound(40)
        barriers.raise_for(_CHANNEL)

        barriers.clear()

        assert len(barriers) == 0 and _CHANNEL not in barriers
        assert barriers.last_inbound_seq == 40
