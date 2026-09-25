from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from supervisor.feature_auth import resolve_actor
from supervisor.admin_api import verify_admin_token
from .service import ReminderConflict


class ReminderCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    due_at: str
    timezone: str = "Asia/Shanghai"


class ReminderAction(BaseModel):
    expected_revision: int = Field(ge=1)
    minutes: int = Field(default=10, ge=1, le=525600)
    action_id: str = Field(default="", max_length=100)


class DeliveryReceipt(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=30)
    message_ids: list[str] = Field(default_factory=list, max_length=100)


class DeliveryMessage(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=512)
    bot_scope: str = Field(min_length=1, max_length=512)
    message_id: str = Field(min_length=1, max_length=128)


class ReplyAction(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=20)
    minutes: int = Field(default=10, ge=1, le=525600)


def _call(fn):
    try:
        return fn()
    except ReminderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def create_router(state: Any) -> APIRouter:
    router = APIRouter(prefix="/v1/reminders", tags=["reminders"], dependencies=[Depends(verify_admin_token)])

    @router.get("")
    def list_reminders(request: Request):
        actor = resolve_actor(request, state)
        return {"reminders": state.reminders.list(actor)}

    @router.post("")
    def create_reminder(body: ReminderCreate, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.reminders.create(actor, body.text, body.due_at, body.timezone))

    @router.get("/delivery-status")
    def delivery_status(request: Request, delivery_id: str):
        verify_admin_token(request)
        if request.headers.get("X-Clonoth-Task-Id") or request.headers.get("X-Clonoth-Adapter-Actor"):
            raise HTTPException(403, "投递查询只允许传输服务调用")
        return state.reminders.delivery_status(delivery_id)

    @router.post("/receipts")
    def receipt(request: Request, body: DeliveryReceipt):
        verify_admin_token(request)
        if request.headers.get("X-Clonoth-Task-Id") or request.headers.get("X-Clonoth-Adapter-Actor"):
            raise HTTPException(403, "投递回执只允许传输服务调用")
        return _call(lambda: state.reminders.receipt(body.delivery_id, body.status, message_ids=body.message_ids))

    @router.post("/delivery-messages")
    def bind_delivery_message(request: Request, body: DeliveryMessage):
        if request.headers.get("X-Clonoth-Task-Id") or request.headers.get("X-Clonoth-Adapter-Actor"):
            raise HTTPException(403, "投递消息绑定只允许传输服务调用")
        return _call(lambda: state.reminders.bind_delivery_message(body.delivery_id, body.scope, body.bot_scope, body.message_id))

    @router.get("/reply-context")
    def reply_context(request: Request, message_id: str):
        actor = resolve_actor(request, state)
        return _call(lambda: state.reminders.reply_context(actor, message_id))

    @router.post("/reply-actions")
    def reply_action(request: Request, body: ReplyAction):
        actor = resolve_actor(request, state)
        return _call(lambda: state.reminders.act_on_reply(actor, body.message_id, body.action, minutes=body.minutes))

    @router.get("/{reminder_id}")
    def get_reminder(reminder_id: str, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.reminders.get(actor, reminder_id))

    @router.post("/{reminder_id}/{action}")
    def act(reminder_id: str, action: str, body: ReminderAction, request: Request):
        actor = resolve_actor(request, state)
        return _call(lambda: state.reminders.act(actor, reminder_id, action, body.expected_revision, minutes=body.minutes, action_id=body.action_id))

    return router
