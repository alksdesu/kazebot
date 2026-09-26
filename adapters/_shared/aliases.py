"""匿名别名表：真实 ID ↔ UserA / GroupB 这类稳定别名，落盘、按保留期回收、在文本里替换。

编号只增不回收：复用会让旧记忆里的 UserA 指向另一个人。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from engine import memory_subjects
from platforms.ids import id_text_pattern

logger = logging.getLogger(__name__)

USER_PREFIX = "User"
GROUP_PREFIX = "Group"
# 每次命中都写盘会把节流窗口打满，同一小时内只记一次就够保留期判定用。
TOUCH_MIN_INTERVAL = 3600.0
FORMAT_VERSION = 1


def alias_from_index(prefix: str, index: int) -> str:
    """0 → A，25 → Z，26 → AA：Excel 列号式的字母编号。"""
    letters = ""
    index = max(0, index)
    while True:
        index, rem = divmod(index, 26)
        letters = chr(ord("A") + rem) + letters
        if index == 0:
            break
        index -= 1
    return f"{prefix}{letters}"


def alias_to_index(prefix: str, alias: str) -> int:
    """alias_from_index 的反函数；不是这个前缀下的合法别名返回 -1。"""
    if not alias.startswith(prefix):
        return -1
    letters = alias[len(prefix):]
    if not letters or not letters.isalpha() or not letters.isupper():
        return -1
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


class AliasTable:
    """一类对象（人或群）的别名：正反两张表、最后出现时间、下一个编号。"""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.unknown = f"{prefix}Unknown"
        self.next_index = 0
        self._aliases: dict[str, str] = {}
        self._reals: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._aliases)

    def __contains__(self, real: object) -> bool:
        return real in self._aliases

    def alias_of(self, real: str) -> str | None:
        return self._aliases.get(real)

    def real_of(self, alias: str) -> str | None:
        return self._reals.get(alias)

    def items(self) -> Iterator[tuple[str, str]]:
        """(真实 ID, 别名)，按登记顺序。"""
        return iter(self._aliases.items())

    def last_seen(self, real: str) -> float | None:
        return self._last_seen.get(real)

    def assign(self, real: str) -> tuple[str, bool]:
        """取已有别名，没有就发下一个编号。第二项表示是不是新发的。"""
        alias = self._aliases.get(real)
        if alias is not None:
            return alias, False
        alias = alias_from_index(self.prefix, self.next_index)
        self.next_index += 1
        self._aliases[real] = alias
        self._reals[alias] = real
        return alias, True

    def touch(self, real: str, now: float) -> bool:
        """刷新最后出现时间，节流到 TOUCH_MIN_INTERVAL 一次；返回是否真的改了。"""
        if now - self._last_seen.get(real, 0.0) > TOUCH_MIN_INTERVAL:
            self._last_seen[real] = now
            return True
        return False

    def restore(self, real: str, alias: str, last_seen: float) -> None:
        self._aliases[real] = alias
        self._reals[alias] = real
        self._last_seen[real] = last_seen

    def resume_numbering(self, persisted_next: Any) -> None:
        """文件里存的下一个编号和已用最大编号取大者；旧文件没有这个字段就只看已用编号。"""
        highest = max((alias_to_index(self.prefix, alias) for alias in self._reals), default=-1)
        try:
            persisted = int(persisted_next or 0)
        except (TypeError, ValueError, OverflowError):
            persisted = 0
        self.next_index = max(0, persisted, highest + 1)

    def prune(self, cutoff: float, protected: frozenset[str]) -> int:
        """删掉 cutoff 之前就没再出现的条目，protected 里的别名不动；编号不回收。"""
        stale = [
            real for real, alias in self._aliases.items()
            if alias not in protected and self._last_seen.get(real, 0.0) < cutoff
        ]
        for real in stale:
            alias = self._aliases.pop(real)
            self._last_seen.pop(real, None)
            self._reals.pop(alias, None)
        return len(stale)

    def payload(self, now: float) -> dict[str, dict[str, Any]]:
        return {
            real: {"alias": alias, "last_seen": self._last_seen.get(real, now)}
            for real, alias in self._aliases.items()
        }


@dataclass(frozen=True)
class AliasLoad:
    """一次加载的结果，给调用方写日志用。rejected 是被调用方的过滤器挡掉的人。"""

    users: int
    groups: int
    pruned: int
    rejected: int


def _accept_all(real: str) -> bool:
    return True


class AliasBook:
    """人和群两张别名表，连同落盘节流和文本替换。一个平台一个实例，挂在适配器状态上。"""

    def __init__(
        self,
        path: Path | str,
        *,
        workspace: Path,
        id_shape: str,
        retention_days: float = 0.0,
        save_min_interval: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.users = AliasTable(USER_PREFIX)
        self.groups = AliasTable(GROUP_PREFIX)
        self.dirty = False
        self.last_saved_at = 0.0
        self._workspace = Path(workspace)
        self._id_shape = id_shape
        self._retention_days = retention_days
        self._save_min_interval = save_min_interval
        self._clock = clock
        self._lock = asyncio.Lock()
        self._save_task: asyncio.Task[None] | None = None
        self._pattern: re.Pattern[str] | None = None
        self._replacements: dict[str, str] = {}

    @property
    def save_pending(self) -> bool:
        return self._save_task is not None and not self._save_task.done()

    def user_alias(self, real: str) -> str:
        return self._alias(self.users, real)

    def group_alias(self, real: str) -> str:
        return self._alias(self.groups, real)

    def _alias(self, table: AliasTable, real: str) -> str:
        real = str(real or "").strip()
        if not real:
            return table.unknown
        alias, created = table.assign(real)
        if created:
            self._invalidate_pattern()
            self.mark_dirty()
        if table.touch(real, self._clock()):
            self.mark_dirty()
        return alias

    def substitute(self, text: str) -> str:
        """把文本里已登记的真实 ID 换成别名。人和群的 ID 字面相同时按群换。"""
        if not text:
            return ""
        safe = str(text)
        pattern = self._text_pattern()
        if pattern is None:
            return safe
        return pattern.sub(lambda match: self._replacements[match.group(0)], safe)

    def _text_pattern(self) -> re.Pattern[str] | None:
        # 编成一条备选正则缓存起来：逐条 re.sub 在表超过 re 的 512 条编译缓存后每次都要重编。
        if self._pattern is not None:
            return self._pattern
        mapping = dict(self.users.items())
        mapping.update(self.groups.items())
        if not mapping:
            return None
        self._replacements = mapping
        self._pattern = id_text_pattern(mapping, self._id_shape)
        return self._pattern

    def _invalidate_pattern(self) -> None:
        self._pattern = None

    def load(self, *, accept_user: Callable[[str], bool] = _accept_all) -> AliasLoad | None:
        """以文件为准恢复两张表和编号，再按保留期回收。没有可读的文件返回 None。

        accept_user 挡掉的人不载入，并标脏让下次写盘把它们清出文件。
        """
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load alias map: %s", self.path, exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        rejected = self._restore(self.users, data.get("users"), accept_user)
        if rejected:
            self.mark_dirty()
        self._restore(self.groups, data.get("groups"), _accept_all)
        self.users.resume_numbering(data.get("user_next"))
        self.groups.resume_numbering(data.get("group_next"))
        # 回收必须在编号恢复之后：先删会让已用最大编号变小，旧文件恢复出的编号回退，别名被复用。
        pruned = self.prune()
        self._invalidate_pattern()
        return AliasLoad(users=len(self.users), groups=len(self.groups), pruned=pruned, rejected=rejected)

    def _restore(self, table: AliasTable, entries: Any, accept: Callable[[str], bool]) -> int:
        if not isinstance(entries, dict):
            return 0
        rejected = 0
        for raw_real, item in entries.items():
            real = str(raw_real or "").strip()
            if not real or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            if not accept(real):
                rejected += 1
                continue
            table.restore(real, alias, self._coerce_last_seen(item.get("last_seen")))
        return rejected

    def _coerce_last_seen(self, value: Any) -> float:
        # 缺失或非法按当前时间：刚加载就被保留期判成过期，等于把别名表清空。
        try:
            return float(value)
        except (TypeError, ValueError):
            return self._clock()

    def prune(self) -> int:
        """按保留期回收久未出现的条目，返回删掉的条数。保留期为 0 表示不回收。"""
        if self._retention_days <= 0:
            return 0
        cutoff = self._clock() - self._retention_days * 86400.0
        # 已建档的人必须能反解：按人加载记忆时要靠反向表找回真实 ID。
        protected = frozenset(memory_subjects.enrolled_subjects(self._workspace))
        removed = self.users.prune(cutoff, protected) + self.groups.prune(cutoff, protected)
        if removed:
            self.mark_dirty()
            self._invalidate_pattern()
        return removed

    def mark_dirty(self) -> None:
        """标记需要回写。窗口内的多次改动合并成一次落盘；没有事件循环时留给下次显式保存。"""
        self.dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.save_pending:
            return
        self._save_task = loop.create_task(self._save_after_window())

    async def _save_after_window(self) -> None:
        # 固定等满一个窗口再写：按距上次写盘算的话，间隔已满足时会立刻写，高频场景每个间隔都写一次。
        try:
            if self._save_min_interval > 0:
                await asyncio.sleep(self._save_min_interval)
            await self.save()
        finally:
            self._save_task = None

    async def save(self) -> None:
        """原子写入别名文件；没有改动就不写。"""
        async with self._lock:
            if not self.dirty:
                return
            now = self._clock()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": FORMAT_VERSION,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "user_next": self.users.next_index,
                    "group_next": self.groups.next_index,
                    "users": self.users.payload(now),
                    "groups": self.groups.payload(now),
                }
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
                self.dirty = False
                self.last_saved_at = self._clock()
            except Exception:
                logger.warning("failed to save alias map: %s", self.path, exc_info=True)

    async def flush(self) -> None:
        """关闭时调用：取消还在等窗口的写入，立刻把最新别名写下去。"""
        task, self._save_task = self._save_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.save()
