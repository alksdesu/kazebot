from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from platforms.descriptor import CHANNEL_GROUP
from platforms.registry import conversation_channel, conversation_key_prefix

from .admin_api import verify_admin_token


@dataclass(frozen=True)
class FeatureActor:
    scope: str = ""
    owner: str = ""
    is_admin: bool = False
    role: str = "member"
    channel: str = "web"
    bot_scope: str = ""
    task_id: str = ""
    message_id: str = ""
    interactive: bool = True
    user_id: str = ""

    def require_scope(self, scope: str) -> None:
        if not self.is_admin and (not scope or self.scope != scope):
            raise HTTPException(status_code=403, detail="无权访问这个会话的内容")

    def require_owner(self, owner: str, scope: str) -> None:
        self.require_scope(scope)
        if not self.is_admin and (not owner or self.owner != owner):
            raise HTTPException(status_code=403, detail="只能操作本人创建的内容")

    def require_manager(self, scope: str) -> None:
        self.require_scope(scope)
        if not self.is_admin and self.role not in {"owner", "admin", "group_admin"}:
            raise HTTPException(status_code=403, detail="需要本群管理权限")

    def require_interaction(self) -> None:
        if not self.interactive or self.task_id:
            raise HTTPException(status_code=403, detail="请由用户确认当前版本，模型不能代替用户确认")


def _text(value: Any, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _is_group_scope(channel: str, scope: str) -> bool:
    """群角色只在群会话里有意义，而且会话要属于发请求的这个通道。"""
    found = conversation_channel(scope)
    return found is not None and found[1].kind == CHANNEL_GROUP and found[1].name == channel


def _owner(instance: str, user_id: str, fallback: str) -> str:
    if not user_id:
        return fallback
    digest = hashlib.sha256(f"{instance}:{user_id}".encode("utf-8")).hexdigest()[:32]
    return f"user:{digest}"


def resolve_actor(request: Request, state: Any, scope: str = "") -> FeatureActor:
    verify_admin_token(request)
    instance = hashlib.sha256(str(state.workspace_root.resolve()).encode("utf-8")).hexdigest()[:24]
    requested_scope = _text(scope or request.query_params.get("scope") or request.headers.get("X-Clonoth-Scope"))
    task_id = _text(request.headers.get("X-Clonoth-Task-Id"))
    if task_id:
        with state._lock:
            task = state.tasks.get(task_id)
            if task is None or task.cancel_requested or state._task_terminal(task):
                raise HTTPException(status_code=403, detail="功能请求没有有效的执行任务")
            execution = getattr(state, "execution", None)
            if execution is not None and hasattr(execution, "actor_for_task"):
                bound = execution.actor_for_task(task)
                if bound is not None:
                    if requested_scope:
                        bound.require_scope(requested_scope)
                    return bound
            context = dict(task.input.get("task_context") or {})
            auth = dict(context.get("platform_auth") or {})
            task_scope = _text(
                context.get("route_conversation_key") or context.get("parent_conversation_key")
                or context.get("conversation_key"),
            )
            channel = _text(context.get("channel") or auth.get("platform") or "web")
            actor = FeatureActor(
                scope=task_scope,
                owner=_owner(instance, _text(auth.get("user_id")), f"web:{task_scope}"),
                is_admin=auth.get("is_admin") is True,
                role=(
                    _text(auth.get("group_role") or "member")
                    if _is_group_scope(channel, task_scope) and auth.get("group_scope") == task_scope else "member"
                ), channel=channel,
                bot_scope=_text(auth.get("bot_scope") or instance), task_id=task_id,
                message_id=_text(context.get("message_id")), interactive=False,
                user_id=_text(auth.get("user_id")),
            )
        if requested_scope:
            actor.require_scope(requested_scope)
        return actor

    adapter_header = request.headers.get("X-Clonoth-Adapter-Actor")
    if adapter_header is not None:
        try:
            raw = json.loads(adapter_header)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="无效的适配器身份")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="无效的适配器身份")
        actor_scope = _text(raw.get("scope"))
        user_id = _text(raw.get("user_id"))
        if not actor_scope or not user_id:
            raise HTTPException(status_code=403, detail="缺少已核验的会话或发送者")
        channel = _text(raw.get("channel")) or conversation_key_prefix(actor_scope)
        actor = FeatureActor(
            scope=actor_scope, owner=_owner(instance, user_id, ""),
            is_admin=raw.get("is_admin") is True,
            role=_text(raw.get("role") or "member") if _is_group_scope(channel, actor_scope) else "member",
            channel=channel,
            bot_scope=_text(raw.get("bot_scope") or instance),
            message_id=_text(raw.get("message_id")),
            user_id=user_id,
        )
        if requested_scope:
            actor.require_scope(requested_scope)
        return actor

    return FeatureActor(
        scope=requested_scope, owner="console:admin", is_admin=True,
        role="admin", channel="web", bot_scope=instance,
    )
