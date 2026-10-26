"""engine 中途退出时正在跑的任务：声明了 auto_continue 的节点任务有进展就重排一次，其余照旧取消。

进展看三处：挂起时记下的步数、engine 优雅退出时报的步数、对话存储里这个任务写过的 assistant 消息。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clonoth_runtime import load_runtime_config, main_session_enabled
from engine.conversation_store import ConversationStore
from engine.interrupted_calls import INTERRUPTED_RESUME_TYPE
from engine.run_budget import STEPS_USED_KEY, reported_step_count, steps_used

from ._helpers import _now
from .task_store import CancelEffect
from .task_traits import REQUEUE_MARK, is_internal_task, node_auto_continues
from .types import Task, TaskKind, TaskStatus

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

ENGINE_SHUTDOWN = "engine_shutdown"
ENGINE_CRASH = "engine_crash"
ENGINE_RESTART = "engine_restart"


@dataclass(frozen=True)
class Orphan:
    """领取已经作废的 running 任务。重排还是取消，锁外查完进展再定。"""

    task_id: str
    claim_id: str
    storage_session: str
    steps_known: int
    candidate: bool


def storage_session(task: Task) -> str:
    """任务的对话写在哪个会话：子节点写自己的子会话，其余写运行会话（入口分支）。"""
    return str(task.input.get("child_session_id") or "").strip() or str(task.session_id or "").strip()


def requeue_candidate_locked(state: "SupervisorState", task: Task) -> bool:
    """除了有没有进展以外，重排的条件都满足：节点任务、没重排过、没被取消、对话实时落盘、会话还在、节点声明了 auto_continue。"""
    if task.kind != TaskKind.node or not task.node_id:
        return False
    if task.input.get(REQUEUE_MARK) or is_internal_task(task):
        return False
    if task.cancel_requested or state._task_cancel_pending_locked(task):
        return False
    child_session = str(task.input.get("child_session_id") or "").strip()
    # 旧快照模式下历史只在挂起时落盘，从存储重建会丢掉中断前做的事
    if not child_session and not main_session_enabled(load_runtime_config(state.workspace_root)):
        return False
    if task.session_id not in state.sessions and state._session_store.get_entry(task.session_id) is None:
        return False
    if child_session and state._session_store.get_entry(child_session) is None:
        return False
    return node_auto_continues(state.workspace_root, str(task.node_id))


def collect_orphans_locked(
    state: "SupervisorState", *, worker_id: str | None = None, claimed_before: datetime | None = None,
) -> list[Orphan]:
    """领取已经作废的 running 任务：给了 worker_id 只看它名下的，给了 claimed_before 只看那之前领走的。"""
    orphans: list[Orphan] = []
    for task in list(state.tasks.values()):
        if task.status != TaskStatus.running:
            continue
        if worker_id is not None and task.worker_id != worker_id:
            continue
        if claimed_before is not None and task.claimed_at is not None and task.claimed_at > claimed_before:
            continue
        orphans.append(Orphan(
            task_id=task.task_id,
            claim_id=str(task.claim_id or ""),
            storage_session=storage_session(task),
            steps_known=steps_used(task.input),
            candidate=requeue_candidate_locked(state, task),
        ))
    return orphans


def stored_steps(workspace_root: Path | str, orphans: list[Orphan]) -> dict[str, int]:
    """锁外读对话存储：任务写下了几条 assistant 消息，一条算走过一步。只查还不知道进展的重排候选。"""
    store = ConversationStore(Path(workspace_root) / "data" / "conversations")
    found: dict[str, int] = {}
    for orphan in orphans:
        if not orphan.candidate or orphan.steps_known > 0 or not orphan.storage_session:
            continue
        try:
            messages = store.load(orphan.storage_session)
        except Exception:
            log.warning("reading %s to check the progress of task %s failed", orphan.storage_session, orphan.task_id[:12], exc_info=True)
            continue
        found[orphan.task_id] = sum(
            1 for message in messages
            if message.role == "assistant" and str(message.source_task_id or "") == orphan.task_id
        )
    return found


def requeue_locked(state: "SupervisorState", task: Task, *, reason: str, steps: int, now: datetime) -> None:
    """放回队列从存储接着跑：resume_data 标成 interrupted，engine 会给中断那一轮补上「中断，结果未知」。"""
    claim = str(task.claim_id or "")
    task.status = TaskStatus.pending
    task.worker_id = None
    task.lease_expires_at = None
    task.updated_at = now
    task.queued_at = now
    # 作废那次领取迟到的回报按重复处理，不会把重排后的任务又改回终态
    if claim:
        task.completed_claim_id = claim
    task.result = {}
    task.input[STEPS_USED_KEY] = max(steps_used(task.input), max(0, int(steps)))
    task.input[REQUEUE_MARK] = {"reason": reason, "at": now.isoformat(), "claim_id": claim}
    task.input["resume_data"] = {"type": INTERRUPTED_RESUME_TYPE, "reason": reason}
    log.warning(
        "requeued interrupted task %s (node=%s, reason=%s, steps=%d)",
        task.task_id[:12], task.node_id, reason, task.input[STEPS_USED_KEY],
    )
    state._event_task_snapshot("task_requeued", task)


def settle_orphans_locked(
    state: "SupervisorState",
    orphans: list[Orphan],
    progress: dict[str, int],
    *,
    reason: str,
    cancel_reason: str,
    now: datetime | None = None,
) -> tuple[int, int]:
    """重排或取消，返回 (重排几个, 取消几个)。锁外那段时间里状态或领取变了的任务不动。"""
    moment = now or _now()
    requeued = cancelled = 0
    for orphan in orphans:
        task = state.tasks.get(orphan.task_id)
        if task is None or task.status != TaskStatus.running or str(task.claim_id or "") != orphan.claim_id:
            continue
        steps = max(orphan.steps_known, progress.get(orphan.task_id, 0))
        if orphan.candidate and steps > 0 and requeue_candidate_locked(state, task):
            requeue_locked(state, task, reason=reason, steps=steps, now=moment)
            requeued += 1
            continue
        # 父在子前：子任务结算时父已是终态，不会被先唤醒再取消
        for target in (task, *state._live_descendants_locked(task.task_id)):
            if state._cancel_task_locked(target, moment, force=True, reason=cancel_reason) is not CancelEffect.none:
                cancelled += 1
    return requeued, cancelled


def release_stale_claims_locked(state: "SupervisorState", worker_id: str) -> None:
    """这个 worker 名下不在跑的任务照常留着，只把作废的领取信息清掉：排队的马上能被领走。"""
    for task in state.tasks.values():
        if task.worker_id == worker_id and task.status in (TaskStatus.pending, TaskStatus.suspended):
            task.worker_id = None
            task.lease_expires_at = None


def requeue_after_shutdown_locked(state: "SupervisorState", task: Task, result: Any) -> bool:
    """engine 优雅退出时报回来的取消：有进展的 auto_continue 任务改为重排，返回重排了没有。"""
    if not isinstance(result, dict) or str(result.get("interrupted") or "") != ENGINE_SHUTDOWN:
        return False
    steps = max(steps_used(task.input), reported_step_count(result))
    if steps <= 0 or not requeue_candidate_locked(state, task):
        return False
    requeue_locked(state, task, reason=ENGINE_SHUTDOWN, steps=steps, now=_now())
    return True
