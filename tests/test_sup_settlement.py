"""子任务的结局只交给正在等它的 caller。

caller 已经不在等（被回收、取消，或在等别的子任务）时结果只记日志丢弃，绝不改投给用户；
批次在兄弟全部真正结束后恢复一次（还在路由去挂起的、结局还没路由完的、旧 worker 还没停手的都要等）；
批次里的子节点自己再派发或被抢占时照常路由。三条取消路径共用一个结算：取消运行中的子任务，worker 停下后 caller 恢复。
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor import async_dispatches, session_reuse, stopped_batch  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_ending import STOP_GRACE, TRANSIT_GRACE, TaskEnding  # noqa: E402
from supervisor.task_store import CompletionOutcome  # noqa: E402
from supervisor.types import RouteStatus, Task, TaskKind, TaskStatus  # noqa: E402

# 线程之间的每一处等待都带上限：出了问题测试失败，不会挂死
_WAIT = 10.0


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-settlement"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _caller(state: SupervisorState, session_id: str) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": "test:sup-settlement"}},
            continuation={},
            source_inbound_seq=None,
        )


def _dispatch(state: SupervisorState, caller: Task, action: dict[str, Any]) -> list[Task]:
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", **action})
    return sorted(
        (t for t in state.tasks.values() if t.caller_task_id == caller.task_id and not state._task_terminal(t)),
        key=lambda t: t.batch_index,
    )


def _single(target: str = "child.node") -> dict[str, Any]:
    return {"target_node": target, "dispatch_input": {"instruction": "go", "context_mode": "fresh"}}


def _batch(*targets: str) -> dict[str, Any]:
    return {"dispatch_batch": [
        {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh"}
        for target in targets
    ]}


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _finish(state: SupervisorState, task: Task, text: str) -> None:
    _report(state, task, {"action": "finish", "result": {"text": text}})


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


class TestResultsOnlyReachAWaitingCaller:
    def test_a_child_of_a_caller_that_stopped_waiting_is_dropped_not_sent_to_the_user(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        caller.status = TaskStatus.failed

        _finish(state, child, "internal notes for the orchestrator")

        assert "internal notes for the orchestrator" not in _outbound_texts(state)
        assert caller.status == TaskStatus.failed

    def test_a_caller_waiting_on_another_child_ignores_a_stale_one(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        caller.waiting_for_task_id = "a-different-child"

        _finish(state, child, "stale child output")

        assert caller.status == TaskStatus.suspended
        assert "stale child output" not in _outbound_texts(state)

    def test_an_engine_reported_cancel_still_resumes_the_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())

        _report(state, child, {"action": "cancelled"})

        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["type"] == "child_cancelled"


class TestBatches:
    def test_the_caller_resumes_once_after_every_sibling_ends(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _finish(state, first, "first part")
        assert caller.status == TaskStatus.suspended
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        entries = caller.input["resume_data"]["entries"]
        assert [entry["text"] for entry in entries] == ["first part", "second part"]
        resumed_at = caller.updated_at
        state.complete_task(
            task_id=first.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "first part"}},
        )
        assert caller.updated_at == resumed_at
        assert not {"first part", "second part"} & set(_outbound_texts(state))

    def test_cancelling_a_pending_sibling_waits_for_the_rest(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        assert state.cancel_single_task(first.task_id)["cancelled_count"] == 1
        assert caller.status == TaskStatus.suspended

        _finish(state, second, "survivor")
        assert caller.status == TaskStatus.pending
        statuses = [entry["status"] for entry in caller.input["resume_data"]["entries"]]
        assert statuses == ["cancelled", "finish"]

    def test_a_batch_child_that_dispatches_gets_its_own_children(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _report(state, first, {"action": "dispatch", **_single("grand.node")})

        assert first.status == TaskStatus.suspended and first.batch_id
        [grandchild] = [t for t in state.tasks.values() if t.caller_task_id == first.task_id]
        assert caller.status == TaskStatus.suspended
        _finish(state, grandchild, "grandchild work")
        assert first.status == TaskStatus.pending
        _finish(state, first, "first after grandchild")
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        assert [entry["text"] for entry in caller.input["resume_data"]["entries"]] == [
            "first after grandchild", "second part",
        ]

    def test_a_preempted_batch_child_counts_as_ended(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))

        _report(state, first, {"action": "preempted", "context_ref": "ctx-1"})
        assert caller.status == TaskStatus.suspended
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        assert [entry["status"] for entry in caller.input["resume_data"]["entries"]] == ["preempted", "finish"]


class TestCancellingARunningChild:
    def test_the_caller_resumes_once_the_worker_stops(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")
        caller = _caller(state, sid)
        [child] = _dispatch(state, caller, _single())
        child.status = TaskStatus.running
        child.worker_id = "worker-1"
        child.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        assert state.cancel_single_task(child.task_id)["cancelled_count"] == 1
        assert child.cancel_requested and child.status == TaskStatus.running
        assert caller.status == TaskStatus.suspended

        state.complete_task(
            task_id=child.task_id, worker_id="worker-1",
            result={"action": "finish", "result": {"text": "ignored after cancel"}},
        )

        assert child.status == TaskStatus.cancelled
        assert caller.status == TaskStatus.pending
        assert caller.input["resume_data"]["type"] == "child_cancelled"
        assert caller.input["_last_child_action_type"] == "cancelled"
        assert "ignored after cancel" not in _outbound_texts(state)


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="test", conversation_key="test:sup-settlement")


def _item(target: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _children_of(state: SupervisorState, caller: Task) -> list[Task]:
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id),
        key=lambda task: task.batch_index,
    )


def _expire(state: SupervisorState, *, after: timedelta) -> None:
    with state._lock:
        async_dispatches.expire_due_locked(state, now=datetime.now(timezone.utc) + after)


def _claimed(task: Task, claim_id: str = "claim-1") -> None:
    """照 worker 领走那样：在跑、带领取代次、租约有效。"""
    now = datetime.now(timezone.utc)
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.claim_id = claim_id
    task.claimed_at = now
    task.lease_expires_at = now + timedelta(minutes=2)


def _stopped_notes(workspace: Path, task: Task) -> list[Message]:
    """被停下时那一批写进它自己会话里的消息。"""
    store = ConversationStore(workspace / "data" / "conversations")
    return [
        message for message in store.load(str(task.input["child_session_id"]))
        if isinstance(message.meta, dict) and message.meta.get(stopped_batch.MARK_KEY) == task.task_id
    ]


def _busy(state: SupervisorState, session_id: str) -> bool:
    with state._lock:
        return session_reuse.conflict_locked(state, session_id, ()) == session_reuse.CONFLICT_BUSY


class _Background:
    """在另一个线程里跑一次调用；join 时把那边抛的异常带回来。"""

    def __init__(self, target: Callable[..., Any], *args: Any) -> None:
        self.error: BaseException | None = None

        def run() -> None:
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> None:
        self.thread.join(_WAIT)
        assert not self.thread.is_alive(), "the background call never finished"
        if self.error is not None:
            raise self.error


class _HeldRoute:
    """卡住一个任务第一次路由里锁外的那段：进去时报一声，放行后才接着走。"""

    def __init__(self, state: SupervisorState, task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        original = state._prepare_route_io

        def held(routed: Task) -> None:
            if routed.task_id == task.task_id and not self.entered.is_set():
                self.entered.set()
                self.release.wait(_WAIT)
            original(routed)

        monkeypatch.setattr(state, "_prepare_route_io", held)


class _Routed:
    """等一个任务的这次完成真正路由完，另起线程的路由也算。"""

    def __init__(self, state: SupervisorState, task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
        self.done = threading.Event()
        original = state._route_completed_task

        def watched(routed: Task) -> None:
            try:
                original(routed)
            finally:
                if routed.task_id == task.task_id and routed.route_status == RouteStatus.routed:
                    self.done.set()

        monkeypatch.setattr(state, "_route_completed_task", watched)


class _HeldWrite:
    """卡住一个任务那一批说明的写入：进去时报一声，放行后才写。"""

    def __init__(self, task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        original = stopped_batch.write

        def held(state: SupervisorState, written: Task) -> None:
            if written.task_id == task.task_id:
                self.entered.set()
                self.release.wait(_WAIT)
            original(state, written)

        monkeypatch.setattr(stopped_batch, "write", held)


class _TornDown:
    """等一个被取消的任务的收尾线程跑完。"""

    def __init__(self, state: SupervisorState, task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
        self.done = threading.Event()
        original = state._finish_cancelled_task

        def watched(finished: Task) -> None:
            try:
                original(finished)
            finally:
                if finished.task_id == task.task_id:
                    self.done.set()

        monkeypatch.setattr(state, "_finish_cancelled_task", watched)


class TestSiblingsInTransit:
    def test_a_sibling_still_routing_its_dispatch_keeps_the_caller_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))
        held = _HeldRoute(state, first, monkeypatch)

        worker = _Background(_report, state, first, {"action": "dispatch", **_single("grand.node")})
        assert held.entered.wait(_WAIT)
        assert state._task_ending_locked(first) is TaskEnding.dispatching
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.suspended
        held.release.set()
        worker.join()
        assert first.status == TaskStatus.suspended
        [grandchild] = _children_of(state, first)
        _finish(state, grandchild, "grandchild work")
        _finish(state, first, "first after grandchild")

        assert caller.status == TaskStatus.pending
        assert [entry["text"] for entry in caller.input["resume_data"]["entries"]] == [
            "first after grandchild", "second part",
        ]

    def test_a_dispatch_whose_route_failed_settles_the_batch_and_a_retry_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))
        _finish(state, second, "second part")
        original_inner = state._route_completed_task_inner_locked

        def broken(task: Task) -> None:
            if task.task_id == first.task_id:
                raise RuntimeError("route broke")
            original_inner(task)

        monkeypatch.setattr(state, "_route_completed_task_inner_locked", broken)
        with pytest.raises(RuntimeError, match="route broke"):
            _report(state, first, {"action": "dispatch", **_single("grand.node")})

        assert first.route_status == RouteStatus.failed
        assert caller.status == TaskStatus.pending
        failed, finished = caller.input["resume_data"]["entries"]
        assert failed["status"] == "fail" and "没有派出去" in failed["error"]
        assert finished["text"] == "second part"

        monkeypatch.setattr(state, "_route_completed_task_inner_locked", original_inner)
        state.complete_task(
            task_id=first.task_id, worker_id="worker-1", result={"action": "dispatch", **_single("grand.node")},
        )

        assert first.status == TaskStatus.cancelled and first.route_status == RouteStatus.routed
        assert _children_of(state, first) == []

    def test_a_dispatch_that_never_finishes_routing_is_given_up_after_the_grace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))
        held = _HeldRoute(state, first, monkeypatch)
        worker = _Background(_report, state, first, {"action": "dispatch", **_single("grand.node")})
        assert held.entered.wait(_WAIT)
        _finish(state, second, "second part")
        assert caller.status == TaskStatus.suspended

        _expire(state, after=TRANSIT_GRACE + timedelta(seconds=1))

        assert caller.status == TaskStatus.pending
        given_up, finished = caller.input["resume_data"]["entries"]
        assert given_up["status"] == "fail" and "没有派出去" in given_up["error"]
        assert finished["text"] == "second part"
        held.release.set()
        worker.join()
        # 路由迟到时调用方已经不等它了：按取消收掉，不再派出子任务
        assert first.status == TaskStatus.cancelled
        assert _children_of(state, first) == []

    def test_an_item_timing_out_while_its_dispatch_is_routing_fails_without_sending_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, {"dispatch_batch": [
            _item("a.node", call_id="w1", timeout_sec=30), _item("b.node", call_id="w2"),
        ]})
        held = _HeldRoute(state, first, monkeypatch)
        routed = _Routed(state, first, monkeypatch)
        worker = _Background(_report, state, first, {"action": "dispatch", "dispatch_batch": [
            _item("grand.node", call_id="g1"),
        ]})
        assert held.entered.wait(_WAIT)

        _expire(state, after=timedelta(seconds=31))

        assert first.input[async_dispatches.ITEM_TIMED_OUT_KEY] is True
        assert first.status == TaskStatus.completed
        held.release.set()
        worker.join()
        assert routed.done.wait(_WAIT)
        assert first.status == TaskStatus.failed
        assert _children_of(state, first) == []
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        timed_out, finished = caller.input["resume_data"]["entries"]
        assert timed_out["timed_out"] is True and timed_out["error"] == "等待超时：30 秒内没有做完，已被停下。"
        assert timed_out["continuable"] is True and finished["text"] == "second part"
        notes = _stopped_notes(tmp_path, first)
        assert [note.message_type for note in notes] == ["dispatch_result", "system"]
        assert "子节点 grand.node（调用 g1" in str(notes[0].content)
        assert "这一项还没派出去" in str(notes[0].content)
        assert "还没派出去就被停下了：等待超时：30 秒内没有做完" in str(notes[1].content)


class TestAFailedChildWhoseWorkerHasNotStopped:
    def test_the_batch_waits_for_the_stopped_worker_and_its_session_stays_busy(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, {"dispatch_batch": [
            _item("a.node", call_id="w1", timeout_sec=30), _item("b.node", call_id="w2"),
        ]})
        _claimed(first)

        _expire(state, after=timedelta(seconds=31))
        _finish(state, second, "second part")

        assert first.status == TaskStatus.failed
        assert state._task_ending_locked(first) is TaskEnding.stopping
        assert caller.status == TaskStatus.suspended
        assert _busy(state, first.input["child_session_id"])

        late = state.complete_task(
            task_id=first.task_id, worker_id="worker-1", result={"action": "cancelled"}, claim_id="claim-1",
        )

        assert late is not None and late.outcome is CompletionOutcome.stale
        assert caller.status == TaskStatus.pending
        timed_out, finished = caller.input["resume_data"]["entries"]
        assert timed_out["status"] == "fail" and timed_out["timed_out"] is True and timed_out["continuable"] is True
        assert finished["text"] == "second part"
        assert not _busy(state, first.input["child_session_id"])

    def test_a_report_from_another_claim_does_not_end_the_wait(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        [child] = _dispatch(state, caller, {"dispatch_batch": [_item("a.node", call_id="w1", timeout_sec=30)]})
        _claimed(child)
        _expire(state, after=timedelta(seconds=31))

        state.complete_task(
            task_id=child.task_id, worker_id="worker-2", result={"action": "cancelled"}, claim_id="claim-other",
        )

        assert state._task_ending_locked(child) is TaskEnding.stopping
        assert caller.status == TaskStatus.suspended

    def test_a_worker_that_never_reports_is_given_up_after_the_stop_grace(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, {"dispatch_batch": [
            _item("a.node", call_id="w1", timeout_sec=30), _item("b.node", call_id="w2"),
        ]})
        _claimed(first)
        _expire(state, after=timedelta(seconds=31))
        _finish(state, second, "second part")
        assert caller.status == TaskStatus.suspended

        _expire(state, after=timedelta(seconds=31) + STOP_GRACE + timedelta(seconds=1))

        assert caller.status == TaskStatus.pending
        assert state._task_ending_locked(first) is TaskEnding.ended
        assert not _busy(state, first.input["child_session_id"])


class TestAMiddleTaskStoppedWhileItWaits:
    def test_what_its_batch_had_goes_into_its_own_history_before_its_caller_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [middle] = _dispatch(state, top, {"dispatch_batch": [_item("mid.node", call_id="m1", timeout_sec=60)]})
        _report(state, middle, {"action": "dispatch", "dispatch_batch": [
            _item("a.node", call_id="w1"), _item("b.node", call_id="w2"), _item("c.node", call_id="w3"),
        ]})
        first, second, third = _children_of(state, middle)
        _finish(state, first, "first findings")
        held = _HeldWrite(middle, monkeypatch)
        routed = _Routed(state, middle, monkeypatch)

        _expire(state, after=timedelta(seconds=61))

        assert middle.status == TaskStatus.failed
        assert [second.status, third.status] == [TaskStatus.cancelled, TaskStatus.cancelled]
        assert held.entered.wait(_WAIT)
        # 写进去之前它的会话算忙：上级马上 continue_from 也接不进来
        assert _busy(state, middle.input["child_session_id"])
        assert top.status == TaskStatus.suspended
        with state._lock:
            capture = dict(middle.input[stopped_batch.STOPPED_BATCH_KEY])
        held.release.set()
        assert routed.done.wait(_WAIT)

        assert top.status == TaskStatus.pending
        [entry] = top.input["resume_data"]["entries"]
        assert entry["status"] == "fail" and entry["timed_out"] is True and entry["continuable"] is True
        notes = _stopped_notes(tmp_path, middle)
        assert [note.message_type for note in notes] == ["dispatch_result"] * 3 + ["system"]
        assert "first findings" in str(notes[0].content)
        assert all("上级任务已失败" in str(note.content) for note in notes[1:3])
        assert "在等上面这批子任务时被停下了：等待超时：60 秒内没有做完" in str(notes[3].content)
        assert not _busy(state, middle.input["child_session_id"])

        with state._lock:
            middle.input[stopped_batch.STOPPED_BATCH_KEY] = capture
        stopped_batch.write(state, middle)
        assert len(_stopped_notes(tmp_path, middle)) == 4
        assert stopped_batch.STOPPED_BATCH_KEY not in middle.input

    def test_a_cancelled_middle_task_that_keeps_its_session_gets_the_note_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [middle] = _dispatch(state, top, {"dispatch_batch": [
            _item("mid.node", call_id="m1", context_mode="accumulate"),
        ]})
        _report(state, middle, {"action": "dispatch", "dispatch_batch": [
            _item("a.node", call_id="w1"), _item("b.node", call_id="w2"),
        ]})
        first, second = _children_of(state, middle)
        _finish(state, first, "first findings")
        torn_down = _TornDown(state, middle, monkeypatch)

        assert state.cancel_single_task(middle.task_id)["cancelled_count"] == 2

        assert torn_down.done.wait(_WAIT)
        assert second.status == TaskStatus.cancelled
        notes = _stopped_notes(tmp_path, middle)
        assert [note.message_type for note in notes] == ["dispatch_result", "dispatch_result", "system"]
        assert "first findings" in str(notes[0].content)
        assert "它当时还没交回结果，已被一并叫停" in str(notes[1].content)
        assert "任务已取消" in str(notes[2].content)

    def test_a_cancelled_middle_task_whose_session_goes_away_leaves_nothing_pending(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        top = _caller(state, _session(state))
        [middle] = _dispatch(state, top, {"dispatch_batch": [_item("mid.node", call_id="m1")]})
        _report(state, middle, {"action": "dispatch", "dispatch_batch": [_item("a.node", call_id="w1")]})

        assert state.cancel_single_task(middle.task_id)["cancelled_count"] == 2

        assert stopped_batch.STOPPED_BATCH_KEY not in middle.input
        assert not _busy(state, middle.input["child_session_id"])
