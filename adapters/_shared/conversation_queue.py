"""待处理 inbound 的排队：同一会话同一时刻只处理一件，排队期间同一身份的追加消息合进还没开始的那件。"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable, Deque, Generic, Hashable, Iterator, TypeVar

Item = TypeVar("Item")


class ConversationQueue(Generic[Item]):
    """按到达顺序排队，但跳过正在处理的会话：同一会话的下一件要等上一件处理完。"""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._items: Deque[tuple[Hashable, Item]] = deque()
        # 会话 → 还没开始处理、后续消息可以合进去的那件，连同它入队时的合并身份。
        self._open: dict[Hashable, tuple[Item, Hashable | None]] = {}
        self._active: set[Hashable] = set()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Item]:
        return iter([item for _, item in self._items])

    def open_item(self, key: Hashable) -> Item | None:
        """这个会话里后续消息会合进去的那件；没有就是 None。"""
        entry = self._open.get(key)
        return entry[0] if entry is not None else None

    def active_keys(self) -> frozenset[Hashable]:
        return frozenset(self._active)

    async def put(
        self,
        key: Hashable,
        item: Item,
        *,
        identity_of: Callable[[Item], Hashable | None],
        merge: Callable[[Item, Item], None],
    ) -> None:
        """排进队尾；同会话里还没开始的那件合并身份相同，就合进那件。

        身份持锁现算：它可能读权限这类随时会变的状态，得和合并判定落在同一刻。身份为 None 不参与合并。
        """
        async with self._condition:
            identity = identity_of(item)
            entry = self._open.get(key)
            if entry is not None and identity is not None and entry[1] == identity:
                merge(entry[0], item)
            else:
                self._items.append((key, item))
                self._open[key] = (item, identity)
            self._condition.notify()

    async def take(self, should_exit: Callable[[], bool]) -> tuple[Hashable, Item] | None:
        """取最早一件会话空闲的并把会话标成处理中。should_exit 持锁判定，为真返回 None。"""
        async with self._condition:
            while True:
                if should_exit():
                    return None
                index = next((index for index, (key, _) in enumerate(self._items) if key not in self._active), None)
                if index is not None:
                    break
                await self._condition.wait()
            key, item = self._items[index]
            del self._items[index]
            self._active.add(key)
            # 只摘指向本件的合并入口：同会话已经排着后来的那件时，入口指向的是它。
            entry = self._open.get(key)
            if entry is not None and entry[0] is item:
                self._open.pop(key, None)
            return key, item

    async def release(self, key: Hashable) -> None:
        """这个会话处理完了，叫醒所有等着的 worker。"""
        async with self._condition:
            self._active.discard(key)
            self._condition.notify_all()

    async def wake_all(self) -> None:
        """worker 数量变了，让所有等着的 worker 重新判一次要不要退。"""
        async with self._condition:
            self._condition.notify_all()
