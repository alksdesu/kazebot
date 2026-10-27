"""异步工具结果回传 supervisor：瞬时失败退避重试，重试完仍送不到就落盘，worker 循环里定期补投。"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from file_lock import try_exclusive_lock

from ..tool_step import TOOL_ERROR, TOOL_SUCCESS

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SEC = (1.0, 4.0, 15.0)
_TRANSIENT_STATUS = frozenset({408, 425, 429})
# 按任务交结果的接口不会因为业务原因回这两个码：回了就是 supervisor 还没有这个接口
_MISSING_ROUTE_STATUS = frozenset({404, 405})
_OUTBOX_MAX_AGE_SEC = 24 * 3600
_FLUSH_LOCK_NAME = ".flush.lock"
# 文件名按它排序补投；同一时钟刻度里写下的几条靠序号保住先后（Windows 上 time_ns 的刻度有十几毫秒）
_OUTBOX_SEQUENCE = itertools.count()

_DELIVERED = "delivered"
_TRANSIENT = "transient"
_REJECTED = "rejected"


def build_async_result_payload(
    *,
    message: str,
    tool_name: str,
    task_id: str,
    node_id: str,
    failure_error: str = "",
    attachments: list[str] | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """所有异步路径共用的结果请求体。cancelled：工具是因为发起任务被取消才停的，supervisor 不再转交。"""
    payload: dict[str, Any] = {
        "message": str(message or ""),
        "tool_name": str(tool_name or ""),
        "task_id": str(task_id or ""),
        "node_id": str(node_id or ""),
        "success": not bool(failure_error),
    }
    if failure_error:
        payload["error"] = str(failure_error)
    if cancelled:
        payload["cancelled"] = True
    if attachments:
        payload["attachment_paths"] = list(attachments)
    return payload


def async_result_tool_status(task_context: Any) -> str:
    """回传的异步结果在新一轮里是一条输入消息，它的 tool_status；不是异步结果回传的任务返回空串。

    supervisor 只把请求体里的 success 换成 route_hints.async_tool_success 带过来，取消和报错分不开，都记 error。
    """
    hints = task_context.get("route_hints") if isinstance(task_context, dict) else None
    success = hints.get("async_tool_success") if isinstance(hints, dict) else None
    if not isinstance(success, bool):
        return ""
    return TOOL_SUCCESS if success else TOOL_ERROR


def outbox_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "data" / "async_tool_outbox"


def _results_url(supervisor_url: str) -> str:
    return f"{supervisor_url}/v1/async_tools/results"


def _session_result_url(supervisor_url: str, session_id: str) -> str:
    """按会话交结果的旧接口，给还没有按任务交结果接口的 supervisor。"""
    return f"{supervisor_url}/v1/sessions/{session_id}/async_tool_result"


async def _post_once(http: Any, url: str, payload: dict[str, Any]) -> tuple[str, str, int | None]:
    try:
        response = await http.post(url, json=payload)
    except Exception as error:
        return _TRANSIENT, f"{type(error).__name__}: {error}", None
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or 200 <= status < 300:
        return _DELIVERED, "", status if isinstance(status, int) else None
    if status >= 500 or status in _TRANSIENT_STATUS:
        return _TRANSIENT, f"HTTP {status}", status
    return _REJECTED, f"HTTP {status}", status


async def _post_result(http: Any, supervisor_url: str, session_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    """交给按任务交结果的接口，落点由 supervisor 按发起任务定；它还没有这个接口时退回按会话交。"""
    outcome, detail, status = await _post_once(http, _results_url(supervisor_url), {**payload, "session_id": session_id})
    if status in _MISSING_ROUTE_STATUS:
        outcome, detail, _status = await _post_once(http, _session_result_url(supervisor_url, session_id), payload)
    return outcome, detail


def _write_outbox(workspace_root: Path, session_id: str, payload: dict[str, Any]) -> Path:
    directory = outbox_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    record = {"session_id": session_id, "payload": payload, "queued_at": time.time()}
    target = directory / f"{time.time_ns():020d}-{next(_OUTBOX_SEQUENCE):08d}-{uuid.uuid4().hex[:8]}.json"
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


async def deliver_async_result(
    http: Any,
    supervisor_url: str,
    session_id: str,
    payload: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
) -> bool:
    """送达返回 True。被拒绝、或重试完仍送不到而转进待补投目录时返回 False。"""
    tool_name = payload.get("tool_name", "")
    detail = ""
    for delay in (0.0, *_RETRY_DELAYS_SEC):
        if delay:
            await asyncio.sleep(delay)
        outcome, detail = await _post_result(http, supervisor_url, session_id, payload)
        if outcome == _DELIVERED:
            return True
        if outcome == _REJECTED:
            logger.error("async tool result rejected: session=%s tool=%s (%s)", session_id, tool_name, detail)
            return False
    if workspace_root is None:
        logger.error("async tool result undeliverable, dropped: session=%s tool=%s (%s)", session_id, tool_name, detail)
        return False
    try:
        queued = await asyncio.to_thread(_write_outbox, Path(workspace_root), session_id, payload)
    except Exception:
        logger.exception("async tool result undeliverable and could not be queued: session=%s tool=%s", session_id, tool_name)
        return False
    logger.error(
        "async tool result undeliverable, queued for redelivery: %s session=%s tool=%s (%s)",
        queued.name, session_id, tool_name, detail,
    )
    return False


def _pending_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def _read_outbox(path: Path) -> dict[str, Any] | None:
    """读一条待补投记录；文件已不在返回 None，内容坏了返回空 dict。"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict) or not record.get("session_id"):
        return {}
    return record


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


async def flush_async_result_outbox(http: Any, supervisor_url: str, workspace_root: Path | str) -> int:
    """补投落盘的结果，返回送达条数。supervisor 仍连不上就停下等下一轮。"""
    directory = outbox_dir(workspace_root)
    if not directory.is_dir():
        return 0
    delivered = 0
    # 同一工作区的几个 worker 只能有一个在补投，否则同一条结果会被送两次
    with try_exclusive_lock(directory / _FLUSH_LOCK_NAME) as acquired:
        if not acquired:
            return 0
        for path in await asyncio.to_thread(_pending_files, directory):
            record = await asyncio.to_thread(_read_outbox, path)
            if record is None:
                continue
            if not record:
                logger.error("async tool outbox entry unreadable, dropped: %s", path.name)
                await asyncio.to_thread(_remove, path)
                continue
            payload = record["payload"]
            session_id = str(record["session_id"])
            queued_at = record.get("queued_at")
            if not isinstance(queued_at, (int, float)) or time.time() - queued_at > _OUTBOX_MAX_AGE_SEC:
                logger.error(
                    "async tool result expired before redelivery, dropped: session=%s tool=%s",
                    session_id, payload.get("tool_name", ""),
                )
                await asyncio.to_thread(_remove, path)
                continue
            outcome, detail = await _post_result(http, supervisor_url, session_id, payload)
            if outcome == _TRANSIENT:
                break
            if outcome == _REJECTED:
                logger.error(
                    "async tool result rejected on redelivery, dropped: session=%s tool=%s (%s)",
                    session_id, payload.get("tool_name", ""), detail,
                )
            else:
                delivered += 1
            await asyncio.to_thread(_remove, path)
    if delivered:
        logger.warning("redelivered %d queued async tool result(s)", delivered)
    return delivered
