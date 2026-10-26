"""supervisor 重启时还没收束的入口分支：补上缺结果的调用和一句说明，合并回父会话再删，告诉用户说「继续」接着做；认不出合并起点的照旧整个丢掉。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.interrupted_calls import INTERRUPTED_MARK_KEY, INTERRUPTED_RESULT_TEXT  # noqa: E402
from supervisor.branch_recovery import (  # noqa: E402
    RESTART_HISTORY_NOTICE,
    RESTART_MARK_KEY,
    RESTART_USER_NOTICE,
    merge_base,
)
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task  # noqa: E402

CONV = "qq_private:9"


def _state(workspace: Path, run_id: str) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id=run_id),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _store(workspace: Path) -> ConversationStore:
    return ConversationStore(workspace / "data" / "conversations")


def _inbound(state: SupervisorState, session_id: str) -> Task:
    payload = {
        "channel": "qq_private",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "把日志模块拆成两个文件",
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _earlier_turn() -> list[Message]:
    return [
        Message(id="old-u", role="user", content="上一轮的问题"),
        Message(id="old-a", role="assistant", content="上一轮的回答"),
    ]


def _interrupted_round(task: Task) -> list[Message]:
    """重启前写下的一轮：用户输入、一次发了两个调用的 assistant、只来得及写回的一个结果。"""
    return [
        Message(id="u1", role="user", content="把日志模块拆成两个文件", source_task_id=task.task_id),
        Message(
            id="a1", role="assistant", content="", source_task_id=task.task_id, meta={"tool_mode": "native"},
            tool_calls=[{"id": "c1", "name": "read_file", "arguments": {}}, {"id": "c2", "name": "execute_command", "arguments": {}}],
        ),
        Message(id="r1", role="tool", content="文件内容", source_task_id=task.task_id, tool_call_id="c1", name="read_file"),
    ]


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


class TestRecovery:
    def test_an_interrupted_branch_is_merged_with_its_dangling_calls_answered(self, tmp_path: Path) -> None:
        first = _state(tmp_path, "run-a")
        sid = first.get_or_create_session(channel="qq_private", conversation_key=CONV)
        _store(tmp_path).append_batch(sid, _earlier_turn())
        task = _inbound(first, sid)
        branch = task.session_id
        _store(tmp_path).append_batch(branch, _interrupted_round(task))

        restarted = _state(tmp_path, "run-b")

        merged = _store(tmp_path).load(sid)
        assert [message.id for message in merged[:5]] == ["old-u", "old-a", "u1", "a1", "r1"]
        answer, notice = merged[5], merged[6]
        assert len(merged) == 7
        assert answer.role == "tool" and answer.tool_call_id == "c2" and answer.content == INTERRUPTED_RESULT_TEXT
        assert answer.meta["tool_status"] == "interrupted" and answer.meta[INTERRUPTED_MARK_KEY] is True
        assert notice.role == "user" and notice.content == RESTART_HISTORY_NOTICE
        assert notice.meta[RESTART_MARK_KEY] == branch and notice.source_task_id == task.task_id
        assert restarted._session_store.get_entry(branch) is None
        assert branch not in restarted.sessions
        assert not (tmp_path / "data" / "conversations" / f"{branch}.jsonl").exists()
        assert _outbound_texts(restarted).count(RESTART_USER_NOTICE) == 1

    def test_a_second_restart_finds_nothing_left_to_do(self, tmp_path: Path) -> None:
        first = _state(tmp_path, "run-a")
        sid = first.get_or_create_session(channel="qq_private", conversation_key=CONV)
        task = _inbound(first, sid)
        _store(tmp_path).append_batch(task.session_id, _interrupted_round(task))
        _state(tmp_path, "run-b")
        merged = [message.id for message in _store(tmp_path).load(sid)]

        third = _state(tmp_path, "run-c")

        assert [message.id for message in _store(tmp_path).load(sid)] == merged
        assert _outbound_texts(third).count(RESTART_USER_NOTICE) == 1

    def test_a_branch_with_nothing_new_is_dropped_quietly(self, tmp_path: Path) -> None:
        first = _state(tmp_path, "run-a")
        sid = first.get_or_create_session(channel="qq_private", conversation_key=CONV)
        _store(tmp_path).append_batch(sid, _earlier_turn())
        branch = _inbound(first, sid).session_id

        restarted = _state(tmp_path, "run-b")

        assert [message.id for message in _store(tmp_path).load(sid)] == ["old-u", "old-a"]
        assert restarted._session_store.get_entry(branch) is None
        assert not (tmp_path / "data" / "conversations" / f"{branch}.jsonl").exists()
        assert RESTART_USER_NOTICE not in _outbound_texts(restarted)

    def test_a_branch_whose_fork_point_cannot_be_found_is_dropped(self, tmp_path: Path) -> None:
        first = _state(tmp_path, "run-a")
        sid = first.get_or_create_session(channel="qq_private", conversation_key=CONV)
        store = _store(tmp_path)
        store.append_batch(sid, _earlier_turn())
        task = _inbound(first, sid)
        branch = task.session_id
        store.append_batch(branch, _interrupted_round(task))
        # 父会话在 fork 之后被整段改写（比如压缩），登记的起点也对不上了
        store.replace_all(sid, [Message(id="summary", role="user", content="前面的摘要")])
        first._session_store.set_entry_field(branch, "base_count", 99, flush=False)
        first._session_store.set_entry_field(branch, "base_last_id", "gone")

        restarted = _state(tmp_path, "run-b")

        assert [message.id for message in _store(tmp_path).load(sid)] == ["summary"]
        assert restarted._session_store.get_entry(branch) is None
        assert RESTART_USER_NOTICE not in _outbound_texts(restarted)

    def test_an_agent_conversation_is_merged_without_telling_anyone(self, tmp_path: Path) -> None:
        first = _state(tmp_path, "run-a")
        agent_sid = first.get_or_create_session(channel="qq_private", conversation_key=f"agent:helper.node:{CONV}:x1")
        task = _inbound(first, agent_sid)
        _store(tmp_path).append_batch(task.session_id, _interrupted_round(task))

        restarted = _state(tmp_path, "run-b")

        assert "a1" in [message.id for message in _store(tmp_path).load(agent_sid)]
        assert RESTART_USER_NOTICE not in _outbound_texts(restarted)

    @pytest.mark.parametrize("with_new_content", [True, False], ids=["merged", "dropped"])
    def test_a_kept_child_of_the_branch_moves_to_the_conversation(self, tmp_path: Path, with_new_content: bool) -> None:
        first = _state(tmp_path, "run-a")
        sid = first.get_or_create_session(channel="qq_private", conversation_key=CONV)
        task = _inbound(first, sid)
        branch = task.session_id
        child_sid, _ = first.get_or_create_child_session(branch, "helper.node", "", "fresh")
        first._session_store.set_entry_field(child_sid, "retain", {
            "task_id": "child-task", "parent_task_id": task.task_id, "node_id": "helper.node", "kind": "child",
            "context_mode": "fresh", "reason": "failed", "since": datetime.now(timezone.utc).isoformat(),
            "until": "", "family_root": sid,
        })
        _store(tmp_path).append_batch(child_sid, [Message(id="c-u", role="user", content="帮忙查一下")])
        if with_new_content:
            _store(tmp_path).append_batch(branch, _interrupted_round(task))

        restarted = _state(tmp_path, "run-b")

        entry = restarted._session_store.get_entry(child_sid)
        assert entry is not None and entry["parent_session_id"] == sid
        # 上一进程的任务都不在了，宽限期从这次启动起算
        left = datetime.fromisoformat(entry["retain"]["until"]) - datetime.now(timezone.utc)
        assert timedelta(hours=23) < left <= timedelta(hours=24)
        assert child_sid in restarted.parent_children.get(sid, set())
        assert [message.id for message in _store(tmp_path).load(child_sid)] == ["c-u"]
        assert restarted._session_store.get_entry(branch) is None
        with restarted._lock:
            assert restarted._session_family_root_locked(child_sid) == sid


def _messages(*ids: str) -> list[Message]:
    return [Message(id=item, role="user", content=item) for item in ids]


class TestMergeBase:
    def test_the_boundary_marker_wins(self) -> None:
        branch = _messages("p1", "p2", "n1")
        branch[0] = Message(id="p1", role="user", content="p1", meta={"_entry_branch_base": "branch_5"})

        assert merge_base("branch_5", {"base_count": 2, "base_last_id": "p2"}, branch, _messages("p1", "p2")) == 1

    def test_the_recorded_count_is_checked_against_the_last_id(self) -> None:
        branch = _messages("p1", "p2", "n1")

        assert merge_base("branch_5", {"base_count": 2, "base_last_id": "p2"}, branch, []) == 2
        assert merge_base("branch_5", {"base_count": 2, "base_last_id": ""}, branch, []) == 2
        assert merge_base("branch_5", {"base_count": 0}, branch, _messages("x")) == 0

    def test_a_recorded_count_that_does_not_match_falls_back_to_the_last_id(self) -> None:
        branch = _messages("p1", "p2", "p3", "n1")

        assert merge_base("branch_5", {"base_count": 2, "base_last_id": "p3"}, branch, []) == 3
        assert merge_base("branch_5", {"base_count": 99, "base_last_id": "p3"}, branch, []) == 3
        assert merge_base("branch_5", {"base_count": "x", "base_last_id": "p3"}, branch, []) == 3

    @pytest.mark.parametrize("entry", [{}, {"base_count": True}, {"base_last_id": "gone"}])
    def test_without_a_usable_record_the_shared_prefix_is_used(self, entry: dict[str, Any]) -> None:
        assert merge_base("branch_5", entry, _messages("p1", "p2", "n1"), _messages("p1", "p2")) == 2

    def test_an_empty_parent_means_everything_is_new(self) -> None:
        assert merge_base("branch_5", {}, _messages("n1", "n2"), []) == 0

    def test_nothing_shared_with_a_non_empty_parent_is_unrecognisable(self) -> None:
        assert merge_base("branch_5", {}, _messages("n1", "n2"), _messages("summary")) is None
