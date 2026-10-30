"""supervisor 收到 wait 批次：节点项的附件校验后透传，子节点带上发起方的身份和根会话键，系统任务、超量和没有目标的批次被拒，
恢复数据带回调用 id，派到一半出错时已经派出的全部撤回、调用方按失败收尾。"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore  # noqa: E402
from engine.dispatch_batch import MAX_DISPATCH_BATCH_ITEMS, batch_item_attachments  # noqa: E402
from supervisor import stopped_batch  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_group:1001"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
# 等另起的线程写完时的上限：出了问题测试失败，不会挂死
_WAIT = 10.0


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-wait-dispatch"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=CONV)


def _context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "conversation_key": CONV,
        "channel": "qq",
        "platform_auth": dict(AUTH),
        "route_hints": {"reply_message_id": "m-1"},
    }
    context.update(overrides)
    return context


def _caller(
    state: SupervisorState,
    session_id: str,
    *,
    task_context: dict[str, Any] | None = None,
    caller_task_id: str | None = None,
    **input_extra: Any,
) -> Task:
    input_data: dict[str, Any] = dict(input_extra)
    if task_context is not None:
        input_data["task_context"] = task_context
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data=input_data,
            continuation={},
            source_inbound_seq=None,
            caller_task_id=caller_task_id,
        )


def _item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _children(state: SupervisorState, caller: Task) -> list[Task]:
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id),
        key=lambda task: task.batch_index,
    )


def _dispatch(state: SupervisorState, caller: Task, *items: Any) -> list[Task]:
    with state._lock:
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", "dispatch_batch": list(items)})
    return _children(state, caller)


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


class TestAttachmentFormat:
    def test_only_well_formed_data_paths_survive(self) -> None:
        raw = [
            {"type": "image", "path": "data/attachments/a.png", "mime_type": "image/png", "name": "a.png"},
            {"path": "data/notes/b.txt"},
            {"path": "data/attachments/a.png"},
            {"path": "../etc/passwd"},
            {"path": "data/../config/models.yaml"},
            {"path": "/data/attachments/x.png"},
            {"path": "C:/data/attachments/x.png"},
            {"path": "config/secrets.env"},
            {"path": 42},
            "data/attachments/plain-string.png",
            {"type": "video", "path": "data/attachments/c.png", "mime_type": "image/png"},
        ]

        assert batch_item_attachments(raw) == [
            {"type": "image", "path": "data/attachments/a.png", "name": "a.png", "mime_type": "image/png"},
            {"type": "file", "path": "data/notes/b.txt", "name": "b.txt"},
            {"type": "image", "path": "data/attachments/c.png", "name": "c.png", "mime_type": "image/png"},
        ]

    def test_anything_but_a_list_gives_nothing(self) -> None:
        assert batch_item_attachments(None) == []
        assert batch_item_attachments({"path": "data/a.png"}) == []
        assert batch_item_attachments("data/a.png") == []


class TestNodeItems:
    def test_attachments_and_the_call_id_reach_the_child_input(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        [child] = _dispatch(state, caller, _item(call_id="w1", attachments=[
            {"type": "image", "path": "data/attachments/a.png", "mime_type": "image/png", "name": "a.png"},
            {"path": "../outside.png"},
        ]))

        assert child.input["attachments"] == [
            {"type": "image", "path": "data/attachments/a.png", "name": "a.png", "mime_type": "image/png"},
        ]
        assert child.input["_dispatch_call_id"] == "w1"
        assert caller.status == TaskStatus.suspended and caller.waiting_for_batch_id == child.batch_id

    def test_an_item_without_attachments_adds_none(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        [child] = _dispatch(state, caller, _item())

        assert "attachments" not in child.input
        assert "_dispatch_call_id" not in child.input

    def test_children_inherit_the_callers_identity_and_root_conversation(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        fresh, kept, keyed = _dispatch(
            state, caller,
            _item("a.node"),
            _item("b.node", context_mode="accumulate"),
            _item("c.node", context_mode="accumulate", context_key="topic"),
        )

        context = fresh.input["task_context"]
        assert context["conversation_key"] == f"agent:a.node:{CONV}:{fresh.input['child_session_id']}"
        assert context["route_conversation_key"] == CONV and context["parent_conversation_key"] == CONV
        assert context["platform_auth"] == AUTH
        assert context["route_hints"] == {"reply_message_id": "m-1"}
        assert context["channel"] == "qq"
        assert context["is_system_task"] is False
        assert context["dispatch_context_mode"] == "fresh"
        assert kept.input["task_context"]["conversation_key"] == f"agent:b.node:{CONV}"
        assert keyed.input["task_context"]["conversation_key"] == f"agent:c.node:topic:{CONV}"

    def test_the_child_input_carries_no_async_dispatch_markers(self, tmp_path: Path) -> None:
        # 带了的话子任务结束时的清理会把调用方的会话当成派发会话拆掉
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context(), parent_session_id="agent-session")

        [child] = _dispatch(state, caller, _item())

        assert "_dispatch_origin" not in child.input
        assert "dispatch_context_mode" not in child.input

    def test_a_nested_caller_passes_on_the_root_conversation(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(
            state, _session(state),
            task_context=_context(conversation_key=f"agent:mid.node:{CONV}", route_conversation_key=CONV),
        )

        [child] = _dispatch(state, caller, _item())

        assert child.input["task_context"]["route_conversation_key"] == CONV

    def test_identity_is_traced_up_the_call_chain_when_the_caller_has_none(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        root = _caller(state, sid, task_context=_context())
        legacy = _caller(state, sid, caller_task_id=root.task_id)

        [child] = _dispatch(state, legacy, _item())

        context = child.input["task_context"]
        assert context["platform_auth"] == AUTH
        assert context["route_conversation_key"] == CONV
        assert context["channel"] == "qq"

    def test_the_policy_origin_of_a_child_is_the_root_conversation(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())
        [child] = _dispatch(state, caller, _item())

        with state._lock:
            origin = state._task_origin_locked(child.task_id)

        assert origin is not None
        assert origin.conversation_key == CONV
        assert origin.platform_auth == AUTH

    def test_fork_children_copy_the_callers_own_conversation(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        nested = _caller(state, sid, task_context=_context(), child_session_id="child_caller")
        top = _caller(state, sid, task_context=_context())

        [from_nested] = _dispatch(state, nested, _item(context_mode="fork"))
        [from_top] = _dispatch(state, top, _item(context_mode="fork"))

        assert from_nested.input["fork_from_session_id"] == "child_caller"
        assert from_top.input["fork_from_session_id"] == sid

    def test_items_that_are_not_objects_are_skipped(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        children = _dispatch(state, caller, "junk", _item())

        assert [child.batch_index for child in children] == [1]


class TestRefusals:
    def test_a_system_task_cannot_open_a_batch(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context(), _system_task=True)

        _report(state, caller, {"action": "dispatch", "dispatch_batch": [_item()]})

        assert caller.status == TaskStatus.failed
        assert "系统任务" in caller.result["error"]
        assert _children(state, caller) == []
        assert _outbound_texts(state) == []

    def test_a_task_marked_system_in_its_context_is_refused_too(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context(is_system_task=True))

        _report(state, caller, {"action": "dispatch", "dispatch_batch": [_item()]})

        assert caller.status == TaskStatus.failed
        assert _children(state, caller) == []

    def test_a_system_task_can_still_dispatch_one_node_synchronously(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context(), _system_task=True)

        with state._lock:
            caller.status = TaskStatus.completed
            state._route_dispatch_locked(caller, {
                "action": "dispatch", "target_node": "child.node",
                "dispatch_input": {"instruction": "go", "context_mode": "fresh"},
            })

        assert len(_children(state, caller)) == 1
        assert caller.status == TaskStatus.suspended

    def test_a_batch_over_the_limit_is_refused_and_the_user_is_told(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())
        items = [_item(f"n{index}.node") for index in range(MAX_DISPATCH_BATCH_ITEMS + 1)]

        _report(state, caller, {"action": "dispatch", "dispatch_batch": items})

        assert caller.status == TaskStatus.failed
        assert _children(state, caller) == []
        expected = (
            f"同步委派被拒绝：一次最多派出 {MAX_DISPATCH_BATCH_ITEMS} 个子任务，这次有 {MAX_DISPATCH_BATCH_ITEMS + 1} 个。"
        )
        assert caller.result["error"] == expected
        assert f"[错误] {expected}" in _outbound_texts(state)

    def test_the_limit_itself_is_allowed(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        children = _dispatch(state, caller, *[_item(f"n{index}.node") for index in range(MAX_DISPATCH_BATCH_ITEMS)])

        assert len(children) == MAX_DISPATCH_BATCH_ITEMS
        assert caller.status == TaskStatus.suspended

    def test_a_batch_without_any_target_is_refused(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())

        _report(state, caller, {"action": "dispatch", "dispatch_batch": [{"kind": "node", "instruction": "x"}]})

        assert caller.status == TaskStatus.failed
        assert "没有一项写了 target" in caller.result["error"]

    def test_a_refused_batch_of_a_batch_child_is_reported_to_its_own_caller(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        root = _caller(state, _session(state), task_context=_context())
        [child] = _dispatch(state, root, _item("a.node", call_id="w1"))

        oversized = [_item(f"n{index}.node") for index in range(MAX_DISPATCH_BATCH_ITEMS + 1)]
        _report(state, child, {"action": "dispatch", "dispatch_batch": oversized})

        assert child.status == TaskStatus.failed
        assert root.status == TaskStatus.pending
        [entry] = root.input["resume_data"]["entries"]
        assert entry["status"] == "fail"
        assert entry["error"].startswith("同步委派被拒绝：一次最多派出")
        assert entry["call_id"] == "w1"
        assert not any(text.startswith("[错误] 同步委派被拒绝") for text in _outbound_texts(state))


class TestResumeEntries:
    def test_entries_carry_the_call_and_child_ids(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())
        first, second = _dispatch(state, caller, _item("a.node", call_id="w1"), _item("b.node", call_id="w2"))

        _finish(state, first, "first result")
        _finish(state, second, "second result")

        entries = caller.input["resume_data"]["entries"]
        assert [entry["call_id"] for entry in entries] == ["w1", "w2"]
        assert [entry["child_task_id"] for entry in entries] == [first.task_id, second.task_id]
        assert [entry["child_session_id"] for entry in entries] == [
            first.input["child_session_id"], second.input["child_session_id"],
        ]
        assert [entry["text"] for entry in entries] == ["first result", "second result"]

    def test_entries_from_items_without_a_call_id_leave_it_out(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())
        [child] = _dispatch(state, caller, _item())

        _finish(state, child, "done")

        [entry] = caller.input["resume_data"]["entries"]
        assert "call_id" not in entry
        assert entry["child_task_id"] == child.task_id


def _failing_on_second_child(state: SupervisorState, caller: Task, monkeypatch: pytest.MonkeyPatch) -> list[Task]:
    """让 caller 派第二个子任务时出错；返回已经建出来的那些。"""
    original = state._create_task_locked
    created: list[Task] = []

    def flaky(**kwargs: Any) -> Task:
        mine = kwargs.get("caller_task_id") == caller.task_id
        if mine and created:
            raise RuntimeError("disk full")
        task = original(**kwargs)
        if mine:
            created.append(task)
        return task

    monkeypatch.setattr(state, "_create_task_locked", flaky)
    return created


class _Written:
    """等一个任务那一批的说明写完，写的线程是路由另起的。"""

    def __init__(self, task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
        self.done = threading.Event()
        original = stopped_batch.write

        def watched(state: SupervisorState, written: Task) -> None:
            # 每次路由前都会先调一次 write，那时还没记下什么；只认真有东西要写的那一次
            pending = written.task_id == task.task_id and stopped_batch.pending_locked(written)
            try:
                original(state, written)
            finally:
                if pending:
                    self.done.set()

        monkeypatch.setattr(stopped_batch, "write", watched)


class TestDispatchFailingHalfway:
    def test_what_was_sent_is_withdrawn_and_the_caller_fails_with_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state), task_context=_context())
        created = _failing_on_second_child(state, caller, monkeypatch)
        result = {"action": "dispatch", "dispatch_batch": [_item("a.node", call_id="w1"), _item("b.node", call_id="w2")]}

        _report(state, caller, result)

        [withdrawn] = _children(state, caller)
        assert created == [withdrawn]
        assert withdrawn.status == TaskStatus.cancelled and withdrawn.result["error"] == "这一批没有派完，已撤回"
        assert caller.status == TaskStatus.failed
        assert caller.result["error"].startswith("派出子任务时出错（RuntimeError: disk full）")
        assert any(text.startswith("[错误] 派出子任务时出错（RuntimeError: disk full）") for text in _outbound_texts(state))

        state.complete_task(task_id=caller.task_id, worker_id="worker-1", result=result)

        assert _children(state, caller) == [withdrawn]
        assert caller.status == TaskStatus.failed

    def test_a_middle_task_is_reported_to_its_caller_once_and_its_history_says_what_went_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        root = _caller(state, _session(state), task_context=_context())
        [middle] = _dispatch(state, root, _item("mid.node", call_id="m1"))
        _failing_on_second_child(state, middle, monkeypatch)
        written = _Written(middle, monkeypatch)

        _report(state, middle, {"action": "dispatch", "dispatch_batch": [
            _item("a.node", call_id="w1"), _item("b.node", call_id="w2"),
        ]})

        assert middle.status == TaskStatus.failed
        assert [child.status for child in _children(state, middle)] == [TaskStatus.cancelled]
        # 撤下的子任务没有先把 root 唤醒成一批「已取消」：root 只收到 middle 失败这一项
        assert root.status == TaskStatus.pending
        [entry] = root.input["resume_data"]["entries"]
        assert entry["status"] == "fail" and entry["call_id"] == "m1"
        assert entry["error"].startswith("派出子任务时出错（RuntimeError: disk full）")
        assert written.done.wait(_WAIT)
        store = ConversationStore(tmp_path / "data" / "conversations")
        notes = [
            message for message in store.load(middle.input["child_session_id"])
            if isinstance(message.meta, dict) and message.meta.get(stopped_batch.MARK_KEY) == middle.task_id
        ]
        assert [note.message_type for note in notes] == ["dispatch_result", "dispatch_result", "system"]
        assert "还没派出去就被停下了：派出子任务时出错" in str(notes[2].content)
