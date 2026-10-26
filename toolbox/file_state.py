"""读后改前的校验：read_file 读到的文本文件记一个戳，write_file / apply_diff 改已有文件之前拿它比对。

戳按「会话 + 规范化的绝对路径」存在进程内的有界 LRU 里；只读了部分行也记整份文件的戳。"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .text_files import replace_bytes

logger = logging.getLogger(__name__)

STALE_MESSAGE = "文件在你上次读取后被修改过，请重新 read_file 后再改"
CONCURRENT_MESSAGE = "文件在这次读取和写入之间被别处改过，没有写入；请重新 read_file 后再改"

_MAX_ENTRIES = 4096
# 按路径哈希分到固定的一组锁上：同一个文件的改动排队，锁的个数不随文件数增长
_LOCK_STRIPES = 64
_LOCKS = tuple(threading.Lock() for _ in range(_LOCK_STRIPES))


@dataclass(frozen=True)
class Stamp:
    mtime_ns: int
    size: int
    sha256: str


@dataclass(frozen=True)
class Entry:
    """会话对一个文件知道到哪一版。revealable：这一版的原文经读权限给模型看过，再回显它不算越权。"""

    stamp: Stamp
    revealable: bool


@dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    data: bytes = b""
    stamp: Stamp | None = None


@dataclass(frozen=True)
class Gate:
    """改之前和记下的戳比对的结论。vanished：读过之后文件没了。"""

    entry: Entry | None
    stale: bool = False
    vanished: bool = False

    @property
    def unread(self) -> bool:
        return self.entry is None

    @property
    def revealable(self) -> bool:
        return self.entry is not None and not self.stale and not self.vanished and self.entry.revealable


class ConcurrentEdit(RuntimeError):
    """写盘前最后一次比对时，文件已经不是这次读到的那一版。"""


def session_key(ctx: Any) -> str:
    # 和 context_archive 的会话取法一致：入口分支归到父会话，accumulate 子节点归到 child session
    return str(getattr(ctx, "_context_archive_session_id", "") or getattr(ctx, "session_id", "") or "")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def stamp_of(data: bytes, mtime_ns: int) -> Stamp:
    return Stamp(mtime_ns, len(data), hashlib.sha256(data).hexdigest())


def take_snapshot(path: Path) -> FileSnapshot:
    """整份读出文件和它的戳；不存在时 exists=False。是目录、没权限之类照常抛 OSError。"""
    try:
        with open(path, "rb") as stream:
            mtime_ns = os.fstat(stream.fileno()).st_mtime_ns
            data = stream.read()
    except FileNotFoundError:
        return FileSnapshot(False)
    return FileSnapshot(True, data, stamp_of(data, mtime_ns))


class StampStore:
    """有界 LRU。工具在 asyncio.to_thread 的线程里读写它，所以自带一把锁。"""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._entries: OrderedDict[tuple[str, str], Entry] = OrderedDict()
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()

    def get(self, session: str, path: Path) -> Entry | None:
        key = (session, _path_key(path))
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, session: str, path: Path, entry: Entry) -> None:
        key = (session, _path_key(path))
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_STORE = StampStore()


def recall(session: str, path: Path) -> Entry | None:
    return _STORE.get(session, path)


def remember(session: str, path: Path, stamp: Stamp, *, revealable: bool) -> None:
    _STORE.put(session, path, Entry(stamp, revealable))


def inspect(session: str, path: Path, snapshot: FileSnapshot) -> Gate:
    """按内容哈希比：只动了 mtime（touch）不算改过。"""
    entry = recall(session, path)
    if entry is None:
        return Gate(None)
    if not snapshot.exists or snapshot.stamp is None:
        return Gate(entry, vanished=True)
    return Gate(entry, stale=snapshot.stamp.sha256 != entry.stamp.sha256)


@contextlib.contextmanager
def edit_lock(path: Path) -> Iterator[None]:
    """本进程里改同一个文件的操作排队。只在 asyncio.to_thread 的线程里拿：持锁期间不能 await。"""
    lock = _LOCKS[zlib.crc32(_path_key(path).encode("utf-8")) % _LOCK_STRIPES]
    with lock:
        yield


def commit_locked(path: Path, data: bytes, *, expected: FileSnapshot) -> Stamp:
    """确认文件仍是 expected 那一版，再原子写入 data，返回新戳。调用方须持有 edit_lock(path)。

    进程内的并发由 edit_lock 挡住；这里再比一次，挡的是别的进程（另一个 engine worker、用户的编辑器）。
    """
    current = take_snapshot(path)
    changed = current.exists != expected.exists or (
        current.stamp is not None and expected.stamp is not None
        and current.stamp.sha256 != expected.stamp.sha256
    )
    if changed:
        raise ConcurrentEdit(CONCURRENT_MESSAGE)
    replace_bytes(path, data)
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        mtime_ns = 0
    return stamp_of(data, mtime_ns)


async def reveal_allowed(ctx: Any, gate: Gate, path: str) -> bool:
    """结果里能不能带文件原文（候选片段、改动处的上下文），否则只给行号。

    本会话经读权限看过这一版就放行；没看过就按 read_file 策略问一次，auto 才放行，approval_required 和 deny 都不放。
    写权限不等于读权限：config/nodes/** 能批准改、却禁止读。只问结论不送审：为几行回显弹一张读审批卡，工具会一直挂到有人处理。
    """
    if gate.revealable:
        return True
    evaluate = getattr(ctx, "evaluate_op", None)
    if not callable(evaluate):
        return False
    try:
        verdict = await evaluate("read_file", {"path": path})
    except Exception as exc:
        logger.warning("read policy check for echoing %s failed; showing line numbers only: %s", path, exc)
        return False
    return isinstance(verdict, dict) and str(verdict.get("safety_level") or "") == "auto"
