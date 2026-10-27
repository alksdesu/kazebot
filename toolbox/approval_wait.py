"""工具等人工审批的这段时间报给所在的 engine worker：任务在等人，不占并发名额。

worker 在每个任务自己的上下文里挂一个监听方，工具和它派生的协程都继承这份上下文；没挂的地方（测试、脚本）什么也不做。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol


class ApprovalWaitListener(Protocol):
    def approval_wait_started(self) -> None: ...

    def approval_wait_ended(self) -> None: ...


_LISTENER: ContextVar[ApprovalWaitListener | None] = ContextVar("approval_wait_listener", default=None)


@contextmanager
def report_approval_waits_to(listener: ApprovalWaitListener | None) -> Iterator[None]:
    """这段代码（以及从这里派生出去的协程）里的审批等待都报给 listener；None 表示不报给任何任务。"""
    token = _LISTENER.set(listener)
    try:
        yield
    finally:
        _LISTENER.reset(token)


class ApprovalWaitRelay:
    """把审批等待转报给另一个监听方，直到 detach：之后的进出都不再转，detach 时还开着的等待一并报结束。

    工具从任务里转到后台继续跑：转之前它在等审批，任务确实在等；转之后再等就和任务无关了。
    """

    def __init__(self, target: ApprovalWaitListener | None) -> None:
        self._target = target
        self._open = 0
        self._lock = threading.Lock()

    def approval_wait_started(self) -> None:
        with self._lock:
            target = self._target
            if target is None:
                return
            self._open += 1
        target.approval_wait_started()

    def approval_wait_ended(self) -> None:
        with self._lock:
            target = self._target
            if target is None or self._open <= 0:
                return
            self._open -= 1
        target.approval_wait_ended()

    def detach(self) -> None:
        with self._lock:
            target, still_open = self._target, self._open
            self._target, self._open = None, 0
        if target is None:
            return
        for _ in range(still_open):
            target.approval_wait_ended()


@contextmanager
def relay_approval_waits() -> Iterator[ApprovalWaitRelay]:
    """这段代码里派生出去的协程，审批等待经一个能中途断开的中转报给当前的监听方。"""
    relay = ApprovalWaitRelay(_LISTENER.get())
    with report_approval_waits_to(relay):
        yield relay


@contextmanager
def waiting_for_approval() -> Iterator[None]:
    """包住一次审批等待：进出各报一次，异常和取消也一样报出去。"""
    listener = _LISTENER.get()
    if listener is None:
        yield
        return
    listener.approval_wait_started()
    try:
        yield
    finally:
        listener.approval_wait_ended()
