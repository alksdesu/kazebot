"""Clonoth SDK 数据类型定义。

Phase 1 (2026-04-17): 初始创建，从 bot_adapter.py 协议交互中提取。
包含所有与 Supervisor HTTP API 交互所需的数据结构。
使用 dataclass 而非 pydantic，减少外部依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
from typing import Any


@dataclass
class InboundResult:
    """POST /v1/inbound 的返回结果。

    提取自 bot_adapter.py _submit_inbound() 返回的 (session_id, inbound_seq) 元组，
    增加 accepted 字段与 Supervisor 响应保持一致。
    """
    session_id: str
    inbound_seq: int
    accepted: bool = True


@dataclass(frozen=True)
class IntentVerdict:
    """POST /v1/intent/judge 的返回结果。

    decided=False 表示没拿到模型答案（超时 / 被在飞上限挡住），此时 agreed 恒为
    False —— 判定层不可用时插话，是这两个方向里错得更难看的那个。
    """

    agreed: bool = False
    reason: str = ""
    error: str = ""
    decided: bool = False
    task_id: str = ""
    action: str = "reply"
    reaction_id: str = ""
    topic_ref: str = ""


@dataclass(frozen=True)
class DeliveryContext:
    """Immutable identity/audit context for one adapter callback delivery.

    ``force_replay`` increments ``replay_generation`` and changes
    ``idempotency_key``. Adapters must use that key (not only ``event_id``), so an
    operator-requested replay intentionally bypasses the prior platform ``sent``
    claim while ordinary retries remain deduplicated.
    """

    event_id: str = ""
    event_seq: int = 0
    task_id: str = ""
    source_inbound_seq: int = 0
    conversation_key: str = ""
    attempt: int = 1
    idempotency_key: str = ""
    replay_generation: int = 0
    force_replay: bool = False
    purpose: str = "direct"
    feature_delivery_id: str = ""
    topic_id: str = ""
    reply_message_id: str = ""

    def child(self, identity: str) -> "DeliveryContext":
        """Derive a stable logical-message context without mutating the parent."""
        digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()[:24]
        event_id = f"{self.event_id}:{digest}" if self.event_id else ""
        key = f"{self.idempotency_key}:{digest}" if self.idempotency_key else ""
        return replace(self, event_id=event_id, idempotency_key=key)


@dataclass
class Event:
    """Supervisor 事件流中的单个事件。

    对应 GET /v1/events 和 WS /v1/ws 返回的事件对象。
    ts 保留为 ISO 格式字符串，调用方可按需用 datetime.fromisoformat() 解析。
    """
    seq: int
    event_id: str
    ts: str
    run_id: str
    session_id: str
    component: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    # [SDK WS 2026-05-19] Why: the Supervisor event schema includes a version
    # field on both HTTP and WebSocket rows. How: keep it as a defaulted trailing
    # dataclass field so existing positional construction remains compatible.
    # Purpose: raw-event hooks can inspect the schema version when needed.
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event without losing replay identity or payload fields."""
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "ts": self.ts,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "component": self.component,
            "type": self.type,
            "payload": dict(self.payload),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        """从 API JSON 响应字典构造 Event 实例。

        对字段做防御性类型转换，避免 Supervisor 返回意外类型时崩溃。
        """
        return cls(
            seq=int(d.get("seq", 0)),
            event_id=str(d.get("event_id", "")),
            ts=str(d.get("ts", "")),
            run_id=str(d.get("run_id", "")),
            session_id=str(d.get("session_id", "")),
            component=str(d.get("component", "")),
            type=str(d.get("type", "")),
            payload=dict(d.get("payload") or {}),
            schema_version=int(d.get("schema_version", 1) or 1),
        )


@dataclass
class RunningTask:
    """会话中活跃任务的信息。

    对应 GET /v1/sessions/{sid}/running_tasks 返回的单个任务对象。
    is_user_entry 标识此任务是否由用户 inbound 直接创建（非子节点/非异步dispatch）。
    """
    task_id: str
    node_id: str
    status: str
    created_at: str
    caller_task_id: str
    is_user_entry: bool
    source_inbound_seq: int | None = None


@dataclass(frozen=True)
class PlatformAuth:
    """入站时 supervisor 记下的发起人身份。网页、定时任务发起的任务没有这一份，各字段都是空值。"""

    platform: str = ""
    user_id: str = ""
    is_admin: bool = False
    group_role: str = ""
    group_scope: str = ""
    bot_scope: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "PlatformAuth":
        if not isinstance(data, dict):
            return cls()
        return cls(
            platform=str(data.get("platform") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            # 只认真布尔：字符串 "false" 也是真值。
            is_admin=data.get("is_admin") is True,
            group_role=str(data.get("group_role") or "").strip(),
            group_scope=str(data.get("group_scope") or "").strip(),
            bot_scope=str(data.get("bot_scope") or "").strip(),
        )


@dataclass(frozen=True)
class TaskOrigin:
    """GET /v1/tasks/{task_id}/origin 的返回。子任务已由 supervisor 回溯到发起会话。"""

    task_id: str
    origin_task_id: str
    conversation_key: str
    channel: str
    platform_auth: PlatformAuth = field(default_factory=PlatformAuth)

    @classmethod
    def from_dict(cls, data: Any) -> "TaskOrigin":
        source = data if isinstance(data, dict) else {}
        return cls(
            task_id=str(source.get("task_id") or ""),
            origin_task_id=str(source.get("origin_task_id") or ""),
            conversation_key=str(source.get("conversation_key") or ""),
            channel=str(source.get("channel") or ""),
            platform_auth=PlatformAuth.from_dict(source.get("platform_auth")),
        )


APPROVAL_PENDING = "pending"
APPROVAL_ALLOWED = "allowed"
APPROVAL_DENIED = "denied"
# supervisor 那边查无此审批时回执里的状态。
APPROVAL_MISSING = "missing"
# 审批没等到决定就作废了：发起它的任务已结束，或者下落查不到。
APPROVAL_EXPIRED = "expired"
_STATUS_OF_DECISION = {"allow": APPROVAL_ALLOWED, "deny": APPROVAL_DENIED}

# 同意了却没生成授权、或者要了永久只给了限时的原因，和 supervisor/approval_grants.py 里的同名常量一致。
GRANT_NOTE_ONCE = "once"
GRANT_NOTE_DISABLED = "disabled"
GRANT_NOTE_NOT_REQUESTER = "not_requester"
GRANT_NOTE_NO_REQUESTER = "no_requester"
GRANT_NOTE_NO_APPROVER = "no_approver"
GRANT_NOTE_PERMANENT_NOT_ADMIN = "permanent_not_admin"
GRANT_NOTE_PERMANENT_DISABLED = "permanent_disabled"


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _non_negative_int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class ApprovalGrant:
    """同意审批时给审批人本人的授权：until 之前他发起的操作免审批。permanent 为真是永久授权，没有 until，收回前一直有效。"""

    grant_id: str
    platform: str
    user_id: str
    bot_scope: str = ""
    until: datetime | None = None
    minutes: int = 0
    approval_id: str = ""
    issued_at: datetime | None = None
    permanent: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "ApprovalGrant | None":
        """supervisor 回的一条授权；没有、或者缺 grant_id 时返回 None。"""
        if not isinstance(data, dict):
            return None
        grant_id = str(data.get("grant_id") or "").strip()
        if not grant_id:
            return None
        return cls(
            grant_id=grant_id,
            platform=str(data.get("platform") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            bot_scope=str(data.get("bot_scope") or "").strip(),
            until=_parse_time(data.get("until")),
            minutes=_non_negative_int(data.get("minutes")),
            approval_id=str(data.get("approval_id") or "").strip(),
            issued_at=_parse_time(data.get("issued_at")),
            permanent=data.get("permanent") is True,
        )


@dataclass(frozen=True)
class ApprovalDecision:
    """POST /v1/approvals/{id} 的回执。返回的状态与本次决定一致才算这次决定生效。

    grant 是这次同意生成的授权；没生成、或者要了永久只给了限时的，grant_note 说明原因。旧版 supervisor 两项都是空的。
    """

    approval_id: str
    decision: str
    status: str
    comment: str = ""
    grant: ApprovalGrant | None = None
    grant_note: str = ""

    @property
    def effective(self) -> bool:
        return _STATUS_OF_DECISION.get(self.decision) == self.status


@dataclass
class HealthInfo:
    """Supervisor 健康状态信息。

    对应 GET /v1/health 返回。workspace_root 可用于启动时动态获取工作区路径。
    """
    status: str
    run_id: str
    workspace_root: str
    started_at: str
    uptime_seconds: float


@dataclass
class OpenAIConfig:
    """OpenAI 兼容 API 的公开配置信息。

    对应 GET /v1/config/openai 返回。这个端点没有令牌门，所以密钥只报有没有。
    """
    base_url: str
    model: str
    api_key_present: bool
