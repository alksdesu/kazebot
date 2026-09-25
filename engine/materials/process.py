from __future__ import annotations

import json
import os
import subprocess
import sys
import signal
import threading
from pathlib import Path

from .storage import MaterialError

_lock = threading.Lock()
_workers: dict[str, set[subprocess.Popen]] = {}
_stopping: set[str] = set()


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def cancel_workers(owner: str) -> None:
    with _lock:
        _stopping.add(owner)
        processes = list(_workers.get(owner, set()))
    for process in processes:
        _terminate(process)


def release_owner(owner: str) -> None:
    with _lock:
        if not _workers.get(owner):
            _stopping.discard(owner)


def run_worker(workspace: Path, operation: str, *, owner: str = "", **data) -> dict:
    configured = os.environ.get("CLONOTH_MATERIALS_PYTHON", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([workspace / ".venv" / "Scripts" / "python.exe", workspace / ".venv" / "bin" / "python", Path(sys.executable)])
    python = next((str(path) for path in candidates if path.is_file()), sys.executable)
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    } or key.startswith("CLONOTH_MATERIALS_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["PYTHONIOENCODING"] = "utf-8"
    with _lock:
        if owner and owner in _stopping:
            raise MaterialError("资料处理已中断。", "interrupted", 409)
    process = subprocess.Popen(
            [python, "-m", "engine.materials.worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    with _lock:
        _workers.setdefault(owner, set()).add(process)
        stopping = owner and owner in _stopping
    if stopping:
        _terminate(process)
    try:
        output, _stderr = process.communicate(json.dumps({"operation": operation, **data}, ensure_ascii=False).encode("utf-8"), timeout=180)
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        process.communicate(timeout=10)
        raise MaterialError("资料处理超时，请拆分资料后重试。", "processing_timeout", 504) from exc
    finally:
        with _lock:
            _workers.get(owner, set()).discard(process)
            if not _workers.get(owner):
                _workers.pop(owner, None)
    with _lock:
        if owner and owner in _stopping:
            raise MaterialError("资料处理已中断。", "interrupted", 409)
    if len(output) > 16 * 1024 * 1024:
        raise MaterialError("资料解析结果过大。", "limit_exceeded", 413)
    try:
        response = json.loads(output.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MaterialError("资料处理进程未返回有效结果。", "worker_failed", 502) from exc
    if not response.get("ok"):
        raise MaterialError(response.get("error", "资料处理失败。"), response.get("code", "processing_failed"), int(response.get("status", 422)))
    return response["data"]
