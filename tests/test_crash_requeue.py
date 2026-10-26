"""engine 中途退出时手里的任务：排队和挂起的留着，声明了 auto_continue、已有进展的重排一次并从存储接着跑，其余照旧取消。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_requeue import collect_orphans_locked, settle_orphans_locked, storage_session  # noqa: E402
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


def _inbound(state: SupervisorState, session_id: str, *, node: str = WORKER) -> Task:
    payload = {
        "channel": "qq_private",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "整理一下日志模块",
        "entry_node_id": node,
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
        assert result["orphans_cancelled"] == 0 and result["orphans_requeued"] == 0
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

        assert result["orphans_requeued"] == 1 and result["orphans_cancelled"] == 0
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

        assert again["orphans_requeued"] == 0 and again["orphans_cancelled"] == 1
        assert task.status == TaskStatus.cancelled

    def test_a_task_without_progress_is_cancelled(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _inbound(state, _session(state))
        _claim(state, task)
        ConversationStore(workspace / "data" / "conversations").append_batch(task.session_id, [
            Message(id="u-1", role="user", content="整理一下日志模块", source_task_id=task.task_id),
        ])

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_requeued"] == 0 and result["orphans_cancelled"] == 1
        assert task.status == TaskStatus.cancelled

    def test_a_node_without_auto_continue_is_cancelled_with_its_descendants(self, workspace: Path) -> None:
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

        assert result["orphans_requeued"] == 0 and result["orphans_cancelled"] == 2
        assert task.status == TaskStatus.cancelled and child.status == TaskStatus.cancelled

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

        assert result["orphans_cancelled"] == 1
        assert task.status == TaskStatus.cancelled

    def test_system_tasks_are_never_requeued(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _running(state, _session(state), WORKER, claim="c-1")
        task.input["_system_task"] = True
        task.input["_steps_used"] = 5

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_cancelled"] == 1
        assert task.status == TaskStatus.cancelled

    def test_the_same_generation_registering_again_changes_nothing(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        task = _running(state, _session(state), WORKER, claim="c-1")

        result = state.register_engine("engine-0", "gen-1")

        assert result["orphans_cancelled"] == 0 and result["orphans_requeued"] == 0
        assert task.status == TaskStatus.running

    def test_tasks_that_changed_while_progress_was_read_are_left_alone(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _running(state, _session(state), CHAT, claim="c-old")
        with state._lock:
            orphans = collect_orphans_locked(state)
        task.claim_id = "c-new"

        with state._lock:
            outcome = settle_orphans_locked(state, orphans, {}, reason="engine_crash", cancel_reason="gone")

        assert outcome == (0, 0)
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
    def test_anything_else_is_cancelled_as_before(
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

        assert task.status == TaskStatus.cancelled
        assert REQUEUE_MARK not in task.input
        assert completed == [task.task_id]

    def test_a_task_requeued_before_is_cancelled_the_second_time(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _inbound(state, _session(state))
        claim = _claim(state, task)
        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result={**SHUTDOWN, "_step_count": 3})
        claim = _claim(state, task)

        state.complete_task(task_id=task.task_id, worker_id="engine-0", claim_id=claim, result={**SHUTDOWN, "_step_count": 5})

        assert task.status == TaskStatus.cancelled

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

        settled = state.cancel_orphaned_tasks(claimed_before=cutoff)

        assert settled == 2
        assert queued.status == TaskStatus.pending
        assert waiting.status == TaskStatus.suspended
        assert old_chat.status == TaskStatus.cancelled
        assert old_worker.status == TaskStatus.pending
        assert old_worker.input[REQUEUE_MARK]["reason"] == "engine_restart"
        assert old_worker.input["_steps_used"] == 4
        assert newer.status == TaskStatus.running

    def test_without_a_cutoff_every_running_task_is_settled(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _running(state, sid, CHAT, claim="c-1")
        second = _running(state, sid, CHAT, claim="c-2", claimed_at=datetime.now(timezone.utc) + timedelta(hours=1))

        assert state.cancel_orphaned_tasks() == 2
        assert first.status == TaskStatus.cancelled and second.status == TaskStatus.cancelled

    def test_nothing_running_means_nothing_to_do(self, workspace: Path) -> None:
        state = _state(workspace)

        assert state.cancel_orphaned_tasks(claimed_before=datetime.now(timezone.utc)) == 0
