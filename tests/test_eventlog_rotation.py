from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import engine.eventlog_rotation as rotation_module
from engine.eventlog_rotation import (
    EventLogRotationRecoveryError,
    _pid_is_alive,
    eventlog_file_lock,
    eventlog_lock_path,
    rotate_event_log,
    rotate_event_log_locked,
)
from supervisor import eventlog as eventlog_module
from supervisor.eventlog import EventLog


def _persisted_rows(path: Path, backups: int) -> list[dict]:
    rows: list[dict] = []
    paths = [path.with_name(f"{path.name}.{i}") for i in range(backups, 0, -1)]
    paths.append(path)
    for candidate in paths:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            rows.append(json.loads(line))
    return rows


def test_threaded_online_and_external_rotation_preserve_every_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    backups = 32
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_MAX_BYTES", 1)
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", backups)
    monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_CHECK_EVERY", 1)
    log = EventLog(path, run_id="thread-test")
    barrier = threading.Barrier(2)

    def append_rows() -> None:
        barrier.wait()
        for value in range(1, 21):
            log.append(
                session_id="rotation",
                component="test",
                type_="probe",
                payload={"value": value},
            )

    def rotate_rows() -> None:
        barrier.wait()
        for _ in range(20):
            rotate_event_log(path, max_bytes=1, backups=backups)

    threads = [threading.Thread(target=append_rows), threading.Thread(target=rotate_rows)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    retained = _persisted_rows(path, backups)
    assert [row["seq"] for row in retained] == list(range(1, 21))
    assert [row["payload"]["value"] for row in retained] == list(range(1, 21))

    continued = log.append(
        session_id="rotation", component="test", type_="probe", payload={"value": 21}
    )
    assert continued["seq"] == 21
    assert _persisted_rows(path, backups)[-1]["payload"]["value"] == 21


def test_process_rotate_and_append_are_serialized_and_ordered(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    count = 24
    backups = 32
    go = tmp_path / "barrier.go"
    writer_ready = tmp_path / "writer.ready"
    rotator_ready = tmp_path / "rotator.ready"
    writer_code = textwrap.dedent(
        """
        import json, sys, time
        from pathlib import Path
        from engine.eventlog_rotation import eventlog_file_lock
        path, ready, go, count = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
        ready.touch()
        while not go.exists(): time.sleep(0.001)
        for seq in range(1, count + 1):
            row = {"seq": seq, "payload": {"value": f"process-{seq}"}}
            with eventlog_file_lock(path):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\\n")
            if seq % 4 == 0: time.sleep(0.001)
        """
    )
    rotator_code = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from engine.eventlog_rotation import rotate_event_log
        path, ready, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
        attempts, backups = int(sys.argv[4]), int(sys.argv[5])
        ready.touch()
        while not go.exists(): time.sleep(0.001)
        for _ in range(attempts):
            rotate_event_log(path, max_bytes=1, backups=backups)
            time.sleep(0.001)
        """
    )
    writer = subprocess.Popen(
        [sys.executable, "-c", writer_code, str(path), str(writer_ready), str(go), str(count)],
        cwd=Path(__file__).resolve().parents[1],
    )
    rotator = subprocess.Popen(
        [
            sys.executable, "-c", rotator_code, str(path), str(rotator_ready), str(go),
            str(count), str(backups),
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    deadline = time.monotonic() + 10
    while not (writer_ready.exists() and rotator_ready.exists()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    go.touch()
    assert writer.wait(timeout=30) == 0
    assert rotator.wait(timeout=30) == 0

    retained = _persisted_rows(path, backups)
    assert [row["seq"] for row in retained] == list(range(1, count + 1))
    assert [row["payload"]["value"] for row in retained] == [
        f"process-{seq}" for seq in range(1, count + 1)
    ]

    # A final rotation may leave no active file.  The same append protocol must
    # recreate it without disturbing the valid oldest-to-newest backup sequence.
    with eventlog_file_lock(path):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"seq": count + 1, "payload": {"value": "continued"}}) + "\n")
    assert _persisted_rows(path, backups)[-1]["seq"] == count + 1


def test_process_rotate_with_eventlog_init_and_iter_keeps_recovery_consistent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    initial = 5
    additions = 20
    backups = 64
    with path.open("w", encoding="utf-8") as stream:
        for seq in range(1, initial + 1):
            stream.write(json.dumps({"seq": seq, "payload": {"value": seq}}) + "\n")

    go = tmp_path / "scan-barrier.go"
    rotate_ready = tmp_path / "rotate.ready"
    scan_ready = tmp_path / "scan.ready"
    done = tmp_path / "rotate.done"
    result_path = tmp_path / "scan-result.json"
    rotate_code = textwrap.dedent(
        """
        import json, sys, time
        from pathlib import Path
        from engine.eventlog_rotation import eventlog_file_lock, rotate_event_log
        path, ready, go, done = map(Path, sys.argv[1:5])
        initial, additions, backups = map(int, sys.argv[5:8])
        ready.touch()
        while not go.exists(): time.sleep(0.001)
        for seq in range(initial + 1, initial + additions + 1):
            with eventlog_file_lock(path):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"seq": seq, "payload": {"value": seq}}) + "\\n")
            rotate_event_log(path, max_bytes=1, backups=backups)
        done.touch()
        """
    )
    scan_code = textwrap.dedent(
        """
        import json, sys, time
        from pathlib import Path
        from supervisor.eventlog import EventLog
        path, ready, go, done, result = map(Path, sys.argv[1:6])
        expected_next = int(sys.argv[6])
        ready.touch()
        while not go.exists(): time.sleep(0.001)
        scans = 0
        observed_max = 0
        while not done.exists():
            log = EventLog(path, run_id="scanner")
            assert log._seq >= observed_max
            observed_max = log._seq
            rows = list(log.iter_persisted_events())
            seqs = [row["seq"] for row in rows]
            assert seqs == sorted(set(seqs))
            assert log._seq <= (max(seqs) if seqs else 0)
            scans += 1
        final = EventLog(path, run_id="scanner-final")
        final_rows = list(final.iter_persisted_events())
        final_seqs = [row["seq"] for row in final_rows]
        assert final_seqs == list(range(1, expected_next))
        event = final.append(session_id="scan", component="test", type_="probe", payload={"value": expected_next})
        result.write_text(json.dumps({"scans": scans, "next": event["seq"]}), encoding="utf-8")
        """
    )
    env = os.environ.copy()
    env["CLONOTH_EVENTS_BACKUPS"] = str(backups)
    cwd = Path(__file__).resolve().parents[1]
    rotator = subprocess.Popen(
        [sys.executable, "-c", rotate_code, str(path), str(rotate_ready), str(go), str(done),
         str(initial), str(additions), str(backups)],
        cwd=cwd, env=env,
    )
    scanner = subprocess.Popen(
        [sys.executable, "-c", scan_code, str(path), str(scan_ready), str(go), str(done),
         str(result_path), str(initial + additions + 1)],
        cwd=cwd, env=env,
    )
    deadline = time.monotonic() + 10
    while not (rotate_ready.exists() and scan_ready.exists()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    go.touch()
    assert rotator.wait(timeout=30) == 0
    assert scanner.wait(timeout=30) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["scans"] > 0
    assert result["next"] == initial + additions + 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle semantics")
def test_windows_pid_probe_does_not_terminate_live_process() -> None:
    assert _pid_is_alive(os.getpid())
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert _pid_is_alive(child.pid)
        time.sleep(0.05)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert not _pid_is_alive(child.pid)


def _rotation_chain_bytes(path: Path, backups: int) -> dict[str, bytes]:
    candidates = [path] + [path.with_name(f"{path.name}.{index}") for index in range(1, backups + 1)]
    return {candidate.name: candidate.read_bytes() for candidate in candidates if candidate.exists()}


def _seed_rotation_chain(path: Path, backups: int) -> dict[str, bytes]:
    path.write_bytes(b"active\n")
    for index in range(1, backups + 1):
        path.with_name(f"{path.name}.{index}").write_bytes(f"backup-{index}\n".encode())
    return _rotation_chain_bytes(path, backups)


@pytest.mark.parametrize("failed_rename", range(1, 6))
def test_rotation_rename_failure_fully_restores_original_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_rename: int,
) -> None:
    path = tmp_path / "events.jsonl"
    before = _seed_rotation_chain(path, backups=2)
    original = rotation_module._rename_no_replace
    calls = 0

    def injected(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_rename:
            raise OSError(f"injected rename {failed_rename}")
        original(source, destination)

    monkeypatch.setattr(rotation_module, "_rename_no_replace", injected)
    with eventlog_file_lock(path):
        with pytest.raises(OSError, match="injected rename"):
            rotate_event_log_locked(path, max_bytes=1, backups=2)
    assert _rotation_chain_bytes(path, 2) == before
    assert not list(tmp_path.glob(".events.jsonl.rotate.*"))


def test_committing_oldest_unlink_failure_finishes_on_next_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    before = _seed_rotation_chain(path, backups=2)
    committed = {"events.jsonl.1": before["events.jsonl"], "events.jsonl.2": before["events.jsonl.1"]}
    original_unlink = Path.unlink
    injected = False

    def failing_unlink(candidate: Path, *args, **kwargs):
        nonlocal injected
        if not injected and candidate.name.startswith(".events.jsonl.rotate.") and candidate.name.endswith(".2"):
            injected = True
            raise OSError("injected oldest unlink")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with eventlog_file_lock(path):
        with pytest.raises(EventLogRotationRecoveryError, match="deferred recovery"):
            rotate_event_log_locked(path, max_bytes=1, backups=2)
    assert injected
    manifests = list(tmp_path.glob(".events.jsonl.rotate.manifest.*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["phase"] == "committing"

    # Recovery sees the still-present oldest staging file, discards it, and keeps
    # the already-published chain rather than attempting an impossible rollback.
    with eventlog_file_lock(path):
        pass
    assert _rotation_chain_bytes(path, 2) == committed
    assert not list(tmp_path.glob(".events.jsonl.rotate.*"))


def test_committing_manifest_unlink_failures_are_idempotent_and_block_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    before = _seed_rotation_chain(path, backups=2)
    committed = {"events.jsonl.1": before["events.jsonl"], "events.jsonl.2": before["events.jsonl.1"]}
    original_unlink = Path.unlink
    remaining_failures = 3

    def failing_manifest_unlink(candidate: Path, *args, **kwargs):
        nonlocal remaining_failures
        if ".rotate.manifest." in candidate.name and candidate.suffix == ".json" and remaining_failures:
            remaining_failures -= 1
            raise OSError("injected manifest unlink")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_manifest_unlink)
    with eventlog_file_lock(path):
        with pytest.raises(EventLogRotationRecoveryError, match="deferred recovery"):
            rotate_event_log_locked(path, max_bytes=1, backups=2)

    # The oldest staging file was already deleted. Repeated recovery failures must
    # preserve the committing manifest and refuse access without changing data.
    for _ in range(2):
        with pytest.raises(EventLogRotationRecoveryError, match="needs recovery"):
            with eventlog_file_lock(path):
                pytest.fail("access must remain blocked while manifest cleanup fails")
        assert _rotation_chain_bytes(path, 2) == committed
        manifests = list(tmp_path.glob(".events.jsonl.rotate.manifest.*.json"))
        assert len(manifests) == 1
        assert json.loads(manifests[0].read_text(encoding="utf-8"))["phase"] == "committing"

    # The same recovery path is idempotent after the transient unlink failures end.
    with eventlog_file_lock(path):
        pass
    assert remaining_failures == 0
    assert _rotation_chain_bytes(path, 2) == committed
    assert not list(tmp_path.glob(".events.jsonl.rotate.*"))


def test_failed_rollback_retains_manifest_and_next_lock_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    before = _seed_rotation_chain(path, backups=2)
    original = rotation_module._rename_no_replace
    calls = 0

    def injected(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls in {5, 6}:
            raise OSError(f"injected rename {calls}")
        original(source, destination)

    with eventlog_file_lock(path):
        monkeypatch.setattr(rotation_module, "_rename_no_replace", injected)
        with pytest.raises(EventLogRotationRecoveryError):
            rotate_event_log_locked(path, max_bytes=1, backups=2)
    assert list(tmp_path.glob(".events.jsonl.rotate.manifest.*.json"))

    monkeypatch.setattr(rotation_module, "_rename_no_replace", original)
    with eventlog_file_lock(path):
        pass
    assert _rotation_chain_bytes(path, 2) == before
    assert not list(tmp_path.glob(".events.jsonl.rotate.*"))


def test_eventlog_init_and_iter_wait_for_rotation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, run_id="seed")
    for value in range(1, 6):
        log.append(session_id="scan", component="test", type_="probe", payload={"value": value})

    staged = threading.Event()
    release = threading.Event()
    original = rotation_module._rename_no_replace
    paused = False

    def pausing_rename(source: Path, destination: Path) -> None:
        nonlocal paused
        original(source, destination)
        if not paused and source == path:
            paused = True
            staged.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(rotation_module, "_rename_no_replace", pausing_rename)
    rotate_thread = threading.Thread(
        target=lambda: rotate_event_log(path, max_bytes=1, backups=3)
    )
    rotate_thread.start()
    assert staged.wait(timeout=5)

    result: dict[str, object] = {}

    def scan() -> None:
        restarted = EventLog(path, run_id="scan")
        result["events"] = list(restarted.iter_persisted_events())
        result["next"] = restarted.append(
            session_id="scan", component="test", type_="probe", payload={"value": 6}
        )["seq"]

    scan_thread = threading.Thread(target=scan)
    scan_thread.start()
    time.sleep(0.05)
    assert scan_thread.is_alive()
    release.set()
    rotate_thread.join(timeout=5)
    scan_thread.join(timeout=5)
    assert not rotate_thread.is_alive()
    assert not scan_thread.is_alive()
    assert [event["seq"] for event in result["events"]] == list(range(1, 6))
    assert result["next"] == 6


def test_stale_owner_lock_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    lock_path = eventlog_lock_path(path)
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "pid": 2_147_483_647,
                "hostname": socket.gethostname(),
                "token": "abandoned",
                "created_at": 0,
            }
        ),
        encoding="utf-8",
    )

    with eventlog_file_lock(path, timeout=1.0, stale_after=3600):
        owner = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["token"] != "abandoned"

    assert not lock_path.exists()
