#!/usr/bin/env python3
"""Periodic cleanup for the Clonoth data directory.

Run by ``clonoth-data-cleanup.timer`` every hour by default. Pure Python, with no
LLM calls. The jobs include event/signal log rotation, expiration of temporary
artifacts and child-session state, conservative QQ cache cleanup, and stale
non-constant memory entry cleanup.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

try:
    # Normal package import when launched from the repository root/runtime.
    from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log
except ModuleNotFoundError:  # pragma: no cover - direct ``python engine/data_cleanup.py``
    from eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log

# ── Paths ──────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS_FILE = DATA_DIR / "events.jsonl"
SIGNALS_FILE = DATA_DIR / "signals.jsonl"
LOG_FILE = DATA_DIR / "logs" / "cleanup.log"

# ── Thresholds ─────────────────────────────────
# [2026-07-16] 轮转阈值/保留份数可通过环境变量调整，便于在
# 事件高频写入的部署里收紧阈值（例如降到 20MB）。EventLog 写入侧
# 的在线护栏（见 supervisor/eventlog.py）会读取同名环境变量，避免
# 单小时突发写入把活动日志擑到数 GB 才被定时 timer 轮转。
EVENTS_MAX_BYTES = int(os.getenv("CLONOTH_EVENTS_MAX_BYTES", str(50 * 1024 * 1024)))   # 50 MB
EVENTS_BACKUPS = int(os.getenv("CLONOTH_EVENTS_BACKUPS", "3"))

TEMP_MAX_AGE = 24 * 3600              # 24 h
ARTIFACT_MAX_AGE = 24 * 3600          # 24 h
ATTACH_MAX_AGE = 24 * 3600            # 24 h

# QQ/NapCat 内部缓存保守清理阈值。QQ Electron 运行时可能在独立 mount
# namespace 中使用 /app/.config/QQ；systemd timer 在宿主 namespace 运行时
# 需要通过 /proc/<qq-pid>/root/... 访问这些路径。
#
# NTQQ 常见缓存还包括账号目录下的 Image/Video，例如：
#   /app/.config/QQ/<qq号>/Image
#   /app/.config/QQ/<qq号>/Video
# 这些目录里的文件不一定都有标准扩展名，所以清理逻辑除扩展名外，
# 也会按父目录名 image/video/cache/tmp/download 等识别旧缓存文件。
QQ_CACHE_MAX_AGE = float(os.getenv("CLONOTH_QQ_CACHE_MAX_AGE_SECONDS", str(7 * 24 * 3600)))
QQ_CACHE_ROOTS_RAW = os.getenv("CLONOTH_QQ_CACHE_ROOTS", "")
QQ_CACHE_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
}
QQ_CACHE_DIR_KEYWORDS = {
    "cache", "cache_data", "code cache", "gpucache", "blob_storage",
    "tmp", "temp", "download", "downloads", "thumb", "thumbnail", "thumbnails",
    # Explicitly cover NTQQ account media cache directories: <qq号>/Image and <qq号>/Video.
    "image", "images", "pic", "pics", "video", "videos", "emoji", "face", "record",
}

# Persistent memory entry cleanup. Entries with constant=true are protected.
# For ordinary entries, use last_hit_at first, then updated_at, then created_at.
# This keeps recently activated memories even if they were created long ago.
MEMORY_ENTRY_MAX_AGE = float(os.getenv("CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS", str(14 * 24 * 3600)))

TEMP_GLOBS = [
    "chat_dump_*", "chat_chunk_*", "chat_compact_*", "chat_messages_*",
]

# Child Session 隔离（Phase C）：过期 child session 的 JSONL 文件清理
# child_*.jsonl 超过此时间未修改则删除。与 runtime.yaml 中 child_session.ttl_hours 对齐。
CHILD_SESSION_MAX_AGE = 24 * 3600     # 24 h（与默认 TTL 一致）

# Phase D：node_contexts 目录清理。child session 已替代 snapshot 机制，
# 保留 48h 宽裕期后清理旧文件（比 child session TTL 长一倍，确保兼容期充分）。
NODE_CONTEXTS_MAX_AGE = 48 * 3600     # 48 h

# TaskRecord 转写目录 data/transcripts/。文件形如 {session_id}.jsonl 与 child_*.jsonl，
# 是每个任务的执行转写，供事后分析/记忆提取使用。长期未更新即可回收。
# 为什么：之前该目录从不清理，会无限累积（本例已 233 个）。
TRANSCRIPTS_MAX_AGE = float(os.getenv("CLONOTH_TRANSCRIPTS_MAX_AGE_SECONDS", str(14 * 24 * 3600)))  # 14 d

# engine/supervisor 进程日志 data/logs/*.log。重启/重连会产生大量空日志文件
# 按龄清理，保留 cleanup.log。
LOG_MAX_AGE = float(os.getenv("CLONOTH_LOG_MAX_AGE_SECONDS", str(3 * 24 * 3600)))  # 3 d
LOG_KEEP_NAMES = {"cleanup.log"}

# LLM 错误快照 data/llm_error_snapshots/：调试用，保留 3 天。
LLM_ERROR_SNAPSHOT_MAX_AGE = float(os.getenv("CLONOTH_LLM_ERROR_SNAPSHOT_MAX_AGE_SECONDS", str(3 * 24 * 3600)))

# stocktool 行情缓存 data/stocktool/cache/：行情/解析缓存，过期可重拉，保留 1 天。
STOCKTOOL_CACHE_MAX_AGE = float(os.getenv("CLONOTH_STOCKTOOL_CACHE_MAX_AGE_SECONDS", str(1 * 24 * 3600)))

# OneBot 引用消息附件索引缓存 data/cache/onebot_reply_attachments.json：
# 指向已落盘附件的路径索引；附件本体由 attachments 清理，这里按文件龄保守清（与 attachments 同龄期）。
ONEBOT_CACHE_MAX_AGE = float(os.getenv("CLONOTH_ONEBOT_CACHE_MAX_AGE_SECONDS", str(24 * 3600)))



def _log_init():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── 1. events.jsonl rotation ──────────────────
def rotate_events():
    # The shared helper acquires the same cross-process lock used by EventLog
    # append/online rotation and re-checks active size plus backup state inside it.
    try:
        rotated = rotate_event_log(
            EVENTS_FILE,
            max_bytes=EVENTS_MAX_BYTES,
            backups=EVENTS_BACKUPS,
        )
    except (OSError, TimeoutError):
        logging.exception("[events] rotation failed")
        return
    if rotated:
        logging.info("[events] rotated")
    else:
        logging.info("[events] absent or below threshold, skip")


# ── 1b. signals.jsonl rotation ────────────────
def rotate_signals():
    # 走和 events 同一个带锁实现。原来这里是另一套裸 rename：没有锁、没有事务，
    # 和 engine 进程里的在线轮转并发时会互相踩掉备份编号。
    try:
        rotated = rotate_event_log(
            SIGNALS_FILE,
            max_bytes=SIGNALS_MAX_BYTES,
            backups=SIGNALS_BACKUPS,
        )
    except (OSError, TimeoutError):
        logging.exception("[signals] rotation failed")
        return
    logging.info("[signals] rotated" if rotated else "[signals] absent or below threshold, skip")


# ── dry-run ───────────────────────────────────
# 这个脚本此前只由 systemd timer 触发，Windows 部署上从来没跑过，所以标称的所有保留
# 策略都没生效过。要在进程内定期调它，就得先有一个能看清「会删什么」的模式。
DRY_RUN = False


def _unlink(path: Path) -> None:
    if DRY_RUN:
        logging.info("[dry-run] would delete file %s", path)
        return
    path.unlink()


def _rmdir(path: Path) -> None:
    if DRY_RUN:
        logging.info("[dry-run] would remove empty dir %s", path)
        return
    path.rmdir()


def _write_text(path: Path, text: str) -> None:
    if DRY_RUN:
        logging.info("[dry-run] would rewrite %s (%d bytes)", path, len(text))
        return
    path.write_text(text, encoding="utf-8")


# ── generic dir purge ─────────────────────────
def purge_dir(directory: Path, max_age: float, label: str, recursive=False):
    """Delete files older than max_age seconds. Remove empty sub-dirs if recursive."""
    if not directory.exists():
        return
    cutoff = time.time() - max_age
    deleted = 0

    targets = list(directory.rglob("*")) if recursive else list(directory.iterdir())
    for p in targets:
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                _unlink(p)
                deleted += 1
        except Exception:
            pass

    if recursive:
        for dirpath, _, _ in os.walk(str(directory), topdown=False):
            dp = Path(dirpath)
            if dp == directory:
                continue
            try:
                next(dp.iterdir())         # non-empty → skip
            except StopIteration:
                _rmdir(dp)                 # empty → remove
            except Exception:
                pass

    logging.info("[%s] deleted %d files", label, deleted)


# ── QQ/NapCat internal cache cleanup ───────────
def _split_configured_roots(raw: str) -> list[Path]:
    roots: list[Path] = []
    for item in str(raw or "").replace("\n", ",").split(","):
        value = item.strip()
        if value:
            roots.append(Path(value))
    return roots


def _read_proc_strings(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except Exception:
        return []
    return [x.decode("utf-8", "replace") for x in data.split(b"\0") if x]


def _append_unique_path(paths: list[Path], path: Path) -> None:
    key = str(path)
    if not key or key == ".":
        return
    if all(str(x) != key for x in paths):
        paths.append(path)


def _discover_qq_cache_roots() -> list[Path]:
    """Return conservative candidate roots for NTQQ/NapCat cache cleanup.

    On the Debian server QQ is launched from /opt/QQ/qq but runs with HOME=/app and
    Chromium child processes use --user-data-dir=/app/.config/QQ.  When /app lives
    only in QQ's mount namespace, the host-side timer can still access it through
    /proc/<pid>/root/app/....
    """
    roots: list[Path] = []
    for p in _split_configured_roots(QQ_CACHE_ROOTS_RAW):
        _append_unique_path(roots, p)

    for p in (Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ")):
        _append_unique_path(roots, p)

    proc = Path("/proc")
    if not proc.exists():
        return roots

    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cmdline = _read_proc_strings(pid_dir / "cmdline")
        environ = _read_proc_strings(pid_dir / "environ")
        haystack = "\n".join(cmdline + environ)
        if "/opt/QQ/qq" not in haystack and "--user-data-dir=" not in haystack and "napcat" not in haystack.lower():
            continue

        proc_root = pid_dir / "root"
        for index, arg in enumerate(cmdline):
            user_data_dir = ""
            if arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and index + 1 < len(cmdline):
                user_data_dir = cmdline[index + 1]
            if user_data_dir.startswith("/"):
                _append_unique_path(roots, proc_root / user_data_dir.lstrip("/"))

        home = ""
        for item in environ:
            if item.startswith("HOME="):
                home = item.split("=", 1)[1].strip()
                break
        if home.startswith("/"):
            _append_unique_path(roots, proc_root / home.lstrip("/") / ".config" / "QQ")
            _append_unique_path(roots, proc_root / home.lstrip("/") / "napcat")

    return roots


def _is_safe_qq_cache_root(root: Path) -> bool:
    text = str(root)
    if not text or text in {"/", "/app", "/root", "/proc"}:
        return False
    lowered = text.lower()
    return "qq" in lowered or "napcat" in lowered


def _is_cache_like_relative_path(rel: Path) -> bool:
    for part in rel.parts[:-1]:
        lowered = part.lower()
        compact = lowered.replace(" ", "")
        if any(keyword in lowered or keyword.replace(" ", "") in compact for keyword in QQ_CACHE_DIR_KEYWORDS):
            return True
    return False


def purge_qq_internal_cache():
    """Clean stale NTQQ/NapCat image/video/cache files.

    This deliberately does not wipe whole QQ data directories. It only removes old
    media files or files inside cache-like directories, including NTQQ account
    media caches such as <qq号>/Image and <qq号>/Video. This avoids deleting login
    state, account databases, and other durable QQ configuration.
    """
    if QQ_CACHE_MAX_AGE <= 0:
        logging.info("[qq_cache] disabled")
        return

    cutoff = time.time() - QQ_CACHE_MAX_AGE
    deleted = 0
    deleted_bytes = 0
    scanned_roots = 0
    roots = _discover_qq_cache_roots()

    for root in roots:
        try:
            if not _is_safe_qq_cache_root(root) or not root.exists() or not root.is_dir():
                continue
            scanned_roots += 1
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    if st.st_mtime >= cutoff:
                        continue
                    rel = p.relative_to(root)
                    is_media = p.suffix.lower() in QQ_CACHE_MEDIA_EXTENSIONS
                    is_cache_file = _is_cache_like_relative_path(rel)
                    if not (is_media or is_cache_file):
                        continue
                    size = st.st_size
                    _unlink(p)
                    deleted += 1
                    deleted_bytes += size
                except Exception:
                    continue

            # Remove empty cache/media subdirectories, but never the root itself.
            for dirpath, _, _ in os.walk(str(root), topdown=False):
                dp = Path(dirpath)
                if dp == root:
                    continue
                try:
                    rel = dp.relative_to(root)
                    if not _is_cache_like_relative_path(rel / "placeholder"):
                        continue
                    next(dp.iterdir())
                except StopIteration:
                    try:
                        _rmdir(dp)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as exc:
            logging.info("[qq_cache] skipped %s: %s", root, exc)

    logging.info(
        "[qq_cache] scanned_roots=%d deleted=%d freed=%dKB max_age_hours=%.1f",
        scanned_roots,
        deleted,
        deleted_bytes // 1024,
        QQ_CACHE_MAX_AGE / 3600,
    )


# ── persistent memory entry cleanup ───────────
def _parse_iso_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _memory_entry_reference_ts(entry: dict[str, object], hit_stamp: str = "") -> float | None:
    """Age an entry by the newest timestamp we know about, not the first one present.

    原来是「取第一个非空」，而排在最前的 last_hit_at 从来没有任何代码写进 YAML
    （唯一的写入方早已删除，命中改记进 .hit_cache.json，而这个 sweep 从不打开它）。
    于是基准恒为 updated_at —— 一条天天被注入的记忆和一条从没被命中过的，第 14 天一起被删。
    """
    stamps = [_parse_iso_timestamp(entry.get(key)) for key in ("last_hit_at", "updated_at", "created_at")]
    stamps.append(_parse_iso_timestamp(hit_stamp))
    known = [ts for ts in stamps if ts is not None]
    return max(known) if known else None


def _load_hit_stamps(mem_root: Path) -> tuple[dict, object]:
    """Return (hit_cache_dict, lookup_fn); both empty/no-op when unavailable."""
    try:
        from engine.memory_hit_cache import hit_timestamp, read_hit_cache

        return read_hit_cache(mem_root.parent.parent), hit_timestamp
    except Exception as exc:
        # 读不到命中数据时按「一条都没命中过」处理会让 sweep 更激进，所以退化成
        # 完全不参考命中，只用 YAML 里的时间戳。
        logging.info("[memory] hit cache unavailable, ageing by yaml timestamps only: %s", exc)
        return {}, (lambda _cache, _ns, _eid: "")


def purge_expired_memory_entries():
    """Remove stale non-constant memory entries from data/memory/**/*.yaml.

    Memory books are YAML files shaped like {book, entries}.  Conversation-scoped
    memory namespaces live under data/memory/<namespace>/*.yaml, while older/global
    books live directly under data/memory/*.yaml.  We scan both layouts.
    """
    if MEMORY_ENTRY_MAX_AGE <= 0:
        logging.info("[memory] entry cleanup disabled")
        return

    mem_root = DATA_DIR / "memory"
    if not mem_root.exists() or not mem_root.is_dir():
        return

    cutoff = time.time() - MEMORY_ENTRY_MAX_AGE
    scanned_books = 0
    deleted_entries = 0
    removed_empty_books = 0
    skipped_archives = 0
    hit_cache, hit_lookup = _load_hit_stamps(mem_root)

    for book_path in sorted(mem_root.rglob("*.yaml")):
        try:
            namespace = book_path.parent.relative_to(mem_root).as_posix()
        except ValueError:
            namespace = ""
        if namespace == ".":
            namespace = ""
        if namespace.startswith("user_"):
            # 跨会话人物档案不参与年龄淘汰：画像的价值就在长期积累，
            # 「三个月前提过对象生日」正是它存在的理由。只能手工删。
            skipped_archives += 1
            continue
        try:
            raw = yaml.safe_load(book_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.info("[memory] skipped unreadable book %s: %s", book_path, exc)
            continue
        if not isinstance(raw, dict):
            continue
        entries = raw.get("entries")
        if not isinstance(entries, list):
            continue

        scanned_books += 1
        kept: list[object] = []
        changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            if bool(entry.get("constant", False)):
                kept.append(entry)
                continue
            # 只清自动提取写的条目。空 source 一律当手工记忆保护 —— save_memory 早期
            # 不写这个字段，把它判成 auto 就会删掉一批本该只有人能删的东西。
            # dream 的提示词规则也是「保护 source != auto」，两边必须一致。
            if str(entry.get("source") or "").strip() != "auto":
                kept.append(entry)
                continue
            ref_ts = _memory_entry_reference_ts(
                entry, hit_lookup(hit_cache, namespace, str(entry.get("id") or "")),
            )
            # Entries without timestamps are legacy/manual data; keep them rather
            # than guessing. save_memory now writes created_at for new entries.
            if ref_ts is None or ref_ts >= cutoff:
                kept.append(entry)
                continue
            deleted_entries += 1
            changed = True

        if not changed:
            continue
        raw["entries"] = kept
        try:
            if kept:
                _write_text(book_path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
            else:
                _unlink(book_path)
                removed_empty_books += 1
        except Exception as exc:
            logging.info("[memory] failed to update book %s: %s", book_path, exc)

    # Remove empty namespace directories after empty book deletion.
    for dirpath, _, _ in os.walk(str(mem_root), topdown=False):
        dp = Path(dirpath)
        if dp == mem_root:
            continue
        try:
            next(dp.iterdir())
        except StopIteration:
            try:
                _rmdir(dp)
            except Exception:
                pass
        except Exception:
            pass

    removed_hit_keys = 0
    try:
        # 直跑 data_cleanup.py（Windows 定时器）时 engine 包不在 path，退化成跳过孤儿回收。
        from engine.memory_hit_cache import prune_orphan_namespace_hits

        surviving = {p.name for p in mem_root.iterdir() if p.is_dir()}
        removed_hit_keys = prune_orphan_namespace_hits(DATA_DIR.parent, surviving)
    except Exception as exc:
        logging.info("[memory] orphan hit-key sweep skipped: %s", exc)

    logging.info(
        "[memory] scanned_books=%d deleted_entries=%d removed_empty_books=%d "
        "skipped_archives=%d removed_hit_keys=%d max_age_hours=%.1f",
        scanned_books,
        deleted_entries,
        removed_empty_books,
        skipped_archives,
        removed_hit_keys,
        MEMORY_ENTRY_MAX_AGE / 3600,
    )


# ── temp glob purge ───────────────────────────
def purge_temp_globs():
    cutoff = time.time() - TEMP_MAX_AGE
    deleted = 0
    for pat in TEMP_GLOBS:
        for p in DATA_DIR.glob(pat):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    _unlink(p)
                    deleted += 1
            except Exception:
                pass
    logging.info("[temp] deleted %d files", deleted)


# ── Child Session JSONL cleanup ───────────────
def purge_expired_child_sessions():
    """清理过期的 child session JSONL 文件。

    Child Session 隔离（Phase C）：扫描 data/conversations/ 下所有 child_*.jsonl，
    按文件修改时间判断是否超过 CHILD_SESSION_MAX_AGE。超期则删除文件。
    映射表的清理由 supervisor 侧在 dispatch 时懒过期处理。
    """
    conv_dir = DATA_DIR / "conversations"
    if not conv_dir.exists():
        return
    cutoff = time.time() - CHILD_SESSION_MAX_AGE
    deleted = 0
    for p in conv_dir.glob("child_*.jsonl"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                _unlink(p)
                deleted += 1
        except Exception:
            pass
    logging.info("[child_sessions] deleted %d expired files", deleted)


# ── transcripts cleanup ────────────────
def purge_expired_transcripts():
    """清理 data/transcripts/ 中长期未更新的 TaskRecord 转写文件。

    engine.task_record 向 data/transcripts/{session_id}.jsonl 追写任务记录，之前
    从不清理，会随会话无限累积。按文件 mtime 删除超过 TRANSCRIPTS_MAX_AGE 的条目。
    """
    if TRANSCRIPTS_MAX_AGE <= 0:
        logging.info("[transcripts] cleanup disabled")
        return
    purge_dir(DATA_DIR / "transcripts", TRANSCRIPTS_MAX_AGE, "transcripts")


# ── logs cleanup ─────────────────────
def purge_old_logs():
    """清理 data/logs/ 下过期的进程日志。

    engine/supervisor 每次重启/重连都会新建日志文件，其中大量为 0 字节空文件，
    长期不清会堆积成千上万个小文件。保留 cleanup.log（本脚本自身日志）。
    """
    if LOG_MAX_AGE <= 0:
        logging.info("[logs] cleanup disabled")
        return
    log_dir = DATA_DIR / "logs"
    if not log_dir.exists():
        return
    cutoff = time.time() - LOG_MAX_AGE
    deleted = 0
    for p in log_dir.iterdir():
        try:
            if not p.is_file() or p.name in LOG_KEEP_NAMES:
                continue
            if p.stat().st_mtime < cutoff:
                _unlink(p)
                deleted += 1
        except Exception:
            pass
    logging.info("[logs] deleted %d files", deleted)


# ── stocktool cache cleanup ─────────────
def purge_stocktool_cache():
    """清理 data/stocktool/cache/ 下过期行情/解析缓存。过期缓存下次会重拉。"""
    if STOCKTOOL_CACHE_MAX_AGE <= 0:
        logging.info("[stocktool_cache] cleanup disabled")
        return
    purge_dir(DATA_DIR / "stocktool" / "cache", STOCKTOOL_CACHE_MAX_AGE,
              "stocktool_cache", recursive=True)


# ── main ──────────────────────────────────────
def main(argv: list[str] | None = None):
    global DRY_RUN

    args = list(sys.argv[1:] if argv is None else argv)
    DRY_RUN = "--dry-run" in args
    _log_init()
    logging.info("=== data cleanup start%s ===", " (dry-run)" if DRY_RUN else "")

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        # Phase D：node_contexts 已被 child session 替代，启用定期清理
        (purge_dir, dict(directory=DATA_DIR / "node_contexts",
                         max_age=NODE_CONTEXTS_MAX_AGE, label="node_contexts",
                         recursive=True)),
        (purge_expired_child_sessions, {}),
        (purge_temp_globs, {}),
        (purge_dir, dict(directory=DATA_DIR / "temp",
                         max_age=TEMP_MAX_AGE, label="temp", recursive=True)),
        (purge_dir, dict(directory=DATA_DIR / "temp_summary",
                         max_age=TEMP_MAX_AGE, label="temp_summary")),
        (purge_dir, dict(directory=DATA_DIR / "artifacts",
                         max_age=ARTIFACT_MAX_AGE, label="artifacts")),
        (purge_dir, dict(directory=DATA_DIR / "attachments",
                         max_age=ATTACH_MAX_AGE, label="attachments", recursive=True)),
        # OneBot 引用附件索引缓存（与 attachments 同龄期，避免指向已删附件的旧索引堆积）。
        (purge_dir, dict(directory=DATA_DIR / "cache",
                         max_age=ONEBOT_CACHE_MAX_AGE, label="onebot_cache", recursive=True)),
        (purge_qq_internal_cache, {}),
        (purge_expired_memory_entries, {}),
        # 新增：之前未覆盖的目录
        (purge_expired_transcripts, {}),
        (purge_old_logs, {}),
        (purge_dir, dict(directory=DATA_DIR / "llm_error_snapshots",
                         max_age=LLM_ERROR_SNAPSHOT_MAX_AGE, label="llm_error_snapshots",
                         recursive=True)),
        (purge_stocktool_cache, {}),
    ]

    for fn, kw in tasks:
        try:
            fn(**kw)
        except Exception as e:
            logging.error("[%s] %s", fn.__name__, e)

    logging.info("=== data cleanup done ===")


if __name__ == "__main__":
    main()
