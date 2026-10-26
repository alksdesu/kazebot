"""审批授权用例共用的身份、任务和审批：supervisor 状态、HTTP 接口和 SDK 的用例都从这里挂待审批、批出授权。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config_layout  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Approval, ApprovalGrant, OpRequestOut, SafetyLevel, Task, TaskKind  # noqa: E402

ADMIN = "10001"
OTHER_ADMIN = "10002"
BOT = "90001"
QQ_KEY = "qq_private:bc3f12b0621298e191a49fa7"
START = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
# 内置策略里改 engine/** 要审批且算敏感：管理员任务也不会自动放行，正好用来挂待审批。
SENSITIVE_WRITE = {"path": "engine/runner.py"}


class Clock:
    """顶替 supervisor.state._now，授权的签发和到期都按它算。"""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def events_path(workspace: Path) -> Path:
    return workspace / "data" / "events.jsonl"


def make_state(workspace: Path, *, run_id: str = "run-grant") -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(events_path(workspace), run_id=run_id),
        policy=PolicyEngine(workspace_root=workspace),
    )


def write_policy(workspace: Path, grant: dict[str, Any] | None) -> None:
    """写一份只要求 engine/** 审批的策略；grant 为 None 时不写 approval_grant 段。"""
    path = config_layout.policy_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "version": 1,
        "write_file": {"default": "auto", "rules": [
            {"pattern": "engine/**", "decision": "approval_required", "reason": "engine source"},
        ]},
    }
    if grant is not None:
        body["approval_grant"] = grant
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def qq_auth(user_id: str = ADMIN, *, is_admin: bool = True, bot_scope: str = BOT) -> dict[str, Any]:
    """QQ 适配器入站时写进 task_context 的 platform_auth。"""
    return {
        "platform": "qq", "user_id": user_id, "is_admin": is_admin,
        "group_role": "", "group_scope": "", "bot_scope": bot_scope,
    }


def qq_approver(user_id: str = ADMIN, bot_scope: str = BOT, platform: str = "qq") -> dict[str, str]:
    return {"platform": platform, "user_id": user_id, "bot_scope": bot_scope}


def start_task(
    state: SupervisorState,
    *,
    auth: dict[str, Any] | None,
    key: str = QQ_KEY,
    channel: str = "qq_private",
    caller_task_id: str | None = None,
    extra_context: dict[str, Any] | None = None,
) -> tuple[str, Task]:
    sid = state.get_or_create_session(channel=channel, conversation_key=key)
    task_context: dict[str, Any] = {"conversation_key": key, "channel": channel, **(extra_context or {})}
    if auth is not None:
        task_context["platform_auth"] = auth
    with state._lock:
        task = state._create_task_locked(
            session_id=sid,
            session_generation=1,
            kind=TaskKind.node,
            node_id="qq.orchestrator",
            input_data={"instruction": "do it", "task_context": task_context},
            continuation={},
            source_inbound_seq=None,
            caller_task_id=caller_task_id,
        )
    return sid, task


def request_op(
    state: SupervisorState,
    sid: str,
    task_id: str | None,
    op: str = "write_file",
    parameters: dict[str, Any] | None = None,
) -> OpRequestOut:
    return state.request_operation(
        session_id=sid, op=op, parameters=dict(parameters or SENSITIVE_WRITE), task_id=task_id,
    )


def pending_approval(state: SupervisorState, user_id: str = ADMIN) -> str:
    """user_id 发起的任务撞上一条要审批的操作，返回挂起的审批 ID。"""
    sid, task = start_task(state, auth=qq_auth(user_id))
    out = request_op(state, sid, task.task_id)
    assert out.safety_level == SafetyLevel.approval_required, out
    assert out.approval_id
    return out.approval_id


def approve(
    state: SupervisorState,
    approval_id: str,
    *,
    approver: dict[str, Any] | None = None,
    minutes: int | None = None,
) -> Approval | None:
    return state.decide_approval(
        approval_id=approval_id, decision="allow", comment="ok", approver=approver, grant_minutes=minutes,
    )


def grant_for(state: SupervisorState, *, user_id: str = ADMIN, minutes: int | None = None) -> ApprovalGrant:
    """user_id 同意自己发起的一条请求，返回由此生成的授权。"""
    approval = approve(state, pending_approval(state, user_id), approver=qq_approver(user_id), minutes=minutes)
    assert approval is not None and approval.grant is not None, approval
    return approval.grant


def logged_events(workspace: Path, type_: str) -> list[dict[str, Any]]:
    path = events_path(workspace)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == type_]
