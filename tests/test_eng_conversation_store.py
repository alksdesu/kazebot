"""对话存储：后台按序写、读之前先落盘、半行不吞下一条、锁文件不随会话数增长。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.builtin.preempt import _inject_preempt_message
from engine.conversation_store import ConversationStore, ConversationWriter, Message, MessageType
from engine.inference.ai_step import _shadow_write
from engine.inference.loop_state import conversation_writer, flush_conversation_writes
from engine.node import Node


def test_an_append_after_a_torn_last_line_keeps_the_new_message(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.append("session", Message(id="first", role="user", content="first"))
    with (tmp_path / "session.jsonl").open("ab") as stream:
        stream.write(b'{"id": "torn", "role": "user", "content": "cut of')

    store.append("session", Message(id="second", role="user", content="second"))

    assert [message.id for message in ConversationStore(tmp_path).load("session")] == ["first", "second"]


def test_the_writer_keeps_submission_order_and_flush_waits_for_all_of_it(tmp_path: Path) -> None:
    writer = ConversationWriter(ConversationStore(tmp_path))

    async def run() -> list[str]:
        for index in range(20):
            writer.submit("session", Message(id=f"m{index}", role="user", content=str(index)))
        await writer.flush()
        return [message.id for message in ConversationStore(tmp_path).load("session")]

    assert asyncio.run(run()) == [f"m{index}" for index in range(20)]


def test_a_failed_write_is_logged_and_later_writes_still_land(tmp_path: Path, monkeypatch, caplog) -> None:
    store = ConversationStore(tmp_path)
    original = store.append

    def flaky(session_id: str, message: Message) -> None:
        if message.id == "bad":
            raise OSError("disk full")
        original(session_id, message)

    monkeypatch.setattr(store, "append", flaky)
    writer = ConversationWriter(store)

    async def run() -> None:
        writer.submit("session", Message(id="bad", role="user", content="x"))
        writer.submit("session", Message(id="good", role="user", content="y"))
        await writer.flush()

    with caplog.at_level(logging.ERROR, logger="engine.conversation_store"):
        asyncio.run(run())

    assert [message.id for message in ConversationStore(tmp_path).load("session")] == ["good"]
    assert "conversation write failed" in caplog.text


def test_without_a_running_loop_the_writer_writes_inline(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    ConversationWriter(store).submit("session", Message(id="a", role="user", content="x"))
    assert [message.id for message in store.load("session")] == ["a"]


def _shadow_state(store: ConversationStore) -> SimpleNamespace:
    rctx = SimpleNamespace(conversation_store=store, child_session_id="", session_id="session", task_id="task", task_context={})
    return SimpleNamespace(rctx=rctx, node=SimpleNamespace(id="node"), last_shadow_message_id="")


def test_shadow_write_persists_the_message_as_it_was_when_submitted(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    state = _shadow_state(store)
    message = {"role": "user", "content": [{"type": "text", "text": "before"}]}

    async def run() -> None:
        _shadow_write(state, message, MessageType.USER_INPUT)
        message["content"][0]["text"] = "after"
        message["_meta"]["late"] = True
        await flush_conversation_writes(state.rctx)

    asyncio.run(run())

    stored = store.load("session")[0]
    assert stored.content == [{"type": "text", "text": "before"}]
    assert "late" not in stored.meta and stored.source_task_id == "task"
    assert state.rctx.first_shadow_message_id == state.rctx.last_shadow_message_id == stored.id


def test_dynamic_and_ephemeral_messages_are_not_persisted(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    state = _shadow_state(store)

    async def run() -> None:
        _shadow_write(state, {"role": "user", "content": "context", "_dynamic": True})
        _shadow_write(state, {"role": "user", "content": "retry", "_ephemeral": True})
        await flush_conversation_writes(state.rctx)

    asyncio.run(run())

    assert store.load("session") == []


def test_one_writer_per_task_is_reused(tmp_path: Path) -> None:
    rctx = SimpleNamespace(conversation_store=ConversationStore(tmp_path))
    assert conversation_writer(rctx) is conversation_writer(rctx)
    assert conversation_writer(SimpleNamespace()) is None


def test_session_locks_are_striped_into_a_fixed_set_of_files(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    paths = {store._lock_path(f"branch-{index}") for index in range(2000)}
    assert len(paths) <= 256
    assert all(path.parent == tmp_path / ".locks" for path in paths)
    assert store._lock_path("same") == store._lock_path("same")


def test_idle_expiry_only_deletes_files_untouched_since_the_cutoff(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.append("old", Message(id="a", role="user", content="a"))
    store.append("fresh", Message(id="b", role="user", content="b"))
    os.utime(tmp_path / "old.jsonl", (1_000, 1_000))
    cutoff = 2_000.0

    assert store.delete_if_idle_since("old", cutoff)
    assert not store.delete_if_idle_since("fresh", cutoff)
    assert not store.delete_if_idle_since("missing", cutoff)

    assert not (tmp_path / "old.jsonl").exists()
    assert store.load("old") == []
    assert [message.id for message in store.load("fresh")] == ["b"]


def test_a_preempt_message_is_on_disk_before_the_supervisor_is_told(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "data" / "conversations")
    seen: list[list] = []

    async def acknowledge(*_args: object, **_kwargs: object) -> None:
        seen.append([message.content for message in ConversationStore(tmp_path / "data" / "conversations").load("branch")])

    prompt = [{"role": "system", "content": "persona"}]
    loop = SimpleNamespace(
        rctx=SimpleNamespace(
            workspace_root=tmp_path, task_context={}, session_id="branch", child_session_id="", task_id="task",
            conversation_store=store, consume_preempt=AsyncMock(side_effect=acknowledge), emit_event=AsyncMock(),
        ),
        messages=list(prompt), history=[], system_prompt=prompt, is_block_mode=False,
        node=Node(id="entry", type="ai"), runtime_cfg={}, preempt_inject_info={"message": "new question"},
    )

    asyncio.run(_inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop))

    assert seen == [["new question"]]
