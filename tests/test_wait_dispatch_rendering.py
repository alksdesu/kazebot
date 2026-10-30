"""恢复后批次结果怎么交给模型：每个子节点带上调用、指令和结局，失败、取消、打断都明说没有结果；落盘用的消息类型和旧数据都兼容。

中间层被停下时，那一批按同样的写法补进它自己的历史。
"""
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
    stopped_batch_messages,
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

        assert message["content"].splitlines() == [
            "子节点 scout（调用 w1；指令：list the files）被打断，没有做完，也没有结果。",
            "打断前的摘要：read half of it",
            "它的会话没有为接着做留着；要做完得重新委派。",
        ]
        assert resume_outcome(message) == ("child_preempted", "scout")

    def test_a_child_that_asks_back_hands_over_its_question(self) -> None:
        [message] = _render(_node(status="ask", text="要列哪个目录？"))

        assert message["content"].splitlines() == [
            "子节点 scout（调用 w1；指令：list the files）没有做完，需要补充信息：",
            "要列哪个目录？",
            "它这一程已经结束，会话没有为接着做留着；补齐信息后要重新委派，那是从零开始的另一份。",
        ]
        assert resume_outcome(message) == ("child_ask", "scout")

    def test_a_kept_asking_child_says_how_to_pick_up_where_it_stopped(self) -> None:
        [message] = _render(_node(status="ask", text="要列哪个目录？", handle="child-1@child_abc", continuable=True))

        assert message["content"].splitlines() == [
            "子节点 scout（调用 w1；指令：list the files）没有做完，需要补充信息：",
            "要列哪个目录？",
            "句柄：child-1@child_abc",
            "补齐它要的信息后，再委派给 scout，带上 continue_from 填这个句柄，它会从停下的地方接着做；不带句柄就是从零开始的另一份。",
        ]
        assert message["_meta"]["handle"] == "child-1@child_abc"
        assert resume_outcome(message) == ("child_ask", "scout")

    def test_an_asking_child_whose_session_was_not_kept_still_shows_its_handle(self) -> None:
        [message] = _render(_node(status="ask", text="要列哪个目录？", handle="child-1@child_abc", continuable=False))

        assert message["content"].splitlines()[-2:] == [
            "它这一程已经结束，会话没有为接着做留着；补齐信息后要重新委派，那是从零开始的另一份。",
            "句柄：child-1@child_abc",
        ]

    def test_a_kept_preempted_child_says_how_to_continue(self) -> None:
        [message] = _render(_node(
            status="preempted", text="", summary="read half of it", handle="child-1@child_abc", continuable=True,
        ))

        assert message["content"].splitlines() == [
            "子节点 scout（调用 w1；指令：list the files）被打断，没有做完，也没有结果。",
            "打断前的摘要：read half of it",
            "句柄：child-1@child_abc",
            "它的会话为接着做留着；要它从打断的地方接着做，就再委派给 scout，带上 continue_from 填这个句柄。",
        ]

    def test_a_finished_child_whose_session_stays_says_it_can_carry_on_with_its_history(self) -> None:
        [message] = _render(_node(handle="child-1@child_abc", continuable=True))

        assert message["content"].endswith(
            "结果：\na.py\nb.py\n句柄：child-1@child_abc\n"
            "它的会话留着；要它带着这段历史接着做，就再委派给 scout，带上 continue_from 填这个句柄。"
        )

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

    def test_a_cancelled_tool_says_there_is_no_result_and_why(self) -> None:
        with_reason, without_reason = _render(
            {"kind": "tool", "name": "probe", "status": "cancelled", "error": "上级任务已失败"},
            {"kind": "tool", "name": "probe", "status": "cancelled", "raw_inline": "stale output"},
        )

        assert with_reason["content"] == 'Tool "probe" 已取消，没有结果。原因：上级任务已失败'
        assert without_reason["content"] == 'Tool "probe" 已取消，没有结果。'
        assert with_reason["_meta"] == {"message_type": "tool_result"}

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
        assert "只写其中几个就只发那几个" in text_part
        assert "data/attachments/x.png" in text_part


class TestLifetimeCap:
    def test_items_cut_short_by_the_callers_lifetime_get_one_note_at_the_end(self) -> None:
        messages = _render(
            _node(status="fail", text="", error="调用方的总运行时长快到上限", timed_out=True, lifetime_capped=True),
            _node(call_id="w2", node_id="helper"),
            {"kind": "tool", "name": "probe", "status": "fail", "error": "x", "timed_out": True, "lifetime_capped": True},
        )

        assert len(messages) == 4
        assert messages[-1]["content"] == (
            "[SYSTEM] 这一批有 2 项是因为本任务的总运行时长快到上限被提前收的；剩下的时间不多，先把已有的结果整理交回。"
        )
        assert messages[-1]["_meta"] == {"message_type": "system"}

    def test_an_item_that_ran_out_of_its_own_timeout_adds_no_note(self) -> None:
        messages = _render(_node(status="fail", text="", error="等待超时：30 秒内没有做完，已被停下。", timed_out=True))

        assert len(messages) == 1


class TestStoppedBatch:
    def test_what_came_back_and_what_was_called_off_go_in_with_a_closing_note(self) -> None:
        attachment = {"type": "image", "path": "data/attachments/x.png", "mime_type": "image/png"}

        finished, cancelled, note = stopped_batch_messages(
            [
                _node(attachments=[attachment]),
                _node(call_id="w2", status="cancelled", text="", error="它当时还没交回结果，已被一并叫停。"),
            ],
            reason="等待超时：60 秒内没有做完，已被停下。",
        )

        assert finished["content"].startswith("子节点 scout（调用 w1；指令：list the files）已完成。")
        assert finished["_meta"]["message_type"] == DISPATCH_RESULT_TYPE
        assert cancelled["content"].endswith("原因：它当时还没交回结果，已被一并叫停。")
        assert note["_meta"] == {"message_type": "system"}
        assert note["content"].splitlines() == [
            "[SYSTEM] 这一轮在等上面这批子任务时被停下了：等待超时：60 秒内没有做完，已被停下。",
            "已经交回的结果照上面写的；没交回的已被叫停，不会再有结果。接着做时以这些为准，需要的话重新派。",
            "子任务留下的附件：data/attachments/x.png。要发出去，就在 finish 的 attachment_paths 里点名。",
        ]

    def test_a_batch_that_never_went_out_says_so(self) -> None:
        node, tool, note = stopped_batch_messages(
            [
                {"kind": "node", "node_id": "scout", "instruction": "go", "status": "cancelled",
                 "error": "这一项还没派出去。", "call_id": "w1"},
                {"kind": "tool", "name": "probe", "status": "cancelled", "error": "这一项还没派出去。"},
            ],
            reason="交回的委派还没派出去就被取消了",
            unsent=True,
        )

        assert node["content"] == "子节点 scout（调用 w1；指令：go）已被取消，没有结果。\n原因：这一项还没派出去。"
        assert tool["content"] == 'Tool "probe" 已取消，没有结果。原因：这一项还没派出去。'
        assert note["content"].splitlines() == [
            "[SYSTEM] 这一轮交回了上面这批委派，还没派出去就被停下了：交回的委派还没派出去就被取消了。",
            "这些子任务一个都没有派出，不会有结果。接着做时以这些为准，需要的话重新派。",
        ]

    def test_an_isolated_copy_is_handed_on_rather_than_merged(self) -> None:
        worktree = {
            "id": "wt-1", "handle": "child-1@child_abc", "path": "/tmp/wt-1", "file_count": 1,
            "files": [{"path": "a.py", "status": "M", "added": 1, "deleted": 0}],
        }

        finished, _note = stopped_batch_messages([_node(worktree=worktree)], reason="任务已取消")

        assert "这个节点不能调用 merge_worktree" in finished["content"]

    def test_without_a_reason_the_note_still_reads(self) -> None:
        [note] = stopped_batch_messages([], reason="")

        assert note["content"].splitlines()[0] == "[SYSTEM] 这一轮在等上面这批子任务时被停下了。"


class TestSingleChildResumes:
    @pytest.mark.parametrize("rtype", ["child_result", "child_failed", "child_cancelled", "child_ask", "child_preempted"])
    def test_single_child_messages_are_stored_as_dispatch_results(self, rtype: str) -> None:
        [message] = _build_resume_messages({"type": rtype, "from_node": "scout", "result": {"text": "x"}})

        assert message["_meta"]["message_type"] == DISPATCH_RESULT_TYPE
        assert message["_meta"]["child_node_id"] == "scout"
        assert resume_outcome(message) == (rtype, "scout")

    def test_a_single_child_that_asks_is_not_reported_as_done(self) -> None:
        # 单个委派的 ask 和 finish 走同一条路交回，只靠 action_type 分开
        [message] = _build_resume_messages({
            "type": "child_result", "action_type": "ask", "child_node_id": "scout",
            "result": {"text": "要列哪个目录？"}, "handle": "child-1@child_abc", "continuable": True,
        })

        assert message["content"].splitlines() == [
            "下游节点 scout 需要补充信息：要列哪个目录？",
            "句柄：child-1@child_abc",
            "补齐它要的信息后，再委派给 scout，带上 continue_from 填这个句柄，它会从停下的地方接着做；不带句柄就是从零开始的另一份。",
        ]
        assert resume_outcome(message) == ("child_ask", "scout")

    def test_a_single_child_that_asks_without_a_kept_session_says_to_dispatch_again(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_result", "action_type": "ask", "child_node_id": "scout", "result": {"text": "要列哪个目录？"},
        })

        assert message["content"] == (
            "下游节点 scout 需要补充信息：要列哪个目录？\n"
            "它这一程已经结束，会话没有为接着做留着；补齐信息后要重新委派，那是从零开始的另一份。"
        )

    def test_a_single_finish_is_still_a_result(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_result", "action_type": "finish", "child_node_id": "scout", "result": {"text": "a.py"},
        })

        assert message["content"] == "下游节点 scout 已完成。\n结果：\na.py"
        assert resume_outcome(message) == ("child_result", "scout")

    def test_a_single_preempted_child_kept_for_continuing(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_preempted", "child_node_id": "scout", "context_ref": "ctx-1",
            "handle": "child-1@child_abc", "continuable": True,
        })

        assert message["content"].splitlines() == [
            "下游节点 scout 被打断，没有做完，也没有结果。",
            "句柄：child-1@child_abc",
            "它的会话为接着做留着；要它从打断的地方接着做，就再委派给 scout，带上 continue_from 填这个句柄。",
        ]
        assert "ctx-1" not in message["content"]

    def test_a_single_preempted_child_that_was_not_kept_says_to_dispatch_again(self) -> None:
        [message] = _build_resume_messages({"type": "child_preempted", "child_node_id": "scout", "context_ref": "ctx-1"})

        assert message["content"] == "下游节点 scout 被打断，没有做完，也没有结果。\n它的会话没有为接着做留着；要做完得重新委派。"

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
