"""任务归谁：按发起人的平台身份判断一轮能不能看、追加、取消另一个任务。群聊里普通成员只能管自己发起的。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def requester_of(auth: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """平台身份里的发起人。平台和号都在才算：匿名消息、控制台建的和系统流程都没有发起人。"""
    if not isinstance(auth, Mapping):
        return None
    platform = str(auth.get("platform") or "").strip()
    user_id = str(auth.get("user_id") or "").strip()
    return (platform, user_id) if platform and user_id else None


def in_group(auth: Mapping[str, Any] | None) -> bool:
    return isinstance(auth, Mapping) and bool(str(auth.get("group_scope") or "").strip())


def may_touch(caller_auth: Mapping[str, Any] | None, target_auth: Mapping[str, Any] | None) -> bool:
    """调用方能不能碰这个任务。

    不是从聊天平台来的（控制台、内部流程）和管理员名单里的人不受限；私聊一个对话只有一个人，也不受限。
    群聊里普通成员只认同一个发起人，没有发起人的任务（定时任务、控制台建的、匿名消息）都不算他的。
    """
    if not isinstance(caller_auth, Mapping) or not str(caller_auth.get("platform") or "").strip():
        return True
    if caller_auth.get("is_admin") is True or not in_group(caller_auth):
        return True
    mine = requester_of(caller_auth)
    return mine is not None and mine == requester_of(target_auth)


def same_requester(caller_auth: Mapping[str, Any] | None, target_auth: Mapping[str, Any] | None) -> bool:
    mine = requester_of(caller_auth)
    return mine is not None and mine == requester_of(target_auth)
