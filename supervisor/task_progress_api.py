"""任务自己的进度查询与适配器投递回执。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from clonoth_sdk.task_status import MAX_STATUS_TASKS

from . import task_progress
from .admin_api import verify_admin_token


class StatusQuery(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=MAX_STATUS_TASKS)


class StatusCheck(BaseModel):
    query_id: str = Field(min_length=1, max_length=64)


class DeliveryReceipt(BaseModel):
    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["file", "message"]
    status: Literal["pending", "sent", "failed", "unknown"]
    count: StrictInt = Field(ge=1, le=1000)
    destination: Literal["private", "group"]
    destination_scope: str = Field(pattern=r"^[0-9a-f]{64}$")


def create_task_progress_router() -> APIRouter:
    router = APIRouter(prefix="/v1/tasks", dependencies=[Depends(verify_admin_token)])

    @router.post("/{task_id}/family/status")
    def query(task_id: str, body: StatusQuery, request: Request) -> dict:
        state = request.app.state.state
        with state._lock:
            caller = state.tasks.get(task_id)
            if caller is None or state._task_terminal(caller):
                raise HTTPException(404, "调用方任务不存在或已结束")
            if any(not tid or len(tid) > 128 for tid in body.task_ids):
                raise HTTPException(422, "任务编号无效")
            return task_progress.snapshot_locked(state, caller, list(dict.fromkeys(body.task_ids)))

    @router.post("/{task_id}/family/status/check")
    def check(task_id: str, body: StatusCheck, request: Request) -> dict:
        state = request.app.state.state
        with state._lock:
            caller = state.tasks.get(task_id)
            result = task_progress.check_locked(state, caller, body.query_id) if caller is not None else None
            if result is None:
                raise HTTPException(404, "查询记录不存在，请重新查询")
            return result

    @router.post("/{task_id}/deliveries")
    def delivery(task_id: str, body: DeliveryReceipt, request: Request) -> dict:
        state = request.app.state.state
        with state._lock:
            task = state.tasks.get(task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            task_progress.record_delivery_locked(state, task, body.model_dump())
            return {"ok": True}

    return router
