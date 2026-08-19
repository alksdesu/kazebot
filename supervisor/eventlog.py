from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.eventlog_rotation import eventlog_file_lock, rotate_event_log_locked


logger = logging.getLogger(__name__)

SYSTEM_SESSION_ID = "__system__"
_MAX_MEMORY_EVENTS = 5000
_MAX_MEMORY_EVENT_BYTES = 64 * 1024 * 1024
_TAIL_READ_BLOCK_BYTES = 1024 * 1024

# [2026-07-16] Why: events.jsonl 只在每小时的 data_cleanup timer 里轮转，
# 一旦高频写入（如压缩死循环），单小时内可膨胀到数 GB。How: append() 写盘后按
# 计数抽检文件大小，超过在线硬上限则立即就地轮转（.N->.N+1，活动文件->.1），
# 与 data_cleanup.rotate_events 策略一致。Purpose: 给日志膨胀一个不依赖定时任务
# 的硬上限。<=0 表示禁用在线护栏（仅靠定时轮转）。
_ONLINE_ROTATE_MAX_BYTES = int(os.getenv("CLONOTH_EVENTS_MAX_BYTES", str(50 * 1024 * 1024)))
_ONLINE_ROTATE_BACKUPS = int(os.getenv("CLONOTH_EVENTS_BACKUPS", "3"))
_ONLINE_ROTATE_CHECK_EVERY = 200  # 每写 N 条检查一次文件大小



def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventAppendResult:
    event: dict[str, Any]


class EventLog:
    """Append-only JSONL event log.

    - 一行一个 JSON（UTF-8）
    - 维护 seq 单调递增（用于轮询 after_seq）
    - 进程内缓存 events，用于快速查询
    """

    def __init__(self, path: Path, run_id: str):
        self._path = path
        self._run_id = run_id
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq: int = 0
        # [2026-07-16] 在线轮转护栏：距上次检查写入的条数计数器。
        self._append_since_size_check: int = 0
        # [WS events 2026-05-17] Why: WebSocket clients need live updates while
        # EventLog remains the durable source of truth. How: keep per-session
        # asyncio.Queue subscribers beside the append-only memory buffer. Purpose:
        # append() can fan out each new event without changing persistence or the
        # existing polling API.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # [WS events 2026-05-19] Why: the web client needs an all-session stream
        # in addition to the existing per-session stream. How: keep a separate
        # subscriber list that append() fans out to after the session-specific
        # queues. Purpose: add global observation without changing session
        # isolation for subscribe()/unsubscribe().
        self._global_subscribers: list[asyncio.Queue] = []

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    @property
    def path(self) -> Path:
        return self._path

    def _load_existing(self) -> None:
        """Restore global seq and the recent cache across active and backups."""
        max_seq = 0
        recent_events: deque[dict[str, Any]] = deque(maxlen=_MAX_MEMORY_EVENTS)
        # The active file may have just been renamed to .1 and not yet recreated.
        # Scan every retained file oldest-first so seq never rolls back and the
        # bounded memory cache preserves true recovery order across rotations.
        # Initialization must observe one stable active+backup snapshot.  Without
        # the shared file lock, a concurrent cleanup could stage files between
        # existence checks and make seq recovery roll backwards.
        with eventlog_file_lock(self._path):
            for event in self._iter_persisted_events_locked():
                recent_events.append(event)
                seq = event.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
        self._seq = max_seq
        self._events = list(recent_events)

    def _read_recent_lines_locked(self) -> list[str]:
        """Return raw JSONL tail lines while the shared file lock is held."""
        chunks: list[bytes] = []
        total_read = 0
        newline_count = 0
        with self._path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            while pos > 0 and newline_count <= _MAX_MEMORY_EVENTS and total_read < _MAX_MEMORY_EVENT_BYTES:
                read_size = min(_TAIL_READ_BLOCK_BYTES, pos, _MAX_MEMORY_EVENT_BYTES - total_read)
                if read_size <= 0:
                    break
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                chunks.append(chunk)
                total_read += len(chunk)
                newline_count += chunk.count(b"\n")

            # Why: if the bounded tail starts in the middle of a line, parsing it
            # would create a spurious JSON error and could displace a real event.
            # How: check the byte just before the buffer; if it is not a newline,
            # drop the first split line. Purpose: keep only complete JSONL rows.
            starts_with_complete_line = pos == 0
            if pos > 0:
                f.seek(pos - 1)
                starts_with_complete_line = f.read(1) == b"\n"

        data = b"".join(reversed(chunks))
        raw_lines = data.splitlines()
        if not starts_with_complete_line and raw_lines:
            raw_lines = raw_lines[1:]
        raw_lines = raw_lines[-_MAX_MEMORY_EVENTS:]
        return [line.decode("utf-8", errors="replace") for line in raw_lines]

    def _read_recent_lines(self) -> list[str]:
        """Return raw JSONL tail lines from one stable active-file snapshot."""
        with eventlog_file_lock(self._path):
            return self._read_recent_lines_locked()

    def _maybe_rotate_locked(self) -> None:
        """Rotate an oversized active file while holding ``self._lock``.

        The cross-process lock is shared with data_cleanup and append().  Size and
        backup state are re-checked inside that lock, so an earlier observation of
        the active inode cannot trigger a second rotation after another process
        already moved it.
        """
        if _ONLINE_ROTATE_MAX_BYTES <= 0:
            return
        try:
            with eventlog_file_lock(self._path):
                rotate_event_log_locked(
                    self._path,
                    max_bytes=_ONLINE_ROTATE_MAX_BYTES,
                    backups=_ONLINE_ROTATE_BACKUPS,
                )
        except (OSError, TimeoutError):
            # Rotation failure must not corrupt the append path; a later online
            # check or cleanup timer retries the same lock-protected operation.
            logger.warning("event log online rotation failed", exc_info=True)

    def append(
        self,
        *,
        session_id: str,
        component: str,
        type_: str,
        payload: dict[str, Any] | None = None,
        transient: bool = False,
    ) -> dict[str, Any]:
        payload = payload or {}

        with self._lock:
            self._seq += 1
            seq = self._seq

            evt = {
                "schema_version": 1,
                "seq": seq,
                "event_id": str(uuid.uuid4()),
                "ts": _now().isoformat(),
                "run_id": self._run_id,
                "session_id": session_id,
                "component": component,
                "type": type_,
                "payload": payload,
            }

            if not transient:
                line = json.dumps(evt, ensure_ascii=False)
                # append and both rotation entry points participate in the same
                # cross-process protocol.  Cleanup can never rename between open
                # and write, including on Windows where an open file blocks rename.
                with eventlog_file_lock(self._path):
                    with self._path.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    self._append_since_size_check += 1
                    if self._append_since_size_check >= _ONLINE_ROTATE_CHECK_EVERY:
                        self._append_since_size_check = 0
                        try:
                            rotate_event_log_locked(
                                self._path,
                                max_bytes=_ONLINE_ROTATE_MAX_BYTES,
                                backups=_ONLINE_ROTATE_BACKUPS,
                            )
                        except OSError:
                            logger.warning("event log online rotation failed", exc_info=True)

            self._events.append(evt)
            # Trim with hysteresis to prevent unbounded memory growth
            if len(self._events) > _MAX_MEMORY_EVENTS + 500:
                self._events = self._events[-_MAX_MEMORY_EVENTS:]
            # [WS events 2026-05-17] Why: broadcasting while holding the threading
            # lock would make subscriber callbacks part of the critical section.
            # How: copy the current session queues and fan out after leaving the
            # lock. Purpose: keep append() fast and avoid awaiting under this lock.
            subscribers = list(self._subscribers.get(session_id, []))
            # [WS events 2026-05-19] Why: /v1/ws must see every EventLog row.
            # How: snapshot global subscribers under the same lock used for
            # per-session subscribers, then fan out outside the critical section.
            # Purpose: avoid races with subscribe_global()/unsubscribe_global()
            # while preserving the existing append() lock behavior.
            global_subscribers = list(self._global_subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                # The current queues are unbounded, but this keeps future bounded
                # queues from breaking event persistence if a client falls behind.
                continue
        for queue in global_subscribers:
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                # [WS events 2026-05-19] Why: global observers are optional
                # consumers and must not block persistence. How: mirror the
                # per-session overflow behavior. Purpose: a slow global client
                # cannot affect EventLog writes or session-specific streams.
                continue
        return evt

    def _iter_persisted_events_locked(self):
        """Yield a stable retained snapshot; caller must hold eventlog_file_lock."""
        paths = [
            self._path.parent / f"{self._path.name}.{index}"
            for index in range(_ONLINE_ROTATE_BACKUPS, 0, -1)
        ]
        paths.append(self._path)
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if isinstance(event, dict):
                            yield event
            except OSError:
                continue

    def iter_persisted_events(self):
        """Yield retained durable events oldest-first from one locked snapshot.

        The lock remains held for the generator's entire consumption.  Internal
        callers that already own it use ``_iter_persisted_events_locked`` to avoid
        attempting to acquire the non-reentrant cross-process lock twice.
        """
        with eventlog_file_lock(self._path):
            yield from self._iter_persisted_events_locked()

    def find_delivery_event(
        self, delivery_id: str, *, event_type: str,
    ) -> dict[str, Any] | None:
        """Find a retained durable event by type and idempotency key."""
        key = str(delivery_id or "").strip()
        if not key:
            return None
        for event in reversed(self._events):
            payload = event.get("payload")
            if (
                event.get("type") == event_type
                and isinstance(payload, dict)
                and str(payload.get("delivery_id") or "").strip() == key
            ):
                return event
        found = None
        for event in self.iter_persisted_events():
            payload = event.get("payload")
            if (
                event.get("type") == event_type
                and isinstance(payload, dict)
                and str(payload.get("delivery_id") or "").strip() == key
            ):
                found = event
        return found

    def find_outbound_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        """Find a retained outbound event by its durable idempotency key."""
        return self.find_delivery_event(delivery_id, event_type="outbound_message")

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Subscribe to new events for one session.

        [WS events 2026-05-17] Why: the WebSocket endpoint needs a live channel
        but EventLog must remain the only event source. How: callers receive an
        asyncio.Queue that append() fills with matching session events. Purpose:
        consumers can combine catch-up reads with live delivery.
        """
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        """Remove a queue previously returned by subscribe()."""
        # [WS events 2026-05-17] Why: disconnected WebSocket clients must not
        # keep receiving events or retain memory. How: remove the exact queue and
        # drop the session list when it becomes empty. Purpose: make cleanup
        # deterministic even when multiple clients watch the same session.
        with self._lock:
            queues = self._subscribers.get(session_id)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                return
            if not queues:
                self._subscribers.pop(session_id, None)

    def subscribe_global(self) -> asyncio.Queue:
        """Subscribe to all new events across all sessions."""
        queue: asyncio.Queue = asyncio.Queue()
        # [WS events 2026-05-19] Why: global subscribers must not be mixed into
        # the per-session mapping. How: append their queues to a dedicated list.
        # Purpose: unsubscribe_global() can clean up without knowing a session id.
        with self._lock:
            self._global_subscribers.append(queue)
        return queue

    def unsubscribe_global(self, queue: asyncio.Queue) -> None:
        """Remove a queue previously returned by subscribe_global()."""
        # [WS events 2026-05-19] Why: disconnected global WebSocket clients should
        # not retain queues. How: remove the exact queue if it is still present.
        # Purpose: keep cleanup deterministic and independent of session streams.
        with self._lock:
            try:
                self._global_subscribers.remove(queue)
            except ValueError:
                pass

    def list_events(self, *, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        # 简单线性过滤（MVP）。后续可按 session 建索引或 snapshot。
        return [
            e
            for e in self._events
            if e.get("session_id") == session_id and int(e.get("seq", 0)) > after_seq
        ]

    def list_all_events(self, *, after_seq: int = 0) -> list[dict[str, Any]]:
        """返回所有 session 中 seq > after_seq 的事件。"""
        return [
            e for e in self._events
            if int(e.get("seq", 0)) > after_seq
        ]

    def last_boot_run_id(self) -> str | None:
        # 从尾部倒序找到最近一次 boot 事件
        for e in reversed(self._events):
            if e.get("type") == "boot":
                return e.get("run_id")
        return None
