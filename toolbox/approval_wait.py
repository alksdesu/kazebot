"""工具等人工审批的这段时间报给所在的 engine worker：任务在等人，不占并发名额。

worker 在每个任务自己的上下文里挂一个监听方，工具和它派生的协程都继承这份上下文；没挂的地方（测试、脚本）什么也不做。
"""
from __future__ import annotations

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
