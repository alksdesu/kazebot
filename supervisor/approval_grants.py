"""审批授权：审批人同意了自己发起的请求，之后一段时间里他发起的操作免审批。

一个身份同一时刻只留一条，过期的用到时顺手清掉。这里不加锁，调用方持 SupervisorState._lock。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from platforms.ids import normalize_id

from .types import ApprovalGrant

# 同意了却没生成授权的原因。clonoth_sdk.types 里有同名的一份，两边要一致。
GRANT_NOTE_ONCE = "once"
GRANT_NOTE_DISABLED = "disabled"
GRANT_NOTE_NOT_REQUESTER = "not_requester"
GRANT_NOTE_NO_REQUESTER = "no_requester"
GRANT_NOTE_NO_APPROVER = "no_approver"

_TARGET_MAX_CHARS = 500

_Model = TypeVar("_Model", bound=BaseModel)


@dataclass(frozen=True)
class GrantIdentity:
    """认人用的三项。platform 不分大小写；bot_scope 两边都有时才要求一致。"""

    platform: str
    user_id: str
    bot_scope: str = ""

    @classmethod
    def from_auth(cls, raw: Any) -> "GrantIdentity | None":
        """从 platform_auth 或审批人身份里取。缺 platform、缺 user_id、号不合法都返回 None。"""
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()
        if not isinstance(raw, Mapping):
            return None
        platform = str(raw.get("platform") or "").strip().lower()
        user_id = normalize_id(raw.get("user_id"))
        raw_scope = raw.get("bot_scope")
        bot_scope = normalize_id(raw_scope)
        if not platform or not user_id:
            return None
        # 带了却不合法的账号不能当成没带：没带的账号跟哪个账号都对得上。
        if str(raw_scope if raw_scope is not None else "").strip() and not bot_scope:
            return None
        return cls(platform=platform, user_id=user_id, bot_scope=bot_scope)

    def same_person(self, other: "GrantIdentity | None") -> bool:
        if other is None or self.platform != other.platform or self.user_id != other.user_id:
            return False
        return not (self.bot_scope and other.bot_scope) or self.bot_scope == other.bot_scope

    def as_dict(self) -> dict[str, str]:
        return {"platform": self.platform, "user_id": self.user_id, "bot_scope": self.bot_scope}


@dataclass(frozen=True)
class GrantVerdict:
    """同意一条审批时授权怎么定：identity 为 None 表示不授权，note 是原因。"""

    identity: GrantIdentity | None
    minutes: int
    note: str | None = None


def grant_verdict(
    *,
    approver: GrantIdentity | None,
    requester: GrantIdentity | None,
    minutes: int,
    requested: int | None,
) -> GrantVerdict:
    """minutes 是按策略算好的时长，requested 是审批人自己报的（None 表示没报）。"""
    if approver is None:
        return GrantVerdict(None, 0, GRANT_NOTE_NO_APPROVER)
    if requester is None:
        return GrantVerdict(None, 0, GRANT_NOTE_NO_REQUESTER)
    if not approver.same_person(requester):
        return GrantVerdict(None, 0, GRANT_NOTE_NOT_REQUESTER)
    if minutes <= 0:
        return GrantVerdict(None, 0, GRANT_NOTE_ONCE if requested == 0 else GRANT_NOTE_DISABLED)
    # 落在更具体的账号上：审批人没报账号时用请求那边记下的。
    identity = GrantIdentity(
        platform=approver.platform,
        user_id=approver.user_id,
        bot_scope=approver.bot_scope or requester.bot_scope,
    )
    return GrantVerdict(identity, minutes)


def model_or_none(model: type[_Model], raw: Any) -> _Model | None:
    """事件 payload 里的一段转成模型；旧事件没有这一段、或者形状不对，都当作没有。"""
    if not isinstance(raw, Mapping) or not raw:
        return None
    try:
        return model.model_validate(dict(raw))
    except ValidationError:
        return None


def operation_target(op: str, parameters: Mapping[str, Any]) -> str:
    """审计记录里这次放行的是什么：路径、命令或重启目标，截断后的原文。"""
    key = {"read_file": "path", "write_file": "path", "execute_command": "command", "restart": "target"}.get(op, "")
    value = str(parameters.get(key) or "") if key else ""
    return value[:_TARGET_MAX_CHARS]


class ApprovalGrantBook:
    """生效中的授权，按身份一条。"""

    def __init__(self) -> None:
        self._grants: dict[GrantIdentity, ApprovalGrant] = {}

    def draft(self, identity: GrantIdentity, *, minutes: int, approval_id: str, now: datetime) -> ApprovalGrant:
        """拟一条 minutes 分钟的授权，还不生效。已有一条到期更晚的，沿用那个到期时间。"""
        until = now + timedelta(minutes=minutes)
        previous = self._grants.get(identity)
        if previous is not None and previous.until > until:
            until = previous.until
        return ApprovalGrant(
            grant_id=str(uuid.uuid4()),
            platform=identity.platform,
            user_id=identity.user_id,
            bot_scope=identity.bot_scope,
            until=until,
            minutes=minutes,
            approval_id=approval_id,
            issued_at=now,
        )

    def store(self, grant: ApprovalGrant, *, now: datetime) -> ApprovalGrant | None:
        """登记一条授权，现发的和回放的都走这里：同一身份后来的顶替先前的，已经过期的不留。"""
        identity = GrantIdentity.from_auth(grant.model_dump())
        if identity is None:
            return None
        self._grants.pop(identity, None)
        if grant.until <= now:
            return None
        normalized = grant.model_copy(update=identity.as_dict())
        self._grants[identity] = normalized
        return normalized

    def active_for(self, identity: GrantIdentity, *, now: datetime) -> ApprovalGrant | None:
        """identity 名下还没到期的授权。账号一边空着时可能对上几条，取到期最晚的。"""
        self.prune(now=now)
        matches = [grant for key, grant in self._grants.items() if key.same_person(identity)]
        return max(matches, key=lambda grant: grant.until, default=None)

    def active(self, *, now: datetime) -> list[ApprovalGrant]:
        self.prune(now=now)
        return sorted(self._grants.values(), key=lambda grant: grant.until)

    def revoke(
        self, *, now: datetime, grant_id: str = "", identity: GrantIdentity | None = None,
    ) -> list[ApprovalGrant]:
        """收回生效中的授权。给了 grant_id 只认那一条，给了身份只认这个人的，两样都给要同时对上。"""
        if not grant_id and identity is None:
            raise ValueError("收回授权要给 grant_id 或身份")
        self.prune(now=now)
        doomed = [
            key for key, grant in self._grants.items()
            if (not grant_id or grant.grant_id == grant_id) and (identity is None or key.same_person(identity))
        ]
        return [self._grants.pop(key) for key in doomed]

    def drop(self, grant_ids: Iterable[str]) -> int:
        """回放一次收回：按当时记下的 grant_id 摘掉，同一身份之后新发的那条不受影响。"""
        wanted = {str(grant_id) for grant_id in grant_ids if grant_id}
        doomed = [key for key, grant in self._grants.items() if grant.grant_id in wanted]
        for key in doomed:
            del self._grants[key]
        return len(doomed)

    def prune(self, *, now: datetime) -> int:
        expired = [key for key, grant in self._grants.items() if grant.until <= now]
        for key in expired:
            del self._grants[key]
        return len(expired)
