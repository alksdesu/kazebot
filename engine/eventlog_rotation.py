"""Cross-process coordination for the append-only event log.

Both the Supervisor writer and the periodic cleanup process use this module.  The
lock is an atomically-created directory rather than a platform-specific advisory
file lock, so the protocol has identical semantics on POSIX and Windows.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK_SUFFIX = ".rotate.lock"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_STALE_AFTER = 120.0

# signals.jsonl 的阈值放在这里，因为 engine 进程的在线轮转（signals/bridge.py）和
# 离线的 data_cleanup 都只依赖本模块；两边取不同值会互相踩掉备份编号。
SIGNALS_MAX_BYTES = int(os.environ.get("CLONOTH_SIGNALS_MAX_BYTES", str(20 * 1024 * 1024)) or 0)
SIGNALS_BACKUPS = int(os.environ.get("CLONOTH_SIGNALS_BACKUPS", "2") or 0)

logger = logging.getLogger(__name__)


class EventLogRotationRecoveryError(RuntimeError):
    """An incomplete rotation could not be recovered without risking data loss."""


def eventlog_lock_path(path: Path) -> Path:
    """Return the lock directory shared by append and rotation operations."""
    return path.with_name(path.name + _LOCK_SUFFIX)


def _windows_pid_is_alive(pid: int) -> bool:
    """Probe a Windows process without sending it any signal.

    ``os.kill(pid, 0)`` is not a harmless existence probe on Windows: CPython can
    route non-console signals through TerminateProcess.  A waitable process handle
    gives us the same information without modifying the target process.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(
        process_query_limited_information | synchronize, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            # Protected/system processes are live but not queryable by this user.
            return True
        # Be conservative for transient or unfamiliar OpenProcess failures: never
        # steal a lock from an owner whose death cannot be established.
        return True

    try:
        wait_result = wait_for_single_object(handle, 0)
        if wait_result == wait_object_0:
            return False
        if wait_result == wait_timeout:
            return True
        return True
    finally:
        close_handle(handle)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _lock_is_stale(lock_path: Path, stale_after: float) -> bool:
    try:
        age = max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return False

    owner_path = lock_path / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = int(owner.get("pid", 0))
        hostname = str(owner.get("hostname", ""))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # The owner may still be writing metadata immediately after mkdir().
        return age >= stale_after

    if hostname == socket.gethostname():
        # Never steal from a demonstrably live local owner merely because a slow
        # filesystem operation exceeded the age threshold.
        return not _pid_is_alive(pid)
    return age >= stale_after


def _recover_stale_lock(lock_path: Path, stale_after: float) -> bool:
    """Atomically claim and remove a stale lock directory, if it is still stale."""
    if not _lock_is_stale(lock_path, stale_after):
        return False
    abandoned = lock_path.with_name(f"{lock_path.name}.stale.{uuid.uuid4().hex}")
    try:
        # rename is the ownership claim: only one contender can move this exact dir.
        os.rename(lock_path, abandoned)
    except OSError:
        return False
    shutil.rmtree(abandoned, ignore_errors=True)
    return True


@contextmanager
def eventlog_file_lock(
    path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    stale_after: float = _DEFAULT_STALE_AFTER,
) -> Iterator[None]:
    """Acquire the event-log protocol lock.

    A dead local owner is recovered immediately.  Locks with missing/unreadable
    owner metadata or a remote hostname are recovered only after ``stale_after``.
    """
    lock_path = eventlog_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)
    token = uuid.uuid4().hex

    while True:
        try:
            lock_path.mkdir()
        except (FileExistsError, PermissionError):
            # On Windows a directory being renamed/removed can briefly report
            # ACCESS_DENIED instead of EXISTS to a concurrent mkdir.
            if _recover_stale_lock(lock_path, stale_after):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for event log lock: {lock_path}")
            time.sleep(0.01)
            continue

        owner = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "token": token,
            "created_at": time.time(),
        }
        try:
            (lock_path / "owner.json").write_text(
                json.dumps(owner, separators=(",", ":")), encoding="utf-8"
            )
        except Exception:
            shutil.rmtree(lock_path, ignore_errors=True)
            raise
        break

    try:
        # A process may have died after staging or publishing data files.  Recover
        # its durable transaction before any append, scan, or new rotation proceeds.
        _recover_incomplete_rotations(path)
        yield
    finally:
        # Only remove a directory that still carries our ownership token.  This
        # prevents a delayed owner from deleting a successor after stale recovery.
        try:
            current = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            released = lock_path.with_name(f"{lock_path.name}.released.{token}")
            # Windows can transiently reject a directory rename while another
            # contender is probing it.  Silently abandoning the canonical lock
            # would deadlock every future writer because the live owner PID is
            # intentionally never considered stale.  Retry for a bounded period;
            # only the matching token owner is allowed to vacate this directory.
            moved = False
            for _ in range(500):
                try:
                    os.rename(lock_path, released)
                except FileNotFoundError:
                    moved = released.exists()
                    break
                except (PermissionError, OSError):
                    time.sleep(0.01)
                    continue
                else:
                    moved = True
                    break
            if moved:
                shutil.rmtree(released, ignore_errors=True)
            else:
                raise TimeoutError(f"failed to release event log lock: {lock_path}")


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Rename within the protocol lock, refusing to overwrite a destination."""
    if destination.exists():
        raise FileExistsError(destination)
    os.rename(source, destination)


def _manifest_path(path: Path, transaction: str) -> Path:
    return path.with_name(f".{path.name}.rotate.manifest.{transaction}.json")


def _write_manifest(manifest_path: Path, manifest: dict) -> None:
    temporary = manifest_path.with_name(f"{manifest_path.name}.write.{uuid.uuid4().hex}")
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _entry_paths(entry: dict) -> tuple[Path, Path, Path | None]:
    original = Path(entry["original"])
    temporary = Path(entry["temporary"])
    destination_raw = entry.get("destination")
    destination = Path(destination_raw) if destination_raw else None
    return original, temporary, destination


def _rollback_manifest(manifest_path: Path, manifest: dict) -> None:
    """Undo every published file, then restore every original source."""
    entries = list(manifest["entries"])
    phase = str(manifest.get("phase") or "staging")
    try:
        if phase != "staging":
            # Reverse publication before restoring originals.  A missing temp plus
            # an existing destination means that entry was atomically published,
            # even if the process died before recording any finer-grained state.
            for entry in reversed(entries):
                _, temporary, destination = _entry_paths(entry)
                if destination is None or temporary.exists():
                    continue
                if destination.exists():
                    _rename_no_replace(destination, temporary)

        for entry in reversed(entries):
            original, temporary, _ = _entry_paths(entry)
            if temporary.exists():
                _rename_no_replace(temporary, original)
            elif not original.exists():
                raise FileNotFoundError(
                    f"rotation transaction lost both source and staging file: {original}"
                )
        manifest_path.unlink()
    except Exception as exc:
        logger.critical(
            "event log rotation rollback failed; refusing further access; manifest=%s",
            manifest_path,
            exc_info=True,
        )
        raise EventLogRotationRecoveryError(
            f"event log rotation rollback failed; recovery manifest retained: {manifest_path}"
        ) from exc


def _finish_committed_manifest(manifest_path: Path, manifest: dict) -> None:
    """Finish the irreversible oldest-backup discard after a crash."""
    try:
        for entry in manifest["entries"]:
            _, temporary, destination = _entry_paths(entry)
            if destination is None and temporary.exists():
                temporary.unlink()
        manifest_path.unlink()
    except Exception as exc:
        logger.critical(
            "event log committed rotation cleanup failed; refusing access; manifest=%s",
            manifest_path,
            exc_info=True,
        )
        raise EventLogRotationRecoveryError(
            f"committed event log rotation needs recovery: {manifest_path}"
        ) from exc


def _recover_incomplete_rotations(path: Path) -> None:
    manifests = sorted(path.parent.glob(f".{path.name}.rotate.manifest.*.json"))
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != 1 or not isinstance(manifest.get("entries"), list):
                raise ValueError("unsupported or malformed rotation manifest")
            if manifest.get("phase") == "committing":
                _finish_committed_manifest(manifest_path, manifest)
            else:
                _rollback_manifest(manifest_path, manifest)
        except EventLogRotationRecoveryError:
            raise
        except Exception as exc:
            logger.critical(
                "event log rotation manifest is unreadable; refusing access: %s",
                manifest_path,
                exc_info=True,
            )
            raise EventLogRotationRecoveryError(
                f"unreadable event log rotation manifest: {manifest_path}"
            ) from exc

    # Staging data without its manifest cannot be assigned safely to an original.
    staging_name = re.compile(
        re.escape(f".{path.name}.rotate.") + r"[0-9a-f]{32}\.\d+"
    )
    orphans = [
        candidate for candidate in path.parent.iterdir()
        if staging_name.fullmatch(candidate.name)
    ]
    if orphans:
        logger.critical("orphan event log rotation staging files: %s", orphans)
        raise EventLogRotationRecoveryError(
            f"orphan event log rotation staging files; refusing access: {orphans}"
        )


def rotate_event_log_locked(path: Path, *, max_bytes: int, backups: int) -> bool:
    """Rotate under the file lock using a durable, fully reversible transaction."""
    if max_bytes <= 0 or backups <= 0:
        return False
    try:
        active_stat = path.stat()
    except FileNotFoundError:
        return False
    if active_stat.st_size < max_bytes:
        return False

    sources = [path] + [path.with_name(f"{path.name}.{i}") for i in range(1, backups + 1)]
    transaction = uuid.uuid4().hex
    manifest_path = _manifest_path(path, transaction)
    entries: list[dict[str, str | None]] = []
    for index, source in enumerate(sources):
        if not source.exists():
            continue
        destination = None
        if index < backups:
            destination = str(path.with_name(f"{path.name}.{index + 1}"))
        entries.append(
            {
                "original": str(source),
                "temporary": str(path.with_name(f".{path.name}.rotate.{transaction}.{index}")),
                "destination": destination,
            }
        )

    manifest = {
        "version": 1,
        "transaction": transaction,
        "active": str(path),
        "backups": backups,
        "phase": "staging",
        "entries": entries,
    }
    _write_manifest(manifest_path, manifest)
    committing_persisted = False
    try:
        for entry in entries:
            original, temporary, _ = _entry_paths(entry)
            _rename_no_replace(original, temporary)

        manifest["phase"] = "publishing"
        _write_manifest(manifest_path, manifest)
        for entry in entries:
            _, temporary, destination = _entry_paths(entry)
            if destination is not None:
                _rename_no_replace(temporary, destination)

        # Mark the transaction committed before the only irreversible operation.
        # A crash from this point is recovered by finishing the discard, never by
        # pretending that the original oldest backup can still be restored.
        manifest["phase"] = "committing"
        _write_manifest(manifest_path, manifest)
        committing_persisted = True
        for entry in entries:
            _, temporary, destination = _entry_paths(entry)
            if destination is None and temporary.exists():
                temporary.unlink()
        manifest_path.unlink()
        return True
    except Exception as exc:
        # ``committing`` is the durable point of no return.  The oldest staging
        # file may already be gone, so rollback could only manufacture a broken
        # original chain.  Preserve the manifest and let the next lock acquisition
        # idempotently finish either the pending discard or manifest cleanup.
        durable_phase = None
        try:
            durable_phase = json.loads(
                manifest_path.read_text(encoding="utf-8")
            ).get("phase")
        except (OSError, json.JSONDecodeError):
            pass
        if committing_persisted or durable_phase == "committing":
            logger.error(
                "committed event log rotation needs deferred recovery; manifest=%s",
                manifest_path,
                exc_info=True,
            )
            raise EventLogRotationRecoveryError(
                f"committed event log rotation needs deferred recovery: {manifest_path}"
            ) from exc

        try:
            _rollback_manifest(manifest_path, manifest)
        except EventLogRotationRecoveryError:
            raise
        raise


def rotate_event_log(
    path: Path,
    *,
    max_bytes: int,
    backups: int,
    timeout: float = _DEFAULT_TIMEOUT,
    stale_after: float = _DEFAULT_STALE_AFTER,
) -> bool:
    """Lock, re-check, and rotate the active event log if it is over threshold."""
    with eventlog_file_lock(path, timeout=timeout, stale_after=stale_after):
        return rotate_event_log_locked(path, max_bytes=max_bytes, backups=backups)
