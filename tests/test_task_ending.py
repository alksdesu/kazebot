"""终态不等于真正结束：交回的委派还在路由、结局还在路由、被判失败的 worker 还没停手，都从同一处分类判断。

批次收齐、单项超时、cancel_dispatch、会话忙闲、异步结果的留存各按自己的问题取 continues / still_writing / result_open。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.task_ending import (  # noqa: E402
    STOP_GRACE,
    STOPPING_KEY,
    TRANSIT_GRACE,
    TaskEnding,
    clear_stopping,
    mark_stopping,
    stopping_claim,
    stopping_expired,
    task_ending,
    transit_due,
)
from supervisor.types import RouteStatus, Task, TaskKind, TaskStatus  # noqa: E402

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
_DELIVERY = "task:t-1:completion:1:v1"


def _task(**fields: Any) -> Task:
    values: dict[str, Any] = {
        "task_id": "t-1", "session_id": "s-1", "kind": TaskKind.node, "node_id": "test.node",
        "created_at": _NOW, "updated_at": _NOW,
    }
    values.update(fields)
    return Task(**values)


def _ended(action: str, **fields: Any) -> Task:
    """交回过一次结局、路由还没跑完的任务。"""
    values: dict[str, Any] = {
        "status": TaskStatus.completed, "result": {"action": action},
        "route_status": RouteStatus.pending, "delivery_id": _DELIVERY,
    }
    values.update(fields)
    return _task(**values)


class TestStates:
    @pytest.mark.parametrize("status", [TaskStatus.pending, TaskStatus.running, TaskStatus.suspended])
    def test_a_task_that_has_not_reached_an_end_is_live(self, status: TaskStatus) -> None:
        assert task_ending(_task(status=status), _NOW) is TaskEnding.live

    def test_a_dispatch_on_its_way_to_suspension_counts_until_the_grace_runs_out(self) -> None:
        task = _ended("dispatch")

        assert task_ending(task, _NOW + TRANSIT_GRACE - timedelta(seconds=1)) is TaskEnding.dispatching
        assert task_ending(task, _NOW + TRANSIT_GRACE) is TaskEnding.ended

    def test_a_dispatch_whose_route_failed_can_still_come_back_unless_it_was_called_off(self) -> None:
        failed = _ended("dispatch", route_status=RouteStatus.failed)
        called_off = _ended("dispatch", route_status=RouteStatus.failed, cancel_requested=True)

        assert task_ending(failed, _NOW) is TaskEnding.dispatch_failed
        assert task_ending(called_off, _NOW) is TaskEnding.ended

    def test_a_routed_dispatch_has_ended(self) -> None:
        assert task_ending(_ended("dispatch", route_status=RouteStatus.routed), _NOW) is TaskEnding.ended

    @pytest.mark.parametrize("action, status", [
        ("finish", TaskStatus.completed),
        ("ask", TaskStatus.completed),
        ("fail", TaskStatus.failed),
    ])
    def test_an_outcome_still_being_routed_is_settling(self, action: str, status: TaskStatus) -> None:
        task = _ended(action, status=status)

        assert task_ending(task, _NOW) is TaskEnding.settling
        assert task_ending(task, _NOW + TRANSIT_GRACE) is TaskEnding.ended

    def test_a_task_cancelled_before_it_ever_completed_is_not_waiting_for_a_route(self) -> None:
        # 从没完成过的任务 route_status 默认就是 pending，没有 delivery_id 说明不会有路由来
        task = _ended("cancelled", status=TaskStatus.cancelled, delivery_id="")

        assert task_ending(task, _NOW) is TaskEnding.ended

    def test_a_failed_task_whose_worker_has_not_stopped_is_stopping_even_once_routed(self) -> None:
        task = _ended("fail", status=TaskStatus.failed, route_status=RouteStatus.routed)
        mark_stopping(task, claim_id="claim-1", now=_NOW)

        assert task_ending(task, _NOW + STOP_GRACE - timedelta(seconds=1)) is TaskEnding.stopping
        assert task_ending(task, _NOW + STOP_GRACE) is TaskEnding.ended

    def test_a_stopping_marker_past_its_grace_falls_back_to_the_route_state(self) -> None:
        task = _ended("fail", status=TaskStatus.failed)
        mark_stopping(task, claim_id="claim-1", now=_NOW)

        assert task_ending(task, _NOW + STOP_GRACE) is TaskEnding.settling

    @pytest.mark.parametrize("since", ["yesterday", "", None])
    def test_an_unreadable_marker_is_not_trusted_and_counts_as_expired(self, since: Any) -> None:
        task = _ended("fail", status=TaskStatus.failed, route_status=RouteStatus.routed)
        task.input[STOPPING_KEY] = {"claim_id": "claim-1", "since": since}

        assert task_ending(task, _NOW) is TaskEnding.ended
        assert stopping_expired(task, _NOW) is True

    def test_a_marker_without_a_time_zone_is_read_as_utc(self) -> None:
        task = _ended("fail", status=TaskStatus.failed, route_status=RouteStatus.routed)
        task.input[STOPPING_KEY] = {"claim_id": "claim-1", "since": "2026-10-30T12:00:00"}

        assert task_ending(task, _NOW + STOP_GRACE - timedelta(seconds=1)) is TaskEnding.stopping
        assert stopping_expired(task, _NOW + STOP_GRACE) is True


class TestQuestions:
    @pytest.mark.parametrize("ending, continues, still_writing, result_open", [
        (TaskEnding.live, True, True, True),
        (TaskEnding.dispatching, True, True, True),
        (TaskEnding.dispatch_failed, True, True, False),
        (TaskEnding.stopping, False, True, True),
        (TaskEnding.settling, False, False, True),
        (TaskEnding.ended, False, False, False),
    ])
    def test_each_question_reads_the_same_states(
        self, ending: TaskEnding, continues: bool, still_writing: bool, result_open: bool,
    ) -> None:
        assert (ending.continues, ending.still_writing, ending.result_open) == (continues, still_writing, result_open)


class TestTransitDue:
    def test_a_stopping_task_is_due_one_stop_grace_after_it_was_failed(self) -> None:
        task = _ended("fail", status=TaskStatus.failed)
        mark_stopping(task, claim_id="claim-1", now=_NOW)

        assert transit_due(task, TaskEnding.stopping) == _NOW + STOP_GRACE

    @pytest.mark.parametrize("ending", [TaskEnding.dispatching, TaskEnding.dispatch_failed, TaskEnding.settling])
    def test_a_route_in_transit_is_due_one_transit_grace_after_its_last_change(self, ending: TaskEnding) -> None:
        assert transit_due(_ended("dispatch"), ending) == _NOW + TRANSIT_GRACE

    @pytest.mark.parametrize("ending", [TaskEnding.live, TaskEnding.ended])
    def test_outside_the_transit_nothing_is_due(self, ending: TaskEnding) -> None:
        assert transit_due(_task(), ending) is None


class TestStoppingMarker:
    def test_only_the_claim_that_was_stopped_can_clear_it(self) -> None:
        task = _ended("fail", status=TaskStatus.failed)
        mark_stopping(task, claim_id="claim-1", now=_NOW)

        assert clear_stopping(task, "claim-2") is False
        assert stopping_claim(task) == "claim-1"
        assert clear_stopping(task, "claim-1") is True
        assert STOPPING_KEY not in task.input
        assert clear_stopping(task, "claim-1") is False

    def test_the_grace_handler_clears_it_whoever_held_it(self) -> None:
        task = _ended("fail", status=TaskStatus.failed)
        mark_stopping(task, claim_id="claim-1", now=_NOW)

        assert stopping_expired(task, _NOW + STOP_GRACE - timedelta(seconds=1)) is False
        assert stopping_expired(task, _NOW + STOP_GRACE) is True
        assert clear_stopping(task) is True
        assert stopping_expired(task, _NOW + STOP_GRACE) is False
