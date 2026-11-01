"""新消息那一轮的动态区：supervisor 给的在跑任务摘要桥接进 task_context，渲染成固定的一段；
没有时提示词一字不变，知识注入重建时带着它，打断注入新消息时不再带这份过时的列表。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference.message_assembly import assemble_initial_messages, assemble_messages_with_injections  # noqa: E402
from engine.builtin.knowledge_inject import _rebuild_prompt_messages  # noqa: E402
from engine.builtin.preempt import _inject_preempt_message  # noqa: E402
from engine.concurrent_tasks import (  # noqa: E402
    ACTIVE_TASKS_INPUT_KEY,
    ACTIVE_TASKS_OMITTED_INPUT_KEY,
    CONCURRENT_CLOSE,
    CONCURRENT_CONTEXT_KEY,
    CONCURRENT_OPEN,
    bridge_input,
    concurrent_parts,
)
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.runner import _build_task_context  # noqa: E402

_ROUND = "6f1c2d3e-0000-4000-8000-000000000001"
_CHILD = "6f1c2d3e-0000-4000-8000-000000000002"
_PERSONA = {"role": "system", "content": "persona"}
_NODE_TURN = {"role": "system", "content": "node turn notes"}
_SKILL_TURN = {"role": "system", "content": "[SKILL:ACTIVE]\nhow\n[/SKILL:ACTIVE]"}
_MEMORY_TURN = {"role": "system", "content": "[MEMORY:ACTIVE]\nrecent\n[/MEMORY:ACTIVE]"}
_HISTORY = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
_PROMPTS = {
    "string": [_PERSONA, _NODE_TURN],
    "block": [_PERSONA, {"role": "user", "content": "fixed rules"}, {"role": "history"}],
}


def _item(task_id: str = _ROUND, **fields: Any) -> dict[str, Any]:
    return {"task_id": task_id, "node_id": "qq.orchestrator", "status": "running", "steerable": True, "age_sec": 125, **fields}


def _value(*items: dict[str, Any], omitted: int = 0) -> dict[str, Any]:
    return {"tasks": list(items), **({"omitted": omitted} if omitted else {})}


def _block(value: Any) -> str:
    [part] = concurrent_parts(value)
    return part


def _dynamic(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message["content"]) for message in messages if message.get("_dynamic"))


def _assemble(workspace: Path, mode: str, **kwargs: Any) -> list[dict[str, Any]]:
    messages, _ = assemble_messages_with_injections(
        workspace_root=workspace, system_prompt=[dict(message) for message in _PROMPTS[mode]],
        history=list(_HISTORY), instruction="now", skill_dynamic=[_SKILL_TURN], memory_dynamic=[_MEMORY_TURN],
        **kwargs,
    )
    return messages


class TestBridge:
    def test_without_running_tasks_nothing_is_bridged(self) -> None:
        assert bridge_input({}) == {}
        assert bridge_input({ACTIVE_TASKS_INPUT_KEY: []}) == {}
        assert bridge_input({ACTIVE_TASKS_INPUT_KEY: "not a list"}) == {}

    def test_listed_tasks_and_the_count_left_out_are_carried(self) -> None:
        raw = {ACTIVE_TASKS_INPUT_KEY: [_item(), "junk", {"node_id": "no id"}], ACTIVE_TASKS_OMITTED_INPUT_KEY: 3}

        assert bridge_input(raw) == {CONCURRENT_CONTEXT_KEY: _value(_item(), omitted=3)}

    def test_a_resumed_run_gets_no_stale_list(self) -> None:
        raw = {ACTIVE_TASKS_INPUT_KEY: [_item()], "resume_data": {"type": "child_result"}}

        assert bridge_input(raw) == {}

    def test_the_runner_puts_it_into_the_task_context(self) -> None:
        context = _build_task_context({ACTIVE_TASKS_INPUT_KEY: [_item()], "task_context": {"channel": "qq_group"}})

        assert context[CONCURRENT_CONTEXT_KEY] == _value(_item())
        assert context["channel"] == "qq_group"


class TestRendering:
    def test_a_running_round_says_what_it_is_doing_and_how_to_steer_it(self) -> None:
        block = _block(_value(_item(instruction="小明（UserA）: 查天气", phase="tool_call", detail="web_search")))

        assert block.startswith(f"{CONCURRENT_OPEN}\n")
        assert block.endswith(CONCURRENT_CLOSE)
        assert f"- 「小明（UserA）: 查天气」｜qq.orchestrator｜在调用工具 web_search｜已跑 2 分 5 秒｜可追加，编号 {_ROUND}" in block
        assert "preempt_task" in block

    @pytest.mark.parametrize("fields, shown", [
        ({"status": "pending", "steerable": True, "age_sec": 3}, "qq.orchestrator｜排队中｜已等 3 秒"),
        ({"status": "suspended", "steerable": False, "phase": "tool_call"}, "qq.orchestrator｜挂起等子任务｜已跑 2 分 5 秒"),
        ({"phase": "thinking", "detail": "ignored"}, "qq.orchestrator｜在思考｜已跑"),
        ({"phase": "awaiting_approval", "detail": "write_file"}, "在等审批 write_file"),
        ({"age_sec": 3661, "steerable": False}, "已跑 1 小时 1 分"),
        ({"mine": False}, "｜不是你发起的｜"),
    ], ids=["pending", "suspended", "thinking", "approval", "hours", "not-mine"])
    def test_each_state_reads_naturally(self, fields: dict[str, Any], shown: str) -> None:
        block = _block(_value(_item(**fields)))

        assert shown in block
        if fields.get("steerable") is False:
            assert "可追加，编号" not in block

    def test_children_hang_under_the_round_that_sent_them(self) -> None:
        orphan = _item("6f1c2d3e-0000-4000-8000-000000000003", node_id="carol", parent_task_id="gone")
        block = _block(_value(_item(), orphan, _item(_CHILD, node_id="bob", parent_task_id=_ROUND)))

        lines = block.splitlines()
        assert lines[1].startswith("- qq.orchestrator")
        assert lines[2].startswith("  - bob")
        assert lines[3].startswith("- carol")

    def test_a_parent_loop_does_not_lose_anything(self) -> None:
        looped = [_item(_ROUND, parent_task_id=_CHILD), _item(_CHILD, node_id="bob", parent_task_id=_ROUND)]

        block = _block(_value(*looped))

        assert block.count("可追加，编号") == 2
        assert _ROUND in block and _CHILD in block

    def test_the_count_left_out_is_said(self) -> None:
        assert "（还有 2 个没列出来）" in _block(_value(_item(), omitted=2))

    @pytest.mark.parametrize("value", [None, {}, {"tasks": []}, {"tasks": [{"node_id": "x"}]}, "tasks"])
    def test_nothing_to_list_renders_nothing(self, value: Any) -> None:
        assert concurrent_parts(value) == []


class TestAssembly:
    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_without_it_the_prompt_is_unchanged(self, tmp_path: Path, mode: str) -> None:
        plain = _assemble(tmp_path, mode)

        for raw in (None, {}, {"tasks": []}):
            assert _assemble(tmp_path, mode, concurrent=raw) == plain

    def test_in_string_mode_it_follows_the_isolation_notice_and_precedes_skills(self, tmp_path: Path) -> None:
        turn_block = _dynamic(_assemble(
            tmp_path, "string", concurrent=_value(_item()),
            isolation={"mode": "worktree", "worktree": "wt-1", "path": str(tmp_path), "origin": str(tmp_path)},
        ))

        assert turn_block.index("node turn notes") < turn_block.index("[ISOLATION]") < turn_block.index(CONCURRENT_OPEN)
        assert turn_block.index(CONCURRENT_CLOSE) < turn_block.index("[SKILL:ACTIVE]") < turn_block.index("[MEMORY:ACTIVE]")

    def test_in_block_mode_it_opens_the_turn_block(self, tmp_path: Path) -> None:
        turn_block = _dynamic(_assemble(tmp_path, "block", concurrent=_value(_item())))

        assert turn_block.startswith(f"以下是本轮动态上下文，每轮可能变化。\n\n{CONCURRENT_OPEN}\n")

    @pytest.mark.parametrize("mode", list(_PROMPTS))
    def test_it_never_touches_the_cached_prefix(self, tmp_path: Path, mode: str) -> None:
        plain = _assemble(tmp_path, mode)
        listed = _assemble(tmp_path, mode, concurrent=_value(_item()))

        assert [message for message in listed if not message.get("_dynamic")] == [
            message for message in plain if not message.get("_dynamic")
        ]

    def test_the_skeleton_reads_it_from_the_task_context(self, tmp_path: Path) -> None:
        messages, _, _ = assemble_initial_messages(
            workspace_root=tmp_path, runtime_cfg={}, node=Node(id="qq.orchestrator", type="ai", prompt="persona"),
            instruction="now", history=[], task_context={CONCURRENT_CONTEXT_KEY: _value(_item())},
        )

        assert CONCURRENT_OPEN in _dynamic(messages)

    def test_the_knowledge_rebuild_keeps_it(self, tmp_path: Path) -> None:
        ctx = SimpleNamespace(
            messages=[], rctx=SimpleNamespace(workspace_root=tmp_path, task_context={CONCURRENT_CONTEXT_KEY: _value(_item())}),
            extra={"system_prompt": [_PERSONA, _NODE_TURN], "history": list(_HISTORY), "instruction_text": "now"},
        )

        asyncio.run(_rebuild_prompt_messages(ctx))

        assert CONCURRENT_OPEN in _dynamic(ctx.messages)

    def test_an_interruption_drops_the_stale_list(self, tmp_path: Path) -> None:
        history = [{"role": "user", "content": "old request"}]
        loop = SimpleNamespace(
            node=Node(id="qq.orchestrator", type="ai"), runtime_cfg={}, preempt_inject_info={"message": "follow-up"},
            rctx=SimpleNamespace(
                workspace_root=tmp_path, task_context={CONCURRENT_CONTEXT_KEY: _value(_item())}, session_id="branch",
                child_session_id="", task_id="task",
                conversation_store=ConversationStore(tmp_path / "data" / "conversations"),
                consume_preempt=AsyncMock(), emit_event=AsyncMock(),
            ),
            messages=[dict(_PERSONA), *history, {"role": "user", "content": _block(_value(_item())), "_dynamic": True}],
            history=history, system_prompt=[_PERSONA, _NODE_TURN], is_block_mode=False,
        )

        asyncio.run(_inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop))

        assert CONCURRENT_OPEN not in _dynamic(loop.messages)
        assert "node turn notes" in _dynamic(loop.messages)
        assert loop.messages[-1]["content"] == "follow-up"
