"""事件没送到、切节点没切成时，模型看到的工具结果得是真实结局，而不是一律 ok。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from engine.context import EVENT_UNCONFIRMED, RunContext
from engine.inference import async_delivery, pseudo_handlers
from engine.inference.tool_format import create_tool_formatter
from engine.protocol import ACTION_FINISH
from providers.base import ToolCall


@pytest.mark.parametrize("answer, delivered", [(200, True), (204, True), (500, False), (404, False), ("raise", False)])
def test_emit_event_reports_whether_the_supervisor_took_the_event(tmp_path: Path, answer: object, delivered: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if answer == "raise":
            raise httpx.ConnectError("supervisor down", request=request)
        return httpx.Response(answer)

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            rctx = RunContext(
                workspace_root=tmp_path, supervisor_url="http://sup", session_id="session", worker_id="worker",
                http=client, llm_http=client, task_id="task",
            )
            return await rctx.emit_event("node_started", {"node_id": "entry"})

    assert asyncio.run(run()) is delivered


class _Response:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _loop_state(tmp_path: Path, *, emit_result: object = None, post: object = None) -> tuple[SimpleNamespace, list]:
    events: list = []

    async def emit_event(event_type: str, payload: dict) -> object:
        events.append(event_type)
        return emit_result

    rctx = SimpleNamespace(
        workspace_root=tmp_path, supervisor_url="http://sup", session_id="branch", parent_session_id="parent",
        task_id="task", task_context={}, emit_event=emit_event, current_llm_request_id="",
        http=SimpleNamespace(post=post or AsyncMock(return_value=_Response(200, {"ok": True}))),
    )
    state = SimpleNamespace(
        rctx=rctx, node=SimpleNamespace(id="entry", tool_mode="native"), messages=[],
        collected_attachments=[], tool_produced_attachments=[], formatter=create_tool_formatter("native"),
    )
    return state, events


def _handle(state: SimpleNamespace, call: ToolCall):
    return asyncio.run(pseudo_handlers._handle_pseudo_tool(state, call, 0))


def test_a_reply_the_supervisor_never_got_is_reported_to_the_model(tmp_path: Path) -> None:
    state, events = _loop_state(tmp_path, emit_result=False)

    assert _handle(state, ToolCall(id="say", name="reply", arguments={"text": "hi"})) is None

    assert events == ["intermediate_reply"]
    result = state.messages[-1]
    assert result["tool_call_id"] == "say" and result["content"].startswith("Error:")


def test_a_delivered_reply_is_acknowledged(tmp_path: Path) -> None:
    state, _events = _loop_state(tmp_path, emit_result=True)
    _handle(state, ToolCall(id="say", name="reply", arguments={"text": "hi"}))
    assert state.messages[-1]["content"] == "ok"


def test_an_unconfirmed_reply_tells_the_model_not_to_send_it_again_as_is(tmp_path: Path) -> None:
    state, _events = _loop_state(tmp_path, emit_result=EVENT_UNCONFIRMED)

    _handle(state, ToolCall(id="say", name="reply", arguments={"text": "hi"}))

    told = state.messages[-1]["content"]
    assert "不确定用户有没有收到" in told and "别原样重发" in told
    assert not told.startswith("Error:")


def test_every_reply_carries_its_own_delivery_id(tmp_path: Path) -> None:
    payloads: list[dict] = []

    async def emit_event(event_type: str, payload: dict) -> bool:
        payloads.append(payload)
        return True

    state, _events = _loop_state(tmp_path)
    state.rctx.emit_event = emit_event

    for call_id in ("one", "two"):
        _handle(state, ToolCall(id=call_id, name="reply", arguments={"text": "hi"}))

    ids = [payload["delivery_id"] for payload in payloads]
    assert all(ids) and len(set(ids)) == 2


def _answer(request: httpx.Request, outcome: object) -> httpx.Response:
    """outcome 是状态码，或者这次要抛的 httpx 异常类型。"""
    if isinstance(outcome, type) and issubclass(outcome, Exception):
        raise outcome("synthetic", request=request)
    return httpx.Response(outcome, json={"ok": True})


def _deliver(tmp_path: Path, outcomes: list, act) -> tuple[object, list[dict]]:
    """用真的 RunContext 走一遍 act(rctx)，第 n 次请求按 outcomes[n] 应答；返回 act 的结果和每次发出的请求体。"""
    sent: list[dict] = []
    pending = list(outcomes)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _answer(request, pending.pop(0))

    async def run() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            rctx = RunContext(
                workspace_root=tmp_path, supervisor_url="http://sup", session_id="session", worker_id="worker",
                http=client, llm_http=client, task_id="task",
            )
            return await act(rctx)

    return asyncio.run(run()), sent


@pytest.fixture()
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(async_delivery, "REQUEST_RETRY_DELAYS_SEC", (0.0, 0.0))


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("outcomes, expected", [
    pytest.param([200], True, id="taken"),
    pytest.param([httpx.ReadTimeout, 200], True, id="taken-on-the-resend"),
    pytest.param([404], False, id="refused"),
    pytest.param([httpx.ConnectError] * 3, False, id="never-reached"),
    pytest.param([httpx.ReadTimeout] * 3, EVENT_UNCONFIRMED, id="no-answer"),
    pytest.param([503] * 3, EVENT_UNCONFIRMED, id="kept-failing"),
    pytest.param([httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectError], EVENT_UNCONFIRMED, id="maybe-taken-first"),
])
def test_an_event_with_a_delivery_id_is_resent_until_answered(tmp_path: Path, outcomes: list, expected: object) -> None:
    async def emit(rctx: RunContext) -> object:
        return await rctx.emit_event("intermediate_reply", {"text": "hi", "delivery_id": "r-1"})

    delivered, sent = _deliver(tmp_path, outcomes, emit)

    assert delivered is expected
    assert len(sent) == len(outcomes)
    assert all(body == sent[0] for body in sent)


def _reply_through_supervisor(tmp_path: Path, outcomes: list) -> tuple[str, list[dict]]:
    async def reply(rctx: RunContext) -> str:
        state = SimpleNamespace(
            rctx=rctx, node=SimpleNamespace(id="entry", tool_mode="native"), messages=[],
            collected_attachments=[], tool_produced_attachments=[], formatter=create_tool_formatter("native"),
        )
        await pseudo_handlers._handle_pseudo_tool(state, ToolCall(id="say", name="reply", arguments={"text": "稍等"}), 0)
        return state.messages[-1]["content"]

    return _deliver(tmp_path, outcomes, reply)


@pytest.mark.usefixtures("no_backoff")
def test_the_model_hears_ok_for_a_reply_the_supervisor_took(tmp_path: Path) -> None:
    told, sent = _reply_through_supervisor(tmp_path, [httpx.ReadTimeout, 200])

    assert told == "ok"
    assert sent[0]["payload"]["delivery_id"] and sent[0] == sent[1]


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("outcomes", [
    pytest.param([httpx.ConnectError] * 3, id="never-reached"),
    pytest.param([400], id="refused"),
])
def test_the_model_hears_the_user_did_not_get_a_reply_that_never_got_in(tmp_path: Path, outcomes: list) -> None:
    told, _sent = _reply_through_supervisor(tmp_path, outcomes)

    assert told == "Error: 这条 reply 没有送到 supervisor，用户没有收到。可以稍后再发一次，或把内容放进 finish。"


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("outcomes", [
    pytest.param([httpx.ReadTimeout] * 3, id="no-answer"),
    pytest.param([502] * 3, id="kept-failing"),
])
def test_the_model_hears_it_is_unknown_whether_the_user_got_the_reply(tmp_path: Path, outcomes: list) -> None:
    told, sent = _reply_through_supervisor(tmp_path, outcomes)

    assert "不确定用户有没有收到" in told and "别原样重发" in told
    assert len(sent) == 3 and len({body["payload"]["delivery_id"] for body in sent}) == 1


@pytest.mark.parametrize("post", [
    AsyncMock(return_value=_Response(500, {"detail": "unavailable"})),
    AsyncMock(side_effect=httpx.ConnectError("supervisor down")),
])
def test_a_failed_switch_keeps_the_turn_on_the_current_node(tmp_path: Path, monkeypatch, post: AsyncMock) -> None:
    monkeypatch.setattr(pseudo_handlers, "_persist_ctx", lambda *_args: "ctx")
    state, events = _loop_state(tmp_path, post=post)

    action = _handle(state, ToolCall(id="sw", name="switch_node", arguments={"target": "helper", "text": "bye"}))

    assert action is None
    assert "node_switch" not in events
    result = state.messages[-1]
    assert result["tool_call_id"] == "sw" and "切换节点失败" in result["content"]


@pytest.mark.parametrize("response, reason", [
    (_Response(500, {"detail": "unavailable"}), "unavailable"),
    (_Response(409, {"detail": {"code": "busy", "message": "会话正在切换"}}), "会话正在切换（busy，HTTP 409）"),
    (_Response(503, None), "supervisor 回了 HTTP 503"),
], ids=["string", "structured", "no-detail"])
def test_a_refused_switch_names_the_supervisors_reason(
    tmp_path: Path, monkeypatch, response: _Response, reason: str,
) -> None:
    monkeypatch.setattr(pseudo_handlers, "_persist_ctx", lambda *_args: "ctx")
    state, _events = _loop_state(tmp_path, post=AsyncMock(return_value=response))

    _handle(state, ToolCall(id="sw", name="switch_node", arguments={"target": "helper", "text": "bye"}))

    assert f"切换节点失败（{reason}）" in state.messages[-1]["content"]


def test_a_successful_switch_finishes_the_turn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pseudo_handlers, "_persist_ctx", lambda *_args: "ctx")
    post = AsyncMock(return_value=_Response(200, {"ok": True}))
    state, events = _loop_state(tmp_path, post=post)

    action = _handle(state, ToolCall(id="sw", name="switch_node", arguments={"target": "helper", "text": "bye"}))

    assert action is not None and action.action == ACTION_FINISH
    assert action.result["text"] == "bye"
    assert events == ["node_switch"]
    assert state.messages[-1]["content"] == "ok"
    assert post.await_args.args[0] == "http://sup/v1/sessions/parent/switch_node"
