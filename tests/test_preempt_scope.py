"""preempt_task 只在调用方的会话家族里认目标：节点名、完整 id、id 前缀三条路和打断端点都由 supervisor 限定。

家族 = 从调用方任务沿发起链走到根会话，根会话、它的入口分支，以及这一家派出去的子任务（agent: 会话、
wait 委派的子任务）和它们的后代。别的会话里的任务一律当作不存在；家族里的工具任务、系统任务和挂起等子任务的任务也不能当目标。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference import pseudo_handlers  # noqa: E402
from engine.inference.pseudo_tools import _preempt_task_spec  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_TOKEN = "preempt-scope-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_CONV_A = "qq_group:family-a"
_CONV_B = "qq_group:family-b"
_MISSING = {"found": False, "reason": "not_found"}


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="preempt-scope"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _inbound(state: SupervisorState, session_id: str, **extra: Any) -> Task:
    """和 /v1/inbound 走同一条建任务的路，建出来的任务当作已被 worker 领走。"""
    payload = {
        "channel": "qq_group",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "request",
        **extra,
    }
    event = state.eventlog.append(
        session_id=session_id, component="shell", type_="inbound_message", payload=payload,
    )
    state.record_inbound_message_event(event)
    task = next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])
    task.status = TaskStatus.running
    task.worker_id = "worker"
    return task


def _conversation(state: SupervisorState, key: str = _CONV_A) -> tuple[str, Task]:
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=key)
    return session_id, _inbound(state, session_id, entry_node_id="qq.orchestrator")


def _delegate(state: SupervisorState, caller: Task, target: str, *, mode: str = "accumulate") -> Task:
    """和 _handle_pseudo_dispatch 发给 /v1/inbound 的一样：子节点跑在自己的 agent: 会话里。"""
    context = caller.input.get("task_context") or {}
    parent_key = str(context.get("route_conversation_key") or context.get("conversation_key") or "")
    parent_session = str(caller.input.get("parent_session_id") or caller.session_id)
    key = f"agent:{target}:{parent_key}" if mode == "accumulate" else f"agent:{target}:{parent_key}:{uuid.uuid4()}"
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=key)
    return _inbound(
        state, session_id, entry_node_id=target, dispatch_context_mode=mode,
        dispatch_origin={
            "parent_session_id": parent_session,
            "caller_node_id": caller.node_id,
            "parent_conversation_key": parent_key,
            "context_mode": mode,
            "caller_task_id": caller.task_id,
        },
    )


def _delegate_and_wait(state: SupervisorState, caller: Task, target: str) -> Task:
    """wait 委派：调用方挂起，子任务和它跑在同一个运行期会话里。"""
    with state._lock:
        state._route_dispatch_locked(caller, {
            "action": "dispatch", "target_node": target,
            "dispatch_input": {"instruction": "help", "context_mode": "fresh"},
        })
    child = state.tasks[str(caller.waiting_for_task_id)]
    child.status = TaskStatus.running
    return child


def _finish(state: SupervisorState, task: Task) -> None:
    state.complete_task(
        task_id=task.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "done"}},
    )


def _rename(state: SupervisorState, task: Task, task_id: str) -> Task:
    """换成指定的 id，好造出共用前缀的任务。只用在没有子任务的叶子任务上。"""
    with state._lock:
        state.tasks.pop(task.task_id)
        state._task_order = [task_id if tid == task.task_id else tid for tid in state._task_order]
        task.task_id = task_id
        state.tasks[task_id] = task
    return task


def _node_file(workspace: Path, name: str, **fields: Any) -> None:
    folder = workspace / "config" / "nodes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(
        yaml.safe_dump({"type": "ai", **fields}, allow_unicode=True), encoding="utf-8",
    )


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _call(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def _untouched(task: Task) -> bool:
    return not task.preempt_requested and task.preempt_revision == 0 and task.preempt_message == ""


class TestByNodeName:
    def test_a_child_the_caller_dispatched_is_found(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        found = state.resolve_family_task(entry.task_id, "bob")

        assert found == {
            "found": True, "task_id": bob.task_id, "node_id": "bob", "status": "running", "matched_by": "node",
        }

    def test_the_only_match_in_another_conversation_is_never_picked(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")

        assert state.resolve_family_task(entry.task_id, "bob") == _MISSING
        assert state.resolve_family_task(other_entry.task_id, "bob")["task_id"] == stranger.task_id

    def test_the_own_child_is_picked_even_when_another_conversation_has_one_too(
        self, state: SupervisorState,
    ) -> None:
        _other_sid, other_entry = _conversation(state, _CONV_B)
        _delegate(state, other_entry, "bob")
        _sid, entry = _conversation(state)
        own = _delegate(state, entry, "bob")

        assert state.resolve_family_task(entry.task_id, "bob")["task_id"] == own.task_id

    @pytest.mark.parametrize("caller_name, target_name", [
        ("entry", "carol"),
        ("entry", "dave"),
        ("sibling", "carol"),
        ("sibling", "dave"),
        ("carol", "dave"),
        ("dave", "carol"),
        ("carol", "sibling"),
    ])
    def test_nested_and_waiting_dispatches_share_one_family(
        self, state: SupervisorState, caller_name: str, target_name: str,
    ) -> None:
        session_id, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        carol = _delegate(state, bob, "carol")
        dave = _delegate_and_wait(state, bob, "dave")
        sibling = _inbound(state, session_id, entry_node_id="sibling")
        tasks = {"entry": entry, "carol": carol, "dave": dave, "sibling": sibling}

        found = state.resolve_family_task(tasks[caller_name].task_id, target_name)

        assert found["task_id"] == tasks[target_name].task_id

    def test_a_task_waiting_for_its_children_is_not_a_target(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        _delegate_and_wait(state, bob, "dave")

        assert bob.status == TaskStatus.suspended
        assert state.resolve_family_task(entry.task_id, "bob") == _MISSING

    def test_the_persistent_agent_session_is_reached_from_a_later_turn(self, state: SupervisorState) -> None:
        # 持久节点的 agent: 会话由多轮共用：派它的那一轮已经结束，后一轮照样找得到它
        session_id, first = _conversation(state)
        bob = _delegate(state, first, "bob", mode="accumulate")
        _finish(state, first)
        later = _inbound(state, session_id, entry_node_id="qq.orchestrator")

        assert state.sessions[bob.input["parent_session_id"]].conversation_key == f"agent:bob:{_CONV_A}"
        assert state.resolve_family_task(later.task_id, "bob")["task_id"] == bob.task_id

    def test_a_dispatch_origin_without_the_caller_still_names_the_parent_session(
        self, state: SupervisorState,
    ) -> None:
        session_id, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        bob.input["_dispatch_origin"].pop("caller_task_id")
        later = _inbound(state, session_id, entry_node_id="qq.orchestrator")

        assert state.resolve_family_task(later.task_id, "bob")["task_id"] == bob.task_id

    def test_the_caller_is_not_its_own_node_match(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        sibling = _inbound(state, session_id, entry_node_id="qq.orchestrator")

        assert state.resolve_family_task(entry.task_id, "qq.orchestrator")["task_id"] == sibling.task_id

    def test_of_two_matches_the_one_the_caller_dispatched_wins(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        mine = _delegate(state, entry, "bob", mode="fresh")
        sibling = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        theirs = _delegate(state, sibling, "bob", mode="fresh")

        assert state.resolve_family_task(entry.task_id, "bob")["task_id"] == mine.task_id
        assert state.resolve_family_task(sibling.task_id, "bob")["task_id"] == theirs.task_id

    def test_matches_the_caller_cannot_tell_apart_come_back_as_candidates(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        first = _delegate(state, entry, "bob", mode="fresh")
        second = _delegate(state, entry, "bob", mode="fresh")

        found = state.resolve_family_task(entry.task_id, "bob")

        assert (found["found"], found["reason"], found["total"]) == (False, "ambiguous", 2)
        shown = [candidate["task_id"] for candidate in found["candidates"]]
        assert all(len(prefix) >= 8 for prefix in shown)
        assert first.task_id.startswith(shown[0]) and second.task_id.startswith(shown[1])
        assert {(c["node_id"], c["status"]) for c in found["candidates"]} == {("bob", "running")}

    def test_a_long_candidate_list_is_capped_but_counted(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        for _ in range(11):
            _delegate(state, entry, "bob", mode="fresh")

        found = state.resolve_family_task(entry.task_id, "bob")

        assert found["total"] == 11
        assert len(found["candidates"]) == 10

    def test_an_old_node_id_reaches_the_renamed_node(self, state: SupervisorState, tmp_path: Path) -> None:
        _node_file(tmp_path, "bob", aliases=["robert"])
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        assert state.resolve_family_task(entry.task_id, "robert")["task_id"] == bob.task_id

    def test_a_tool_task_is_not_a_target(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        with state._lock:
            state._route_dispatch_locked(bob, {
                "action": "dispatch",
                "dispatch_batch": [{"kind": "tool", "target": "read_file", "arguments": {}}],
            })
        [tool_task] = [task for task in state.tasks.values() if task.kind == TaskKind.tool]
        tool_task.status = TaskStatus.running

        assert state.resolve_family_task(entry.task_id, tool_task.task_id) == _MISSING

    def test_a_system_task_of_the_same_conversation_is_not_a_target(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        extractor = _inbound(state, session_id, entry_node_id="system.memory_extractor", _system_task=True)

        assert extractor.input["_system_task"] is True
        assert state.resolve_family_task(entry.task_id, "system.memory_extractor") == _MISSING
        assert state.resolve_family_task(entry.task_id, extractor.task_id) == _MISSING

    def test_a_system_flag_in_the_task_context_alone_is_enough(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        with state._lock:
            summarizer = state._create_task_locked(
                session_id=session_id, session_generation=1, kind=TaskKind.node, node_id="summarizer",
                input_data={"instruction": "sum up", "task_context": {"is_system_task": True}},
                continuation={}, source_inbound_seq=None,
            )
            helper = state._create_task_locked(
                session_id=session_id, session_generation=1, kind=TaskKind.node, node_id="helper",
                input_data={"instruction": "help", "task_context": {"is_system_task": False}},
                continuation={}, source_inbound_seq=None,
            )
        summarizer.status = helper.status = TaskStatus.running

        assert state.resolve_family_task(entry.task_id, "summarizer") == _MISSING
        assert state.resolve_family_task(entry.task_id, "helper")["task_id"] == helper.task_id

    def test_a_pending_task_can_be_steered_before_it_starts(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        bob.status = TaskStatus.pending

        assert state.resolve_family_task(entry.task_id, "bob")["status"] == "pending"


class TestSessionAncestry:
    @staticmethod
    def _child_session_task(state: SupervisorState, entry: Task) -> Task:
        """直接建在入口分支的子会话上、没有发起链的任务：家族只能靠会话登记往上找。"""
        with state._lock:
            child_sid, _is_new = state.get_or_create_child_session(entry.session_id, "helper", "", "fresh")
            helper = state._create_task_locked(
                session_id=child_sid, session_generation=1, kind=TaskKind.node, node_id="helper",
                input_data={"instruction": "help"}, continuation={}, source_inbound_seq=None,
            )
        helper.status = TaskStatus.running
        return helper

    def test_a_child_session_under_an_entry_branch_belongs_to_the_conversation(
        self, state: SupervisorState,
    ) -> None:
        session_id, entry = _conversation(state)
        helper = self._child_session_task(state, entry)

        with state._lock:
            assert state._session_family_root_locked(helper.session_id) == session_id
        assert state.resolve_family_task(entry.task_id, "helper")["task_id"] == helper.task_id

    def test_the_session_registry_alone_still_links_the_family(self, state: SupervisorState) -> None:
        # 重启后内存里的分支和子会话索引要重建，sessions.json 的登记行得能单独认出上级
        session_id, entry = _conversation(state)
        helper = self._child_session_task(state, entry)
        with state._lock:
            state.entry_branch_parents.clear()
            state.parent_entry_branches.clear()
            state.child_session_map.clear()
            state.parent_children.clear()
            assert state._session_family_root_locked(helper.session_id) == session_id

        assert state.resolve_family_task(entry.task_id, "helper")["task_id"] == helper.task_id

    def test_another_conversations_child_session_stays_outside(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = self._child_session_task(state, other_entry)

        assert state.resolve_family_task(entry.task_id, "helper") == _MISSING
        assert state.resolve_family_task(other_entry.task_id, "helper")["task_id"] == stranger.task_id

    def test_an_origin_naming_the_callers_entry_branch_reaches_the_conversation(
        self, state: SupervisorState,
    ) -> None:
        # 发起任务没带父会话时，engine 在 dispatch_origin 里报的是它的运行分支
        session_id, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        bob.input["_dispatch_origin"].pop("caller_task_id")
        bob.input["_dispatch_origin"]["parent_session_id"] = entry.session_id
        later = _inbound(state, session_id, entry_node_id="qq.orchestrator")

        assert entry.session_id != session_id
        assert state.resolve_family_task(later.task_id, "bob")["task_id"] == bob.task_id

    def test_an_origin_naming_an_unknown_session_joins_no_family(self, state: SupervisorState) -> None:
        session_id, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        bob.input["_dispatch_origin"].pop("caller_task_id")
        bob.input["_dispatch_origin"]["parent_session_id"] = "session-that-never-existed"
        later = _inbound(state, session_id, entry_node_id="qq.orchestrator")

        assert state.resolve_family_task(later.task_id, "bob") == _MISSING


class TestByFullId:
    def test_a_task_in_the_family_is_found(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        found = state.resolve_family_task(entry.task_id, bob.task_id)

        assert (found["task_id"], found["matched_by"]) == (bob.task_id, "id")

    def test_another_conversations_task_reads_exactly_like_a_missing_one(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")

        assert state.resolve_family_task(entry.task_id, stranger.task_id) == _MISSING
        assert state.resolve_family_task(entry.task_id, str(uuid.uuid4())) == _MISSING

    def test_a_finished_task_is_not_a_target(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        _finish(state, bob)

        assert state.resolve_family_task(entry.task_id, bob.task_id) == _MISSING

    def test_a_finished_or_unknown_caller_reaches_nothing(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        _finish(state, entry)

        assert state.resolve_family_task(entry.task_id, bob.task_id) == {"found": False, "reason": "caller_not_found"}
        assert state.resolve_family_task("no-such-task", "bob") == {"found": False, "reason": "caller_not_found"}

    def test_the_caller_is_never_its_own_target(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _rename(state, entry, "abcdef12-1000-4000-8000-000000000001")

        assert state.resolve_family_task(entry.task_id, entry.task_id) == _MISSING
        assert state.resolve_family_task(entry.task_id, "abcdef12") == _MISSING


class TestByPrefix:
    def test_a_unique_prefix_is_found_regardless_of_case(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        bob = _rename(state, _delegate(state, entry, "bob"), "abcdef12-1000-4000-8000-000000000001")

        found = state.resolve_family_task(entry.task_id, "ABCDEF12")

        assert (found["task_id"], found["matched_by"]) == (bob.task_id, "prefix")

    def test_a_shared_prefix_lists_candidates_that_tell_them_apart(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _rename(state, _delegate(state, entry, "bob", mode="fresh"), "abcdef12-1000-4000-8000-000000000001")
        carol = _rename(state, _delegate(state, entry, "carol"), "abcdef12-2000-4000-8000-000000000002")

        found = state.resolve_family_task(entry.task_id, "abcdef12")

        assert (found["reason"], found["total"]) == ("ambiguous", 2)
        assert [c["task_id"] for c in found["candidates"]] == ["abcdef12-1", "abcdef12-2"]
        assert [c["node_id"] for c in found["candidates"]] == ["bob", "carol"]
        assert state.resolve_family_task(entry.task_id, "abcdef12-2")["task_id"] == carol.task_id

    def test_matches_outside_the_family_do_not_count(self, state: SupervisorState) -> None:
        _other_sid, other_entry = _conversation(state, _CONV_B)
        _rename(state, _delegate(state, other_entry, "bob"), "abcdef12-2000-4000-8000-000000000002")
        _sid, entry = _conversation(state)
        own = _rename(state, _delegate(state, entry, "bob"), "abcdef12-1000-4000-8000-000000000001")

        found = state.resolve_family_task(entry.task_id, "abcdef12")

        assert (found["task_id"], found["matched_by"]) == (own.task_id, "prefix")

    def test_a_prefix_matching_only_another_conversation_reads_as_missing(self, state: SupervisorState) -> None:
        _other_sid, other_entry = _conversation(state, _CONV_B)
        _rename(state, _delegate(state, other_entry, "bob"), "abcdef12-2000-4000-8000-000000000002")
        _sid, entry = _conversation(state)

        assert state.resolve_family_task(entry.task_id, "abcdef12") == _MISSING

    def test_a_hex_looking_name_without_a_task_prefix_is_read_as_a_node(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        node = _delegate(state, entry, "deadbeef")

        found = state.resolve_family_task(entry.task_id, "deadbeef")

        assert (found["task_id"], found["matched_by"]) == (node.task_id, "node")

    def test_fewer_than_eight_characters_are_not_a_prefix(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        _rename(state, _delegate(state, entry, "bob"), "abcdef12-1000-4000-8000-000000000001")

        assert state.resolve_family_task(entry.task_id, "abcdef1") == _MISSING


class TestResolveEndpoint:
    def test_a_match_comes_back_with_how_it_was_found(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        response = _call(
            _app(state, tmp_path), "GET", f"/v1/tasks/{entry.task_id}/family/resolve",
            params={"ref": "bob"}, headers=_AUTH,
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"task_id": bob.task_id, "node_id": "bob", "status": "running", "matched_by": "node"}

    def test_another_conversation_answers_like_a_missing_task(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")
        app = _app(state, tmp_path)
        url = f"/v1/tasks/{entry.task_id}/family/resolve"

        by_name = _call(app, "GET", url, params={"ref": "bob"}, headers=_AUTH)
        by_id = _call(app, "GET", url, params={"ref": stranger.task_id}, headers=_AUTH)
        missing = _call(app, "GET", url, params={"ref": str(uuid.uuid4())}, headers=_AUTH)

        assert by_name.status_code == by_id.status_code == missing.status_code == 404
        assert by_name.json() == by_id.json() == missing.json()

    def test_an_ambiguous_name_is_a_conflict_with_candidates(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        _delegate(state, entry, "bob", mode="fresh")
        _delegate(state, entry, "bob", mode="fresh")

        response = _call(
            _app(state, tmp_path), "GET", f"/v1/tasks/{entry.task_id}/family/resolve",
            params={"ref": "bob"}, headers=_AUTH,
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert (detail["error"], detail["total"], len(detail["candidates"])) == ("ambiguous", 2, 2)

    def test_it_needs_the_admin_token_and_a_reference(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        app = _app(state, tmp_path)
        url = f"/v1/tasks/{entry.task_id}/family/resolve"

        assert _call(app, "GET", url, params={"ref": "bob"}).status_code == 401
        assert _call(app, "GET", url, params={"ref": ""}, headers=_AUTH).status_code == 422
        assert _call(app, "GET", url, headers=_AUTH).status_code == 422

    def test_the_unscoped_node_lookups_are_gone(self, state: SupervisorState, tmp_path: Path) -> None:
        session_id, entry = _conversation(state)
        _delegate(state, entry, "bob")
        app = _app(state, tmp_path)

        assert _call(app, "GET", "/v1/tasks/active-by-node/bob", headers=_AUTH).status_code in (404, 405)
        assert _call(app, "GET", f"/v1/sessions/{session_id}/tasks/by-node/bob", headers=_AUTH).status_code in (404, 405)


class TestPreemptEndpoint:
    def test_a_target_in_the_family_gets_the_message(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        response = _call(
            _app(state, tmp_path), "POST", f"/v1/tasks/{bob.task_id}/preempt",
            json={"message": "focus", "caller_task_id": entry.task_id},
        )

        assert response.status_code == 200, response.text
        assert bob.preempt_requested and bob.preempt_message == "focus"

    def test_a_target_outside_the_family_is_refused_like_a_missing_one(
        self, state: SupervisorState, tmp_path: Path,
    ) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")
        app = _app(state, tmp_path)

        refused = _call(
            app, "POST", f"/v1/tasks/{stranger.task_id}/preempt",
            json={"message": "hijack", "caller_task_id": entry.task_id},
        )
        missing = _call(
            app, "POST", f"/v1/tasks/{uuid.uuid4()}/preempt",
            json={"message": "hijack", "caller_task_id": entry.task_id},
        )

        assert refused.status_code == missing.status_code == 404
        assert refused.json() == missing.json()
        assert _untouched(stranger)

    def test_a_finished_or_unknown_caller_cannot_preempt(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        _finish(state, entry)
        app = _app(state, tmp_path)

        for caller in (entry.task_id, "no-such-task"):
            response = _call(
                app, "POST", f"/v1/tasks/{bob.task_id}/preempt", json={"message": "x", "caller_task_id": caller},
            )
            assert response.status_code == 404
        assert _untouched(bob)

    @pytest.mark.parametrize("caller", ["", "   ", 7, ["task"]])
    def test_a_malformed_caller_is_rejected(self, state: SupervisorState, tmp_path: Path, caller: Any) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        response = _call(
            _app(state, tmp_path), "POST", f"/v1/tasks/{bob.task_id}/preempt",
            json={"message": "x", "caller_task_id": caller},
        )

        assert response.status_code == 422
        assert _untouched(bob)

    def test_the_adapter_without_a_caller_keeps_preempting_by_id(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)

        response = _call(_app(state, tmp_path), "POST", f"/v1/tasks/{entry.task_id}/preempt", json={"message": "more"})

        assert response.status_code == 200
        assert entry.preempt_message == "more"

    def test_the_state_method_applies_the_same_check(self, state: SupervisorState) -> None:
        _sid, entry = _conversation(state)
        own = _delegate(state, entry, "bob")
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")

        assert state.preempt_task(stranger.task_id, "hijack", caller_task_id=entry.task_id) is False
        assert _untouched(stranger)
        assert state.preempt_task(own.task_id, "steer", caller_task_id=entry.task_id) is True
        assert own.preempt_message == "steer"

    def test_system_and_tool_tasks_of_the_family_are_refused_like_missing_ones(
        self, state: SupervisorState, tmp_path: Path,
    ) -> None:
        session_id, entry = _conversation(state)
        extractor = _inbound(state, session_id, entry_node_id="system.memory_extractor", _system_task=True)
        bob = _delegate(state, entry, "bob")
        with state._lock:
            state._route_dispatch_locked(bob, {
                "action": "dispatch",
                "dispatch_batch": [{"kind": "tool", "target": "read_file", "arguments": {}}],
            })
        [tool_task] = [task for task in state.tasks.values() if task.kind == TaskKind.tool]
        tool_task.status = TaskStatus.running
        app = _app(state, tmp_path)

        for target in (extractor, tool_task):
            response = _call(
                app, "POST", f"/v1/tasks/{target.task_id}/preempt",
                json={"message": "x", "caller_task_id": entry.task_id},
            )
            assert response.status_code == 404
            assert _untouched(target)

    def test_a_task_cannot_preempt_itself_through_the_endpoint(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)

        response = _call(
            _app(state, tmp_path), "POST", f"/v1/tasks/{entry.task_id}/preempt",
            json={"message": "talk to yourself", "caller_task_id": entry.task_id},
        )

        assert response.status_code == 404
        assert _untouched(entry)
        assert state.preempt_task(entry.task_id, "again", caller_task_id=entry.task_id) is False

    def test_the_adapter_path_keeps_its_old_reach(self, state: SupervisorState) -> None:
        # 适配器按自己会话的 running_tasks 选目标，不带调用方，家族和目标种类的限制都不套在它身上
        session_id, _entry = _conversation(state)
        extractor = _inbound(state, session_id, entry_node_id="system.memory_extractor", _system_task=True)

        assert state.preempt_task(extractor.task_id, "more") is True
        assert extractor.preempt_message == "more"


def _loop(http: httpx.AsyncClient, caller_task_id: str) -> SimpleNamespace:
    return SimpleNamespace(rctx=SimpleNamespace(task_id=caller_task_id, supervisor_url="http://test", http=http))


def _engine_preempt(app: Any, caller: Task, ref: str, message: str = "steer") -> str:
    """engine 的 preempt_task 经真实的 supervisor 端点走一趟。"""

    async def run() -> str:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=_AUTH) as http:
            return await pseudo_handlers._preempt_in_family(_loop(http, caller.task_id), ref, message)

    return asyncio.run(run())


class TestEngineTool:
    def test_the_tool_steers_the_child_it_dispatched(
        self, state: SupervisorState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")
        results: list[str] = []
        monkeypatch.setattr(
            pseudo_handlers, "_emit_pseudo_tool_result",
            lambda _ls, _call, content, **_kwargs: results.append(content),
        )
        app = _app(state, tmp_path)

        async def run() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=_AUTH) as http:
                await pseudo_handlers._handle_pseudo_preempt_task(
                    _loop(http, entry.task_id), SimpleNamespace(id="call-1", name="preempt_task", arguments={}),
                    {"task_id": "bob", "message": "focus on the second file"},
                )

        asyncio.run(run())

        assert results == [f"已向 task bob({bob.task_id[:8]}) 注入追加指令，子任务继续执行"]
        assert bob.preempt_requested and bob.preempt_message == "focus on the second file"

    def test_without_a_message_the_child_is_asked_to_stop(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        bob = _delegate(state, entry, "bob")

        result = _engine_preempt(_app(state, tmp_path), entry, bob.task_id[:8], message="")

        assert result == f"已标记 task bob({bob.task_id[:8]}) 为 preempt，等待优雅退出"
        assert bob.preempt_requested and bob.preempt_message == ""

    def test_another_conversations_task_is_left_alone(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        _other_sid, other_entry = _conversation(state, _CONV_B)
        stranger = _delegate(state, other_entry, "bob")
        app = _app(state, tmp_path)

        by_name = _engine_preempt(app, entry, "bob")
        by_id = _engine_preempt(app, entry, stranger.task_id)

        assert "没有能插话的 'bob'" in by_name
        assert f"没有能插话的 '{stranger.task_id}'" in by_id
        assert _untouched(stranger)

    def test_an_ambiguous_name_lists_prefixes_to_retry_with(self, state: SupervisorState, tmp_path: Path) -> None:
        _sid, entry = _conversation(state)
        first = _delegate(state, entry, "bob", mode="fresh")
        second = _delegate(state, entry, "bob", mode="fresh")

        result = _engine_preempt(_app(state, tmp_path), entry, "bob")

        assert "对上了多个正在运行的任务" in result
        assert f"- {first.task_id[:8]}" in result and f"- {second.task_id[:8]}" in result
        assert _untouched(first) and _untouched(second)

    def test_an_empty_target_is_refused_before_any_request(self) -> None:
        loop = SimpleNamespace(rctx=SimpleNamespace(task_id="caller", supervisor_url="http://test", http=None))

        assert asyncio.run(pseudo_handlers._preempt_in_family(loop, "", "x")) == "task_id 不能为空"

    def test_a_loop_without_its_own_task_id_does_not_guess(self) -> None:
        loop = SimpleNamespace(rctx=SimpleNamespace(task_id="", supervisor_url="http://test", http=None))

        assert "找不到发起这次调用的任务" in asyncio.run(pseudo_handlers._preempt_in_family(loop, "bob", "x"))


def _scripted_preempt(
    ref: str, message: str, *, resolve: httpx.Response | Exception, preempt: httpx.Response | None = None,
) -> tuple[str, list[httpx.Request]]:
    """engine 的 preempt_task 对着一个照脚本回话的 supervisor 走一趟，返回工具结果和它发出的请求。"""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/family/resolve"):
            if isinstance(resolve, Exception):
                raise resolve
            return resolve
        if preempt is None:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        return preempt

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as http:
            return await pseudo_handlers._preempt_in_family(_loop(http, "caller-1"), ref, message)

    return asyncio.run(run()), sent


class TestEngineToolAgainstSupervisorAnswers:
    def test_it_asks_within_its_own_family_and_names_itself_when_preempting(self) -> None:
        result, sent = _scripted_preempt(
            "bob", "steer",
            resolve=httpx.Response(200, json={"task_id": "t-1", "node_id": "bob", "status": "running"}),
            preempt=httpx.Response(200, json={"ok": True, "task_id": "t-1"}),
        )

        resolve_request, preempt_request = sent
        assert resolve_request.method == "GET"
        assert resolve_request.url.path == "/v1/tasks/caller-1/family/resolve"
        assert resolve_request.url.params["ref"] == "bob"
        assert (preempt_request.method, preempt_request.url.path) == ("POST", "/v1/tasks/t-1/preempt")
        assert json.loads(preempt_request.content) == {"caller_task_id": "caller-1", "message": "steer"}
        assert result == "已向 task bob(t-1) 注入追加指令，子任务继续执行"

    def test_a_reference_with_odd_characters_travels_as_a_query_value(self) -> None:
        _result, sent = _scripted_preempt("a b/../c?d", "x", resolve=httpx.Response(404, json={"detail": "none"}))

        [request] = sent
        assert request.url.path == "/v1/tasks/caller-1/family/resolve"
        assert request.url.params["ref"] == "a b/../c?d"

    @pytest.mark.parametrize("resolve, preempt, expected", [
        pytest.param(
            httpx.Response(500, text="boom"), None, "查询任务 'bob' 失败: HTTP 500", id="查询出错",
        ),
        pytest.param(
            httpx.Response(200, json={"node_id": "bob"}), None, "查询任务 'bob' 失败: 没有返回任务 ID", id="没回 id",
        ),
        pytest.param(
            httpx.Response(200, json=["bob"]), None, "查询任务 'bob' 失败: 没有返回任务 ID", id="回的不是对象",
        ),
        pytest.param(
            httpx.Response(200, json={"task_id": "t-1", "node_id": "bob"}), httpx.Response(404, json={}),
            "task bob(t-1) 刚刚结束，或挂起去等子任务了，这次没有生效", id="找到之后又结束了",
        ),
        pytest.param(
            httpx.Response(200, json={"task_id": "t-1", "node_id": "bob"}), httpx.Response(422, json={}),
            "API 返回 422", id="打断端点拒绝",
        ),
    ])
    def test_each_supervisor_answer_reads_as_what_happened(
        self, resolve: httpx.Response, preempt: httpx.Response | None, expected: str,
    ) -> None:
        result, sent = _scripted_preempt("bob", "steer", resolve=resolve, preempt=preempt)

        assert result == expected
        assert len(sent) == (2 if preempt is not None else 1)

    def test_an_unreachable_supervisor_is_reported_not_raised(self) -> None:
        result, _sent = _scripted_preempt("bob", "steer", resolve=httpx.ConnectError("refused"))

        assert result.startswith("调用失败")


class TestAmbiguityMessage:
    @staticmethod
    def _answer(body: Any) -> SimpleNamespace:
        return SimpleNamespace(json=lambda: body)

    def test_a_capped_list_says_how_many_were_left_out(self) -> None:
        candidates = [
            {"task_id": f"abcdef1{index:x}", "node_id": "bob", "status": "running"} for index in range(10)
        ]

        text = pseudo_handlers._ambiguous_preempt_target(
            "bob", self._answer({"detail": {"error": "ambiguous", "total": 11, "candidates": candidates}}),
        )

        assert text.count("\n- ") == 10
        assert "- abcdef10（bob，running）" in text
        assert text.endswith("一共 11 个，这里只列了 10 个。")

    def test_entries_without_an_id_are_skipped_and_counted_as_left_out(self) -> None:
        text = pseudo_handlers._ambiguous_preempt_target("bob", self._answer({"detail": {
            "total": 2, "candidates": [{"node_id": "ghost"}, {"task_id": "abcdef12", "node_id": "bob"}],
        }}))

        assert "ghost" not in text
        assert "- abcdef12（bob，?）" in text
        assert text.endswith("一共 2 个，这里只列了 1 个。")

    @pytest.mark.parametrize("body", [["not", "an", "object"], {"detail": "ambiguous"}, None])
    def test_an_unexpected_body_still_gives_the_instruction(self, body: Any) -> None:
        text = pseudo_handlers._ambiguous_preempt_target("bob", self._answer(body))

        assert text == "'bob' 对上了多个正在运行的任务，task_id 换成下面某一个 ID 前缀再调用："

    def test_a_body_that_is_not_json_still_gives_the_instruction(self) -> None:
        def broken() -> Any:
            raise ValueError("not json")

        text = pseudo_handlers._ambiguous_preempt_target("bob", SimpleNamespace(json=broken))

        assert text == "'bob' 对上了多个正在运行的任务，task_id 换成下面某一个 ID 前缀再调用："


class TestToolDescription:
    def test_the_prefix_length_matches_what_the_supervisor_accepts(self) -> None:
        described = _preempt_task_spec()["function"]["parameters"]["properties"]["task_id"]["description"]

        assert f"至少 {SupervisorState._FAMILY_ID_PREFIX_MIN} 位" in described
