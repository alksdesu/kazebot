"""委派句柄：子任务的 task id 加上它的对话所在的会话，写成 task_id@session_id。

委派结果都带上句柄；父节点委派时用 continue_from 交回来，就接着那个子 agent 的历史往下做。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HANDLE_SEPARATOR = "@"
_TASK_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}\Z")
_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.:-]{0,199}\Z")


@dataclass(frozen=True)
class DispatchHandle:
    task_id: str
    session_id: str

    def __str__(self) -> str:
        return f"{self.task_id}{HANDLE_SEPARATOR}{self.session_id}"


def format_handle(task_id: Any, session_id: Any) -> str:
    """拼出句柄；缺一段或含不该有的字符时给空串。"""
    handle = _validated(str(task_id or "").strip(), str(session_id or "").strip())
    return str(handle) if handle is not None else ""


def parse_handle(text: Any) -> DispatchHandle | None:
    if not isinstance(text, str):
        return None
    task_id, separator, session_id = text.strip().partition(HANDLE_SEPARATOR)
    if not separator:
        return None
    return _validated(task_id.strip(), session_id.strip())


def _validated(task_id: str, session_id: str) -> DispatchHandle | None:
    if not _TASK_ID.match(task_id) or not _SESSION_ID.match(session_id):
        return None
    return DispatchHandle(task_id=task_id, session_id=session_id)
