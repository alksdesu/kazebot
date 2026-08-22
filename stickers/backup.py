"""表情包库的备份与恢复。

元数据走 manifest.json，图片按内容哈希平铺在 images/ 下，整包原子落盘。
导入前先全量校验再落地，中途失败不留半个库。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .collect import LIBRARY_DIR, PENDING_DIR, sticker_root, store_path
from .store import normalize_name

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

KIND = "clonoth-sticker-library"
SCHEMA_VERSION = 1

MODE_MERGE = "merge"
MODE_REPLACE = "replace"
MODES = (MODE_MERGE, MODE_REPLACE)

MANIFEST_NAME = "manifest.json"
IMAGE_DIR = "images"
BACKUP_DIR = "backups"

DEFAULT_KEEP = 5
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 200_000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")
_ARCHIVE_NAME = re.compile(r"^stickers-(\d{8}-\d{6}-\d{6})\.zip$")
_CHUNK = 1024 * 1024

Progress = Callable[[str, int, int], None]


class BackupError(RuntimeError):
    """备份包不可信或不可用。"""


@dataclass(frozen=True)
class ExportResult:
    path: Path
    stickers: int
    images: int
    pruned: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    imported: int = 0
    skipped: int = 0
    removed: int = 0
    renamed: int = 0
    exported_at: int = 0


@dataclass(frozen=True)
class _Entry:
    digest: str
    row: dict[str, Any]
    rel_path: str
    member: str


@dataclass(frozen=True)
class _Plan:
    members: dict[str, zipfile.ZipInfo] = field(default_factory=dict)
    entries: list[_Entry] = field(default_factory=list)
    sent_log: list[tuple[str, str, int]] = field(default_factory=list)
    exported_at: int = 0


# ── 路径安全 ──

def backup_dir(workspace_root: Path | str) -> Path:
    return sticker_root(Path(workspace_root)) / BACKUP_DIR


def _media_roots(workspace_root: Path) -> tuple[Path, ...]:
    """恢复只许往待审/在库两个目录写。库文件和历史备份都在同级，写歪一步就覆盖它们。"""
    root = sticker_root(workspace_root)
    return tuple((root / name).resolve() for name in (PENDING_DIR, LIBRARY_DIR))


def _safe_member(name: str) -> str:
    """只接受纯相对路径。不可信来源的条目能靠 .. 、绝对路径或盘符写到库外面去。"""
    raw = str(name or "")
    if not raw or raw != raw.strip() or raw.startswith("/") or raw.endswith("/"):
        return ""
    if "\\" in raw or ":" in raw:
        return ""
    if any(part in ("", ".", "..") for part in raw.split("/")):
        return ""
    return raw


def _within(roots: tuple[Path, ...], workspace_root: Path, rel: str) -> Path | None:
    # 多开时 data/stickers 是指向共享库的软链，解析之后才谈得上比对。
    target = (workspace_root / rel).resolve()
    return target if any(base in target.parents for base in roots) else None


def _resolve(roots: tuple[Path, ...], workspace_root: Path, rel: str) -> Path:
    target = _within(roots, workspace_root, rel)
    if target is None:
        raise BackupError(f"备份条目落点越界: {rel!r}")
    return target


# ── 库连接 ──

@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise BackupError(f"表情包库不存在: {path}")
    db = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA synchronous=FULL")
    try:
        yield db
    finally:
        db.close()


def _columns(db: sqlite3.Connection) -> list[str]:
    rows = db.execute("PRAGMA table_info(stickers)").fetchall()
    if not rows:
        raise BackupError("表情包库里没有 stickers 表")
    return [str(row["name"]) for row in rows]


def _library_schema(db: sqlite3.Connection) -> str:
    try:
        row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.DatabaseError:
        return ""
    return str(row[0]) if row else ""


def _tick(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress is not None:
        progress(phase, done, total)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 归档文件 ──

def list_backups(out_dir: Path | str) -> list[Path]:
    """按时间新到旧列出备份。只认自己产出的文件名，目录里别的东西不归这里管。"""
    directory = Path(out_dir)
    if not directory.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        matched = _ARCHIVE_NAME.match(path.name)
        if matched and path.is_file():
            found.append((matched.group(1), path))
    return [path for _, path in sorted(found, key=lambda item: item[0], reverse=True)]


def prune_backups(out_dir: Path | str, *, keep: int = DEFAULT_KEEP) -> list[Path]:
    # keep<=0 是"不轮转"，不是"全删光"：配置写错不该把历史备份清空。
    if keep <= 0:
        return []
    removed: list[Path] = []
    for path in list_backups(out_dir)[keep:]:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("表情包备份轮转删不掉 %s: %s", path, exc)
            continue
        removed.append(path)
    return removed


def _next_archive_path(out_dir: Path) -> Path:
    # 带微秒：文件名即排序键，秒级精度会在连续导出时重名，进而重用刚被轮转删掉的名字。
    for _ in range(100):
        path = out_dir / f"stickers-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.zip"
        if not path.exists():
            return path
    raise BackupError("备份文件名一直重名")


# ── 导出 ──

def export_library(
    workspace_root: Path | str,
    *,
    out_dir: Path | str | None = None,
    keep: int = DEFAULT_KEEP,
    progress: Progress | None = None,
) -> ExportResult:
    """把整个库打成一个 ZIP，写完再改名，读到的永远是完整包。"""
    root = Path(workspace_root)
    target_dir = Path(out_dir) if out_dir is not None else backup_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)

    with _connect(store_path(root)) as db:
        columns = _columns(db)
        library_schema = _library_schema(db)
        # 两条 SELECT 取同一个快照，否则导出期间的发送会让计数和日志对不上。
        db.execute("BEGIN")
        try:
            rows = db.execute("SELECT * FROM stickers ORDER BY created_at,sha256").fetchall()
            sent = db.execute(
                "SELECT conversation_key,sha256,sent_at FROM sent_log"
                " ORDER BY conversation_key,sha256"
            ).fetchall()
        finally:
            db.execute("COMMIT")

    entries: list[dict[str, Any]] = []
    for row in rows:
        record = {name: row[name] for name in columns}
        rel = str(record.get("rel_path") or "")
        suffix = Path(rel).suffix.lower()
        member = ""
        if rel and (root / rel).is_file():
            member = f"{IMAGE_DIR}/{record['sha256']}{suffix if _SUFFIX.match(suffix) else ''}"
        entries.append({"row": record, "image": member})

    path = _next_archive_path(target_dir)
    tmp = target_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
    total = len(entries)
    images = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for index, entry in enumerate(entries, 1):
                member = str(entry["image"])
                if member:
                    try:
                        zf.write(root / str(entry["row"]["rel_path"]), member)
                        images += 1
                    except OSError as exc:
                        logger.warning("表情包备份跳过读不了的图 %s: %s", member, exc)
                        entry["image"] = ""
                _tick(progress, "export", index, total)
            zf.writestr(MANIFEST_NAME, json.dumps(
                {
                    "kind": KIND,
                    "schema": SCHEMA_VERSION,
                    "library_schema": library_schema,
                    "exported_at": int(time.time()),
                    "columns": columns,
                    "stickers": entries,
                    "sent_log": [
                        {
                            "conversation_key": str(item["conversation_key"]),
                            "sha256": str(item["sha256"]),
                            "sent_at": _as_int(item["sent_at"]),
                        }
                        for item in sent
                    ],
                },
                ensure_ascii=False,
            ))
        with tmp.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    logger.info("表情包备份已导出 %s: %d 条 %d 图", path.name, total, images)
    return ExportResult(
        path=path,
        stickers=total,
        images=images,
        pruned=tuple(prune_backups(target_dir, keep=keep)),
    )


# ── 校验 ──

def _read_json(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Any:
    with zf.open(info) as stream:
        return json.loads(stream.read().decode("utf-8-sig"))


def _collect_members(
    zf: zipfile.ZipFile, *, max_total_bytes: int, max_entries: int,
) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > max_entries:
        raise BackupError(f"备份条目数超过上限 {max_entries}")
    members: dict[str, zipfile.ZipInfo] = {}
    declared = 0
    for info in infos:
        raw = info.filename
        name = _safe_member(raw[:-1] if info.is_dir() else raw)
        if not name:
            raise BackupError(f"备份里有越界的路径条目: {raw!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BackupError(f"备份里有符号链接条目: {name}")
        if info.is_dir():
            continue
        if name in members:
            raise BackupError(f"备份里有重复条目: {name}")
        declared += max(_as_int(info.file_size), 0)
        if declared > max_total_bytes:
            raise BackupError(f"备份声明的解包体积超过上限 {max_total_bytes} 字节")
        members[name] = info
    return members


def _plan(
    zf: zipfile.ZipFile,
    *,
    columns: list[str],
    library_schema: str,
    max_total_bytes: int,
    max_entries: int,
) -> _Plan:
    members = _collect_members(zf, max_total_bytes=max_total_bytes, max_entries=max_entries)
    if MANIFEST_NAME not in members:
        raise BackupError("备份缺少 manifest.json")
    manifest = _read_json(zf, members[MANIFEST_NAME])
    if not isinstance(manifest, dict):
        raise BackupError("manifest.json 不是对象")
    if str(manifest.get("kind") or "") != KIND:
        raise BackupError(f"不是表情包库备份: {manifest.get('kind')!r}")
    if _as_int(manifest.get("schema")) != SCHEMA_VERSION:
        raise BackupError(f"备份格式版本不支持: {manifest.get('schema')!r}")
    if str(manifest.get("library_schema") or "") != library_schema:
        raise BackupError(f"备份的库版本与当前库不一致: {manifest.get('library_schema')!r}")
    if list(manifest.get("columns") or []) != columns:
        raise BackupError("备份的表结构与当前库不一致")

    raw_entries = manifest.get("stickers")
    if not isinstance(raw_entries, list):
        raise BackupError("manifest.json 里没有条目表")
    expected = set(columns)
    entries: list[_Entry] = []
    seen: set[str] = set()
    for item in raw_entries:
        row = item.get("row") if isinstance(item, dict) else None
        if not isinstance(row, dict) or set(row.keys()) != expected:
            raise BackupError("备份条目的字段与当前库不一致")
        for key, value in row.items():
            if value is not None and not isinstance(value, (str, int, float)):
                raise BackupError(f"备份条目字段类型无效: {key}")
        digest = str(row.get("sha256") or "")
        if not _SHA256.match(digest):
            raise BackupError(f"备份条目哈希无效: {digest!r}")
        if digest in seen:
            raise BackupError(f"备份条目哈希重复: {digest}")
        seen.add(digest)
        rel = str(row.get("rel_path") or "")
        if rel and not _safe_member(rel):
            raise BackupError(f"备份条目路径越界: {rel!r}")
        member = str(item.get("image") or "")
        if member and member not in members:
            raise BackupError(f"备份缺少图片: {member}")
        if member and not rel:
            raise BackupError(f"备份条目有图却没有落点: {digest}")
        entries.append(_Entry(digest=digest, row=row, rel_path=rel, member=member))

    sent_log: list[tuple[str, str, int]] = []
    for item in manifest.get("sent_log") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("conversation_key") or "")
        digest = str(item.get("sha256") or "")
        if key and _SHA256.match(digest):
            sent_log.append((key, digest, _as_int(item.get("sent_at"))))

    return _Plan(
        members=members,
        entries=entries,
        sent_log=sent_log,
        exported_at=_as_int(manifest.get("exported_at")),
    )


def _stage(
    zf: zipfile.ZipFile,
    plan: _Plan,
    staging: Path,
    *,
    max_total_bytes: int,
    progress: Progress | None,
) -> dict[str, Path]:
    """先把图全解到暂存区并逐张核哈希，一张对不上就整包不认。"""
    staging.mkdir(parents=True, exist_ok=False)
    staged: dict[str, Path] = {}
    budget = max_total_bytes
    total = len(plan.entries)
    for index, entry in enumerate(plan.entries, 1):
        if entry.member:
            target = staging / entry.digest
            digest = hashlib.sha256()
            with zf.open(plan.members[entry.member]) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    # 声明大小可以撒谎，实读也要卡住，否则解包能把磁盘撑爆。
                    budget -= len(chunk)
                    if budget < 0:
                        raise BackupError(f"备份解包体积超过上限 {max_total_bytes} 字节")
                    digest.update(chunk)
                    sink.write(chunk)
            if digest.hexdigest() != entry.digest:
                raise BackupError(f"备份里的图片与哈希对不上: {entry.member}")
            staged[entry.digest] = target
        _tick(progress, "verify", index, total)
    return staged


# ── 导入 ──

def _free_name(db: sqlite3.Connection, wanted: str, digest: str) -> str:
    base = normalize_name(wanted, digest=digest)
    candidate = base
    for serial in range(2, 100):
        row = db.execute(
            "SELECT 1 FROM stickers WHERE name=? AND sha256<>?", (candidate, digest)
        ).fetchone()
        if not row:
            return candidate
        candidate = f"{base}{serial}"
    return f"{base}{digest[:6]}"


def _apply(
    db: sqlite3.Connection,
    plan: _Plan,
    staged: dict[str, Path],
    *,
    workspace_root: Path,
    mode: str,
    columns: list[str],
    progress: Progress | None,
) -> ImportResult:
    roots = _media_roots(workspace_root)
    placed: list[Path] = []
    stale: set[Path] = set()
    imported = skipped = removed = renamed = 0
    insert = (
        f"INSERT INTO stickers({','.join(columns)})"
        f" VALUES({','.join('?' * len(columns))})"
    )
    total = len(plan.entries)

    db.execute("BEGIN IMMEDIATE")
    try:
        if mode == MODE_REPLACE:
            keeping = {
                _resolve(roots, workspace_root, entry.rel_path)
                for entry in plan.entries if entry.rel_path
            }
            for row in db.execute("SELECT sha256,rel_path FROM stickers").fetchall():
                removed += 1
                rel = str(row["rel_path"] or "")
                old = _within(roots, workspace_root, rel) if rel else None
                if old is not None and old not in keeping:
                    stale.add(old)
            db.execute("DELETE FROM sent_log")
            db.execute("DELETE FROM stickers")

        known = {str(row[0]) for row in db.execute("SELECT sha256 FROM stickers").fetchall()}
        for index, entry in enumerate(plan.entries, 1):
            if entry.digest in known:
                skipped += 1
                _tick(progress, "install", index, total)
                continue
            row = dict(entry.row)
            name = _free_name(db, str(row.get("name") or ""), entry.digest)
            if name != row.get("name"):
                renamed += 1
            row["name"] = name
            db.execute(insert, [row[column] for column in columns])
            source = staged.get(entry.digest)
            if source is not None:
                # 落点被别的哈希占着就整包不认：写下去等于把在库的那张图悄悄换掉。
                if db.execute(
                    "SELECT 1 FROM stickers WHERE rel_path=? AND sha256<>?",
                    (entry.rel_path, entry.digest),
                ).fetchone():
                    raise BackupError(f"备份条目的落点已被别的图占用: {entry.rel_path}")
                target = _resolve(roots, workspace_root, entry.rel_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                fresh = not target.exists()
                os.replace(source, target)
                if fresh:
                    placed.append(target)
            known.add(entry.digest)
            imported += 1
            _tick(progress, "install", index, total)

        for key, digest, sent_at in plan.sent_log:
            if digest in known:
                db.execute(
                    "INSERT INTO sent_log(conversation_key,sha256,sent_at) VALUES(?,?,?)"
                    " ON CONFLICT(conversation_key,sha256) DO UPDATE SET sent_at=excluded.sent_at",
                    (key, digest, sent_at),
                )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        for path in placed:
            path.unlink(missing_ok=True)
        raise

    for path in stale:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("表情包恢复删不掉旧图 %s: %s", path, exc)
    return ImportResult(
        imported=imported,
        skipped=skipped,
        removed=removed,
        renamed=renamed,
        exported_at=plan.exported_at,
    )


def import_library(
    archive: Path | str,
    workspace_root: Path | str,
    *,
    mode: str = MODE_MERGE,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    progress: Progress | None = None,
) -> ImportResult:
    """从备份包恢复。merge 跳过已有哈希，replace 先清空再导入。"""
    if mode not in MODES:
        raise BackupError(f"未知导入模式: {mode!r}")
    root = Path(workspace_root)
    staging = sticker_root(root) / f".import.{uuid.uuid4().hex}"

    with _connect(store_path(root)) as db:
        columns = _columns(db)
        library_schema = _library_schema(db)
        try:
            with zipfile.ZipFile(Path(archive)) as zf:
                plan = _plan(
                    zf,
                    columns=columns,
                    library_schema=library_schema,
                    max_total_bytes=max_total_bytes,
                    max_entries=max_entries,
                )
                try:
                    staged = _stage(
                        zf, plan, staging,
                        max_total_bytes=max_total_bytes, progress=progress,
                    )
                    result = _apply(
                        db, plan, staged,
                        workspace_root=root, mode=mode, columns=columns, progress=progress,
                    )
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
        except (zipfile.BadZipFile, zlib.error, EOFError, ValueError,
                sqlite3.DatabaseError) as exc:
            raise BackupError(f"备份包读不了: {exc}") from exc

    logger.info(
        "表情包备份已恢复 %s: 入库 %d 跳过 %d 清除 %d 改名 %d",
        Path(archive).name, result.imported, result.skipped, result.removed, result.renamed,
    )
    return result
