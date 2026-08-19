"""记忆命中时间戳的侧车存储：data/memory/.hit_cache.json。

热路径只写进程内 dict，落盘按 namespace/entry_id 为 key 合并，跨进程靠文件锁。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from engine.eventlog_rotation import eventlog_file_lock

logger = logging.getLogger(__name__)

# 只攒本进程这一轮新增的命中，不再在内存里镜像整份磁盘内容：两个 engine worker
# 各持一份全量再整体覆盖写，等于互相抹掉对方的命中记录。
_hit_pending: dict[str, str] = {}        # {namespace/entry_id: iso_timestamp}
_hit_cache_lock = threading.Lock()
_hit_cache_last_flush: float = 0.0
_hit_atexit_registered: bool = False
_HIT_CACHE_FLUSH_INTERVAL = 600          # 10 minutes
_HIT_KEY_SEP = "/"


def hit_cache_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "memory" / ".hit_cache.json"


def hit_cache_key(namespace: str, entry_id: str) -> str:
    """Hit-cache key scoped to one memory namespace.

    裸 entry id 会让两个会话里同名的 `user_zhangsan` 共用一个时间戳槽 —— 提取器的
    提示词本来就要求 id 锚在稳定别名上，撞车是常态而非意外。
    """
    ns = str(namespace or "").strip()
    eid = str(entry_id or "").strip()
    if not eid:
        return ""
    return f"{ns}{_HIT_KEY_SEP}{eid}" if ns else eid


def hit_timestamp(cache: dict[str, Any], namespace: str, entry_id: str) -> str:
    """Look up one entry's last hit, falling back to the pre-namespace flat key."""
    scoped = cache.get(hit_cache_key(namespace, entry_id))
    if scoped:
        return str(scoped)
    return str(cache.get(str(entry_id or "")) or "")


def read_hit_cache(workspace_root: Path) -> dict[str, str]:
    """Read the hit sidecar, treating anything but a JSON object as empty.

    只挡解析异常是不够的：`[]` 能解析成功，随后每一次 `.get` 都抛 TypeError，
    而这条异常会一路把 prompt 构建打掉（skill 与 memory 四个注入键全不设置）。
    """
    path = hit_cache_path(workspace_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as read_error:
        logger.warning("hit cache unreadable, treating as empty: %s", read_error)
        return {}
    if not isinstance(raw, dict):
        logger.warning("hit cache is %s, not an object; treating as empty", type(raw).__name__)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def flush_hit_cache(workspace_root: Path | None = None) -> None:
    """Merge this process's pending hits into the sidecar and publish atomically."""
    global _hit_pending, _hit_cache_last_flush
    if workspace_root is None:
        return
    with _hit_cache_lock:
        if not _hit_pending:
            return
        pending = _hit_pending
        _hit_pending = {}
        _hit_cache_last_flush = time.monotonic()

    # 文件锁只能在内存锁之外拿：record_hits 每次 LLM 调用都要碰内存锁，把一把
    # 带超时的跨进程锁套在里面就是把热路径钉死。
    path = hit_cache_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with eventlog_file_lock(path):
            merged = read_hit_cache(workspace_root)
            for key, stamp in pending.items():
                # 时间戳一律是 datetime.now(timezone.utc).isoformat()，格式固定，
                # 所以字典序就是时间序，不必为取 max 解析上千条。
                if stamp > str(merged.get(key) or ""):
                    merged[key] = stamp
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(merged), encoding="utf-8")
            os.replace(tmp, path)
    except Exception as flush_error:
        logger.warning("hit cache flush failed, keeping hits for the next attempt: %s", flush_error)
        with _hit_cache_lock:
            for key, stamp in pending.items():
                if stamp > str(_hit_pending.get(key) or ""):
                    _hit_pending[key] = stamp


def record_hits(workspace_root: Path, entries: list[dict[str, Any]]) -> None:
    """Record keyword hits in memory. No YAML, no file IO on this path."""
    global _hit_atexit_registered, _hit_cache_last_flush
    try:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        flush_now = False
        with _hit_cache_lock:
            if _hit_cache_last_flush == 0.0:
                # time.monotonic() 是个大数，不初始化的话「距上次 flush 超 600 秒」
                # 恒成立，每次 LLM 调用都会起一个线程落盘。
                _hit_cache_last_flush = time.monotonic()
            for entry in entries:
                key = hit_cache_key(str(entry.get("namespace") or ""), entry.get("id", ""))
                if key:
                    _hit_pending[key] = now_iso
            if not _hit_atexit_registered:
                # 之前只 import 了 atexit 从未 register，文件头注释承诺的
                # 「or at shutdown」并不存在：worker 活不到 10 分钟就一条也不落盘。
                atexit.register(flush_hit_cache, workspace_root)
                _hit_atexit_registered = True
            if _hit_pending and (time.monotonic() - _hit_cache_last_flush) > _HIT_CACHE_FLUSH_INTERVAL:
                flush_now = True
        if flush_now:
            threading.Thread(target=flush_hit_cache, args=(workspace_root,), daemon=True).start()
    except Exception as record_error:
        # 命中统计是旁路能力，绝不能让它把整条 prompt 构建打掉。
        logger.warning("recording memory hits failed: %s", record_error)


def prune_namespace_hits(workspace_root: Path, namespace: str) -> int:
    """Drop one namespace's hit stamps, leaving every other namespace intact.

    保留不带 namespace 的历史扁平 key：同一个 entry id 会在多个会话里重复出现，
    删掉它等于让别的会话的这条记忆变成「从没命中过」，下一次扫除就会删掉它。
    """
    ns = str(namespace or "").strip()
    if not ns:
        return 0
    prefix = f"{ns}{_HIT_KEY_SEP}"
    return _drop_hit_keys(workspace_root, lambda key: key.startswith(prefix))


def prune_entry_hits(workspace_root: Path, namespace: str, entry_id: str) -> int:
    """Drop one entry's hit stamp.

    留着的话，之后在同一个会话里新建同 id 的记忆会继承旧命中时间，一进来就是「老热记忆」。
    """
    entry = str(entry_id or "").strip()
    if not entry:
        return 0
    doomed = hit_cache_key(namespace, entry)
    return _drop_hit_keys(workspace_root, lambda key: key == doomed)


def _drop_hit_keys(workspace_root: Path, matches: Callable[[str], bool]) -> int:
    """从待落盘缓冲和盘上侧车里摘掉命中的 key，返回删掉的条数。"""
    # 先在内存锁内摘掉待落盘的项，文件锁在锁外拿（顺序同 flush_hit_cache）。
    with _hit_cache_lock:
        for key in [k for k in _hit_pending if matches(k)]:
            del _hit_pending[key]
    path = hit_cache_path(workspace_root)
    if not path.exists():
        return 0
    try:
        with eventlog_file_lock(path):
            cache = read_hit_cache(workspace_root)
            doomed = [k for k in cache if matches(k)]
            if not doomed:
                return 0
            for key in doomed:
                del cache[key]
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, path)
            return len(doomed)
    except Exception as prune_error:
        logger.warning("pruning hit stamps failed: %s", prune_error)
        return 0


def prune_orphan_namespace_hits(workspace_root: Path, known_namespaces: Iterable[str]) -> int:
    """Drop scoped hit stamps whose namespace directory is gone."""
    known = {str(ns) for ns in known_namespaces}
    # 空集合不能被解读成「所有 namespace 都没了」：调用方拿不到目录列表时要按兵不动。
    if not known:
        return 0
    path = hit_cache_path(workspace_root)
    if not path.exists():
        return 0
    try:
        with eventlog_file_lock(path):
            cache = read_hit_cache(workspace_root)
            doomed = []
            for key in cache:
                ns_part, sep, _ = key.partition(_HIT_KEY_SEP)
                if not sep or not ns_part:
                    continue  # 扁平 key（无前缀段）一律保留
                if ns_part not in known:
                    doomed.append(key)
            if not doomed:
                return 0
            for key in doomed:
                del cache[key]
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, path)
            return len(doomed)
    except Exception as prune_error:
        logger.warning("pruning orphan namespace hits failed: %s", prune_error)
        return 0
