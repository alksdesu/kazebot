#!/usr/bin/env python3
"""Periodic cleanup for the Clonoth data directory.

Run by ``clonoth-data-cleanup.timer`` every hour by default. Pure Python, with no
LLM calls. The jobs include event/signal log rotation, expiration of temporary
artifacts and child-session state, stale non-constant memory entry cleanup,
and each platform's own maintenance (adapter and client caches) through the
platform's maintenance hook.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    # Normal package import when launched from the repository root/runtime.
    from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log
except ModuleNotFoundError:  # pragma: no cover - direct ``python engine/data_cleanup.py``
    from eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # 直接 ``python engine/data_cleanup.py`` 时项目根不在路径上
    sys.path.insert(0, str(_REPO_ROOT))
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    MemoryBookUnreadable,
    _load_book,
    _memory_write_lock,
    _save_book,
    memory_entry_constant,
)
from engine.memory_namespace import (  # noqa: E402
    SUBJECT_PREFIX,
    is_namespace_conversation_key,
    memory_root,
    namespace_dirs,
)
from platforms.descriptor import HOOK_MAINTENANCE  # noqa: E402
from platforms.maintenance import SCOPE_WORKSPACE, SCOPES  # noqa: E402
from platforms.registry import MissingPlatformHook, current_platform, platform_hook  # noqa: E402
from workspace import resolve_workspace_root  # noqa: E402

# ── Paths ──────────────────────────────────────
DATA_DIR = resolve_workspace_root(_REPO_ROOT) / "data"
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
# dream 的整理任务会话。留 7 天够回溯几轮整理做了什么，再久没有价值。
TASK_CONVERSATION_MAX_AGE = float(
    os.getenv("CLONOTH_TASK_CONVERSATION_MAX_AGE_SECONDS", str(7 * 24 * 3600)))

# Phase D：node_contexts 目录清理。child session 已替代 snapshot 机制，
# 保留 48h 宽裕期后清理旧文件（比 child session TTL 长一倍，确保兼容期充分）。
NODE_CONTEXTS_MAX_AGE = 48 * 3600     # 48 h

# TaskRecord 转写目录 data/transcripts/。文件形如 {session_id}.jsonl 与 child_*.jsonl，
# 是每个任务的执行转写，供事后分析/记忆提取使用。长期未更新即可回收。
# 为什么：之前该目录从不清理，会无限累积（本例已 233 个）。
TRANSCRIPTS_MAX_AGE = float(os.getenv("CLONOTH_TRANSCRIPTS_MAX_AGE_SECONDS", str(14 * 24 * 3600)))  # 14 d

# engine/supervisor 进程日志 data/logs/*.log。重启/重连会产生大量空日志文件
# 按龄清理。cleanup.log 是本脚本自己的；supervisor.log 由 supervisor 按大小轮转，
# 开着的文件删掉后进程会一直往已删的文件里写。
LOG_MAX_AGE = float(os.getenv("CLONOTH_LOG_MAX_AGE_SECONDS", str(3 * 24 * 3600)))  # 3 d
LOG_KEEP_NAMES = {"cleanup.log", "supervisor.log"}
# ProcessManager 每拉起一次子进程就新开一份 <名字>-<年月日>-<时分秒>.log，最新那份可能还开着。
_SPAWN_LOG = re.compile(r"^(?P<series>.+)-\d{8}-\d{6}\.log$")

# LLM 错误快照 data/llm_error_snapshots/：调试用，保留 3 天。
LLM_ERROR_SNAPSHOT_MAX_AGE = float(os.getenv("CLONOTH_LLM_ERROR_SNAPSHOT_MAX_AGE_SECONDS", str(3 * 24 * 3600)))

# stocktool 行情缓存 data/stocktool/cache/：行情/解析缓存，过期可重拉，保留 1 天。
STOCKTOOL_CACHE_MAX_AGE = float(os.getenv("CLONOTH_STOCKTOOL_CACHE_MAX_AGE_SECONDS", str(1 * 24 * 3600)))


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


def _unlink_idle_conversation(path: Path, cutoff: float) -> bool:
    """超期的会话文件拿着会话锁再判一次才删：两次判断之间有人写进来的话不删。"""
    if DRY_RUN:
        logging.info("[dry-run] would delete file %s", path)
        return True
    return ConversationStore(path.parent).delete_if_idle_since(path.stem, cutoff)


def _rmdir(path: Path) -> None:
    if DRY_RUN:
        logging.info("[dry-run] would remove empty dir %s", path)
        return
    path.rmdir()


def _rewrite_book(path: Path, data: dict) -> None:
    if DRY_RUN:
        logging.info("[dry-run] would rewrite %s (%d entries left)", path, len(data.get("entries") or []))
        return
    _save_book(path, data)


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


# ── platform maintenance ──────────────────────
# 宿主级的维护（清 NapCat 容器里的缓存这类）要 root。装了宿主清理的机器由它单独跑，实例的清理只管自己的工作区。
CLEANUP_SCOPES = {"all": frozenset(SCOPES), "workspace": frozenset({SCOPE_WORKSPACE})}
CLEANUP_SCOPE_ENV = "CLONOTH_MAINTENANCE_SCOPE"


@dataclass(frozen=True)
class _CleanupContext:
    """交给平台 maintenance 钩子的上下文：删文件都走这里，演练模式才管得住。"""

    workspace_root: Path
    data_dir: Path

    @property
    def dry_run(self) -> bool:
        return DRY_RUN

    def remove_file(self, path: Path) -> None:
        _unlink(path)

    def remove_empty_dir(self, path: Path) -> None:
        _rmdir(path)

    def purge_dir(self, directory: Path, max_age: float, label: str, *, recursive: bool = False) -> None:
        purge_dir(directory, max_age, label, recursive=recursive)


def run_platform_maintenance(scopes: frozenset[str] = CLEANUP_SCOPES["all"]):
    """本实例平台自己的定期维护，比如清适配器和平台客户端攒的缓存，由平台描述的 maintenance 钩子给出。

    只跑本实例在跑的那个平台：别的平台的客户端不在这台实例底下，清它的缓存目录清的是别人的东西。
    scopes 之外的不跑，只记一笔，免得日志里看不出那一项去哪了。
    """
    context = _CleanupContext(workspace_root=DATA_DIR.parent, data_dir=DATA_DIR)
    platform = current_platform()
    try:
        tasks = platform_hook(platform, HOOK_MAINTENANCE).maintenance_tasks()
    except MissingPlatformHook:
        return
    except Exception as exc:
        logging.error("[%s] maintenance hook unavailable: %s", platform.id, exc)
        return
    for task in tasks:
        if task.scope not in scopes:
            logging.info("[%s.%s] skipped: %s task, left to the host cleanup", platform.id, task.name, task.scope)
            continue
        try:
            task.run(context)
        except Exception as exc:
            logging.error("[%s.%s] %s", platform.id, task.name, exc)


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


_HitLookup = Callable[[dict, str, str], str]


def _memory_entry_expired(
    entry: object, namespace: str, cutoff: float, hit_cache: dict, hit_lookup: _HitLookup,
) -> bool:
    if not isinstance(entry, dict) or memory_entry_constant(entry):
        return False
    # 只清自动提取写的条目。没有 source 的条目里有人手写的，一律当手工记忆保护，
    # 判成 auto 就会删掉一批本该只有人能删的东西。
    # dream 的提示词规则也是「保护 source != auto」，两边必须一致。
    if str(entry.get("source") or "").strip() != "auto":
        return False
    ref_ts = _memory_entry_reference_ts(
        entry, hit_lookup(hit_cache, namespace, str(entry.get("id") or "")),
    )
    # 一个时间戳都没有的条目判不出年龄，留着不猜。
    return ref_ts is not None and ref_ts < cutoff


@dataclass
class _MemorySweep:
    scanned_books: int = 0
    deleted_entries: int = 0
    removed_empty_books: int = 0
    skipped_archives: int = 0


def _expire_book_locked(
    book_path: Path, namespace: str, cutoff: float, hit_cache: dict, hit_lookup: _HitLookup,
) -> tuple[int, bool]:
    """淘汰一本记忆里过期的自动条目，返回 (删掉的条数, 是否整本删空)。调用方持有记忆写锁。"""
    data = _load_book(book_path)
    entries = data["entries"]
    kept = [entry for entry in entries if not _memory_entry_expired(entry, namespace, cutoff, hit_cache, hit_lookup)]
    deleted = len(entries) - len(kept)
    if not deleted:
        return 0, False
    if kept:
        data["entries"] = kept
        _rewrite_book(book_path, data)
        return deleted, False
    _unlink(book_path)
    return deleted, True


def _sweep_namespace_locked(
    namespace: str,
    directory: Path,
    cutoff: float,
    hit_cache: dict,
    hit_lookup: _HitLookup,
    sweep: _MemorySweep,
) -> None:
    """一个 namespace：淘汰过期的自动条目，删空了连目录一起收掉。调用方持有记忆写锁。"""
    books = sorted(path for path in directory.glob("*.yaml") if path.is_file())
    if namespace.startswith(SUBJECT_PREFIX):
        # 跨会话人物档案不参与年龄淘汰：画像的价值就在长期积累，
        # 「三个月前提过对象生日」正是它存在的理由。只能手工删。
        sweep.skipped_archives += len(books)
    else:
        for book_path in books:
            try:
                deleted, emptied = _expire_book_locked(book_path, namespace, cutoff, hit_cache, hit_lookup)
            except MemoryBookUnreadable as exc:
                logging.info("[memory] skipped unreadable book %s: %s", book_path, exc.reason)
                continue
            except OSError as exc:
                logging.info("[memory] failed to update book %s: %s", book_path, exc)
                continue
            sweep.scanned_books += 1
            sweep.deleted_entries += deleted
            sweep.removed_empty_books += int(emptied)
    # 判空和 rmdir 都得在锁里：engine 可能正往这个目录里写第一本。
    if namespace and not any(directory.iterdir()):
        _rmdir(directory)


def purge_expired_memory_entries():
    """Remove stale non-constant memory entries from the memory books.

    Memory books are YAML files shaped like {book, entries}.  Namespaced books
    live under data/memory/<namespace>/*.yaml, while older/global books live
    directly under data/memory/*.yaml.  We scan both layouts.
    """
    if MEMORY_ENTRY_MAX_AGE <= 0:
        logging.info("[memory] entry cleanup disabled")
        return

    workspace_root = DATA_DIR.parent
    mem_root = memory_root(workspace_root)
    if not mem_root.is_dir():
        return

    cutoff = time.time() - MEMORY_ENTRY_MAX_AGE
    sweep = _MemorySweep()
    hit_cache, hit_lookup = _load_hit_stamps(mem_root)

    namespaces = [("", mem_root), *((directory.name, directory) for directory in namespace_dirs(workspace_root))]
    for namespace, directory in namespaces:
        try:
            # 和 engine 的记忆工具同一把锁：读完到写回之间插进来的那次保存，会被这次整本写回盖掉。
            with _memory_write_lock(workspace_root):
                _sweep_namespace_locked(namespace, directory, cutoff, hit_cache, hit_lookup, sweep)
        except (OSError, TimeoutError) as exc:
            logging.info("[memory] namespace %s skipped: %s", namespace or "(root)", exc)

    removed_hit_keys = 0
    try:
        from engine.memory_hit_cache import prune_orphan_namespace_hits

        surviving = {p.name for p in mem_root.iterdir() if p.is_dir()}
        removed_hit_keys = prune_orphan_namespace_hits(DATA_DIR.parent, surviving)
    except Exception as exc:
        logging.info("[memory] orphan hit-key sweep skipped: %s", exc)

    logging.info(
        "[memory] scanned_books=%d deleted_entries=%d removed_empty_books=%d "
        "skipped_archives=%d removed_hit_keys=%d max_age_hours=%.1f",
        sweep.scanned_books,
        sweep.deleted_entries,
        sweep.removed_empty_books,
        sweep.skipped_archives,
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

    入口分支同理：merge 后由 supervisor 删，但进程崩在半路就会留下一个 fork 自
    父会话的全量副本，没有别的清理者。
    """
    conv_dir = DATA_DIR / "conversations"
    if not conv_dir.exists():
        return
    cutoff = time.time() - CHILD_SESSION_MAX_AGE
    deleted = 0
    for p in [*conv_dir.glob("child_*.jsonl"), *conv_dir.glob("branch_*.jsonl")]:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff and _unlink_idle_conversation(p, cutoff):
                deleted += 1
        except Exception:
            pass
    logging.info("[child_sessions] deleted %d expired files", deleted)


# ── 内部任务会话 JSONL cleanup ───────────────
def purge_expired_task_conversations():
    """清理内部任务会话的对话记录。

    dream 拿 memory namespace 当 conversation_key 建整理任务（save_memory 靠它推
    目录），supervisor 于是给它开一条 session，每跑一轮就往同一个 jsonl 追一段。
    文件名是 session_id，看不出类型，只能回 sessions.json 按会话键形态反查。
    """
    if TASK_CONVERSATION_MAX_AGE <= 0:
        logging.info("[task_conversations] cleanup disabled")
        return
    conv_dir = DATA_DIR / "conversations"
    sessions_path = DATA_DIR / "sessions.json"
    if not conv_dir.exists() or not sessions_path.exists():
        return
    try:
        raw = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.info("[task_conversations] sessions.json unreadable: %s", exc)
        return

    rows = raw.values() if isinstance(raw, dict) else raw
    session_ids = {
        str(row.get("session_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and is_namespace_conversation_key(row.get("conversation_key"))
    }
    cutoff = time.time() - TASK_CONVERSATION_MAX_AGE
    deleted = 0
    for session_id in sorted(session_ids):
        if not session_id:
            continue
        path = conv_dir / f"{session_id}.jsonl"
        try:
            if path.is_file() and path.stat().st_mtime < cutoff and _unlink_idle_conversation(path, cutoff):
                deleted += 1
        except Exception:
            pass
    logging.info(
        "[task_conversations] scanned=%d deleted=%d max_age_hours=%.1f",
        len(session_ids), deleted, TASK_CONVERSATION_MAX_AGE / 3600.0,
    )


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


def purge_expired_context_archives():
    from engine.context_archive import ContextArchiveStore

    deleted = ContextArchiveStore(DATA_DIR.parent).purge(dry_run=DRY_RUN)
    logging.info("[context_archives] expired=%d dry_run=%s", deleted, DRY_RUN)


def purge_expired_tool_outputs():
    from engine.tool_outputs import ToolOutputStore

    store = ToolOutputStore(DATA_DIR.parent)
    expired = store.purge(dry_run=DRY_RUN)
    logging.info("[tool_outputs] expired=%d retention_days=%d dry_run=%s", expired, store.retention_days, DRY_RUN)


def purge_expired_edit_journals():
    from toolbox.edit_journal import EditJournal

    journal = EditJournal(DATA_DIR.parent)
    expired = journal.purge(dry_run=DRY_RUN)
    logging.info("[edit_journal] expired=%d retention_days=%d dry_run=%s", expired, journal.retention_days, DRY_RUN)


# ── logs cleanup ─────────────────────
def _newest_spawn_logs(log_dir: Path) -> set[str]:
    """每个子进程系列里最新的那份：进程可能还开着它写，删了输出就进了已删的文件。"""
    newest: dict[str, str] = {}
    for path in log_dir.iterdir():
        match = _SPAWN_LOG.match(path.name)
        if match is None or not path.is_file():
            continue
        series = match.group("series")
        # 时间戳定长，同一系列按名字比就是按时间比。
        if path.name > newest.get(series, ""):
            newest[series] = path.name
    return set(newest.values())


def purge_old_logs():
    """清理 data/logs/ 下过期的进程日志。

    engine/supervisor 每次重启/重连都会新建日志文件，其中大量为 0 字节空文件，
    长期不清会堆积成千上万个小文件。保留 cleanup.log、supervisor.log 和每个子进程最新的那份。
    """
    if LOG_MAX_AGE <= 0:
        logging.info("[logs] cleanup disabled")
        return
    log_dir = DATA_DIR / "logs"
    if not log_dir.exists():
        return
    cutoff = time.time() - LOG_MAX_AGE
    in_use = _newest_spawn_logs(log_dir)
    deleted = 0
    for p in log_dir.iterdir():
        try:
            if not p.is_file() or p.name in LOG_KEEP_NAMES or p.name in in_use:
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
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m engine.data_cleanup")
    parser.add_argument("--dry-run", action="store_true", help="只记会删什么，不动文件")
    parser.add_argument(
        "--scope",
        choices=sorted(CLEANUP_SCOPES),
        help=f"workspace 只清本工作区，all 连宿主级的一起清；不给时看 {CLEANUP_SCOPE_ENV}，再没有就是 all",
    )
    return parser.parse_args(argv)


def _resolve_scope(flag: str | None) -> str:
    if flag:
        return flag
    configured = os.getenv(CLEANUP_SCOPE_ENV, "").strip()
    if not configured or configured in CLEANUP_SCOPES:
        return configured or "all"
    # 写错了按 all 跑，和没设时一样：宿主级任务多跑一遍无害。
    logging.warning(
        "[scope] %s=%r is not one of %s, running all",
        CLEANUP_SCOPE_ENV, configured, "/".join(sorted(CLEANUP_SCOPES)),
    )
    return "all"


def main(argv: list[str] | None = None):
    global DRY_RUN

    args = _parse_args(argv)
    DRY_RUN = args.dry_run
    _log_init()
    scope = _resolve_scope(args.scope)
    suffix = (" (dry-run)" if DRY_RUN else "") + ("" if scope == "all" else f" (scope: {scope})")
    logging.info("=== data cleanup start%s ===", suffix)

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        # Phase D：node_contexts 已被 child session 替代，启用定期清理
        (purge_dir, dict(directory=DATA_DIR / "node_contexts",
                         max_age=NODE_CONTEXTS_MAX_AGE, label="node_contexts",
                         recursive=True)),
        (purge_expired_child_sessions, {}),
        (purge_expired_task_conversations, {}),
        (purge_temp_globs, {}),
        (purge_dir, dict(directory=DATA_DIR / "temp",
                         max_age=TEMP_MAX_AGE, label="temp", recursive=True)),
        (purge_dir, dict(directory=DATA_DIR / "temp_summary",
                         max_age=TEMP_MAX_AGE, label="temp_summary")),
        (purge_dir, dict(directory=DATA_DIR / "artifacts",
                         max_age=ARTIFACT_MAX_AGE, label="artifacts")),
        (purge_dir, dict(directory=DATA_DIR / "attachments",
                         max_age=ATTACH_MAX_AGE, label="attachments", recursive=True)),
        # 平台自己的缓存由各平台声明的维护钩子清。
        (run_platform_maintenance, dict(scopes=CLEANUP_SCOPES[scope])),
        (purge_expired_memory_entries, {}),
        # 新增：之前未覆盖的目录
        (purge_expired_transcripts, {}),
        (purge_expired_context_archives, {}),
        (purge_expired_tool_outputs, {}),
        (purge_expired_edit_journals, {}),
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
