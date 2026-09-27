"""表情包发送的通用内核：按名字挑图、按发送计划读图、确认送达后按回执记账、同一会话串行。

平台只给发送计划库、图库句柄和按自己限制读图的函数；把图真正发出去、发成什么样归平台。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping, Optional

from adapters._shared.outbound import ResolvedSticker
from adapters._shared.send_guard import SendContractError, TwoPhaseIdempotencyStore
from stickers.roster import candidates
from stickers.store import Sticker, StickerStore

logger = logging.getLogger(__name__)

_RECEIPT_BATCH = 100


@dataclass(frozen=True)
class StickerDelivery:
    plans: TwoPhaseIdempotencyStore
    open_store: Callable[[], Optional[StickerStore]]
    # 按平台的限制读图：文件不在、太大、格式发不了都返回空字节，挑图时换下一张。
    read: Callable[[Sticker], bytes]

    async def pick(
        self, name: str, *, conversation_key: str, repeat_window_sec: int, selected: set[str] | None = None,
    ) -> ResolvedSticker | None:
        """按名字、再按标签挑一张：同一会话去重窗口内发过的、这条回复里已经挑过的都跳过。

        回执没记完就不挑：刚送达还没记账的图不在去重窗口里，会被再挑一次。
        """
        store = self.open_store()
        if store is None:
            return None
        try:
            if not await self.settle():
                return None
            skip = store.recently_sent(conversation_key, within_sec=repeat_window_sec) if conversation_key else set()
            if selected is not None:
                skip.update(selected)
            for row in candidates(store, name, skip=skip):
                raw = await asyncio.to_thread(self.read, row)
                if raw:
                    if selected is not None:
                        selected.add(row.sha256)
                    return ResolvedSticker(row.sha256, hashlib.sha256(raw).hexdigest())
            return None
        except Exception:
            logger.warning("表情包取图失败: %s", name, exc_info=True)
            return None

    async def load(self, segment: Mapping[str, Any]) -> bytes:
        """按发送计划读出选中的图；内容和挑中时对不上就拒发，不自动换图。"""
        store = self.open_store()
        row = store.get(str(segment.get("sha256") or "")) if store is not None else None
        raw = await asyncio.to_thread(self.read, row) if row is not None else b""
        if not raw or hashlib.sha256(raw).hexdigest() != segment.get("content_sha256"):
            raise SendContractError("已选表情包不可用，保持原投递内容且不自动换图")
        return raw

    async def settle(self, delivery_id: str = "") -> bool:
        """把确认送达、还没记账的计划记进图库。都记完返回 True；图库打不开或记账出错返回 False，下次再记。"""
        try:
            receipts = await self.plans.pending_message_receipts(limit=_RECEIPT_BATCH, delivery_id=delivery_id)
            if not receipts:
                return True
            for receipt in receipts:
                store = self.open_store() if receipt.selected_images else None
                if receipt.selected_images and store is None:
                    return False
                for digest in receipt.selected_images:
                    await asyncio.to_thread(
                        store.record_sent, receipt.conversation_key, digest,
                        delivery_id=receipt.delivery_id, sent_at=int(receipt.confirmed_at),
                    )
                await self.plans.ack_message_receipt(receipt.delivery_id)
            return not await self.plans.pending_message_receipts(limit=1, delivery_id=delivery_id)
        except Exception:
            logger.warning("表情包发送回执记账失败，稍后重试", exc_info=True)
            return False


class ConversationLocks:
    """同一会话同时只有一条回复在挑图、发图，去重窗口才靠得住。没人用的锁随手删掉，不随会话数增长。"""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @contextlib.asynccontextmanager
    async def hold(self, conversation_key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(conversation_key, (asyncio.Lock(), 0))
        self._locks[conversation_key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[conversation_key]
            if users == 1:
                del self._locks[conversation_key]
            else:
                self._locks[conversation_key] = (lock, users - 1)
