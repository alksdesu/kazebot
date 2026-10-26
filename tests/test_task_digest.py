"""任务内进度摘要：触发条件、按轮切分不拆配对、开头指令保留原文、指纹对不上就不用、落盘后恢复、
生成失败退回清单、增量摘要，以及供应商报上下文溢出时先摘要再重试。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _path in (_ROOT, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _engine_loop_fixture import ScriptedRegistry, make_loop_state  # noqa: E402
from engine.builtin import compact  # noqa: E402
from engine.compact import estimate_message_tokens, total_content_chars  # noqa: E402
from engine.context_store import cleanup_old_contexts  # noqa: E402
from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.inference.llm_call import _call_llm_with_retry, request_view  # noqa: E402
from engine.inference.tool_format import (  # noqa: E402
    build_llm_messages,
    create_tool_formatter,
    repair_tool_result_pairing_with_stats,
)
from engine.node import Node  # noqa: E402
from engine.protocol import ACTION_CANCELLED, ACTION_DISPATCH, TaskAction  # noqa: E402
from engine.runner import _message_to_history_dict  # noqa: E402
from engine.task_digest import (  # noqa: E402
    DIGEST_MESSAGE_TYPE,
    DIGEST_PREFIX,
    OVERFLOW_REASON,
    THRESHOLD_REASON,
    DigestOutcome,
    TaskDigest,
    digest_path,
    digest_settings,
    load_task_digest,
    plan_task_digest,
    restore_task_digest,
    run_task_digest,
    save_task_digest,
    task_layout,
)
from providers.base import ProviderResponse  # noqa: E402

TASK = "task-1"
INSTRUCTION = "把 engine/compact.py 里的截断改成开头和结尾都留"
NARRATIVE = "1. 目标：截断改成头尾都留。\n2. 已完成：读了 engine/compact.py，跑过测试。\n6. 下一步：改 _bounded_content。"


def _meta(**extra: Any) -> dict[str, Any]:
    return {"source_task_id": TASK, **extra}


def _call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": dict(arguments or {})}


def _assistant(*calls: dict[str, Any], text: str = "") -> dict[str, Any]:
    return {
        "role": "assistant", "content": text, "tool_calls": list(calls),
        "_meta": _meta(tool_mode="native", message_type="assistant"),
    }


def _result(call_id: str, name: str, content: str, status: str = "success") -> dict[str, Any]:
    return {
        "role": "tool", "tool_call_id": call_id, "name": name, "content": content,
        "_meta": _meta(tool_mode="native", message_type="tool_result", tool_status=status),
    }


def _round(call_id: str, name: str, arguments: dict[str, Any], content: str, status: str = "success") -> list[dict[str, Any]]:
    return [_assistant(_call(call_id, name, arguments)), _result(call_id, name, content, status)]


def _command_round(number: int, chars: int = 1000) -> list[dict[str, Any]]:
    return _round(f"cmd-{number}", "execute_command", {"command": f"pytest -k case{number}"}, f"run {number}\n" + "o" * chars)


def _prefix() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "earlier question", "_meta": {"source_task_id": "old"}},
        {"role": "assistant", "content": "earlier answer", "_meta": {"source_task_id": "old"}},
        {"role": "user", "content": "以下是本轮动态上下文", "_dynamic": True},
        {"role": "user", "content": INSTRUCTION, "_meta": _meta(message_type="user_input")},
    ]


def _history(rounds: int, chars: int = 1000) -> list[dict[str, Any]]:
    messages = _prefix()
    for number in range(rounds):
        messages += _command_round(number, chars)
    return messages


def _loop(tmp_path: Path, messages: list[dict[str, Any]], **fields: Any) -> SimpleNamespace:
    rctx = SimpleNamespace(task_id=TASK, workspace_root=tmp_path, emit_event=AsyncMock(), total_usage={})
    state = SimpleNamespace(
        messages=messages, rctx=rctx, node=SimpleNamespace(id="worker"), runtime_cfg={}, compact_threshold=0,
        last_prompt_chars=0, last_prompt_tokens=None, task_digest=None, formatter=None,
    )
    for key, value in fields.items():
        setattr(state, key, value)
    return state


class _Writer:
    """顶替摘要请求：按顺序给出回复，每次请求的消息留一份。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> Any:
        self.requests.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _digest(loop: SimpleNamespace, writer: _Writer, *, keep_tokens: int = 0, trigger_tokens: int = 0) -> DigestOutcome:
    return asyncio.run(run_task_digest(
        loop, step=3, view=list(loop.messages), chars_per_token=1.0, trigger_tokens=trigger_tokens,
        keep_tokens=keep_tokens, call=writer, reason=THRESHOLD_REASON,
    ))


def _unit_tokens(messages: list[dict[str, Any]], indices: tuple[int, ...]) -> int:
    return sum(estimate_message_tokens(messages[index], 1.0) for index in indices)


class TestLayout:
    def test_rounds_stay_whole_and_the_instruction_is_set_apart(self) -> None:
        messages = _prefix() + [
            _assistant(_call("a", "read_file", {"path": "x.py"}), _call("b", "execute_command", {"command": "ls"})),
            _result("a", "read_file", "── x.py ── 共 3 行"),
            _result("b", "execute_command", "files"),
            {"role": "user", "content": [{"type": "text", "text": "以上工具执行产生了以下图片结果："}],
             "_meta": _meta(message_type="tool_result_attachment")},
            {"role": "user", "content": "顺便把测试也补上", "_meta": _meta(message_type="user_input")},
            {"role": "user", "content": "你上一条回复被截断了", "_retry_hint": True},
            *_command_round(1),
        ]

        layout = task_layout(messages, TASK)

        assert layout.instruction == 4
        assert [unit.indices for unit in layout.units] == [(5, 6, 7, 8), (9,), (11, 12)]
        assert [unit.tool_round for unit in layout.units] == [True, False, True]
        assert all(unit.complete for unit in layout.units)

    def test_a_round_missing_a_result_is_incomplete(self) -> None:
        messages = _prefix() + [
            _assistant(_call("a", "read_file", {"path": "x.py"}), _call("b", "execute_command", {"command": "ls"})),
            _result("a", "read_file", "── x.py ── 共 3 行"),
        ]

        layout = task_layout(messages, TASK)

        assert [unit.indices for unit in layout.units] == [(5, 6)]
        assert layout.units[0].complete is False

    def test_other_tasks_and_runtime_only_messages_are_not_part_of_the_task(self) -> None:
        layout = task_layout(_history(2), TASK)

        assert layout.instruction == 4
        assert [unit.indices for unit in layout.units] == [(5, 6), (7, 8)]
        assert task_layout(_history(2), "someone-else").units == ()


class TestPlanning:
    def test_nothing_happens_at_or_below_the_trigger(self) -> None:
        messages = _history(6)
        layout = task_layout(messages, TASK)
        total = estimate_message_tokens(messages[4], 1.0) + sum(_unit_tokens(messages, unit.indices) for unit in layout.units)

        at_trigger = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0,
            trigger_tokens=total, keep_tokens=0,
        )
        over_trigger = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0,
            trigger_tokens=total - 1, keep_tokens=0,
        )

        assert at_trigger is None
        assert over_trigger is not None and over_trigger.total_tokens == total

    def test_the_oldest_rounds_outside_the_keep_window_are_planned(self) -> None:
        messages = _history(6)
        unit = _unit_tokens(messages, task_layout(messages, TASK).units[-1].indices)

        plan = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0,
            trigger_tokens=0, keep_tokens=unit * 2,
        )

        assert (plan.covered_units, plan.target_units) == (0, 4)

    def test_the_newest_round_is_always_kept(self) -> None:
        messages = _history(6)

        plan = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0, trigger_tokens=0, keep_tokens=0,
        )

        assert plan.target_units == 5

    def test_nothing_is_planned_when_everything_fits_the_keep_window(self) -> None:
        messages = _history(6)

        assert plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0,
            trigger_tokens=0, keep_tokens=10**9,
        ) is None
        assert plan_task_digest(
            _history(1), _history(1), task_id=TASK, digest=None, chars_per_token=1.0, trigger_tokens=0, keep_tokens=0,
        ) is None

    def test_an_incomplete_round_stops_the_coverage(self) -> None:
        messages = _history(6)
        del messages[10]

        plan = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=None, chars_per_token=1.0, trigger_tokens=0, keep_tokens=0,
        )

        assert plan.target_units == 2

    def test_a_view_that_does_not_line_up_is_refused(self) -> None:
        messages = _history(6)

        assert plan_task_digest(
            messages, messages[:-1], task_id=TASK, digest=None, chars_per_token=1.0, trigger_tokens=0, keep_tokens=0,
        ) is None


class TestGeneration:
    def test_the_covered_rounds_are_replaced_in_the_request_view(self, tmp_path: Path) -> None:
        messages = _history(6)
        loop = _loop(tmp_path, messages)
        writer = _Writer(ProviderResponse(ok=True, text=NARRATIVE, usage={"prompt_tokens": 50, "completion_tokens": 20}))

        assert _digest(loop, writer) is DigestOutcome.CREATED

        digest = loop.task_digest
        assert digest.covered_rounds == 5 and digest.generated is True
        assert digest.text.startswith(DIGEST_PREFIX) and NARRATIVE in digest.text
        view = request_view(messages, loop)
        assert len(view) == 8
        assert view[:5] == messages[:5]
        assert view[5]["content"] == digest.text
        assert view[5]["_meta"]["message_type"] == DIGEST_MESSAGE_TYPE
        assert view[6:] == messages[15:]
        assert len(messages) == 17
        _, stats = repair_tool_result_pairing_with_stats(view)
        assert stats["removed_orphan_tool_results"] == 0 and stats["fixed_orphan_tool_calls"] == 0
        replayed = build_llm_messages(view, create_tool_formatter("native"))
        assert [message.get("tool_call_id") for message in replayed if message.get("role") == "tool"] == ["cmd-5"]

    def test_the_event_usage_and_file_are_recorded(self, tmp_path: Path) -> None:
        messages = _history(6)
        loop = _loop(tmp_path, messages)
        writer = _Writer(ProviderResponse(ok=True, text=NARRATIVE, usage={"prompt_tokens": 50, "completion_tokens": 20}))

        _digest(loop, writer)

        event, payload = loop.rctx.emit_event.await_args.args
        assert event == "task_digest"
        assert payload["reason"] == THRESHOLD_REASON and payload["step"] == 3
        assert payload["covered_rounds"] == 5
        assert payload["covered_chars"] == total_content_chars(messages[5:15])
        assert payload["generated"] is True and payload["incremental"] is False
        assert loop.rctx.total_usage == {"prompt_tokens": 50, "completion_tokens": 20}
        assert load_task_digest(tmp_path, TASK) == loop.task_digest

    def test_the_request_shows_the_instruction_and_only_the_covered_rounds(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, _history(6))
        writer = _Writer(ProviderResponse(ok=True, text=NARRATIVE))

        _digest(loop, writer)

        system, user = writer.requests[0]
        assert system["role"] == "system" and "进度摘要" in system["content"]
        assert user["role"] == "user"
        assert f"【任务指令】\n{INSTRUCTION}" in user["content"]
        for number in range(5):
            assert f"pytest -k case{number}" in user["content"]
        assert "pytest -k case5" not in user["content"]
        assert "── 第 1 轮 ──" in user["content"] and "── 第 5 轮 ──" in user["content"]
        assert "【上一份进展摘要】" not in user["content"]

    def test_the_lists_come_from_the_tool_calls_and_survive_a_failed_request(self, tmp_path: Path) -> None:
        long_command = "echo " + "y" * 300 + "\nsecond line"
        messages = _prefix() + [
            *_round("r1", "read_file", {"files": [{"path": "engine/a.py", "startLine": 1, "endLine": 80}]},
                    "── engine/a.py ── 文件共 300 行，显示 1-80 行\n   1 | x"),
            *_round("w1", "write_file", {"path": "engine/a.py", "content": "new"}, "ok"),
            *_round("w2", "apply_diff", {"path": "engine/b.py", "diffs": []}, "ERROR: no match", status="error"),
            *_round("c1", "execute_command", {"command": "make test"}, "FAILED", status="error"),
            *_round("c2", "execute_command", {"command": long_command}, "ok"),
            *_round("c3", "execute_command", {"command": "ls"}, "ok"),
        ]
        loop = _loop(tmp_path, messages)

        _digest(loop, _Writer(ProviderResponse(ok=False, error="upstream down", status_code=502)))

        digest = loop.task_digest
        assert digest.generated is False
        assert "（这次没能生成文字摘要" in digest.text
        assert "【读过的文件】\n- engine/a.py（第 1–80 行）" in digest.text
        changed = digest.text.split("【改过的文件】")[1].split("【执行过的命令】")[0]
        assert "- engine/a.py（write_file）" in changed and "engine/b.py" not in changed
        assert "- make test（出错）" in digest.text
        assert "- echo " + "y" * 195 + "…" in digest.text
        assert "- ls" not in digest.text

    def test_an_exception_from_the_request_also_falls_back_to_the_lists(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, _history(6))

        assert _digest(loop, _Writer(RuntimeError("connection reset"))) is DigestOutcome.CREATED

        assert loop.task_digest.generated is False
        assert "【执行过的命令】" in loop.task_digest.text

    def test_a_cancelled_request_leaves_nothing_behind(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, _history(6))

        assert _digest(loop, _Writer(None)) is DigestOutcome.CANCELLED

        assert loop.task_digest is None
        assert not digest_path(tmp_path, TASK).exists()
        loop.rctx.emit_event.assert_not_awaited()

    def test_a_digest_that_no_longer_matches_is_not_applied(self, tmp_path: Path) -> None:
        messages = _history(6)
        loop = _loop(tmp_path, messages)
        _digest(loop, _Writer(ProviderResponse(ok=True, text=NARRATIVE)))

        messages[6] = {**messages[6], "content": "changed after the digest"}
        view = request_view(messages, loop)

        assert len(view) == len(messages)
        assert all(message.get("_meta", {}).get("message_type") != DIGEST_MESSAGE_TYPE for message in view)


class TestIncrementalDigest:
    def test_a_new_digest_covers_more_and_builds_on_the_previous_one(self, tmp_path: Path) -> None:
        messages = _history(4)
        loop = _loop(tmp_path, messages)
        writer = _Writer(ProviderResponse(ok=True, text="第一份摘要"), ProviderResponse(ok=True, text="第二份摘要"))
        _digest(loop, writer)
        first = loop.task_digest

        for number in range(4, 7):
            messages += _command_round(number)
        assert _digest(loop, writer) is DigestOutcome.CREATED

        second = loop.task_digest
        assert (first.covered_rounds, second.covered_rounds) == (3, 6)
        assert second.fingerprints[:len(first.fingerprints)] == first.fingerprints
        request = writer.requests[1][1]["content"]
        assert "【上一份进展摘要】" in request and "第一份摘要" in request
        new_steps = request.split("【新完成的步骤】")[1]
        assert "pytest -k case3" in new_steps and "pytest -k case5" in new_steps
        assert "pytest -k case2" not in new_steps and "pytest -k case6" not in new_steps
        assert "── 第 4 轮 ──" in new_steps
        assert loop.rctx.emit_event.await_args.args[1]["incremental"] is True
        assert request_view(messages, loop)[5]["content"] == second.text

    def test_without_new_material_nothing_is_regenerated(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, _history(4))
        writer = _Writer(ProviderResponse(ok=True, text="第一份摘要"))
        _digest(loop, writer)

        assert _digest(loop, writer) is DigestOutcome.SKIPPED
        assert len(writer.requests) == 1

    def test_a_new_digest_waits_for_the_remaining_raw_text_to_cross_the_trigger(self, tmp_path: Path) -> None:
        messages = _history(6)
        loop = _loop(tmp_path, messages)
        _digest(loop, _Writer(ProviderResponse(ok=True, text="短摘要")))
        messages += _command_round(6)
        layout = task_layout(messages, TASK)
        remaining = estimate_message_tokens(messages[4], 1.0) + sum(
            _unit_tokens(messages, unit.indices) for unit in layout.units[5:]
        )

        at_line = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=loop.task_digest, chars_per_token=1.0,
            trigger_tokens=remaining, keep_tokens=0,
        )
        over_line = plan_task_digest(
            messages, list(messages), task_id=TASK, digest=loop.task_digest, chars_per_token=1.0,
            trigger_tokens=remaining - 1, keep_tokens=0,
        )

        assert at_line is None
        assert (over_line.covered_units, over_line.target_units, over_line.total_tokens) == (5, 6, remaining)


class TestPersistence:
    def _reload(self, tmp_path: Path, runtime: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """照 _shadow_write 落盘、照 runner 重载，得到任务恢复时新 loop state 里的消息。"""
        store = ConversationStore(tmp_path / "data" / "conversations")
        stored = []
        for message in runtime[1:]:
            if message.get("_dynamic"):
                continue
            meta = copy.deepcopy(message.get("_meta") or {})
            stored.append(Message(
                id=uuid4().hex, role=message["role"], content=copy.deepcopy(message["content"]),
                message_type=str(meta.get("message_type") or ""), created_at="2026-10-26T00:00:00+00:00",
                meta=meta, source_node_id="worker", source_task_id=str(meta.get("source_task_id") or ""),
                tool_calls=copy.deepcopy(message.get("tool_calls") or []),
                tool_call_id=str(message.get("tool_call_id") or ""), name=str(message.get("name") or ""),
            ))
        store.append_batch("session", stored)
        history = [_message_to_history_dict(item, current_task_id=TASK) for item in store.load("session")]
        return [{"role": "system", "content": "sys"}, *history, {"role": "user", "content": "动态上下文", "_dynamic": True}]

    def test_a_resumed_task_gets_its_digest_back(self, tmp_path: Path) -> None:
        runtime = _history(6)
        first = _loop(tmp_path, runtime)
        _digest(first, _Writer(ProviderResponse(ok=True, text=NARRATIVE)))
        resumed = self._reload(tmp_path, runtime)
        second = _loop(tmp_path, resumed)

        restored = asyncio.run(restore_task_digest(second))

        assert restored == first.task_digest and second.task_digest == restored
        view = request_view(resumed, second)
        assert view[3]["content"] == INSTRUCTION
        assert view[4]["content"] == restored.text
        assert [message.get("tool_call_id") for message in view if message.get("role") == "tool"] == ["cmd-5"]

    def test_a_digest_for_changed_history_is_not_restored(self, tmp_path: Path) -> None:
        runtime = _history(6)
        first = _loop(tmp_path, runtime)
        _digest(first, _Writer(ProviderResponse(ok=True, text=NARRATIVE)))
        resumed = self._reload(tmp_path, runtime)
        resumed[5] = {**resumed[5], "content": "the result changed"}
        second = _loop(tmp_path, resumed)

        assert asyncio.run(restore_task_digest(second)) is None
        assert second.task_digest is None

    def test_a_damaged_or_foreign_file_is_ignored(self, tmp_path: Path) -> None:
        path = digest_path(tmp_path, TASK)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        assert load_task_digest(tmp_path, TASK) is None

        path.write_text(json.dumps({"version": 1, "task_id": "other", "text": "x", "fingerprints": ["f"],
                                    "covered_units": 1, "covered_rounds": 1, "covered_chars": 1}), encoding="utf-8")
        assert load_task_digest(tmp_path, TASK) is None

    def test_the_file_round_trips(self, tmp_path: Path) -> None:
        digest = TaskDigest(
            task_id=TASK, text="[本任务前面步骤的进展摘要]\n…", narrative="…", fingerprints=("a", "b"),
            covered_units=2, covered_rounds=2, covered_chars=100, generated=True, created_at="2026-10-26T00:00:00+00:00",
        )

        save_task_digest(tmp_path, digest)

        assert load_task_digest(tmp_path, TASK) == digest
        assert not list(digest_path(tmp_path, TASK).parent.glob(".*.tmp"))

    def test_the_file_name_cannot_leave_the_digest_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "data" / "node_contexts" / "task_digests"

        path = digest_path(tmp_path, TASK)
        assert path.parent.parent == directory and path.name == f"{TASK}.json"
        escaped = digest_path(tmp_path, "../../etc/passwd")
        assert escaped.parent.parent == directory
        assert "/" not in escaped.name and "\\" not in escaped.name and not escaped.name.startswith(".")

    def test_the_hourly_snapshot_cleanup_does_not_reach_the_digests(self, tmp_path: Path) -> None:
        digest = TaskDigest(
            task_id=TASK, text="[本任务前面步骤的进展摘要]\n…", narrative="", fingerprints=("a",),
            covered_units=1, covered_rounds=1, covered_chars=10, generated=False, created_at="",
        )
        path = save_task_digest(tmp_path, digest)
        stale = time.time() - 2 * 3600
        os.utime(path, (stale, stale))

        assert cleanup_old_contexts(tmp_path, max_age_seconds=3600.0) == 0
        assert load_task_digest(tmp_path, TASK) == digest


class TestSettings:
    def test_defaults_and_clamping(self) -> None:
        defaults = digest_settings({})
        clamped = digest_settings({"engine": {"compact": {"task_digest_trigger_ratio": 3, "task_keep_recent_tokens": -5}}})
        off = digest_settings({"engine": {"compact": {"task_digest_trigger_ratio": 0}}})

        assert (defaults.trigger_ratio, defaults.keep_recent_tokens) == (0.5, 20_000)
        assert (clamped.trigger_ratio, clamped.keep_recent_tokens) == (0.95, 0)
        assert off.enabled is False and off.trigger_tokens(100_000) == 0

    def test_the_keep_window_stays_below_half_the_trigger(self) -> None:
        settings = digest_settings({})

        assert settings.trigger_tokens(100_000) == 50_000
        assert settings.keep_tokens(100_000) == 20_000
        assert settings.keep_tokens(10_000) == 2_500
        assert settings.trigger_tokens(0) == 0 and settings.keep_tokens(0) == 20_000


class _Provider:
    name = "test"
    model = "test"

    def __init__(self, *responses: ProviderResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, Any]], Any]] = []

    async def chat(self, *, messages: list[dict[str, Any]], tools: Any) -> ProviderResponse:
        self.calls.append((copy.deepcopy(messages), tools))
        return self.responses.pop(0)


def _checker_loop(tmp_path: Path, **fields: Any) -> SimpleNamespace:
    loop = _loop(tmp_path, _history(6), compact_threshold=100_000, compacted=True, preempt_after_step=False)
    for key, value in fields.items():
        setattr(loop, key, value)
    return loop


def _checker_ctx(loop: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(messages=loop.messages, extra={"loop_state": loop, "step_count": 0}, step=1)


class TestCompactChecker:
    def test_no_digest_when_the_step_suspends_for_compaction(self, tmp_path: Path, monkeypatch) -> None:
        dispatch = TaskAction(action=ACTION_DISPATCH, node_id="worker", target_node="system.compactor")
        monkeypatch.setattr(compact, "_check_and_compact", AsyncMock(return_value=dispatch))
        digest = AsyncMock(return_value=DigestOutcome.CREATED)
        monkeypatch.setattr(compact, "run_task_digest", digest)
        loop = _checker_loop(tmp_path)

        result = asyncio.run(compact.CompactChecker().handle(_checker_ctx(loop)))

        assert result.action is dispatch
        digest.assert_not_awaited()

    def test_the_digest_runs_after_the_compaction_check_with_the_derived_sizes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(compact, "_check_and_compact", AsyncMock(return_value=None))
        digest = AsyncMock(return_value=DigestOutcome.SKIPPED)
        monkeypatch.setattr(compact, "run_task_digest", digest)
        loop = _checker_loop(tmp_path)

        assert asyncio.run(compact.CompactChecker().handle(_checker_ctx(loop))) is None

        kwargs = digest.await_args.kwargs
        assert (kwargs["trigger_tokens"], kwargs["keep_tokens"], kwargs["reason"]) == (50_000, 20_000, THRESHOLD_REASON)
        assert len(kwargs["view"]) == len(loop.messages)

    def test_a_cancelled_digest_cancels_the_task(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(compact, "_check_and_compact", AsyncMock(return_value=None))
        monkeypatch.setattr(compact, "run_task_digest", AsyncMock(return_value=DigestOutcome.CANCELLED))

        result = asyncio.run(compact.CompactChecker().handle(_checker_ctx(_checker_loop(tmp_path))))

        assert result.action.action == ACTION_CANCELLED

    @pytest.mark.parametrize("fields", [
        {"preempt_after_step": True},
        {"runtime_cfg": {"engine": {"compact": {"task_digest_trigger_ratio": 0}}}},
        {"compact_threshold": 0},
    ])
    def test_no_digest_after_a_soft_preempt_or_when_switched_off(
        self, tmp_path: Path, monkeypatch, fields: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(compact, "_check_and_compact", AsyncMock(return_value=None))
        digest = AsyncMock(return_value=DigestOutcome.CREATED)
        monkeypatch.setattr(compact, "run_task_digest", digest)

        asyncio.run(compact.CompactChecker().handle(_checker_ctx(_checker_loop(tmp_path, **fields))))

        digest.assert_not_awaited()

    def test_a_real_loop_state_gets_digested_end_to_end(self, tmp_path: Path) -> None:
        loop = make_loop_state(
            tmp_path, ScriptedRegistry(), messages=_history(6),
            runtime_cfg={"engine": {"compact": {"task_keep_recent_tokens": 0}}},
        )
        loop.provider = _Provider(ProviderResponse(ok=True, text=NARRATIVE))
        loop.compact_threshold = 2_000
        loop.compacted = True
        ctx = SimpleNamespace(messages=loop.messages, extra={"loop_state": loop, "step_count": 0}, step=1)

        asyncio.run(compact.CompactChecker().handle(ctx))

        assert loop.task_digest is not None and loop.task_digest.covered_rounds == 5
        assert loop.provider.calls[0][1] is None
        assert loop.rctx.events.of("task_digest")[0]["covered_rounds"] == 5
        assert request_view(loop.messages, loop)[5]["content"] == loop.task_digest.text
        assert digest_path(tmp_path, TASK).exists()


def _overflow() -> ProviderResponse:
    return ProviderResponse(ok=False, error="prompt is too long", status_code=400, error_kind="context_length")


def _retry_loop(tmp_path: Path, messages: list[dict[str, Any]], provider: _Provider, **fields: Any) -> SimpleNamespace:
    loop = _loop(
        tmp_path, messages, provider=provider, node=Node(id="entry", type="ai"), openai_tools=[], use_stream=False,
        retry_max=0, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
        runtime_cfg={"engine": {"compact": {"task_keep_recent_tokens": 0}}},
    )
    loop.rctx.begin_llm_request = lambda: "request"
    for key, value in fields.items():
        setattr(loop, key, value)
    return loop


class TestContextOverflow:
    def test_an_overflow_is_answered_with_a_digest_before_cutting(self, tmp_path: Path) -> None:
        provider = _Provider(_overflow(), ProviderResponse(ok=True, text=NARRATIVE), ProviderResponse(ok=True, text="done"))
        loop = _retry_loop(tmp_path, _history(6), provider)

        response = asyncio.run(_call_llm_with_retry(loop, 0))

        assert response.ok and response.text == "done"
        assert len(provider.calls) == 3
        digest_request, digest_tools = provider.calls[1]
        assert digest_tools is None and digest_request[0]["role"] == "system"
        retried = provider.calls[2][0]
        assert any(str(message.get("content") or "").startswith(DIGEST_PREFIX) for message in retried)
        assert len(retried) == len(provider.calls[0][0]) - 10 + 1
        events = [call.args[0] for call in loop.rctx.emit_event.await_args_list]
        assert "task_digest" in events and "reactive_compact" not in events
        payload = next(call.args[1] for call in loop.rctx.emit_event.await_args_list if call.args[0] == "task_digest")
        assert payload["reason"] == OVERFLOW_REASON

    def test_with_a_digest_already_in_place_the_overflow_falls_back_to_cutting(self, tmp_path: Path) -> None:
        messages = _history(6)
        messages[-1] = {**messages[-1], "content": "o" * 20_000}
        provider = _Provider(_overflow(), ProviderResponse(ok=True, text="done"))
        loop = _retry_loop(tmp_path, messages, provider)
        _digest(loop, _Writer(ProviderResponse(ok=True, text=NARRATIVE)))
        loop.rctx.emit_event.reset_mock()

        response = asyncio.run(_call_llm_with_retry(loop, 0))

        assert response.ok and len(provider.calls) == 2
        first, second = (len(call[0][-1]["content"]) for call in provider.calls)
        assert first == 20_000 and second < first
        events = [call.args[0] for call in loop.rctx.emit_event.await_args_list]
        assert "reactive_compact" in events and "task_digest" not in events

    def test_without_rounds_to_cover_the_overflow_cuts_as_before(self, tmp_path: Path) -> None:
        messages = _history(1, chars=20_000)
        provider = _Provider(_overflow(), ProviderResponse(ok=True, text="done"))
        loop = _retry_loop(tmp_path, messages, provider)

        response = asyncio.run(_call_llm_with_retry(loop, 0))

        assert response.ok and len(provider.calls) == 2
        assert loop.task_digest is None
        assert len(provider.calls[1][0][-1]["content"]) < len(provider.calls[0][0][-1]["content"])
