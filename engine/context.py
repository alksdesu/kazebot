from __future__ import annotations

from dataclasses import dataclass, field
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

# 桥接信号系统：让所有 emit_event 调用自动走 SignalBus
from engine.signals import Signal, get_bus

logger = logging.getLogger(__name__)


@dataclass
class TaskLease:
    """worker 对一个任务的领取凭据。心跳被 supervisor 拒绝后，本地按用户取消同样的路径收手。"""

    task_id: str
    worker_id: str
    claim_id: str = ""
    lost_reason: str = ""
    # 含挂起前各段的累计步数；engine 优雅退出时带给 supervisor，判这个任务有没有进展
    steps_completed: int = 0

    @property
    def lost(self) -> bool:
        return bool(self.lost_reason)


@dataclass(frozen=True)
class ServingProvider:
    """实际出答案的那个 provider：模型名、provider 类型、渠道块名。"""

    model: str = ""
    provider: str = ""
    channel: str = ""

    @classmethod
    def of(cls, provider: Any) -> "ServingProvider":
        return cls(
            model=str(getattr(provider, "model", "") or ""),
            provider=str(getattr(provider, "name", "") or ""),
            channel=str(getattr(provider, "plugin_channel", "") or ""),
        )


@dataclass
class RunContext:
    """单个 task 执行片段的运行上下文。"""

    workspace_root: Path
    supervisor_url: str
    session_id: str
    worker_id: str
    http: httpx.AsyncClient
    llm_http: httpx.AsyncClient
    # 文件工具、命令和项目说明按它找文件；实例数据和配置仍按 workspace_root 找。没给就是 workspace_root
    project_root: Path | None = None
    # 隔离在 git worktree 里的任务：项目根在原仓库里对应的目录。不是隔离任务为 None
    project_origin: Path | None = None
    # [Fork/Merge 2026-05-12] parent_session_id separates event routing from runtime storage.
    # Why: entry tasks now run on a branch session, but SDK adapters know the
    # user-facing conversation_key through the parent session. How: keep
    # session_id as the ConversationStore/runtime session and use this optional
    # parent for supervisor event POSTs. Purpose: branch history stays isolated
    # while events still reach the channel-level SDK route.
    parent_session_id: str = ""
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-4o-mini"
    user_text: str = ""
    task_id: str = ""
    session_generation: int = 0
    source_inbound_seq: int | None = None
    task_context: dict = field(default_factory=dict)
    # Child Session 隔离：dispatch 子节点使用独立 session ID。
    # 非空时，_shadow_write 和 ConversationStore 操作写入此 session 而非 parent session_id。
    # 主节点和无 child session 的场景下为空字符串。
    child_session_id: str = ""
    # P0 Task 内核化：task 级数据采集字段
    tool_call_log: list = field(default_factory=list)   # [{name: str, args_summary: str}]
    total_usage: dict = field(default_factory=dict)      # accumulated {prompt_tokens, completion_tokens, total_tokens}
    first_shadow_message_id: str = ""   # UUID of first message written to ConversationStore
    last_shadow_message_id: str = ""    # UUID of last message written to ConversationStore
    completed_steps: int = 0             # actual step count from ai_step loop
    # 前几段挂起前已经用掉的步数：completed_steps 从它接着数，TaskRecord 只记本段的差值
    step_offset: int = 0
    # 备用渠道顶上时答案不是节点 provider 出的，TaskRecord 按最后一次成功的那个记模型和渠道
    serving_provider: ServingProvider | None = None
    # [AutoC 2026-06-04] Why: one visible streaming card must map to one concrete LLM
    # request, not to the whole task. How: keep the current request id on RunContext
    # and let emit_event copy it into stream/tool/outbound-related payloads. Purpose:
    # frontend reducers can replace the exact card that produced the request.
    current_llm_request_id: str = ""
    llm_request_index: int = 0
    lease: TaskLease | None = None

    def __post_init__(self) -> None:
        if not self.project_root:
            self.project_root = self.workspace_root

    @property
    def claim_id(self) -> str:
        return self.lease.claim_id if self.lease is not None else ""

    def begin_llm_request(self) -> str:
        """Start a new request-scoped id for one provider call."""
        # [AutoC 2026-06-04] Why: task_id is too coarse because one task can make
        # several provider requests. How: combine task id, a monotonic per-task counter,
        # and a short uuid suffix. Purpose: every stream card has a stable backend key
        # for final replacement without depending on event order heuristics.
        self.llm_request_index += 1
        base = self.task_id or self.session_id or "llm"
        self.current_llm_request_id = f"{base}:llm:{self.llm_request_index}:{uuid.uuid4().hex[:8]}"
        return self.current_llm_request_id

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> bool:
        """把事件交给 supervisor，返回它有没有收下。"""
        from .conversation_routing import routing_meta
        for key, value in routing_meta(self.task_context).items():
            payload.setdefault(key, value)
        if self.source_inbound_seq is not None:
            payload.setdefault("source_inbound_seq", self.source_inbound_seq)
        if self.current_llm_request_id:
            # 实时流、工具事件和之后的 outbound_message 靠它对上同一次模型请求
            payload.setdefault("llm_request_id", self.current_llm_request_id)
        # 入口分支上跑的任务，事件发到父会话：SDK 的会话映射只认面向用户的那个
        route_session_id = self.parent_session_id or self.session_id
        if self.parent_session_id and self.parent_session_id != self.session_id:
            payload.setdefault("parent_session_id", self.parent_session_id)
            payload.setdefault("branch_session_id", self.session_id)
        # Bridge emit_event → SignalBus，用 dict(payload) 复制避免后续修改影响信号订阅方
        get_bus().emit(Signal(name=event_type, payload=dict(payload)))
        try:
            response = await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{route_session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception as exc:
            logger.debug("event %s not delivered: %s", event_type, exc)
            return False
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 200 <= status < 300:
            return True
        logger.debug("event %s rejected by supervisor: HTTP %s", event_type, status)
        return False

    async def check_cancelled(self) -> bool:
        if self.lease is not None and self.lease.lost:
            return True
        # 显式 2s 超时：轮询每 0.2-0.3s 一次，supervisor 卡住时不能把取消检测拖上几十秒，超时当没取消、下次再问
        try:
            if self.task_id:
                options: dict[str, Any] = {"timeout": 2.0}
                if self.claim_id:
                    options["params"] = {"claim_id": self.claim_id}
                r = await self.http.get(f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled", **options)
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/sessions/{self.session_id}/cancelled",
                    timeout=2.0,
                )
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def check_preempted(self) -> dict:
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempted"
                )
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {"preempted": False, "message": "", "attachments": []}

    async def consume_preempt(self, revision: int | None = None) -> None:
        """通知 supervisor 已消费 preempt message，防止重复注入。"""
        try:
            if self.task_id:
                await self.http.post(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempt_consumed",
                    json={"revision": revision} if revision is not None else {},
                )
        except Exception:
            pass
