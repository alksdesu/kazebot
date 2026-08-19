from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SafetyLevel(str, Enum):
    auto = "auto"
    approval_required = "approval_required"
    deny = "deny"


class ApprovalStatus(str, Enum):
    pending = "pending"
    allowed = "allowed"
    denied = "denied"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    suspended = "suspended"
    cancelled = "cancelled"


class TaskKind(str, Enum):
    node = "node"
    tool = "tool"


class RouteStatus(str, Enum):
    """Durable completion-routing state for retryable Supervisor delivery."""

    pending = "pending"
    routed = "routed"
    failed = "failed"


class Event(BaseModel):
    schema_version: int = 1
    seq: int
    event_id: str
    ts: datetime
    run_id: str
    session_id: str
    component: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class InboundMessageIn(BaseModel):
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True
    entry_node_id: str | None = None
    # Adapters set this when a user command selected the node; capability routing must not override it.
    entry_node_pinned: bool = False
    # [2026-05-28] 异步 dispatch 统一走 inbound：新增 3 个可选字段。
    # 为什么：异步子节点委派改为 POST /v1/inbound，需要在 payload 中携带回调元数据。
    # dispatch_origin: 回调目标信息（parent_session_id, caller_node_id）
    # dispatch_context_mode: 上下文模式（accumulate/fresh/fork）
    # dispatch_fork_from_session: fork 模式的源 session ID
    dispatch_origin: dict[str, Any] | None = None
    dispatch_context_mode: str | None = None
    dispatch_fork_from_session: str | None = None
    # Platform-provided identity metadata. This is produced by trusted adapters
    # such as OneBot/Discord, not parsed from user text.
    platform_auth: dict[str, Any] = Field(default_factory=dict)
    # Adapter-provided routing hints, e.g. has_image/channel/platform. These are
    # trusted platform metadata and are used only for capability routing.
    route_hints: dict[str, Any] = Field(default_factory=dict)
    # 本轮涉及的人（{"subjects": [匿名别名]}），用于按人加载跨会话长期记忆。
    # 与 platform_auth 分开：后者参与鉴权，不该混入非鉴权字段。
    memory_hints: dict[str, Any] = Field(default_factory=dict)


class InboundMessageOut(BaseModel):
    session_id: str
    inbound_seq: int = 0
    accepted: bool = True


class OutboundMessageIn(BaseModel):
    text: str
    source_inbound_seq: int | None = None
    attachments: list[dict[str, Any]] | None = None
    # [AutoC 2026-06-04] Why: manual outbound API calls may also be used to finalize
    # an existing live request card. How: accept the same request-level id that engine
    # task results carry. Purpose: the API contract remains consistent across outbound
    # producers.
    llm_request_id: str | None = None
    delivery_id: str | None = None


class OutboundMessageOut(BaseModel):
    ok: bool = True


class InboundWorkItem(BaseModel):
    inbound_seq: int
    session_id: str
    channel: str = "cli"
    conversation_key: str
    message_id: str | None = None
    text: str
    attachments: list[dict[str, Any]] | None = None
    use_context: bool = True
    entry_node_id: str | None = None
    platform_auth: dict[str, Any] = Field(default_factory=dict)
    route_hints: dict[str, Any] = Field(default_factory=dict)
    memory_hints: dict[str, Any] = Field(default_factory=dict)


class InboundAckIn(BaseModel):
    worker_id: str


class InboundAckOut(BaseModel):
    ok: bool = True


class Task(BaseModel):
    task_id: str
    session_id: str
    session_generation: int = 1
    kind: TaskKind
    node_id: str | None = None
    tool_name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    source_inbound_seq: int | None = None
    caller_task_id: str | None = None
    batch_id: str | None = None
    batch_index: int = 0
    waiting_for_task_id: str | None = None
    status: TaskStatus = TaskStatus.pending
    cancel_requested: bool = False
    preempt_requested: bool = False
    preempted_context_ref: str = ""
    preempt_message: str = ""
    preempt_attachments: list = Field(default_factory=list)
    worker_id: str | None = None
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    # route_schema_version=0 identifies legacy snapshots. Legacy terminal tasks
    # are intentionally treated as already handled during startup migration; only
    # versioned route intents may be replayed.
    route_schema_version: int = 0
    route_status: RouteStatus = RouteStatus.pending
    route_generation: int = 0
    delivery_id: str = ""
    route_context: dict[str, Any] = Field(default_factory=dict)
    # Durable non-critical work intent. It is first persisted in task_routed;
    # legacy routed tasks keep schema 0 and are never post-work replayed.
    post_work_schema_version: int = 0
    post_work_id: str = ""
    post_work_status: str = ""
    post_work_error: str = ""
    post_work_phases: list[dict[str, Any]] = Field(default_factory=list)
    routed_at: datetime | None = None
    route_error: str = ""
    # [AutoC 2026-06-04] Runtime-only task activity tracking. Not persisted.
    # Updated by supervisor when transient events (stream_delta, tool_call_*,
    # approval_*) arrive. Read by GET /v1/admin/tasks/active.
    current_phase: str = ""
    current_detail: str = ""


class TaskCompleteIn(BaseModel):
    worker_id: str
    result: dict[str, Any] = Field(default_factory=dict)


class SessionEventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


HandoffEventIn = SessionEventIn


class ApprovalRequestIn(BaseModel):
    session_id: str
    operation: str
    details: dict[str, Any] = Field(default_factory=dict)
    # [AutoC 2026-05-31] Why: approval prompts now render inside the matching
    # ToolCallCard instead of a standalone block. How: accept optional tool and
    # execution identity fields from callers that know them. Purpose: legacy callers
    # remain valid while new callers can anchor approvals to tool executions.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


class ApprovalDecisionIn(BaseModel):
    decision: Literal["allow", "deny"]
    comment: str | None = None


class Approval(BaseModel):
    approval_id: str
    session_id: str
    operation: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus
    fingerprint: str
    requested_at: datetime
    decided_at: datetime | None = None
    decision: Literal["allow", "deny"] | None = None
    comment: str | None = None
    # [AutoC 2026-05-31] Why: the frontend needs to merge approval state into the
    # existing ToolExecution card. How: include the provider tool_call_id and the
    # execution origin in the approval event payload. Purpose: approval_requested
    # and approval_decided can update the correct tool card instead of creating an
    # independent ApprovalBlock.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


class OpRequestIn(BaseModel):
    session_id: str
    op: Literal["read_file", "write_file", "execute_command", "restart"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    # [AutoC 2026-05-31] Why: policy approvals are requested while a tool is
    # executing. How: carry optional tool_call_id/node_id/task_id through the ops
    # request. Purpose: create_approval can emit enough identity data for the web
    # reducer to update ToolCallCard in place.
    tool_call_id: str | None = None
    node_id: str | None = None
    task_id: str | None = None


class OpRequestOut(BaseModel):
    safety_level: SafetyLevel
    reason: str
    approval_id: str | None = None


class RestartIn(BaseModel):
    target: Literal["engine", "all"]
    reason: str | None = None
    approval_id: str | None = None
    session_id: str | None = None


class RestartOut(BaseModel):
    scheduled: bool
    target: str


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    run_id: str
    workspace_root: str = ""
    started_at: datetime
    uptime_seconds: float


class AdminStateOut(BaseModel):
    sessions: int
    approvals: dict[str, int]
    tasks: dict[str, int] = Field(default_factory=dict)
    pending_approvals: list[Approval] = Field(default_factory=list)
    engine_runtime: dict[str, Any] = Field(default_factory=dict)


class OpenAIConfigSecret(BaseModel):
    # 收下并保留没声明的键。openai 块走 Pydantic、别的块走自由 dict，不收的话
    # supports_vision 这种手写项会在下次 reload 回写时被静默抹掉，只有这一个块会。
    model_config = ConfigDict(extra="allow")

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"


class ActiveProviderSecret(OpenAIConfigSecret):
    """`/v1/config/openai/secret` 的响应形状。

    多一个 provider 字段告诉 engine 该用哪个 provider 类。不把它加进
    OpenAIConfigSecret：那个类型是 AppConfigSecret.openai 的成员，加了字段会让
    data/config.yaml 的 openai 块凭空多 dump 出一个 provider 键。
    """

    provider: str = "openai"


class OpenAIConfigUpdateIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class SystemModelUpdateIn(BaseModel):
    """系统槽位的四项。None 表示不动，空串表示删掉这一项。"""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    provider: str | None = None


class OpenAIConfigPublic(BaseModel):
    # 只报有没有，不报是什么：这个形状走的是 /v1/config 和 /v1/config/openai，
    # 两个都没有令牌门，CLONOTH_HOST 一改就是全网可读。
    base_url: str
    model: str
    api_key_present: bool


class AppConfigSecret(BaseModel):
    version: int = 1
    provider: str = "openai"
    openai: OpenAIConfigSecret = Field(default_factory=OpenAIConfigSecret)


class AppConfigPublic(BaseModel):
    version: int = 1
    provider: str = "openai"
    openai: OpenAIConfigPublic


class ProviderConfigPublic(BaseModel):
    """单个渠道的公开配置（api_key 脱敏）"""
    base_url: str = ""
    model: str = ""
    api_key_present: bool = False
    api_key_redacted: str = ""


class ProvidersResponse(BaseModel):
    """所有渠道配置响应"""
    active_provider: str = "openai"
    providers: dict[str, ProviderConfigPublic] = Field(default_factory=dict)
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    node_fallbacks: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class ProviderModelsIn(BaseModel):
    """列模型时用的地址和密钥。都可以不给：不给就用已经存下的那份。

    页面上改了还没保存时要用改后的值去问，所以不能只读配置。
    base_url 给空串表示「用这家的默认地址」，与不给是两回事。
    """

    base_url: str | None = None
    api_key: str | None = None


class ProviderUpdateIn(BaseModel):
    """更新渠道请求（部分更新）"""
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    # 收不收图片。auto 表示删掉这一项、回去跟随这家的默认；None 表示不动。
    supports_vision: Literal["auto", "yes", "no"] | None = None


class ActiveProviderIn(BaseModel):
    """切换活跃渠道"""
    provider: str


class FallbacksUpdateIn(BaseModel):
    """更新 fallback 链"""
    fallbacks: list[dict[str, Any]]


class NodeFallbacksUpdateIn(BaseModel):
    """更新某个节点的专属 fallback 链。

    fallbacks 为空列表表示这个节点禁用 fallback；要让它回退到全局链请用 DELETE。
    """
    fallbacks: list[dict[str, Any]]


class ConfigReloadOut(BaseModel):
    ok: bool = True
    config: AppConfigPublic
