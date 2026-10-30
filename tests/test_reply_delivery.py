"""中间回复按投递 id 只落一条：engine 没等到应答就带着同一个 id 重发，supervisor 认出来回第一次写下的那条，重启后也认得；
没带 id 的照旧一条条写。事件端点在线程池里拿锁，不卡事件循环。"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _lock_probe import ThreadRecordingLock  # noqa: E402
from engine.context import RunContext  # noqa: E402
from engine.inference import async_delivery, pseudo_handlers  # noqa: E402
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from providers.base import ToolCall  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

CONV = "test:reply-delivery"
SUPERVISOR = "http://testserver"


def _state(workspace: Path, run_id: str = "reply-delivery") -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id=run_id),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _replies(state: SupervisorState) -> list[dict[str, Any]]:
    return [event for event in state.eventlog.iter_persisted_events() if event.get("type") == "intermediate_reply"]


def _reply_event(delivery_id: str = "r-1", text: str = "稍等") -> dict[str, Any]:
    payload: dict[str, Any] = {"node_id": "entry", "task_id": "task-1", "text": text, "attachments": []}
    if delivery_id:
        payload["delivery_id"] = delivery_id
    return {"type": "intermediate_reply", "payload": payload}


def _post_events(app: Any, session_id: str, *bodies: dict[str, Any]) -> list[httpx.Response]:
    async def run() -> list[httpx.Response]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SUPERVISOR) as client:
            return [await client.post(f"/v1/sessions/{session_id}/events", json=body) for body in bodies]

    return asyncio.run(run())


class _AnswerLostOnce(httpx.AsyncBaseTransport):
    """第一次请求照常交给 supervisor，应答却丢在回来的路上（按读超时抛出）；之后的照常回。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.sent: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        response = await self.inner.handle_async_request(request)
        if len(self.sent) == 1:
            await response.aread()
            await response.aclose()
            raise httpx.ReadTimeout("answer lost on the way back", request=request)
        return response


def test_a_reply_resent_after_its_answer_was_lost_lands_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(async_delivery, "REQUEST_RETRY_DELAYS_SEC", (0.0, 0.0))
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)
    transport = _AnswerLostOnce(httpx.ASGITransport(app=_app(state, tmp_path)))

    async def run() -> str:
        async with httpx.AsyncClient(transport=transport, base_url=SUPERVISOR) as client:
            rctx = RunContext(
                workspace_root=tmp_path, supervisor_url=SUPERVISOR, session_id=session_id, worker_id="worker",
                http=client, llm_http=client, task_id="task-1",
            )
            loop_state = SimpleNamespace(
                rctx=rctx, node=SimpleNamespace(id="entry", tool_mode="native"), messages=[],
                collected_attachments=[], tool_produced_attachments=[], formatter=create_tool_formatter("native"),
            )
            call = ToolCall(id="say", name="reply", arguments={"text": "稍等，我查一下"})
            await pseudo_handlers._handle_pseudo_tool(loop_state, call, 0)
            return loop_state.messages[-1]["content"]

    told = asyncio.run(run())

    assert told == "ok"
    first, resent = transport.sent
    assert first["payload"]["delivery_id"] and resent == first
    [reply] = _replies(state)
    assert reply["payload"]["text"] == "稍等，我查一下"


def test_a_repeated_reply_is_answered_with_the_event_already_written(tmp_path: Path) -> None:
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)

    first, again = _post_events(_app(state, tmp_path), session_id, _reply_event(), _reply_event(text="稍等（重发）"))

    assert first.status_code == again.status_code == 200
    assert first.json()["deduped"] is False and again.json()["deduped"] is True
    assert (again.json()["event_id"], again.json()["seq"]) == (first.json()["event_id"], first.json()["seq"])
    [reply] = _replies(state)
    assert reply["payload"]["text"] == "稍等"
    # 带上前缀再落盘，和 reminder:、async-tool: 这些别处的投递 id 分开
    assert reply["payload"]["delivery_id"] == "reply:r-1"


def test_a_reply_id_already_used_by_another_session_is_refused_and_not_written(tmp_path: Path) -> None:
    # 只认本会话的凭据不能拿别的会话的 id 去换那一条事件
    state = _state(tmp_path)
    mine = state.get_or_create_session(channel="test", conversation_key=CONV)
    other = state.get_or_create_session(channel="test", conversation_key=f"{CONV}:other")
    app = _app(state, tmp_path)

    [first, again] = _post_events(app, mine, _reply_event(), _reply_event())
    [borrowed] = _post_events(app, other, _reply_event(text="借来的 id"))

    assert again.json()["deduped"] is True and again.json()["event_id"] == first.json()["event_id"]
    assert borrowed.status_code == 409
    assert "已经被别的会话用过" in borrowed.json()["detail"]
    assert first.json()["event_id"] not in borrowed.text
    [reply] = _replies(state)
    assert reply["session_id"] == mine and reply["payload"]["text"] == "稍等"


def test_a_reply_is_still_recognised_after_a_restart(tmp_path: Path) -> None:
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)
    [first] = _post_events(_app(state, tmp_path), session_id, _reply_event())
    restarted = _state(tmp_path, run_id="reply-delivery-restarted")

    [again] = _post_events(_app(restarted, tmp_path), session_id, _reply_event())

    assert again.json()["deduped"] is True and again.json()["event_id"] == first.json()["event_id"]
    assert len(_replies(restarted)) == 1


def test_replies_without_an_id_are_each_written(tmp_path: Path) -> None:
    # 旧 engine、脚本工具直接发来的都不带 id
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)

    answers = _post_events(_app(state, tmp_path), session_id, _reply_event(delivery_id=""), _reply_event(delivery_id=""))

    assert [answer.json() for answer in answers] == [{"ok": True}, {"ok": True}]
    assert len(_replies(state)) == 2


def test_the_event_endpoint_takes_its_locks_off_the_event_loop(tmp_path: Path) -> None:
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)
    app = _app(state, tmp_path)
    state._lock = ThreadRecordingLock(state._lock)
    state.eventlog._lock = ThreadRecordingLock(state.eventlog._lock)
    bodies = [_reply_event(), {"type": "tool_call_start", "payload": {"task_id": "task-1", "tool_name": "probe"}}]

    async def run() -> tuple[int, list[int]]:
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SUPERVISOR) as client:
            statuses = [(await client.post(f"/v1/sessions/{session_id}/events", json=body)).status_code for body in bodies]
        return loop_thread, statuses

    loop_thread, statuses = asyncio.run(run())

    assert statuses == [200, 200]
    assert state._lock.threads and state.eventlog._lock.threads
    assert loop_thread not in state._lock.threads + state.eventlog._lock.threads
