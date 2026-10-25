"""同步压缩失败过一次，本任务内不再挂起当轮去压；阈值按实际发给模型的那份截短视图估。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.builtin import compact
from engine.compact import should_compact


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compact, "_HARD_COMPACT_FAILED_TASKS", OrderedDict())
    monkeypatch.setattr(compact, "_persist_ctx", lambda *_args: "ctx")


def _history() -> list[dict]:
    messages = []
    for number in range(6):
        messages.append({"role": "user", "content": f"question {number} " + "x" * 300, "_meta": {"source_task_id": f"t{number}"}})
        messages.append({"role": "assistant", "content": f"answer {number} " + "y" * 300, "_meta": {"source_task_id": f"t{number}"}})
    return messages


def _state(tmp_path: Path, messages: list[dict], *, resume: dict | None = None, task_id: str = "task-1", **sizes: int):
    response = SimpleNamespace(content=b"{}", raise_for_status=lambda: None, json=lambda: {"task_id": "background-1"})
    rctx = SimpleNamespace(
        workspace_root=tmp_path, session_id="session", parent_session_id="", child_session_id="",
        conversation_store=None, emit_event=AsyncMock(), task_id=task_id, supervisor_url="http://sup",
        http=SimpleNamespace(post=AsyncMock(return_value=response)),
    )
    state = SimpleNamespace(
        messages=messages, rctx=rctx, node=SimpleNamespace(id="entry"), compacted=False,
        compact_threshold=sizes.get("soft", 10), compact_hard_threshold=sizes.get("hard", 20),
        last_prompt_tokens=None, compact_keep_recent=3, compact_keep_recent_tokens=0,
        compact_async_requested=False, resume_data=resume,
    )
    return SimpleNamespace(messages=messages, extra={}, step=1), state


_FAILED = {"type": "compact_done", "success": False, "before": 0, "after": 0}


def test_after_a_failed_compaction_the_turn_is_not_suspended_again(tmp_path: Path) -> None:
    ctx, state = _state(tmp_path, _history(), resume=_FAILED)

    compact._note_compact_outcome(state)
    action = asyncio.run(compact._check_and_compact(ctx, state))

    assert state.hard_compact_disabled is True
    assert action is None
    url = state.rctx.http.post.await_args.args[0]
    assert url == "http://sup/v1/tasks/compact-async"


def test_the_failure_is_remembered_for_later_loop_states_of_the_same_task(tmp_path: Path) -> None:
    _ctx, first = _state(tmp_path, _history(), resume=_FAILED)
    compact._note_compact_outcome(first)

    ctx, later = _state(tmp_path, _history(), resume={"type": "child_result"})
    compact._note_compact_outcome(later)

    assert later.hard_compact_disabled is True
    assert asyncio.run(compact._check_and_compact(ctx, later)) is None

    _ctx, other_task = _state(tmp_path, _history(), task_id="task-2")
    compact._note_compact_outcome(other_task)
    assert getattr(other_task, "hard_compact_disabled", False) is False


def test_a_successful_compaction_keeps_the_hard_threshold_armed(tmp_path: Path) -> None:
    ctx, state = _state(tmp_path, _history(), resume={"type": "compact_done", "success": True})

    compact._note_compact_outcome(state)
    action = asyncio.run(compact._check_and_compact(ctx, state))

    assert getattr(state, "hard_compact_disabled", False) is False
    assert action is not None and action.target_node == "system.compactor"


def test_the_threshold_is_estimated_on_the_view_the_model_actually_receives(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "read the log", "_meta": {"source_task_id": "t0"}},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read-1", "name": "read_file", "arguments": {}}],
         "_meta": {"source_task_id": "t0"}},
        {"role": "tool", "tool_call_id": "read-1", "name": "read_file", "content": "z" * 60_000,
         "_meta": {"source_task_id": "t0", "message_type": "tool_result"}},
    ]
    ctx, state = _state(tmp_path, messages, soft=5_000, hard=6_000)

    assert should_compact(messages, 5_000) is True
    assert asyncio.run(compact._check_and_compact(ctx, state)) is None
    state.rctx.http.post.assert_not_awaited()
