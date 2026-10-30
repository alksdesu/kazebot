"""测端点在哪个线程上拿锁：包住原来的锁，记下每次拿到时所在的线程。"""
from __future__ import annotations

import threading
from typing import Any


class ThreadRecordingLock:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.threads: list[int] = []

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        acquired = self.inner.acquire(*args, **kwargs)
        if acquired:
            self.threads.append(threading.get_ident())
        return acquired

    def release(self) -> None:
        self.inner.release()

    def __enter__(self) -> "ThreadRecordingLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()
