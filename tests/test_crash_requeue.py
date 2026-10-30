"""engine 中途退出时手里的任务：排队和挂起的留着，声明了 auto_continue、已有进展的重排一次并从存储接着跑，
其余按失败收尾并写明是引擎重启打断的，系统内部流程和群里没人叫 bot 的插话照旧静默取消。"""
from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_requeue import (  # noqa: E402
    DELEGATE_INTERRUPTED_ERROR,
    USER_INTERRUPTED_ERROR,
    OrphanSettlement,
    collect_orphans_locked,
    settle_orphans_locked,
    storage_session,
)
from supervisor.task_store import CompletionOutcome  # noqa: E402
from supervisor.task_traits import REQUEUE_MARK  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_private:7"
WORKER = "worker.coder"
CHAT = "chat.entry"
SHUTDOWN = {"action": "cancelled", "error": "engine graceful shutdown", "interrupted": "engine_shutdown"}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / f"{WORKER}.yaml").write_text("type: ai\nname: worker\nauto_continue: true\nprompt: Work.\n", encoding="utf-8")
    (nodes / f"{CHAT}.yaml").write_text("type: ai\nname: chat\nprompt: Chat.\n", encoding="utf-8")
    return tmp_path


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-crash-requeue"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="qq_private", conversation_key=CONV)


def _inbound(state: SupervisorState, session_id: str, *, node: str = WORKER, **extra: Any) -> Task:
    payload = {
        "channel": "qq_private",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "整理一下日志模块",
        "entry_node_id": node,
        **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _claim(state: SupervisorState, task: Task, worker: str = "engine-0") -> str:
    claimed = state.assign_next_task(worker_id=worker)
    assert claimed is not None and claimed["task_id"] == task.task_id
    return str(claimed["claim_id"])


def _running(
    state: SupervisorState, session_id: str, node: str, *, claim: str, claimed_at: datetime | None = None,
) -> Task:
    """直接摆出一个 engine-0 领走、正在跑的任务。"""
    now = datetime.now(timezone.utc)
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id, session_generation=1, kind=TaskKind.node, node_id=node,
            input_data={"instruction": "x"}, continuation={}, source_inbound_seq=None,
        )
    task.status = TaskStatus.running
    task.worker_id = "engine-0"
    task.claim_id = claim
    task.claimed_at = claimed_at or now
    task.lease_expires_at = now + timedelta(minutes=2)
    return task


def _progress(workspace: Path, task: Task, steps: int, *, author: str | None = None) -> None:
    """往任务的对话里写 steps 条 assistant 消息，一条算走过一步。"""
    store = ConversationStore(workspace / "data" / "conversations")
    store.append_batch(storage_session(task), [
        Message(id=f"{author or task.task_id}-{index}", role="assistant", content=f"step {index}", source_task_id=author or task.task_id)
        for index in range(steps)
    ])


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


def _async_child(state: SupervisorState, session_id: str, caller: Task) -> Task:
    """caller 异步派出去的子任务：一条带 dispatch_origin 的入站，在自己的 agent: 会话里跑。"""
    agent_sid = state.get_or_create_session(channel="qq_private", conversation_key=f"agent:{CHAT}:{CONV}:{uuid.uuid4()}")
    return _inbound(
        state, agent_sid, node=CHAT, dispatch_context_mode="fresh",
        dispatch_origin={
            "parent_session_id": session_id, "caller_node_id": str(caller.node_id or ""), "parent_conversation_key": CONV,
            "context_mode": "fresh", "caller_task_id": caller.task_id,
        },
    )


def _callbacks(state: SupervisorState) -> list[Task]:
    # 后台路由线程可能正在往任务表里加任务
    with state._lock:
        return [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]


def _finish_on(state: SupervisorState, task: Task, worker: str, text: str = "done") -> None:
    task.status = TaskStatus.running
    task.worker_id = worker
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id=worker, result={"action": "finish", "result": {"text": text}})


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """入口分支上的任务被判失败后，路由在后台线程里跑完。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


class TestWorkerRestart:
    def test_queued_and_suspended_tasks_survive(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        caller = _inbound(state, _session(state))
        claim = _claim(state, caller)
        state.complete_task(task_id=caller.task_id, worker_id="engine-0", claim_id=claim, result={
            "action": "dispatch",
            "dispatch_batch": [{"kind": "node", "target": CHAT, "instruction": "查一下", "context_mode": "fresh"}],
            "_step_count": 4,
        })
        [child] = [task for task in state.tasks.values() if task.caller_task_id == caller.task_id]
        assert caller.status == TaskStatus.suspended and caller.worker_id is None
        assert caller.input["_steps_used"] == 4

        result = state.register_engine("engine-0", "gen-2")

        assert result["ok"] is True
        assert result["orphans_cancelled"] == 0 and result["orphans_requeued"] == 0 and result["orphans_failed"] == 0
        assert caller.status == TaskStatus.suspended
        assert child.status == TaskStatus.pending

    def test_a_queued_task_held_by_the_old_lease_can_be_claimed_at_once(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        queued = _inbound(state, _session(state))
        queued.worker_id = "engine-0"
        queued.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        state.register_engine("engine-0", "gen-2")

        assert queued.status == TaskStatus.pending
        assert queued.worker_id is None and queued.lease_expires_at is None
        assert state.assign_next_task(worker_id="engine-1")["task_id"] == queued.task_id

    def test_a_running_auto_continue_task_with_progress_is_requeued_once(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _inbound(state, _session(state))
        old_claim = _claim(state, task)
        _progress(workspace, task, 2)
        _progress(workspace, task, 3, author="another-task")

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_requeued"] == 1 and result["orphans_cancelled"] == 0 and result["orphans_failed"] == 0
        assert task.status == TaskStatus.pending
        assert task.worker_id is None and task.lease_expires_at is None
        assert task.result == {}
        assert task.input["_steps_used"] == 2
        assert task.input["resume_data"] == {"type": "interrupted", "reason": "engine_crash"}
        assert task.input[REQUEUE_MARK]["reason"] == "engine_crash"
        assert task.input[REQUEUE_MARK]["claim_id"] == old_claim
        assert task.completed_claim_id == old_claim

        late = state.complete_task(
            task_id=task.task_id, worker_id="engine-0", claim_id=old_claim,
            result={"action": "finish", "result": {"text": "旧进程的结果"}},
        )
        assert late is not None and late.outcome is CompletionOutcome.duplicate
        assert task.status == TaskStatus.pending
        assert "旧进程的结果" not in _outbound_texts(state)

        reclaimed = state.assign_next_task(worker_id="engine-0")
        assert reclaimed is not None and reclaimed["task_id"] == task.task_id
        assert reclaimed["claim_id"] != old_claim
        assert reclaimed["input"]["resume_data"]["type"] == "interrupted"
        assert reclaimed["input"]["_steps_used"] == 2

        again = state.register_engine("engine-0", "gen-3")

        assert again["orphans_requeued"] == 0 and again["orphans_failed"] == 1 and again["orphans_cancelled"] == 0
        assert task.status == TaskStatus.failed
        assert task.result["error"] == USER_INTERRUPTED_ERROR

    def test_a_task_without_progress_fails_with_the_restart_as_its_reason(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _inbound(state, _session(state))
        _claim(state, task)
        ConversationStore(workspace / "data" / "conversations").append_batch(task.session_id, [
            Message(id="u-1", role="user", content="整理一下日志模块", source_task_id=task.task_id),
        ])

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_requeued"] == 0 and result["orphans_failed"] == 1 and result["orphans_cancelled"] == 0
        assert task.status == TaskStatus.failed
        assert task.result["error"] == USER_INTERRUPTED_ERROR

    def test_a_node_without_auto_continue_fails_and_its_descendants_are_cancelled(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        sid = _session(state)
        task = _running(state, sid, CHAT, claim="c-1")
        _progress(workspace, task, 3)
        with state._lock:
            child = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id=WORKER,
                input_data={"instruction": "y"}, continuation={}, source_inbound_seq=None, caller_task_id=task.task_id,
            )

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_requeued"] == 0 and result["orphans_failed"] == 1 and result["orphans_cancelled"] == 1
        assert task.status == TaskStatus.failed and child.status == TaskStatus.cancelled
        assert f"[错误] {USER_INTERRUPTED_ERROR}" in _outbound_texts(state)

    def test_without_the_conversation_store_nothing_is_requeued(self, workspace: Path) -> None:
        # 旧快照模式下中断前做的事没落盘，从存储接着跑会丢掉它们
        (workspace / "config" / "runtime.yaml").write_text(
            "engine:\n  child_session:\n    main_session_enabled: false\n", encoding="utf-8",
        )
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _running(state, _session(state), WORKER, claim="c-1")
        _progress(workspace, task, 3)

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_failed"] == 1
        assert task.status == TaskStatus.failed

    def test_system_tasks_are_never_requeued_and_are_cancelled_quietly(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _running(state, _session(state), WORKER, claim="c-1")
        task.input["_system_task"] = True
        task.input["_steps_used"] = 5

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_cancelled"] == 1 and result["orphans_failed"] == 0
        assert task.status == TaskStatus.cancelled
        assert _outbound_texts(state) == []

    def test_an_interjection_nobody_asked_for_is_cancelled_quietly(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _inbound(state, _session(state), node=CHAT, route_hints={"delivery_purpose": "ambient"})
        _claim(state, task)

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_cancelled"] == 1 and result["orphans_failed"] == 0
        assert task.status == TaskStatus.cancelled

    def test_an_interrupted_wait_child_comes_back_to_its_caller_as_a_failure_it_can_continue(
        self, workspace: Path,
    ) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        caller = _inbound(state, _session(state), node=CHAT)
        claim = _claim(state, caller)
        state.complete_task(task_id=caller.task_id, worker_id="engine-0", claim_id=claim, result={
            "action": "dispatch",
            "dispatch_batch": [{"kind": "node", "target": CHAT, "instruction": "查一下", "context_mode": "fresh", "call_id": "w1"}],
        })
        [child] = [task for task in state.tasks.values() if task.caller_task_id == caller.task_id]
        _claim(state, child)

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_failed"] == 1 and result["orphans_cancelled"] == 0
        assert child.status == TaskStatus.failed
        assert caller.status == TaskStatus.pending
        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "fail" and entry["error"] == DELEGATE_INTERRUPTED_ERROR
        assert entry["continuable"] is True and entry["call_id"] == "w1"

    def test_an_interrupted_async_dispatch_tells_its_dispatcher_it_failed(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        sid = _session(state)
        caller = _inbound(state, sid, node=CHAT)
        _finish_on(state, caller, "engine-0", "交出去了")
        child = _async_child(state, sid, caller)
        _claim(state, child)

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_failed"] == 1
        assert child.status == TaskStatus.failed
        assert _wait_until(lambda: bool(_callbacks(state)))
        [callback] = _callbacks(state)
        assert callback.node_id == CHAT
        assert callback.input["inbound_child_status"] == "fail"
        assert DELEGATE_INTERRUPTED_ERROR in callback.input["instruction"]
        assert USER_INTERRUPTED_ERROR not in "\n".join(_outbound_texts(state))

    def test_results_held_for_an_interrupted_round_are_handed_back(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        sid = _session(state)
        round_task = _running(state, sid, CHAT, claim="c-1")
        child = _async_child(state, sid, round_task)
        _finish_on(state, child, "engine-1", "查到了")
        assert _callbacks(state) == []

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_failed"] == 1 and round_task.status == TaskStatus.failed
        [callback] = _callbacks(state)
        assert callback.node_id == CHAT and callback.input["inbound_child_task_id"] == child.task_id

    def test_the_same_generation_registering_again_changes_nothing(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _running(state, _session(state), WORKER, claim="c-1")

        result = state.register_engine("engine-0", "gen-1")

        assert result["orphans_cancelled"] == 0 and result["orphans_requeued"] == 0 and result["orphans_failed"] == 0
        assert task.status == TaskStatus.running

    def test_tasks_that_changed_while_progress_was_read_are_left_alone(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _running(state, _session(state), CHAT, claim="c-old")
        with state._lock:
            orphans = collect_orphans_locked(state)
        task.claim_id = "c-new"

        with state._lock:
            outcome = settle_orphans_locked(state, orphans, {}, reason="engine_crash", cancel_reason="gone")

        assert outcome == OrphanSettlement()
        assert task.status == TaskStatus.running


class TestGracefulShutdown:
    def _completed(self, monkeypatch: pytest.MonkeyPatch, state: SupervisorState) -> list[str]:
        calls: list[str] = []
        # 没注册功能模块的 SupervisorState 本来就没有 execution，complete_task 按 getattr 取
        monkeypatch.setattr(
            state, "execution", SimpleNamespace(on_task_completed=lambda task: calls.append(task.task_id)), raising=False,
        )
        return calls

    def test_an_auto_continue_task_with_progress_goes_back_to_the_queue(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state))
        claim = _claim(state, task)
        completed = self._completed(monkeypatch, state)

        completion = state.complete_task(
            task_id=task.task_id, worker_id="engine-0", claim_id=claim,
            result={**SHUTDOWN, "node_id": WORKER, "_step_count": 7},
        )

        assert completion is not None and completion.outcome is CompletionOutcome.accepted
        assert task.status == TaskStatus.pending and task.result == {}
        assert task.input["_steps_used"] == 7
        assert task.input["resume_data"] == {"type": "interrupted", "reason": "engine_shutdown"}
        assert task.input[REQUEUE_MARK]["reason"] == "engine_shutdown"
        # 还没有结局，执行计划那一步不记
        assert completed == []

    @pytest.mark.parametrize("node,steps", [(CHAT, 7), (WORKER, 0)])
    def test_anything_else_fails_with_the_restart_as_its_reason(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, node: str, steps: int,
    ) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state), node=node)
        claim = _claim(state, task)
        completed = self._completed(monkeypatch, state)

        state.complete_task(
            task_id=task.task_id, worker_id="engine-0", claim_id=claim,
            result={**SHUTDOWN, "node_id": node, "_step_count": steps},
        )

        assert task.status == TaskStatus.failed
        assert task.result["error"] == USER_INTERRUPTED_ERROR and task.result["interrupted"] == "engine_shutdown"
        assert REQUEUE_MARK not in task.input
        assert completed == [task.task_id]
        assert f"[错误] {USER_INTERRUPTED_ERROR}" in _outbound_texts(state)

    def test_an_interjection_nobody_asked_for_is_still_cancelled_quietly(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state), node=CHAT, route_hints={"delivery_purpose": "ambient"})
        claim = _claim(state, task)

        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result={**SHUTDOWN, "node_id": CHAT})

        assert task.status == TaskStatus.cancelled
        assert _outbound_texts(state) == []

    def test_a_task_requeued_before_fails_the_second_time(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state))
        claim = _claim(state, task)
        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result={**SHUTDOWN, "_step_count": 3})
        claim = _claim(state, task)

        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result={**SHUTDOWN, "_step_count": 5})

        assert task.status == TaskStatus.failed

    def test_steps_recorded_at_a_suspend_count_as_progress(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state))
        claim = _claim(state, task)
        task.input["_steps_used"] = 6

        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result=dict(SHUTDOWN))

        assert task.status == TaskStatus.pending
        assert task.input["_steps_used"] == 6


class TestManualEngineRestart:
    def test_only_tasks_the_old_engine_had_claimed_are_settled(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        cutoff = datetime.now(timezone.utc)
        with state._lock:
            queued = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id=WORKER,
                input_data={"instruction": "q"}, continuation={}, source_inbound_seq=None,
            )
        waiting = _running(state, sid, WORKER, claim="c-wait", claimed_at=cutoff - timedelta(minutes=5))
        waiting.status = TaskStatus.suspended
        old_chat = _running(state, sid, CHAT, claim="c-chat", claimed_at=cutoff - timedelta(minutes=1))
        old_worker = _running(state, sid, WORKER, claim="c-work", claimed_at=cutoff - timedelta(minutes=1))
        _progress(workspace, old_worker, 4)
        newer = _running(state, sid, CHAT, claim="c-new", claimed_at=cutoff + timedelta(seconds=1))

        settled = state.settle_orphaned_tasks(claimed_before=cutoff)

        assert settled == 2
        assert queued.status == TaskStatus.pending
        assert waiting.status == TaskStatus.suspended
        assert old_chat.status == TaskStatus.failed
        assert old_chat.result["error"] == USER_INTERRUPTED_ERROR
        assert old_worker.status == TaskStatus.pending
        assert old_worker.input[REQUEUE_MARK]["reason"] == "engine_restart"
        assert old_worker.input["_steps_used"] == 4
        assert newer.status == TaskStatus.running

    def test_without_a_cutoff_every_running_task_is_settled(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _running(state, sid, CHAT, claim="c-1")
        second = _running(state, sid, CHAT, claim="c-2", claimed_at=datetime.now(timezone.utc) + timedelta(hours=1))

        assert state.settle_orphaned_tasks() == 2
        assert first.status == TaskStatus.failed and second.status == TaskStatus.failed

    def test_nothing_running_means_nothing_to_do(self, workspace: Path) -> None:
        state = _state(workspace)

        assert state.settle_orphaned_tasks(claimed_before=datetime.now(timezone.utc)) == 0
