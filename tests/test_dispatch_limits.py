"""委派上限：建任务时记下层数和所属的任务树，wait 批次和异步入站超出上限的那一项当场拒，其余照派。

层数从用户那一轮算 0；任务树对应一次用户请求，回调落在它接着做的那一轮那一层，自动续跑和上一段同层。
"""
from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import dispatch_batch  # noqa: E402
from engine.dispatch_batch import (  # noqa: E402
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_LIVE,
    INBOUND_BAD_MODE_STATUS,
    INBOUND_CONFLICT_STATUS,
    MAX_DEPTH_CEILING,
    MAX_LIVE_CEILING,
    DispatchLimits,
)
from engine.dispatch_results import INBOUND_CONTINUES_TASK_KEY  # noqa: E402
from engine.inference.pseudo_tools import _dispatch_delegate_spec, _dispatch_delegate_specs  # noqa: E402
from engine.run_budget import AUTO_CONTINUE_KEY  # noqa: E402
from supervisor import dispatch_limits  # noqa: E402
from supervisor.dispatch_limits import (  # noqa: E402
    CALLBACK_PRODUCER_KEY,
    DISPATCH_DEPTH_KEY,
    DISPATCH_TREE_KEY,
    task_place_locked,
)
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_group:3003"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}


@pytest.fixture(autouse=True)
def _default_running_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", raising=False)


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-dispatch-limits"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _write_limits(workspace: Path, **limits: Any) -> None:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"engine": {"dispatch": limits}}), encoding="utf-8")


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq", conversation_key=key)


def _inbound(state: SupervisorState, session_id: str, *, node: str = "test.orchestrator", **extra: Any) -> Task:
    payload = {
        "channel": "qq",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": f"run {node}",
        "entry_node_id": node,
        "platform_auth": dict(AUTH),
        **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    task = state.record_inbound_message_event(event)
    assert task is not None
    return task


def _route_session(state: SupervisorState, task: Task) -> str:
    with state._lock:
        return state._route_session_id_for_task_locked(task)


def _origin(state: SupervisorState, caller: Task, *, mode: str = "fresh") -> dict[str, Any]:
    return {
        "parent_session_id": _route_session(state, caller), "caller_node_id": str(caller.node_id or ""),
        "parent_conversation_key": CONV, "context_mode": mode, "caller_task_id": caller.task_id,
    }


def _async_child(state: SupervisorState, caller: Task, *, node: str = "a.node") -> Task:
    """照 engine 异步委派那样投一条带 dispatch_origin 的入站，子节点在自己的 agent: 会话里跑。"""
    agent_sid = state.get_or_create_session(channel="qq", conversation_key=f"agent:{node}:{CONV}:{uuid.uuid4()}")
    return _inbound(state, agent_sid, node=node, dispatch_context_mode="fresh", dispatch_origin=_origin(state, caller))


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


def _item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def _tool_item(target: str = "read_file") -> dict[str, Any]:
    return {"kind": "tool", "target": target, "arguments": {"path": "a.txt"}, "call_id": f"call-{target}"}


def _wait_batch(state: SupervisorState, caller: Task, *items: dict[str, Any]) -> list[Task]:
    """caller 这一程交回 wait 批次；返回这一批的子任务（派出的和当场拒的），按批次序号排。"""
    _report(state, caller, {"action": "dispatch", "dispatch_batch": list(items)})
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id and task.batch_id),
        key=lambda task: task.batch_index,
    )


def _callbacks(state: SupervisorState) -> list[Task]:
    return [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]


def _place(state: SupervisorState, task: Task) -> tuple[int, str]:
    with state._lock:
        place = task_place_locked(state, task)
    return place.depth, place.tree


def _create(state: SupervisorState, session_id: str, input_data: dict[str, Any], **fields: Any) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=fields.pop("kind", TaskKind.node),
            node_id=fields.pop("node_id", "test.node"),
            input_data=input_data,
            continuation={},
            **fields,
        )


def _refused(task: Task) -> str:
    assert task.status == TaskStatus.failed, task.status
    return str(task.result.get("error") or "")


class TestConfig:
    @pytest.fixture(autouse=True)
    def _fresh_warnings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dispatch_batch, "_WARNED_LIMIT_VALUES", set())

    def test_unwritten_limits_use_the_defaults(self) -> None:
        assert DispatchLimits.from_config({}) == DispatchLimits(max_depth=DEFAULT_MAX_DEPTH, max_live=DEFAULT_MAX_LIVE)
        assert DispatchLimits.from_config(None) == DispatchLimits(DEFAULT_MAX_DEPTH, DEFAULT_MAX_LIVE)

    def test_zero_lifts_a_limit(self) -> None:
        limits = DispatchLimits.from_config({"engine": {"dispatch": {"max_depth": 0, "max_live_descendants": 0}}})

        assert limits == DispatchLimits(0, 0)
        assert not limits.depth_exceeded(1000) and not limits.live_full(1000)

    def test_values_over_the_ceiling_are_capped(self) -> None:
        limits = DispatchLimits.from_config({"engine": {"dispatch": {"max_depth": 999, "max_live_descendants": 99999}}})

        assert limits == DispatchLimits(MAX_DEPTH_CEILING, MAX_LIVE_CEILING)

    @pytest.mark.parametrize("raw", [-1, "abc", True, 4.5])
    def test_a_negative_or_malformed_value_falls_back_to_the_default_and_warns_once(
        self, raw: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="engine.dispatch_batch")
        cfg = {"engine": {"dispatch": {"max_depth": raw}}}

        assert DispatchLimits.from_config(cfg).max_depth == DEFAULT_MAX_DEPTH
        assert DispatchLimits.from_config(cfg).max_depth == DEFAULT_MAX_DEPTH

        warnings = [record.getMessage() for record in caplog.records if record.name == "engine.dispatch_batch"]
        assert len(warnings) == 1 and "engine.dispatch.max_depth" in warnings[0], warnings

    def test_the_checks_compare_against_the_limits(self) -> None:
        limits = DispatchLimits(max_depth=2, max_live=3)

        assert not limits.depth_exceeded(2) and limits.depth_exceeded(3)
        assert not limits.live_full(2) and limits.live_full(3)


class TestDepthAndTree:
    def test_a_user_turn_is_the_root_of_its_tree(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        assert _place(state, entry) == (0, entry.task_id)
        assert entry.input[DISPATCH_DEPTH_KEY] == 0 and entry.input[DISPATCH_TREE_KEY] == entry.task_id

    def test_wait_children_are_one_level_deeper_in_the_same_tree(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        [child] = _wait_batch(state, entry, _item())
        [grandchild] = _wait_batch(state, child, _item("b.node"))

        assert _place(state, child) == (1, entry.task_id)
        assert _place(state, grandchild) == (2, entry.task_id)

    def test_async_children_follow_their_dispatcher(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        child = _async_child(state, entry)
        grandchild = _async_child(state, child, node="b.node")

        assert _place(state, child) == (1, entry.task_id)
        assert _place(state, grandchild) == (2, entry.task_id)

    def test_values_written_by_anyone_else_are_overwritten(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        task = _create(state, _session(state), {DISPATCH_DEPTH_KEY: 99, DISPATCH_TREE_KEY: "forged"})

        assert _place(state, task) == (0, task.task_id)

    def test_an_auto_continued_round_keeps_the_depth_and_tree_of_the_previous_one(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        [child] = _wait_batch(state, entry, _item())

        continued = _create(state, _session(state), {AUTO_CONTINUE_KEY: {"previous_task_id": child.task_id}})

        assert _place(state, continued) == (1, entry.task_id)

    def test_snapshots_after_task_created_keep_depth_and_tree(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        [child] = _wait_batch(state, entry, _item())

        _report(state, child, {"action": "finish", "result": {"text": "done"}})

        completed = next(
            event["payload"] for event in reversed(list(state.eventlog.iter_persisted_events()))
            if event.get("type") == "task_completed" and event["payload"].get("task_id") == child.task_id
        )
        assert completed["input"]["_input_slimmed"] is True
        assert completed["input"][DISPATCH_DEPTH_KEY] == 1
        assert completed["input"][DISPATCH_TREE_KEY] == entry.task_id


class TestCallbacks:
    def test_a_callback_takes_the_depth_and_tree_of_the_round_it_continues(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)
        entry = _inbound(state, sid)
        middle = _async_child(state, entry)
        producer = _async_child(state, middle, node="b.node")

        # accumulate 的中间层接着做：回调带着中间层原来的 dispatch_origin，不能当成又一次委派
        callback = _create(state, sid, {
            CALLBACK_PRODUCER_KEY: producer.task_id,
            INBOUND_CONTINUES_TASK_KEY: middle.task_id,
            "_dispatch_origin": _origin(state, entry, mode="accumulate"),
            "parent_session_id": _route_session(state, middle),
        })

        assert _place(state, callback) == (1, entry.task_id)

    def test_a_result_delivered_back_to_the_user_turn_is_at_depth_zero(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        child = _async_child(state, entry)
        # 那一轮还没做完时结果先留给它，不另起一轮
        _report(state, entry, {"action": "finish", "result": {"text": "派出去了"}})

        _report(state, child, {"action": "finish", "result": {"text": "done"}})

        [callback] = _callbacks(state)
        assert callback.input[CALLBACK_PRODUCER_KEY] == child.task_id
        assert _place(state, callback) == (0, entry.task_id)

    def test_a_result_back_in_the_producers_own_session_continues_the_producer(self, tmp_path: Path) -> None:
        # 异步工具的结果回到产出它的子节点自己的会话：接着做的就是那个子节点
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        child = _async_child(state, entry)

        callback = _create(state, _session(state), {
            CALLBACK_PRODUCER_KEY: child.task_id, "parent_session_id": _route_session(state, child),
        })

        assert _place(state, callback) == (1, entry.task_id)

    def test_a_result_handed_to_the_top_of_a_sync_chain_takes_its_depth(self, tmp_path: Path) -> None:
        # 同步子任务的路由会话也是用户会话，只拿同步链最上面的任务比对
        state = _state(tmp_path)
        sid = _session(state)
        entry = _inbound(state, sid)
        [first] = _wait_batch(state, entry, _item())
        [second] = _wait_batch(state, first, _item("b.node"))

        callback = _create(state, sid, {
            CALLBACK_PRODUCER_KEY: second.task_id, "parent_session_id": _route_session(state, entry),
        })

        assert _place(state, second) == (2, entry.task_id)
        assert _place(state, callback) == (0, entry.task_id)

    def test_without_a_landing_on_the_chain_a_callback_sits_one_level_above_its_producer(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        child = _async_child(state, entry)
        producer = _async_child(state, child, node="b.node")
        elsewhere = _session(state, "qq_group:elsewhere")

        callback = _create(state, elsewhere, {CALLBACK_PRODUCER_KEY: producer.task_id, "parent_session_id": elsewhere})

        assert _place(state, callback) == (1, entry.task_id)

    def test_a_callback_whose_producer_is_gone_starts_from_zero(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = _session(state)

        callback = _create(state, sid, {CALLBACK_PRODUCER_KEY: "gone-task", "parent_session_id": sid})

        assert _place(state, callback) == (0, "gone-task")


class TestTasksWithoutStoredPlaces:
    def test_a_task_without_the_keys_is_worked_out_from_its_chain(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        [child] = _wait_batch(state, entry, _item())
        child.input.pop(DISPATCH_DEPTH_KEY)
        child.input.pop(DISPATCH_TREE_KEY)

        assert _place(state, child) == (1, entry.task_id)

    def test_a_chain_that_breaks_starts_a_tree_at_the_missing_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        now = datetime.now(timezone.utc)
        orphan = Task(
            task_id="legacy-child", session_id=_session(state), kind=TaskKind.node, node_id="a.node",
            caller_task_id="gone-caller", status=TaskStatus.pending, created_at=now, updated_at=now,
        )
        state.tasks[orphan.task_id] = orphan

        assert _place(state, orphan) == (1, "gone-caller")


class TestWaitBatchLimits:
    def test_an_item_past_the_depth_limit_is_refused_and_the_rest_still_go(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_depth=1)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        [child] = _wait_batch(state, entry, _item())
        assert child.status == TaskStatus.pending

        node_item, tool_item = _wait_batch(state, child, _item("b.node"), _tool_item())

        error = _refused(node_item)
        assert "第 2 层" in error and "上限 1 层" in error
        assert tool_item.kind == TaskKind.tool and tool_item.status == TaskStatus.pending

    def test_the_live_limit_counts_the_items_admitted_in_the_same_batch(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_live_descendants=2)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        first, second, third = _wait_batch(state, entry, _item(), _item("b.node"), _item("c.node"))

        assert first.status == TaskStatus.pending and second.status == TaskStatus.pending
        error = _refused(third)
        assert "已有 2 个" in error and "上限是 2 个" in error

    def test_a_caller_on_its_way_to_suspending_counts_as_live(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_live_descendants=2)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        [child] = _wait_batch(state, entry, _item())

        admitted, refused = _wait_batch(state, child, _item("b.node"), _item("c.node"))

        assert admitted.status == TaskStatus.pending
        assert "已有 2 个" in _refused(refused)

    def test_other_trees_and_system_tasks_do_not_count(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_live_descendants=1)
        state = _state(tmp_path)
        sid = _session(state)
        first_turn = _inbound(state, sid)
        second_turn = _inbound(state, sid)
        [busy] = _wait_batch(state, first_turn, _item())
        assert busy.status == TaskStatus.pending
        _create(state, sid, {}, node_id="system.compactor", caller_task_id=second_turn.task_id)

        [other] = _wait_batch(state, second_turn, _item())

        assert other.status == TaskStatus.pending

    def test_zero_lifts_both_limits(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_depth=0, max_live_descendants=0)
        state = _state(tmp_path)
        caller = _inbound(state, _session(state))
        for depth in range(1, 7):
            children = _wait_batch(state, caller, _item(), _item("b.node"))
            assert all(child.status == TaskStatus.pending for child in children)
            assert _place(state, children[0])[0] == depth
            caller = children[0]


class TestAsyncInboundLimits:
    @staticmethod
    def _payload(state: SupervisorState, dispatcher: Task, **origin: Any) -> dict[str, Any]:
        return {
            "channel": "qq", "conversation_key": f"agent:b.node:{CONV}:{uuid.uuid4()}", "text": "do b",
            "entry_node_id": "b.node", "dispatch_origin": {**_origin(state, dispatcher), **origin},
            "dispatch_context_mode": "fresh",
        }

    def test_an_async_dispatch_past_the_depth_limit_is_refused(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_depth=1)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        child = _async_child(state, entry)

        with state._lock:
            refusal = dispatch_limits.inbound_refusal_locked(state, self._payload(state, child))

        assert refusal is not None and refusal.status == INBOUND_CONFLICT_STATUS
        assert "b.node" in refusal.message and "第 2 层" in refusal.message

    def test_an_async_dispatch_over_the_live_limit_is_refused(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_live_descendants=1)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))
        _async_child(state, entry)

        with state._lock:
            refusal = dispatch_limits.inbound_refusal_locked(state, self._payload(state, entry))

        assert refusal is not None and refusal.status == INBOUND_CONFLICT_STATUS
        assert "已有 1 个" in refusal.message

    def test_an_async_dispatch_within_the_limits_is_let_through(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        with state._lock:
            assert dispatch_limits.inbound_refusal_locked(state, self._payload(state, entry)) is None

    def test_a_dispatch_origin_without_its_caller_is_refused_as_malformed(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        with state._lock:
            refusal = dispatch_limits.inbound_refusal_locked(state, self._payload(state, entry, caller_task_id=""))

        assert refusal is not None and refusal.status == INBOUND_BAD_MODE_STATUS

    def test_a_dispatcher_the_supervisor_does_not_know_is_refused(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        with state._lock:
            refusal = dispatch_limits.inbound_refusal_locked(
                state, self._payload(state, entry, caller_task_id="no-such-task"),
            )

        assert refusal is not None and refusal.status == INBOUND_CONFLICT_STATUS

    def test_plain_inbound_and_lifted_limits_are_not_checked(self, tmp_path: Path) -> None:
        _write_limits(tmp_path, max_depth=0, max_live_descendants=0)
        state = _state(tmp_path)
        entry = _inbound(state, _session(state))

        with state._lock:
            assert dispatch_limits.inbound_refusal_locked(state, {"conversation_key": CONV, "text": "hi"}) is None
            assert dispatch_limits.inbound_refusal_locked(
                state, self._payload(state, entry, caller_task_id="no-such-task"),
            ) is None


def _limits_line(spec: dict[str, Any]) -> str | None:
    description = spec["function"]["description"]
    return next((line for line in description.splitlines() if line.startswith("委派有上限")), None)


class TestDelegateToolDescription:
    def test_the_description_states_the_configured_limits(self) -> None:
        line = _limits_line(_dispatch_delegate_spec("scout", limits=DispatchLimits(max_depth=3, max_live=5)))

        assert line is not None
        assert "往下最多 3 层" in line and "（含挂起等结果的）最多 5 个" in line

    def test_a_lifted_limit_is_left_out(self) -> None:
        only_live = _limits_line(_dispatch_delegate_spec("scout", limits=DispatchLimits(max_depth=0, max_live=5)))
        only_depth = _limits_line(_dispatch_delegate_spec("scout", limits=DispatchLimits(max_depth=3, max_live=0)))

        assert only_live is not None and "最多 5 个" in only_live and "往下最多" not in only_live
        assert only_depth is not None and "往下最多 3 层" in only_depth and "同时没结束" not in only_depth
        assert _limits_line(_dispatch_delegate_spec("scout", limits=DispatchLimits(0, 0))) is None

    def test_the_default_limits_are_described_when_none_are_given(self) -> None:
        line = _limits_line(_dispatch_delegate_spec("scout"))

        assert line is not None
        assert f"往下最多 {DEFAULT_MAX_DEPTH} 层" in line and f"最多 {DEFAULT_MAX_LIVE} 个" in line

    def test_every_delegate_tool_gets_the_same_limits(self) -> None:
        specs = _dispatch_delegate_specs(["scout", "coder"], limits=DispatchLimits(max_depth=2, max_live=3))

        lines = [_limits_line(spec) for spec in specs]
        assert len(lines) == 2 and lines[0] == lines[1]
        assert lines[0] is not None and "往下最多 2 层" in lines[0] and "最多 3 个" in lines[0]
