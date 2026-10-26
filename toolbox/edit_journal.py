"""编辑日志：改文件的工具在同一个任务里第一次改某个文件之前记下原文件，undo_edits 按任务把它放回去。

存在 data/edit_journal/<会话目录>/<任务目录>/：manifest.json 记每个文件改前、改后的哈希，原文件内容 gzip 后另存。
会话是 supervisor 给的发起链根会话，派出去的子任务也记在那里；写 manifest 都在这个会话的跨进程锁里做。"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int, load_runtime_config
from engine.tool_outputs import session_dir_name
from file_lock import exclusive_lock

from . import file_state
from .file_state import _path_key
from .text_files import replace_bytes

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 3
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TASK_BYTES = 100 * 1024 * 1024
MAX_TASK_FILES = 1000

SKIP_TOO_LARGE = "too_large"
SKIP_TASK_BYTES = "task_bytes"
SKIP_ERROR = "error"

_MANIFEST = "manifest.json"
_MANIFEST_VERSION = 1
# 改文件的锁（edit-*.lock）也在这个目录里，清理时不动它
_LOCK_DIR = file_state.LOCK_SUBDIR
# 按会话目录名散列到固定数量的锁文件：锁文件个数不随会话增长，清理时手里只有目录名也算得出是哪一把
_LOCK_STRIPE_CHARS = 2
# Windows 上整条路径超过 260 个字符就写不进去，原文件按 sha256 的前 32 位起名
_BLOB_NAME = re.compile(r"[0-9a-f]{32}\.gz")
_BLOB_NAME_HEX = 32
_BLOB_SUFFIX = ".gz"
# 写原文件时持着这个文件的改动锁，压缩取最快的一档
_COMPRESS_LEVEL = 1
_PRIVATE_DIR_MODE = file_state.PRIVATE_DIR_MODE
_SECONDS_PER_DAY = 86400
_MIB = 1024 * 1024
_UNRECORDED_LIMIT = 4096


JOURNAL_REFUSED_MESSAGE = "data/edit_journal/ 只由编辑工具自己记录，不能直接改：改了它就能伪造 undo_edits 要放回的内容"


def journal_root(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / file_state.JOURNAL_DIR


def inside_journal(workspace_root: Path | str, path: Path | str) -> bool:
    """path 落在编辑日志目录里（软链也按指向的位置算）。"""
    try:
        return Path(path).resolve().is_relative_to(journal_root(workspace_root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def retention_days_of(runtime_cfg: Any) -> int:
    cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
    return get_int(cfg, "edit_journal.retention_days", DEFAULT_RETENTION_DAYS, min_value=1, max_value=3650)


class JournalCorrupt(ValueError):
    """manifest 或原文件内容读不出来、对不上。消息可以直接给模型看。"""


@dataclass(frozen=True)
class JournalScope:
    """谁在记：工作区、日志记在哪个会话、任务号。"""

    workspace_root: Path
    session: str
    task_id: str
    node_id: str = ""

    @classmethod
    def of(cls, ctx: Any, *, session: str = "") -> "JournalScope":
        """session 是 supervisor 给的发起链根会话；不给（没有任务号、离线）就用 file_state 的会话键。"""
        # 推理循环和独立工具任务都会填 task_id；没有时退到 run_id，它在推理循环里也先取任务号
        task_id = str(getattr(ctx, "task_id", "") or getattr(ctx, "run_id", "") or "").strip()
        return cls(
            workspace_root=Path(getattr(ctx, "workspace_root")),
            session=str(session or "").strip() or file_state.session_key(ctx),
            task_id=task_id,
            node_id=str(getattr(ctx, "node_id", "") or ""),
        )


@dataclass(frozen=True)
class JournalNote:
    """这次改动记没记进编辑日志。reason 是没记下的原因，message 是给模型看的一整句；记下了两个都是空串。"""

    undoable: bool
    reason: str = ""
    message: str = ""

    def lines(self) -> list[str]:
        return [self.message] if self.message else []

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"undoable": self.undoable}
        if self.message:
            data["undoNote"] = self.message
        return data


RECORDED = JournalNote(True)


def not_undoable(reason: str) -> JournalNote:
    return JournalNote(False, reason, f"注意：{reason}，这次改动不可撤销。")


@dataclass(frozen=True)
class PreImage:
    """改之前的样子。skipped 非空表示没记下原文件内容，reason 是给模型看的原因，这一项撤不了。"""

    exists: bool
    sha256: str = ""
    size: int = 0
    mtime_ns: int = 0
    mode: int | None = None
    blob: str = ""
    blob_size: int = 0
    revealable: bool = False
    created_dirs: tuple[str, ...] = ()
    skipped: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PostImage:
    exists: bool
    sha256: str = ""
    size: int = 0
    mtime_ns: int = 0


@dataclass(frozen=True)
class UndoMark:
    at: float
    by_task: str
    action: str
    forced: bool


@dataclass(frozen=True)
class JournalEntry:
    """一个任务对一个文件的一段改动。post 是最后一次写成的版本，从没写成过是 None。"""

    id: int
    path: str
    display: str
    tool: str
    recorded_at: float
    pre: PreImage
    post: PostImage | None
    writes: int
    undone: UndoMark | None

    @property
    def undoable(self) -> bool:
        return not self.pre.skipped

    @property
    def pending(self) -> bool:
        return self.undone is None and self.undoable

    @property
    def created(self) -> bool:
        return not self.pre.exists


@dataclass(frozen=True)
class TaskJournal:
    session: str
    task_id: str
    node_id: str
    created_at: float
    last_write_at: float
    entries: tuple[JournalEntry, ...]

    @property
    def changed_at(self) -> float:
        return self.last_write_at or self.created_at


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pre_from(raw: Any) -> PreImage | None:
    if not isinstance(raw, dict):
        return None
    dirs = raw.get("created_dirs")
    mode = raw.get("mode")
    pre = PreImage(
        exists=bool(raw.get("exists")),
        sha256=str(raw.get("sha256") or ""),
        size=_as_int(raw.get("size")),
        mtime_ns=_as_int(raw.get("mtime_ns")),
        mode=mode if isinstance(mode, int) and not isinstance(mode, bool) else None,
        blob=str(raw.get("blob") or ""),
        blob_size=_as_int(raw.get("blob_size")),
        revealable=bool(raw.get("revealable")),
        created_dirs=tuple(item for item in dirs if isinstance(item, str)) if isinstance(dirs, list) else (),
        skipped=str(raw.get("skipped") or ""),
        reason=str(raw.get("reason") or ""),
    )
    if pre.exists and not pre.skipped and (not _BLOB_NAME.fullmatch(pre.blob) or len(pre.sha256) != 64):
        return replace(pre, skipped=SKIP_ERROR, reason="编辑日志里这个文件的原文件记录不完整")
    return pre


def _post_from(raw: Any) -> PostImage | None:
    if not isinstance(raw, dict):
        return None
    return PostImage(
        exists=bool(raw.get("exists", True)),
        sha256=str(raw.get("sha256") or ""),
        size=_as_int(raw.get("size")),
        mtime_ns=_as_int(raw.get("mtime_ns")),
    )


def _undo_from(raw: Any) -> UndoMark | None:
    if not isinstance(raw, dict):
        return None
    return UndoMark(
        at=_as_float(raw.get("at")),
        by_task=str(raw.get("by_task") or ""),
        action=str(raw.get("action") or ""),
        forced=bool(raw.get("forced")),
    )


def _entry_from(raw: Any) -> JournalEntry | None:
    if not isinstance(raw, dict):
        return None
    entry_id = _as_int(raw.get("id"), -1)
    path = str(raw.get("path") or "")
    pre = _pre_from(raw.get("pre"))
    if entry_id < 0 or not path or not os.path.isabs(path) or pre is None:
        return None
    return JournalEntry(
        id=entry_id,
        path=path,
        display=str(raw.get("display") or path),
        tool=str(raw.get("tool") or ""),
        recorded_at=_as_float(raw.get("recorded_at")),
        pre=pre,
        post=_post_from(raw.get("post")),
        writes=_as_int(raw.get("writes")),
        undone=_undo_from(raw.get("undone")),
    )


def _task_from(manifest: dict[str, Any], session: str) -> TaskJournal | None:
    task_id = str(manifest.get("task_id") or "").strip()
    if not task_id:
        return None
    entries = tuple(entry for raw in manifest["entries"] if (entry := _entry_from(raw)) is not None)
    return TaskJournal(
        # 撤销时按它找回任务目录，得用列出这个目录的会话，不能信 manifest 里记的
        session=session,
        task_id=task_id,
        node_id=str(manifest.get("node_id") or ""),
        created_at=_as_float(manifest.get("created_at")),
        last_write_at=_as_float(manifest.get("last_write_at")),
        entries=entries,
    )


def _entry_by_id(manifest: dict[str, Any], entry_id: int) -> dict[str, Any] | None:
    for entry in manifest["entries"]:
        if isinstance(entry, dict) and _as_int(entry.get("id"), -1) == entry_id:
            return entry
    return None


def _latest_entry_for(manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
    for entry in reversed(manifest["entries"]):
        path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        if path and _path_key(Path(path)) == key:
            return entry
    return None


def _blob_of(entry: Any) -> str:
    pre = entry.get("pre") if isinstance(entry, dict) else None
    blob = str(pre.get("blob") or "") if isinstance(pre, dict) else ""
    return blob if _BLOB_NAME.fullmatch(blob) else ""


def _note_of(entry: dict[str, Any]) -> JournalNote:
    pre = entry.get("pre") if isinstance(entry.get("pre"), dict) else {}
    if pre.get("skipped"):
        return not_undoable(str(pre.get("reason") or "没有记下原文件"))
    return RECORDED


def _mode_of(path: Path) -> int | None:
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError:
        return None


def _missing_dirs(path: Path) -> tuple[str, ...]:
    """path 往上还不存在的那几层目录，最深的在前；write_file 新建文件时会把它们一起建出来。"""
    missing: list[str] = []
    parent = path.parent
    while parent != parent.parent and not parent.exists():
        missing.append(str(parent))
        parent = parent.parent
    return tuple(missing)


def _inflate(packed: bytes, size: int) -> bytes:
    """解压时最多只放出 size 个字节：manifest 被改过的话，一份小小的压缩包也可能撑出几个 G。"""
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        data = decompressor.decompress(packed, size + 1)
    except zlib.error as exc:
        raise JournalCorrupt(f"编辑日志里的原文件内容解不开（{exc}）") from exc
    if len(data) != size or not decompressor.eof:
        raise JournalCorrupt("编辑日志里的原文件内容和记录的大小对不上")
    return data


def _newest_mtime(directory: Path) -> float:
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = directory.stat().st_mtime
        for path in directory.iterdir():
            with contextlib.suppress(OSError):
                newest = max(newest, path.stat().st_mtime)
    return newest


def _remove_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):
        directory.rmdir()


class EditJournal:
    def __init__(self, workspace_root: Path | str, *, retention_days: int | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.root = journal_root(self.workspace_root)
        self._retention_days = retention_days

    @property
    def retention_days(self) -> int:
        if self._retention_days is not None:
            return max(1, int(self._retention_days))
        return retention_days_of(load_runtime_config(self.workspace_root))

    def _inside(self, directory: Path) -> Path:
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("edit journal directory escapes data/edit_journal")
        return directory

    def session_dir(self, session: str) -> Path:
        if not str(session or "").strip():
            raise ValueError("edit journal session is empty")
        return self._inside(self.root / session_dir_name(session))

    def task_dir(self, session: str, task_id: str) -> Path:
        if not str(task_id or "").strip():
            raise ValueError("edit journal task id is empty")
        return self._inside(self.session_dir(session) / session_dir_name(task_id))

    def _ensure_root(self) -> None:
        # 日志里存着原文件的内容：原文件是 0600 的话，副本也不能让同机的别的用户读到
        self.root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)

    def _ensure_task_dir(self, task_dir: Path) -> None:
        self._ensure_root()
        task_dir.parent.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
        task_dir.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self, directory_name: str) -> Iterator[None]:
        """一个会话目录的跨进程锁；按目录名取，清理时手里只有目录名。"""
        stripe = hashlib.sha1(directory_name.encode("utf-8")).hexdigest()[:_LOCK_STRIPE_CHARS]
        self._ensure_root()
        with exclusive_lock(self.root / _LOCK_DIR / f"{stripe}.lock"):
            yield

    @staticmethod
    def _load_manifest(task_dir: Path) -> dict[str, Any] | None:
        """没有 manifest 返回 None；读不出来或格式不对抛 JournalCorrupt。"""
        path = task_dir / _MANIFEST
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalCorrupt(f"{path} 读不出来（{exc}）") from exc
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise JournalCorrupt(f"{path} 格式不对")
        return data

    @staticmethod
    def _save_manifest(task_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = time.time()
        replace_bytes(task_dir / _MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))

    @staticmethod
    def _new_manifest(scope: JournalScope) -> dict[str, Any]:
        now = time.time()
        return {
            "version": _MANIFEST_VERSION,
            "session": scope.session,
            "task_id": scope.task_id,
            "node_id": scope.node_id,
            "created_at": now,
            "updated_at": now,
            "last_write_at": 0.0,
            "blob_bytes": 0,
            "entries": [],
        }

    def _pre_image(
        self, task_dir: Path, manifest: dict[str, Any], path: Path, snapshot: file_state.FileSnapshot, revealable: bool,
    ) -> dict[str, Any]:
        if not snapshot.exists or snapshot.stamp is None:
            return {"exists": False, "created_dirs": list(_missing_dirs(path))}
        stamp = snapshot.stamp
        base: dict[str, Any] = {
            "exists": True, "sha256": stamp.sha256, "size": stamp.size, "mtime_ns": stamp.mtime_ns,
            "mode": _mode_of(path),
        }
        if stamp.size > MAX_FILE_BYTES:
            reason = f"这个文件太大（{stamp.size} 字节，超过 {MAX_FILE_BYTES // _MIB}MB），没有记下原文件"
            return {**base, "skipped": SKIP_TOO_LARGE, "reason": reason}
        name = f"{stamp.sha256[:_BLOB_NAME_HEX]}{_BLOB_SUFFIX}"
        blob_path = task_dir / name
        with contextlib.suppress(OSError):
            if blob_path.is_file():
                return {**base, "blob": name, "blob_size": blob_path.stat().st_size, "revealable": revealable}
        packed = gzip.compress(snapshot.data, compresslevel=_COMPRESS_LEVEL, mtime=0)
        used = _as_int(manifest.get("blob_bytes"))
        if used + len(packed) > MAX_TASK_BYTES:
            reason = f"本任务的编辑日志已满（上限 {MAX_TASK_BYTES // _MIB}MB），没有记下原文件"
            return {**base, "skipped": SKIP_TASK_BYTES, "reason": reason}
        try:
            replace_bytes(blob_path, packed)
        except OSError as exc:
            logger.warning("edit journal: could not store the original of %s: %s", path, exc)
            return {**base, "skipped": SKIP_ERROR, "reason": f"编辑日志没能存下原文件（{exc}）"}
        manifest["blob_bytes"] = used + len(packed)
        return {**base, "blob": name, "blob_size": len(packed), "revealable": revealable}

    def begin(
        self,
        scope: JournalScope,
        path: Path,
        snapshot: file_state.FileSnapshot,
        *,
        tool: str,
        display: str,
        revealable: bool,
        unrecorded: str | None = None,
    ) -> tuple[int | None, bool, JournalNote]:
        """记下 path 在这个任务里改之前的样子，返回 (条目号, 是否这次新建的条目, 结论)。

        这个任务里已经记过同一个文件就沿用那一条，原文件不覆盖。unrecorded 是本进程记着的「这个文件先前没能记下」的原因。
        """
        task_dir = self.task_dir(scope.session, scope.task_id)
        with self._locked(task_dir.parent.name):
            try:
                manifest = self._load_manifest(task_dir)
            except JournalCorrupt as exc:
                logger.warning("edit journal: starting over an unreadable manifest: %s", exc)
                manifest = None
            if manifest is None:
                manifest = self._new_manifest(scope)
            latest = _latest_entry_for(manifest, _path_key(path))
            if latest is not None and latest.get("undone") is None:
                return _as_int(latest.get("id"), -1), False, _note_of(latest)
            if unrecorded:
                return None, False, not_undoable(unrecorded)
            if len(manifest["entries"]) >= MAX_TASK_FILES:
                return None, False, not_undoable(f"本任务记下的文件已达 {MAX_TASK_FILES} 个，没有记下原文件")
            self._ensure_task_dir(task_dir)
            entry = {
                "id": max((_as_int(item.get("id")) for item in manifest["entries"] if isinstance(item, dict)), default=0) + 1,
                "path": str(path),
                "display": display,
                "tool": tool,
                "recorded_at": time.time(),
                "pre": self._pre_image(task_dir, manifest, path, snapshot, revealable),
                "post": None,
                "writes": 0,
                "undone": None,
            }
            manifest["entries"].append(entry)
            self._save_manifest(task_dir, manifest)
            return entry["id"], True, _note_of(entry)

    def finish(self, scope: JournalScope, entry_id: int, stamp: file_state.Stamp) -> JournalNote:
        """写盘成功之后记下新内容的哈希。"""
        task_dir = self.task_dir(scope.session, scope.task_id)
        with self._locked(task_dir.parent.name):
            manifest = self._load_manifest(task_dir)
            entry = _entry_by_id(manifest, entry_id) if manifest is not None else None
            if entry is None:
                raise JournalCorrupt("编辑日志里找不到这次改动的记录")
            now = time.time()
            entry["post"] = {
                "exists": True, "sha256": stamp.sha256, "size": stamp.size, "mtime_ns": stamp.mtime_ns, "at": now,
            }
            entry["writes"] = _as_int(entry.get("writes")) + 1
            manifest["last_write_at"] = now
            self._save_manifest(task_dir, manifest)
            return _note_of(entry)

    def drop(self, scope: JournalScope, entry_id: int) -> None:
        """撤掉这次新建、却没写成的条目；它的原文件没别的条目用就一起删。"""
        task_dir = self.task_dir(scope.session, scope.task_id)
        with self._locked(task_dir.parent.name):
            manifest = self._load_manifest(task_dir)
            entry = _entry_by_id(manifest, entry_id) if manifest is not None else None
            if manifest is None or entry is None or entry.get("post") is not None:
                return
            manifest["entries"].remove(entry)
            blob = _blob_of(entry)
            if blob and not any(_blob_of(other) == blob for other in manifest["entries"]):
                with contextlib.suppress(OSError):
                    (task_dir / blob).unlink(missing_ok=True)
                pre = entry.get("pre") if isinstance(entry.get("pre"), dict) else {}
                manifest["blob_bytes"] = max(0, _as_int(manifest.get("blob_bytes")) - _as_int(pre.get("blob_size")))
            if manifest["entries"]:
                self._save_manifest(task_dir, manifest)
            else:
                shutil.rmtree(task_dir, ignore_errors=True)

    def tasks(self, session: str) -> list[TaskJournal]:
        """本会话的编辑记录，最近改过文件的在前；manifest 读不出来的记一条警告后略过。

        manifest 都是整份替换写的，读的时候不用拿锁。
        """
        try:
            directory = self.session_dir(session)
        except ValueError:
            return []
        try:
            task_dirs = sorted(path for path in directory.iterdir() if path.is_dir())
        except FileNotFoundError:
            return []
        journals: list[TaskJournal] = []
        for task_dir in task_dirs:
            try:
                manifest = self._load_manifest(task_dir)
            except JournalCorrupt as exc:
                logger.warning("edit journal: skipping %s", exc)
                continue
            except OSError as exc:
                logger.warning("edit journal: cannot read %s: %s", task_dir, exc)
                continue
            journal = _task_from(manifest, session) if manifest is not None else None
            if journal is not None and journal.entries:
                journals.append(journal)
        journals.sort(key=lambda journal: journal.changed_at, reverse=True)
        return journals

    def tasks_in(self, sessions: Iterable[str]) -> list[TaskJournal]:
        """几个会话的编辑记录合在一起，最近改过文件的在前；同一个任务号只取先列出的会话里的那份。"""
        seen: set[str] = set()
        merged: list[TaskJournal] = []
        for session in dict.fromkeys(str(item or "").strip() for item in sessions):
            if not session:
                continue
            for journal in self.tasks(session):
                if journal.task_id in seen:
                    continue
                seen.add(journal.task_id)
                merged.append(journal)
        merged.sort(key=lambda journal: journal.changed_at, reverse=True)
        return merged

    def entry(self, session: str, task_id: str, entry_id: int) -> JournalEntry | None:
        manifest = self._load_manifest(self.task_dir(session, task_id))
        raw = _entry_by_id(manifest, entry_id) if manifest is not None else None
        return _entry_from(raw) if raw is not None else None

    def load_pre_image(self, session: str, task_id: str, entry: JournalEntry) -> bytes:
        """读回原文件内容，核对大小和 sha256；对不上抛 JournalCorrupt。"""
        pre = entry.pre
        if not pre.exists or pre.skipped or not _BLOB_NAME.fullmatch(pre.blob):
            raise JournalCorrupt("这一项没有记下原文件内容")
        try:
            packed = (self.task_dir(session, task_id) / pre.blob).read_bytes()
        except FileNotFoundError as exc:
            raise JournalCorrupt("编辑日志里的原文件内容不见了") from exc
        data = _inflate(packed, pre.size)
        if hashlib.sha256(data).hexdigest() != pre.sha256:
            raise JournalCorrupt("编辑日志里的原文件内容和记录的哈希对不上")
        return data

    def mark_undone(
        self, session: str, task_id: str, entry_id: int, *, by_task: str, action: str, forced: bool,
    ) -> bool:
        """标记为已撤销；已经标过的返回 False。"""
        task_dir = self.task_dir(session, task_id)
        with self._locked(task_dir.parent.name):
            manifest = self._load_manifest(task_dir)
            entry = _entry_by_id(manifest, entry_id) if manifest is not None else None
            if manifest is None or entry is None or entry.get("undone") is not None:
                return False
            entry["undone"] = {"at": time.time(), "by_task": by_task, "action": action, "forced": forced}
            self._save_manifest(task_dir, manifest)
            return True

    def purge(self, *, dry_run: bool = False) -> int:
        """删掉过了保留期的任务日志（按任务目录里最新的修改时间算），返回过期的任务数。"""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * _SECONDS_PER_DAY
        expired = 0
        for session_dir in sorted(path for path in self.root.iterdir() if path.is_dir() and path.name != _LOCK_DIR):
            try:
                with self._locked(session_dir.name):
                    for task_dir in sorted(path for path in session_dir.iterdir() if path.is_dir()):
                        if _newest_mtime(task_dir) > cutoff:
                            continue
                        expired += 1
                        if dry_run:
                            logger.info("[dry-run] would delete edit journal %s", task_dir)
                            continue
                        shutil.rmtree(task_dir, ignore_errors=True)
                    if not dry_run:
                        _remove_if_empty(session_dir)
            except OSError:
                logger.warning("edit journal: purging %s failed", session_dir, exc_info=True)
        return expired

    def purge_session(self, session: str) -> int:
        """会话被拆掉时连同它名下的编辑日志一起删，返回删掉的任务数。"""
        try:
            directory = self.session_dir(session)
        except ValueError:
            return 0
        if not directory.is_dir():
            return 0
        # 拆会话的那一串清理里排在中间，删不掉只记日志，不能拦住后面的对话历史和转写
        try:
            with self._locked(directory.name):
                removed = sum(1 for path in directory.iterdir() if path.is_dir())
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            logger.warning("edit journal: purging session directory %s failed", directory, exc_info=True)
            return 0
        return removed


_UNRECORDED: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
_UNRECORDED_GUARD = threading.Lock()


def _unrecorded_key(scope: JournalScope, path_key: str) -> tuple[str, str, str, str]:
    return (str(scope.workspace_root), scope.session, scope.task_id, path_key)


def _remember_unrecorded(scope: JournalScope, path_key: str, reason: str) -> None:
    # 第一次没记下原文件，这个任务后面再改它也不能记：记下来的只会是改到一半的版本，撤销时却当成原文件放回去
    key = _unrecorded_key(scope, path_key)
    with _UNRECORDED_GUARD:
        _UNRECORDED[key] = reason
        _UNRECORDED.move_to_end(key)
        while len(_UNRECORDED) > _UNRECORDED_LIMIT:
            _UNRECORDED.popitem(last=False)


def _unrecorded_reason(scope: JournalScope, path_key: str) -> str | None:
    with _UNRECORDED_GUARD:
        return _UNRECORDED.get(_unrecorded_key(scope, path_key))


class PendingEdit:
    """一次写盘在编辑日志里对应的条目：写成了调 record_post_image，没写成调 discard。"""

    def __init__(
        self,
        journal: EditJournal | None,
        scope: JournalScope,
        entry_id: int | None,
        created: bool,
        note: JournalNote,
    ) -> None:
        self._journal = journal
        self._scope = scope
        self._entry_id = entry_id
        self._created = created
        self._note = note

    @property
    def note(self) -> JournalNote:
        return self._note

    def record_post_image(self, stamp: file_state.Stamp) -> JournalNote:
        if self._journal is None or self._entry_id is None:
            return self._note
        try:
            self._note = self._journal.finish(self._scope, self._entry_id, stamp)
        except (OSError, ValueError) as exc:
            logger.warning("edit journal: could not record the new version of entry %s: %s", self._entry_id, exc)
            reason = f"编辑日志没能记下改后的版本（{exc}）"
            self._note = JournalNote(False, reason, f"注意：{reason}，撤销这次改动时要加 force=true。")
        return self._note

    def discard(self) -> None:
        if self._journal is None or self._entry_id is None or not self._created:
            return
        try:
            self._journal.drop(self._scope, self._entry_id)
        except (OSError, ValueError):
            logger.warning("edit journal: could not drop entry %s", self._entry_id, exc_info=True)


def record_pre_image(
    target: Any,
    resolved: Path,
    snapshot: file_state.FileSnapshot | None = None,
    *,
    tool: str = "",
    display: str = "",
    revealable: bool = False,
) -> PendingEdit:
    """改文件之前记下原文件，同一个任务里同一个文件只记第一次。写文件的工具都走这里。

    target 是 ToolContext 或 JournalScope。调用方须持有 file_state.edit_lock(resolved, 工作区)，snapshot 传写盘比对用的那一份，
    记下的才是这次改之前的版本。日志写不进去不拦这次改动，只在返回的结论里注明不可撤销。
    """
    scope = target if isinstance(target, JournalScope) else JournalScope.of(target)
    if not scope.session or not scope.task_id:
        return PendingEdit(None, scope, None, False, not_undoable("没有会话号或任务号，编辑日志没有记录"))
    key = _path_key(resolved)
    journal = EditJournal(scope.workspace_root)
    try:
        current = snapshot if snapshot is not None else file_state.take_snapshot(resolved)
        entry_id, created, note = journal.begin(
            scope, resolved, current,
            tool=tool, display=display or str(resolved), revealable=revealable,
            unrecorded=_unrecorded_reason(scope, key),
        )
    except (OSError, ValueError) as exc:
        logger.warning("edit journal: could not record %s: %s", resolved, exc)
        note = not_undoable(f"编辑日志没能写入（{exc}）")
        _remember_unrecorded(scope, key, note.reason)
        return PendingEdit(None, scope, None, False, note)
    if entry_id is None:
        _remember_unrecorded(scope, key, note.reason)
        return PendingEdit(None, scope, None, False, note)
    return PendingEdit(journal, scope, entry_id, created, note)
