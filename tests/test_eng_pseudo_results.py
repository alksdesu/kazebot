"""事件没送到、切节点没切成时，模型看到的工具结果得是真实结局，而不是一律 ok。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from engine.context import RunContext
from engine.inference import pseudo_handlers
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
