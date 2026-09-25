from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from supervisor.feature_auth import resolve_actor
from supervisor.admin_api import verify_admin_token
from .models import PlanDraft
from .store import ConflictError


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class EditRequest(PlanDraft):
    expected_revision: int = Field(ge=1)


class RetryRequest(BaseModel):
    step_ids: list[str] = Field(default_factory=list, max_length=120)


class ResolveRequest(BaseModel):
    step_id: str = Field(min_length=1, max_length=64)
    resolution: str = Field(min_length=1, max_length=20)
    note: str = Field(min_length=1, max_length=4000)


def _call(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def create_router(state: Any) -> APIRouter:
    router = APIRouter(prefix="/v1/execution", tags=["execution"], dependencies=[Depends(verify_admin_token)])

    @router.get("/tools")
    def tools(request: Request):
        actor = resolve_actor(request, state)
        return {"tools": _call(lambda: state.execution.tools(actor))}

    @router.get("/plans")
    def list_plans(request: Request):
        actor = resolve_actor(request, state)
        return {"plans": [state.execution._view(plan) for plan in state.execution.list(actor)]}

    @router.post("/plans")
    def create_plan(body: PlanDraft, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.create(actor, body))

    @router.get("/plans/{plan_id}")
    def get_plan(plan_id: str, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.view(actor, plan_id))

    @router.patch("/plans/{plan_id}")
    def edit_plan(plan_id: str, body: EditRequest, request: Request):
        actor = resolve_actor(request, state)
        draft = PlanDraft.model_validate(body.model_dump(exclude={"expected_revision"}))
        return _call(lambda: state.execution.edit(actor, plan_id, body.expected_revision, draft))

    @router.post("/plans/{plan_id}/confirm")
    def confirm_plan(plan_id: str, body: RevisionRequest, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.confirm(actor, plan_id, body.expected_revision))

    @router.post("/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: str, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.cancel(actor, plan_id))

    @router.post("/plans/{plan_id}/retry")
    def retry_plan(plan_id: str, request: Request, body: RetryRequest = RetryRequest()):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.retry(actor, plan_id, body.step_ids))

    @router.post("/plans/{plan_id}/resolve-step")
    def resolve_step(plan_id: str, request: Request, body: ResolveRequest):
        actor = resolve_actor(request, state)
        return _call(lambda: state.execution.resolve_step(actor, plan_id, body.step_id, body.resolution, body.note))

    @router.get("/artifacts/{artifact_id}")
    def download_artifact(artifact_id: str, request: Request):
        actor = resolve_actor(request, state)
        path, name = _call(lambda: state.execution.artifact_path(actor, artifact_id))
        return FileResponse(path, filename=name)

    @router.get("/plans/{plan_id}/bundle")
    def download_bundle(plan_id: str, request: Request):
        actor = resolve_actor(request, state)
        path = _call(lambda: state.execution.bundle(actor, plan_id))
        return FileResponse(path, filename=f"{plan_id}.zip")

    return router
