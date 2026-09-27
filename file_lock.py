"""跨进程互斥锁：同一个锁文件同一时刻只有一个持有者，持有的进程死了操作系统会替它放掉。

多开的几个 supervisor 各自的进程锁管不到彼此，得落到文件上。
"""
from __future__ import annotations

import contextlib
import errno
import os
import threading
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

_GUARD = threading.Lock()
# 弱引用：按会话取的锁没人用了就回收，不会越攒越多。
_PROCESS_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


def _process_lock(path: Path) -> threading.Lock:
    # 同一个文件换个写法（软链、带 ..）也得落到同一把上，同进程的几个线程先在这里排队。
    key = os.path.normcase(str(path.resolve()))
    with _GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _prepare(path: Path | str) -> tuple[Path, threading.Lock]:
    lock_path = Path(path)
    # 先建目录再取进程锁：目录还不存在时算出的真实路径可能和建好之后不一样。
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path, _process_lock(lock_path)


@contextlib.contextmanager
def _opened(path: Path) -> Iterator[BinaryIO]:
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            # 锁的是开头那一个字节，先让它存在。
            stream.write(b"0")
            stream.flush()
        yield stream


def _acquire_windows(stream: BinaryIO, *, blocking: bool) -> bool:
    import msvcrt

    held = (errno.EACCES, errno.EDEADLOCK)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    while True:
        try:
            msvcrt.locking(stream.fileno(), mode, 1)
        except OSError as exc:
            if exc.errno not in held:
                raise
            if blocking:
                # LK_LOCK 只重试十秒就报错；阻塞语义得接着等，和 flock 一致。
                continue
            return False
        return True


def _acquire_posix(stream: BinaryIO, *, blocking: bool) -> bool:
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _acquire(stream: BinaryIO, *, blocking: bool) -> bool:
    stream.seek(0)
    if os.name == "nt":
        return _acquire_windows(stream, blocking=blocking)
    return _acquire_posix(stream, blocking=blocking)


def _release(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_lock(path: Path | str) -> Iterator[None]:
    """独占这个锁文件；别人拿着就一直等。"""
    lock_path, process_lock = _prepare(path)
    with process_lock, _opened(lock_path) as stream:
        _acquire(stream, blocking=True)
        try:
            yield
        finally:
            _release(stream)


@contextlib.contextmanager
def try_exclusive_lock(path: Path | str) -> Iterator[bool]:
    """试着独占这个锁文件，给出拿没拿到；别人拿着就立刻给 False，不等。"""
    lock_path, process_lock = _prepare(path)
    if not process_lock.acquire(blocking=False):
        yield False
        return
    try:
        with _opened(lock_path) as stream:
            if not _acquire(stream, blocking=False):
                yield False
                return
            try:
                yield True
            finally:
                _release(stream)
    finally:
        process_lock.release()
