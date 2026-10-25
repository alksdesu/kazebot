"""Signal → JSONL file bridge.

Writes selected signals as JSON lines to <workspace>/data/signals.jsonl, with
full-content payload fields (tool arguments, results, message text) redacted.
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
from typing import Iterable, Optional

from engine.eventlog_rotation import SIGNALS_BACKUPS, SIGNALS_MAX_BYTES, rotate_event_log
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
        "payload": _redacted_payload(signal.payload) if isinstance(signal.payload, dict) else signal.payload,
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
            _bridge_exclude_patterns,
        )
