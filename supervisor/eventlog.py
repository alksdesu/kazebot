from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from engine.eventlog_rotation import eventlog_file_lock, rotate_event_log_locked


logger = logging.getLogger(__name__)

SYSTEM_SESSION_ID = "__system__"
_MAX_MEMORY_EVENTS = 5000
_MAX_MEMORY_EVENT_BYTES = 64 * 1024 * 1024
_TAIL_READ_BLOCK_BYTES = 1024 * 1024
# 按 seq 跳读一份日志：二分到相差不到这么多字节就改为顺着读
_SEEK_WINDOW_BYTES = 64 * 1024

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


@dataclass(frozen=True)
class ReceiptKey:
    """一种回执：哪类事件的 payload 里哪个字段是它的幂等键。EventLog 只给 RECEIPT_KEYS 里这几种建表。"""

    event_type: str
    path: tuple[str, ...]

    def value_in(self, payload: dict[str, Any]) -> str:
        node: Any = payload
        for part in self.path:
            if not isinstance(node, dict):
                return ""
            node = node.get(part)
        return str(node or "").strip()


INBOUND_DELIVERY = ReceiptKey("inbound_message", ("delivery_id",))
OUTBOUND_DELIVERY = ReceiptKey("outbound_message", ("delivery_id",))
REPLY_DELIVERY = ReceiptKey("intermediate_reply", ("delivery_id",))
POST_PHASE_TASK = ReceiptKey("task_created", ("input", "_post_phase_id"))
MEMORY_EXTRACT_TASK = ReceiptKey("task_created", ("input", "_memory_extract_intent_id"))
# 任务快照也都带着任务自己的 delivery_id，不在这里点名的一概不进表，否则表要大好几倍
RECEIPT_KEYS: tuple[ReceiptKey, ...] = (
    INBOUND_DELIVERY, OUTBOUND_DELIVERY, REPLY_DELIVERY, POST_PHASE_TASK, MEMORY_EXTRACT_TASK,
)
_RECEIPTS_OF_TYPE: dict[str, tuple[ReceiptKey, ...]] = {
    event_type: tuple(key for key in RECEIPT_KEYS if key.event_type == event_type)
    for event_type in dict.fromkeys(key.event_type for key in RECEIPT_KEYS)
}
_DELIVERY_OF_TYPE: dict[str, ReceiptKey] = {
    key.event_type: key for key in (INBOUND_DELIVERY, OUTBOUND_DELIVERY, REPLY_DELIVERY)
}


@dataclass(frozen=True, slots=True)
class _Receipt:
    seq: int
    event_id: str
    session_id: str
    event_type: str
    ts: str

    def envelope(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "type": self.event_type,
            "ts": self.ts,
        }


class DeliveryIdTaken(Exception):
    """要写的投递 id 已经落在别的会话的一条事件上：不写，也不把那一条交出去。"""

    def __init__(self, receipt_key: ReceiptKey, delivery_id: str) -> None:
        super().__init__(f"{receipt_key.event_type} 的投递 id 已经被别的会话用过")
        self.receipt_key = receipt_key
        self.delivery_id = delivery_id


def _decode_event_line(raw: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _event_seq(raw: bytes) -> int | None:
    event = _decode_event_line(raw)
    seq = event.get("seq") if event is not None else None
    return seq if isinstance(seq, int) else None


def _last_seq(stream: BinaryIO, size: int) -> int | None:
    """一份日志里最后一条读得懂的事件的 seq：从末尾往前读，最后一行比读的这段还长就加倍再读。"""
    block = _SEEK_WINDOW_BYTES
    while True:
        start = max(0, size - block)
        stream.seek(start)
        lines = stream.read(size - start).split(b"\n")
        if start > 0:
            lines = lines[1:]
        for raw in reversed(lines):
            seq = _event_seq(raw)
            if seq is not None:
                return seq
        if start == 0:
            return None
        block *= 2


def _line_start_at_or_after(stream: BinaryIO, position: int) -> int:
    if position <= 0:
        return 0
    stream.seek(position - 1)
    stream.readline()
    return stream.tell()


def _offset_after_seq(stream: BinaryIO, size: int, after_seq: int) -> int:
    """从哪个行首顺着读，能读到这份日志里 seq 大于 after_seq 的全部事件。

    单写者写下的一份日志里 seq 递增，按它二分到相差不到一个窗口；探到读不懂的行就停在已知的位置。
    """
    low, high = 0, size
    while high - low > _SEEK_WINDOW_BYTES:
        middle = (low + high) // 2
        start = _line_start_at_or_after(stream, middle)
        if start >= high:
            high = middle
            continue
        stream.seek(start)
        seq = _event_seq(stream.readline())
        if seq is None:
            break
        if seq <= after_seq:
            low = start
        else:
            high = start
    return low


class _EventQueue(asyncio.Queue):
    def __init__(self, publisher_lock: Any):
        super().__init__()
        self._publisher_lock = publisher_lock
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

    async def get(self) -> Any:
        with self._publisher_lock:
            loop = asyncio.get_running_loop()
            if self._event_loop is None:
                self._event_loop = loop
            elif self._event_loop is not loop:
                raise RuntimeError("event subscriber belongs to another event loop")
        return await super().get()

    def _publish_locked(self, event: dict[str, Any]) -> None:
        if self._event_loop is None:
            self.put_nowait(event)
        elif not self._event_loop.is_closed():
            # Always schedule, including local publishers, to preserve sequence order.
            self._event_loop.call_soon_threadsafe(self.put_nowait, event)


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
        self._durable_seq: int = 0
        # [2026-07-16] 在线轮转护栏：距上次检查写入的条数计数器。
        self._append_since_size_check: int = 0
        # [WS events 2026-05-17] Why: WebSocket clients need live updates while
        # EventLog remains the durable source of truth. How: keep per-session
        # asyncio.Queue subscribers beside the append-only memory buffer. Purpose:
        # append() can fan out each new event without changing persistence or the
        # existing polling API.
        self._subscribers: dict[str, list[_EventQueue]] = {}
        # [WS events 2026-05-19] Why: the web client needs an all-session stream
        # in addition to the existing per-session stream. How: keep a separate
        # subscriber list that append() fans out to after the session-specific
        # queues. Purpose: add global observation without changing session
        # isolation for subscribe()/unsubscribe().
        self._global_subscribers: list[_EventQueue] = []
        # 回执表：RECEIPT_KEYS 里那几种事件按幂等键记下，按 seq 从旧到新排
        self._receipts: OrderedDict[tuple[ReceiptKey, str], _Receipt] = OrderedDict()

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

    @property
    def durable_seq(self) -> int:
        with self._lock:
            return self._durable_seq

    def oldest_retained_seq(self) -> int:
        events = self.iter_persisted_events()
        try:
            first = next(events, None)
            return int(first.get("seq") or 0) if first else 0
        finally:
            events.close()

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
        with self._lock, eventlog_file_lock(self._path):
            for event in self._iter_persisted_events_locked():
                recent_events.append(event)
                seq = event.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
                self._remember_receipts_locked(event)
        self._seq = max_seq
        self._durable_seq = max_seq
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
                self._forget_rotated_receipts_locked()
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
        with self._lock:
            return self._append_locked(
                session_id=session_id, component=component, type_=type_, payload=payload, transient=transient,
            )

    def append_delivery(
        self,
        receipt_key: ReceiptKey,
        *,
        session_id: str,
        component: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """带投递 id 的消息事件只写一次，返回 (事件, 这次是不是新写的)。

        这个会话里已有这个 id 就不写，回那一条的基本字段（同 find_receipt）；落在别的会话里的抛 DeliveryIdTaken。
        查和写在同一次持锁里：同一个 id 的两次请求同时到也只落一条。
        """
        if receipt_key not in _DELIVERY_OF_TYPE.values():
            raise ValueError(f"{receipt_key.event_type} 不按 delivery_id 查重")
        key = receipt_key.value_in(payload)
        with self._lock:
            known = self._receipts.get((receipt_key, key)) if key else None
            if known is not None:
                if known.session_id != session_id:
                    raise DeliveryIdTaken(receipt_key, key)
                return known.envelope(), False
            event = self._append_locked(
                session_id=session_id, component=component, type_=receipt_key.event_type, payload=payload,
            )
            return event, True

    def _append_locked(
        self,
        *,
        session_id: str,
        component: str,
        type_: str,
        payload: dict[str, Any] | None = None,
        transient: bool = False,
    ) -> dict[str, Any]:
        payload = payload or {}
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
                self._durable_seq = seq
                # 落了盘就记下：之后轮转出错抛出去，这一条也已经在盘上，重试时得认得
                self._remember_receipts_locked(evt)
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
                    # data_cleanup 在另一个进程里也会轮转，每次检查都对一遍最旧那份
                    self._forget_rotated_receipts_locked()

        self._events.append(evt)
        # Trim with hysteresis to prevent unbounded memory growth
        if len(self._events) > _MAX_MEMORY_EVENTS + 500:
            self._events = self._events[-_MAX_MEMORY_EVENTS:]
        subscribers = [*self._subscribers.get(session_id, []), *self._global_subscribers]
        for queue in subscribers:
            try:
                queue._publish_locked(evt)
            except RuntimeError:
                # A disconnected subscriber may close its loop during publication.
                continue
        return evt

    def _remember_receipts_locked(self, event: dict[str, Any]) -> None:
        receipt_keys = _RECEIPTS_OF_TYPE.get(str(event.get("type") or ""))
        payload = event.get("payload")
        seq = event.get("seq")
        if not receipt_keys or not isinstance(payload, dict) or not isinstance(seq, int):
            return
        receipt: _Receipt | None = None
        for receipt_key in receipt_keys:
            value = receipt_key.value_in(payload)
            if not value:
                continue
            if receipt is None:
                receipt = _Receipt(
                    seq=seq,
                    event_id=str(event.get("event_id") or ""),
                    session_id=str(event.get("session_id") or ""),
                    event_type=receipt_key.event_type,
                    ts=str(event.get("ts") or ""),
                )
            slot = (receipt_key, value)
            self._receipts[slot] = receipt
            self._receipts.move_to_end(slot)

    def _forget_rotated_receipts_locked(self) -> None:
        """轮转丢掉的最旧那份里的回执不再认：和 iter_persisted_events 看得到的范围一致，表也不随运行时间一直长。"""
        if not self._receipts:
            return
        oldest = self._first_retained_seq_locked()
        if oldest is None:
            return
        while self._receipts:
            slot = next(iter(self._receipts))
            if self._receipts[slot].seq >= oldest:
                return
            self._receipts.popitem(last=False)

    def _first_retained_seq_locked(self) -> int | None:
        """留存日志里最旧一条事件的 seq。哪份读不了就给 None，宁可多记也不能把还留着的回执忘掉。"""
        for path in self._retained_paths():
            try:
                with path.open("rb") as stream:
                    for raw in stream:
                        event = _decode_event_line(raw)
                        if event is not None:
                            seq = event.get("seq")
                            return seq if isinstance(seq, int) else None
            except FileNotFoundError:
                continue
            except OSError:
                return None
        return None

    def _retained_paths(self) -> list[Path]:
        """留存的几份日志，从旧到新：备份 .N 到 .1，最后是活动文件。"""
        paths = [
            self._path.parent / f"{self._path.name}.{index}"
            for index in range(_ONLINE_ROTATE_BACKUPS, 0, -1)
        ]
        paths.append(self._path)
        return paths

    def _iter_persisted_events_locked(self):
        """Yield a stable retained snapshot; caller must hold eventlog_file_lock."""
        for path in self._retained_paths():
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

    def iter_persisted_events_after(self, after_seq: int):
        """seq 大于 after_seq 的留存事件，从旧到新；生成器读完之前一直持着文件锁。

        最后一条不超过 after_seq 的那几份整份不读，起点所在的那份按 seq 跳到附近再顺着读。
        """
        with eventlog_file_lock(self._path):
            yield from self._iter_persisted_events_after_locked(int(after_seq))

    def _iter_persisted_events_after_locked(self, after_seq: int):
        for path in self._retained_paths():
            try:
                stream = path.open("rb")
            except OSError:
                continue
            try:
                with stream:
                    size = stream.seek(0, os.SEEK_END)
                    last = _last_seq(stream, size)
                    if last is not None and last <= after_seq:
                        continue
                    stream.seek(_offset_after_seq(stream, size, after_seq))
                    for raw in stream:
                        event = _decode_event_line(raw)
                        if event is None:
                            continue
                        seq = event.get("seq")
                        if isinstance(seq, int) and seq <= after_seq:
                            continue
                        yield event
            except OSError:
                continue

    def find_receipt(self, receipt_key: ReceiptKey, value: Any) -> dict[str, Any] | None:
        """按回执表查：value 是 receipt_key 指的那个字段的值。

        查到返回那条事件的基本字段（seq、event_id、session_id、type、ts，不带 payload）。表里是启动时留存的几份日志
        加上本实例之后写下的，轮转丢掉的不再认；只查表，不读盘。
        """
        if receipt_key not in RECEIPT_KEYS:
            raise ValueError(f"没有给 {receipt_key.event_type} 的 {'.'.join(receipt_key.path)} 建回执表")
        key = str(value or "").strip()
        if not key:
            return None
        with self._lock:
            receipt = self._receipts.get((receipt_key, key))
        return receipt.envelope() if receipt is not None else None

    def find_delivery_event(
        self, delivery_id: str, *, event_type: str,
    ) -> dict[str, Any] | None:
        """按 payload 里的 delivery_id 查入站、出站和中间回复，返回值同 find_receipt。"""
        receipt_key = _DELIVERY_OF_TYPE.get(event_type)
        if receipt_key is None:
            raise ValueError(f"{event_type} 不按 delivery_id 查重")
        return self.find_receipt(receipt_key, delivery_id)

    def find_outbound_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        """按 delivery_id 查出站消息，返回值同 find_receipt。"""
        return self.find_delivery_event(delivery_id, event_type="outbound_message")

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Subscribe to new events for one session.

        [WS events 2026-05-17] Why: the WebSocket endpoint needs a live channel
        but EventLog must remain the only event source. How: callers receive an
        asyncio.Queue that append() fills with matching session events. Purpose:
        consumers can combine catch-up reads with live delivery.
        """
        queue = _EventQueue(self._lock)
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
        return self.subscribe_global_with_cursor()[0]

    def subscribe_global_with_cursor(self) -> tuple[asyncio.Queue, int]:
        """Subscribe and capture the durable replay boundary under the same lock."""
        queue = _EventQueue(self._lock)
        with self._lock:
            self._global_subscribers.append(queue)
            return queue, self._durable_seq

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
