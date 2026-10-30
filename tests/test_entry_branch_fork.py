"""入口分支：这段对话的历史按字节整份复制进去，持着全局锁时只解析父会话上次以来新追加的行；复制不了这一轮判失败。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.conversation_store import ConversationStore, Message
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import Task, TaskStatus

CONV = "qq_private:entry-branch"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}


@pytest.fixture()
def state(tmp_path: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="entry-branch"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    state._defer_post_work_enqueue = True
    return state


def _store(state: SupervisorState) -> ConversationStore:
    return ConversationStore(state.workspace_root / "data" / "conversations")


def _session_with_history(state: SupervisorState, count: int) -> str:
    session_id = state.get_or_create_session(channel="qq_private", conversation_key=CONV)
    _store(state).append_batch(session_id, [
        Message(
            id=f"m{index}", role="system" if index == 0 else ("user" if index % 2 else "assistant"),
            content=f"第 {index} 条",
        )
        for index in range(count)
    ])
    return session_id


def _inbound(state: SupervisorState, session_id: str, text: str = "接着说") -> Task:
    payload = {
        "channel": "qq_private", "conversation_key": CONV, "text": text,
        "entry_node_id": "test.orchestrator", "platform_auth": dict(AUTH),
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    task = state.record_inbound_message_event(event)
    assert task is not None
    return task


def _count_parsing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """从这以后每解析出一条消息就记下它的 id。"""
    parse = Message.from_dict.__func__
    parsed: list[str] = []

    def counting(cls: type[Message], data: dict[str, Any]) -> Message:
        message = parse(cls, data)
        parsed.append(message.id)
        return message

    monkeypatch.setattr(Message, "from_dict", classmethod(counting))
    return parsed


def test_a_round_starts_with_the_whole_history_of_its_conversation(state: SupervisorState) -> None:
    parent = _session_with_history(state, 5)

    task = _inbound(state, parent)

    branch = task.input["branch_session_id"]
    assert [message.id for message in _store(state).load(branch)] == ["m0", "m1", "m2", "m3", "m4"]
    assert (task.input["base_count"], task.input["base_last_id"], task.input["fork_copied"]) == (5, "m4", 5)
    registered = state._session_store.get_entry(branch)
    assert (registered["base_count"], registered["base_last_id"]) == (5, "m4")


def test_under_the_lock_only_what_was_appended_since_is_parsed(
    state: SupervisorState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _session_with_history(state, 40)
    with state._lock:
        state._create_entry_branch_locked(parent, 101)
    # 上一轮的分支合并回来的两条
    _store(state).append_batch(parent, [
        Message(id="merged-ask", role="user", content="上一轮"),
        Message(id="merged-reply", role="assistant", content="答复"),
    ])
    parsed = _count_parsing(monkeypatch)

    with state._lock:
        branch, fork = state._create_entry_branch_locked(parent, 102)

    assert parsed == ["merged-ask", "merged-reply"]
    assert (fork["base_count"], fork["base_last_id"], fork["copied"]) == (42, "merged-reply", 42)
    assert len(_store(state).load(branch)) == 42


def test_a_round_whose_history_cannot_be_copied_fails_instead_of_starting_blind(
    state: SupervisorState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _session_with_history(state, 3)

    def unreadable(self: ConversationStore, source_session_id: str, target_session_id: str) -> Any:
        raise PermissionError("held by another process")

    monkeypatch.setattr(ConversationStore, "copy_history", unreadable)
    task = _inbound(state, parent)

    assert task.status == TaskStatus.failed
    assert task.result["error"] == "没能开始：复制这段对话的历史时读写出错（PermissionError），可以稍后重试"
