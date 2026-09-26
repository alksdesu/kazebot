"""OneBot outbound send contract and in-process idempotency primitives.

This module intentionally has no NoneBot dependency so the delivery contract can be
unit-tested in the core development environment.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from clonoth_sdk.types import DeliveryContext as OutboundSendContext

logger = logging.getLogger(__name__)


class OneBotSendError(RuntimeError):
    """A classified outbound failure.

    ``retryable`` means a retry is believed not to duplicate a successfully delivered
    message. Ambiguous acknowledgement timeouts are therefore non-retryable.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        ambiguous_ack: bool = False,
        definitely_not_sent: bool = False,
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous_ack = ambiguous_ack
        self.definitely_not_sent = definitely_not_sent
        self.cause = cause


class OneBotAttachmentBatchError(OneBotSendError):
    """One or more attachments failed after all batch items were attempted."""

    def __init__(self, errors: list[BaseException]):
        self.errors = tuple(errors)
        retryable = all(getattr(error, "retryable", False) is True for error in errors)
        ambiguous = any(getattr(error, "ambiguous_ack", False) is True for error in errors)
        super().__init__(
            f"{len(errors)} OneBot attachment(s) failed: "
            + "; ".join(str(error) for error in errors),
            retryable=retryable,
            ambiguous_ack=ambiguous,
        )


class OneBotAmbiguousAckError(OneBotSendError):
    """Platform may have accepted the send; automatic replay is unsafe."""

    def __init__(self, message: str, *, cause: BaseException | None = None):
        super().__init__(message, retryable=False, ambiguous_ack=True, cause=cause)


class OneBotSendNotStartedError(OneBotSendError):
    """Cancellation/failure happened before platform send began; release is safe."""

    def __init__(self, message: str, *, cause: BaseException | None = None):
        super().__init__(
            message, retryable=True, definitely_not_sent=True, cause=cause,
        )


class OneBotSendContractError(OneBotSendError):
    """Invalid local send request; retrying without changing inputs cannot help."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class IdempotencyOwnershipError(OneBotSendError):
    """A stale logical-send owner attempted to commit/release a claim."""

    def __init__(self, key: str):
        super().__init__(f"OneBot idempotency ownership lost: {key}", retryable=True)
        self.key = key


class OneBotSendInProgress(OneBotSendError):
    """Another owner still holds the same logical delivery claim."""

    def __init__(self, key: str):
        super().__init__(f"OneBot send already in progress: {key}", retryable=True)
        self.key = key


@dataclass(frozen=True)
class IdempotencyClaim:
    key: str
    acquired: bool
    state: str
    owner: str = ""


@dataclass(frozen=True)
class AmbiguousClaim:
    """One dead-lettered delivery, as shown to an operator."""

    key: str
    updated: float
    platform_message_id: str
    last_error: str

    @property
    def handle(self) -> str:
        # Keys embed content digests and are unwieldy to retype in a chat window.
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class MessagePlan:
    key: str
    delivery_id: str
    claim_key: str
    target: str
    processed_segments: list[dict[str, Any]]
    send_identity: str
    selected_images: list[str]
    conversation_key: str
    quoted: bool = False
    confirmed_at: float = 0.0
    platform_message_id: str = ""


def _plan_payload(prepared: Mapping[str, Any]) -> str:
    if not isinstance(prepared, Mapping):
        raise OneBotSendContractError("message plan builder must return an object")
    segments = prepared.get("processed_segments")
    digests = prepared.get("selected_images", [])
    identity = prepared.get("send_identity")
    conversation = prepared.get("conversation_key", "")
    quoted = prepared.get("quoted", False)
    if not isinstance(segments, list) or any(not isinstance(item, dict) for item in segments):
        raise OneBotSendContractError("message plan requires processed segment objects")
    if not isinstance(digests, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in digests
    ):
        raise OneBotSendContractError("message plan requires SHA-256 image references")
    if not isinstance(identity, str) or not identity or not isinstance(conversation, str):
        raise OneBotSendContractError("message plan requires a send identity and conversation key")
    if not isinstance(quoted, bool):
        raise OneBotSendContractError("message plan quoted flag must be boolean")
    for item in segments:
        if str(item.get("type") or "") != "image":
            continue
        remaining: list[Any] = [item]
        visited: set[int] = set()
        while remaining:
            value = remaining.pop()
            if isinstance(value, (dict, list)):
                if id(value) in visited:
                    continue
                visited.add(id(value))
            if isinstance(value, dict):
                remaining.extend(value.values())
            elif isinstance(value, list):
                remaining.extend(value)
            elif isinstance(value, str) and value.lower().startswith(("base64://", "data:image/")):
                raise OneBotSendContractError("message plans cannot persist inline image data")
        content = str(item.get("url") or item.get("content") or "")
        if content and not content.lower().startswith(("https://", "http://")):
            raise OneBotSendContractError("local plan images must use SHA-256 references, not inline data or paths")
        if not content and not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sticker_sha256") or "")):
            raise OneBotSendContractError("local plan image is missing its SHA-256 reference")
        for name in ("sticker_sha256", "content_sha256"):
            if name in item and not re.fullmatch(r"[0-9a-f]{64}", str(item[name])):
                raise OneBotSendContractError("invalid message plan image digest")
    payload = {
        "processed_segments": segments, "send_identity": identity,
        "selected_images": digests, "conversation_key": conversation, "quoted": quoted,
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OneBotSendContractError("message plan must contain JSON-compatible data") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise OneBotSendContractError("message plan exceeds the persistence size limit")
    return encoded


class TwoPhaseIdempotencyStore:
    """SQLite/WAL pending→sent idempotency store spanning callback and restarts."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        pending_ttl: float = 900.0,
        sent_ttl: float = 7 * 24 * 3600.0,
        max_items: int = 50_000,
        ambiguous_ttl: float = 24 * 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root = Path(os.environ.get("CLONOTH_WORKSPACE") or Path.cwd())
        configured = Path(path) if path is not None else Path("data/onebot_outbound_idempotency.sqlite3")
        self.path = (configured if configured.is_absolute() else root / configured).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_ttl = max(300.0, pending_ttl)
        self.lease_seconds = self.pending_ttl
        self.sent_ttl = sent_ttl
        self.max_items = max_items
        # A never-expiring dead letter permanently blocks its key; content-derived
        # keys recur, so the block outlives the incident it came from.
        self.ambiguous_ttl = max(0.0, float(ambiguous_ttl))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._db = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            self._enable_wal(self._db)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL,owner TEXT NOT NULL,lease_until REAL NOT NULL DEFAULT 0,retention_until REAL NOT NULL DEFAULT 0,platform_message_id TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '')"
            )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(claims)")}
            if "lease_until" not in columns:
                self._db.execute("ALTER TABLE claims ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
            if "retention_until" not in columns:
                self._db.execute("ALTER TABLE claims ADD COLUMN retention_until REAL NOT NULL DEFAULT 0")
            if "platform_message_id" not in columns:
                self._db.execute("ALTER TABLE claims ADD COLUMN platform_message_id TEXT NOT NULL DEFAULT ''")
            if "last_error" not in columns:
                self._db.execute("ALTER TABLE claims ADD COLUMN last_error TEXT NOT NULL DEFAULT ''")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS message_plans (
                delivery_id TEXT PRIMARY KEY,plan_key TEXT NOT NULL,claim_key TEXT NOT NULL UNIQUE,
                target TEXT NOT NULL,state TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,lease_until REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL,expires REAL NOT NULL,cleanup_after REAL NOT NULL,
                delivery_ttl REAL NOT NULL,payload TEXT NOT NULL DEFAULT '',
                receipt_state TEXT NOT NULL DEFAULT '',confirmed_at REAL NOT NULL DEFAULT 0,
                platform_message_id TEXT NOT NULL DEFAULT '')"""
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS message_plan_active_key ON message_plans(plan_key) WHERE active=1"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS message_plan_receipts ON message_plans(receipt_state,confirmed_at)"
            )
            self._db.execute("CREATE TABLE IF NOT EXISTS outbound_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            self._db.execute(
                "INSERT OR IGNORE INTO outbound_metadata(key,value) VALUES('message_plan_protocol_started_at',?)",
                (str(self._clock()),),
            )
            self.protocol_started_at = float(self._db.execute(
                "SELECT value FROM outbound_metadata WHERE key='message_plan_protocol_started_at'"
            ).fetchone()[0])
            self._db.execute("COMMIT")
        except BaseException:
            try:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
            finally:
                self._db.close()
            raise

    @staticmethod
    def _enable_wal(db: sqlite3.Connection) -> None:
        timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
        deadline = time.monotonic() + 15.0
        delay = 0.01
        try:
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                db.execute(f"PRAGMA busy_timeout={min(250, int(remaining * 1000))}")
                try:
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if str(mode).lower() != "wal":
                        raise sqlite3.OperationalError("OneBot outbound store could not enable WAL")
                    return
                except sqlite3.OperationalError as error:
                    if (getattr(error, "sqlite_errorcode", 0) & 0xff) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.1)
        finally:
            db.execute(f"PRAGMA busy_timeout={timeout}")

    def close(self) -> None:
        self._db.close()

    def _expire_sent_plans_locked(self, now: float) -> None:
        expired_receipts = int(self._db.execute(
            "SELECT COUNT(*) FROM message_plans WHERE state='sent' AND receipt_state='pending' AND cleanup_after<=?",
            (now,),
        ).fetchone()[0])
        self._db.execute("DELETE FROM message_plans WHERE state='sent' AND cleanup_after<=?", (now,))
        if expired_receipts:
            logger.warning("onebot_message_receipt_repair_expired count=%d", expired_receipts)

    def _prune_locked(self, now: float) -> None:
        quarantine = max(self.sent_ttl, self.ambiguous_ttl)
        self._db.execute(
            """UPDATE message_plans SET state='ambiguous',updated=?,expires=?,cleanup_after=?
            WHERE state='ready' AND claim_key IN
            (SELECT key FROM claims WHERE state='pending' AND lease_until<=?)""",
            (now, now + quarantine, now + quarantine, now),
        )
        self._db.execute(
            """UPDATE claims SET state='ambiguous',updated=?,owner='',lease_until=0,
            last_error='platform acknowledgement unknown after delivery lease expired'
            WHERE state='pending' AND lease_until<=? AND key IN
            (SELECT claim_key FROM message_plans WHERE state='ambiguous')""",
            (now, now),
        )
        self._db.execute("DELETE FROM message_plans WHERE state='building' AND lease_until<=?", (now,))
        self._db.execute(
            """UPDATE message_plans SET active=0 WHERE state!='building' AND expires<=?
            AND claim_key NOT IN (SELECT key FROM claims WHERE state='pending')""", (now,),
        )
        self._expire_sent_plans_locked(now)
        self._db.execute(
            """DELETE FROM message_plans WHERE state!='building' AND cleanup_after<=?
            AND claim_key NOT IN (SELECT key FROM claims WHERE state='pending')""", (now,),
        )
        self._db.execute(
            "DELETE FROM claims WHERE state='pending' AND lease_until<=?",
            (now,),
        )
        self._db.execute(
            "DELETE FROM claims WHERE state='sent' AND retention_until>0 AND retention_until<=?",
            (now,),
        )
        if self.ambiguous_ttl > 0:
            cursor = self._db.execute(
                """DELETE FROM claims WHERE state='ambiguous' AND updated<=?
                AND key NOT IN (SELECT claim_key FROM message_plans WHERE state='ambiguous')""",
                (now - self.ambiguous_ttl,),
            )
            if cursor.rowcount:
                logger.info(
                    "onebot_dead_letter_expired count=%d ttl=%.0f", cursor.rowcount, self.ambiguous_ttl,
                )
        count = int(self._db.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        if self.max_items > 0 and count > self.max_items:
            # Never prune a valid pending owner merely to satisfy the bound. Remove
            # oldest sent tombstones only; pending rows may temporarily exceed it.
            self._db.execute(
                """DELETE FROM claims WHERE key IN (SELECT key FROM claims WHERE state='sent'
                AND key NOT IN (SELECT claim_key FROM message_plans) ORDER BY updated LIMIT ?)""",
                (count - self.max_items,),
            )

    @staticmethod
    def _message_plan(row: tuple[Any, ...]) -> MessagePlan:
        payload = json.loads(row[4])
        return MessagePlan(
            key=str(row[0]), delivery_id=str(row[1]), claim_key=str(row[2]), target=str(row[3]),
            processed_segments=payload["processed_segments"], send_identity=payload["send_identity"],
            selected_images=payload["selected_images"], conversation_key=payload["conversation_key"],
            quoted=payload.get("quoted", False), confirmed_at=float(row[5]), platform_message_id=str(row[6]),
        )

    def _read_message_plan_locked(self, delivery_id: str) -> MessagePlan:
        row = self._db.execute(
            """SELECT plan_key,delivery_id,claim_key,target,payload,confirmed_at,platform_message_id
            FROM message_plans WHERE delivery_id=?""", (delivery_id,),
        ).fetchone()
        if row is None or not row[4]:
            raise OneBotSendContractError("message plan is missing or incomplete")
        return self._message_plan(row)

    async def _heartbeat_message_plan(self, delivery_id: str, owner: str) -> None:
        while True:
            await asyncio.sleep(max(0.05, min(30.0, self.lease_seconds / 3)))
            async with self._lock:
                cursor = self._db.execute(
                    "UPDATE message_plans SET lease_until=? WHERE delivery_id=? AND state='building' AND owner=?",
                    (self._clock() + self.lease_seconds, delivery_id, owner),
                )
                if cursor.rowcount != 1:
                    return

    async def get_or_create_message_plan(
        self, key: str, target: Mapping[str, Any],
        builder: Callable[[], Awaitable[Mapping[str, Any]]], *,
        sent_ttl: float | None = None, wait_timeout: float = 30.0,
    ) -> MessagePlan:
        """Freeze a preparation-only builder once; it must never call the platform."""
        if not key:
            raise OneBotSendContractError("message plan key must not be empty")
        target_key = target_identity(target)
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while True:
            async with self._lock:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    now = self._clock()
                    self._prune_locked(now)
                    row = self._db.execute(
                        "SELECT delivery_id,state,target FROM message_plans WHERE plan_key=? AND active=1", (key,),
                    ).fetchone()
                    if row and row[2] != target_key:
                        raise OneBotSendContractError("message plan target mismatch")
                    if row and row[1] == "ambiguous":
                        self._db.execute("COMMIT")
                        raise OneBotAmbiguousAckError("message plan has an unknown platform acknowledgement")
                    if row and row[1] != "building":
                        plan = self._read_message_plan_locked(str(row[0]))
                        self._db.execute("COMMIT")
                        return plan
                    if row is None:
                        count = int(self._db.execute("SELECT COUNT(*) FROM message_plans").fetchone()[0])
                        if self.max_items > 0 and count >= self.max_items:
                            raise OneBotSendInProgress(key)
                        delivery_id, owner = uuid.uuid4().hex, uuid.uuid4().hex
                        claim_key = f"event:message-plan:{delivery_id}:{target_key}"
                        ttl = self.sent_ttl if sent_ttl is None else max(1.0, float(sent_ttl))
                        retention = max(self.sent_ttl, ttl, self.lease_seconds)
                        self._db.execute(
                            """INSERT INTO message_plans(delivery_id,plan_key,claim_key,target,state,owner,
                            lease_until,updated,expires,cleanup_after,delivery_ttl)
                            VALUES(?,?,?,?,'building',?,?,?,?,?,?)""",
                            (delivery_id, key, claim_key, target_key, owner, now + self.lease_seconds,
                             now, now + retention, now + retention, ttl),
                        )
                    self._db.execute("COMMIT")
                except BaseException:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise
            if row is None:
                break
            if time.monotonic() >= deadline:
                raise OneBotSendInProgress(key)
            await asyncio.sleep(0.02)

        heartbeat = asyncio.create_task(self._heartbeat_message_plan(delivery_id, owner))
        try:
            encoded = _plan_payload(await builder())
            empty = not json.loads(encoded)["processed_segments"]
            async with self._lock:
                now = self._clock()
                ready_retention = ttl if empty else retention
                cursor = self._db.execute(
                    """UPDATE message_plans SET state='ready',owner='',lease_until=0,payload=?,updated=?,
                    expires=?,cleanup_after=?
                    WHERE delivery_id=? AND state='building' AND owner=?""",
                    (encoded, now, now + ready_retention, now + ready_retention, delivery_id, owner),
                )
                if cursor.rowcount != 1:
                    raise IdempotencyOwnershipError(key)
                return self._read_message_plan_locked(delivery_id)
        except BaseException:
            async with self._lock:
                self._db.execute(
                    "DELETE FROM message_plans WHERE delivery_id=? AND state='building' AND owner=?",
                    (delivery_id, owner),
                )
            raise
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def pending_message_receipts(
        self, *, limit: int = 100, delivery_id: str = "",
    ) -> list[MessagePlan]:
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._expire_sent_plans_locked(self._clock())
                query = """SELECT plan_key,delivery_id,claim_key,target,payload,confirmed_at,platform_message_id
                FROM message_plans WHERE state='sent' AND receipt_state='pending'"""
                params: list[Any] = []
                if delivery_id:
                    query += " AND delivery_id=?"
                    params.append(delivery_id)
                query += " ORDER BY confirmed_at,delivery_id LIMIT ?"
                params.append(max(1, min(1000, int(limit))))
                rows = self._db.execute(query, params).fetchall()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return [self._message_plan(row) for row in rows]

    async def ack_message_receipt(self, delivery_id: str) -> bool:
        async with self._lock:
            cursor = self._db.execute(
                "UPDATE message_plans SET receipt_state='acked' WHERE delivery_id=? AND state='sent'",
                (delivery_id,),
            )
            return cursor.rowcount == 1

    async def has_legacy_message_claims(self, event_prefix: str, target: Mapping[str, Any]) -> bool:
        if not event_prefix:
            return False
        prefix = f"event:{event_prefix}:"
        suffix = f":{target_identity(target)}"
        async with self._lock:
            row = self._db.execute(
                """SELECT 1 FROM claims WHERE updated<? AND substr(key,1,?)=? AND substr(key,-?)=?
                AND key NOT IN (SELECT claim_key FROM message_plans) LIMIT 1""",
                (self.protocol_started_at, len(prefix), prefix, len(suffix), suffix),
            ).fetchone()
            return row is not None

    async def begin(self, key: str) -> IdempotencyClaim:
        if not key:
            raise OneBotSendContractError("idempotency key must not be empty")
        async with self._lock:
            now = self._clock()
            owner = uuid.uuid4().hex
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_locked(now)
                planned = self._db.execute(
                    "SELECT state FROM message_plans WHERE claim_key=?", (key,),
                ).fetchone()
                if planned and planned[0] == "ambiguous":
                    self._db.execute("COMMIT")
                    raise OneBotAmbiguousAckError("message plan has an unknown platform acknowledgement")
                if planned and planned[0] == "sent":
                    self._db.execute("COMMIT")
                    return IdempotencyClaim(key=key, acquired=False, state="sent")
                row = self._db.execute(
                    "SELECT state,last_error FROM claims WHERE key=?", (key,)
                ).fetchone()
                if row is not None and str(row[0]) == "ambiguous":
                    raise OneBotAmbiguousAckError(
                        f"OneBot delivery is dead-lettered as ambiguous: {key} ({row[1]})"
                    )
                if row is not None:
                    self._db.execute("COMMIT")
                    return IdempotencyClaim(key=key, acquired=False, state=str(row[0]))
                self._db.execute(
                    "INSERT INTO claims(key,state,updated,owner,lease_until) VALUES(?, 'pending', ?, ?, ?)",
                    (key, now, owner, now + self.lease_seconds),
                )
                self._db.execute("COMMIT")
                return IdempotencyClaim(key=key, acquired=True, state="pending", owner=owner)
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    async def heartbeat(self, claim: IdempotencyClaim) -> float:
        if not claim.acquired:
            raise IdempotencyOwnershipError(claim.key)
        async with self._lock:
            now = self._clock()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    "UPDATE claims SET updated=?,lease_until=? WHERE key=? AND state='pending' AND owner=?",
                    (now, now + self.lease_seconds, claim.key, claim.owner),
                )
                if cursor.rowcount != 1:
                    raise IdempotencyOwnershipError(claim.key)
                self._prune_locked(now)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return now + self.lease_seconds

    async def commit(
        self, claim: IdempotencyClaim, *, sent_ttl: float | None = None,
        platform_message_id: str = "",
    ) -> None:
        if not claim.acquired:
            raise IdempotencyOwnershipError(claim.key)
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                planned = self._db.execute(
                    "SELECT state,delivery_ttl FROM message_plans WHERE claim_key=?", (claim.key,),
                ).fetchone()
                if planned and planned[0] != "ready":
                    raise OneBotSendContractError("only a prepared message plan can be committed")
                ttl = float(planned[1]) if planned else self.sent_ttl if sent_ttl is None else max(1.0, sent_ttl)
                cursor = self._db.execute(
                    "UPDATE claims SET state='sent',updated=?,owner='',lease_until=0,retention_until=?,platform_message_id=? WHERE key=? AND state='pending' AND owner=?",
                    (now, now + ttl, str(platform_message_id or ""), claim.key, claim.owner),
                )
                if cursor.rowcount != 1:
                    raise IdempotencyOwnershipError(claim.key)
                if planned:
                    self._db.execute(
                        """UPDATE message_plans SET state='sent',updated=?,expires=?,cleanup_after=?,
                        receipt_state='pending',confirmed_at=?,platform_message_id=? WHERE claim_key=?""",
                        (now, now + ttl, now + max(ttl, self.sent_ttl), now,
                         str(platform_message_id or ""), claim.key),
                    )
                self._prune_locked(now)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    async def sent_message_id(self, key: str) -> str:
        async with self._lock:
            row = self._db.execute("SELECT platform_message_id FROM claims WHERE key=? AND state='sent'", (key,)).fetchone()
            if row is None:
                row = self._db.execute(
                    "SELECT platform_message_id FROM message_plans WHERE claim_key=? AND state='sent' AND cleanup_after>?",
                    (key, self._clock()),
                ).fetchone()
            return str(row[0] or "") if row else ""

    async def mark_ambiguous(
        self,
        claim: IdempotencyClaim,
        *,
        platform_message_id: str = "",
        error: BaseException | str = "cancelled during platform acknowledgement",
    ) -> None:
        if not claim.acquired:
            raise IdempotencyOwnershipError(claim.key)
        detail = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    """UPDATE claims SET state='ambiguous',updated=?,owner='',lease_until=0,
                    platform_message_id=?,last_error=?
                    WHERE key=? AND state='pending' AND owner=?""",
                    (self._clock(), str(platform_message_id), detail[:2000], claim.key, claim.owner),
                )
                if cursor.rowcount != 1:
                    raise IdempotencyOwnershipError(claim.key)
                now = self._clock()
                quarantine = max(self.sent_ttl, self.ambiguous_ttl)
                self._db.execute(
                    """UPDATE message_plans SET state='ambiguous',updated=?,expires=?,cleanup_after=?,
                    platform_message_id=? WHERE claim_key=? AND state='ready'""",
                    (now, now + quarantine, now + quarantine, str(platform_message_id or ""), claim.key),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def release(self, claim: IdempotencyClaim) -> None:
        if not claim.acquired:
            raise IdempotencyOwnershipError(claim.key)
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._db.execute(
                    "DELETE FROM claims WHERE key=? AND state='pending' AND owner=?",
                    (claim.key, claim.owner),
                )
                if cursor.rowcount != 1:
                    raise IdempotencyOwnershipError(claim.key)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    async def state(self, key: str) -> str | None:
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_locked(self._clock())
                row = self._db.execute("SELECT state FROM claims WHERE key=?", (key,)).fetchone()
                if row is None:
                    row = self._db.execute(
                        "SELECT state FROM message_plans WHERE claim_key=? AND state IN ('sent','ambiguous')",
                        (key,),
                    ).fetchone()
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return str(row[0]) if row else None

    async def ambiguous_claims(self, *, limit: int = 20) -> list[AmbiguousClaim]:
        """List dead letters, newest first; ``limit<=0`` means no bound."""
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_locked(self._clock())
                query = (
                    "SELECT key,updated,platform_message_id,last_error FROM claims "
                    "WHERE state='ambiguous' ORDER BY updated DESC"
                )
                params: tuple = ()
                if limit > 0:
                    query += " LIMIT ?"
                    params = (limit,)
                rows = self._db.execute(query, params).fetchall()
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return [
            AmbiguousClaim(
                key=str(row[0]),
                updated=float(row[1]),
                platform_message_id=str(row[2]),
                last_error=str(row[3]),
            )
            for row in rows
        ]

    async def ambiguous_count(self) -> int:
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_locked(self._clock())
                count = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM claims WHERE state='ambiguous'"
                    ).fetchone()[0]
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return count

    async def clear_ambiguous(self, key: str) -> bool:
        """Delete one dead letter; ``False`` when the key is not dead-lettered."""
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                self._db.execute(
                    """UPDATE message_plans SET state='ready',updated=?,expires=?,cleanup_after=?,
                    receipt_state='',confirmed_at=0,platform_message_id=''
                    WHERE claim_key=? AND state='ambiguous' AND claim_key IN
                    (SELECT key FROM claims WHERE state='ambiguous')""",
                    (now, now + self.sent_ttl, now + self.sent_ttl, key),
                )
                # Restricted to ambiguous: deleting a sent row would license a real duplicate.
                cursor = self._db.execute(
                    "DELETE FROM claims WHERE key=? AND state='ambiguous'", (key,),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

    async def clear_all_ambiguous(self) -> int:
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                self._db.execute(
                    """UPDATE message_plans SET state='ready',updated=?,expires=?,cleanup_after=?,
                    receipt_state='',confirmed_at=0,platform_message_id=''
                    WHERE state='ambiguous' AND claim_key IN (SELECT key FROM claims WHERE state='ambiguous')""",
                    (now, now + self.sent_ttl, now + self.sent_ttl),
                )
                cursor = self._db.execute("DELETE FROM claims WHERE state='ambiguous'")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return cursor.rowcount

    async def wait_for_resolution(self, key: str, timeout: float = 30.0) -> str | None:
        """Wait for the owner; raise an explicit retryable conflict on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.state(key)
            if state != "pending":
                return state
            await asyncio.sleep(0.02)
        raise OneBotSendInProgress(key)


async def protected_claim_send(
    store: TwoPhaseIdempotencyStore,
    claim: IdempotencyClaim,
    operation: Callable[[], Awaitable[Any]],
    *,
    sent_ttl: float | None = None,
    message_id_getter: Callable[[Any], str] = lambda result: str(result or ""),
) -> Any:
    """Shield platform send+commit and persist unknown cancellation as ambiguous."""
    async def send_and_commit() -> Any:
        try:
            result = await operation()
        except asyncio.CancelledError as exc:
            await asyncio.shield(store.mark_ambiguous(claim, error=exc))
            raise OneBotAmbiguousAckError(
                f"OneBot send cancelled with ambiguous acknowledgement: {claim.key}",
                cause=exc,
            ) from exc
        except Exception as exc:
            classified = classify_send_exception(exc)
            if classified.ambiguous_ack:
                await store.mark_ambiguous(claim, error=classified)
            else:
                await store.release(claim)
            raise classified from exc

        platform_message_id = message_id_getter(result)
        try:
            await store.commit(claim, sent_ttl=sent_ttl, platform_message_id=platform_message_id)
        except asyncio.CancelledError as exc:
            await asyncio.shield(store.mark_ambiguous(
                claim, platform_message_id=platform_message_id, error=exc,
            ))
            raise OneBotAmbiguousAckError(
                f"OneBot commit cancelled after platform send: {claim.key}", cause=exc,
            ) from exc
        except Exception as exc:
            await store.mark_ambiguous(
                claim, platform_message_id=platform_message_id, error=exc,
            )
            raise OneBotAmbiguousAckError(
                f"OneBot platform sent but idempotency commit failed: {claim.key}",
                cause=exc,
            ) from exc
        return result

    protected = asyncio.create_task(send_and_commit())
    while True:
        try:
            return await asyncio.shield(protected)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()
            if protected.done():
                return protected.result()


def validate_send_request(bot: Any, target: Mapping[str, Any] | None) -> str:
    """Validate mandatory routing inputs and return the normalized target identity."""
    if bot is None:
        raise OneBotSendContractError("OneBot send missing bot")
    if target is None:
        raise OneBotSendContractError("OneBot send missing target")
    return target_identity(target)


# NapCat reports unrelated failures with the same retcode, so only message/wording text separates them.
_AMBIGUOUS_TIMEOUT_HINTS = ("sendmsg", "call api", "networkerror", "network error")
_TEMP_FILE_HINTS = ("enoent", "no such file")
# NapCat fails at message assembly time when an at segment's uid cannot be resolved.
_AT_UID_HINTS = ("get uid", "uid error", "getuiderror")
_NOT_STARTED_HINTS = _TEMP_FILE_HINTS + _AT_UID_HINTS + ("connection refused",)


def send_error_text(exc: BaseException) -> str:
    return f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''} {exc}".lower()


def is_temp_file_missing(exc: BaseException) -> bool:
    return any(hint in send_error_text(exc) for hint in _TEMP_FILE_HINTS)


def is_at_uid_failure(exc: BaseException) -> bool:
    return any(hint in send_error_text(exc) for hint in _AT_UID_HINTS)


def classify_send_exception(exc: BaseException) -> OneBotSendError:
    """Classify known OneBot/NapCat failures by retry safety."""
    if isinstance(exc, OneBotSendError):
        return exc
    text = send_error_text(exc)
    if "timeout" in text and any(hint in text for hint in _AMBIGUOUS_TIMEOUT_HINTS):
        return OneBotAmbiguousAckError(
            f"OneBot send acknowledgement is ambiguous: {exc}", cause=exc,
        )
    if isinstance(exc, ConnectionRefusedError) or any(hint in text for hint in _NOT_STARTED_HINTS):
        return OneBotSendNotStartedError(
            f"OneBot send did not reach the platform: {exc}", cause=exc,
        )
    if isinstance(exc, (ConnectionError, ConnectionResetError)) or "connection reset" in text:
        return OneBotSendError(f"OneBot send failed: {exc}", retryable=True, cause=exc)
    permanent = any(token in text for token in (
        "invalid", "bad request", "forbidden", "not found", "unknown group", "unknown user",
    ))
    if permanent:
        return OneBotSendError(f"OneBot permanent send failure: {exc}", retryable=False, cause=exc)
    return OneBotAmbiguousAckError(f"OneBot send ack is unknown: {exc}", cause=exc)


def target_identity(target: Mapping[str, Any]) -> str:
    target_type = str(target.get("type") or "").strip()
    if target_type == "group":
        target_id = target.get("group_id")
    elif target_type == "private":
        target_id = target.get("user_id")
    else:
        raise OneBotSendContractError(f"unknown QQ target type: {dict(target)!r}")
    if target_id is None or str(target_id).strip() == "":
        raise OneBotSendContractError(f"{target_type} target missing {'group_id' if target_type == 'group' else 'user_id'}")
    return f"{target_type}:{target_id}"


def target_from_idempotency_key(key: str) -> str:
    """Recover the ``group:<id>`` / ``private:<id>`` identity a claim key was built for."""
    if key.startswith("content:"):
        parts = key.split(":")
        value = f"{parts[1]}:{parts[2]}" if len(parts) >= 4 else ""
    else:
        parts = key.rsplit(":", 2)
        value = ":".join(parts[-2:]) if len(parts) >= 2 else ""
    return value if re.fullmatch(r"(group|private):\d+", value) else ""


def make_idempotency_key(
    target: Mapping[str, Any],
    content_identity: str,
    *,
    event_id: str = "",
) -> str:
    """Prefer outbound event identity, falling back to target + content digest."""
    target_key = target_identity(target)
    content_digest = hashlib.sha256(content_identity.encode("utf-8", "ignore")).hexdigest()
    if event_id:
        return f"event:{event_id}:{target_key}"
    return f"content:{target_key}:{content_digest}"


def image_content_identity(data: bytes) -> str:
    """Return one identity for local-path and base64 representations of an image."""
    return f"image:sha256:{hashlib.sha256(data).hexdigest()}"


def context_from_sources(
    *,
    trigger: Any = None,
    main_state: Any = None,
    platform_data: Mapping[str, Any] | None = None,
    event_data: Mapping[str, Any] | None = None,
    conversation_key: str = "",
) -> OutboundSendContext:
    """Build context from old and future SDK callback parameter shapes.

    Existing SDK versions expose ``trigger`` and ``main_state`` only. Future callers
    may place event metadata in either platform_data dictionary or pass event_data.
    """
    merged: dict[str, Any] = {}
    trigger_data = getattr(trigger, "platform_data", None)
    if isinstance(trigger_data, Mapping):
        merged.update(trigger_data)
    state_data = getattr(main_state, "platform_data", None)
    if isinstance(state_data, Mapping):
        merged.update(state_data)
    if isinstance(platform_data, Mapping):
        merged.update(platform_data)
    if isinstance(event_data, Mapping):
        merged.update(event_data)
    payload = merged.get("payload") if isinstance(merged.get("payload"), Mapping) else {}

    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value is not None and str(value) != "" else None
        except (TypeError, ValueError):
            return None

    return OutboundSendContext(
        event_seq=_integer(merged.get("event_seq", merged.get("seq"))) or 0,
        event_id=str(merged.get("event_id") or ""),
        task_id=str(payload.get("task_id") or merged.get("task_id") or getattr(trigger, "task_id", "") or ""),
        source_inbound_seq=_integer(
            payload.get("source_inbound_seq", merged.get("source_inbound_seq", getattr(trigger, "inbound_seq", None)))
        ) or 0,
        conversation_key=str(
            payload.get("conversation_key")
            or merged.get("conversation_key")
            or conversation_key
            or getattr(trigger, "conversation_key", "")
            or ""
        ),
        attempt=_integer(merged.get("attempt")) or 1,
        idempotency_key=str(merged.get("idempotency_key") or ""),
        replay_generation=_integer(merged.get("replay_generation")) or 0,
        force_replay=bool(merged.get("force_replay", False)),
    )
