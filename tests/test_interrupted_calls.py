"""重启打断时末尾那一轮缺结果的工具调用：补「中断，结果未知」，finish / ask 不补，伪工具不记执行状态，只补一次。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.interrupted_calls import (  # noqa: E402
    INTERRUPTED_MARK_KEY,
    INTERRUPTED_RESULT_TEXT,
    INTERRUPTED_TOOL_STATUS,
    append_interrupted_results,
    dangling_tail_calls,
    interrupted_results,
)

_TASK = "task-a"


def _user(text: str = "改一下配置", index: int = 0) -> Message:
    return Message(id=f"u{index}", role="user", content=text, message_type=MessageType.USER_INPUT)


def _assistant(*calls: dict[str, Any], index: int = 1, task_id: str = _TASK, meta: dict[str, Any] | None = None) -> Message:
    return Message(
        id=f"a{index}", role="assistant", content="", message_type=MessageType.ASSISTANT,
        meta=dict(meta or {"tool_mode": "native"}), source_node_id="node-1", source_task_id=task_id,
        tool_calls=list(calls),
    )


def _call(call_id: str, name: str) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": {}}


def _result(call_id: str, name: str, index: int = 2) -> Message:
    return Message(
        id=f"r{index}-{call_id}", role="tool", content="ok", message_type=MessageType.TOOL_RESULT,
        source_task_id=_TASK, tool_call_id=call_id, name=name,
    )


class TestDanglingCalls:
    def test_only_the_unanswered_calls_of_the_last_round_are_listed(self) -> None:
        messages = [
            _user(),
            _assistant(_call("c1", "read_file"), _call("c2", "execute_command"), _call("c3", "apply_diff")),
            _result("c1", "read_file"),
        ]

        assistant, missing = dangling_tail_calls(messages)

        assert assistant is messages[1]
        assert missing == [("c2", "execute_command"), ("c3", "apply_diff")]

    def test_openai_shaped_calls_are_read_too(self) -> None:
        messages = [_user(), _assistant({"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}})]

        assert dangling_tail_calls(messages)[1] == [("c1", "read_file")]

    @pytest.mark.parametrize("tail", [
        pytest.param([_user()], id="ends-with-a-user-message"),
        pytest.param([_user(), Message(id="a1", role="assistant", content="好了")], id="plain-reply"),
        pytest.param([], id="empty"),
    ])
    def test_a_history_not_ending_in_a_tool_round_has_nothing_dangling(self, tail: list[Message]) -> None:
        assert dangling_tail_calls(tail) == (None, [])

    def test_finish_and_ask_are_left_alone(self) -> None:
        messages = [_user(), _assistant(_call("f1", "finish"), _call("q1", "ask"), _call("c1", "read_file"))]

        assert dangling_tail_calls(messages)[1] == [("c1", "read_file")]

    def test_an_id_answered_in_an_earlier_round_does_not_count(self) -> None:
        messages = [
            _user(),
            _assistant(_call("c1", "read_file"), index=1),
            _result("c1", "read_file", index=2),
            _assistant(_call("c1", "read_file"), index=3),
        ]

        assert dangling_tail_calls(messages)[1] == [("c1", "read_file")]

    def test_a_repeated_id_needs_one_result_each(self) -> None:
        messages = [_user(), _assistant(_call("c1", "read_file"), _call("c1", "read_file")), _result("c1", "read_file")]

        assert dangling_tail_calls(messages)[1] == [("c1", "read_file")]

    def test_calls_without_an_id_or_a_name_are_skipped(self) -> None:
        messages = [_user(), _assistant({"id": "", "name": "read_file"}, {"id": "c2"}, "not a call", _call("c3", "glob"))]

        assert dangling_tail_calls(messages)[1] == [("c3", "glob")]

    def test_a_task_filter_ignores_rounds_of_other_tasks(self) -> None:
        messages = [_user(), _assistant(_call("c1", "read_file"), task_id="task-b")]

        assert dangling_tail_calls(messages, task_ids={_TASK}) == (None, [])
        assert dangling_tail_calls(messages, task_ids={"task-b"})[1] == [("c1", "read_file")]


class TestResults:
    def test_each_missing_call_gets_a_result_shaped_like_a_stored_one(self) -> None:
        meta = {"tool_mode": "native", "response_action": "reply", "reply_message_id": "m-9", "provider": "x"}
        messages = [_user(), _assistant(_call("c1", "read_file"), _call("c2", "execute_command"), meta=meta)]

        results = interrupted_results(messages, now="2026-03-01T12:00:00+00:00")

        assert [(item.tool_call_id, item.name) for item in results] == [("c1", "read_file"), ("c2", "execute_command")]
        first = results[0]
        assert first.role == "tool" and first.content == INTERRUPTED_RESULT_TEXT
        assert first.message_type == MessageType.TOOL_RESULT
        assert first.created_at == "2026-03-01T12:00:00+00:00"
        assert first.source_task_id == _TASK and first.source_node_id == "node-1"
        assert first.meta == {
            "response_action": "reply",
            "reply_message_id": "m-9",
            "tool_mode": "native",
            "message_type": MessageType.TOOL_RESULT,
            INTERRUPTED_MARK_KEY: True,
            "source_task_id": _TASK,
            "tool_status": INTERRUPTED_TOOL_STATUS,
        }
        assert len({item.id for item in results}) == 2

    def test_pseudo_tools_get_a_result_without_an_execution_status(self) -> None:
        messages = [_user(), _assistant(_call("r1", "reply"), _call("d1", "dispatch_to_helper"), _call("c1", "glob"))]

        results = {item.tool_call_id: item for item in interrupted_results(messages)}

        assert "tool_status" not in results["r1"].meta
        assert "tool_status" not in results["d1"].meta
        assert results["c1"].meta["tool_status"] == INTERRUPTED_TOOL_STATUS

    def test_the_tool_mode_defaults_when_the_round_did_not_record_one(self) -> None:
        messages = [_user(), _assistant(_call("c1", "glob"), meta={"provider": "x"})]

        assert interrupted_results(messages)[0].meta["tool_mode"] == "fake-native"

    def test_nothing_is_made_when_nothing_is_missing(self) -> None:
        messages = [_user(), _assistant(_call("c1", "glob")), _result("c1", "glob")]

        assert interrupted_results(messages) == []


class TestAppend:
    def test_the_results_are_stored_once(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "conversations")
        store.append_batch("s-1", [_user(), _assistant(_call("c1", "read_file"), _call("c2", "glob")), _result("c1", "read_file")])

        added = append_interrupted_results(store, "s-1")
        again = append_interrupted_results(store, "s-1")

        assert [item.tool_call_id for item in added] == ["c2"]
        assert again == []
        stored = store.load("s-1")
        assert [message.tool_call_id for message in stored if message.role == "tool"] == ["c1", "c2"]
        assert stored[-1].meta[INTERRUPTED_MARK_KEY] is True

    def test_a_task_filter_is_passed_through(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "conversations")
        store.append_batch("s-1", [_user(), _assistant(_call("c1", "read_file"), task_id="task-b")])

        assert append_interrupted_results(store, "s-1", task_ids={_TASK}) == []
        assert len(store.load("s-1")) == 2

    @pytest.mark.parametrize("session_id", ["", "   ", None])
    def test_no_session_means_nothing_to_do(self, tmp_path: Path, session_id: Any) -> None:
        assert append_interrupted_results(ConversationStore(tmp_path / "conversations"), session_id) == []
