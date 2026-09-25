from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from supervisor.feature_auth import resolve_actor

from .service import OperationsService


def create_router(state: Any) -> APIRouter:
    service = OperationsService(state)
    state.operations = service
    router = APIRouter(prefix="/v1/operations")

    async def read_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=422, detail="请求体需要 JSON 对象") from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="请求体需要 JSON 对象")
        return body

    def authorize(request: Request, *, interactive: bool = False):
        actor = resolve_actor(request, state)
        if not actor.is_admin:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        if interactive:
            actor.require_interaction()
        return actor

    @router.get("/status")
    async def status(request: Request):
        authorize(request)
        return service.status()

    @router.get("/instances")
    async def instances(request: Request):
        authorize(request)
        return {"instances": service.instances()}

    @router.post("/diagnostics")
    async def diagnose(request: Request):
        authorize(request, interactive=True)
        body = await read_body(request)
        try:
            return await service.diagnose(test_model=body.get("test_model") is True, include_logs=body.get("include_logs") is True)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @router.get("/diagnostics/{report_id}")
    async def report(request: Request, report_id: str):
        authorize(request)
        try:
            return service.get_report(report_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error))

    @router.get("/diagnostics/{report_id}/export")
    async def export(request: Request, report_id: str):
        result = await report(request, report_id)
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="diagnostics-{report_id}.json"'})

    @router.get("/templates")
    @router.post("/templates/preview")
    async def retired_templates(request: Request):
        authorize(request)
        raise HTTPException(status_code=410, detail="模板功能已移除，请切换实例独立设置")

    @router.post("/templates")
    @router.post("/templates/apply")
    async def retired_template_writes(request: Request):
        authorize(request, interactive=True)
        raise HTTPException(status_code=410, detail="模板功能已移除，请切换实例独立设置")

    return router
