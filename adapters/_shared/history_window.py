"""频道的最近消息窗口与清空门槛。

逐条编号，对端确认收下后按水位只补新行；被容量挤掉的未送达行记成缺口，提示词里要说明少了几条。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Hashable, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryLine:
    """窗口里的一行。seq 从 1 起逐 1 递增，和对端确认的水位比对。"""

    seq: int
    text: str


@dataclass(frozen=True)
class PendingHistory:
    """这一轮该补给对端的行。covered_seq 是带出后水位该推进到的位置，lost 是被容量挤掉的未送达行数。"""

    watermark: int
    lines: tuple[HistoryLine, ...]
    covered_seq: int
    lost: int


class HistoryWindows:
    """按频道分开的消息窗口。

    容量是硬上限，软上限之上的位置只留给还没送到对端的行。水位由构造方给：谁确认送达，谁知道水位。
    """

    def __init__(
        self,
        *,
        capacity: Callable[[], int],
        soft_cap: Callable[[], int],
        watermark: Callable[[Hashable], int],
    ) -> None:
        self._capacity = capacity
        self._soft_cap = soft_cap
        self._watermark = watermark
        self._windows: dict[Hashable, Deque[HistoryLine]] = {}
        self._last_seq: dict[Hashable, int] = {}
        self._gap: dict[Hashable, int] = {}

    def __getitem__(self, channel: Hashable) -> Deque[HistoryLine]:
        """这个频道的窗口，没有就按当前容量新建。"""
        window = self._windows.get(channel)
        if window is None:
            window = self._windows[channel] = deque(maxlen=self._capacity())
        return window

    def __contains__(self, channel: object) -> bool:
        return channel in self._windows

    def __len__(self) -> int:
        return len(self._windows)

    def channels(self) -> list[Hashable]:
        return list(self._windows)

    def capacities(self) -> list[int]:
        return sorted({int(window.maxlen or 0) for window in self._windows.values()})

    def last_seq(self, channel: Hashable) -> int:
        return self._last_seq.get(channel, 0)

    def watermark(self, channel: Hashable) -> int:
        """对端已确认收到的最大序号；-1 表示这一轮要带完整历史。"""
        return self._watermark(channel)

    def append(self, channel: Hashable, text: str) -> int:
        """写入一行并返回序号。所有写入都必须走这里，序号和缺口才对得上。"""
        seq = self._last_seq.get(channel, 0) + 1
        self._last_seq[channel] = seq
        window = self[channel]
        self._evict_delivered(channel, window)
        if window.maxlen and len(window) == window.maxlen:
            # 容量允许配 0（关掉窗口），那不是缺口，所以先看 maxlen。
            self._note_gap(channel, window[0].seq)
        window.append(HistoryLine(seq=seq, text=text))
        return seq

    def _evict_delivered(self, channel: Hashable, window: Deque[HistoryLine]) -> None:
        soft_cap = max(int(self._soft_cap()), 0)
        watermark = self.watermark(channel)
        while window and len(window) >= soft_cap and window[0].seq <= watermark:
            window.popleft()

    def _note_gap(self, channel: Hashable, dropped_seq: int) -> None:
        if int(dropped_seq) <= self.watermark(channel):
            return
        if channel not in self._gap:
            logger.warning("history window overflow: channel=%s dropped undelivered seq=%s", channel, dropped_seq)
        self._gap[channel] = max(self._gap.get(channel, 0), int(dropped_seq))

    def lost(self, channel: Hashable, watermark: int | None = None) -> int:
        """容量挤掉了几条还没送出去的行。

        seq 从 1 开始逐 1 递增，缺的是 (watermark, gap] 这一段；水位 -1（什么都没送出去）
        要按 0 算，否则第一行会被多数一条。
        """
        mark = self.watermark(channel) if watermark is None else watermark
        return max(0, self._gap.get(channel, 0) - max(int(mark), 0))

    def gapped_channels(self) -> list[Hashable]:
        return [channel for channel in self._gap if self.lost(channel) > 0]

    def pending(self, channel: Hashable) -> PendingHistory:
        watermark = self.watermark(channel)
        lines = tuple(line for line in self[channel] if line.seq > watermark)
        return PendingHistory(
            watermark=watermark,
            lines=lines,
            covered_seq=max((line.seq for line in lines), default=watermark),
            lost=self.lost(channel, watermark),
        )

    def forget_gap(self, channel: Hashable) -> None:
        """水位被重定义时调用：缺口是按旧水位算出来的差值，水位一变它就不再成立。"""
        self._gap.pop(channel, None)

    def drop(self, channel: Hashable) -> None:
        """清掉这个频道的行。序号不归零：对端的水位还按旧序号记着，归零会让新行被当成已送达。"""
        self._windows.pop(channel, None)

    def clear(self) -> None:
        """整体作废（比如换了账号）：行、序号、缺口一起清。"""
        self._windows.clear()
        self._last_seq.clear()
        self._gap.clear()

    def resize(self) -> bool:
        """按当前容量重建每个窗口，返回是否有变。调小时被挤掉的未送达行记进缺口。"""
        target = self._capacity()
        changed = False
        for channel, window in list(self._windows.items()):
            if window.maxlen == target:
                continue
            for line in list(window)[:max(0, len(window) - target)]:
                self._note_gap(channel, line.seq)
            self._windows[channel] = deque(window, maxlen=target)
            changed = True
        return changed


class ClearBarriers:
    """清空上下文那一刻的 inbound 序号。

    清空时还在飞的任务照样会回来投递，带的是上一段上下文的产物，落回刚清空的窗口就等于没清。
    """

    def __init__(self) -> None:
        self.last_inbound_seq = 0
        self._barriers: dict[Hashable, int] = {}

    def __contains__(self, channel: object) -> bool:
        return channel in self._barriers

    def __len__(self) -> int:
        return len(self._barriers)

    def barrier(self, channel: Hashable) -> int:
        return self._barriers.get(channel, 0)

    def note_inbound(self, seq: object) -> None:
        """记下见过的最大 inbound 序号，清空时拿它划界。"""
        value = int(seq or 0)
        if value > self.last_inbound_seq:
            self.last_inbound_seq = value

    def raise_for(self, channel: Hashable, pending_seqs: Iterable[int] = ()) -> None:
        """把清空时刻记成门槛。还在排队的触发也算上一段的，它们的序号一并盖住。"""
        self._barriers[channel] = max([self.last_inbound_seq, *pending_seqs])

    def is_stale(self, channel: Hashable, source_seq: int) -> bool:
        """这次投递是不是清空之前那段上下文的产物。取不到来源序号就放行：宁可多留一条，也不能抹掉正常回复。"""
        barrier = self._barriers.get(channel, 0)
        return bool(barrier) and bool(source_seq) and source_seq <= barrier

    def clear(self) -> None:
        self._barriers.clear()
