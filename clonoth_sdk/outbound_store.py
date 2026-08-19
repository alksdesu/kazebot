"""SQLite/WAL durable outbox with cross-process delivery leases."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .types import Event

logger = logging.getLogger(__name__)


class OutboundOwnershipError(RuntimeError):
    """A writer attempted to mutate a row it no longer owns."""


@dataclass
class OutboundRecord:
    key: str
    event: dict[str, Any]
    event_id: str
    seq: int
    task_id: str
    attempt: int = 0
    last_error: str = ""
    next_retry: float = 0.0
    received_at: float = field(default_factory=time.time)
    delivery: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    owner: str = ""
    lease_until: float = 0.0
    replay_generation: int = 0
    record_type: str = "outbound_message"
    sent_at: float = 0.0


class OutboundStore:
    """Transactional SQLite outbox.

    ``due()`` is only a scanner.  A sender must acquire ``claim()`` immediately
    before platform I/O. Every terminal/retry mutation validates the claim owner and
    ``delivering`` state, so a stale process cannot overwrite a newer ``sent`` row.
    """

    _MIGRATION_MARKER = "legacy_json_v1_complete"

    def __init__(self, path: Path):
        requested = Path(path).resolve()
        self.legacy_path = requested if requested.suffix.lower() == ".json" else None
        self.path = requested.with_suffix(".sqlite3") if self.legacy_path else requested
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.legacy_path and self.legacy_path.exists() and not self.path.exists():
            raw = self._read_legacy()
            self._publish_legacy_database(raw)
        self._db = self._connect(self.path, wal=True)
        try:
            self._verify_integrity(self._db)
            self._create_or_upgrade_schema()
            self._verify_integrity(self._db)
        except Exception:
            self._db.close()
            logger.critical(
                "outbound_store_open_failed", exc_info=True,
                extra={"outbound_path": str(self.path), "outcome": "load_failed"},
            )
            raise

    @staticmethod
    def _connect(path: Path, *, wal: bool) -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=WAL" if wal else "PRAGMA journal_mode=DELETE")
        return db

    @staticmethod
    def _verify_integrity(db: sqlite3.Connection) -> None:
        rows = db.execute("PRAGMA quick_check").fetchall()
        if not rows or any(str(row[0]).lower() != "ok" for row in rows):
            details = [str(row[0]) for row in rows]
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {details}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    @staticmethod
    @contextmanager
    def _transaction_on(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def stable_key(
        event_id: str, seq: int, record_type: str = "outbound_message",
    ) -> str:
        """Identity is namespaced by event type; intermediate/final may share seq."""
        event_id = str(event_id or "").strip()
        identity = f"id:{event_id}" if event_id else f"seq:{int(seq or 0)}"
        return f"{record_type}:{identity}"

    @property
    def processed_seq(self) -> int:
        row = self._db.execute("SELECT value FROM metadata WHERE key='processed_seq'").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _create_schema_on(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata(key,value) VALUES('processed_seq','0')")
        db.execute(
            """CREATE TABLE outbound (
                key TEXT PRIMARY KEY, event_json TEXT NOT NULL, event_id TEXT NOT NULL,
                seq INTEGER NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', next_retry REAL NOT NULL DEFAULT 0,
                received_at REAL NOT NULL, delivery_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN ('pending','delivering','dead_letter','sent')),
                owner TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                replay_generation INTEGER NOT NULL DEFAULT 0,
                record_type TEXT NOT NULL DEFAULT 'outbound_message',
                sent_at REAL NOT NULL DEFAULT 0
            )"""
        )
        db.execute("CREATE UNIQUE INDEX outbound_event_id ON outbound(record_type,event_id) WHERE event_id<>''")
        db.execute("CREATE UNIQUE INDEX outbound_seq ON outbound(record_type,seq) WHERE seq>0")
        db.execute("CREATE INDEX outbound_due ON outbound(status,next_retry,lease_until)")

    def _create_or_upgrade_schema(self) -> None:
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound'"
        ).fetchone()
        if not exists:
            with self._transaction():
                self._create_schema_on(self._db)
            return
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(outbound)")}
        table_sql = str(self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbound'"
        ).fetchone()[0])
        if {
            "owner", "lease_until", "replay_generation", "record_type", "sent_at",
        }.issubset(columns) and "delivering" in table_sql:
            return
        # Upgrade the first-round schema in one explicit transaction.
        with self._transaction():
            self._db.execute("ALTER TABLE outbound RENAME TO outbound_old")
            self._db.execute("DROP INDEX IF EXISTS outbound_event_id")
            self._db.execute("DROP INDEX IF EXISTS outbound_seq")
            self._db.execute("DROP INDEX IF EXISTS outbound_due")
            self._db.execute(
                """CREATE TABLE outbound (
                    key TEXT PRIMARY KEY, event_json TEXT NOT NULL, event_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', next_retry REAL NOT NULL DEFAULT 0,
                    received_at REAL NOT NULL, delivery_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL CHECK(status IN ('pending','delivering','dead_letter','sent')),
                    owner TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    replay_generation INTEGER NOT NULL DEFAULT 0,
                    record_type TEXT NOT NULL DEFAULT 'outbound_message',
                    sent_at REAL NOT NULL DEFAULT 0
                )"""
            )
            self._db.execute(
                """INSERT INTO outbound
                (key,event_json,event_id,seq,task_id,attempt,last_error,next_retry,received_at,delivery_json,status,record_type,sent_at)
                SELECT key,event_json,event_id,seq,task_id,attempt,last_error,next_retry,received_at,delivery_json,status,
                       'outbound_message', CASE WHEN status='sent' THEN received_at ELSE 0 END
                FROM outbound_old"""
            )
            self._db.execute("DROP TABLE outbound_old")
            self._db.execute("CREATE UNIQUE INDEX outbound_event_id ON outbound(record_type,event_id) WHERE event_id<>''")
            self._db.execute("CREATE UNIQUE INDEX outbound_seq ON outbound(record_type,seq) WHERE seq>0")
            self._db.execute("CREATE INDEX outbound_due ON outbound(status,next_retry,lease_until)")

    def _read_legacy(self) -> dict[str, Any]:
        assert self.legacy_path is not None
        try:
            raw = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("outbox", []), list):
                raise ValueError("legacy outbox root/schema is invalid")
            return raw
        except Exception:
            logger.critical(
                "outbound_legacy_load_failed", exc_info=True,
                extra={"outbound_path": str(self.legacy_path), "outcome": "load_failed"},
            )
            raise

    def _publish_legacy_database(self, raw: dict[str, Any]) -> None:
        """Build/validate a temporary DB, then atomically publish it."""
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.migrating")
        db: sqlite3.Connection | None = None
        try:
            db = self._connect(temporary, wal=False)
            with self._transaction_on(db):
                self._create_schema_on(db)
                db.execute(
                    "UPDATE metadata SET value=? WHERE key='processed_seq'",
                    (str(max(0, int(raw.get("processed_seq") or 0))),),
                )
                for item in raw.get("outbox", []):
                    if not isinstance(item, dict):
                        raise ValueError("legacy outbox contains a non-object row")
                    event = dict(item.get("event") or {})
                    event_id, seq = str(item.get("event_id") or ""), int(item.get("seq") or 0)
                    db.execute(
                        """INSERT INTO outbound
                        (key,event_json,event_id,seq,task_id,attempt,last_error,next_retry,received_at,delivery_json,status)
                        VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')""",
                        (
                            str(item.get("key") or self.stable_key(event_id, seq, "outbound_message")),
                            json.dumps(event, ensure_ascii=False), event_id, seq,
                            str(item.get("task_id") or ""), max(0, int(item.get("attempt") or 0)),
                            str(item.get("last_error") or ""), max(0.0, float(item.get("next_retry") or 0)),
                            float(item.get("received_at") or time.time()),
                            json.dumps(dict(item.get("delivery") or {}), ensure_ascii=False),
                        ),
                    )
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('migration_marker',?)",
                    (self._MIGRATION_MARKER,),
                )
            self._verify_integrity(db)
            marker = db.execute("SELECT value FROM metadata WHERE key='migration_marker'").fetchone()
            if not marker or marker[0] != self._MIGRATION_MARKER:
                raise sqlite3.DatabaseError("legacy migration marker missing")
            db.close()
            db = None
            os.replace(temporary, self.path)
        except Exception:
            if db is not None:
                db.close()
            if temporary.exists():
                temporary.unlink()
            raise

    @staticmethod
    def _record(row: sqlite3.Row) -> OutboundRecord:
        return OutboundRecord(
            key=row["key"], event=json.loads(row["event_json"]), event_id=row["event_id"],
            seq=int(row["seq"]), task_id=row["task_id"], attempt=int(row["attempt"]),
            last_error=row["last_error"], next_retry=float(row["next_retry"]),
            received_at=float(row["received_at"]), delivery=json.loads(row["delivery_json"]),
            status=row["status"], owner=row["owner"], lease_until=float(row["lease_until"]),
            replay_generation=int(row["replay_generation"]),
            record_type=row["record_type"], sent_at=float(row["sent_at"]),
        )

    def _find(
        self, record_type: str, event_id: str, seq: int,
    ) -> OutboundRecord | None:
        row = None
        if event_id:
            row = self._db.execute(
                "SELECT * FROM outbound WHERE record_type=? AND event_id=?",
                (record_type, event_id),
            ).fetchone()
        if row is None and seq > 0:
            row = self._db.execute(
                "SELECT * FROM outbound WHERE record_type=? AND seq=?",
                (record_type, seq),
            ).fetchone()
        return self._record(row) if row else None

    def enqueue(self, event: Event, *, delivery: dict[str, Any] | None = None) -> OutboundRecord | None:
        record_type = str(event.type or "outbound_message")
        with self._transaction():
            existing = self._find(record_type, event.event_id, event.seq)
            if existing:
                if delivery:
                    merged = dict(existing.delivery)
                    for key, value in delivery.items():
                        if value not in (None, ""):
                            merged.setdefault(key, value)
                    self._db.execute(
                        "UPDATE outbound SET delivery_json=? WHERE key=?",
                        (json.dumps(merged, ensure_ascii=False), existing.key),
                    )
                    existing.delivery = merged
                return None if existing.status == "sent" else existing
            record = OutboundRecord(
                key=self.stable_key(event.event_id, event.seq, record_type), event=event.to_dict(),
                event_id=event.event_id, seq=event.seq,
                task_id=str(event.payload.get("task_id") or ""), delivery=dict(delivery or {}),
                record_type=record_type,
            )
            try:
                self._db.execute(
                    """INSERT INTO outbound
                    (key,event_json,event_id,seq,task_id,received_at,delivery_json,status,record_type)
                    VALUES(?,?,?,?,?,?,?,'pending',?)""",
                    (record.key, json.dumps(record.event, ensure_ascii=False), record.event_id,
                     record.seq, record.task_id, record.received_at,
                     json.dumps(record.delivery, ensure_ascii=False), record_type),
                )
                return record
            except sqlite3.IntegrityError:
                raced = self._find(record_type, event.event_id, event.seq)
                if raced is None:
                    raise
                return None if raced.status == "sent" else raced

    def claim(
        self, record: OutboundRecord, *, owner: str, lease_seconds: float, now: float | None = None,
    ) -> OutboundRecord | None:
        current = time.time() if now is None else now
        lease_until = current + max(1.0, lease_seconds)
        with self._transaction():
            cursor = self._db.execute(
                """UPDATE outbound SET status='delivering',owner=?,lease_until=?
                WHERE key=? AND (
                    (status='pending' AND next_retry<=?) OR
                    (status='delivering' AND lease_until<=?)
                )""",
                (owner, lease_until, record.key, current, current),
            )
            if cursor.rowcount != 1:
                return None
            row = self._db.execute("SELECT * FROM outbound WHERE key=?", (record.key,)).fetchone()
        return self._record(row)

    def heartbeat(
        self, record: OutboundRecord, *, owner: str, lease_seconds: float,
        now: float | None = None,
    ) -> float:
        lease_until = (time.time() if now is None else now) + max(1.0, lease_seconds)
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE outbound SET lease_until=? WHERE key=? AND status='delivering' AND owner=?",
                (lease_until, record.key, owner),
            )
            if cursor.rowcount != 1:
                raise OutboundOwnershipError(f"outbox heartbeat ownership lost: {record.key}")
        record.lease_until = lease_until
        return lease_until

    def release_claim(self, record: OutboundRecord, *, owner: str) -> None:
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE outbound SET status='pending',owner='',lease_until=0 WHERE key=? AND status='delivering' AND owner=?",
                (record.key, owner),
            )
            if cursor.rowcount != 1:
                raise OutboundOwnershipError(f"outbox release ownership lost: {record.key}")

    def acknowledge(self, record: OutboundRecord, *, owner: str) -> None:
        """Atomically commit sent row and cursor, validating the current owner."""
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE outbound SET status='sent',owner='',lease_until=0,next_retry=0,sent_at=? WHERE key=? AND status='delivering' AND owner=?",
                (time.time(), record.key, owner),
            )
            if cursor.rowcount != 1:
                raise OutboundOwnershipError(f"outbox ack ownership lost: {record.key}")
            if record.seq > 0:
                self._db.execute(
                    "UPDATE metadata SET value=CAST(MAX(CAST(value AS INTEGER), ?) AS TEXT) WHERE key='processed_seq'",
                    (record.seq,),
                )
        record.status, record.owner = "sent", ""

    def mark_failed(
        self, record: OutboundRecord, error: BaseException, *, owner: str,
        initial_delay: float, max_delay: float, now: float | None = None,
    ) -> float:
        attempt = record.attempt + 1
        delay = max(0.05, min(max_delay, initial_delay * (2 ** min(attempt - 1, 30))))
        next_retry = (time.time() if now is None else now) + delay
        last_error = f"{type(error).__name__}: {error}"[:2000]
        with self._transaction():
            cursor = self._db.execute(
                """UPDATE outbound SET status='pending',owner='',lease_until=0,
                attempt=?,last_error=?,next_retry=?
                WHERE key=? AND status='delivering' AND owner=?""",
                (attempt, last_error, next_retry, record.key, owner),
            )
            if cursor.rowcount != 1:
                raise OutboundOwnershipError(f"outbox failure ownership lost: {record.key}")
        record.attempt, record.last_error, record.next_retry = attempt, last_error, next_retry
        record.status, record.owner = "pending", ""
        return delay

    def mark_dead_letter(self, record: OutboundRecord, error: BaseException, *, owner: str) -> None:
        attempt = record.attempt + 1
        last_error = f"{type(error).__name__}: {error}"[:2000]
        with self._transaction():
            cursor = self._db.execute(
                """UPDATE outbound SET status='dead_letter',owner='',lease_until=0,
                attempt=?,last_error=?,next_retry=0
                WHERE key=? AND status='delivering' AND owner=?""",
                (attempt, last_error, record.key, owner),
            )
            if cursor.rowcount != 1:
                raise OutboundOwnershipError(f"outbox dead-letter ownership lost: {record.key}")
        record.status, record.owner, record.attempt, record.last_error = "dead_letter", "", attempt, last_error

    def force_replay(
        self, *, event_id: str = "", seq: int = 0, record_type: str = "",
    ) -> OutboundRecord:
        """Force a true platform replay with a new replay generation identity.

        ``record_type`` disambiguates intermediate/final rows that may intentionally
        share a server sequence in compatibility/recovery scenarios.
        """
        with self._transaction():
            if record_type:
                record = self._find(record_type, str(event_id or ""), int(seq or 0))
            else:
                clauses, params = [], []
                if event_id:
                    clauses.append("event_id=?")
                    params.append(str(event_id))
                if seq:
                    clauses.append("seq=?")
                    params.append(int(seq))
                if not clauses:
                    raise KeyError("event_id or seq is required")
                rows = self._db.execute(
                    f"SELECT * FROM outbound WHERE {' OR '.join(clauses)}", params,
                ).fetchall()
                if len(rows) > 1:
                    raise KeyError("outbound identity is ambiguous; provide record_type")
                record = self._record(rows[0]) if rows else None
            if record is None:
                raise KeyError(
                    f"outbound event not found: type={record_type!r} event_id={event_id!r} seq={seq}"
                )
            if record.status == "delivering" and record.lease_until > time.time():
                raise OutboundOwnershipError(f"cannot force replay active delivery: {record.key}")
            self._db.execute(
                """UPDATE outbound SET status='pending',owner='',lease_until=0,next_retry=0,
                last_error='',replay_generation=replay_generation+1 WHERE key=?""",
                (record.key,),
            )
            row = self._db.execute("SELECT * FROM outbound WHERE key=?", (record.key,)).fetchone()
        return self._record(row)

    def prune_sent(
        self, *, ttl_seconds: float, max_rows: int, now: float | None = None,
    ) -> int:
        """Reclaim sent event JSON only; never touch recoverable/failed rows."""
        current = time.time() if now is None else now
        cutoff = current - max(0.0, ttl_seconds)
        removed = 0
        with self._transaction():
            cursor_seq = self.processed_seq
            cursor = self._db.execute(
                "DELETE FROM outbound WHERE status='sent' AND seq<=? AND sent_at>0 AND sent_at<=?",
                (cursor_seq, cutoff),
            )
            removed += max(0, cursor.rowcount)
            if max_rows > 0:
                count = int(self._db.execute(
                    "SELECT COUNT(*) FROM outbound WHERE status='sent'"
                ).fetchone()[0])
                excess = count - max_rows
                if excess > 0:
                    cursor = self._db.execute(
                        """DELETE FROM outbound WHERE key IN (
                            SELECT key FROM outbound
                            WHERE status='sent' AND seq<=?
                            ORDER BY sent_at,seq LIMIT ?
                        )""",
                        (cursor_seq, excess),
                    )
                    removed += max(0, cursor.rowcount)
        return removed

    def due(self, now: float | None = None) -> list[OutboundRecord]:
        current = time.time() if now is None else now
        rows = self._db.execute(
            """SELECT * FROM outbound WHERE
            (status='pending' AND next_retry<=?) OR
            (status='delivering' AND lease_until<=?)
            ORDER BY next_retry,seq,received_at""",
            (current, current),
        ).fetchall()
        return [self._record(row) for row in rows]

    def next_retry_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(CASE WHEN status='pending' THEN next_retry ELSE lease_until END)
            FROM outbound WHERE status='pending' OR status='delivering'"""
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def pending(self) -> list[OutboundRecord]:
        rows = self._db.execute(
            "SELECT * FROM outbound WHERE status IN ('pending','delivering') ORDER BY seq,received_at"
        ).fetchall()
        return [self._record(row) for row in rows]

    def dead_letters(self) -> list[OutboundRecord]:
        rows = self._db.execute(
            "SELECT * FROM outbound WHERE status='dead_letter' ORDER BY seq,received_at"
        ).fetchall()
        return [self._record(row) for row in rows]
