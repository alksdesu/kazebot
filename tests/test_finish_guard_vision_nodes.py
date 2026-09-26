"""读图失败判定认结构化的下游结果，视觉节点集合来自 routing.vision。

以前写死 `qq.vision` 并匹配中文正文，换个节点名这道防线就静默失效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin.finish_guard import (  # noqa: E402
    _has_unresolved_vision_failure,
    vision_node_ids,
)
from engine.inference.resume_builder import _build_resume_messages, resume_outcome  # noqa: E402

_RUNTIME = {
    "routing": {
        "vision": {
            "entry_node_id": "vision.reader",
            "channels": {"tg_group": {"entry_node_id": "vision.tg"}, "qq_group": {}},
        }
    }
}


def _turn(*resumes: dict) -> list[dict]:
    messages = [{"role": "user", "content": "看看这张图"}]
    for resume in resumes:
        messages.extend(_build_resume_messages(resume))
    return messages


def test_vision_nodes_come_from_routing() -> None:
    assert vision_node_ids(_RUNTIME) == frozenset({"vision.reader", "vision.tg"})
    assert vision_node_ids({}) == frozenset()
    assert vision_node_ids(None) == frozenset()


@pytest.mark.parametrize("rtype", ["child_result", "child_failed", "child_cancelled", "child_ask", "child_preempted"])
def test_child_resumes_carry_their_outcome(rtype: str) -> None:
    messages = _build_resume_messages({"type": rtype, "from_node": "vision.reader", "result": {"text": "x"}})
    assert messages
    assert all(resume_outcome(message) == (rtype, "vision.reader") for message in messages)


def test_tool_resumes_are_not_tagged_as_child_outcomes() -> None:
    messages = _build_resume_messages({"type": "tool_results", "tool_results": [{"name": "read_image", "raw_inline": "ok"}]})
    assert all(resume_outcome(message) == ("", "") for message in messages)


def test_a_renamed_vision_node_failure_is_caught() -> None:
    messages = _turn({"type": "child_failed", "from_node": "vision.reader", "error": "timeout"})
    assert _has_unresolved_vision_failure(messages, vision_nodes=vision_node_ids(_RUNTIME)) is True


def test_a_renamed_vision_node_success_clears_it() -> None:
    messages = _turn(
        {"type": "child_failed", "from_node": "vision.tg", "error": "timeout"},
        {"type": "child_result", "from_node": "vision.tg", "result": {"text": "一只猫"}},
    )
    assert _has_unresolved_vision_failure(messages, vision_nodes=vision_node_ids(_RUNTIME)) is False


def test_other_nodes_failing_is_not_a_vision_failure() -> None:
    messages = _turn({"type": "child_failed", "from_node": "draw.image_gen", "error": "quota"})
    assert _has_unresolved_vision_failure(messages, vision_nodes=vision_node_ids(_RUNTIME)) is False


def test_restored_messages_without_meta_still_match_by_text() -> None:
    messages = [
        {"role": "user", "content": "看看这张图"},
        {"role": "user", "content": "下游节点 vision.reader 执行失败：timeout"},
    ]
    assert _has_unresolved_vision_failure(messages, vision_nodes=vision_node_ids(_RUNTIME)) is True


def test_the_old_hard_coded_name_no_longer_matches_when_not_configured() -> None:
    messages = [
        {"role": "user", "content": "看看这张图"},
        {"role": "user", "content": "下游节点 qq.vision 执行失败：timeout"},
    ]
    assert _has_unresolved_vision_failure(messages, vision_nodes=vision_node_ids(_RUNTIME)) is False
    assert _has_unresolved_vision_failure(messages, vision_nodes=frozenset({"qq.vision"})) is True
