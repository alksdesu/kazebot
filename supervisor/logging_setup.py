"""supervisor 进程的日志出口：标准 logging、uvicorn、warnings 和线程里没接住的异常都写进 data/logs/supervisor.log。

终端留给前台的 TUI；没有 handler 的 logger 会落到 lastResort，不带时间和来源地打到 stderr，INFO 整条丢。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from types import TracebackType

from uvicorn.config import LOG_LEVELS

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_LEVEL = logging.INFO

# 每条任务都有几行带结构化字段的 INFO，不轮转的话一个月能长到几百 MB。
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
MAX_BYTES_ENV = "CLONOTH_SUPERVISOR_LOG_MAX_BYTES"
BACKUP_COUNT_ENV = "CLONOTH_SUPERVISOR_LOG_BACKUPS"

# 每发一个请求就记一条 INFO 的第三方库，平时压到 WARNING，调到 debug 时放开。
CHATTY_LIBRARIES = ("httpx", "httpcore")

UNCAUGHT_LOGGER = "supervisor.uncaught"

_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
# uvicorn 给彩色终端另备的一份消息，和正文重复。
_PRESENTATION_FIELDS = frozenset({"color_message"})

_installed: logging.Handler | None = None


def _render(value: object) -> str:
    if isinstance(value, str) and value and not any(char.isspace() for char in value):
        return value
    return repr(value)


class KeyValueFormatter(logging.Formatter):
    """消息后面带上 extra= 传进来的字段。

    路由、会话几处把任务号、会话号都放在 extra 里，只印消息的话一行只剩「completed task routed」。
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        fields = [
            f"{key}={_render(value)}"
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and key not in _PRESENTATION_FIELDS and not key.startswith("_")
        ]
        return f"{line} {' '.join(fields)}" if fields else line


def backup_name(default_name: str) -> str:
    """supervisor.log.1 改叫 supervisor.1.log：控制台的日志页和定期清理都只认 .log 结尾。"""
    rotated = Path(default_name)
    original = rotated.with_suffix("")
    return str(original.with_name(f"{original.stem}{rotated.suffix}{original.suffix}"))


class SupervisorLogHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, path: Path, *, max_bytes: int, backup_count: int) -> None:
        super().__init__(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        self.namer = backup_name


def _env_count(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def parse_level(name: str | int) -> int | None:
    """按 uvicorn 的 --log-level 认级别名，两边用同一套；认不出返回 None。"""
    if isinstance(name, int):
        return name
    return LOG_LEVELS.get(str(name).strip().lower())


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    # 和默认的一样：线程里 sys.exit() 不算出错。
    if issubclass(args.exc_type, SystemExit):
        return
    thread = args.thread.name if args.thread is not None else "?"
    logging.getLogger(UNCAUGHT_LOGGER).error(
        "线程 %s 里有异常没接住", thread,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _log_fatal_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    if not issubclass(exc_type, KeyboardInterrupt):
        logging.getLogger(UNCAUGHT_LOGGER).critical(
            "主线程有异常没接住，supervisor 退出", exc_info=(exc_type, exc_value, exc_traceback),
        )
    # 进程要退了，终端和 systemd 日志里也照常留一份。
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def configure_supervisor_logging(log_path: Path, level: str | int = DEFAULT_LEVEL) -> int:
    """把整个进程的日志接到 log_path，返回实际用的级别，uvicorn 要用同一个。

    再调一次会换掉上次装的 handler，不会叠出两份。
    """
    global _installed
    resolved = parse_level(level)
    effective = DEFAULT_LEVEL if resolved is None else resolved

    handler = SupervisorLogHandler(
        Path(log_path),
        max_bytes=_env_count(MAX_BYTES_ENV, DEFAULT_MAX_BYTES),
        backup_count=_env_count(BACKUP_COUNT_ENV, DEFAULT_BACKUP_COUNT),
    )
    handler.setFormatter(KeyValueFormatter(LOG_FORMAT))

    root = logging.getLogger()
    if _installed is not None:
        root.removeHandler(_installed)
        _installed.close()
    root.addHandler(handler)
    root.setLevel(effective)
    _installed = handler

    for name in CHATTY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING if effective >= logging.INFO else logging.NOTSET)

    logging.captureWarnings(True)
    threading.excepthook = _log_thread_exception
    sys.excepthook = _log_fatal_exception

    if resolved is None:
        logging.getLogger(__name__).warning("日志级别 %r 认不出，按 %s 记", level, logging.getLevelName(effective))
    return effective
