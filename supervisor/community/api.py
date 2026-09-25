from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from supervisor.feature_auth import resolve_actor

from .commands import command
from .service import CommunityService


def create_router(state) -> APIRouter:
    if getattr(state, "community", None) is None:
        state.community = CommunityService(state.workspace_root)
    service = state.community
    router = APIRouter(prefix="/v1/community", tags=["community"])

    def call(request, method, *args):
        actor = resolve_actor(request, state)
        try:
            return method(actor, *args)
        except (ValueError, TypeError, OverflowError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/state")
    def get_state(request: Request):
        return call(request, service.state)

    @router.patch("/settings")
    def settings(request: Request, body: dict = Body(...)):
        return call(request, service.update_settings, body)

    @router.put("/guide")
    def guide(request: Request, body: dict = Body(...)):
        return call(request, service.update_guide, body)

    @router.post("/quiet")
    def quiet(request: Request, body: dict = Body(...)):
        return call(request, service.set_quiet, str(body.get("mode") or "listen"), body.get("duration_sec", 1800))

    @router.delete("/quiet")
    def unquiet(request: Request):
        return call(request, service.set_quiet, "off", 0)

    @router.get("/activities")
    def activities(request: Request):
        return {"items": call(request, service.activities)}

    @router.post("/activities")
    def create_activity(request: Request, body: dict = Body(...)):
        return call(request, service.create_activity, body)

    @router.post("/activities/{identity}/{action}")
    def activity_action(identity: str, action: str, request: Request, body: dict = Body(default={})):
        return call(request, service.activity_action, identity, action, body)

    @router.post("/commands")
    def commands(request: Request, body: dict = Body(...)):
        return {"result": call(request, lambda actor: command(service, actor, body))}

    @router.post("/notices")
    def notices(request: Request, body: dict = Body(...)):
        return call(request, service.notice, body)

    @router.get("/notifications")
    def notifications(request: Request):
        return {"items": call(request, service.notifications)}

    @router.post("/notifications/{identity}/ack")
    def acknowledge(identity: str, request: Request, body: dict = Body(...)):
        return call(request, service.acknowledge_notification, identity, str(body.get("message_id") or ""))

    @router.post("/messages")
    def messages(request: Request, body: dict = Body(...)):
        return call(request, service.ingest, body)

    @router.get("/decisions")
    def decisions(request: Request):
        return {"items": call(request, service.decisions)}

    @router.post("/decisions")
    def decision(request: Request, body: dict = Body(...)):
        return call(request, service.record_decision, body)

    return router
