"""频道历史的溢出账本。

context.py 与 messaging.py 各有一份 push 实现（互不 import），缺口记账只放这里，
避免再多一份复制粘贴。
"""
from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger("ereuna_v2")


def channel_watermark(rt: Any, channel_id: int) -> int:
    """Clonoth 侧已确认收到的最大历史序号；-1 表示这一轮要带完整历史。"""
    if not rt.session_state:
        return -1
    return rt.session_state.get_high_watermark(int(channel_id))


def note_gap(rt: Any, channel_id: int, dropped_seq: int) -> None:
    """记下一条因为队列满而被丢掉的行；只有还没送出去的行才算缺口。"""
    cid = int(channel_id)
    if int(dropped_seq) <= channel_watermark(rt, cid):
        return
    if cid not in rt.history_gap:
        logger.warning(
            "Discord channel history overflow: channel=%s dropped undelivered seq=%s",
            cid, dropped_seq,
        )
    rt.history_gap[cid] = max(rt.history_gap.get(cid, 0), int(dropped_seq))


def lost(rt: Any, channel_id: int, watermark: int) -> int:
    """队列上限吃掉了几条还没送出去的行。

    seq 从 0 开始逐 1 递增，缺的就是 (watermark, gap] 这一段。seq 0 是合法值，因此
    「没记过缺口」只能用 in 判断，不能用 0 代表。
    """
    cid = int(channel_id)
    if cid not in rt.history_gap:
        return 0
    return max(0, rt.history_gap[cid] - max(int(watermark), -1))


def forget_gap(rt: Any, channel_id: int) -> None:
    """水位被重定义后，按旧水位算出来的缺口不再成立。"""
    rt.history_gap.pop(int(channel_id), None)
