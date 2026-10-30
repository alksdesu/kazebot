"""对话历史写盘失败：OSError 重试两次（一条重试完仍失败后，后面的只试一次，写成功一条再恢复），仍没写进去的在落盘点报成一条 conversation_write_failed 会话事件，原因里不带路径，任务结局不变。"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

# 先把 engine.inference 整个拉起来：只跑部分测试时，先导入 plaintext 会撞上它和 ai_step 的循环导入。
from _engine_loop_fixture import (  # noqa: E402
    SESSION_ID,
    TASK_ID,
    EventRecorder,
    NodeRun,
    ScriptedRegistry,
    call,
    make_loop_state,
    turn,
    use_hooks,
)
from engine import conversation_store, runner  # noqa: E402
from engine.conversation_store import ConversationStore, ConversationWriter, Message, WriteFailure  # noqa: E402
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.loop_state import CONVERSATION_WRITE_FAILED, conversation_writer, flush_conversation_writes  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.protocol import ACTION_DISPATCH, ACTION_FINISH, TaskAction  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

REASON = "PermissionError: Permission denied"


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conversation_store, "_WRITE_RETRY_DELAYS", (0.0, 0.0))


def _disk_error(root: Path) -> OSError:
    """str() 带着文件的绝对路径，和真实的写盘失败一样。"""
    return OSError(errno.EACCES, "Permission denied", str(root / "data" / "conversations" / f"{SESSION_ID}.jsonl"))


class _BrokenDisk:
    """顶替 ConversationStore.append：每次都抛 OSError，记下每次尝试写的消息 id。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.attempts: list[str] = []

    def __call__(self, session_id: str, message: Message) -> None:
        self.attempts.append(message.id)
        raise _disk_error(self.root)

    @property
    def failed(self) -> list[str]:
        return list(dict.fromkeys(self.attempts))


def _flush(rctx: SimpleNamespace, messages: list[tuple[str, Message]]) -> None:
    async def run() -> None:
        writer = conversation_writer(rctx)
        for session_id, message in messages:
            writer.submit(session_id, message)
        await flush_conversation_writes(rctx)

    asyncio.run(run())


def _message(message_id: str) -> Message:
    return Message(id=message_id, role="user", content=message_id)


def _names_a_path(report: dict, root: Path) -> bool:
    text = " ".join(str(value) for value in report.values())
    return str(root) in text or ".jsonl" in text


def test_a_write_that_fails_once_lands_once_and_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConversationStore(tmp_path)
    original = store.append
    attempts: list[str] = []

    def flaky(session_id: str, message: Message) -> None:
        attempts.append(message.id)
        if len(attempts) == 1:
            raise _disk_error(tmp_path)
        original(session_id, message)

    monkeypatch.setattr(store, "append", flaky)
    events = EventRecorder()

    _flush(SimpleNamespace(conversation_store=store, emit_event=events, task_id=TASK_ID), [(SESSION_ID, _message("m1"))])

    assert attempts == ["m1", "m1"]
    assert [message.id for message in ConversationStore(tmp_path).load(SESSION_ID)] == ["m1"]
    assert events.events == []


class _Clock:
    """顶替 conversation_store 里的 time：sleep 只记下要等多久，不真等。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def test_after_one_message_runs_out_of_retries_the_next_ones_fail_fast_until_a_write_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conversation_store, "_WRITE_RETRY_DELAYS", (0.2, 1.0))
    clock = _Clock()
    monkeypatch.setattr(conversation_store, "time", clock)
    store = ConversationStore(tmp_path)
    original = store.append
    attempts: list[str] = []

    def disk(session_id: str, message: Message) -> None:
        attempts.append(message.id)
        if message.id.startswith("bad"):
            raise _disk_error(tmp_path)
        original(session_id, message)

    monkeypatch.setattr(store, "append", disk)
    writer = ConversationWriter(store)

    async def write(*message_ids: str) -> None:
        for message_id in message_ids:
            writer.submit(SESSION_ID, _message(message_id))
        await writer.flush()

    asyncio.run(write("bad1", "bad2", "bad3", "bad4", "bad5"))

    assert clock.slept == [0.2, 1.0] and sum(clock.slept) <= sum(conversation_store._WRITE_RETRY_DELAYS)
    assert attempts == ["bad1"] * 3 + ["bad2", "bad3", "bad4", "bad5"]

    asyncio.run(write("good", "bad6"))

    assert clock.slept == [0.2, 1.0, 0.2, 1.0]
    assert attempts[7:] == ["good", "bad6", "bad6", "bad6"]
    assert [message.id for message in ConversationStore(tmp_path).load(SESSION_ID)] == ["good"]
    assert [failure.message_id for failure in writer.take_failures()] == ["bad1", "bad2", "bad3", "bad4", "bad5", "bad6"]


def test_an_error_other_than_a_disk_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConversationStore(tmp_path)
    attempts: list[str] = []

    def unencodable(session_id: str, message: Message) -> None:
        attempts.append(message.id)
        raise ValueError("message cannot be encoded")

    monkeypatch.setattr(store, "append", unencodable)
    writer = ConversationWriter(store)

    async def run() -> list[WriteFailure]:
        writer.submit(SESSION_ID, _message("m1"))
        await writer.flush()
        return writer.take_failures()

    assert asyncio.run(run()) == [WriteFailure(SESSION_ID, "m1", "ValueError: message cannot be encoded")]
    assert attempts == ["m1"]


def test_a_write_that_keeps_failing_is_reported_once_and_the_task_keeps_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry())
    disk = _BrokenDisk(tmp_path)
    monkeypatch.setattr(run.rctx.conversation_store, "append", disk)

    action = run.run(turn(call("finish", {"text": "done"}, call_id="f")))

    assert action.action == ACTION_FINISH and action.result["text"] == "done"
    [report] = run.rctx.events.of(CONVERSATION_WRITE_FAILED)
    # 第一条重试完仍失败之后，后面的只试一次
    assert disk.failed and disk.attempts == [disk.failed[0]] * 3 + disk.failed[1:]
    assert report["failed_count"] == len(disk.failed) and report["message_ids"] == disk.failed
    assert report["session_id"] == SESSION_ID and report["task_id"] == TASK_ID
    assert report["reason"] == REASON
    assert report["message"] == f"对话历史有 {len(disk.failed)} 条没写进会话 {SESSION_ID}：{REASON}"
    assert not _names_a_path(report, tmp_path)


def test_the_event_lists_at_most_twenty_ids_but_counts_every_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConversationStore(tmp_path)
    monkeypatch.setattr(store, "append", _BrokenDisk(tmp_path))
    events = EventRecorder()
    ids = [f"m{index:02d}" for index in range(25)]

    _flush(SimpleNamespace(conversation_store=store, emit_event=events, task_id=TASK_ID), [(SESSION_ID, _message(i)) for i in ids])

    [report] = events.of(CONVERSATION_WRITE_FAILED)
    assert report["failed_count"] == 25 and report["message_ids"] == ids[:20]


def test_failures_in_two_sessions_are_reported_per_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConversationStore(tmp_path)
    monkeypatch.setattr(store, "append", _BrokenDisk(tmp_path))
    events = EventRecorder()

    _flush(
        SimpleNamespace(conversation_store=store, emit_event=events, task_id=TASK_ID),
        [("branch", _message("a")), ("child", _message("b")), ("branch", _message("c"))],
    )

    reports = {report["session_id"]: report for report in events.of(CONVERSATION_WRITE_FAILED)}
    assert {session: report["message_ids"] for session, report in reports.items()} == {"branch": ["a", "c"], "child": ["b"]}
    assert reports["branch"]["failed_count"] == 2 and reports["child"]["failed_count"] == 1


def test_failures_already_reported_are_not_reported_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConversationStore(tmp_path)
    monkeypatch.setattr(store, "append", _BrokenDisk(tmp_path))
    events = EventRecorder()
    rctx = SimpleNamespace(conversation_store=store, emit_event=events, task_id=TASK_ID)

    _flush(rctx, [(SESSION_ID, _message("first"))])
    _flush(rctx, [])
    _flush(rctx, [(SESSION_ID, _message("second"))])

    assert [report["message_ids"] for report in events.of(CONVERSATION_WRITE_FAILED)] == [["first"], ["second"]]


@pytest.mark.parametrize("answer", ["refused", "raised"])
def test_a_report_the_supervisor_did_not_take_is_logged_and_the_flush_still_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, answer: str,
) -> None:
    store = ConversationStore(tmp_path)
    monkeypatch.setattr(store, "append", _BrokenDisk(tmp_path))
    emit = AsyncMock(return_value=False) if answer == "refused" else AsyncMock(side_effect=RuntimeError("supervisor gone"))

    with caplog.at_level(logging.WARNING, logger="engine.inference.loop_state"):
        _flush(SimpleNamespace(conversation_store=store, emit_event=emit, task_id=TASK_ID), [(SESSION_ID, _message("m1"))])

    warnings = [record.getMessage() for record in caplog.records if record.name == "engine.inference.loop_state"]
    assert emit.await_count == 1
    assert any("was not reported to the supervisor" in text for text in warnings)
    assert all(str(tmp_path) not in text and ".jsonl" not in text for text in warnings)


def test_failures_are_reported_before_the_task_suspends_for_a_wait_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_hooks(monkeypatch)
    ls = make_loop_state(tmp_path, ScriptedRegistry())
    ls.allowed_dispatch_targets = {"scout"}
    disk = _BrokenDisk(tmp_path)
    monkeypatch.setattr(ls.rctx.conversation_store, "append", disk)
    wait = call("dispatch_to_scout", {"instruction": "look around", "wait": True}, call_id="w1")

    action = asyncio.run(_handle_tool_calls(ls, turn(wait), 1, final_step=False))

    assert action is not None and action.action == ACTION_DISPATCH
    [report] = ls.rctx.events.of(CONVERSATION_WRITE_FAILED)
    assert disk.failed and report["failed_count"] == len(disk.failed)
    assert report["message_ids"] == disk.failed and report["reason"] == REASON
    assert ls.rctx.conversation_writer.take_failures() == []


def test_failures_are_reported_when_the_task_ends_and_the_result_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead = Node(id="lead", type="ai", prompt="Work.", tool_mode="native")
    monkeypatch.setattr(runner, "load_node", lambda root, node_id: lead)
    monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(runner, "install_event_bridge", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "run_ai_node", AsyncMock(
        return_value=TaskAction(action="finish", node_id="lead", result={"text": "done"}),
    ))
    disk = _BrokenDisk(tmp_path)
    monkeypatch.setattr(ConversationStore, "append", lambda store, session_id, message: disk(session_id, message))
    posted: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async def run_task() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await runner._run_node_task(
                http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
                registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
                task={"task_id": "task-9", "node_id": "lead", "input": {"task_context": {}, "instruction": "hello"}},
                worker_id="worker", session_id="session-9", session_generation=1,
                api_key="test", base_url="http://model.invalid", default_model="test",
            )

    result = asyncio.run(run_task())

    assert result["action"] == "finish" and result["result"]["text"] == "done"
    [report] = [body["payload"] for body in posted if body["type"] == CONVERSATION_WRITE_FAILED]
    assert len(disk.failed) == 1 and len(disk.attempts) == 3
    assert report["failed_count"] == 1 and report["message_ids"] == disk.failed
    assert report["session_id"] == "session-9" and report["task_id"] == "task-9"
    assert not _names_a_path(report, tmp_path)
