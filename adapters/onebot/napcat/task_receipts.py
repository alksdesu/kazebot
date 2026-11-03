"""把平台发送结果按任务归属交回 supervisor。"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..send_contract import classify_send_exception, protected_claim_send
from . import state

log = logging.getLogger(__name__)


async def report(context: Any, target: dict, kind: str, key: str, status: str, count: int = 1) -> None:
    client = state._adapter.client
    task_id = getattr(context, "task_id", "")
    if not task_id or client is None or not hasattr(client, "report_task_delivery"):
        return
    destination = target.get("type")
    if destination not in ("private", "group"):
        return
    raw_target = str(target.get("group_id") if destination == "group" else target.get("user_id"))
    body = {
        "receipt_id": hashlib.sha256(key.encode()).hexdigest(),
        "kind": kind, "status": status, "count": count, "destination": destination,
        "destination_scope": hashlib.sha256(f"qq:{destination}:{raw_target}".encode()).hexdigest(),
    }
    try:
        await client.report_task_delivery(task_id, body)
    except Exception:
        # 回执丢失只能显示未知，不能因此重复发送已经交付的内容。
        log.warning("task delivery receipt not recorded: task=%s status=%s", task_id, status)


async def claimed_send(guard: Any, claim: Any, send: Any, *, context: Any, target: dict,
                       kind: str, count: int = 1, **kwargs: Any) -> Any:
    await report(context, target, kind, claim.key, "pending", count)
    try:
        result = await protected_claim_send(guard, claim, send, **kwargs)
    except Exception as exc:
        failure = classify_send_exception(exc)
        status = "unknown" if failure.ambiguous_ack or not failure.definitely_not_sent else "failed"
        await report(context, target, kind, claim.key, status, count)
        raise
    await report(context, target, kind, claim.key, "sent", count)
    return result
