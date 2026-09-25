from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from supervisor.feature_auth import resolve_actor
from supervisor.conversation_labels import describe_namespaces, memory_namespace, scoped_conversation_keys

from .commands import command
from .service import CommunityService, SettingsConflict


def create_router(state) -> APIRouter:
    if getattr(state, "community", None) is None:
        state.community = CommunityService(state.workspace_root)
    service = state.community
    router = APIRouter(prefix="/v1/community", tags=["community"])

    def call(request, method, *args, defaults=False):
        actor = resolve_actor(request, state)
        if defaults and ("X-Clonoth-Adapter-Actor" in request.headers or "X-Clonoth-Task-Id" in request.headers):
            raise HTTPException(status_code=403, detail="全局默认只能在当前实例的控制台中管理")
        try:
            return method(actor, *args)
        except SettingsConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ValueError, TypeError, OverflowError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/state")
    def get_state(request: Request):
        return call(request, service.state)

    @router.get("/settings/scopes")
    def settings_scopes(request: Request):
        scopes = call(request, service.settings_scopes, defaults=True)
        labels = describe_namespaces(state.workspace_root, conversation_keys=scopes)
        current_keys = scoped_conversation_keys(state.workspace_root)
        return {"items": [{"scope": scope, "owner": labels.get(memory_namespace(scope)),
                           "current_account": current_keys is None or scope in current_keys} for scope in scopes]}

    @router.get("/settings/defaults")
    def defaults(request: Request):
        return call(request, service.defaults, defaults=True)

    @router.patch("/settings/defaults")
    def update_defaults(request: Request, body: dict = Body(...)):
        return call(request, service.update_defaults, body, defaults=True)

    @router.patch("/settings/overrides")
    def overrides(request: Request, body: dict = Body(...)):
        return call(request, service.update_overrides, body)

    @router.patch("/settings")
    def settings(request: Request, body: dict = Body(...)):
        return call(request, service.update_settings, body)

    @router.get("/guide")
    def get_guide(request: Request):
        return call(request, service.guide)

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
