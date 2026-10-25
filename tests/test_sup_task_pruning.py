"""内存里只留最近一批已了结的任务。

还没路由完、分支没收束、后置工作还在排队或在跑的不删；活着的任务还要读的也不删：
批次里先结束的兄弟要等最后一个结束时一起打包给 caller，派发链上的祖先要拿来认身份。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import RouteStatus, Task, TaskKind, TaskStatus  # noqa: E402
from tests.test_sup_settlement import _batch, _caller, _dispatch, _finish, _state  # noqa: E402


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="test", conversation_key="test:sup-pruning")


def _settled(state: SupervisorState, session_id: str, **input_data: Any) -> Task:
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.node",
            input_data=dict(input_data),
            continuation={},
            source_inbound_seq=None,
        )
        task.status = TaskStatus.completed
        task.route_status = RouteStatus.routed
    return task


def _prune(state: SupervisorState, keep: int) -> int:
    with state._lock:
        return state._prune_finished_tasks_locked(keep)


class TestQuota:
    def test_only_the_newest_settled_tasks_stay(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        tasks = [_settled(state, sid) for _ in range(5)]

        assert _prune(state, 2) == 3

        assert set(state.tasks) == {tasks[3].task_id, tasks[4].task_id}
        assert state._task_order == [tasks[3].task_id, tasks[4].task_id]

    def test_live_tasks_are_neither_counted_nor_dropped(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        live = _caller(state, sid)
        _settled(state, sid)

        assert _prune(state, 0) == 1

        assert list(state.tasks) == [live.task_id]


class TestUnfinishedBusiness:
    def test_a_completion_still_waiting_to_be_routed_is_kept(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        task = _settled(state, _session(state))
        task.route_status = RouteStatus.pending

        assert _prune(state, 0) == 0
        assert task.task_id in state.tasks

    def test_a_branch_task_is_kept_until_its_branch_is_finalized(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        task = _settled(state, _session(state), branch_session_id="branch-1")

        assert _prune(state, 0) == 0
        task.input["_branch_finalized"] = True
        assert _prune(state, 0) == 1

    def test_a_task_is_kept_while_its_post_work_is_queued_or_running(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        task = _settled(state, _session(state))
        task.post_work_id = "post-1"
        with state._lock:
            state._ensure_post_completion_state_locked()
            state._post_work_enqueued.add("post-1")

        assert _prune(state, 0) == 0
        with state._lock:
            state._post_work_enqueued.discard("post-1")
            state._post_work_running.add("post-1")
        assert _prune(state, 0) == 0
        with state._lock:
            state._post_work_running.discard("post-1")
        assert _prune(state, 0) == 1


class TestStillNeededByLiveTasks:
    def test_a_finished_batch_sibling_survives_until_the_caller_gets_every_result(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        caller = _caller(state, _session(state))
        first, second = _dispatch(state, caller, _batch("a.node", "b.node"))
        _finish(state, first, "first part")

        _prune(state, 0)
        assert first.task_id in state.tasks
        _finish(state, second, "second part")

        assert caller.status == TaskStatus.pending
        assert [entry["text"] for entry in caller.input["resume_data"]["entries"]] == ["first part", "second part"]

    def test_the_finished_origin_of_a_live_async_child_keeps_its_identity(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        admin = {"platform": "qq", "is_admin": True, "user_id": "u-admin"}
        entry = _settled(state, sid, task_context={"platform_auth": dict(admin)})
        with state._lock:
            child = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="draw.node",
                input_data={"_dispatch_origin": {"caller_task_id": entry.task_id, "parent_session_id": sid}},
                continuation={}, source_inbound_seq=None,
            )

        assert _prune(state, 0) == 0

        origin = state.task_origin(child.task_id)
        assert origin is not None and origin.platform_auth == admin
