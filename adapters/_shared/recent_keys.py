"""短窗口去重：同一个键在窗口内只认第一次。"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, TypeVar

Key = TypeVar("Key", bound=Hashable)


class RecentKeys(Generic[Key]):
    """记最近见过的键。过了窗口的同一个键当成新的：平台的消息号、文件号迟早会复用。"""

    def __init__(self, *, window_sec: float, max_items: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = float(window_sec)
        self._max_items = max(1, int(max_items))
        self._clock = clock
        self._seen: "OrderedDict[Key, float]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def first(self, key: Key) -> bool:
        """这个键是不是窗口内第一次出现；第一次的同时记下它。"""
        now = self._clock()
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at <= self._window:
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        while len(self._seen) > self._max_items:
            self._seen.popitem(last=False)
        return True
