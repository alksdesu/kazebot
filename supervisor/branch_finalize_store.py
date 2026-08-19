"""Cross-process fenced ownership for entry-branch finalization."""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _windows_pid_alive(pid: int) -> bool | None:
    """Conservatively probe a Windows PID without signalling the process."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(
        process_query_limited_information | synchronize, False, pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            # Protected/system processes are live or at least not provably dead.
            return True
        # Unknown/transient failures must never steal an active lease.
        return None

    try:
        wait_result = wait_for_single_object(handle, 0)
        if wait_result == wait_timeout:
            return True
        if wait_result != wait_object_0:
            return None
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(handle, ctypes.byref(exit_code)):
            return None
        # A signalled handle should have a terminal exit code, but conservatively
        # treat an inconsistent STILL_ACTIVE result as live.
        return True if exit_code.value == still_active else False
    finally:
        close_handle(handle)


_WAL_SWITCH_ATTEMPTS = 8


@dataclass(frozen=True)
class BranchClaim:
    status: str
    identity: str
    owner_run_id: str
    owner_id: str
    owner_pid: int
    token: str
    fencing_generation: int
    lease_expires_at: float


class BranchFinalizeClaimStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            self._ensure_wal(db)
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS branch_finalize_claims (
                    identity TEXT PRIMARY KEY,
                    owner_run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    fencing_generation INTEGER NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _ensure_wal(db: sqlite3.Connection) -> None:
        """Put the database in WAL mode, tolerating a concurrent initializer.

        journal_mode 切换要独占锁，而这一条**不受 busy_timeout 约束** —— sqlite 直接
        返回 SQLITE_BUSY。两个进程同时开这个 store 就必有一个在 __init__ 里崩掉，
        而它存在的理由正是跨进程协调。WAL 是写进文件头的持久属性，已经是 wal 就不必再切。
        """
        for attempt in range(_WAL_SWITCH_ATTEMPTS):
            try:
                row = db.execute("PRAGMA journal_mode").fetchone()
                if row is not None and str(row[0] or "").lower() == "wal":
                    return
                db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == _WAL_SWITCH_ATTEMPTS - 1:
                    raise
                # 撞上了只说明另一个进程正在切；等它切完，下一轮就会读到 wal。
                time.sleep(0.05 * (attempt + 1))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _pid_alive(pid: int) -> bool | None:
        if pid <= 0:
            return None
        if os.name == "nt":
            try:
                return _windows_pid_alive(pid)
            except Exception:
                # Missing/unavailable WinAPI is unknown, never evidence of death.
                return None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None

    @staticmethod
    def _row_claim(row: sqlite3.Row, status: str | None = None) -> BranchClaim:
        return BranchClaim(
            status=status or str(row["status"]),
            identity=str(row["identity"]),
            owner_run_id=str(row["owner_run_id"]),
            owner_id=str(row["owner_id"]),
            owner_pid=int(row["owner_pid"]),
            token=str(row["token"]),
            fencing_generation=int(row["fencing_generation"]),
            lease_expires_at=float(row["lease_expires_at"]),
        )

    def get(self, identity: str) -> BranchClaim | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM branch_finalize_claims WHERE identity=?", (identity,),
            ).fetchone()
        return self._row_claim(row) if row is not None else None

    def seed_legacy(self, identity: str, payload: dict[str, Any]) -> bool:
        """Insert a legacy EventLog owner only when SQLite has no row."""
        lease = payload.get("lease_expires_at")
        try:
            lease_epoch = __import__("datetime").datetime.fromisoformat(str(lease)).timestamp() if lease else 0.0
        except Exception:
            lease_epoch = 0.0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO branch_finalize_claims
                (identity, owner_run_id, owner_id, owner_pid, token,
                 fencing_generation, lease_expires_at, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    str(payload.get("owner_run_id") or payload.get("run_id") or "legacy"),
                    str(payload.get("owner_id") or "legacy"),
                    int(payload.get("owner_pid") or 0),
                    str(payload.get("token") or "legacy"),
                    max(1, int(payload.get("fencing_generation") or 1)),
                    lease_epoch,
                    str(payload.get("status") or "started"),
                    str(payload.get("error") or ""),
                    time.time(),
                ),
            ).rowcount > 0
            db.commit()
            return inserted

    def claim(
        self, identity: str, *, owner_run_id: str, owner_pid: int,
        lease_seconds: float, now: float | None = None,
    ) -> BranchClaim:
        now = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM branch_finalize_claims WHERE identity=?", (identity,),
            ).fetchone()
            if row is not None:
                current = self._row_claim(row)
                if current.status == "completed":
                    db.commit()
                    return BranchClaim(**{**current.__dict__, "status": "completed"})
                alive = self._pid_alive(current.owner_pid)
                lease_active = current.lease_expires_at > now
                if current.status == "started" and lease_active and alive is not False:
                    db.commit()
                    return BranchClaim(**{**current.__dict__, "status": "deferred"})
                fence = current.fencing_generation + 1
            else:
                fence = 1
            owner_id = uuid.uuid4().hex
            token = uuid.uuid4().hex
            lease_expires = now + max(0.1, float(lease_seconds))
            db.execute(
                """
                INSERT INTO branch_finalize_claims
                (identity, owner_run_id, owner_id, owner_pid, token,
                 fencing_generation, lease_expires_at, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'started', '', ?)
                ON CONFLICT(identity) DO UPDATE SET
                  owner_run_id=excluded.owner_run_id,
                  owner_id=excluded.owner_id,
                  owner_pid=excluded.owner_pid,
                  token=excluded.token,
                  fencing_generation=excluded.fencing_generation,
                  lease_expires_at=excluded.lease_expires_at,
                  status='started', error='', updated_at=excluded.updated_at
                """,
                (identity, owner_run_id, owner_id, owner_pid, token, fence, lease_expires, now),
            )
            db.commit()
            return BranchClaim(
                "owned", identity, owner_run_id, owner_id, owner_pid,
                token, fence, lease_expires,
            )

    def _conditional_update(
        self, claim: BranchClaim, *, status: str, lease_expires_at: float,
        error: str = "",
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """
                UPDATE branch_finalize_claims
                SET status=?, lease_expires_at=?, error=?, updated_at=?
                WHERE identity=? AND owner_id=? AND token=?
                  AND fencing_generation=? AND status='started'
                """,
                (
                    status, lease_expires_at, error[:4000], time.time(),
                    claim.identity, claim.owner_id, claim.token,
                    claim.fencing_generation,
                ),
            ).rowcount == 1
            db.commit()
            return changed

    def heartbeat(self, claim: BranchClaim, lease_seconds: float) -> BranchClaim | None:
        lease = time.time() + max(0.1, float(lease_seconds))
        if not self._conditional_update(claim, status="started", lease_expires_at=lease):
            return None
        return BranchClaim(**{**claim.__dict__, "lease_expires_at": lease, "status": "owned"})

    def complete(self, claim: BranchClaim) -> bool:
        return self._conditional_update(claim, status="completed", lease_expires_at=0.0)

    def fail(self, claim: BranchClaim, error: str) -> bool:
        return self._conditional_update(claim, status="failed", lease_expires_at=0.0, error=error)

    def is_owner(self, claim: BranchClaim) -> bool:
        current = self.get(claim.identity)
        return bool(
            current is not None
            and current.status == "started"
            and current.owner_id == claim.owner_id
            and current.token == claim.token
            and current.fencing_generation == claim.fencing_generation
        )

    def fenced_action(
        self, claim: BranchClaim, action: Callable[[], T], *, complete: bool = True,
    ) -> T:
        """Hold SQLite writer fencing across a commit; optionally terminalize."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM branch_finalize_claims WHERE identity=?", (claim.identity,),
            ).fetchone()
            if row is None:
                db.rollback()
                raise RuntimeError("branch finalize claim missing")
            current = self._row_claim(row)
            if not (
                current.status == "started"
                and current.owner_id == claim.owner_id
                and current.token == claim.token
                and current.fencing_generation == claim.fencing_generation
            ):
                db.rollback()
                raise RuntimeError("branch finalize fence lost")
            try:
                result = action()
            except Exception:
                db.rollback()
                raise
            if complete:
                changed = db.execute(
                    """
                    UPDATE branch_finalize_claims SET status='completed',
                      lease_expires_at=0, updated_at=?
                    WHERE identity=? AND owner_id=? AND token=?
                      AND fencing_generation=? AND status='started'
                    """,
                    (time.time(), claim.identity, claim.owner_id, claim.token, claim.fencing_generation),
                ).rowcount
                if changed != 1:
                    db.rollback()
                    raise RuntimeError("branch finalize fenced completion lost")
            db.commit()
            return result
