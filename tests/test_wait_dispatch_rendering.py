"""恢复后批次结果怎么交给模型：每个子节点带上调用、指令和结局，失败、取消、打断都明说没有结果；落盘用的消息类型和旧数据都兼容。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.finish_guard import _has_unresolved_vision_failure  # noqa: E402
from engine.inference.loop_state import TOOL_RESULT_ATTACHMENT_TYPE  # noqa: E402
from engine.inference.resume_builder import (  # noqa: E402
    DISPATCH_RESULT_TYPE,
    _build_resume_messages,
    resume_outcome,
)


def _node(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "node", "node_id": "scout", "status": "finish", "instruction": "list the files",
        "text": "a.py\nb.py", "summary": "", "call_id": "w1", "child_task_id": "child-1",
        "child_session_id": "child_abc",
    }
    entry.update(fields)
    return entry


def _render(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return _build_resume_messages({"type": "batch_results", "entries": list(entries)})


class TestNodeEntries:
    def test_a_finished_child_names_its_call_instruction_and_result(self) -> None:
        [message] = _render(_node(summary="two files"))

        assert message["role"] == "user"
        assert message["content"] == (
            "子节点 scout（调用 w1；指令：list the files）已完成。\n摘要：two files\n结果：\na.py\nb.py"
        )
        assert message["_meta"] == {
            "message_type": DISPATCH_RESULT_TYPE,
            "child_node_id": "scout",
            "resume_type": "child_result",
            "resume_from_node": "scout",
            "summary": "two files",
            "child_task_id": "child-1",
            "child_session_id": "child_abc",
        }

    def test_a_finished_child_without_output_says_so(self) -> None:
        [message] = _render(_node(text="", summary=""))

        assert message["content"].endswith("它没有返回结果文本。")

    def test_a_failed_child_says_there_is_no_result(self) -> None:
        [message] = _render(_node(status="fail", text="", error="timeout"))

        assert message["content"] == (
            "子节点 scout（调用 w1；指令：list the files）执行失败：timeout\n它没有给出结果，不要臆测它的输出。"
        )
        assert resume_outcome(message) == ("child_failed", "scout")

    def test_a_failure_without_a_reason_still_reads_as_a_failure(self) -> None:
        [message] = _render(_node(status="fail", text="", error=""))

        assert "执行失败：未知错误" in message["content"]

    def test_a_cancelled_child_says_there_is_no_result_and_why(self) -> None:
        [message] = _render(_node(status="cancelled", text="", error="上级任务已失败"))

        assert message["content"] == "子节点 scout（调用 w1；指令：list the files）已被取消，没有结果。\n原因：上级任务已失败"
        assert resume_outcome(message) == ("child_cancelled", "scout")

    def test_a_preempted_child_says_it_did_not_finish(self) -> None:
        [message] = _render(_node(status="preempted", text="", summary="read half of it"))

        assert message["content"] == (
            "子节点 scout（调用 w1；指令：list the files）被打断，没有做完，也没有结果。\n打断前的摘要：read half of it"
        )
        assert resume_outcome(message) == ("child_preempted", "scout")

    def test_a_child_that_asks_back_hands_over_its_question(self) -> None:
        [message] = _render(_node(status="ask", text="要列哪个目录？"))

        assert message["content"].splitlines() == [
            "子节点 scout（调用 w1；指令：list the files）没有做完，需要补充信息：",
            "要列哪个目录？",
            "它这一程已经结束；补齐信息后要重新委派。",
        ]
        assert resume_outcome(message) == ("child_ask", "scout")

    def test_an_unexpected_status_is_not_reported_as_done(self) -> None:
        [message] = _render(_node(status="dispatch", text="", error=""))

        assert message["content"] == "子节点 scout（调用 w1；指令：list the files）结束了，但没有拿到结果（状态：dispatch）。"
        assert resume_outcome(message) == ("", "")

    def test_a_long_instruction_is_cut_and_flattened(self) -> None:
        instruction = "第一行\n" + "长" * 300

        [message] = _render(_node(instruction=instruction))

        first_line = message["content"].splitlines()[0]
        assert "\n" not in first_line and "第一行 长" in first_line
        assert first_line.endswith("…）已完成。")
        assert len(first_line) < 260


class TestOldEntries:
    def test_an_entry_from_before_call_ids_still_renders(self) -> None:
        [message] = _render({"kind": "node", "node_id": "scout", "status": "finish", "instruction": "go", "text": "ok"})

        assert message["content"] == "子节点 scout（指令：go）已完成。\n结果：\nok"
        assert message["_meta"]["child_node_id"] == "scout"
        assert "child_task_id" not in message["_meta"]

    def test_an_entry_with_almost_nothing_still_renders(self) -> None:
        [message] = _render({"kind": "node"})

        assert message["content"] == "子节点 unknown 已完成。\n它没有返回结果文本。"

    def test_tool_entries_keep_their_old_shape(self) -> None:
        ok, failed = _render(
            {"kind": "tool", "name": "probe", "status": "finish", "raw_inline": "fine"},
            {"kind": "tool", "name": "probe", "status": "fail", "error": "boom"},
        )

        assert ok["content"] == 'Tool result for "probe":\nfine'
        assert failed["content"] == 'Tool "probe" 执行失败：boom'
        assert ok["_meta"] == {"message_type": "tool_result"}
        assert resume_outcome(ok) == ("", "")

    def test_non_object_entries_are_skipped(self) -> None:
        messages = _render("junk", _node())  # type: ignore[arg-type]

        assert len(messages) == 1


class TestAttachments:
    def test_attachments_come_last_and_say_they_go_out_with_finish(self) -> None:
        attachment = {"type": "image", "path": "data/attachments/x.png", "mime_type": "image/png"}

        node_message, attachments_message = _render(_node(attachments=[attachment]))

        assert node_message["content"].startswith("子节点 scout")
        assert attachments_message["_meta"] == {"message_type": TOOL_RESULT_ATTACHMENT_TYPE}
        text_part = attachments_message["content"][0]["text"]
        assert "finish 时不写 attachment_paths" in text_part
        assert "data/attachments/x.png" in text_part


class TestSingleChildResumes:
    @pytest.mark.parametrize("rtype", ["child_result", "child_failed", "child_cancelled", "child_ask", "child_preempted"])
    def test_single_child_messages_are_stored_as_dispatch_results(self, rtype: str) -> None:
        [message] = _build_resume_messages({"type": rtype, "from_node": "scout", "result": {"text": "x"}})

        assert message["_meta"]["message_type"] == DISPATCH_RESULT_TYPE
        assert message["_meta"]["child_node_id"] == "scout"
        assert resume_outcome(message) == (rtype, "scout")

    def test_the_old_tool_results_shape_is_typed_as_tool_output(self) -> None:
        [message] = _build_resume_messages({"type": "tool_results", "tool_results": [{"name": "probe", "raw_inline": "ok"}]})

        assert message["content"] == 'Tool result for "probe":\nok'
        assert message["_meta"] == {"message_type": "tool_result"}

    def test_compact_done_still_adds_nothing(self) -> None:
        assert _build_resume_messages({"type": "compact_done", "success": True}) == []


class TestVisionGuard:
    def test_a_failed_vision_child_in_a_batch_is_caught(self) -> None:
        messages = [{"role": "user", "content": "看看这张图"}, *_render(_node(node_id="vision.reader", status="fail", text="", error="429"))]

        assert _has_unresolved_vision_failure(messages, vision_nodes=frozenset({"vision.reader"})) is True

    def test_a_finished_vision_child_in_a_batch_clears_it(self) -> None:
        messages = [
            {"role": "user", "content": "看看这张图"},
            *_render(_node(node_id="vision.reader", status="fail", text="", error="429")),
            *_render(_node(node_id="vision.reader", text="一只猫")),
        ]

        assert _has_unresolved_vision_failure(messages, vision_nodes=frozenset({"vision.reader"})) is False
