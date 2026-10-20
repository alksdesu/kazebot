from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from clonoth_runtime import get_int, load_runtime_config
from engine.eventlog_rotation import eventlog_file_lock, eventlog_lock_path


MAX_PAGE_CHARS = 20_000
MAX_RECORD_CHARS = 12_000
MAX_RESULTS = 50
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_ARCHIVE_ID = re.compile(r"[a-f0-9]{64}\Z")


def _resolved(path: Path) -> Path:
    value = str(path.resolve())
    if os.name == "nt":
        # Windows may keep the extended prefix while SQLite holds a shared-memory file open.
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise ValueError("Invalid context archive session")
    return value


def _integer(value: int, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        result = parsed.timestamp()
    else:
        raise ValueError("Invalid timestamp")
    if not math.isfinite(result):
        raise ValueError("Invalid timestamp")
    datetime.fromtimestamp(result, timezone.utc)
    return result


def _message_timestamp(record: dict[str, Any]) -> float | None:
    candidates = [record.get("created_at")]
    for key in ("meta", "_meta"):
        meta = record.get(key)
        if isinstance(meta, dict):
            candidates.extend((meta.get("timestamp"), meta.get("time")))
    for value in candidates:
        try:
            stamp = _timestamp(value)
            if stamp is not None:
                return stamp
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _search_text(record: dict[str, Any]) -> str:
    parts = [record.get("content", "")]
    parts.extend(record[key] for key in ("tool_calls", "tool_call_id", "name") if record.get(key))
    return "\n".join(part if isinstance(part, str) else _json(part) for part in parts)


class ContextArchiveStore:
    def __init__(self, workspace_root: Path):
        self.workspace_root = _resolved(Path(workspace_root))
        self.path = self.workspace_root / "data" / "context_archive.sqlite3"
        self._initialization_path = self.path.with_suffix(".initialize")

    @property
    def retention_days(self) -> int:
        return get_int(
            load_runtime_config(self.workspace_root),
            "engine.context_archive.retention_days", 7, min_value=1, max_value=3650,
        )

    def _cutoff(self) -> float:
        return time.time() - self.retention_days * 86400

    def _check_path(self) -> None:
        for path in (
            self.path.parent, self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"),
            eventlog_lock_path(self._initialization_path),
        ):
            if not _resolved(path).is_relative_to(self.workspace_root):
                raise ValueError("Context archive path escapes workspace")

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return
        if version != 0:
            raise ValueError("Unsupported context archive schema version")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS archives (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                reason TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                operation_id TEXT,
                message_count INTEGER NOT NULL,
                UNIQUE(session_id, reason, source_hash),
                UNIQUE(session_id, reason, operation_id)
            );
            CREATE INDEX IF NOT EXISTS archive_session_created
                ON archives(session_id, created_at);
            CREATE TABLE IF NOT EXISTS messages (
                archive_id TEXT NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                timestamp REAL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                search_text TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY(archive_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS archive_message_time
                ON messages(archive_id, timestamp);
            PRAGMA user_version=1;
            COMMIT;
        """)

    @contextmanager
    def _connect(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        self._check_path()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = None
        try:
            # Only initialization needs this lock; normal WAL transactions use SQLite's own locks.
            with eventlog_file_lock(self._initialization_path):
                connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                self._initialize(connection)
            yield connection
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "archive_id": row["id"],
            "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
            "reason": row["reason"],
            "message_count": row["message_count"],
        }

    def archive(
        self, session_id: str, messages: list[Any], *, reason: str, operation_id: str = "",
    ) -> dict[str, Any]:
        session_id = _session_id(session_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 128:
            raise ValueError("Archive reason must contain 1 to 128 characters")
        if not isinstance(operation_id, str) or len(operation_id) > 512:
            raise ValueError("Archive operation_id must contain at most 512 characters")
        if not isinstance(messages, (list, tuple)):
            raise ValueError("Archive messages must be a list")
        records = [message.to_dict() if hasattr(message, "to_dict") else message for message in messages]
        if any(not isinstance(record, dict) for record in records):
            raise ValueError("Archive messages must contain objects")
        serialized = [_json(record) for record in records]
        source_hash = hashlib.sha256(("[" + ",".join(serialized) + "]").encode("utf-8")).hexdigest()
        with self._connect(create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                cutoff = now - self.retention_days * 86400
                connection.execute(
                    "DELETE FROM archives WHERE session_id=? AND reason=? AND created_at<=? "
                    "AND (source_hash=? OR operation_id=?)",
                    (session_id, reason, cutoff, source_hash, operation_id or None),
                )
                if operation_id:
                    previous = connection.execute(
                        "SELECT * FROM archives WHERE session_id=? AND reason=? AND operation_id=?",
                        (session_id, reason, operation_id),
                    ).fetchone()
                    if previous is not None and previous["source_hash"] != source_hash:
                        raise ValueError("Archive operation_id was already used for a different snapshot")
                existing = connection.execute(
                    "SELECT * FROM archives WHERE session_id=? AND reason=? AND source_hash=?",
                    (session_id, reason, source_hash),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._receipt(existing)
                archive_id = hashlib.sha256(
                    _json([session_id, reason, source_hash, uuid.uuid4().hex]).encode("utf-8"),
                ).hexdigest()
                connection.execute(
                    "INSERT INTO archives VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (archive_id, session_id, now, reason, source_hash, operation_id or None, len(records)),
                )
                for ordinal, (record, data) in enumerate(zip(records, serialized)):
                    text = _search_text(record)
                    connection.execute(
                        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (archive_id, ordinal, str(record.get("id") or ""), _message_timestamp(record),
                         str(record.get("role") or ""), text, text.casefold(), data),
                    )
                row = connection.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
                connection.commit()
                return self._receipt(row)
            except BaseException:
                connection.rollback()
                raise

    def search(
        self, session_id: str, query: str = "", after: str = "", before: str = "",
        limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        session_id = _session_id(session_id)
        limit = _integer(limit, "limit", minimum=1, maximum=MAX_RESULTS)
        offset = _integer(offset, "offset", minimum=0, maximum=1_000_000)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Archive query must contain at most 200 characters")
        start, end = _timestamp(after), _timestamp(before)
        if start is not None and end is not None and start > end:
            raise ValueError("after must not be later than before")
        result: dict[str, Any] = {"matches": [], "next_offset": None}
        self._check_path()
        if not self.path.exists():
            return result
        clauses = ["a.session_id=?", "a.created_at>?"]
        values: list[Any] = [session_id, self._cutoff()]
        if query:
            escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("m.search_text LIKE ? ESCAPE '\\'")
            values.append(f"%{escaped}%")
        for operator, value in ((">=", start), ("<=", end)):
            if value is not None:
                clauses.append(f"m.timestamp {operator} ?")
                values.append(value)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.archive_id, m.ordinal, m.message_id, m.timestamp, m.role, m.text, "
                "a.created_at FROM messages m JOIN archives a ON a.id=m.archive_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY a.created_at DESC, a.id, m.ordinal LIMIT ? OFFSET ?",
                (*values, limit + 1, offset),
            ).fetchall()
        budget = MAX_PAGE_CHARS - 1000
        for row in rows[:limit]:
            text = row["text"]
            match_index = text.casefold().find(query.casefold()) if query else 0
            begin = max(0, match_index - 200)
            excerpt = text[begin:begin + 1200]
            item = {
                "archive_id": row["archive_id"], "offset": row["ordinal"],
                "message_id": row["message_id"][:512], "role": row["role"][:64],
                "created_at": datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat()
                if row["timestamp"] is not None else None,
                "excerpt": excerpt, "truncated": begin > 0 or begin + len(excerpt) < len(text),
            }
            size = len(_json(item))
            if size > budget:
                break
            result["matches"].append(item)
            budget -= size
        consumed = len(result["matches"])
        if len(rows) > consumed:
            result["next_offset"] = offset + consumed
        return result

    def read(
        self, session_id: str, archive_id: str, offset: int = 0, limit: int = 20,
        char_offset: int = 0,
    ) -> dict[str, Any]:
        session_id = _session_id(session_id)
        if not isinstance(archive_id, str) or not _ARCHIVE_ID.fullmatch(archive_id):
            raise ValueError("Invalid context archive id")
        limit = _integer(limit, "limit", minimum=1, maximum=MAX_RESULTS)
        offset = _integer(offset, "offset", minimum=0, maximum=1_000_000)
        char_offset = _integer(char_offset, "char_offset", minimum=0, maximum=2**63 - 1)
        self._check_path()
        if not self.path.exists():
            raise FileNotFoundError("Context archive not found or expired")
        with self._connect() as connection:
            connection.execute("BEGIN")
            archive = connection.execute(
                "SELECT * FROM archives WHERE id=? AND session_id=? AND created_at>?",
                (archive_id, session_id, self._cutoff()),
            ).fetchone()
            if archive is None:
                raise FileNotFoundError("Context archive not found or expired")
            rows = connection.execute(
                "SELECT ordinal, data FROM messages WHERE archive_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?",
                (archive_id, offset, limit),
            ).fetchall()
        result = {**self._receipt(archive), "messages": [], "next_offset": None, "next_char_offset": 0}
        budget = MAX_PAGE_CHARS - 1000
        for row in rows:
            raw = row["data"]
            begin = char_offset if row["ordinal"] == offset else 0
            if begin > len(raw):
                raise ValueError("char_offset exceeds message length")
            if len(raw) <= MAX_RECORD_CHARS and begin == 0:
                item = {"offset": row["ordinal"], "message": json.loads(raw)}
                next_offset, next_char_offset = row["ordinal"] + 1, 0
            else:
                chunk = raw[begin:begin + MAX_RECORD_CHARS // 2]
                item = {
                    "offset": row["ordinal"], "data_json_chunk": chunk,
                    "char_offset": begin, "total_chars": len(raw),
                }
                complete = begin + len(chunk) >= len(raw)
                next_offset = row["ordinal"] + 1 if complete else row["ordinal"]
                next_char_offset = 0 if complete else begin + len(chunk)
            size = len(_json(item))
            if size > budget:
                break
            result["messages"].append(item)
            budget -= size
            result["next_offset"] = next_offset if next_offset < archive["message_count"] else None
            result["next_char_offset"] = next_char_offset
            if next_char_offset:
                break
        return result

    def purge(self, dry_run: bool = False) -> int:
        self._check_path()
        if not self.path.exists():
            return 0
        cutoff = self._cutoff()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM archives WHERE created_at<=?", (cutoff,),
                ).fetchone()[0]
                if not dry_run:
                    connection.execute("DELETE FROM archives WHERE created_at<=?", (cutoff,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return int(count)

    def purge_session(self, session_id: str) -> int:
        session_id = _session_id(session_id)
        self._check_path()
        if not self.path.exists():
            return 0
        with self._connect() as connection:
            deleted = connection.execute("DELETE FROM archives WHERE session_id=?", (session_id,)).rowcount
        return deleted
