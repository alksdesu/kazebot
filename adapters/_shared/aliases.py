"""匿名别名表：真实 ID ↔ UserA / GroupB 这类稳定别名，落盘、按保留期回收、在文本里替换。

编号只增不回收：复用会让旧记忆里的 UserA 指向另一个人。记忆跨平台共享，编号也全平台共用一份。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Container, Iterable, Iterator, Mapping

from engine import memory_subjects
from file_lock import exclusive_lock
from platforms.descriptor import ID_SCOPE_ACCOUNT, STATE_ALIASES, PlatformDescriptor
from platforms.ids import id_text_pattern, identity_key, split_identity_key
from platforms.registry import (
    PLATFORM_DATA_ROOT,
    STANDARD_STATE_FILES,
    alias_numbering_path,
    all_platforms,
    platform_state_path,
)

logger = logging.getLogger(__name__)

USER_PREFIX = "User"
GROUP_PREFIX = "Group"
USER_BUCKET = "users"
GROUP_BUCKET = "groups"
# 每次命中都写盘会把节流窗口打满，同一小时内只记一次就够保留期判定用。
TOUCH_MIN_INTERVAL = 3600.0
FORMAT_VERSION = 1
NUMBERING_FORMAT_VERSION = 1
# 别名文件里每张表的前缀、桶名和「下一个编号」字段。
_TABLE_FIELDS = ((USER_PREFIX, USER_BUCKET, "user_next"), (GROUP_PREFIX, GROUP_BUCKET, "group_next"))

ClaimAlias = Callable[[str, Container[str]], str]


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


def _coerce_index(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _read_alias_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("alias map unreadable while rebuilding the numbering: %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _next_index_in(data: Mapping[str, Any], prefix: str, bucket: str, next_field: str) -> int:
    """一份别名文件里这张表往后该从几号发：存下的下一个编号和已用最大编号取大者。"""
    entries = data.get(bucket)
    used = [
        alias_to_index(prefix, str(item.get("alias") or ""))
        for item in (entries.values() if isinstance(entries, dict) else ())
        if isinstance(item, dict)
    ]
    return max(_coerce_index(data.get(next_field)), max(used, default=-1) + 1)


def write_alias_file(path: Path, document: Mapping[str, Any]) -> None:
    """原子写别名文件：bot 和 supervisor 随时会读它，不能读到半份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class AliasTaken(ValueError):
    """别名已经记在别的身份名下：一个别名只能反查回一个人。"""

    def __init__(self, alias: str, holder: str) -> None:
        super().__init__(f"alias {alias!r} already belongs to {holder!r}")
        self.alias = alias
        self.holder = holder


@dataclass(frozen=True)
class AliasRebind:
    document: dict[str, Any]
    # 这个身份原来的别名，换掉以后不再指向谁；原来没有、或者本来就是这个别名时是空串。
    replaced: str
    changed: bool


def rebind_alias(document: Mapping[str, Any], bucket: str, key: str, alias: str, *, now: float) -> AliasRebind:
    """别名文件里把 key 这个身份接到 alias 上，返回改好的一份，传进来的不动。

    跨平台绑定用：另一个平台上的同一个人、同一个群沿用原来的别名，记忆和建档名册都按别名认人。
    alias 已经记在别的身份名下时抛 AliasTaken。
    """
    prefix, next_field = _bucket_fields(bucket)
    index = alias_to_index(prefix, alias)
    if index < 0:
        raise ValueError(f"{alias!r} is not a {prefix} alias")
    key = str(key or "").strip()
    if not key:
        raise ValueError("an alias needs an identity key")
    data = copy.deepcopy(dict(document))
    entries = data.get(bucket, {})
    if not isinstance(entries, dict):
        raise ValueError(f"{bucket} in the alias file is not an object")
    holder = next((
        other for other, item in entries.items()
        if other != key and isinstance(item, dict) and str(item.get("alias") or "").strip() == alias
    ), None)
    if holder is not None:
        raise AliasTaken(alias, holder)
    current = entries.get(key)
    previous = str(current.get("alias") or "").strip() if isinstance(current, dict) else ""
    if previous == alias:
        return AliasRebind(data, "", changed=False)
    # 补齐成 AliasBook 写出来的形状：目标平台还没有别名表时，这就是它的第一份。
    for _, table_bucket, table_next in _TABLE_FIELDS:
        data.setdefault(table_bucket, {})
        data.setdefault(table_next, 0)
    data.setdefault("version", FORMAT_VERSION)
    data["updated_at"] = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
    data[bucket] = {**entries, key: {"alias": alias, "last_seen": now}}
    data[next_field] = max(_coerce_index(data.get(next_field)), index + 1)
    return AliasRebind(data, previous, changed=True)


def _bucket_fields(bucket: str) -> tuple[str, str]:
    for prefix, name, next_field in _TABLE_FIELDS:
        if name == bucket:
            return prefix, next_field
    raise ValueError(f"unknown alias bucket: {bucket!r}")


class AliasNumbering:
    """全平台共用的别名编号：每个前缀记着下一个要发的编号，落在 data/platforms/ 下，跨进程加锁。

    每个平台各一个进程、各管各的别名表；编号要是各发各的，两个平台的 UserA 就共用了一份记忆。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        platforms: Callable[[], Iterable[PlatformDescriptor]] = all_platforms,
    ) -> None:
        self._workspace = Path(workspace)
        self.path = alias_numbering_path(self._workspace)
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._platforms = platforms

    def claim(self, prefix: str, taken: Container[str] = ()) -> str:
        """发下一个别名并记下；taken 里已经有的跳过。"""
        with exclusive_lock(self._lock_path):
            counters = self._counters_locked()
            index = counters.get(prefix, 0)
            while alias_from_index(prefix, index) in taken:
                index += 1
            counters[prefix] = index + 1
            self._write_locked(counters)
        return alias_from_index(prefix, index)

    def reserve(self, prefix: str, next_index: int) -> None:
        """这个前缀往后至少从 next_index 发：某个平台用过的编号，别的平台不能再发。"""
        if next_index <= 0:
            return
        with exclusive_lock(self._lock_path):
            counters = self._counters_locked()
            if counters.get(prefix, 0) >= next_index:
                return
            counters[prefix] = next_index
            self._write_locked(counters)

    def _counters_locked(self) -> dict[str, int]:
        counters = self._read_locked()
        if counters is None:
            # 重建完立刻落盘，之后不用每次都扫一遍别名文件。
            counters = self._rebuild()
            self._write_locked(counters)
        return counters

    def _read_locked(self) -> dict[str, int] | None:
        """编号文件里的计数；没有文件或读不懂返回 None。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("alias numbering unreadable, rebuilding it from the alias maps: %s", self.path, exc_info=True)
            return None
        counters = data.get("next") if isinstance(data, dict) else None
        if not isinstance(counters, dict):
            logger.warning("alias numbering has an unexpected shape, rebuilding it from the alias maps: %s", self.path)
            return None
        return {str(prefix): _coerce_index(value) for prefix, value in counters.items()}

    def _rebuild(self) -> dict[str, int]:
        """从各平台的别名表和建档名册认出用过的最大编号。"""
        counters: dict[str, int] = {}
        for path in self._alias_files():
            data = _read_alias_file(path)
            for prefix, bucket, next_field in _TABLE_FIELDS:
                counters[prefix] = max(counters.get(prefix, 0), _next_index_in(data, prefix, bucket, next_field))
        # 建档的人记忆还在；别名表丢了，他的别名也不能再发给别人。
        enrolled = (alias_to_index(USER_PREFIX, subject) for subject in memory_subjects.enrolled_subjects(self._workspace))
        counters[USER_PREFIX] = max(counters.get(USER_PREFIX, 0), max(enrolled, default=-1) + 1)
        return counters

    def _alias_files(self) -> list[Path]:
        # env 挪走的老文件只有看得见那个变量的进程找得到；各平台加载时都会 reserve 自己的编号，补上这个缺口。
        declared = {platform_state_path(platform, STATE_ALIASES, self._workspace) for platform in self._platforms()}
        standard = (self._workspace / PLATFORM_DATA_ROOT).glob(f"*/{STANDARD_STATE_FILES[STATE_ALIASES]}")
        return sorted(declared.union(standard))

    def _write_locked(self, counters: Mapping[str, int]) -> None:
        payload = {
            "version": NUMBERING_FORMAT_VERSION,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "next": dict(sorted(counters.items())),
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # 本进程的表会跳过自己占着的别名，写不下去也不至于在同一个平台里发重。
            logger.warning("failed to save alias numbering: %s", self.path, exc_info=True)


class AliasTable:
    """一类对象（人或群）的别名：正反两张表、最后出现时间、下一个编号。

    给了 claim 就从全平台共用的编号里领，不给就在本表里自己往下数。
    """

    def __init__(self, prefix: str, *, claim: ClaimAlias | None = None) -> None:
        self.prefix = prefix
        self.unknown = f"{prefix}Unknown"
        self.next_index = 0
        self._claim = claim
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
        if self._claim is None:
            alias = alias_from_index(self.prefix, self.next_index)
        else:
            # 本表占着的跳过：编号文件被删掉重建时，还没落盘的新别名不在任何一份文件里。
            alias = self._claim(self.prefix, self._reals)
        self.next_index = max(self.next_index, alias_to_index(self.prefix, alias) + 1)
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
        self.next_index = max(_coerce_index(persisted_next), highest + 1)

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
    """人和群两张别名表，连同落盘节流和文本替换。一个平台一个实例，挂在适配器状态上。

    表里的键是 identity_key：整个平台唯一的 ID 原样记，按账号分的带上账号。替换文本、反查真实 ID
    只认当前账号名下的条目：别的号名下的 ID 在这个号上既不会出现，拿到了也用不了。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        platform: PlatformDescriptor,
        workspace: Path,
        retention_days: float = 0.0,
        save_min_interval: float = 5.0,
        clock: Callable[[], float] = time.time,
        numbering: AliasNumbering | None = None,
    ) -> None:
        self.path = Path(path)
        self._platform = platform
        self._workspace = Path(workspace)
        self._numbering = numbering if numbering is not None else AliasNumbering(self._workspace)
        self.users = AliasTable(USER_PREFIX, claim=self._numbering.claim)
        self.groups = AliasTable(GROUP_PREFIX, claim=self._numbering.claim)
        self.dirty = False
        self.last_saved_at = 0.0
        self._account = ""
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

    @property
    def account(self) -> str:
        return self._account

    def bind_account(self, account: str) -> None:
        """记下当前登录的机器人账号。按账号分 ID 的平台，之后登记、替换、反查都只认这个号名下的。"""
        account = str(account or "").strip()
        if account != self._account:
            self._account = account
            self._invalidate_pattern()

    def identity(self, real: str) -> str:
        """这个原始 ID 在表里的键。按账号分 ID 的平台还没绑定账号时直接抛：漏绑会把几个号的人记成一个。"""
        return identity_key(self._platform, real, account=self._account)

    def user_alias(self, real: str) -> str:
        return self._alias(self.users, real)

    def group_alias(self, real: str) -> str:
        return self._alias(self.groups, real)

    def user_alias_of(self, real: str) -> str | None:
        """这个人已经发过的别名，没发过是 None；只查不登记。"""
        return self._alias_of(self.users, real)

    def group_alias_of(self, real: str) -> str | None:
        return self._alias_of(self.groups, real)

    def user_real_of(self, alias: str) -> str | None:
        """别名对应的原始 ID；落在别的账号名下的不算。"""
        return self._real_of(self.users, alias)

    def group_real_of(self, alias: str) -> str | None:
        return self._real_of(self.groups, alias)

    def _alias(self, table: AliasTable, real: str) -> str:
        real = str(real or "").strip()
        if not real:
            return table.unknown
        key = self.identity(real)
        alias, created = table.assign(key)
        if created:
            self._invalidate_pattern()
            self.mark_dirty()
        if table.touch(key, self._clock()):
            self.mark_dirty()
        return alias

    def _alias_of(self, table: AliasTable, real: str) -> str | None:
        real = str(real or "").strip()
        return table.alias_of(self.identity(real)) if real else None

    def _real_of(self, table: AliasTable, alias: str) -> str | None:
        key = table.real_of(str(alias or "").strip())
        return None if key is None else self._own_raw(key)

    def _own_raw(self, key: str) -> str | None:
        """键落在当前账号名下时给出原始 ID；整个平台唯一的 ID 不分账号。"""
        account, raw = split_identity_key(self._platform, key)
        if self._platform.id_scope == ID_SCOPE_ACCOUNT and not (account and account == self._account):
            return None
        return raw

    def _own_entries(self, table: AliasTable) -> Iterator[tuple[str, str]]:
        for key, alias in table.items():
            raw = self._own_raw(key)
            if raw is not None:
                yield raw, alias

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
        mapping = dict(self._own_entries(self.users))
        mapping.update(self._own_entries(self.groups))
        if not mapping:
            return None
        self._replacements = mapping
        self._pattern = id_text_pattern(mapping, self._platform.id_shape)
        return self._pattern

    def _invalidate_pattern(self) -> None:
        self._pattern = None

    def load(self, *, accept_user: Callable[[str], bool] = _accept_all) -> AliasLoad | None:
        """以文件为准恢复两张表和编号，再按保留期回收。没有可读的文件返回 None。

        accept_user 按原始 ID 判断，挡掉的人不载入，并标脏让下次写盘把它们清出文件。
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
        rejected = self._restore(self.users, data.get(USER_BUCKET), accept_user)
        if rejected:
            self.mark_dirty()
        self._restore(self.groups, data.get(GROUP_BUCKET), _accept_all)
        self.users.resume_numbering(data.get("user_next"))
        self.groups.resume_numbering(data.get("group_next"))
        # 本平台用过的编号记进全平台编号：编号文件丢过，或者别的进程重建时没看见这份文件，都靠这一步补齐。
        for table in (self.users, self.groups):
            self._numbering.reserve(table.prefix, table.next_index)
        # 回收必须在编号恢复之后：先删会让已用最大编号变小，旧文件恢复出的编号回退，别名被复用。
        pruned = self.prune()
        self._invalidate_pattern()
        return AliasLoad(users=len(self.users), groups=len(self.groups), pruned=pruned, rejected=rejected)

    def _restore(self, table: AliasTable, entries: Any, accept: Callable[[str], bool]) -> int:
        if not isinstance(entries, dict):
            return 0
        rejected = 0
        for raw_key, item in entries.items():
            key = str(raw_key or "").strip()
            if not key or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            if not accept(split_identity_key(self._platform, key)[1]):
                rejected += 1
                continue
            table.restore(key, alias, self._coerce_last_seen(item.get("last_seen")))
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
                write_alias_file(self.path, {
                    "version": FORMAT_VERSION,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "user_next": self.users.next_index,
                    "group_next": self.groups.next_index,
                    USER_BUCKET: self.users.payload(now),
                    GROUP_BUCKET: self.groups.payload(now),
                })
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
