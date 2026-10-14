"""平台输出说明从 SDK 到提示词：只进这一轮的提示词，不进对话历史。

稳定的段进缓存前缀，每轮会变的段进动态区；打断时换成新的；转述结果的那一轮沿用发起任务的；
不带这个字段的老客户端拿到的提示词不变。
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from clonoth_sdk.client import ClonothClient
from engine.builtin.knowledge_inject import _rebuild_prompt_messages
from engine.builtin.preempt import _inject_preempt_message
from engine.context import RunContext
from engine.conversation_store import ConversationStore
from engine.inference.message_assembly import assemble_initial_messages, assemble_messages_with_injections
from engine.node import Node
import supervisor.admin_api as admin_api
from engine.output_guide import (
    OUTPUT_GUIDE_KEY,
    STATIC_GUIDE_MARKER,
    guide_texts,
    has_static_guide,
    static_guide_messages,
    turn_guide_parts,
)
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.eventlog import EventLog
from supervisor.execution.service import ExecutionService
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import OUTPUT_GUIDE_MAX_CHARS, Task, TaskStatus

_SPLIT = {"key": "split", "text": "【分条】用 [SPLIT] 分条", "volatile": False}
_EMOJI = {"key": "emoji", "text": "【可用表情】甲、乙", "volatile": True}
_GUIDE = {"version": 1, "sections": [_SPLIT, _EMOJI]}
_NEW_GUIDE = {"version": 1, "sections": [
    {"key": "split", "text": "【分条】最多 3 条", "volatile": False},
    {"key": "emoji", "text": "【可用表情】丙", "volatile": True},
]}
_CLEARED = {"version": 1, "sections": []}

_PERSONA = {"role": "system", "content": "persona"}
_MEMORY = {"role": "system", "content": "[MEMORY:CONSTANT]\nfact\n[/MEMORY:CONSTANT]", "_knowledge_source": "memory"}
_MEMORY_TURN = {"role": "system", "content": "[MEMORY:ACTIVE]\nrecent\n[/MEMORY:ACTIVE]"}
_HISTORY = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
_PROMPTS = {
    "string": [_PERSONA],
    "block": [_PERSONA, {"role": "user", "content": "fixed rules"}, {"role": "history"}],
}


def _texts(messages: list[dict]) -> str:
    return "\n".join(str(message.get("content", "")) for message in messages)


def _dynamic(messages: list[dict]) -> str:
    return "\n".join(str(message["content"]) for message in messages if message.get("_dynamic"))


class TestRendering:
    def test_stable_and_volatile_sections_are_told_apart(self) -> None:
        assert guide_texts(_GUIDE) == (["【分条】用 [SPLIT] 分条"], ["【可用表情】甲、乙"])

    @pytest.mark.parametrize("raw", [
        None, "text", {"sections": "x"},
        {"sections": [None, 3, {"text": ""}, {"text": 5}, {"key": "k"}, {"text": "  "}]},
    ])
    def test_malformed_input_renders_nothing(self, raw: object) -> None:
        assert guide_texts(raw) == ([], [])
        assert static_guide_messages(raw) == [] and turn_guide_parts(raw) == []

    def test_only_a_real_true_makes_a_section_volatile(self) -> None:
        assert guide_texts({"sections": [{"text": "a", "volatile": "yes"}]}) == (["a"], [])

    def test_the_static_message_is_marked_for_replacement(self) -> None:
        assert static_guide_messages(_GUIDE) == [{
            "role": "system",
            "content": "[OUTPUT_GUIDE:CONSTANT]\n【分条】用 [SPLIT] 分条\n[/OUTPUT_GUIDE:CONSTANT]",
            STATIC_GUIDE_MARKER: True,
        }]

    def test_the_turn_part_carries_volatile_sections_unless_asked_for_all(self) -> None:
        assert turn_guide_parts(_GUIDE) == ["[OUTPUT_GUIDE:ACTIVE]\n【可用表情】甲、乙\n[/OUTPUT_GUIDE:ACTIVE]"]
        assert turn_guide_parts(_GUIDE, include_stable=True) == [
            "[OUTPUT_GUIDE:ACTIVE]\n【分条】用 [SPLIT] 分条\n\n【可用表情】甲、乙\n[/OUTPUT_GUIDE:ACTIVE]",
        ]


class TestLayout:
    @staticmethod
    def _assemble(mode: str, **kwargs: object) -> list[dict]:
        messages, _ = assemble_messages_with_injections(
            workspace_root=Path("."), system_prompt=list(_PROMPTS[mode]), history=list(_HISTORY), instruction="now",
            memory_static=[_MEMORY], memory_dynamic=[_MEMORY_TURN], **kwargs,  # type: ignore[arg-type]
        )
        return messages

    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_stable_sections_follow_memory_and_volatile_ones_join_the_turn_block(self, mode: str) -> None:
        messages = self._assemble(mode, output_guide=_GUIDE)

        guide_at = next(index for index, message in enumerate(messages) if message.get(STATIC_GUIDE_MARKER))
        assert messages[guide_at - 1] == _MEMORY and messages[guide_at + 1] == _HISTORY[0]
        turn = _dynamic(messages)
        assert turn.index("[MEMORY:ACTIVE]") < turn.index("[OUTPUT_GUIDE:ACTIVE]") and "【可用表情】甲、乙" in turn
        assert "【可用表情】" not in messages[guide_at]["content"]
        assert messages[-1] == {"role": "user", "content": "now"}

    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_without_a_guide_nothing_changes(self, mode: str) -> None:
        plain = self._assemble(mode)

        assert self._assemble(mode, output_guide=None) == plain == self._assemble(mode, output_guide=_CLEARED)
        assert not has_static_guide(plain) and "OUTPUT_GUIDE" not in _texts(plain)

    @pytest.mark.parametrize("prompt", ["persona text", [{"role": "system", "content": "persona"}, {"role": "history"}]])
    def test_the_skeleton_already_carries_the_guide(self, tmp_path: Path, prompt: object) -> None:
        messages, _, _ = assemble_initial_messages(
            workspace_root=tmp_path, runtime_cfg={}, node=Node(id="chat", type="ai", prompt=prompt),  # type: ignore[arg-type]
            instruction="now", history=list(_HISTORY), task_context={OUTPUT_GUIDE_KEY: _GUIDE},
        )

        assert has_static_guide(messages) and "【可用表情】甲、乙" in _dynamic(messages)
        assert messages[-1] == {"role": "user", "content": "now"}

    def test_the_knowledge_rebuild_keeps_the_guide(self) -> None:
        ctx = SimpleNamespace(
            messages=[], rctx=SimpleNamespace(workspace_root=Path("."), task_context={OUTPUT_GUIDE_KEY: _GUIDE}),
            extra={
                "system_prompt": [_PERSONA], "history": list(_HISTORY), "instruction_text": "now",
                "memory_static_messages": [_MEMORY],
            },
        )

        _rebuild_prompt_messages(ctx)

        assert has_static_guide(ctx.messages) and "【可用表情】甲、乙" in _dynamic(ctx.messages)


def _loop(root: Path, payload: dict, *, guide: dict | None = _GUIDE, in_prefix: bool = True) -> SimpleNamespace:
    context: dict = {"platform_auth": {"is_admin": False}}
    if guide is not None:
        context[OUTPUT_GUIDE_KEY] = guide
    prefix = [dict(_PERSONA), *(static_guide_messages(guide) if in_prefix else [])]
    history = [{"role": "user", "content": "old request"}]
    return SimpleNamespace(
        node=Node(id="chat", type="ai"), runtime_cfg={}, preempt_inject_info=payload,
        rctx=SimpleNamespace(
            workspace_root=root, task_context=context, session_id="branch", child_session_id="",
            task_id="task", conversation_store=ConversationStore(root / "data" / "conversations"),
            consume_preempt=AsyncMock(), emit_event=AsyncMock(),
        ),
        messages=[*prefix, *history, {"role": "user", "content": "outdated turn", "_dynamic": True}],
        history=history, system_prompt=[_PERSONA], is_block_mode=False,
    )


class TestPreempt:
    @pytest.mark.asyncio
    async def test_a_new_guide_replaces_the_old_one_in_place(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, {"message": "follow-up", OUTPUT_GUIDE_KEY: _NEW_GUIDE})
        position = next(index for index, message in enumerate(loop.messages) if message.get(STATIC_GUIDE_MARKER))

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        assert loop.messages[position] == static_guide_messages(_NEW_GUIDE)[0]
        assert sum(1 for message in loop.messages if message.get(STATIC_GUIDE_MARKER)) == 1
        assert "【可用表情】丙" in _dynamic(loop.messages) and "甲、乙" not in _texts(loop.messages)
        assert loop.rctx.task_context[OUTPUT_GUIDE_KEY] == _NEW_GUIDE
        assert loop.messages[-1] == {"role": "user", "content": "follow-up"}

    @pytest.mark.asyncio
    async def test_without_a_new_guide_the_turn_block_keeps_the_old_one(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, {"message": "follow-up"})

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        assert "outdated turn" not in _texts(loop.messages)
        assert "【可用表情】甲、乙" in _dynamic(loop.messages)
        assert loop.messages[1] == static_guide_messages(_GUIDE)[0]

    @pytest.mark.asyncio
    async def test_a_guide_arriving_mid_task_does_not_touch_the_prefix(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, {"message": "follow-up", OUTPUT_GUIDE_KEY: _NEW_GUIDE}, guide=None, in_prefix=False)
        prefix = [message for message in loop.messages if message.get("role") == "system"]

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        assert [message for message in loop.messages if message.get("role") == "system"] == prefix
        turn = _dynamic(loop.messages)
        assert "【分条】最多 3 条" in turn and "【可用表情】丙" in turn

    @pytest.mark.asyncio
    async def test_an_empty_guide_clears_both_places(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, {"message": "follow-up", OUTPUT_GUIDE_KEY: _CLEARED})

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        assert not has_static_guide(loop.messages) and "OUTPUT_GUIDE" not in _texts(loop.messages)

    @pytest.mark.asyncio
    async def test_the_guide_never_reaches_the_conversation_store(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, {"message": "follow-up", OUTPUT_GUIDE_KEY: _NEW_GUIDE})

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        stored = loop.rctx.conversation_store.load("branch")
        assert [message.content for message in stored] == ["follow-up"]
        assert OUTPUT_GUIDE_KEY not in json.dumps([message.meta for message in stored])


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="output-guide"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _inbound(state: SupervisorState, session_id: str, **extra: object) -> Task:
    event = state.eventlog.append(
        session_id=session_id, component="shell", type_="inbound_message",
        payload={"channel": "qq_private", "conversation_key": state.sessions[session_id].conversation_key,
                 "text": "request", **extra},
    )
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _finish(state: SupervisorState, task: Task) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker"
    state.complete_task(task_id=task.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "done"}})


def _session(state: SupervisorState, key: str = "qq_private:123") -> str:
    return state.get_or_create_session(channel="qq_private", conversation_key=key)


class TestSupervisorStorage:
    def test_the_guide_rides_in_task_context_without_blank_sections(self, state: SupervisorState) -> None:
        blank = {"key": "blank", "text": "  ", "volatile": False}
        task = _inbound(state, _session(state), output_guide={"version": 1, "sections": [_SPLIT, blank, _EMOJI]})

        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE
        assert task.input["instruction"] == "request"

    @pytest.mark.parametrize("guide", [None, {}, _CLEARED, {"sections": "broken"}])
    def test_no_usable_guide_leaves_no_key(self, state: SupervisorState, guide: object) -> None:
        task = _inbound(state, _session(state), output_guide=guide)

        assert OUTPUT_GUIDE_KEY not in task.input["task_context"]

    def test_only_task_created_stores_the_guide(self, state: SupervisorState) -> None:
        task = _inbound(state, _session(state), output_guide=_GUIDE)

        created = state._event_payload_for_task(task, event_type="task_created")
        started = state._event_payload_for_task(task, event_type="task_started")

        assert created["input"]["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE
        assert OUTPUT_GUIDE_KEY not in started["input"]["task_context"]
        assert started["input"]["task_context"]["platform_auth"] == task.input["task_context"]["platform_auth"]
        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE

    def test_a_replayed_task_gets_its_guide_back_from_task_created(self, state: SupervisorState) -> None:
        task = _inbound(state, _session(state), output_guide=_GUIDE)
        created = state._event_payload_for_task(task, event_type="task_created")
        started = state._event_payload_for_task(task, event_type="task_started")
        state.tasks.clear()

        state._apply_task_snapshot(created)
        state._apply_task_snapshot(started)

        assert state.tasks[task.task_id].input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE


class TestSupervisorPreempt:
    @pytest.fixture
    def task(self, state: SupervisorState) -> Task:
        task = _inbound(state, _session(state), output_guide=_GUIDE)
        task.status = TaskStatus.running
        return task

    def test_a_new_guide_is_handed_over_and_written_back(self, state: SupervisorState, task: Task) -> None:
        assert state.preempt_task(task.task_id, "follow-up", output_guide=_NEW_GUIDE)
        pending = state.is_task_preempted(task.task_id)

        assert pending[OUTPUT_GUIDE_KEY] == _NEW_GUIDE
        consumed = state.consume_preempt_message(task.task_id, expected_revision=pending["revision"])
        assert consumed[OUTPUT_GUIDE_KEY] == _NEW_GUIDE
        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _NEW_GUIDE
        assert task.preempt_output_guide is None

    def test_a_preempt_without_a_guide_keeps_the_task_one(self, state: SupervisorState, task: Task) -> None:
        assert state.preempt_task(task.task_id, "follow-up")

        assert OUTPUT_GUIDE_KEY not in state.is_task_preempted(task.task_id)
        assert OUTPUT_GUIDE_KEY not in state.consume_preempt_message(task.task_id)
        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE

    def test_an_empty_guide_clears_the_task_one(self, state: SupervisorState, task: Task) -> None:
        assert state.preempt_task(task.task_id, "follow-up", output_guide=_CLEARED)

        assert state.consume_preempt_message(task.task_id)[OUTPUT_GUIDE_KEY] == _CLEARED
        assert OUTPUT_GUIDE_KEY not in task.input["task_context"]

    def test_a_newer_preempt_without_a_guide_drops_the_older_pending_one(self, state: SupervisorState, task: Task) -> None:
        state.preempt_task(task.task_id, "first", output_guide=_NEW_GUIDE)
        state.preempt_task(task.task_id, "second")

        assert OUTPUT_GUIDE_KEY not in state.is_task_preempted(task.task_id)


class TestApi:
    @pytest.fixture
    def client(self, state: SupervisorState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-token")
        monkeypatch.setattr(admin_api, "_admin_token", "")
        monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
        app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path))
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    def _post_inbound(self, client: TestClient, guide: object) -> httpx.Response:
        body = {"channel": "qq_private", "conversation_key": "qq_private:777", "text": "hello", "output_guide": guide}
        return client.post("/v1/inbound", json=body)

    def test_an_inbound_guide_reaches_the_entry_task(self, client: TestClient, state: SupervisorState) -> None:
        response = self._post_inbound(client, _GUIDE)

        assert response.status_code == 200
        [task] = [task for task in state.tasks.values() if task.source_inbound_seq == response.json()["inbound_seq"]]
        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE

    @pytest.mark.parametrize("guide", [
        pytest.param({"sections": [{"text": "no key"}]}, id="missing_key"),
        pytest.param({"sections": [{"key": "k", "text": 5}]}, id="text_not_a_string"),
        pytest.param({"sections": [{"key": "k", "text": "t"}] * 33}, id="too_many_sections"),
        pytest.param({"sections": [{"key": f"k{i}", "text": "字" * 30_000} for i in range(3)]}, id="too_long"),
    ])
    def test_a_malformed_guide_is_refused(self, client: TestClient, guide: dict) -> None:
        assert self._post_inbound(client, guide).status_code == 422

    def test_the_limits_match_what_a_full_qq_emoji_list_needs(self) -> None:
        # 收藏表情 200 个、表情包 200 张拉满时，表情那一段两万字上下。
        assert OUTPUT_GUIDE_MAX_CHARS >= 40_000

    def test_the_preempt_endpoint_validates_and_forwards(self, client: TestClient, state: SupervisorState) -> None:
        task = _inbound(state, _session(state), output_guide=_GUIDE)
        task.status = TaskStatus.running

        refused = client.post(f"/v1/tasks/{task.task_id}/preempt", json={"message": "x", "output_guide": {"sections": [{}]}})
        accepted = client.post(f"/v1/tasks/{task.task_id}/preempt", json={"message": "x", "output_guide": _NEW_GUIDE})

        assert refused.status_code == 422 and accepted.status_code == 200
        assert state.is_task_preempted(task.task_id)[OUTPUT_GUIDE_KEY] == _NEW_GUIDE


class TestRelays:
    def test_a_dispatch_result_is_relayed_with_the_callers_guide(self, state: SupervisorState) -> None:
        parent = _session(state)
        caller = _inbound(state, parent, output_guide=_GUIDE)
        _finish(state, caller)
        key = state.sessions[parent].conversation_key
        child = _session(state, f"agent:child:{key}:fresh")
        origin = {
            "parent_session_id": parent, "caller_node_id": "main", "parent_conversation_key": key,
            "context_mode": "fresh", "caller_task_id": caller.task_id,
        }
        dispatched = _inbound(state, child, entry_node_id="child", dispatch_context_mode="fresh", dispatch_origin=origin)

        _finish(state, dispatched)

        [relay] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert relay.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE

    def test_an_old_dispatch_without_a_caller_is_relayed_without_a_guide(self, state: SupervisorState) -> None:
        parent = _session(state)
        _finish(state, _inbound(state, parent, output_guide=_GUIDE))
        key = state.sessions[parent].conversation_key
        child = _session(state, f"agent:child:{key}:fresh")
        origin = {"parent_session_id": parent, "caller_node_id": "main", "parent_conversation_key": key, "context_mode": "fresh"}

        _finish(state, _inbound(state, child, entry_node_id="child", dispatch_context_mode="fresh", dispatch_origin=origin))

        [relay] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert OUTPUT_GUIDE_KEY not in relay.input["task_context"]

    def test_an_async_tool_result_is_relayed_with_the_launching_tasks_guide(self, state: SupervisorState) -> None:
        session = _session(state)
        launcher = _inbound(state, session, output_guide=_GUIDE)
        _finish(state, launcher)

        assert state.inject_async_result(session, "tool finished", task_id=launcher.task_id)["strategy"] == "inbound"

        relay = max(state.tasks.values(), key=lambda task: task.source_inbound_seq or 0)
        assert relay.task_id != launcher.task_id
        assert relay.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE

    def test_a_late_async_result_after_cancel_does_not_create_a_new_task(self, state: SupervisorState) -> None:
        session = _session(state)
        launcher = _inbound(state, session, output_guide=_GUIDE)
        state.cancel_active_tasks(session)
        task_count = len(state.tasks)

        result = state.inject_async_result(session, "late", task_id=launcher.task_id)

        assert result == {"ok": True, "strategy": "dropped", "reason": "origin_cancelled"}
        assert len(state.tasks) == task_count
        assert not any(event["type"] == "inbound_message" and event["payload"].get("text") == "late" for event in state.eventlog.events)

    def test_a_late_attachment_after_cancel_does_not_send_outbound(self, state: SupervisorState) -> None:
        session = _session(state)
        launcher = _inbound(state, session)
        state.cancel_active_tasks(session)

        result = state.inject_async_result(session, "", ["/tmp/late.png"], task_id=launcher.task_id)

        assert result == {"ok": True, "strategy": "dropped", "reason": "origin_cancelled"}
        assert not any(event["type"] == "outbound_message" and event["payload"].get("task_id") == launcher.task_id for event in state.eventlog.events)

    def test_a_plan_does_not_copy_the_turns_guide(self, state: SupervisorState, tmp_path: Path) -> None:
        task = _inbound(state, _session(state), output_guide=_GUIDE)
        service = ExecutionService.__new__(ExecutionService)
        service.state = state

        _, context = service._origin(SimpleNamespace(task_id=task.task_id))

        assert OUTPUT_GUIDE_KEY not in context and context["platform_auth"] == task.input["task_context"]["platform_auth"]
        assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _GUIDE


class TestSdk:
    @staticmethod
    async def _requests(call) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"session_id": "s", "inbound_seq": 1, "accepted": True, "ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sdk = ClonothClient("http://test")
            sdk._client = http
            await call(sdk)
        return requests

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guide, sent", [(_GUIDE, True), (None, False), ({}, False)])
    async def test_submit_sends_the_guide_only_when_there_is_one(self, guide: object, sent: bool) -> None:
        [request] = await self._requests(lambda sdk: sdk.submit_inbound(
            channel="qq_private", conversation_key="qq_private:1", text="hi", output_guide=guide,
        ))

        body = json.loads(request.content)
        assert (OUTPUT_GUIDE_KEY in body) is sent and body["text"] == "hi"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guide, sent", [(_NEW_GUIDE, True), (_CLEARED, True), (None, False)])
    async def test_preempt_sends_an_explicit_guide_even_when_empty(self, guide: object, sent: bool) -> None:
        [request] = await self._requests(lambda sdk: sdk.preempt_task("task", message="hi", output_guide=guide))

        assert (OUTPUT_GUIDE_KEY in json.loads(request.content)) is sent


@pytest.mark.asyncio
async def test_sdk_api_and_engine_swap_the_same_guide(state: SupervisorState, tmp_path: Path) -> None:
    task = _inbound(state, _session(state), output_guide=_GUIDE)
    task.status = TaskStatus.running
    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path))
    loop = _loop(tmp_path, {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sdk = ClonothClient("http://test")
        sdk._client = client
        assert await sdk.preempt_task(task.task_id, message="follow-up", output_guide=_NEW_GUIDE)
        context = RunContext(
            workspace_root=tmp_path, supervisor_url="http://test", session_id="session",
            worker_id="worker", http=client, llm_http=client, task_id=task.task_id,
            task_context=dict(task.input["task_context"]),
        )
        context.conversation_store = loop.rctx.conversation_store
        loop.rctx = context
        loop.preempt_inject_info = await context.check_preempted()
        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=0), loop)

    assert "【分条】最多 3 条" in _texts(loop.messages) and "【可用表情】丙" in _dynamic(loop.messages)
    assert task.input["task_context"][OUTPUT_GUIDE_KEY] == _NEW_GUIDE
    assert not state.is_task_preempted(task.task_id)["preempted"]
