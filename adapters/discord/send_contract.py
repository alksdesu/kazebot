"""Durable Discord outbound delivery primitives.

The module deliberately does not depend on the Discord runtime so its retry and
idempotency contract can be tested with small platform fakes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import discord  # type: ignore[import-untyped]


class DiscordSendError(RuntimeError):
    """Classified Discord delivery failure consumed by the SDK retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        ambiguous_ack: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous_ack = ambiguous_ack
        self.cause = cause


class DiscordSendNotStartedError(DiscordSendError):
    """Platform send was explicitly cancelled before any request was issued."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message, retryable=True, cause=cause)
        self.definitely_not_sent = True


class DiscordSendContractError(DiscordSendError):
    """Permanent invalid request or missing target."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message, retryable=False, cause=cause)


class DiscordAmbiguousAckError(DiscordSendError):
    """The request may have reached Discord; automatic replay could duplicate it."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message, retryable=False, ambiguous_ack=True, cause=cause)


class DiscordSendInProgress(DiscordSendError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Discord send already in progress: {key}", retryable=True)
        self.key = key


class DiscordIdempotencyOwnershipError(DiscordSendError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Discord idempotency ownership lost: {key}", retryable=True)
        self.key = key


@dataclass(frozen=True)
class DiscordIdempotencyClaim:
    key: str
    acquired: bool
    state: str
    owner: str = ""
    platform_message_ids: tuple[str, ...] = ()
    last_error: str = ""


class DiscordIdempotencyStore:
    """SQLite/WAL pending-owner/lease -> sent store shared across restarts."""

    def __init__(
        self,
        path: Path | str,
        *,
        lease_seconds: float = 900.0,
        sent_ttl: float = 90 * 24 * 60 * 60,
        max_items: int = 100_000,
        ambiguous_ttl: float = 24 * 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.sent_ttl = max(0.0, float(sent_ttl))
        self.max_items = max(0, int(max_items))
        # 永不过期的死信会永久堵死它的 key，靠 begin() 抛错打掉整轮投递。
        self.ambiguous_ttl = max(0.0, float(ambiguous_ttl))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._db = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS claims (
                key TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('pending','sent','ambiguous')),
                updated REAL NOT NULL,
                owner TEXT NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0,
                platform_message_ids TEXT NOT NULL DEFAULT '[]',
                last_error TEXT NOT NULL DEFAULT ''
            )"""
        )
        columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(claims)")}
        table_sql = str(self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone()[0])
        if "ambiguous" not in table_sql or "last_error" not in columns:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("ALTER TABLE claims RENAME TO claims_old")
                self._db.execute(
                    """CREATE TABLE claims (
                        key TEXT PRIMARY KEY,
                        state TEXT NOT NULL CHECK(state IN ('pending','sent','ambiguous')),
                        updated REAL NOT NULL,
                        owner TEXT NOT NULL,
                        lease_until REAL NOT NULL DEFAULT 0,
                        platform_message_ids TEXT NOT NULL DEFAULT '[]',
                        last_error TEXT NOT NULL DEFAULT ''
                    )"""
                )
                old_columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(claims_old)")}
                ids_expr = "platform_message_ids" if "platform_message_ids" in old_columns else "'[]'"
                error_expr = "last_error" if "last_error" in old_columns else "''"
                self._db.execute(
                    f"""INSERT INTO claims
                    (key,state,updated,owner,lease_until,platform_message_ids,last_error)
                    SELECT key,state,updated,owner,lease_until,{ids_expr},{error_expr}
                    FROM claims_old"""
                )
                self._db.execute("DROP TABLE claims_old")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _ids(raw: Any) -> tuple[str, ...]:
        try:
            value = json.loads(str(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return tuple(str(item) for item in value if item is not None)

    def _prune_expired_locked(self, now: float) -> int:
        """Bound committed sent rows and release dead letters past their TTL;
        pending owners are never retention-pruned."""
        before = self._db.total_changes
        if self.sent_ttl > 0:
            self._db.execute(
                "DELETE FROM claims WHERE state='sent' AND updated<?",
                (now - self.sent_ttl,),
            )
        if self.ambiguous_ttl > 0:
            self._db.execute(
                "DELETE FROM claims WHERE state='ambiguous' AND updated<=?",
                (now - self.ambiguous_ttl,),
            )
        if self.max_items > 0:
            sent_count = int(
                self._db.execute("SELECT COUNT(*) FROM claims WHERE state='sent'").fetchone()[0]
            )
            overflow = sent_count - self.max_items
            if overflow > 0:
                self._db.execute(
                    """DELETE FROM claims WHERE key IN (
                        SELECT key FROM claims WHERE state='sent'
                        ORDER BY updated ASC,key ASC LIMIT ?
                    )""",
                    (overflow,),
                )
        return self._db.total_changes - before

    async def prune(self) -> int:
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                removed = self._prune_expired_locked(self._clock())
                self._db.execute("COMMIT")
                return removed
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def begin(self, key: str) -> DiscordIdempotencyClaim:
        if not key:
            raise DiscordSendContractError("Discord idempotency key must not be empty")
        async with self._lock:
            now = self._clock()
            owner = uuid.uuid4().hex
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_expired_locked(now)
                row = self._db.execute(
                    "SELECT state,owner,lease_until,platform_message_ids,last_error FROM claims WHERE key=?",
                    (key,),
                ).fetchone()
                if row is not None and str(row[0]) == "ambiguous":
                    raise DiscordAmbiguousAckError(
                        f"Discord delivery is dead-lettered as ambiguous: {key} ({row[4]})"
                    )
                if row is not None and str(row[0]) == "sent":
                    self._db.execute("COMMIT")
                    return DiscordIdempotencyClaim(
                        key, False, "sent", platform_message_ids=self._ids(row[3])
                    )
                if row is not None and float(row[2]) > now:
                    self._db.execute("COMMIT")
                    return DiscordIdempotencyClaim(key, False, "pending", owner=str(row[1]))
                if row is None:
                    self._db.execute(
                        """INSERT INTO claims
                        (key,state,updated,owner,lease_until,platform_message_ids)
                        VALUES(?,'pending',?,?,?,'[]')""",
                        (key, now, owner, now + self.lease_seconds),
                    )
                else:
                    self._db.execute(
                        """UPDATE claims SET state='pending',updated=?,owner=?,lease_until=?,
                        platform_message_ids='[]' WHERE key=?""",
                        (now, owner, now + self.lease_seconds, key),
                    )
                self._db.execute("COMMIT")
                return DiscordIdempotencyClaim(key, True, "pending", owner=owner)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def heartbeat(self, claim: DiscordIdempotencyClaim) -> float:
        if not claim.acquired:
            raise DiscordIdempotencyOwnershipError(claim.key)
        async with self._lock:
            now = self._clock()
            lease_until = now + self.lease_seconds
            cursor = self._db.execute(
                """UPDATE claims SET updated=?,lease_until=?
                WHERE key=? AND state='pending' AND owner=?""",
                (now, lease_until, claim.key, claim.owner),
            )
            if cursor.rowcount != 1:
                raise DiscordIdempotencyOwnershipError(claim.key)
            return lease_until

    async def commit(
        self,
        claim: DiscordIdempotencyClaim,
        platform_message_ids: list[str] | tuple[str, ...],
    ) -> None:
        if not claim.acquired:
            raise DiscordIdempotencyOwnershipError(claim.key)
        encoded = json.dumps([str(item) for item in platform_message_ids])
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    """UPDATE claims SET state='sent',updated=?,owner='',lease_until=0,
                    platform_message_ids=? WHERE key=? AND state='pending' AND owner=?""",
                    (self._clock(), encoded, claim.key, claim.owner),
                )
                if cursor.rowcount != 1:
                    raise DiscordIdempotencyOwnershipError(claim.key)
                self._prune_expired_locked(self._clock())
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def mark_ambiguous(
        self,
        claim: DiscordIdempotencyClaim,
        *,
        platform_message_ids: list[str] | tuple[str, ...] = (),
        error: BaseException | str = "cancelled during platform acknowledgement",
    ) -> None:
        if not claim.acquired:
            raise DiscordIdempotencyOwnershipError(claim.key)
        encoded = json.dumps([str(item) for item in platform_message_ids])
        detail = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        async with self._lock:
            cursor = self._db.execute(
                """UPDATE claims SET state='ambiguous',updated=?,owner='',lease_until=0,
                platform_message_ids=?,last_error=?
                WHERE key=? AND state='pending' AND owner=?""",
                (self._clock(), encoded, detail[:2000], claim.key, claim.owner),
            )
            if cursor.rowcount != 1:
                raise DiscordIdempotencyOwnershipError(claim.key)

    async def release(self, claim: DiscordIdempotencyClaim) -> None:
        if not claim.acquired:
            raise DiscordIdempotencyOwnershipError(claim.key)
        async with self._lock:
            cursor = self._db.execute(
                "DELETE FROM claims WHERE key=? AND state='pending' AND owner=?",
                (claim.key, claim.owner),
            )
            if cursor.rowcount != 1:
                raise DiscordIdempotencyOwnershipError(claim.key)

    async def state(self, key: str) -> DiscordIdempotencyClaim | None:
        async with self._lock:
            self._prune_expired_locked(self._clock())
            row = self._db.execute(
                "SELECT state,owner,lease_until,platform_message_ids,last_error FROM claims WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if str(row[0]) == "pending" and float(row[2]) <= self._clock():
                return None
            return DiscordIdempotencyClaim(
                key,
                False,
                str(row[0]),
                owner=str(row[1]),
                platform_message_ids=self._ids(row[3]),
                last_error=str(row[4] or ""),
            )

    async def wait_for_resolution(
        self, key: str, *, timeout: float = 30.0
    ) -> DiscordIdempotencyClaim | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.state(key)
            if state is None or state.state != "pending":
                return state
            await asyncio.sleep(0.02)
        raise DiscordSendInProgress(key)


def classify_discord_send_exception(exc: BaseException) -> DiscordSendError:
    """Map Discord/network failures onto safe retry vs permanent/dead-letter."""
    if isinstance(exc, DiscordSendError):
        return exc
    if isinstance(exc, (TypeError, ValueError, FileNotFoundError, PermissionError)):
        return DiscordSendContractError(f"Discord send request is invalid: {exc}", cause=exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return DiscordAmbiguousAckError(
            f"Discord send acknowledgement is ambiguous: {exc}", cause=exc
        )
    if isinstance(exc, discord.NotFound):
        return DiscordSendContractError(f"Discord target does not exist: {exc}", cause=exc)
    if isinstance(exc, discord.Forbidden):
        return DiscordSendContractError(f"Discord target is forbidden: {exc}", cause=exc)
    if isinstance(exc, discord.HTTPException):
        status = int(getattr(exc, "status", 0) or 0)
        if status == 429 or status >= 500:
            return DiscordSendError(f"Discord transient HTTP send failure: {exc}", retryable=True, cause=exc)
        return DiscordSendContractError(f"Discord permanent HTTP send failure: {exc}", cause=exc)
    if isinstance(exc, (ConnectionError, ConnectionResetError, OSError)):
        return DiscordSendError(f"Discord connection send failure: {exc}", retryable=True, cause=exc)
    return DiscordAmbiguousAckError(
        f"Discord send failed with unknown acknowledgement state: {exc}", cause=exc
    )
