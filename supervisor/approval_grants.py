"""审批授权：审批人同意了自己发起的请求，之后一段时间里他发起的操作免审批；管理员名单里的人可以要永久的，收回前一直有效。

一个身份同一时刻只留一条，过期的用到时顺手清掉；生效中的另落一份文件，事件日志轮转掉了也不丢。这里不加锁，调用方持 SupervisorState._lock。
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

import config_layout
from platforms.ids import normalize_id

from .types import ApprovalGrant

logger = logging.getLogger(__name__)

# 同意了却没生成授权、或者要了永久只给了限时的原因。clonoth_sdk.types 里有同名的一份，两边要一致。
GRANT_NOTE_ONCE = "once"
GRANT_NOTE_DISABLED = "disabled"
GRANT_NOTE_NOT_REQUESTER = "not_requester"
GRANT_NOTE_NO_REQUESTER = "no_requester"
GRANT_NOTE_NO_APPROVER = "no_approver"
GRANT_NOTE_PERMANENT_NOT_ADMIN = "permanent_not_admin"
GRANT_NOTE_PERMANENT_DISABLED = "permanent_disabled"

GRANT_FILE_VERSION = 1
_GRANT_FILE_MODE = 0o600
_TARGET_MAX_CHARS = 500
_FOREVER = datetime.max.replace(tzinfo=timezone.utc)

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
    """同意一条审批时授权怎么定：identity 为 None 表示不授权，permanent 为真是永久授权；note 是没授权、或者没给永久的原因。"""

    identity: GrantIdentity | None
    minutes: int
    note: str | None = None
    permanent: bool = False


def grant_verdict(
    *,
    approver: GrantIdentity | None,
    requester: GrantIdentity | None,
    minutes: int,
    requested: int | None,
    permanent: bool = False,
    permanent_enabled: bool = True,
    requester_is_admin: bool = False,
) -> GrantVerdict:
    """minutes 是按策略算好的时长，requested 是审批人自己报的（None 表示没报）。

    permanent 为真是审批人要永久授权：策略允许、请求发起人在管理员名单里才给，否则按 minutes 给限时的，note 写明为什么。
    """
    if approver is None:
        return GrantVerdict(None, 0, GRANT_NOTE_NO_APPROVER)
    if requester is None:
        return GrantVerdict(None, 0, GRANT_NOTE_NO_REQUESTER)
    if not approver.same_person(requester):
        return GrantVerdict(None, 0, GRANT_NOTE_NOT_REQUESTER)
    # 落在更具体的账号上：审批人没报账号时用请求那边记下的。
    identity = GrantIdentity(
        platform=approver.platform,
        user_id=approver.user_id,
        bot_scope=approver.bot_scope or requester.bot_scope,
    )
    refusal: str | None = None
    if permanent:
        if not permanent_enabled:
            refusal = GRANT_NOTE_PERMANENT_DISABLED
        elif not requester_is_admin:
            refusal = GRANT_NOTE_PERMANENT_NOT_ADMIN
        else:
            return GrantVerdict(identity, 0, permanent=True)
    if minutes <= 0:
        return GrantVerdict(None, 0, refusal or (GRANT_NOTE_ONCE if requested == 0 else GRANT_NOTE_DISABLED))
    return GrantVerdict(identity, minutes, refusal)


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


def _expiry(grant: ApprovalGrant) -> datetime:
    """比较和排序用的到期时间，永久授权当作永不到期。"""
    return _FOREVER if grant.until is None else grant.until


class ApprovalGrantBook:
    """生效中的授权，按身份一条。"""

    def __init__(self) -> None:
        self._grants: dict[GrantIdentity, ApprovalGrant] = {}

    def draft(
        self, identity: GrantIdentity, *, minutes: int, approval_id: str, now: datetime, permanent: bool = False,
    ) -> ApprovalGrant:
        """拟一条授权，还不生效：permanent 为真是永久的，否则 minutes 分钟。已有一条到期更晚的，沿用它的到期时间，永久的照旧永久。"""
        previous = self._grants.get(identity)
        until: datetime | None = None
        if permanent or (previous is not None and previous.permanent):
            minutes, permanent = 0, True
        else:
            until = now + timedelta(minutes=minutes)
            if previous is not None and _expiry(previous) > until:
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
            permanent=permanent,
        )

    def store(self, grant: ApprovalGrant, *, now: datetime) -> ApprovalGrant | None:
        """登记一条授权，现发的、回放的和从文件读回的都走这里：同一身份后来的顶替先前的，已经过期的不留。"""
        identity = GrantIdentity.from_auth(grant.model_dump())
        if identity is None:
            return None
        self._grants.pop(identity, None)
        if _expiry(grant) <= now:
            return None
        normalized = grant.model_copy(update=identity.as_dict())
        self._grants[identity] = normalized
        return normalized

    def active_for(
        self, identity: GrantIdentity, *, now: datetime, permanent_ok: bool = True,
    ) -> ApprovalGrant | None:
        """identity 名下还没到期的授权，permanent_ok 为假时永久的不算。账号一边空着时可能对上几条，取到期最晚的。"""
        self.prune(now=now)
        matches = [
            grant for key, grant in self._grants.items()
            if key.same_person(identity) and (permanent_ok or not grant.permanent)
        ]
        return max(matches, key=_expiry, default=None)

    def active(self, *, now: datetime) -> list[ApprovalGrant]:
        self.prune(now=now)
        return sorted(self._grants.values(), key=_expiry)

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
        expired = [key for key, grant in self._grants.items() if _expiry(grant) <= now]
        for key in expired:
            del self._grants[key]
        return len(expired)


class ApprovalGrantFile:
    """生效中的授权另落一份：事件日志会轮转，永久的和长的授权不能只记在事件里。只有本实例的 supervisor 写它，不跨进程加锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> list[ApprovalGrant] | None:
        """读回来的授权。没有这份文件、读不了或形状不对返回 None，认不出的单条跳过；后两种都记一条。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("approval grants file unreadable, rebuilding from events only: %s", self.path, exc_info=True)
            return None
        items = raw.get("grants") if isinstance(raw, dict) and raw.get("version") == GRANT_FILE_VERSION else None
        if not isinstance(items, list):
            logger.warning("approval grants file has an unknown shape, rebuilding from events only: %s", self.path)
            return None
        grants = [model_or_none(ApprovalGrant, item) for item in items]
        if None in grants:
            logger.warning(
                "approval grants file: skipped %d malformed grant(s) in %s", grants.count(None), self.path,
            )
        return [grant for grant in grants if grant is not None]

    def write(self, grants: Iterable[ApprovalGrant]) -> None:
        """整份重写，写失败抛 OSError。"""
        body = {"version": GRANT_FILE_VERSION, "grants": [grant.model_dump(mode="json") for grant in grants]}
        config_layout.write_locked(
            self.path, json.dumps(body, ensure_ascii=False, indent=2) + "\n", mode=_GRANT_FILE_MODE,
        )
