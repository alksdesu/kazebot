"""Signal → JSONL file bridge.

Writes selected signals as JSON lines to <workspace>/data/signals.jsonl, with
full-content payload fields (tool arguments, results, message text) redacted.
Zero dependency on supervisor — works purely within the engine process.

Usage:
    from engine.signals.bridge import install_event_bridge
    install_event_bridge(bus)  # idempotent, call once at startup
"""

from __future__ import annotations

import atexit
import fnmatch
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, append_lines, rotate_event_log
from engine.signals.types import Signal
from engine.signals.bus import SignalBus
from workspace import resolve_workspace_root

log = logging.getLogger(__name__)

# 没配 exclude_patterns 时也不落的事件：高频流式增量，以及整段带着工具参数、工具结果、回复正文的事件。
DEFAULT_BRIDGE_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "stream_delta",
    "tool_call_delta",
    "tool_call_start",
    "tool_call_end",
    "stream_text_final",
    "intermediate_reply",
)
# 落盘前换成占位的 payload 字段：这个文件给监控用，不该成为工具参数、密钥和对话内容的又一份副本。
DEFAULT_REDACTED_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "arguments", "args_summary", "result", "raw_inline", "content", "text", "attachments",
})

_bridge_installed = False
_SIGNALS_LOG: Optional[Path] = None

# 在线轮转：这个文件此前只在离线的 data_cleanup 脚本里轮转，而那个脚本在 Windows 上
# 压根不跑 —— 于是 signals.jsonl 无界增长。signals 是高频写入，每条都 stat 一次不值。
_ROTATE_CHECK_EVERY = 512
_writes_since_rotate_check = 0

# 只有磁盘卡住时才会堆积：轮转锁最长等 30 秒，峰值每秒几十条，留一个数量级的余量，占内存几 MB
_QUEUE_LIMIT = 10_000
_DROP_REPORT_INTERVAL_SEC = 60.0
_EXIT_FLUSH_SEC = 2.0


@dataclass(frozen=True)
class _Append:
    path: Path
    name: str
    line: bytes


@dataclass(frozen=True)
class _Rotate:
    path: Path


_Job = Union[_Append, _Rotate, threading.Event]


def _rotate(path: Path, failure: str) -> None:
    if SIGNALS_MAX_BYTES <= 0 or SIGNALS_BACKUPS <= 0:
        return
    try:
        rotate_event_log(path, max_bytes=SIGNALS_MAX_BYTES, backups=SIGNALS_BACKUPS)
    except Exception:
        # 轮转失败只是文件继续变大，不能让它打断信号写入。
        log.warning(failure, exc_info=True)


def _maybe_rotate(path: Path) -> None:
    """每 N 次写入检查一次体积。只在写线程里调。"""
    global _writes_since_rotate_check
    _writes_since_rotate_check += 1
    if _writes_since_rotate_check < _ROTATE_CHECK_EVERY:
        return
    _writes_since_rotate_check = 0
    _rotate(path, "signals log rotation failed")


def _write(job: _Append) -> None:
    try:
        append_lines(job.path, job.line)
    except Exception:
        log.debug("Failed to write signal %s to log", job.name, exc_info=True)
        return
    _maybe_rotate(job.path)


class _SignalWriter:
    """按提交顺序追加信号行的后台线程，轮转也在这里做。

    发信号的一方只排队：写盘慢、轮转等锁都拖不住它。队列满了丢掉新来的，隔一段时间报一次丢了多少条。
    """

    def __init__(self, limit: int = _QUEUE_LIMIT) -> None:
        self.limit = limit
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=limit)
        self._guard = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._last_drop_report = float("-inf")

    def submit(self, job: _Job) -> None:
        self._ensure_running()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._guard:
                self._dropped += 1
            self.report_drops()

    def flush(self, timeout: float) -> bool:
        """阻塞等到此刻之前交上来的都处理完；到点还没完返回 False。"""
        with self._guard:
            if self._thread is None:
                return True
        self._ensure_running()
        done = threading.Event()
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            self._queue.put(done, timeout=max(0.0, timeout))
        except queue.Full:
            return False
        return done.wait(max(0.0, deadline - time.monotonic()))

    def report_drops(self, *, force: bool = False) -> None:
        """有没报过的丢弃、且离上次报已经过了间隔（force 时不看间隔），记一条 WARNING。"""
        now = time.monotonic()
        with self._guard:
            if not self._dropped:
                return
            if not force and now - self._last_drop_report < _DROP_REPORT_INTERVAL_SEC:
                return
            dropped, self._dropped = self._dropped, 0
            self._last_drop_report = now
        log.warning("signals log queue is full (%d waiting): dropped %d signal(s)", self.limit, dropped)

    def _ensure_running(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="signals-writer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if isinstance(job, threading.Event):
                job.set()
            elif isinstance(job, _Rotate):
                _rotate(job.path, "signals log rotation at startup failed")
            else:
                _write(job)
            self.report_drops()


_writer = _SignalWriter()


def flush_signal_writes(timeout: float = 5.0) -> bool:
    """阻塞等排着的信号都写进文件，到点没写完返回 False。给测试和进程退出用，别在事件循环上调。"""
    return _writer.flush(timeout)


def _flush_at_exit() -> None:
    if not _writer.flush(_EXIT_FLUSH_SEC):
        log.warning("signals log: queued signals were still unwritten %.1fs after exit began", _EXIT_FLUSH_SEC)
    _writer.report_drops(force=True)


atexit.register(_flush_at_exit)


def _redacted(value: object) -> object:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, str):
        return f"[redacted {len(value)} chars]"
    if isinstance(value, (list, tuple)):
        return f"[redacted {len(value)} items]"
    return "[redacted]"


def _redacted_payload(payload: dict) -> dict:
    if not _redacted_keys:
        return payload
    return {key: _redacted(value) if key in _redacted_keys else value for key, value in payload.items()}


def _bridge_handler(signal: Signal) -> None:
    """Queue a selected signal as a JSON line for the writer thread."""
    path = _SIGNALS_LOG
    if path is None:
        return
    # Apply the optional allowlist first, then the denylist.  A denylist lets
    # deployments suppress known high-volume signals while retaining new signal
    # types by default.
    if _bridge_patterns is not None and not any(
        fnmatch.fnmatch(signal.name, pattern) for pattern in _bridge_patterns
    ):
        return
    if _bridge_exclude_patterns and any(
        fnmatch.fnmatch(signal.name, pattern) for pattern in _bridge_exclude_patterns
    ):
        return
    entry = {
        "ts": datetime.fromtimestamp(signal.ts, tz=timezone.utc).isoformat(),
        "name": signal.name,
        "payload": _redacted_payload(signal.payload) if isinstance(signal.payload, dict) else signal.payload,
    }
    if signal.trace_id:
        entry["trace_id"] = signal.trace_id
    if signal.span_id:
        entry["span_id"] = signal.span_id
    try:
        # 在发信号的线程里编码：payload 里的对象之后可能还会被改，写线程拿到的得是这一刻的样子
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    except Exception:
        log.debug("Failed to encode signal %s for the log", signal.name, exc_info=True)
        return
    _writer.submit(_Append(path, signal.name, line))


# Lock for idempotent install check
_install_lock = threading.Lock()
# Glob patterns for filtering which signals to bridge (None = all).
_bridge_patterns: Optional[list[str]] = None
# Glob patterns excluded after the optional allowlist is applied.
_bridge_exclude_patterns: Optional[list[str]] = None
# Top-level payload keys replaced by a placeholder before writing.
_redacted_keys: frozenset[str] = DEFAULT_REDACTED_PAYLOAD_KEYS


def install_event_bridge(
    bus: SignalBus,
    log_dir: Optional[Path] = None,
    patterns: Optional[list[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
    redact_keys: Optional[Iterable[str]] = None,
) -> None:
    """Install the JSONL file bridge on a SignalBus.

    Idempotent: multiple calls only register the handler once.

    Args:
        bus: The SignalBus instance to bridge.
        log_dir: Directory for signals.jsonl. Defaults to the workspace's data/.
        patterns: Optional allowlist of glob patterns. If None, all signals are eligible.
        exclude_patterns: Optional denylist applied after ``patterns``.
            None means DEFAULT_BRIDGE_EXCLUDE_PATTERNS.
        redact_keys: Top-level payload keys to redact. None means
            DEFAULT_REDACTED_PAYLOAD_KEYS; an empty iterable disables redaction.
    """
    global _bridge_installed, _SIGNALS_LOG, _bridge_patterns, _bridge_exclude_patterns, _redacted_keys
    with _install_lock:
        if _bridge_installed:
            log.debug("Signal bridge already installed, skipping")
            return
        if log_dir is None:
            # 多实例部署里代码目录和工作区是分开的，信号文件得落在本实例自己的 data/ 下
            log_dir = resolve_workspace_root(Path(__file__).resolve().parent.parent.parent) / "data"
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        _SIGNALS_LOG = log_dir / "signals.jsonl"
        _bridge_patterns = patterns
        _bridge_exclude_patterns = (
            list(DEFAULT_BRIDGE_EXCLUDE_PATTERNS) if exclude_patterns is None else exclude_patterns
        )
        _redacted_keys = DEFAULT_REDACTED_PAYLOAD_KEYS if redact_keys is None else frozenset(redact_keys)
        _bridge_installed = True
        # 启动时先补一次：进程重启前积累的体积不该等到再写 512 条才被发现。排在本次所有写入之前。
        _writer.submit(_Rotate(_SIGNALS_LOG))
        bus.subscribe("*", _bridge_handler)
        log.info(
            "Signal → JSONL bridge installed: %s (patterns=%s, exclude_patterns=%s)",
            _SIGNALS_LOG,
            patterns,
            _bridge_exclude_patterns,
        )
