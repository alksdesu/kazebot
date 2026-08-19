"""Signal → JSONL file bridge.

Writes selected signals as JSON lines to data/signals.jsonl.
Zero dependency on supervisor — works purely within the engine process.

Usage:
    from engine.signals.bridge import install_event_bridge
    install_event_bridge(bus)  # idempotent, call once at startup
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log
from engine.signals.types import Signal
from engine.signals.bus import SignalBus

log = logging.getLogger(__name__)

_bridge_installed = False
_SIGNALS_LOG: Optional[Path] = None
_write_lock = threading.Lock()

# 在线轮转：这个文件此前只在离线的 data_cleanup 脚本里轮转，而那个脚本在 Windows 上
# 压根不跑 —— 于是 signals.jsonl 无界增长。signals 是高频写入，每条都 stat 一次不值。
_ROTATE_CHECK_EVERY = 512
_writes_since_rotate_check = 0


def _maybe_rotate_locked() -> None:
    """每 N 次写入检查一次体积。调用方必须已持有 _write_lock。"""
    global _writes_since_rotate_check
    _writes_since_rotate_check += 1
    if _writes_since_rotate_check < _ROTATE_CHECK_EVERY:
        return
    _writes_since_rotate_check = 0
    if _SIGNALS_LOG is None or SIGNALS_MAX_BYTES <= 0 or SIGNALS_BACKUPS <= 0:
        return
    try:
        rotate_event_log(_SIGNALS_LOG, max_bytes=SIGNALS_MAX_BYTES, backups=SIGNALS_BACKUPS)
    except Exception:
        # 轮转失败只是文件继续变大，不能让它打断信号写入。
        log.warning("signals log rotation failed", exc_info=True)


def _bridge_handler(signal: Signal) -> None:
    """Append a selected signal as a JSON line to the signals log file."""
    if _SIGNALS_LOG is None:
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
        "payload": signal.payload,
    }
    if signal.trace_id:
        entry["trace_id"] = signal.trace_id
    if signal.span_id:
        entry["span_id"] = signal.span_id
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _write_lock:
            with _SIGNALS_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_rotate_locked()
    except Exception:
        log.debug("Failed to write signal %s to log", signal.name, exc_info=True)


# Lock for idempotent install check
_install_lock = threading.Lock()
# Glob patterns for filtering which signals to bridge (None = all).
_bridge_patterns: Optional[list[str]] = None
# Glob patterns excluded after the optional allowlist is applied.
_bridge_exclude_patterns: Optional[list[str]] = None


def install_event_bridge(
    bus: SignalBus,
    log_dir: Optional[Path] = None,
    patterns: Optional[list[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
) -> None:
    """Install the JSONL file bridge on a SignalBus.

    Idempotent: multiple calls only register the handler once.

    Args:
        bus: The SignalBus instance to bridge.
        log_dir: Directory for signals.jsonl. Defaults to data/ relative to workspace root.
        patterns: Optional allowlist of glob patterns. If None, all signals are eligible.
        exclude_patterns: Optional denylist applied after ``patterns``.
    """
    global _bridge_installed, _SIGNALS_LOG, _bridge_patterns, _bridge_exclude_patterns
    with _install_lock:
        if _bridge_installed:
            log.debug("Signal bridge already installed, skipping")
            return
        if log_dir is None:
            log_dir = Path(__file__).resolve().parent.parent.parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        _SIGNALS_LOG = log_dir / "signals.jsonl"
        _bridge_patterns = patterns
        _bridge_exclude_patterns = exclude_patterns
        _bridge_installed = True
        # 启动时先补一次：进程重启前积累的体积不该等到再写 512 条才被发现。
        if SIGNALS_MAX_BYTES > 0 and SIGNALS_BACKUPS > 0:
            try:
                rotate_event_log(_SIGNALS_LOG, max_bytes=SIGNALS_MAX_BYTES, backups=SIGNALS_BACKUPS)
            except Exception:
                log.warning("signals log rotation at startup failed", exc_info=True)
        bus.subscribe("*", _bridge_handler)
        log.info(
            "Signal → JSONL bridge installed: %s (patterns=%s, exclude_patterns=%s)",
            _SIGNALS_LOG,
            patterns,
            exclude_patterns,
        )
