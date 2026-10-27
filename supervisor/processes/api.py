from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from supervisor.admin_api import verify_admin_token

from .service import ProcessError


class ProcessStartIn(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    ready_pattern: str = ""
    max_runtime_sec: float | None = None


class ProcessTaskIn(BaseModel):
    task_id: str = Field(default="", max_length=128)


def _call(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except ProcessError as exc:
        raise HTTPException(exc.status, exc.message) from exc


def create_router(state: Any) -> APIRouter:
    """engine 的工具和控制台用：带 task_id 的只看得到那个任务会话家族里的进程，不带的是控制台，看全部。"""
    router = APIRouter(prefix="/v1/processes", tags=["processes"], dependencies=[Depends(verify_admin_token)])

    @router.post("")
    def start(body: ProcessStartIn) -> dict[str, Any]:
        return _call(lambda: state.processes.start_process(
            task_id=body.task_id, command=body.command, name=body.name,
            ready_pattern=body.ready_pattern, max_runtime_sec=body.max_runtime_sec,
        ))

    @router.get("")
    def list_processes(task_id: str = Query("", max_length=128)) -> dict[str, Any]:
        return _call(lambda: state.processes.list_processes(task_id=task_id))

    @router.get("/{process_id}")
    def status(process_id: str, task_id: str = Query("", max_length=128)) -> dict[str, Any]:
        return _call(lambda: state.processes.status(task_id=task_id, process_id=process_id))

    @router.get("/{process_id}/output")
    def output(
        process_id: str,
        task_id: str = Query("", max_length=128),
        tail: int | None = Query(None, ge=1),
        since: int | None = Query(None, ge=0),
        search: str = Query(""),
        max_lines: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        return _call(lambda: state.processes.read_output(
            task_id=task_id, process_id=process_id, tail=tail, since=since, search=search, max_lines=max_lines,
        ))

    @router.post("/{process_id}/stop")
    def stop(process_id: str, body: ProcessTaskIn) -> dict[str, Any]:
        return _call(lambda: state.processes.stop_process(task_id=body.task_id, process_id=process_id))

    return router
