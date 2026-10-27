"""隔离副本的接口：merge_worktree 工具合回前预览、合回、放弃，控制台列出登记。一律要管理令牌。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel


class WorktreeActionIn(BaseModel):
    task_id: str
    handle: str


class WorktreeMergeIn(BaseModel):
    task_id: str
    handle: str
    digest: str


def create_router(state: Any) -> APIRouter:
    from ..admin_api import verify_admin_token
    from .service import service_of

    router = APIRouter(prefix="/v1/worktrees", dependencies=[Depends(verify_admin_token)])

    # 都会跑 git、读写文件：用同步函数，FastAPI 放到线程池里跑
    @router.get("")
    def listing() -> dict[str, Any]:
        return {"worktrees": service_of(state).listing()}

    @router.post("/preview")
    def preview(body: WorktreeActionIn) -> dict[str, Any]:
        return service_of(state).preview(task_id=body.task_id.strip(), handle=body.handle.strip())

    @router.post("/merge")
    def merge(body: WorktreeMergeIn) -> dict[str, Any]:
        return service_of(state).merge(task_id=body.task_id.strip(), handle=body.handle.strip(), digest=body.digest.strip())

    @router.post("/abandon")
    def abandon(body: WorktreeActionIn) -> dict[str, Any]:
        return service_of(state).abandon(task_id=body.task_id.strip(), handle=body.handle.strip())

    return router
