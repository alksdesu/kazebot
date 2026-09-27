"""管理接口的重启同一时刻只排上一个；排不上时说得出正在跑的是哪一次、几点开始的。"""
from __future__ import annotations

import threading

from ._helpers import _now
from .types import RestartInProgress


class RestartGate:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._current: RestartInProgress | None = None

    def claim(self, target: str) -> RestartInProgress | None:
        """占下这次重启。已有一次在跑就不占，返回那一次。"""
        with self._guard:
            if self._current is not None:
                return self._current
            self._current = RestartInProgress(target=target, since=_now())
            return None

    def release(self) -> None:
        with self._guard:
            self._current = None

    @property
    def current(self) -> RestartInProgress | None:
        return self._current
