"""持有租约期间在后台续租：投递队列、发送防重 claim、消息计划共用这一份续租循环。"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger(__name__)


async def renew_until_lost(
    renew: Callable[[], Any],
    *,
    interval: float,
    lost: tuple[type[BaseException], ...] = (),
    label: str = "",
) -> None:
    """每隔 interval 秒调一次 renew，直到被取消、租约丢失或续租出错。

    renew 可以是同步或异步函数，返回 False 表示租约已不在自己手里。异常一律在这里收住：
    续租失败只该让续租停下，不该从持有者的 finally 里冒出来盖掉真正的发送结果。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            result = renew()
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except lost:
            return
        except Exception:
            logger.warning("lease renewal failed, heartbeat stopped: %s", label, exc_info=True)
            return
        if result is False:
            return


@contextlib.asynccontextmanager
async def lease_heartbeat(
    renew: Callable[[], Any],
    *,
    interval: float,
    lost: tuple[type[BaseException], ...] = (),
    label: str = "",
) -> AsyncIterator[None]:
    """块内持续续租，离开时停掉续租并等它真正退出。"""
    task = asyncio.create_task(renew_until_lost(renew, interval=interval, lost=lost, label=label))
    try:
        yield
    finally:
        task.cancel()
        # asyncio.wait 不吞调用方自己的取消，也不重抛续租任务的结果。
        await asyncio.wait({task})
