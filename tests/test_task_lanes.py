"""任务分道：建任务时定道并随快照落盘；领任务时快道插队、交互道在前、后台道全局限并发，worker 只剩保留的空位时不给后台道。"""
from __future__ import annotations

import asyncio
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.auto_continue import ContinuePlan, start_locked  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_lanes import LaneLimits, TaskLane, derive_lane, parse_lane  # noqa: E402
from supervisor.task_requeue import storage_session  # noqa: E402
from supervisor.task_traits import REQUEUE_MARK  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_TOKEN = "task-lanes-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
CONV = "qq_group:42"
WORKER_NODE = "worker.coder"
HELPER_NODE = "helper.node"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / f"{WORKER_NODE}.yaml").write_text(
        "type: ai\nname: worker\nauto_continue: true\nprompt: Work.\n", encoding="utf-8",
    )
    (nodes / f"{HELPER_NODE}.yaml").write_text("type: ai\nname: helper\nprompt: Help.\n", encoding="utf-8")
    return tmp_path


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-task-lanes"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _runtime(workspace: Path, text: str) -> None:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="qq_group", conversation_key=CONV)


def _inbound(state: SupervisorState, session_id: str, payload: dict[str, Any]) -> Task:
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _user_message(state: SupervisorState, session_id: str, text: str = "在吗") -> Task:
    conversation_key = state.sessions[session_id].conversation_key
    return _inbound(state, session_id, {"channel": "qq_group", "conversation_key": conversation_key, "text": text})


def _queue(
    state: SupervisorState, session_id: str, *, node_id: str = "test.node", **input_data: Any,
) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id=node_id,
            input_data={"task_context": {"conversation_key": CONV}, "instruction": node_id, **input_data},
            continuation={},
            source_inbound_seq=None,
        )


def _background(state: SupervisorState, session_id: str, node_id: str = "system.memory_extractor") -> Task:
    return _queue(state, session_id, node_id=node_id, _system_task=True)


def _intent(state: SupervisorState, session_id: str) -> Task:
    return _queue(state, session_id, node_id="chat.intent", _system_task=True, _reply_intent=True)


def _async_child(state: SupervisorState, parent_session_id: str, sender: Task, *, suffix: str) -> Task:
    """sender 用 dispatch:{target} 异步派出去的子任务：一条带 dispatch_origin 的入站。"""
    conversation_key = f"agent:{HELPER_NODE}:{CONV}:{suffix}"
    child_session = state.get_or_create_session(channel="qq_group", conversation_key=conversation_key)
    return _inbound(state, child_session, {
        "channel": "qq_group",
        "conversation_key": conversation_key,
        "text": "去查一下",
        "entry_node_id": HELPER_NODE,
        "dispatch_origin": {
            "parent_session_id": parent_session_id,
            "caller_node_id": str(sender.node_id or ""),
            "caller_task_id": sender.task_id,
            "parent_conversation_key": CONV,
            "context_mode": "fresh",
        },
        "dispatch_context_mode": "fresh",
    })


def _claim(state: SupervisorState, worker: str = "engine-a", **slots: Any) -> dict[str, Any] | None:
    return state.assign_next_task(worker_id=worker, **slots)


def _complete(state: SupervisorState, claimed: dict[str, Any], result: dict[str, Any], worker: str = "engine-a") -> None:
    state.complete_task(task_id=claimed["task_id"], worker_id=worker, claim_id=claimed["claim_id"], result=result)


def _finish(state: SupervisorState, claimed: dict[str, Any], worker: str = "engine-a") -> None:
    _complete(state, claimed, {"action": "finish", "result": {"text": "好了"}}, worker)


def _children(state: SupervisorState, caller: Task) -> list[Task]:
    return sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id),
        key=lambda task: task.batch_index,
    )


def _events(state: SupervisorState, event_type: str, task_id: str) -> list[dict[str, Any]]:
    return [
        event for event in state.eventlog.iter_persisted_events()
        if event.get("type") == event_type and (event.get("payload") or {}).get("task_id") == task_id
    ]


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


class TestLaneRule:
    def test_the_reply_intent_judgement_jumps_the_queue(self) -> None:
        assert derive_lane({"_system_task": True, "_reply_intent": True}) is TaskLane.fast

    @pytest.mark.parametrize("marker", [
        {"_system_task": True},
        {"_compact_async": True},
        {"_turn_summary_dispatch": True},
        {"_memory_extract_intent_id": "intent-1"},
        {"execution_plan_id": "P0000000001"},
        {"task_context": {"is_system_task": True}},
    ])
    def test_system_flows_go_to_the_background(self, marker: dict[str, Any]) -> None:
        assert derive_lane(marker) is TaskLane.background

    def test_everything_else_is_interactive(self) -> None:
        assert derive_lane({"instruction": "帮我看看日志"}) is TaskLane.interactive
        assert derive_lane({"_system_task": False, "task_context": "not a dict"}) is TaskLane.interactive
        assert derive_lane(None) is TaskLane.interactive
        assert derive_lane(["not", "a", "mapping"]) is TaskLane.interactive

    def test_only_the_background_is_passed_down(self) -> None:
        assert derive_lane({}, inherited=TaskLane.background) is TaskLane.background
        assert derive_lane({}, inherited=TaskLane.interactive) is TaskLane.interactive
        assert derive_lane({}, inherited=TaskLane.fast) is TaskLane.interactive

    def test_the_task_s_own_marker_beats_the_inherited_lane(self) -> None:
        assert derive_lane({"_system_task": True}, inherited=TaskLane.interactive) is TaskLane.background
        assert derive_lane({"_reply_intent": True}, inherited=TaskLane.background) is TaskLane.fast

    def test_parse_lane_only_knows_the_three_lanes(self) -> None:
        assert parse_lane("fast") is TaskLane.fast
        assert parse_lane(TaskLane.background) is TaskLane.background
        assert parse_lane("turbo") is None
        assert parse_lane(None) is None
        assert parse_lane("") is None


class TestLaneLimits:
    def test_defaults_match_the_bundled_runtime_config(self, tmp_path: Path) -> None:
        from clonoth_runtime import load_runtime_config

        limits = LaneLimits.from_config(load_runtime_config(tmp_path))
        assert limits == LaneLimits(background_max_running=2, interactive_reserve=1)

    def test_bad_values_fall_back_and_negative_ones_are_clamped(self) -> None:
        limits = LaneLimits.from_config({"engine": {"lanes": {"background_max_running": "lots", "interactive_reserve": -3}}})
        assert limits == LaneLimits(background_max_running=2, interactive_reserve=0)
        assert LaneLimits.from_config(None) == LaneLimits()  # type: ignore[arg-type]

    def test_a_worker_takes_background_work_only_beyond_the_reserve(self) -> None:
        limits = LaneLimits(background_max_running=2, interactive_reserve=1)
        assert limits.worker_takes_background(None, None) is True
        assert limits.worker_takes_background(2, 4) is True
        assert limits.worker_takes_background(1, 4) is False
        assert limits.worker_takes_background(1, None) is False
        assert limits.worker_takes_background(1, 1) is True

    def test_zero_means_no_global_background_limit(self) -> None:
        assert LaneLimits(background_max_running=0).background_has_room(100) is True
        assert LaneLimits(background_max_running=2).background_has_room(1) is True
        assert LaneLimits(background_max_running=2).background_has_room(2) is False


class TestLegacySnapshots:
    @staticmethod
    def _legacy(**extra: Any) -> dict[str, Any]:
        return {
            "task_id": "legacy", "session_id": "s", "kind": "node", "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            **extra,
        }

    def test_a_snapshot_without_lane_is_derived_from_its_input(self) -> None:
        assert Task.model_validate(self._legacy()).lane is TaskLane.interactive
        assert Task.model_validate(self._legacy(input={"_system_task": True})).lane is TaskLane.background
        assert Task.model_validate(
            self._legacy(input={"_system_task": True, "_reply_intent": True}),
        ).lane is TaskLane.fast
        assert Task.model_validate(
            self._legacy(input={"task_context": {"is_system_task": True}}),
        ).lane is TaskLane.background

    def test_an_unknown_lane_value_is_derived_again(self) -> None:
        task = Task.model_validate(self._legacy(lane="turbo", input={"_system_task": True}))
        assert task.lane is TaskLane.background

    def test_a_stored_lane_is_kept_and_written_back(self) -> None:
        task = Task.model_validate(self._legacy(lane="background", input={"instruction": "x"}))
        assert task.lane is TaskLane.background
        dumped = task.model_dump(mode="json")
        assert dumped["lane"] == "background"
        assert Task.model_validate(dumped).lane is TaskLane.background

    def test_replaying_slimmed_snapshots_derives_from_the_full_input(self, tmp_path: Path) -> None:
        # 后面的快照只存瘦身过的 input，_reply_intent 不在保留的键里：要和 task_created 那份并起来推
        state = _state(tmp_path)
        state.rebuild_from_events([
            {"type": "task_created", "session_id": "s", "payload": self._legacy(
                input={"_system_task": True, "_reply_intent": True, "instruction": "判一下"},
            )},
            {"type": "task_started", "session_id": "s", "payload": self._legacy(
                status="running", input={"_input_slimmed": True, "_system_task": True},
            )},
        ])
        assert state.tasks["legacy"].lane is TaskLane.fast
        assert state.tasks["legacy"].status == TaskStatus.running


class TestLaneAtCreation:
    def test_a_user_message_is_interactive_and_the_lane_is_persisted(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _user_message(state, _session(state))

        assert task.lane is TaskLane.interactive
        [created] = _events(state, "task_created", task.task_id)
        assert created["payload"]["lane"] == "interactive"

    def test_system_flows_created_through_hooks_are_background(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        with state._lock:
            extractor = state._create_task_from_hook_locked(
                session_id=sid, kind="node", node_id="system.memory_extractor",
                input_data={"instruction": "x", "_system_task": True, "_memory_extract_intent_id": "i-1"},
            )
            dream = state._create_task_from_hook_locked(
                conversation_key="memory:dream", channel="system", kind="node", node_id="system.dream",
                input_data={"instruction": "x", "_system_task": True, "task_context": {"is_system_task": True}},
            )
        assert extractor.lane is TaskLane.background
        assert dream.lane is TaskLane.background

    def test_an_explicit_lane_wins_and_an_unknown_one_is_derived(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        with state._lock:
            pinned = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="test.node",
                input_data={"instruction": "x"}, lane="background",
            )
            garbled = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="test.node",
                input_data={"instruction": "x", "_system_task": True}, lane="turbo",
            )
        assert pinned.lane is TaskLane.background
        assert garbled.lane is TaskLane.background

    def test_wait_dispatched_children_work_in_the_lane_of_their_caller(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)

        background_caller = _background(state, sid, "system.execution")
        claimed = _claim(state)
        assert claimed is not None and claimed["task_id"] == background_caller.task_id
        _complete(state, claimed, {
            "action": "dispatch", "target_node": HELPER_NODE,
            "dispatch_input": {"instruction": "压缩前先查一下", "context_mode": "fresh"},
        })
        [background_child] = _children(state, background_caller)
        assert background_caller.status == TaskStatus.suspended
        assert background_child.lane is TaskLane.background

        user_caller = _user_message(state, sid)
        claimed = _claim(state)
        assert claimed is not None and claimed["task_id"] == user_caller.task_id
        _complete(state, claimed, {
            "action": "dispatch",
            "dispatch_batch": [
                {"kind": "node", "target": HELPER_NODE, "instruction": "查 A", "context_mode": "fresh"},
                {"kind": "tool", "target": "read_file", "arguments": {"path": "README.md"}},
            ],
        })
        node_child, tool_child = _children(state, user_caller)
        assert node_child.lane is TaskLane.interactive
        assert tool_child.kind == TaskKind.tool and tool_child.lane is TaskLane.interactive

    def test_an_async_child_follows_the_task_that_sent_it(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)

        assert _async_child(state, sid, _background(state, sid, "system.execution"), suffix="1").lane is TaskLane.background
        assert _async_child(state, sid, _user_message(state, sid), suffix="2").lane is TaskLane.interactive
        # 插队只给接话判断本身，它派出去的活不跟着插队
        assert _async_child(state, sid, _intent(state, sid), suffix="3").lane is TaskLane.interactive

    def test_the_resumed_caller_and_the_auto_continue_round_are_interactive(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        user = _user_message(state, sid)
        claimed = _claim(state)
        assert claimed is not None and claimed["task_id"] == user.task_id

        child = _async_child(state, sid, user, suffix="cb")
        claimed_child = _claim(state)
        assert claimed_child is not None and claimed_child["task_id"] == child.task_id
        _finish(state, claimed)
        _finish(state, claimed_child)
        [callback] = [
            task for task in state.tasks.values()
            if task.input.get("inbound_message_type") == "dispatch_result" and task.input.get("inbound_child_task_id") == child.task_id
        ]
        assert callback.lane is TaskLane.interactive

        plan = ContinuePlan(
            go=True, note="", round=1, max_rounds=3,
            chain_started_at=datetime.now(timezone.utc), origin_task_id=user.task_id,
        )
        with state._lock:
            continued = start_locked(state, user, plan)
        assert continued is not None and continued.lane is TaskLane.interactive


class TestClaimOrder:
    def test_fast_first_then_interactive_then_background_each_in_queue_order(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        summary = _background(state, sid, "system.turn_summarizer")
        first_user = _user_message(state, sid)
        intent = _intent(state, sid)
        second_user = _user_message(state, sid, "还有一件事")

        claims = [_claim(state) for _ in range(4)]

        assert [item["task_id"] for item in claims if item] == [
            intent.task_id, first_user.task_id, second_user.task_id, summary.task_id,
        ]
        assert [item["lane"] for item in claims if item] == ["fast", "interactive", "interactive", "background"]
        assert _claim(state) is None

    def test_the_claimed_payload_keeps_the_fields_the_engine_reads(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _user_message(state, _session(state))

        claimed = _claim(state, free_slots=4, capacity=4)

        assert claimed is not None and claimed["task_id"] == task.task_id
        assert claimed["lane"] == "interactive"
        assert claimed["claim_id"] and claimed["claimed_at"] and claimed["created_at"]
        assert isinstance(claimed["input"], dict)
        [started] = _events(state, "task_started", task.task_id)
        assert started["payload"]["lane"] == "interactive"

    def test_queued_background_work_never_delays_a_new_message_or_intent(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        for index in range(5):
            _background(state, sid, f"system.job{index}")

        first = _claim(state, "engine-a", free_slots=4, capacity=4)
        second = _claim(state, "engine-a", free_slots=3, capacity=4)
        assert first is not None and second is not None
        assert first["lane"] == second["lane"] == "background"
        assert _claim(state, "engine-a", free_slots=2, capacity=4) is None

        user = _user_message(state, sid, "@bot 帮我看看")
        intent = _intent(state, sid)

        fast = _claim(state, "engine-b", free_slots=4, capacity=4)
        interactive = _claim(state, "engine-a", free_slots=2, capacity=4)
        assert fast is not None and fast["task_id"] == intent.task_id
        assert interactive is not None and interactive["task_id"] == user.task_id
        assert _claim(state, "engine-b", free_slots=3, capacity=4) is None

    def test_the_background_limit_counts_every_worker(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        jobs = [_background(state, sid, f"system.job{index}") for index in range(3)]

        on_a = _claim(state, "engine-a", free_slots=4, capacity=4)
        on_b = _claim(state, "engine-b", free_slots=4, capacity=4)
        assert on_a is not None and on_a["task_id"] == jobs[0].task_id
        assert on_b is not None and on_b["task_id"] == jobs[1].task_id
        assert _claim(state, "engine-a", free_slots=3, capacity=4) is None
        assert _claim(state, "engine-b", free_slots=3, capacity=4) is None

        _finish(state, on_a, "engine-a")

        third = _claim(state, "engine-b", free_slots=3, capacity=4)
        assert third is not None and third["task_id"] == jobs[2].task_id

    def test_the_last_free_slot_is_kept_for_interactive_work(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        job = _background(state, sid)

        assert _claim(state, free_slots=1, capacity=4) is None
        assert job.status == TaskStatus.pending

        user = _user_message(state, sid)
        claimed_user = _claim(state, free_slots=1, capacity=4)
        assert claimed_user is not None and claimed_user["task_id"] == user.task_id
        claimed_job = _claim(state, free_slots=2, capacity=4)
        assert claimed_job is not None and claimed_job["task_id"] == job.task_id

    def test_an_idle_single_slot_worker_still_runs_background_work(self, workspace: Path) -> None:
        state = _state(workspace)
        job = _background(state, _session(state))

        claimed = _claim(state, free_slots=1, capacity=1)

        assert claimed is not None and claimed["task_id"] == job.task_id

    def test_a_worker_without_free_slots_gets_nothing(self, workspace: Path) -> None:
        state = _state(workspace)
        user = _user_message(state, _session(state))

        assert _claim(state, free_slots=0, capacity=4) is None
        assert user.status == TaskStatus.pending

    def test_old_engines_without_a_slot_report_only_meet_the_global_limit(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        jobs = [_background(state, sid, f"system.job{index}") for index in range(3)]

        claimed = [_claim(state) for _ in range(3)]

        assert [item["task_id"] for item in claimed if item] == [jobs[0].task_id, jobs[1].task_id]
        assert claimed[2] is None

    def test_a_background_task_waiting_for_approval_does_not_count(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        jobs = [_background(state, sid, f"system.job{index}") for index in range(4)]
        parked = _claim(state, "engine-a", free_slots=4, capacity=4)
        _claim(state, "engine-a", free_slots=3, capacity=4)
        assert parked is not None and _claim(state, "engine-b", free_slots=4, capacity=4) is None

        approval = state.create_approval(
            session_id=sid, operation="write_file", details={"path": "data/x.txt"}, task_id=parked["task_id"],
        )
        third = _claim(state, "engine-b", free_slots=4, capacity=4)
        assert third is not None and third["task_id"] == jobs[2].task_id

        state.decide_approval(approval_id=approval.approval_id, decision="allow")
        assert _claim(state, "engine-b", free_slots=3, capacity=4) is None

    def test_an_approval_left_over_from_an_earlier_claim_does_not_park_the_task(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        for index in range(3):
            _background(state, sid, f"system.job{index}")
        first = _claim(state, "engine-a", free_slots=4, capacity=4)
        assert first is not None and _claim(state, "engine-a", free_slots=3, capacity=4) is not None
        task = state.tasks[first["task_id"]]
        approval = state.create_approval(
            session_id=sid, operation="write_file", details={"path": "data/x.txt"}, task_id=task.task_id,
        )
        # 重排后又被领走：上一次领取提的审批还挂着，可已经没有工具在等它
        task.claimed_at = approval.requested_at + timedelta(seconds=1)

        with state._lock:
            assert task.task_id not in state._tasks_awaiting_approval_locked()
        assert _claim(state, "engine-b", free_slots=4, capacity=4) is None


class TestLaneLimitsFromConfig:
    def test_the_global_background_limit_is_configurable(self, workspace: Path) -> None:
        _runtime(workspace, "engine:\n  lanes:\n    background_max_running: 1\n")
        state = _state(workspace)
        sid = _session(state)
        jobs = [_background(state, sid, f"system.job{index}") for index in range(2)]

        first = _claim(state, free_slots=4, capacity=4)
        assert first is not None and first["task_id"] == jobs[0].task_id
        assert _claim(state, free_slots=3, capacity=4) is None

    def test_zero_lifts_the_limit_and_the_reserve(self, workspace: Path) -> None:
        _runtime(workspace, "engine:\n  lanes:\n    background_max_running: 0\n    interactive_reserve: 0\n")
        state = _state(workspace)
        sid = _session(state)
        jobs = [_background(state, sid, f"system.job{index}") for index in range(3)]

        claimed = [_claim(state, free_slots=1, capacity=4) for _ in range(3)]

        assert [item["task_id"] for item in claimed if item] == [job.task_id for job in jobs]

    def test_a_larger_reserve_keeps_more_slots(self, workspace: Path) -> None:
        _runtime(workspace, "engine:\n  lanes:\n    interactive_reserve: 2\n")
        state = _state(workspace)
        job = _background(state, _session(state))

        assert _claim(state, free_slots=2, capacity=4) is None
        claimed = _claim(state, free_slots=3, capacity=4)
        assert claimed is not None and claimed["task_id"] == job.task_id


class TestRequeueKeepsTheLane:
    def test_a_background_child_requeued_after_an_engine_crash_stays_in_the_background(self, workspace: Path) -> None:
        state = _state(workspace)
        state.register_engine("engine-0", "gen-1")
        sid = _session(state)
        caller = _background(state, sid, "system.execution")
        claimed_caller = _claim(state, "engine-0")
        assert claimed_caller is not None and claimed_caller["task_id"] == caller.task_id
        _complete(state, claimed_caller, {
            "action": "dispatch", "target_node": WORKER_NODE,
            "dispatch_input": {"instruction": "改一下日志模块", "context_mode": "fresh"},
        }, "engine-0")
        [child] = _children(state, caller)
        assert child.lane is TaskLane.background
        claimed_child = _claim(state, "engine-0")
        assert claimed_child is not None and claimed_child["task_id"] == child.task_id
        ConversationStore(workspace / "data" / "conversations").append_batch(storage_session(child), [
            Message(id=f"{child.task_id}-{index}", role="assistant", content=f"step {index}", source_task_id=child.task_id)
            for index in range(2)
        ])

        result = state.register_engine("engine-0", "gen-2")

        assert result["orphans_requeued"] == 1
        assert child.status == TaskStatus.pending and child.input[REQUEUE_MARK]["reason"] == "engine_crash"
        assert child.lane is TaskLane.background
        assert _events(state, "task_requeued", child.task_id)[-1]["payload"]["lane"] == "background"

        user = _user_message(state, sid)
        first = _claim(state, "engine-0", free_slots=4, capacity=4)
        second = _claim(state, "engine-0", free_slots=3, capacity=4)
        assert first is not None and first["task_id"] == user.task_id
        assert second is not None and second["task_id"] == child.task_id and second["lane"] == "background"


class TestApi:
    def test_tasks_next_takes_the_slot_report(self, workspace: Path) -> None:
        state = _state(workspace)
        job = _background(state, _session(state))
        app = _app(state, workspace)

        kept = _call(app, "GET", "/v1/tasks/next", params={"worker_id": "engine-a", "free_slots": 1, "capacity": 4})
        assert kept.status_code == 204

        given = _call(app, "GET", "/v1/tasks/next", params={"worker_id": "engine-a", "free_slots": 2, "capacity": 4})
        assert given.status_code == 200
        assert given.json()["task_id"] == job.task_id and given.json()["lane"] == "background"

    @pytest.mark.parametrize("params", [
        {"worker_id": "engine-a", "free_slots": -1},
        {"worker_id": "engine-a", "capacity": 0},
        {"worker_id": "engine-a", "free_slots": "many"},
    ])
    def test_tasks_next_rejects_a_malformed_slot_report(self, workspace: Path, params: dict[str, Any]) -> None:
        state = _state(workspace)
        task = _user_message(state, _session(state))

        response = _call(_app(state, workspace), "GET", "/v1/tasks/next", params=params)

        assert response.status_code == 422
        assert task.status == TaskStatus.pending

    def test_old_engines_still_claim_without_the_slot_report(self, workspace: Path) -> None:
        state = _state(workspace)
        task = _user_message(state, _session(state))

        response = _call(_app(state, workspace), "GET", "/v1/tasks/next", params={"worker_id": "engine-a"})

        assert response.status_code == 200 and response.json()["task_id"] == task.task_id

    def test_active_tasks_report_the_lane_and_the_approval_wait(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        user = _user_message(state, sid)
        job = _background(state, sid)
        intent = _intent(state, sid)
        claimed = [_claim(state), _claim(state)]
        assert [item["task_id"] for item in claimed if item] == [intent.task_id, user.task_id]
        state.create_approval(session_id=sid, operation="execute_command", details={"command": "ls"}, task_id=user.task_id)

        response = _call(_app(state, workspace), "GET", "/v1/admin/tasks/active", headers=_AUTH)

        assert response.status_code == 200
        rows = {row["task_id"]: row for row in response.json()}
        assert rows[intent.task_id]["lane"] == "fast"
        assert rows[user.task_id]["lane"] == "interactive"
        assert rows[job.task_id]["lane"] == "background"
        assert rows[user.task_id]["awaiting_approval"] is True
        assert rows[intent.task_id]["awaiting_approval"] is False
        assert rows[job.task_id]["awaiting_approval"] is False

    def test_runtime_status_counts_the_queue(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        _user_message(state, sid)
        _user_message(state, sid, "第二条")
        _background(state, sid)
        assert _claim(state) is not None

        response = _call(_app(state, workspace), "GET", "/v1/admin/runtime/status", headers=_AUTH)

        assert response.status_code == 200
        assert response.json()["tasks"] == {"queued": 2, "running": 1}

    def test_an_intent_judgement_overtakes_queued_work_end_to_end(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        for index in range(3):
            _background(state, sid, f"system.job{index}")
        user = _user_message(state, sid)
        app = _app(state, workspace)

        async def scenario() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                judge = asyncio.create_task(client.post(
                    "/v1/intent/judge", headers=_AUTH,
                    json={"conversation_key": CONV, "text": "bot 在吗", "timeout_sec": 10},
                ))
                intent: Task | None = None
                for _ in range(500):
                    intent = next((task for task in state.tasks.values() if task.input.get("_reply_intent")), None)
                    if intent is not None:
                        break
                    await asyncio.sleep(0.01)
                assert intent is not None and intent.lane is TaskLane.fast
                claimed = state.assign_next_task(worker_id="engine-a", free_slots=1, capacity=4)
                assert claimed is not None and claimed["task_id"] == intent.task_id
                _complete(state, claimed, {"action": "finish", "result": {"text": "yes|有人在叫它"}})
                return await asyncio.wait_for(judge, 10)

        response = asyncio.run(scenario())

        assert response.status_code == 200 and response.json()["agreed"] is True
        claimed_user = state.assign_next_task(worker_id="engine-a", free_slots=1, capacity=4)
        assert claimed_user is not None and claimed_user["task_id"] == user.task_id
