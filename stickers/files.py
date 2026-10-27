"""表情包库在磁盘上的布局：图落在哪、怎么落盘、崩溃留下的半成品怎么清。

库里登记的 rel_path 一律相对工作区根目录，图片只许待在 data/stickers 下的待审和在库两个目录里。
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from config_layout import is_dir_link
from real_paths import real_path

from .store import Sticker, StickerStore

logger = logging.getLogger(__name__)

PENDING_DIR = "pending"
LIBRARY_DIR = "library"
MEDIA_DIRS = (PENDING_DIR, LIBRARY_DIR)
BACKUP_DIR = "backups"

# 写图、导出、导入进行中都会不停刷新 mtime，一小时没动过的只能是崩溃留下的。
STALE_AFTER_SEC = 3600
SWEEP_INTERVAL_SEC = 600

_STAGING_PREFIX = ".import."
_TEMP_SUFFIX = ".tmp"
_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")
_EXT_BY_FMT = {"png": ".png", "gif": ".gif", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp"}
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2)


def sticker_root(workspace_root: Path | str) -> Path:
    """多开时这个目录是指向共享库的软链，各实例看到同一份图。"""
    return Path(workspace_root) / "data" / "stickers"


def store_path(workspace_root: Path | str) -> Path:
    return sticker_root(workspace_root) / "stickers.sqlite3"


def staging_dir(workspace_root: Path | str) -> Path:
    """导入用的暂存目录，和图片目录在同一个库里，搬进去只是同一文件系统内的改名。"""
    return sticker_root(workspace_root) / f"{_STAGING_PREFIX}{secrets.token_hex(16)}"


def safe_suffix(raw: Any, *, default: str = "") -> str:
    """扩展名会原样拼进库里的路径，只留短小的字母数字。"""
    suffix = str(raw or "").lower()
    return suffix if _SUFFIX.match(suffix) else default


def suffix_for(fmt: str, *, fallback: str = ".bin") -> str:
    """read_shape 认出的格式对应的扩展名，认不出的用 fallback。"""
    return _EXT_BY_FMT.get(fmt) or fallback


def pure_relative(raw: Any) -> str:
    """只认纯相对路径，原样给回；不可信来源能靠 ..、绝对路径、盘符或反斜杠跳出去，这些给空串。"""
    text = str(raw or "")
    if not text or text != text.strip() or text.startswith("/") or text.endswith("/"):
        return ""
    if "\\" in text or ":" in text:
        return ""
    if any(part in ("", ".", "..") for part in text.split("/")):
        return ""
    return text


def media_roots(workspace_root: Path | str) -> tuple[Path, ...]:
    """图片只许落在这两个目录。库文件、打标锁和历史备份都在上一层，落点歪一步就会碰到它们。"""
    root = sticker_root(workspace_root)
    return tuple(real_path(root / name) for name in MEDIA_DIRS)


def sticker_file(
    workspace_root: Path | str, rel_path: Any, *, roots: Sequence[Path] | None = None,
) -> Path | None:
    """库里登记的 rel_path 换成真实路径；不是纯相对路径，或解析后不在待审、在库目录下的给 None。

    多开时 data/stickers 是软链，比对的是解析后的真实位置。文件在不在由调用方自己看。
    """
    rel = pure_relative(rel_path)
    if not rel:
        return None
    try:
        bases = media_roots(workspace_root) if roots is None else tuple(roots)
        target = real_path(Path(workspace_root) / rel)
    except (OSError, RuntimeError, ValueError):
        return None
    return target if any(base in target.parents for base in bases) else None


@contextlib.contextmanager
def replacing(path: Path | str) -> Iterator[Path]:
    """给出同目录下的临时文件，块正常结束才落盘并原子换上去；中途出错不留临时文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}{_TEMP_SUFFIX}")
    try:
        yield tmp
        # 在写句柄上 fsync：Windows 对只读句柄做 fsync 会报 EBADF。
        with tmp.open("rb+") as stream:
            os.fsync(stream.fileno())
        _replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(target.parent)


def write_atomic(path: Path | str, data: bytes) -> None:
    with replacing(path) as tmp:
        tmp.write_bytes(data)


def place(
    workspace_root: Path | str,
    store: StickerStore,
    data: bytes,
    *,
    location: str,
    sha256: str,
    **fields: Any,
) -> Sticker | None:
    """把图写到 data/stickers/<location> 再登记，返回登记出的行；这张图已经被登记过时给 None。

    共享库里几个进程可能同时收同一张图：输了的一方只在文件是自己这次新建、
    且库里登记的落点和自己不同时才删，赢家的文件不会被顺手删掉。
    """
    root = Path(workspace_root)
    target = sticker_root(root) / location
    rel_path = target.relative_to(root).as_posix()
    if sticker_file(root, rel_path) is None:
        raise ValueError(f"表情包落点不在库里: {location!r}")
    created = not target.exists()
    write_atomic(target, data)
    row = store.add(sha256=sha256, rel_path=rel_path, **fields)
    if row is None and created:
        winner = store.get(sha256)
        if winner is None or winner.rel_path != rel_path:
            target.unlink(missing_ok=True)
    return row


def sweep_stale(
    workspace_root: Path | str, *, older_than: float = STALE_AFTER_SEC, now: float | None = None,
) -> list[Path]:
    """清掉崩溃留下的半成品：写图和导出的临时文件、导入的暂存目录。只看名字和年龄。"""
    root = sticker_root(workspace_root)
    cutoff = (time.time() if now is None else now) - older_than
    candidates = [path for folder in (*MEDIA_DIRS, BACKUP_DIR) for path in _temp_files(root / folder)]
    candidates.extend(
        path for path in _entries(root) if path.name.startswith(_STAGING_PREFIX) and _plain_dir(path)
    )
    removed: list[Path] = []
    for path in candidates:
        if _last_touched(path) < cutoff and _remove(path):
            removed.append(path)
    if removed:
        logger.info("表情包库清掉 %d 个崩溃留下的临时文件", len(removed))
    return removed


def _replace(source: Path, target: Path) -> None:
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Windows 上目标正被别的进程打开时替换会被拒，另一个进程刚换完还没关句柄也算；等一下再换。
            if os.name != "nt":
                raise
            time.sleep(delay)
    os.replace(source, target)


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _plain_dir(path: Path) -> bool:
    return not is_dir_link(path) and path.is_dir()


def _temp_files(directory: Path) -> list[Path]:
    if not _plain_dir(directory):
        return []
    found: list[Path] = []
    for parent, _dirs, names in os.walk(directory, followlinks=False):
        found.extend(
            Path(parent) / name for name in names
            if name.startswith(".") and name.endswith(_TEMP_SUFFIX)
        )
    return found


def _last_touched(path: Path) -> float:
    """目录取自己和直接子项里最新的 mtime：往里写大文件时目录本身的 mtime 不会动。"""
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return float("inf")
    if _plain_dir(path):
        for child in _entries(path):
            with contextlib.suppress(OSError):
                newest = max(newest, child.lstat().st_mtime)
    return newest


def _remove(path: Path) -> bool:
    try:
        if is_dir_link(path):
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("表情包库删不掉临时文件 %s: %s", path, exc)
        return False
    return True
