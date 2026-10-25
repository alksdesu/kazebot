"""同步压缩不再与 engine 来回循环，压缩的大块磁盘 I/O 不占 supervisor 全局锁。

没有可压缩的历史、摘要没能落盘都计入熔断；同一任务连续失败到上限就按失败结束；
成功一次清零。compactor 完成时整份读写会话历史发生在锁外，锁内只提交结果。
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor.compact_breaker import MAX_TASK_CONSECUTIVE_FAILURES  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import RouteStatus, Task, TaskKind, TaskStatus  # noqa: E402

_SUMMARY = "<summary>" + "Preserved decisions and facts from the first large record. " * 8 + "</summary>"
_PREFIX = "[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n"


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-compaction"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _seed_history(workspace: Path, session_id: str) -> ConversationStore:
    store = ConversationStore(workspace / "data" / "conversations")
    store.append_batch(session_id, [
        Message(id="large", role="user", content="x" * 200_000, source_task_id="old"),
        Message(id="remainder", role="assistant", content="y" * 150_000, source_task_id="old"),
        Message(id="recent1", role="user", content="recent one", source_task_id="recent1"),
        Message(id="recent2", role="user", content="recent two", source_task_id="recent2"),
    ])
    return store


def _caller(state: SupervisorState, session_id: str) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="chat",
            input_data={"task_context": {"conversation_key": "test:sup-compaction"}},
            continuation={},
            source_inbound_seq=None,
        )


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _ask_for_compaction(state: SupervisorState, caller: Task, session_id: str) -> None:
    _report(state, caller, {
        "action": "dispatch",
        "target_node": "system.compactor",
        "context_ref": "",
        "dispatch_input": {
            "_compact_dispatch": True, "target_session_id": session_id, "context_mode": "fresh",
            "instruction": "request", "_compact_keep_recent": 2, "_compact_keep_recent_tokens": 0,
        },
    })


def _compactor_of(state: SupervisorState, caller: Task) -> Task:
    return state.tasks[str(caller.waiting_for_task_id)]


class TestFailedRoundsAreCounted:
    def test_no_plan_counts_into_the_breaker_and_the_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        caller = _caller(state, sid)

        _ask_for_compaction(state, caller, sid)

        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["type"] == "compact_done"
        assert caller.input["resume_data"]["success"] is False
        assert caller.input["_compact_failures"] == 1
        assert state.compact_breaker.failure_count(sid) == 1

    def test_the_task_fails_after_the_last_allowed_failure(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        caller = _caller(state, sid)

        for _ in range(MAX_TASK_CONSECUTIVE_FAILURES):
            _ask_for_compaction(state, caller, sid)

        assert caller.status == TaskStatus.failed
        assert caller.cancel_requested
        assert "上下文压缩连续失败" in caller.result["error"]
        assert "_compact_dispatch_pending" not in caller.input
        assert caller.route_status == RouteStatus.routed
        texts = [
            str((event.get("payload") or {}).get("text") or "")
            for event in state.eventlog.iter_persisted_events()
            if event.get("type") == "outbound_message"
        ]
        assert any("上下文压缩连续失败" in text for text in texts)

    def test_an_async_summary_that_no_longer_fits_counts_as_a_failure(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        store = _seed_history(tmp_path, sid)
        sizing = {"_compact_keep_recent": 2, "_compact_keep_recent_tokens": 0}
        plan, _ = state._prepare_compact_input(sid, sizing)
        assert plan
        store.replace_all(sid, [Message(id="other", role="user", content="history moved on", source_task_id="new")])
        task = SimpleNamespace(
            input={**sizing, "_compact_plan": plan, "_compact_target_session_id": sid},
            result={"action": "finish", "result": {"text": _SUMMARY}},
        )

        with state._lock:
            state._apply_async_compact_result_locked(task)

        assert state.compact_breaker.failure_count(sid) == 1


class TestASuccessfulRound:
    def test_success_clears_the_task_counter_and_writes_a_real_paragraph_break(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        store = _seed_history(tmp_path, sid)
        caller = _caller(state, sid)
        caller.input["_compact_failures"] = MAX_TASK_CONSECUTIVE_FAILURES - 1

        _ask_for_compaction(state, caller, sid)
        _report(state, _compactor_of(state, caller), {"action": "finish", "result": {"text": _SUMMARY}})

        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["success"] is True
        assert "_compact_failures" not in caller.input
        summary = store.load(sid)[0]
        assert summary.content.startswith(_PREFIX)
        assert "\\n" not in summary.content[:len(_PREFIX) + 5]

    def test_conversation_rewrites_happen_outside_the_supervisor_lock(self, tmp_path: Path, monkeypatch) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        _seed_history(tmp_path, sid)
        caller = _caller(state, sid)
        held: list[bool] = []
        original = ConversationStore.replace_all

        def watched(store: ConversationStore, session_id: str, messages: list[Any]) -> None:
            if session_id == sid:
                held.append(state._lock._is_owned())
            original(store, session_id, messages)

        monkeypatch.setattr(ConversationStore, "replace_all", watched)

        _ask_for_compaction(state, caller, sid)
        _report(state, _compactor_of(state, caller), {"action": "finish", "result": {"text": _SUMMARY}})

        assert caller.input["resume_data"]["success"] is True
        assert held == [False]


class TestOneRouterPerTask:
    def test_a_second_route_of_the_same_task_waits_for_the_first(self, tmp_path: Path, monkeypatch) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-compaction")
        task = _caller(state, sid)
        entered = threading.Event()
        release = threading.Event()
        active: list[int] = []
        overlaps: list[int] = []
        calls: list[int] = []

        def slow_route(self: SupervisorState, routed: Task) -> None:
            if active:
                overlaps.append(1)
            active.append(1)
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                assert release.wait(timeout=3)
            active.pop()

        monkeypatch.setattr(SupervisorState, "_route_completed_task_with_branch", slow_route)
        first = threading.Thread(target=state._route_completed_task, args=(task,))
        second = threading.Thread(target=state._route_completed_task, args=(task,))
        first.start()
        assert entered.wait(timeout=2)
        second.start()
        time.sleep(0.2)
        assert len(calls) == 1
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)

        assert not first.is_alive() and not second.is_alive()
        assert len(calls) == 2 and overlaps == []
