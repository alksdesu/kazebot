"""旧工具结果的截短、让位、清空在批量时刻打成标记写进 meta 并落盘，请求视图只按标记渲染。

DeepSeek、Gemini 的前缀缓存要求历史只往后追加：任务中途按新的比值重切保护窗、现算让位，
那条消息之后的整段缓存就不命中。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _path in (_ROOT, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _engine_loop_fixture import SESSION_ID, ScriptedRegistry, make_loop_state, make_rctx  # noqa: E402
from engine.branch_history import branch_base_count, without_branch_boundary  # noqa: E402
from engine.builtin import compact as checker  # noqa: E402
from engine.compact import (  # noqa: E402
    COMPACT_SUMMARY_PREFIX,
    TOOL_RESULT_VIEW_CHARS,
    VIEW_MARK_KEY,
    IdleSettings,
    bounded_tool_result_view,
    idle_settings,
    microcompact_targets,
    view_mark_of,
)
from engine.compaction_plan import build_compaction_plan, record_fingerprint  # noqa: E402
from engine.conversation_store import ConversationChangedError, ConversationStore, Message, MessageType  # noqa: E402
from engine.inference.llm_call import (  # noqa: E402
    loop_chars_per_token,
    provider_messages,
    record_response_usage,
    replays_reasoning,
    request_view,
    view_settings,
)
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from engine.runner import _message_to_history_dict  # noqa: E402
from engine.task_record import TaskRecord, write_task_record  # noqa: E402
from engine.view_marks import (  # noqa: E402
    MAX_MARK_REQUESTS,
    MarkSettings,
    apply_view_marks_to_store,
    keyed_marks,
    mark_fingerprint,
    merge_view_marks,
    parse_mark_requests,
    plan_view_marks,
    render_view_marks,
    with_effective_marks,
    with_planned_marks,
    with_view_mark,
)
from providers.openai import OpenAIProvider  # noqa: E402
from providers.options import OptionSet  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_router import CompactApplyJob  # noqa: E402
from supervisor.types import TaskKind  # noqa: E402

_TRUNCATED = {"kind": "truncated", "chars": TOOL_RESULT_VIEW_CHARS}
_CLEARED = {"kind": "cleared"}
_GOOD_MARK = {"message_id": "m1", "fingerprint": "a" * 64, "mark": {"kind": "cleared"}}
_TOKEN = "view-marks-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _round(
    task_id: str, number: int, content: str, *, stamp: str, tool: str = "probe",
    arguments: dict[str, Any] | None = None,
) -> list[Message]:
    call_id = f"{task_id}-c{number}"
    return [
        Message(
            id=f"{call_id}-call", role="assistant", content="", message_type=MessageType.ASSISTANT,
            created_at=stamp, source_task_id=task_id, meta={"tool_mode": "native"},
            tool_calls=[{"id": call_id, "name": tool, "arguments": dict(arguments or {})}],
        ),
        Message(
            id=f"{call_id}-result", role="tool", content=content, message_type=MessageType.TOOL_RESULT,
            created_at=stamp, source_task_id=task_id, meta={"tool_mode": "native", "tool_status": "success"},
            tool_call_id=call_id, name=tool,
        ),
    ]


def _task(task_id: str, results: list[str], *, stamp: str) -> list[Message]:
    messages = [Message(
        id=f"{task_id}-ask", role="user", content=f"please handle {task_id}",
        message_type=MessageType.USER_INPUT, created_at=stamp, source_task_id=task_id,
    )]
    for number, content in enumerate(results):
        messages += _round(task_id, number, content, stamp=stamp)
    messages.append(Message(
        id=f"{task_id}-done", role="assistant", content=f"{task_id} finished",
        message_type=MessageType.ASSISTANT, created_at=stamp, source_task_id=task_id,
    ))
    return messages


def _read_output(path: str) -> str:
    body = "\n".join(f"{number:>4} | code {number}" for number in range(1, 4))
    return f"── {path} ── 共 10 行\n{body}"


def _runtime(messages: list[Message]) -> list[dict[str, Any]]:
    """和 runner 从会话存储重载历史时一样的运行期形状。"""
    return [_message_to_history_dict(message) for message in messages]


def _results(messages: list[Any]) -> list[int]:
    return [
        index for index, message in enumerate(messages)
        if (message.get("role") if isinstance(message, dict) else message.role) == "tool"
    ]


def _ask(task_id: str) -> dict[str, Any]:
    return {
        "role": "user", "content": f"please handle {task_id}",
        "_meta": {"source_task_id": task_id, "message_type": "user_input"},
    }


def _live_round(task_id: str, step: int, content: str) -> list[dict[str, Any]]:
    call_id = f"{task_id}-live-{step}"
    meta = {"tool_mode": "native", "source_task_id": task_id}
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": "probe", "arguments": {}}],
         "_meta": {**meta, "message_type": "assistant"}},
        {"role": "tool", "tool_call_id": call_id, "name": "probe", "content": content,
         "_meta": {**meta, "message_type": "tool_result", "tool_status": "success"}},
    ]


def _send(view: list[dict[str, Any]], workspace: Path) -> list[str]:
    """一份请求视图转成 native 渠道收到的消息，每条序列化成一个字符串，前缀按它逐条比。"""
    sent = provider_messages(
        view, formatter=create_tool_formatter("native"), provider=SimpleNamespace(name="test"),
        workspace_root=workspace, strip_images=False,
    )
    return [json.dumps(message, ensure_ascii=False, sort_keys=True) for message in sent]


def _wire(messages: list[dict[str, Any]], workspace: Path) -> list[str]:
    return _send(request_view(messages), workspace)


def _is_prefix(earlier: list[str], later: list[str]) -> bool:
    return later[:len(earlier)] == earlier


def _plain_state(workspace: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="cache-stable-view"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _post(app: Any, path: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, **kwargs)

    return asyncio.run(run())


def _usage(tokens: int, chars: int, conversation: str | None = None, *, reasoning: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node_id": "worker", "task_id": "t", "usage": {"prompt_tokens": tokens},
        "prompt_chars": chars, "reasoning_counted": reasoning,
    }
    if conversation is not None:
        payload["conversation_session_id"] = conversation
    return payload


@pytest.fixture
def branch_case(tmp_path: Path) -> SimpleNamespace:
    state = _plain_state(tmp_path)
    state._defer_post_work_enqueue = True
    parent = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:view-marks")
    store = ConversationStore(tmp_path / "data" / "conversations")
    history = [
        message for number in range(3)
        for message in _task(f"task-{number}", ["g" * 6000] * 2, stamp=_ago(2))
    ]
    store.append_batch(parent, history)
    with state._lock:
        branch, fork = state._create_entry_branch_locked(parent, 101)
        task = state._create_task_locked(
            session_id=branch, session_generation=1, kind=TaskKind.node, node_id="qq.orchestrator",
            input_data={"parent_session_id": parent, "branch_session_id": branch, **fork},
        )
    tail = Message(id="current-tail", role="assistant", content="working on it", source_task_id=task.task_id)
    store.append(branch, tail)
    return SimpleNamespace(state=state, store=store, parent=parent, branch=branch, task=task, tail=tail, history=history)


def _branch_prefix(case: SimpleNamespace) -> tuple[list[Message], list[Message]]:
    messages = case.store.load(case.branch)
    boundary = branch_base_count(messages, case.branch, case.task.input["base_count"])
    return without_branch_boundary(messages[:boundary], case.branch), messages[boundary:]


class TestPrefixStaysPut:
    def test_inside_one_task_every_request_extends_the_previous_one(self, tmp_path: Path) -> None:
        history = _runtime(_task("t-old", ["o" * 6000] * 20, stamp=_ago(0.5)))
        planned = plan_view_marks(history, settings=MarkSettings(protect_tokens=10_000), exclude_task_ids=("t-now",))
        assert planned
        messages, _ = with_planned_marks(history, planned)
        messages = [*messages, _ask("t-now")]

        sent = []
        for step in range(6):
            sent.append(_wire(messages, tmp_path))
            messages = [*messages, *_live_round("t-now", step, "n" * 6000)]

        assert all(_is_prefix(earlier, later) for earlier, later in zip(sent, sent[1:]))

    def test_the_old_on_the_fly_cut_rewrites_earlier_results_as_the_task_grows(self, tmp_path: Path) -> None:
        messages = [*_runtime(_task("t-old", ["o" * 6000] * 20, stamp=_ago(0.5))), _ask("t-now")]

        sent = []
        for step in range(2):
            view, _ = bounded_tool_result_view(messages, protect_tokens=10_000, chars_per_token=3.0)
            sent.append(_send(view, tmp_path))
            messages = [*messages, *_live_round("t-now", step, "n" * 6000)]

        # 新结果把保护窗往后推，上一次原样发出去的旧结果这一次被截短
        assert not _is_prefix(sent[0], sent[1])

    def test_recalibrating_reshapes_the_on_the_fly_cut_but_not_the_marked_view(self, tmp_path: Path) -> None:
        messages = [*_runtime(_task("t-old", ["o" * 6000] * 20, stamp=_ago(0.5))), _ask("t-now")]
        loose, _ = bounded_tool_result_view(messages, protect_tokens=40_000, chars_per_token=3.0)
        tight, _ = bounded_tool_result_view(messages, protect_tokens=40_000, chars_per_token=1.5)
        assert _send(loose, tmp_path) != _send(tight, tmp_path)

        planned = plan_view_marks(messages, settings=MarkSettings(protect_tokens=10_000), exclude_task_ids=("t-now",))
        marked, _ = with_planned_marks(messages, planned)
        before = _wire(marked, tmp_path)
        loop = SimpleNamespace(
            last_prompt_tokens=10_000, last_prompt_chars=15_000, task_digest=None,
            rctx=SimpleNamespace(task_id="t-now"),
        )

        assert loop_chars_per_token(loop) == pytest.approx(1.5)
        assert _send(request_view(marked, loop), tmp_path) == before

    def test_the_next_task_reloads_the_same_bytes_and_adds_nothing_below_the_trigger(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "data" / "conversations")
        original = _task("t1", ["p" * 6000] * 20, stamp=_ago(0.2))
        store.append_batch(SESSION_ID, original)
        settings = MarkSettings(protect_tokens=10_000)
        runtime = _runtime(store.load(SESSION_ID))
        marks = keyed_marks(plan_view_marks(runtime, settings=settings, exclude_task_ids=("t2",)))
        effective = apply_view_marks_to_store(store, SESSION_ID, marks)
        in_task, changed = with_effective_marks(runtime, marks, effective)
        assert changed == len(marks) == 15

        reloaded = _runtime(ConversationStore(tmp_path / "data" / "conversations").load(SESSION_ID))

        assert _wire(reloaded, tmp_path) == _wire(in_task, tmp_path)
        assert [message.content for message in store.load(SESSION_ID)] == [message.content for message in original]
        assert plan_view_marks(reloaded, settings=settings, exclude_task_ids=("t3",)) == []


class TestMarks:
    def test_dropping_a_mark_sends_the_original_text_again(self) -> None:
        message = _runtime(_round("t1", 0, "q" * 6000, stamp=_ago(1)))[1]
        marked = with_view_mark(message, _TRUNCATED)
        restored = {**marked, "_meta": {key: value for key, value in marked["_meta"].items() if key != VIEW_MARK_KEY}}

        assert render_view_marks([marked])[0]["content"] != message["content"]
        assert marked["content"] == message["content"]
        assert render_view_marks([restored])[0]["content"] == message["content"]

    @pytest.mark.parametrize("raw", [
        None,
        {},
        "cleared",
        {"kind": "erased"},
        {"kind": "truncated"},
        {"kind": "truncated", "chars": 0},
        {"kind": "truncated", "chars": True},
        {"kind": "superseded", "paths": []},
        {"kind": "superseded", "paths": [""]},
    ])
    def test_history_without_a_valid_mark_is_sent_exactly_as_stored(self, raw: Any) -> None:
        message = {
            "role": "tool", "tool_call_id": "c", "name": "probe", "content": "r" * 9000,
            "_meta": {"message_type": "tool_result"},
        }
        if raw is not None:
            message["_meta"][VIEW_MARK_KEY] = raw

        assert render_view_marks([message]) == [message]

    def test_marks_on_anything_but_a_tool_result_are_ignored(self) -> None:
        summary = {
            "role": "user", "content": COMPACT_SUMMARY_PREFIX + "earlier work",
            "_meta": {"message_type": "summary", VIEW_MARK_KEY: _CLEARED},
        }

        assert render_view_marks([summary]) == [summary]

    def test_a_stronger_mark_replaces_a_weaker_one_and_never_the_other_way(self) -> None:
        record = _round("t1", 0, "x" * 5000, stamp=_ago(1))[1]
        fingerprint = mark_fingerprint(record)

        weaker = merge_view_marks([with_view_mark(record, _CLEARED)], {record.id: (fingerprint, _TRUNCATED)})
        stronger = merge_view_marks([with_view_mark(record, _TRUNCATED)], {record.id: (fingerprint, _CLEARED)})

        assert weaker.changed == 0 and weaker.effective == {record.id: _CLEARED}
        assert stronger.changed == 1 and view_mark_of(stronger.records[0]) == _CLEARED
        assert stronger.records[0].content == record.content

    def test_the_runtime_copy_never_takes_a_weaker_mark_either(self) -> None:
        message = with_view_mark(_runtime(_round("t1", 0, "x" * 5000, stamp=_ago(1)))[1], _CLEARED)
        marks = {message["_meta"]["message_id"]: (mark_fingerprint(message), _TRUNCATED)}

        result, changed = with_effective_marks([message], marks, {message["_meta"]["message_id"]: _TRUNCATED})

        assert changed == 0 and view_mark_of(result[0]) == _CLEARED

    def test_results_that_already_carry_a_mark_are_not_planned_again(self) -> None:
        stamp = _ago(1)
        unmarked = _runtime([
            *_round("t1", 0, _read_output("a.py"), stamp=stamp, tool="read_file", arguments={"path": "a.py"}),
            *_round("t1", 1, "ok", stamp=stamp, tool="write_file", arguments={"path": "a.py", "content": "x"}),
        ])
        marked = list(unmarked)
        marked[1] = with_view_mark(marked[1], _TRUNCATED)
        settings = MarkSettings()

        fresh = plan_view_marks(unmarked, settings=settings, force=True)

        assert [item.index for item in fresh] == [1] and fresh[0].mark["kind"] == "superseded"
        # 截短过的不会再换成让位说明，否则同一条消息第二次变样
        assert plan_view_marks(marked, settings=settings, force=True) == []

    def test_task_start_waits_for_the_trigger_and_then_pushes_to_the_low_watermark(self) -> None:
        settings = MarkSettings(protect_tokens=10_000)
        below = _runtime(_task("t1", ["b" * 6000] * 7, stamp=_ago(1)))
        above = _runtime(_task("t1", ["b" * 6000] * 8, stamp=_ago(1)))

        assert plan_view_marks(below, settings=settings) == []
        planned = plan_view_marks(above, settings=settings)

        assert [item.index for item in planned] == _results(above)[:3]
        assert all(item.mark == _TRUNCATED for item in planned)

    def test_the_running_task_s_own_results_are_never_marked(self) -> None:
        messages = _runtime(_task("t1", ["c" * 6000] * 10, stamp=_ago(1)))

        planned = plan_view_marks(
            messages, settings=MarkSettings(), exclude_task_ids=("t1",), force=True, cleared=_results(messages),
        )

        assert planned == []

    def test_superseding_starts_at_the_push_and_only_cold_moments_reach_further_back(self) -> None:
        stamp = _ago(1)
        messages = _runtime([
            *_round("t1", 0, _read_output("a.py"), stamp=stamp, tool="read_file", arguments={"path": "a.py"}),
            *_round("t1", 1, "ok", stamp=stamp, tool="write_file", arguments={"path": "a.py", "content": "x"}),
            *[message for number in range(2, 10) for message in _round("t1", number, "b" * 6000, stamp=stamp)],
        ])
        read = _results(messages)[0]
        settings = MarkSettings(protect_tokens=10_000)

        warm = {item.index: item.mark for item in plan_view_marks(messages, settings=settings)}
        cold = {item.index: item.mark for item in plan_view_marks(messages, settings=settings, force=True)}

        assert warm and read not in warm
        assert cold[read]["kind"] == "superseded" and cold[read]["paths"] == ["a.py"]
        assert cold[read]["by"] == "t1-c1-result"

    def test_idle_clearing_upgrades_a_truncated_result(self) -> None:
        messages = _runtime(_task("t1", ["d" * 300] * 8, stamp=_ago(3)))
        first = _results(messages)[0]
        messages[first] = with_view_mark(messages[first], _TRUNCATED)

        targets = microcompact_targets(messages, gap_minutes=60, keep_recent=5)
        planned = plan_view_marks(messages, settings=MarkSettings(), force=True, cleared=targets)

        assert targets == _results(messages)[:3]
        assert {item.index: item.mark for item in planned} == {index: _CLEARED for index in targets}

    def test_marks_land_only_on_matching_text_and_retry_a_concurrent_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = ConversationStore(tmp_path / "data" / "conversations")
        stored = _round("t1", 0, "d" * 6000, stamp=_ago(1)) + _round("t1", 1, "e" * 6000, stamp=_ago(1))
        store.append_batch(SESSION_ID, stored)
        first, second = stored[1], stored[3]
        marks = {
            first.id: (mark_fingerprint(first), _TRUNCATED),
            second.id: ("0" * 64, _CLEARED),
        }
        real = store.replace_all
        calls: list[str] = []

        def flaky(session_id: str, messages: list[Message]) -> None:
            calls.append(session_id)
            if len(calls) == 1:
                raise ConversationChangedError("written meanwhile")
            real(session_id, messages)

        monkeypatch.setattr(store, "replace_all", flaky)

        effective = apply_view_marks_to_store(store, SESSION_ID, marks)

        assert effective == {first.id: _TRUNCATED}
        assert len(calls) == 2
        reloaded = {message.id: message for message in ConversationStore(tmp_path / "data" / "conversations").load(SESSION_ID)}
        assert view_mark_of(reloaded[first.id]) == _TRUNCATED
        assert view_mark_of(reloaded[second.id]) is None
        assert reloaded[first.id].content == first.content
        # 已经生效的标记不再重写文件
        assert apply_view_marks_to_store(store, SESSION_ID, marks) == {first.id: _TRUNCATED}
        assert len(calls) == 2

    def test_a_compaction_plan_still_recognises_a_message_marked_after_it(self) -> None:
        record = _round("t1", 0, "f" * 6000, stamp=_ago(1))[1]
        bare = dataclasses.replace(record, meta={})

        for message in (record, bare, record.to_dict(), _message_to_history_dict(record)):
            assert record_fingerprint(with_view_mark(message, _CLEARED)) == record_fingerprint(message)

    def test_the_fingerprint_is_the_same_for_the_runtime_and_the_stored_form(self) -> None:
        record = _round("t1", 0, "g" * 100, stamp=_ago(1))[1]

        assert mark_fingerprint(record) == mark_fingerprint(record.to_dict())
        assert mark_fingerprint(record) == mark_fingerprint(_message_to_history_dict(record))
        assert mark_fingerprint(dataclasses.replace(record, content="other")) != mark_fingerprint(record)


def _mark_item(**overrides: Any) -> dict[str, Any]:
    return {**_GOOD_MARK, **overrides}


class TestMarkRequests:
    def test_a_valid_list_becomes_keyed_marks(self) -> None:
        parsed = parse_mark_requests([_mark_item(), _mark_item(message_id="m2", mark={"kind": "truncated", "chars": 2000})])

        assert parsed == {
            "m1": ("a" * 64, _CLEARED),
            "m2": ("a" * 64, {"kind": "truncated", "chars": 2000}),
        }

    @pytest.mark.parametrize("raw", [
        None,
        {},
        [],
        "marks",
        ["not a mark"],
        [{"fingerprint": "a" * 64, "mark": _CLEARED}],
        [_mark_item(message_id="")],
        [_mark_item(message_id="m" * 257)],
        [_mark_item(fingerprint="A" * 64)],
        [_mark_item(fingerprint="a" * 63)],
        [_mark_item(mark={"kind": "erased"})],
        [_mark_item(mark={"kind": "truncated", "chars": -1})],
        [_mark_item(), _mark_item()],
    ])
    def test_anything_malformed_rejects_the_whole_request(self, raw: Any) -> None:
        assert parse_mark_requests(raw) is None

    def test_the_request_size_is_capped(self) -> None:
        items = [_mark_item(message_id=f"m{number}") for number in range(MAX_MARK_REQUESTS + 1)]

        assert parse_mark_requests(items) is None
        assert parse_mark_requests(items[:-1]) is not None


class TestSupervisorMarks:
    def test_the_parent_and_every_live_branch_copy_get_the_same_marks_and_the_base_stays(
        self, branch_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        case = branch_case
        events: list[str] = []
        snapshot = case.state._event_task_snapshot

        def spy(event: str, task: Any, *args: Any, **kwargs: Any) -> Any:
            events.append(event)
            return snapshot(event, task, *args, **kwargs)

        monkeypatch.setattr(case.state, "_event_task_snapshot", spy)
        base = (case.task.input["base_count"], case.task.input["base_last_id"])
        targets = [message for message in case.history if message.role == "tool"][:2]
        marks = {message.id: (mark_fingerprint(message), dict(_TRUNCATED)) for message in targets}

        result = case.state._apply_view_marks_via_conv_store(
            case.parent, marks, branch_session_id=case.branch, reason="task_start",
        )

        assert result == {
            "applied": True, "synchronized": True, "branch_synchronized": True,
            "marks": {message.id: _TRUNCATED for message in targets},
        }
        assert "task_context_compacted" not in events
        assert (case.task.input["base_count"], case.task.input["base_last_id"]) == base
        parent_messages = case.store.load(case.parent)
        assert [message.content for message in parent_messages] == [message.content for message in case.history]
        assert {
            message.id: view_mark_of(message) for message in parent_messages if view_mark_of(message) is not None
        } == {message.id: _TRUNCATED for message in targets}
        prefix, tail = _branch_prefix(case)
        assert [message.to_dict() for message in prefix] == [message.to_dict() for message in parent_messages]
        assert tail == [case.tail]

    def test_a_branch_copy_whose_text_differs_is_left_alone_and_reported(self, branch_case: SimpleNamespace) -> None:
        case = branch_case
        target = next(message for message in case.history if message.role == "tool")
        writer = ConversationStore(case.state.workspace_root / "data" / "conversations")
        diverged = [
            dataclasses.replace(message, content="different text") if message.id == target.id else message
            for message in writer.load(case.branch)
        ]
        writer.replace_all(case.branch, diverged)
        before = [message.to_dict() for message in writer.load(case.branch)]

        result = case.state._apply_view_marks_via_conv_store(
            case.parent, {target.id: (mark_fingerprint(target), dict(_CLEARED))}, branch_session_id=case.branch,
        )

        assert result["applied"] is True and result["marks"] == {target.id: _CLEARED}
        assert result["synchronized"] is False and result["branch_synchronized"] is False
        assert [message.to_dict() for message in writer.load(case.branch)] == before
        assert view_mark_of(next(message for message in case.store.load(case.parent) if message.id == target.id)) == _CLEARED

    def test_marks_for_messages_the_parent_does_not_have_change_nothing(self, branch_case: SimpleNamespace) -> None:
        case = branch_case
        before = [message.to_dict() for message in case.store.load(case.parent)]

        result = case.state._apply_view_marks_via_conv_store(
            case.parent, {"missing": ("a" * 64, dict(_CLEARED))}, branch_session_id=case.branch,
        )

        assert result == {"applied": False, "synchronized": True, "branch_synchronized": True, "marks": {}}
        assert [message.to_dict() for message in case.store.load(case.parent)] == before

    def test_after_an_llm_compaction_the_kept_history_is_marked_on_the_parent_and_the_branch(
        self, branch_case: SimpleNamespace,
    ) -> None:
        case = branch_case
        plan, _ = build_compaction_plan(case.store.load(case.parent), keep_recent=1)
        job = CompactApplyJob(
            target_session_id=case.parent, summary="Summary of the earlier tasks.", plan=plan,
            storage="conversation", context_ref="",
            sizing=case.state._compact_sizing({"_compact_keep_recent": 1, "_compact_threshold_tokens": 5_000}),
        )
        with case.state._lock:
            assert case.task.task_id in case.state._active_task_ids_for_session_locked(case.parent)

        outcome = case.state._run_compact_apply_job(job)

        assert outcome.success
        parent_messages = case.store.load(case.parent)
        kept = [message for message in parent_messages if message.role == "tool"]
        # 软阈值 5000 → 保护 2000 token：最新一条留原文，更早那条截短
        assert [view_mark_of(message) for message in kept] == [_TRUNCATED, None]
        assert [message.content for message in kept] == ["g" * 6000] * 2
        prefix, tail = _branch_prefix(case)
        assert [message.to_dict() for message in prefix] == [message.to_dict() for message in parent_messages]
        assert tail == [case.tail]


class TestViewMarksEndpoint:
    @pytest.fixture(autouse=True)
    def _admin_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
        monkeypatch.setattr(admin_api, "_admin_token", "")
        monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())

    def test_a_branch_task_s_marks_are_applied_and_confirmed(self, branch_case: SimpleNamespace) -> None:
        case = branch_case
        target = next(message for message in case.history if message.role == "tool")

        response = _post(_app(case.state, case.state.workspace_root), "/v1/tasks/view-marks", headers=_AUTH, json={
            "target_session_id": case.parent, "branch_session_id": case.branch, "reason": "idle_clear",
            "marks": [{"message_id": target.id, "fingerprint": mark_fingerprint(target), "mark": _CLEARED}],
        })

        assert response.status_code == 200, response.text
        assert response.json() == {
            "applied": True, "synchronized": True, "branch_synchronized": True, "marks": {target.id: _CLEARED},
        }
        prefix, _ = _branch_prefix(case)
        assert view_mark_of(next(message for message in prefix if message.id == target.id)) == _CLEARED

    def test_it_needs_the_admin_token(self, branch_case: SimpleNamespace) -> None:
        case = branch_case
        target = next(message for message in case.history if message.role == "tool")
        before = [message.to_dict() for message in case.store.load(case.parent)]

        response = _post(_app(case.state, case.state.workspace_root), "/v1/tasks/view-marks", json={
            "target_session_id": case.parent, "branch_session_id": case.branch, "reason": "idle_clear",
            "marks": [{"message_id": target.id, "fingerprint": mark_fingerprint(target), "mark": _CLEARED}],
        })

        assert response.status_code == 401
        assert [message.to_dict() for message in case.store.load(case.parent)] == before
        prefix, _ = _branch_prefix(case)
        assert view_mark_of(next(message for message in prefix if message.id == target.id)) is None

    @pytest.mark.parametrize("make_body", [
        lambda parent: [_GOOD_MARK],
        lambda parent: {"marks": [_GOOD_MARK]},
        lambda parent: {"target_session_id": "   ", "marks": [_GOOD_MARK]},
        lambda parent: {"target_session_id": parent, "marks": []},
        lambda parent: {"target_session_id": parent},
        lambda parent: {"target_session_id": parent, "marks": [_mark_item(fingerprint="xyz")]},
        lambda parent: {"target_session_id": parent, "marks": [_mark_item(mark={"kind": "erased"})]},
        lambda parent: {"target_session_id": parent, "marks": [_GOOD_MARK, _GOOD_MARK]},
        lambda parent: {"target_session_id": parent, "branch_session_id": 7, "marks": [_GOOD_MARK]},
        lambda parent: {"target_session_id": parent, "reason": ["idle"], "marks": [_GOOD_MARK]},
    ])
    def test_malformed_requests_are_rejected(self, tmp_path: Path, make_body: Any) -> None:
        state = _plain_state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:view-marks-endpoint")

        response = _post(_app(state, tmp_path), "/v1/tasks/view-marks", headers=_AUTH, json=make_body(parent))

        assert response.status_code == 400, response.text

    def test_a_body_that_is_not_json_is_rejected(self, tmp_path: Path) -> None:
        response = _post(
            _app(_plain_state(tmp_path), tmp_path), "/v1/tasks/view-marks",
            content=b"{", headers={**_AUTH, "content-type": "application/json"},
        )

        assert response.status_code == 400

    def test_an_unknown_session_is_not_found(self, tmp_path: Path) -> None:
        response = _post(_app(_plain_state(tmp_path), tmp_path), "/v1/tasks/view-marks", headers=_AUTH, json={
            "target_session_id": "no-such-session", "marks": [_GOOD_MARK],
        })

        assert response.status_code == 404


class TestEngineBranchPath:
    @staticmethod
    def _history() -> list[dict[str, Any]]:
        return _runtime(_task("t-old", ["h" * 6000] * 8, stamp=_ago(0.5)))

    def _loop(self, tmp_path: Path, response: Any) -> tuple[SimpleNamespace, Any]:
        rctx = make_rctx(tmp_path, session_id="branch_7", parent_session_id="parent-1")
        rctx.http = SimpleNamespace(post=AsyncMock(return_value=response))
        loop = make_loop_state(tmp_path, ScriptedRegistry(), rctx=rctx, messages=self._history())
        # 软阈值 25000 → 保护 10000 token，8 条 2000 token 的旧结果越过 1.5 倍触发线
        loop.compact_threshold = 25_000
        return SimpleNamespace(messages=loop.messages, extra={}, step=0), loop

    @staticmethod
    def _reply(payload: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    def test_the_engine_sends_its_marks_and_keeps_only_what_the_supervisor_confirmed(self, tmp_path: Path) -> None:
        history = self._history()
        oldest = [history[index]["_meta"]["message_id"] for index in _results(history)[:3]]
        ctx, loop = self._loop(tmp_path, self._reply({
            "branch_synchronized": True, "marks": {oldest[0]: _TRUNCATED, oldest[1]: _CLEARED},
        }))

        changed = asyncio.run(checker._push_view_marks(ctx, loop, force=False, reason="task_start"))

        assert changed is True
        url = loop.rctx.http.post.await_args.args[0]
        body = loop.rctx.http.post.await_args.kwargs["json"]
        assert url == "http://supervisor.invalid/v1/tasks/view-marks"
        assert (body["target_session_id"], body["branch_session_id"], body["reason"]) == ("parent-1", "branch_7", "task_start")
        assert [item["message_id"] for item in body["marks"]] == oldest
        assert all(item["mark"] == _TRUNCATED and len(item["fingerprint"]) == 64 for item in body["marks"])
        marks = {
            message["_meta"]["message_id"]: view_mark_of(message)
            for message in loop.messages if message.get("role") == "tool"
        }
        assert (marks[oldest[0]], marks[oldest[1]], marks[oldest[2]]) == (_TRUNCATED, _CLEARED, None)
        assert ctx.messages is loop.messages
        # 入口分支上的任务不在本地改会话文件，一律经 supervisor
        assert loop.rctx.conversation_store.load("parent-1") == []
        assert loop.rctx.conversation_store.load("branch_7") == []

    def test_when_the_branch_copy_was_not_updated_the_runtime_messages_stay_as_they_were(self, tmp_path: Path) -> None:
        ctx, loop = self._loop(tmp_path, self._reply({"branch_synchronized": False, "marks": {}}))
        before = loop.messages

        assert asyncio.run(checker._push_view_marks(ctx, loop, force=False, reason="task_start")) is False
        assert loop.messages is before
        assert all(view_mark_of(message) is None for message in loop.messages)

    def test_a_failed_request_leaves_the_runtime_messages_alone(self, tmp_path: Path) -> None:
        def unavailable() -> None:
            raise RuntimeError("supervisor unavailable")

        ctx, loop = self._loop(tmp_path, SimpleNamespace(raise_for_status=unavailable, json=lambda: {}))
        before = loop.messages

        assert asyncio.run(checker._push_view_marks(ctx, loop, force=True, reason="soft_snip")) is False
        assert loop.messages is before


class TestIdleCleanup:
    @staticmethod
    def _loop(
        tmp_path: Path, stored: list[Message], runtime_cfg: dict[str, Any] | None = None,
    ) -> tuple[SimpleNamespace, Any]:
        rctx = make_rctx(tmp_path)
        rctx.conversation_store.append_batch(SESSION_ID, stored)
        loop = make_loop_state(
            tmp_path, ScriptedRegistry(), rctx=rctx, runtime_cfg=runtime_cfg,
            messages=_runtime(rctx.conversation_store.load(SESSION_ID)),
        )
        return SimpleNamespace(messages=loop.messages, extra={}, step=0), loop

    @staticmethod
    def _summarize(tmp_path: Path, task_ids: list[str]) -> None:
        for task_id in task_ids:
            write_task_record(tmp_path, TaskRecord(
                task_id=task_id, session_id=SESSION_ID, node_id="test", summary=f"Summary of {task_id}",
            ))

    def test_cleared_marks_are_written_to_the_store_and_reload_identically(self, tmp_path: Path) -> None:
        stored = _task("t-old", ["i" * 300] * 8, stamp=_ago(3))
        ctx, loop = self._loop(tmp_path, stored)

        assert asyncio.run(checker._run_idle_cleanup(ctx, loop)) is True

        reloaded = _runtime(ConversationStore(tmp_path / "data" / "conversations").load(SESSION_ID))
        cleared = [index for index, message in enumerate(reloaded) if view_mark_of(message) == _CLEARED]
        assert cleared == _results(reloaded)[:3]
        assert _wire(reloaded, tmp_path) == _wire(loop.messages, tmp_path)
        assert [message.content for message in loop.rctx.conversation_store.load(SESSION_ID)] == [
            message.content for message in stored
        ]

    def test_zero_switches_leave_an_idle_session_alone(self, tmp_path: Path) -> None:
        stored = [message for number in range(5) for message in _task(f"t{number}", ["j" * 300] * 3, stamp=_ago(30))]
        self._summarize(tmp_path, [f"t{number}" for number in range(5)])
        ctx, loop = self._loop(tmp_path, stored, {"engine": {"compact": {"idle_clear_minutes": 0, "idle_snip_hours": 0}}})

        assert asyncio.run(checker._run_idle_cleanup(ctx, loop)) is False

        persisted = loop.rctx.conversation_store.load(SESSION_ID)
        assert [message.to_dict() for message in persisted] == [message.to_dict() for message in stored]

    @pytest.mark.parametrize("keep, summarized", [(1, 3), (3, 1)])
    def test_idle_snip_keeps_the_configured_number_of_tasks(self, tmp_path: Path, keep: int, summarized: int) -> None:
        stored = [message for number in range(4) for message in _task(f"t{number}", ["k" * 300], stamp=_ago(2))]
        self._summarize(tmp_path, [f"t{number}" for number in range(4)])
        ctx, loop = self._loop(tmp_path, stored, {"engine": {"compact": {
            "idle_snip_hours": 1, "idle_snip_keep_tasks": keep, "idle_clear_minutes": 0,
        }}})

        assert asyncio.run(checker._run_idle_cleanup(ctx, loop)) is True

        persisted = loop.rctx.conversation_store.load(SESSION_ID)
        assert sum(1 for message in persisted if message.message_type == "summary") == summarized

    @pytest.mark.parametrize("channel, provider_type, expected", [
        ("", "", IdleSettings(60, 5, 1.0, 3)),
        ("", "deepseek", IdleSettings(1440, 5, 24.0, 3)),
        ("ds-relay", "openai", IdleSettings(30, 2, 1.0, 3)),
        ("ds-relay", "deepseek", IdleSettings(30, 2, 24.0, 3)),
    ])
    def test_thresholds_follow_the_channel_then_the_provider_then_the_global_value(
        self, channel: str, provider_type: str, expected: IdleSettings,
    ) -> None:
        cfg = {"engine": {"compact": {"idle_overrides": {
            "deepseek": {"idle_clear_minutes": 1440, "idle_snip_hours": 24},
            "ds-relay": {"idle_clear_minutes": 30, "idle_clear_keep_results": 2},
        }}}}

        assert idle_settings(cfg, channel=channel, provider_type=provider_type) == expected

    def test_zero_turns_idle_clearing_off(self) -> None:
        messages = _runtime(_task("t-old", ["j" * 300] * 8, stamp=_ago(30)))

        assert microcompact_targets(messages, gap_minutes=0) == []
        assert idle_settings({"engine": {"compact": {"idle_clear_minutes": 0}}}).clear_minutes == 0

    def test_the_bundled_defaults_give_deepseek_a_day(self) -> None:
        cfg = yaml.safe_load((_ROOT / "defaults" / "runtime.yaml").read_text(encoding="utf-8"))
        compact = cfg["engine"]["compact"]

        assert idle_settings(cfg) == IdleSettings(60, 5, 1.0, 3)
        assert idle_settings(cfg, provider_type="deepseek") == IdleSettings(1440, 5, 24.0, 3)
        assert (compact["tool_result_mark_trigger_ratio"], compact["tool_result_mark_low_ratio"]) == (1.5, 1.0)


class TestSoftThresholdSnipsFirst:
    @staticmethod
    def _loop(tmp_path: Path, sizes: list[int], *, summaries: bool) -> tuple[SimpleNamespace, Any]:
        rctx = make_rctx(tmp_path)
        stored = [
            message for number, size in enumerate(sizes)
            for message in _task(f"t{number}", ["s" * size], stamp=_ago(0.1))
        ]
        rctx.conversation_store.append_batch(SESSION_ID, stored)
        if summaries:
            for number in range(len(sizes)):
                write_task_record(tmp_path, TaskRecord(
                    task_id=f"t{number}", session_id=SESSION_ID, node_id="test", summary=f"Summary of task {number}",
                ))
        response = SimpleNamespace(content=b"{}", raise_for_status=lambda: None, json=lambda: {"task_id": "background-1"})
        rctx.http = SimpleNamespace(post=AsyncMock(return_value=response))
        loop = make_loop_state(
            tmp_path, ScriptedRegistry(), rctx=rctx, messages=_runtime(rctx.conversation_store.load(SESSION_ID)),
        )
        loop.compact_threshold, loop.compact_hard_threshold = 2_000, 100_000
        loop.compact_keep_recent = 1
        return SimpleNamespace(messages=loop.messages, extra={}, step=1), loop

    def test_a_snip_that_gets_under_the_soft_threshold_skips_background_compaction(self, tmp_path: Path) -> None:
        ctx, loop = self._loop(tmp_path, [6000, 6000, 100, 100, 100], summaries=True)

        assert asyncio.run(checker._check_and_compact(ctx, loop)) is None

        loop.rctx.http.post.assert_not_awaited()
        assert [event["threshold"] for event in loop.rctx.events.of("snip_compact")] == ["soft"]
        assert ctx.extra.get("compact_modified") is True
        persisted = loop.rctx.conversation_store.load(SESSION_ID)
        assert sum(1 for message in persisted if message.message_type == "summary") == 2

    def test_still_over_after_the_snip_starts_background_compaction(self, tmp_path: Path) -> None:
        ctx, loop = self._loop(tmp_path, [100, 100, 6000, 6000, 6000], summaries=True)

        assert asyncio.run(checker._check_and_compact(ctx, loop)) is None

        assert [event["threshold"] for event in loop.rctx.events.of("snip_compact")] == ["soft"]
        assert loop.rctx.http.post.await_args.args[0] == "http://supervisor.invalid/v1/tasks/compact-async"

    def test_without_turn_summaries_it_goes_straight_to_background_compaction(self, tmp_path: Path) -> None:
        ctx, loop = self._loop(tmp_path, [6000, 6000, 100, 100, 100], summaries=False)

        assert asyncio.run(checker._check_and_compact(ctx, loop)) is None

        assert loop.rctx.events.of("snip_compact") == []
        assert loop.rctx.http.post.await_args.args[0] == "http://supervisor.invalid/v1/tasks/compact-async"


class TestCalibration:
    def test_the_first_step_estimates_with_the_ratio_the_supervisor_handed_over(self, tmp_path: Path) -> None:
        rctx = make_rctx(tmp_path, task_context={"chars_per_token": 1.5})
        loop = make_loop_state(tmp_path, ScriptedRegistry(), rctx=rctx)

        assert loop_chars_per_token(loop) == pytest.approx(1.5)
        assert checker._mark_settings(loop, view_settings(loop)).chars_per_token == pytest.approx(1.5)

    @pytest.mark.parametrize("provider, mode, counted", [
        (SimpleNamespace(name="deepseek"), "json", True),
        (SimpleNamespace(name="openai", _options=OptionSet(OpenAIProvider.OPTIONS, {})), "native", True),
        (SimpleNamespace(name="openai", _options=OptionSet(OpenAIProvider.OPTIONS, {"replay_reasoning": False})), "native", False),
        (SimpleNamespace(name="anthropic"), "native", True),
        (SimpleNamespace(name="anthropic"), "json", False),
        (SimpleNamespace(name="gemini"), "native", False),
        (SimpleNamespace(name="openai-responses"), "native", False),
        (None, "native", False),
    ])
    def test_reasoning_counts_only_where_the_channel_sends_it_back(self, provider: Any, mode: str, counted: bool) -> None:
        assert replays_reasoning(provider, create_tool_formatter(mode)) is counted

    def test_the_usage_event_counts_reasoning_the_way_it_was_sent_and_names_the_conversation(self) -> None:
        rctx = SimpleNamespace(
            task_id="t", emit_event=AsyncMock(), session_id="branch_1", parent_session_id="parent-1", child_session_id="",
        )
        loop = SimpleNamespace(
            node=SimpleNamespace(id="worker"), rctx=rctx, provider=SimpleNamespace(name="deepseek"), formatter=None,
            compact_threshold=0, compact_hard_threshold=0, context_window=None,
        )
        messages = [{"role": "user", "content": "x" * 300}, {"role": "assistant", "content": "ok", "reasoning_content": "r" * 700}]

        asyncio.run(record_response_usage(loop, SimpleNamespace(usage={"prompt_tokens": 500}), messages, None))

        event, payload = rctx.emit_event.await_args.args
        assert event == "context_usage"
        assert payload["prompt_chars"] == 1002 and payload["reasoning_counted"] is True
        assert payload["conversation_session_id"] == "parent-1"

    def test_the_parent_keeps_its_own_ratio_while_children_and_branches_report(self, tmp_path: Path) -> None:
        state = _plain_state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:calibration")
        with state._lock:
            branch, _ = state._create_entry_branch_locked(parent, 5)
            child, _ = state.get_or_create_child_session(parent, "worker", "ctx", "accumulate")

        state.update_context_usage(parent, _usage(1000, 2000, parent, reasoning=True))
        state.update_context_usage(parent, _usage(500, 3000, child))
        state.update_context_usage(parent, _usage(100, 600, "agent:ghost"))

        own = state.session_calibration(parent)
        child_calibration = state.session_calibration(child)
        ghost = state.session_calibration("agent:ghost")
        assert own == {"prompt_tokens": 1000, "prompt_chars": 2000, "reasoning": True}
        assert child_calibration == {"prompt_tokens": 500, "prompt_chars": 3000, "reasoning": False}
        assert ghost is None
        assert state._session_chars_per_token(parent, []) == pytest.approx(2.0)
        assert state._session_counts_reasoning(parent) is True
        assert state._session_chars_per_token(child, []) == pytest.approx(6.0)
        # 展示用的数字照旧是最近一次请求
        assert state._session_context_usage[parent]["prompt_tokens"] == 100

        state.update_context_usage(parent, _usage(800, 2400, branch))
        assert state.session_calibration(parent)["prompt_tokens"] == 800

        state.update_context_usage(parent, _usage(1200, 4800))
        assert state.session_calibration(parent) == {
            "prompt_tokens": 1200, "prompt_chars": 4800, "reasoning": False,
        }

    def test_display_numbers_alone_do_not_calibrate(self, tmp_path: Path) -> None:
        state = _plain_state(tmp_path)
        state._session_context_usage["session-x"] = {"prompt_tokens": 1000, "prompt_chars": 1500}

        assert state._session_chars_per_token("session-x", []) == pytest.approx(3.0)

    def test_the_store_fallback_counts_reasoning_like_the_measured_request(self, tmp_path: Path) -> None:
        state = _plain_state(tmp_path)
        state._session_context_usage["session-x"] = {"calibration": {"prompt_tokens": 1000, "reasoning": True}}
        messages = [
            Message(id="a", role="user", content="x" * 1000),
            Message(id="b", role="assistant", content="y" * 1000, meta={"reasoning": "z" * 1000}),
        ]

        assert state._session_chars_per_token("session-x", messages) == pytest.approx(3.0)
        state._session_context_usage["session-x"]["calibration"]["reasoning"] = False
        assert state._session_chars_per_token("session-x", messages) == pytest.approx(2.0)

    def test_a_new_entry_task_carries_the_conversation_s_last_measured_ratio(self, tmp_path: Path) -> None:
        state = _plain_state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:entry-ratio")
        with state._lock:
            first = state._create_entry_task_for_inbound_locked(inbound_seq=1, session_id=parent, payload={"text": "hello"})

        state.update_context_usage(parent, _usage(1000, 1600, parent))
        with state._lock:
            second = state._create_entry_task_for_inbound_locked(inbound_seq=2, session_id=parent, payload={"text": "again"})

        assert first is not None and "chars_per_token" not in first.input["task_context"]
        assert second is not None and second.input["task_context"]["chars_per_token"] == pytest.approx(1.6)
