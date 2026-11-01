"""同一段对话里并发的几轮：新消息那一轮看得到还在跑的任务，群聊里普通成员只能看、追加、取消自己发起的。"""
from __future__ import annotations

import asyncio
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.concurrent_tasks import ACTIVE_TASKS_INPUT_KEY, ACTIVE_TASKS_OMITTED_INPUT_KEY  # noqa: E402
from supervisor import admin_api, async_dispatches  # noqa: E402
from supervisor.active_tasks import (  # noqa: E402
    MAX_LISTED_TASKS,
    REQUEST_TEXT_KEY,
    REQUEST_TEXT_MAX_CHARS,
    request_preview,
)
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_owners import may_touch  # noqa: E402
from supervisor.types import Task, TaskStatus  # noqa: E402

_TOKEN = "concurrent-turns-token"
_GROUP = "qq_group:concurrent"
_PRIVATE = "qq_private:concurrent"
_ALICE, _BOB, _ADMIN = "10001", "10002", "10003"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="concurrent-turns"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _auth(user_id: str, *, conversation: str = _GROUP, admin: bool = False) -> dict[str, Any]:
    grouped = conversation.startswith("qq_group:")
    return {
        "platform": "qq", "user_id": user_id, "is_admin": admin,
        "group_role": "member" if grouped else "", "group_scope": conversation if grouped else "", "bot_scope": "bot",
    }


def _session(state: SupervisorState, conversation: str = _GROUP) -> str:
    return state.get_or_create_session(channel=conversation.split(":", 1)[0], conversation_key=conversation)


def _inbound(state: SupervisorState, session_id: str, text: str = "request", **extra: Any) -> Task:
    """和 /v1/inbound 走同一条建任务的路，建出来的任务当作已被 worker 领走。"""
    info = state.sessions[session_id]
    payload = {"channel": info.channel, "conversation_key": info.conversation_key, "text": text, **extra}
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    task = next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])
    task.status = TaskStatus.running
    task.worker_id = "worker"
    return task


def _round(state: SupervisorState, session_id: str, user_id: str, text: str = "request", **extra: Any) -> Task:
    conversation = state.sessions[session_id].conversation_key
    extra.setdefault("platform_auth", _auth(user_id, conversation=conversation, admin=user_id == _ADMIN))
    return _inbound(state, session_id, text, entry_node_id="qq.orchestrator", **extra)


def _async_child(state: SupervisorState, caller: Task, target: str = "bob") -> Task:
    """和异步委派发给 /v1/inbound 的一样：子节点跑在自己的 agent: 会话里，身份沿 dispatch_origin 认发起的那一轮。"""
    context = caller.input.get("task_context") or {}
    parent_key = str(context.get("conversation_key") or "")
    session_id = state.get_or_create_session(
        channel="qq_group", conversation_key=f"agent:{target}:{parent_key}:{uuid.uuid4()}",
    )
    return _inbound(
        state, session_id, "help", entry_node_id=target, dispatch_context_mode="fresh",
        dispatch_origin={
            "parent_session_id": str(caller.input.get("parent_session_id") or caller.session_id),
            "caller_node_id": caller.node_id,
            "parent_conversation_key": parent_key,
            "context_mode": "fresh",
            "caller_task_id": caller.task_id,
        },
    )


def _summary(task: Task) -> list[dict[str, Any]]:
    return list(task.input.get(ACTIVE_TASKS_INPUT_KEY) or [])


def _listed(task: Task) -> list[str]:
    return [item["task_id"] for item in _summary(task)]


class TestWhoMayTouchWhat:
    @pytest.mark.parametrize("caller, target, allowed", [
        ({}, _auth(_BOB), True),
        ({"platform": "", "user_id": _ALICE}, _auth(_BOB), True),
        (_auth(_ALICE, conversation=_PRIVATE), {}, True),
        (_auth(_ADMIN, admin=True), _auth(_BOB), True),
        (_auth(_ALICE), _auth(_ALICE), True),
        (_auth(_ALICE), _auth(_BOB), False),
        (_auth(_ALICE), {}, False),
        ({**_auth(_ALICE), "user_id": ""}, {**_auth(_BOB), "user_id": ""}, False),
    ], ids=[
        "console", "no-platform", "private-chat", "group-admin", "same-member",
        "other-member", "task-without-requester", "anonymous-members",
    ])
    def test_only_group_members_who_are_not_admins_are_limited_to_their_own(
        self, caller: dict[str, Any], target: dict[str, Any], allowed: bool,
    ) -> None:
        assert may_touch(caller, target) is allowed


class TestTheNewMessageSeesWhatIsRunning:
    def test_a_running_round_is_listed_with_what_it_is_doing(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        first = _round(state, session_id, _ALICE, "整段 instruction", request_text="帮我  查一下\n明天的天气")
        first.current_phase, first.current_detail = "tool_call", "web_search"

        second = _round(state, session_id, _ALICE, "进度呢")

        [item] = _summary(second)
        assert item["task_id"] == first.task_id
        assert item["node_id"] == "qq.orchestrator"
        assert item["status"] == "running" and item["steerable"] is True
        assert (item["phase"], item["detail"]) == ("tool_call", "web_search")
        assert item["instruction"] == "帮我 查一下 明天的天气"
        assert isinstance(item["age_sec"], int) and item["age_sec"] >= 0
        # 私聊只有一个人，不分谁发起的
        assert "mine" not in item
        assert ACTIVE_TASKS_OMITTED_INPUT_KEY not in second.input

    def test_nothing_running_leaves_the_input_without_a_summary(self, state: SupervisorState) -> None:
        task = _round(state, _session(state), _ALICE)

        assert ACTIVE_TASKS_INPUT_KEY not in task.input

    def test_a_suspended_round_shows_no_stale_activity_and_cannot_be_steered(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        first = _round(state, session_id, _ALICE)
        with state._lock:
            state._route_dispatch_locked(first, {
                "action": "dispatch", "target_node": "worker.node",
                "dispatch_input": {"instruction": "help", "context_mode": "fresh"},
            })
        child = state.tasks[str(first.waiting_for_task_id)]
        child.status = TaskStatus.running
        first.current_phase, first.current_detail = "tool_call", "dispatch_to_worker"

        later = _round(state, session_id, _ALICE)

        items = {item["task_id"]: item for item in _summary(later)}
        assert items[first.task_id]["status"] == "suspended"
        assert items[first.task_id]["steerable"] is False
        assert "phase" not in items[first.task_id]
        assert items[child.task_id]["parent_task_id"] == first.task_id
        assert items[child.task_id]["steerable"] is True

    def test_async_dispatches_are_listed_under_the_round_that_sent_them(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        first = _round(state, session_id, _ALICE)
        child = _async_child(state, first)

        later = _round(state, session_id, _ALICE)

        assert _listed(later) == [first.task_id, child.task_id]
        assert _summary(later)[1]["parent_task_id"] == first.task_id

    def test_internal_flows_and_interjections_are_left_out(self, state: SupervisorState) -> None:
        session_id = _session(state)
        _inbound(state, session_id, "compact", _system_task=True)
        _round(state, session_id, _ALICE, route_hints={"delivery_purpose": "ambient"})

        later = _round(state, session_id, _ADMIN)

        assert _listed(later) == []

    @pytest.mark.parametrize("extra", [
        {"route_hints": {"delivery_purpose": "ambient"}},
        {"schedule_id": "daily"},
        {"_system_task": True},
    ], ids=["interjection", "scheduled", "system"])
    def test_rounds_nobody_typed_get_no_summary(self, state: SupervisorState, extra: dict[str, Any]) -> None:
        session_id = _session(state)
        _round(state, session_id, _ALICE)

        task = _round(state, session_id, _ALICE, **extra)

        assert ACTIVE_TASKS_INPUT_KEY not in task.input

    def test_a_dispatched_child_gets_no_summary(self, state: SupervisorState) -> None:
        session_id = _session(state)
        first = _round(state, session_id, _ALICE)
        _round(state, session_id, _ALICE)

        child = _async_child(state, first)

        assert ACTIVE_TASKS_INPUT_KEY not in child.input

    def test_the_list_is_capped_and_says_how_many_were_left_out(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        rounds = [_round(state, session_id, _ALICE, f"第 {index} 件") for index in range(MAX_LISTED_TASKS + 2)]

        later = _round(state, session_id, _ALICE)

        assert _listed(later) == [task.task_id for task in rounds[:MAX_LISTED_TASKS]]
        assert later.input[ACTIVE_TASKS_OMITTED_INPUT_KEY] == 2

    def test_rounds_are_kept_before_children_when_the_list_is_full(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        first = _round(state, session_id, _ALICE)
        children = [_async_child(state, first) for _ in range(MAX_LISTED_TASKS)]

        later = _round(state, session_id, _ALICE)

        assert _listed(later) == [first.task_id, *[child.task_id for child in children[:MAX_LISTED_TASKS - 1]]]
        assert later.input[ACTIVE_TASKS_OMITTED_INPUT_KEY] == 1


class TestRequestText:
    def test_the_users_own_words_are_kept_capped_and_previewed(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        words = "查" * (REQUEST_TEXT_MAX_CHARS + 50)
        first = _round(state, session_id, _ALICE, "【群聊上下文记录】……", request_text=f"  {words}  ")

        later = _round(state, session_id, _ALICE)

        assert first.input[REQUEST_TEXT_KEY] == words[:REQUEST_TEXT_MAX_CHARS]
        [item] = _summary(later)
        assert item["instruction"].endswith("…") and len(item["instruction"]) == 120

    def test_rounds_started_by_a_result_or_a_continuation_say_so(self) -> None:
        assert request_preview({"inbound_message_type": "dispatch_result", "instruction": "三页\n 文档"}) == "交回的委派结果：三页 文档"
        assert request_preview({"inbound_message_type": "auto_continue", "instruction": ""}) == "自动续跑"
        assert request_preview({"inbound_message_type": "", "instruction": "你好"}) == "你好"

    def test_without_it_the_instruction_is_the_preview(self, state: SupervisorState) -> None:
        session_id = _session(state, _PRIVATE)
        _round(state, session_id, _ALICE, "直接说的话")

        later = _round(state, session_id, _ALICE)

        assert _summary(later)[0]["instruction"] == "直接说的话"

    def test_the_console_preview_prefers_it(self, state: SupervisorState, tmp_path: Path) -> None:
        session_id = _session(state, _PRIVATE)
        task = _round(state, session_id, _ALICE, "当前时间: 2026-11-01 00:07 CST\n……", request_text="小明（UserA）: 你好")
        app = create_app(
            state=state, process_manager=None,
            config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
        )

        async def run() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/v1/admin/tasks/active", headers={"Authorization": f"Bearer {_TOKEN}"})

        response = asyncio.run(run())

        assert response.status_code == 200
        [item] = [item for item in response.json() if item["task_id"] == task.task_id]
        assert item["input_summary"] == "小明（UserA）: 你好"


class TestGroupIsolation:
    def test_a_member_only_sees_their_own_rounds(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        _round(state, session_id, _BOB)

        later = _round(state, session_id, _ALICE)

        assert _listed(later) == [alice.task_id]
        assert _summary(later)[0]["mine"] is True

    def test_an_admin_sees_everyone_and_whose_is_whose(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        own = _round(state, session_id, _ADMIN)

        later = _round(state, session_id, _ADMIN)

        assert {item["task_id"]: item["mine"] for item in _summary(later)} == {alice.task_id: False, own.task_id: True}

    def test_a_member_cannot_steer_someone_elses_round(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        bob = _round(state, session_id, _BOB)

        assert state.resolve_family_task(bob.task_id, alice.task_id) == {"found": False, "reason": "not_found"}
        assert state.preempt_task(alice.task_id, "改成英文", caller_task_id=bob.task_id) is False
        assert not alice.preempt_requested

    def test_the_same_member_and_an_admin_can_steer_it(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        again = _round(state, session_id, _ALICE)
        admin = _round(state, session_id, _ADMIN)

        assert state.resolve_family_task(again.task_id, alice.task_id)["task_id"] == alice.task_id
        assert state.preempt_task(alice.task_id, "改成英文", caller_task_id=again.task_id) is True
        assert state.preempt_task(alice.task_id, "再短一点", caller_task_id=admin.task_id) is True
        assert alice.preempt_message == "再短一点"

    def test_a_child_of_someone_elses_round_is_theirs_too(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        child = _async_child(state, alice)
        bob = _round(state, session_id, _BOB)

        assert state.preempt_task(child.task_id, "停一下", caller_task_id=bob.task_id) is False
        assert state.resolve_family_task(bob.task_id, "bob") == {"found": False, "reason": "not_found"}

    def test_cancelling_from_a_members_round_spares_other_members_rounds(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        bob = _round(state, session_id, _BOB)
        caller = _round(state, session_id, _ALICE)

        result = state.cancel_active_tasks(session_id, exclude_task_id=caller.task_id)

        assert result == {"ok": True, "cancelled_count": 1}
        assert alice.status == TaskStatus.cancelled
        assert bob.status == TaskStatus.running and caller.status == TaskStatus.running

    def test_an_admin_and_a_private_chat_cancel_everything_else(self, state: SupervisorState) -> None:
        group = _session(state)
        alice = _round(state, group, _ALICE)
        admin = _round(state, group, _ADMIN)
        private = _session(state, _PRIVATE)
        earlier = _round(state, private, _ALICE)
        scheduled = _inbound(state, private, "定时提醒", schedule_id="daily", entry_node_id="qq.orchestrator")
        caller = _round(state, private, _ALICE)

        assert state.cancel_active_tasks(group, exclude_task_id=admin.task_id)["cancelled_count"] == 1
        assert state.cancel_active_tasks(private, exclude_task_id=caller.task_id)["cancelled_count"] == 2
        assert alice.status == earlier.status == scheduled.status == TaskStatus.cancelled

    def test_a_caller_left_unknown_cancels_like_before(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        bob = _round(state, session_id, _BOB)

        assert state.cancel_active_tasks(session_id)["cancelled_count"] == 2
        assert alice.status == bob.status == TaskStatus.cancelled


class TestGroupIsolationOfAsyncDispatches:
    def test_someone_elses_dispatch_is_not_listed_awaited_or_cancelled(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        child = _async_child(state, alice)
        bob = _round(state, session_id, _BOB)
        with state._lock:
            handle = async_dispatches.handle_of_locked(state, child)
            awaited, reason = async_dispatches._task_for_handle_locked(
                state, async_dispatches.registry_locked(state), bob, handle, purpose=async_dispatches.PURPOSE_AWAIT,
            )

        listing = async_dispatches.list_dispatches(state, bob.task_id)
        refused = async_dispatches.cancel_dispatch(state, bob.task_id, handle)

        assert listing is not None and listing["active"] == [] and listing["active_total"] == 0
        assert awaited is None and reason == async_dispatches._NOT_FOUND
        assert refused["ok"] is False and refused["reason"] == "not_found"
        assert child.status == TaskStatus.running

    def test_the_same_member_still_lists_and_cancels_it(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        child = _async_child(state, alice)
        again = _round(state, session_id, _ALICE)
        with state._lock:
            handle = async_dispatches.handle_of_locked(state, child)
            listing = async_dispatches.list_locked(state, again.task_id)
            result = async_dispatches.cancel_locked(state, again.task_id, handle)

        assert [item["handle"] for item in listing["active"]] == [handle]
        assert result["ok"] is True

    def test_a_finished_dispatch_stays_with_whoever_started_it(self, state: SupervisorState) -> None:
        session_id = _session(state)
        alice = _round(state, session_id, _ALICE)
        child = _async_child(state, alice)
        state.complete_task(task_id=child.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "done"}})
        bob = _round(state, session_id, _BOB)
        again = _round(state, session_id, _ALICE)
        with state._lock:
            record = async_dispatches.registry_locked(state).ledger[child.task_id]
            others = async_dispatches.list_locked(state, bob.task_id)
            own = async_dispatches.list_locked(state, again.task_id)

        assert record.owner["user_id"] == _ALICE
        assert others["finished"] == [] and others["finished_total"] == 0
        assert own["finished_total"] == 1
