"""用户那一轮收尾时补进对话的话：失败时发给用户一句人话、历史里留一条说明（取消和打断不算失败），
比它晚开始的轮先合并了的那一轮，开头那句输入标出它自己的起止时间。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_routing import (  # noqa: E402
    LATE_MERGE_KEY,
    SHORT_REPLY,
    SHORT_REPLY_HINT,
    late_merge_meta,
    late_merge_note,
    render_history_inputs,
)
from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.protocol import ACTION_FAIL, TaskAction  # noqa: E402
from supervisor import turn_notes  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_requeue import USER_INTERRUPTED_ERROR  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

_CST = timezone(timedelta(hours=8))
_CONV = "qq_private:turn-notes"
_AUTH = {"platform": "qq", "user_id": "10001", "is_admin": False, "group_scope": ""}


def _task(**overrides: Any) -> Task:
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "task_id": "t-1", "session_id": "branch_1", "kind": TaskKind.node, "node_id": "qq.orchestrator",
        "input": {"task_context": {}}, "status": TaskStatus.failed, "created_at": now, "updated_at": now,
        "result": {"action": "fail", "error": "boom", "error_kind": "network"},
    }
    fields.update(overrides)
    return Task(**fields)


def _message(message_id: str, task_id: str, at: str, *, role: str = "user", kind: str = MessageType.USER_INPUT,
             meta: dict[str, Any] | None = None, content: str = "hi") -> Message:
    return Message(
        id=message_id, role=role, content=content, message_type=kind, created_at=at,
        meta=dict(meta or {}), source_task_id=task_id,
    )


class TestWhatTheUserIsTold:
    @pytest.mark.parametrize("kind, reply", [
        ("network", "这条没能回复：连不上模型服务，稍后再发一次试试。"),
        ("Timeout", "这条没能回复：模型服务迟迟没有回应，稍后再发一次试试。"),
        ("incomplete", "这条没能回复：模型的回复中途断了，再发一次试试。"),
    ])
    def test_a_transport_failure_is_told_by_its_kind_never_by_the_raw_error(self, kind: str, reply: str) -> None:
        result = {"error": "PluginTransportError: Provider transport failed (ConnectError) (已重试 3 次)", "error_kind": kind}

        assert turn_notes.failure_reply(_task(), result) == reply

    @pytest.mark.parametrize("kind, lead", [
        ("Rate_Limit", "模型服务的额度或频率到了上限"),
        ("context_length", "对话太长，模型装不下了"),
        ("unknown", "调用模型时出错了"),
    ])
    def test_what_the_upstream_said_is_passed_on_with_the_kind(self, kind: str, lead: str) -> None:
        result = {"error": "Your billing account has exceeded its monthly spending cap.", "error_kind": kind}

        reply = turn_notes.failure_reply(_task(), result)

        assert lead in reply
        assert "（Your billing account has exceeded its monthly spending cap.）" in reply

    def test_an_auth_failure_never_carries_the_upstream_text(self) -> None:
        result = {"error": "Incorrect API key provided: sk-abc123", "error_kind": "auth"}

        assert turn_notes.failure_reply(_task(), result) == "这条没能回复：模型服务不认这个渠道的密钥，需要管理员检查渠道配置。"

    def test_an_error_that_merely_repeats_the_kind_is_not_echoed(self) -> None:
        result = {"error": "rate_limit", "error_kind": "rate_limit"}

        assert turn_notes.failure_reply(_task(), result) == "这条没能回复：模型服务的额度或频率到了上限，过一会儿再试。"

    def test_a_long_upstream_answer_is_cut(self) -> None:
        reply = turn_notes.failure_reply(_task(), {"error": "x" * 400, "error_kind": "server"})

        assert reply == "这条没能回复：模型服务出错了（" + "x" * 159 + "…），稍后再发一次试试。"

    def test_a_reason_written_for_people_is_passed_on(self) -> None:
        assert turn_notes.failure_reply(_task(), {"error": USER_INTERRUPTED_ERROR}) == f"这条没能回复：{USER_INTERRUPTED_ERROR}"
        assert turn_notes.failure_reply(_task(), {"error": "达到最大步数限制"}) == "这条没能回复：达到最大步数限制。"

    @pytest.mark.parametrize("started_by", [
        {"inbound_message_type": "dispatch_result"}, {"inbound_message_type": "auto_continue"}, {"schedule_id": "daily"},
    ], ids=["dispatch-result", "auto-continue", "schedule"])
    def test_a_round_no_user_message_started_is_not_called_unanswered(self, started_by: dict[str, Any]) -> None:
        task = _task(input={"task_context": {}, **started_by})

        assert turn_notes.failure_reply(task, {"error_kind": "network"}) == "这一轮没做完：连不上模型服务，稍后再发一次试试。"

    def test_a_long_reason_is_folded_and_cut(self) -> None:
        reply = turn_notes.failure_reply(_task(), {"error": "第一行\n" + "长" * 400})

        assert "\n" not in reply
        assert reply.endswith("…") and len(reply) == len("这条没能回复：") + 200

    def test_an_empty_reason_still_says_something(self) -> None:
        assert turn_notes.failure_reply(_task(), {}) == "这条没能回复：处理时出了问题，稍后再发一次试试。"

    def test_the_engine_reports_the_kind_of_a_model_failure(self) -> None:
        action = TaskAction(action=ACTION_FAIL, node_id="n", error="boom", error_kind="timeout")

        assert action.to_dict()["error_kind"] == "timeout"
        assert "error_kind" not in TaskAction(action=ACTION_FAIL, node_id="n", error="boom").to_dict()


class TestTheHistoryNotice:
    def test_a_failed_round_of_the_user_leaves_a_notice_with_the_cause(self) -> None:
        notice = turn_notes.failure_notice(_task(), "fail")

        assert notice == "[SYSTEM] 这一轮没做完，没有给出回复（连不上模型服务）；用户那边只收到一句报错。"

    @pytest.mark.parametrize("outcome", ["cancelled", "preempted", "finish"])
    def test_only_a_failure_leaves_one(self, outcome: str) -> None:
        assert turn_notes.failure_notice(_task(), outcome) == ""

    @pytest.mark.parametrize("overrides", [
        {"caller_task_id": "parent"},
        {"input": {"_dispatch_origin": {"caller_task_id": "parent"}, "task_context": {}}},
        {"input": {"_system_task": True, "task_context": {}}},
        {"input": {"task_context": {"route_hints": {"delivery_purpose": "ambient"}}}},
    ], ids=["sync-child", "async-child", "system", "interjection"])
    def test_rounds_the_user_did_not_ask_for_leave_none(self, overrides: dict[str, Any]) -> None:
        assert turn_notes.failure_notice(_task(**overrides), "fail") == ""

    def test_the_notice_closes_the_round_once(self) -> None:
        tail = [_message("in", "t-1", "2026-11-01T00:00:00+00:00")]
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)

        first = turn_notes.with_failure_notice(tail, "[SYSTEM] x", identity="i", task_id="t-1", node_id="n", now=now)
        again = turn_notes.with_failure_notice(tail, "[SYSTEM] x", identity="i", task_id="t-1", node_id="n", now=now)

        assert [message.content for message in first] == ["hi", "[SYSTEM] x"]
        assert first[1].id == again[1].id
        assert first[1].message_type == MessageType.SYSTEM and first[1].meta[turn_notes.FAILED_TURN_MARK_KEY] is True
        assert (first[1].source_task_id, first[1].source_node_id) == ("t-1", "n")

    def test_without_the_users_message_in_the_tail_no_notice_is_added(self) -> None:
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)

        assert turn_notes.with_failure_notice([], "[SYSTEM] x", identity="i", task_id="t", node_id="n", now=now) == []
        assert turn_notes.with_failure_notice(
            [_message("in", "t", "2026-11-01T00:00:00+00:00")], "", identity="i", task_id="t", node_id="n", now=now,
        )[-1].id == "in"


class TestLateMerge:
    _START = "2026-10-31T16:07:52+00:00"
    _NOW = datetime(2026, 10, 31, 16, 10, 46, tzinfo=timezone.utc)

    def _marked(self, parent: list[Message], tail: list[Message] | None = None) -> list[Message]:
        tail = tail or [_message("a-in", "a", self._START), _message("a-out", "a", self._START, role="assistant")]
        return turn_notes.mark_late_merge(tail, parent, zone=_CST, now=self._NOW)

    def test_a_round_overtaken_by_a_later_one_gets_its_start_and_end(self) -> None:
        marked = self._marked([_message("b-in", "b", "2026-10-31T16:09:24+00:00")])

        assert marked[0].meta[LATE_MERGE_KEY] == {"started_at": "00:07", "finished_at": "00:10"}
        assert LATE_MERGE_KEY not in marked[1].meta

    def test_rounds_that_started_earlier_change_nothing(self) -> None:
        marked = self._marked([_message("b-in", "b", "2026-10-31T16:00:00+00:00")])

        assert LATE_MERGE_KEY not in marked[0].meta

    @pytest.mark.parametrize("later", [
        _message("note", "b", "2026-10-31T16:09:00+00:00", kind=MessageType.SYSTEM),
        _message("sum", "compact_summary", "2026-10-31T16:09:00+00:00", kind=MessageType.SUMMARY,
                 meta={"compressed_task_ids": ["x"]}),
        _message("reply", "b", "2026-10-31T16:09:00+00:00", role="assistant", kind=MessageType.ASSISTANT),
        _message("own", "a", "2026-10-31T16:09:00+00:00"),
    ], ids=["system-note", "compaction-summary", "assistant", "own-round"])
    def test_what_is_not_another_rounds_input_does_not_count(self, later: Message) -> None:
        assert LATE_MERGE_KEY not in self._marked([later])[0].meta

    def test_a_snipped_round_still_counts_by_its_start(self) -> None:
        snipped = _message("b-sum", "b", "2026-10-31T16:09:24+00:00", kind=MessageType.SUMMARY)

        assert LATE_MERGE_KEY in self._marked([snipped])[0].meta

    def test_a_round_across_midnight_shows_dates(self) -> None:
        meta = late_merge_meta(
            datetime(2026, 10, 31, 15, 50, tzinfo=timezone.utc), datetime(2026, 10, 31, 16, 5, tzinfo=timezone.utc), _CST,
        )

        assert meta == {"started_at": "10-31 23:50", "finished_at": "11-01 00:05"}

    def test_a_tail_without_the_users_message_is_left_alone(self) -> None:
        tail = [_message("x", "a", self._START, role="assistant", kind=MessageType.ASSISTANT)]

        assert self._marked([_message("b-in", "b", "2026-10-31T16:09:24+00:00")], tail) == tail


class TestRendering:
    _MARK = {LATE_MERGE_KEY: {"started_at": "00:07", "finished_at": "00:10"}}
    _NOTE = "[这一轮 00:07 开始、00:10 做完；上面有几轮比它开始得晚，先做完排在了前面]"

    def test_the_note_opens_the_input_it_was_recorded_on(self) -> None:
        history = [{"role": "user", "content": "帮我整理", "_meta": dict(self._MARK)}, {"role": "assistant", "content": "好"}]

        rendered = render_history_inputs(history)

        assert rendered[0]["content"] == f"{self._NOTE}\n帮我整理"
        assert rendered[1] is history[1] and history[0]["content"] == "帮我整理"

    def test_rendering_twice_adds_it_once(self) -> None:
        once = render_history_inputs([{"role": "user", "content": "帮我整理", "_meta": dict(self._MARK)}])

        assert render_history_inputs(once) == once

    def test_a_picture_message_gets_it_as_its_first_part(self) -> None:
        content = [{"type": "image_url", "image_url": {"url": "data:,"}}]

        [rendered] = render_history_inputs([{"role": "user", "content": content, "_meta": dict(self._MARK)}])

        assert rendered["content"] == [{"type": "text", "text": self._NOTE}, *content]

    def test_it_sits_with_the_short_reply_hint(self) -> None:
        meta = {**self._MARK, "response_action": SHORT_REPLY, "message_type": MessageType.USER_INPUT}

        [rendered] = render_history_inputs([{"role": "user", "content": "在吗", "_meta": meta}])

        assert rendered["content"] == f"{self._NOTE}\n在吗\n\n{SHORT_REPLY_HINT}"

    @pytest.mark.parametrize("meta", [None, {}, {LATE_MERGE_KEY: "00:07"}, {LATE_MERGE_KEY: {"started_at": "00:07"}}])
    def test_a_missing_or_broken_mark_shows_nothing(self, meta: Any) -> None:
        assert late_merge_note(meta) == ""


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="turn-notes"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _store(state: SupervisorState) -> ConversationStore:
    return ConversationStore(state.workspace_root / "data" / "conversations")


def _round(state: SupervisorState, session_id: str, *, said_at: str, **extra: Any) -> Task:
    """建一轮入口任务并当作 engine 已领走、已把用户这句写进分支。"""
    payload = {
        "channel": "qq_private", "conversation_key": _CONV, "text": "request",
        "entry_node_id": "qq.orchestrator", "platform_auth": dict(_AUTH), **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    task = state.record_inbound_message_event(event)
    assert task is not None
    task.status = TaskStatus.running
    task.worker_id = "worker"
    _store(state).append(str(task.input["branch_session_id"]), Message(
        id=f"in-{task.task_id}", role="user", content="request", message_type=MessageType.USER_INPUT,
        created_at=said_at, source_task_id=task.task_id,
    ))
    return task


def _outbound(state: SupervisorState) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


class TestThroughTheSupervisor:
    def test_a_failed_round_tells_the_user_plainly_and_leaves_the_notice_in_history(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq_private", conversation_key=_CONV)
        task = _round(state, session_id, said_at="2026-10-31T16:09:24+00:00")

        state.complete_task(task_id=task.task_id, worker_id="worker", result={
            "action": "fail", "error": "PluginTransportError: Provider transport failed (ConnectError)",
            "error_kind": "network",
        })

        merged = _store(state).load(session_id)
        assert [message.content for message in merged] == [
            "request", "[SYSTEM] 这一轮没做完，没有给出回复（连不上模型服务）；用户那边只收到一句报错。",
        ]
        assert merged[1].source_task_id == task.task_id
        assert [item["text"] for item in _outbound(state)] == ["这条没能回复：连不上模型服务，稍后再发一次试试。"]

    def test_a_failed_interjection_says_nothing_and_leaves_no_notice(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq_private", conversation_key=_CONV)
        task = _round(state, session_id, said_at="2026-10-31T16:09:24+00:00", route_hints={"delivery_purpose": "ambient"})

        state.complete_task(task_id=task.task_id, worker_id="worker", result={
            "action": "fail", "error": "boom", "error_kind": "network",
        })

        assert [message.content for message in _store(state).load(session_id)] == ["request"]
        [sent] = _outbound(state)
        assert sent["text"] == "" and sent["action_type"] == "no_reply"

    def test_a_round_finished_after_a_later_one_is_marked(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq_private", conversation_key=_CONV)
        early = _round(state, session_id, said_at="2026-10-31T16:07:52+00:00")
        late = _round(state, session_id, said_at="2026-10-31T16:09:24+00:00")

        for task in (late, early):
            state.complete_task(task_id=task.task_id, worker_id="worker", result={
                "action": "finish", "result": {"text": "done"},
            })

        inputs = {message.source_task_id: message for message in _store(state).load(session_id)}
        assert LATE_MERGE_KEY not in inputs[late.task_id].meta
        assert inputs[early.task_id].meta[LATE_MERGE_KEY]["started_at"].endswith("00:07")
