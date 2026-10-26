"""工具输出进事件时的体积：tool_call_end 和工具任务快照只带摘录，WS 推送超限时截副本、不动事件日志里的那份。

进上下文的工具结果不受影响，那边由 engine 落盘、给带 ref 的预览。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import ScriptedRegistry, call, make_loop_state, real_calls, use_hooks  # noqa: E402
from engine.inference.ai_step import _execute_real_tools  # noqa: E402
from engine.tool_outputs import (  # noqa: E402
    EVENT_RESULT_CHARS,
    EVENT_TEXT_CHARS,
    bounded_for_event,
    tool_result_for_event,
)
from supervisor import api as api_module  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

# 摘录在行边界上切，中间那行说明和换行会让长度略超预算，留一点余量
_SLACK = 200


def _lines(count: int, width: int = 60) -> str:
    return "".join(f"{index:06d} {'x' * width}\n" for index in range(count))


@pytest.fixture(autouse=True)
def _isolated_hooks(monkeypatch: Any) -> None:
    use_hooks(monkeypatch)


class TestBoundedForEvent:
    def test_a_value_within_the_limit_is_returned_as_is(self) -> None:
        value = {"data": {"result": "short", "items": [1, "two", None]}, "ok": True}

        bounded, clipped = bounded_for_event(value)

        assert bounded is value and clipped is False

    def test_a_long_string_becomes_an_excerpt_of_head_and_tail(self) -> None:
        text = _lines(5000)

        bounded, clipped = bounded_for_event(text)

        assert clipped is True
        assert len(bounded) <= EVENT_TEXT_CHARS + _SLACK
        assert bounded.startswith("000000 ") and bounded.rstrip().endswith("x" * 60)
        assert "004999 " in bounded

    def test_long_strings_are_cut_wherever_they_sit_and_the_rest_is_kept(self) -> None:
        long_text = "y" * (EVENT_TEXT_CHARS * 3)
        value = {"data": {"result": long_text, "rows": [{"cell": long_text}, ("keep", long_text)], "count": 3}}

        bounded, clipped = bounded_for_event(value)

        assert clipped is True
        assert bounded["data"]["count"] == 3
        assert len(bounded["data"]["result"]) <= EVENT_TEXT_CHARS + _SLACK
        assert len(bounded["data"]["rows"][0]["cell"]) <= EVENT_TEXT_CHARS + _SLACK
        assert isinstance(bounded["data"]["rows"][1], tuple) and bounded["data"]["rows"][1][0] == "keep"
        assert value["data"]["result"] == long_text

    def test_nesting_past_the_depth_limit_is_judged_as_text(self) -> None:
        deep: Any = "z" * (EVENT_TEXT_CHARS * 2)
        for _ in range(40):
            deep = [deep]

        bounded, clipped = bounded_for_event(deep)

        assert clipped is True
        assert len(json.dumps(bounded, ensure_ascii=False)) < EVENT_TEXT_CHARS * 2


class TestToolResultForEvent:
    def test_a_normal_result_is_unchanged(self) -> None:
        result = {"ok": True, "data": {"result": "returncode=0\nfine", "returncode": 0}}

        assert tool_result_for_event(result) == (result, False)

    def test_a_long_result_text_keeps_the_structure_around_it(self) -> None:
        text = _lines(4000)
        result = {"ok": True, "data": {"result": f"returncode=0\n{text}", "output": text, "returncode": 0}}

        bounded, clipped = tool_result_for_event(result)

        assert clipped is True
        assert bounded["ok"] is True and bounded["data"]["returncode"] == 0
        assert len(bounded["data"]["result"]) <= EVENT_TEXT_CHARS + _SLACK
        assert len(bounded["data"]["output"]) <= EVENT_TEXT_CHARS + _SLACK

    def test_many_small_entries_leave_only_the_text_and_the_attachments(self) -> None:
        entries = [{"name": f"node_modules/pkg{index}/index.js", "type": "file"} for index in range(20000)]
        result = {
            "ok": True,
            "success": True,
            "data": {
                "result": "20000 files",
                "results": [{"path": ".", "entries": entries}],
                "attachments": [{"path": "out.png"}],
            },
        }

        bounded, clipped = tool_result_for_event(result)

        assert clipped is True
        assert bounded == {
            "ok": True,
            "success": True,
            "data": {"result": "20000 files", "attachments": [{"path": "out.png"}], "omittedForEvent": True},
        }
        assert len(json.dumps(bounded)) <= EVENT_RESULT_CHARS

    def test_an_attachment_list_too_large_for_the_event_is_left_out(self) -> None:
        attachments = [{"path": f"out/{index:06d}.png", "kind": "image"} for index in range(5000)]
        result = {"ok": True, "data": {"result": "drew 5000 images", "attachments": attachments}}

        bounded, _clipped = tool_result_for_event(result)

        assert bounded["data"] == {"result": "drew 5000 images", "omittedForEvent": True}

    def test_a_failure_keeps_its_error(self) -> None:
        result = {"ok": False, "error": "e" * (EVENT_TEXT_CHARS * 2), "data": {"result": "ERROR: boom", "rows": ["r" * 50] * 5000}}

        bounded, _clipped = tool_result_for_event(result)

        assert bounded["ok"] is False
        assert bounded["error"].startswith("eee") and len(bounded["error"]) <= EVENT_TEXT_CHARS + _SLACK
        assert bounded["data"]["result"] == "ERROR: boom"


class TestToolCallEndEvent:
    def test_the_event_carries_an_excerpt_while_the_context_entry_keeps_everything(self, tmp_path: Path) -> None:
        text = _lines(20000)

        async def big(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"result": text, "output": text}}

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"big": big}))

        asyncio.run(_execute_real_tools(ls, real_calls(call("big", {}, call_id="b")), step=1))

        [end] = ls.rctx.events.of("tool_call_end")
        assert end["truncated_for_event"] is True
        assert len(end["raw_inline"]) <= EVENT_TEXT_CHARS + _SLACK
        assert len(end["result"]["data"]["result"]) <= EVENT_TEXT_CHARS + _SLACK
        assert len(json.dumps(end, ensure_ascii=False)) < len(text)

    def test_a_small_result_is_sent_untouched_and_unmarked(self, tmp_path: Path) -> None:
        async def small(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"result": "fine"}}

        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"small": small}))

        asyncio.run(_execute_real_tools(ls, real_calls(call("small", {}, call_id="s")), step=1))

        [end] = ls.rctx.events.of("tool_call_end")
        assert "truncated_for_event" not in end
        assert end["result"] == {"ok": True, "data": {"result": "fine"}}


def _state(tmp_path: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="event-bounds"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state._defer_post_work_enqueue = True
    return state


def _task(kind: TaskKind, result: dict[str, Any]) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        task_id=f"{kind.value}-1", session_id="s1", kind=kind, tool_name="execute_command" if kind == TaskKind.tool else None,
        node_id=None if kind == TaskKind.tool else "chat", status=TaskStatus.completed, created_at=now, updated_at=now,
        result=result,
    )


class TestTaskSnapshots:
    def test_a_tool_task_snapshot_stores_an_excerpt_and_the_live_task_keeps_everything(self, tmp_path: Path) -> None:
        text = _lines(20000)
        task = _task(TaskKind.tool, {"summary": "ran", "text": text, "raw_inline": text, "tool_ok": True})
        state = _state(tmp_path)

        payload = state._event_payload_for_task(task, event_type="task_completed")

        assert payload["result"]["_event_text_clipped"] is True
        assert payload["result"]["summary"] == "ran" and payload["result"]["tool_ok"] is True
        assert len(payload["result"]["text"]) <= EVENT_TEXT_CHARS + _SLACK
        assert len(payload["result"]["raw_inline"]) <= EVENT_TEXT_CHARS + _SLACK
        assert task.result["text"] == text

    def test_a_replayed_tool_task_gets_the_excerpt(self, tmp_path: Path) -> None:
        text = _lines(20000)
        task = _task(TaskKind.tool, {"text": text, "raw_inline": text})
        state = _state(tmp_path)
        payload = state._event_payload_for_task(task, event_type="task_completed")

        state._apply_task_snapshot(payload)

        replayed = state.tasks[task.task_id]
        assert replayed.result["_event_text_clipped"] is True
        assert len(replayed.result["text"]) <= EVENT_TEXT_CHARS + _SLACK

    def test_a_small_tool_task_and_a_node_task_are_stored_whole(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        small = _task(TaskKind.tool, {"text": "fine", "raw_inline": "fine"})
        answer = "a" * (EVENT_TEXT_CHARS * 2)
        node = _task(TaskKind.node, {"text": answer})

        assert state._event_payload_for_task(small, event_type="task_completed")["result"] == small.result
        assert state._event_payload_for_task(node, event_type="task_completed")["result"]["text"] == answer


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _tool_end_event(result_text: str, raw: str) -> dict[str, Any]:
    return {
        "seq": 7,
        "type": "tool_call_end",
        "payload": {"tool_name": "execute_command", "result": {"ok": True, "data": {"result": result_text}}, "raw_inline": raw},
    }


class TestWebSocketSend:
    def test_an_oversized_event_is_clipped_on_a_copy(self) -> None:
        event = _tool_end_event("r" * 50_000, "w" * 50_000)
        before = json.dumps(event)
        socket = _Socket()

        asyncio.run(api_module._send_ws_json(socket, event))

        [sent] = socket.sent
        assert len(sent.encode("utf-8")) <= api_module._WS_MAX_EVENT_BYTES
        assert json.dumps(event) == before
        assert json.loads(sent)["payload"]["tool_name"] == "execute_command"

    def test_result_is_clipped_first_and_raw_inline_only_when_still_needed(self) -> None:
        socket = _Socket()

        asyncio.run(api_module._send_ws_json(socket, _tool_end_event("r" * 80_000, "short preview")))

        payload = json.loads(socket.sent[0])["payload"]
        assert payload["result"].endswith("[truncated for WS]")
        assert payload["raw_inline"] == "short preview"

    def test_the_cap_counts_bytes_not_characters(self) -> None:
        # 3 万个汉字只有 3 万个字符，UTF-8 却是 9 万字节，超了上限
        socket = _Socket()

        asyncio.run(api_module._send_ws_json(socket, _tool_end_event("汉" * 30_000, "")))

        assert len(socket.sent[0].encode("utf-8")) <= api_module._WS_MAX_EVENT_BYTES

    def test_a_small_event_is_sent_unchanged(self) -> None:
        event = _tool_end_event("fine", "fine")
        socket = _Socket()

        asyncio.run(api_module._send_ws_json(socket, event))

        assert json.loads(socket.sent[0]) == event
