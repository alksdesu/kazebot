from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .task_lanes import TaskLane, derive_lane, parse_lane


OUTPUT_GUIDE_MAX_SECTIONS = 32
OUTPUT_GUIDE_SECTION_MAX_CHARS = 32_000
OUTPUT_GUIDE_MAX_CHARS = 64_000


class OutputGuideSection(BaseModel):
    """平台输出说明的一段。volatile 的每轮会变，引擎放进动态区；其余进缓存前缀。"""

    key: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=OUTPUT_GUIDE_SECTION_MAX_CHARS)
    volatile: bool = False


class OutputGuide(BaseModel):
    """适配器按平台能力声明生成的输出说明，随入站送来，只进提示词不进对话历史。"""

    version: int = 1
    sections: list[OutputGuideSection] = Field(default_factory=list, max_length=OUTPUT_GUIDE_MAX_SECTIONS)

    @model_validator(mode="after")
    def _bounded(self) -> "OutputGuide":
        if sum(len(section.text) for section in self.sections) > OUTPUT_GUIDE_MAX_CHARS:
            raise ValueError(f"output_guide is longer than {OUTPUT_GUIDE_MAX_CHARS} characters")
        return self

    def task_payload(self) -> dict[str, Any]:
        """存进 task_context 的形状，没有正文的段不存。"""
        return {
            "version": self.version,
            "sections": [section.model_dump() for section in self.sections if section.text.strip()],
        }


def output_guide_payload(raw: Any) -> dict[str, Any] | None:
    """规整成存进任务的形状；形状不对、或者一段正文都没有时是 None。"""
    if not raw:
        return None
    try:
        guide = raw if isinstance(raw, OutputGuide) else OutputGuide.model_validate(raw)
    except ValidationError:
        return None
    payload = guide.task_payload()
    return payload if payload["sections"] else None


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
    # such as OneBot, not parsed from user text.
    platform_auth: dict[str, Any] = Field(default_factory=dict)
    # Adapter-provided routing hints, e.g. has_image/channel/platform. These are
    # trusted platform metadata and are used only for capability routing.
    route_hints: dict[str, Any] = Field(default_factory=dict)
    # 本轮涉及的人（{"subjects": [匿名别名]}），用于按人加载跨会话长期记忆。
    # 与 platform_auth 分开：后者参与鉴权，不该混入非鉴权字段。
    memory_hints: dict[str, Any] = Field(default_factory=dict)
    output_guide: OutputGuide | None = None
    # 没收到应答时调用方带着同一个 id 重发（异步委派就是这样）：同一段对话里收下过的不再建第二份，回第一次的应答
    delivery_id: str | None = Field(default=None, max_length=128)


class InboundMessageOut(BaseModel):
    session_id: str
    inbound_seq: int = 0
    accepted: bool = True
    # 这条消息建出的入口任务；内容为空没建任务时为空串。异步委派拿它和 session_id 拼出句柄
    task_id: str = ""
    # 这是一次重发：前面三项是第一次收下时的应答
    deduped: bool = False


class Task(BaseModel):
    task_id: str
    session_id: str
    session_generation: int = 1
    kind: TaskKind
    node_id: str | None = None
    tool_name: str | None = None
    # 建任务时定下、随快照落盘：重排回队列的任务照旧排原来那一道，不按领取时的情形现算
    lane: TaskLane = TaskLane.interactive
    input: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    source_inbound_seq: int | None = None
    caller_task_id: str | None = None
    batch_id: str | None = None
    batch_index: int = 0
    waiting_for_task_id: str | None = None
    waiting_for_batch_id: str | None = None
    status: TaskStatus = TaskStatus.pending
    cancel_requested: bool = False
    preempt_requested: bool = False
    preempted_context_ref: str = ""
    preempt_message: str = ""
    preempt_attachments: list = Field(default_factory=list)
    preempt_memory_hints: dict[str, Any] | None = None
    # 没带是沿用任务原有的输出说明；带了一段都没有的就是清掉。
    preempt_output_guide: dict[str, Any] | None = None
    preempt_revision: int = 0
    worker_id: str | None = None
    # 每次领取换一个；complete / renew_lease / cancelled 带回来比对，旧领取的回报一律作废。
    claim_id: str = ""
    # 已受理过完成回报的那次领取，同一领取重发的 complete 据此认作重复。
    completed_claim_id: str = ""
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None = None
    claimed_at: datetime | None = None
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

    @model_validator(mode="before")
    @classmethod
    def _lane_for_legacy_snapshot(cls, data: Any) -> Any:
        """旧快照、旧事件没有 lane，或者写的是认不出的值：按任务自己的 input 推导。"""
        if not isinstance(data, dict) or parse_lane(data.get("lane")) is not None:
            return data
        return {**data, "lane": derive_lane(data.get("input"))}


class TaskCompleteIn(BaseModel):
    worker_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    claim_id: str | None = None


class TaskLeaseRenewIn(BaseModel):
    worker_id: str = ""
    lease_sec: float = 120.0
    claim_id: str | None = None


class ToolsReloadIn(BaseModel):
    """让每个 engine worker 重扫脚本工具。"""
    # 重开会话会丢掉有状态服务在各对话里攒下的状态，只在点名要时才让 MCP 一起重来
    mcp: bool = False


class TaskOriginOut(BaseModel):
    """任务的发起来源：派发出去的子任务回溯到发起会话和入口任务的已校验身份。"""

    task_id: str
    origin_task_id: str
    conversation_key: str = ""
    channel: str = ""
    platform_auth: dict[str, Any] = Field(default_factory=dict)


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


class PolicyUpdateIn(BaseModel):
    """整份替换 policy。部分更新会让"删掉一条规则"没法表达。"""

    policy: dict[str, Any]
    # 打开编辑器时读到的指纹。给了就先比对，中途被共享组里别的实例改过就 409，一个字节都不写。
    base_hash: str | None = None


class AccountSwitchIn(BaseModel):
    """切到某个此前登录过的号。没有缓存登录态的号只能扫码。"""

    account: str


class QqQuickLoginIn(BaseModel):
    """改路径之前的 QQ 切号请求，账号字段叫 uin。"""

    uin: str


class ApproverIdentity(BaseModel):
    """审批人的平台身份。和请求发起人是同一个人时，同意才会生成授权。"""

    model_config = ConfigDict(frozen=True)

    platform: str = Field(default="", max_length=64)
    user_id: str = Field(default="", max_length=128)
    bot_scope: str = Field(default="", max_length=128)


class ApprovalGrant(BaseModel):
    """同意审批时给审批人本人的授权：until 之前他发起的任务里，原本要审批的操作直接放行。

    永久授权 until 为空、minutes 为 0，只给管理员名单里的人，收回之前一直有效。
    """

    model_config = ConfigDict(frozen=True)

    grant_id: str
    platform: str
    user_id: str
    bot_scope: str = ""
    # 必须写出来：缺了这一项不能当成永久。
    until: datetime | None
    minutes: int
    approval_id: str
    issued_at: datetime | None = None
    permanent: bool = False

    @field_validator("until", "issued_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        # 到期判断拿带时区的当前时间比，不带时区的一比就抛 TypeError。
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _permanent_has_no_until(self) -> "ApprovalGrant":
        # 两项对不上的一律不认：until 写成空不能就此变成永久，标了永久的也不能带着到期时间。
        if self.permanent != (self.until is None):
            raise ValueError("a permanent grant has no until, a timed grant needs one")
        return self


class ApprovalDecisionIn(BaseModel):
    decision: Literal["allow", "deny"]
    comment: str | None = None
    # 控制台、TUI 和旧客户端不带审批人，同意了也不生成授权。
    approver: ApproverIdentity | None = None
    # None 按 policy 的 approval_grant.default_minutes；0 只同意这一次。
    grant_minutes: int | None = Field(default=None, ge=0)
    # 要永久授权。够不上（不在管理员名单里、策略关了）就按时长上限给限时的。
    grant_permanent: bool = False

    @model_validator(mode="after")
    def _one_duration(self) -> "ApprovalDecisionIn":
        if self.grant_permanent and self.grant_minutes is not None:
            raise ValueError("grant_permanent and grant_minutes cannot be given together")
        return self


class ApprovalGrantRevokeIn(BaseModel):
    """按 grant_id 收回一条，或按身份收回这个人名下全部生效中的；两样都给时要同时对上。"""

    grant_id: str | None = Field(default=None, max_length=64)
    identity: ApproverIdentity | None = None


class ApprovalGrantListOut(BaseModel):
    grants: list[ApprovalGrant] = Field(default_factory=list)


class ApprovalGrantRevokeOut(BaseModel):
    revoked: list[ApprovalGrant] = Field(default_factory=list)
    count: int = 0


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
    # 旧事件里没有这三项，按没有授权处理。
    approver: ApproverIdentity | None = None
    grant: ApprovalGrant | None = None
    # 同意了却没生成授权的原因：once / disabled / not_requester / no_requester / no_approver；
    # 要了永久却只给了限时的原因：permanent_not_admin / permanent_disabled。
    grant_note: str | None = None
    # 没人定案、随任务失效而被系统收掉时给人看的原因；人批的、toolbox 等审批超时收的都没有这一项
    expired_reason: str | None = None


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


# 重启没排上的原因。restart_failed 事件和 RestartOut.reason 用的是同一组，控制台按它换成中文。
RESTART_NO_PROCESS_MANAGER = "no process manager"
RESTART_IN_PROGRESS = "restart already in progress"


class RestartInProgress(BaseModel):
    target: str
    since: datetime


class RestartOut(BaseModel):
    scheduled: bool
    target: str
    reason: str | None = None
    # 因为已有一次在跑而没排上时，给出是哪一次、几点开始的。
    in_progress: RestartInProgress | None = None


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
    config/models.yaml 的 openai 块凭空多 dump 出一个 provider 键。
    """

    provider: str = "openai"
    channel_name: str = ""
    # 活跃渠道块里手写的 options 原样给出：跟随主渠道的节点要和地址、密钥从同一份配置拿。
    options: dict[str, Any] = Field(default_factory=dict)


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


class ImageDefaultChannelIn(BaseModel):
    """配了多个生图渠道时默认用哪个。空串 = 交给模型按用途判断。"""

    default_channel: str = ""


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
    # runtime 的 shell.entry_node_id：网页替用户挑入口时优先挑它
    default_entry_node_id: str = ""


class ProviderConfigPublic(BaseModel):
    """单个渠道的公开配置（api_key 脱敏）"""
    base_url: str = ""
    model: str = ""
    api_key_present: bool = False
    api_key_redacted: str = ""
    # 块名只是名字，这个才决定请求按谁的格式发。没写就等于块名。
    type: str = ""
    type_explicit: bool = False
    label: str = ""


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
    # 从某个系统槽位发起时填它。槽位自己那把密钥问不到渠道块里去，不带这个就会拿错。
    slot: str | None = None


class ProviderUpdateIn(BaseModel):
    """更新渠道请求（部分更新）"""
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    # 收不收图片。auto 表示删掉这一项、回去跟随这家的默认；None 表示不动。
    supports_vision: Literal["auto", "yes", "no"] | None = None
    # 线格式。空串表示删掉这一项、回去按块名猜；None 表示不动。
    type: str | None = None
    label: str | None = None


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


class DreamRunIn(BaseModel):
    """手动触发一次记忆整理。"""
    # 整理要跑几分钟，触发的人早就不在那一轮对话里了；给了会话键才知道把结果回报到哪。
    notify_conversation_key: str = ""
    notify_channel: str = ""


class DreamRunOut(BaseModel):
    ok: bool
    # started=已启动；busy=上一轮还在跑；unavailable=引擎侧没接住
    status: str


class ConfigReloadOut(BaseModel):
    ok: bool = True
    config: AppConfigPublic
