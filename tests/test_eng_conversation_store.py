"""对话存储：后台按序写、读之前先落盘、半行不吞下一条、锁文件不随会话数增长，整份复制历史时只数新追加的行。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine.builtin.preempt import _inject_preempt_message
from engine.conversation_store import ConversationStore, ConversationWriter, HistoryCopy, Message, MessageType
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


def _staged_files(directory: Path) -> list[Path]:
    return sorted(directory.glob(".fork.*.tmp"))


def _parent_history(store: ConversationStore) -> None:
    store.append_batch("parent", [
        Message(id="sys", role="system", content="persona"),
        Message(id="ask", role="user", content="帮我查一下"),
        Message(id="answer", role="assistant", content="好的"),
    ])


def test_a_staged_fork_installs_the_source_as_it_was_when_staged(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)

    staged = store.stage_fork("parent")
    store.append("parent", Message(id="later", role="user", content="派出去之后才写的"))

    assert (staged.count, staged.source_found) == (2, True)
    assert store.install_fork(staged, "child") == 2
    assert [message.id for message in ConversationStore(tmp_path).load("child")] == ["ask", "answer"]
    ConversationStore.discard_fork(staged)
    assert _staged_files(tmp_path) == []


def test_a_fork_never_lands_on_a_target_that_already_has_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    store.append("child", Message(id="own", role="user", content="自己的"))

    staged = store.stage_fork("parent")
    try:
        assert store.install_fork(staged, "child") == 0
    finally:
        ConversationStore.discard_fork(staged)

    assert [message.id for message in ConversationStore(tmp_path).load("child")] == ["own"]
    assert _staged_files(tmp_path) == []


def test_a_fork_keeps_unreadable_leftovers_in_the_target_and_lands_after_them(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    (tmp_path / "child.jsonl").write_bytes(b'{"id": "torn", "role": "us')

    staged = store.stage_fork("parent")
    assert store.install_fork(staged, "child") == 2
    ConversationStore.discard_fork(staged)

    assert (tmp_path / "child.jsonl").read_bytes().startswith(b'{"id": "torn", "role": "us\n')
    assert [message.id for message in ConversationStore(tmp_path).load("child")] == ["ask", "answer"]
    assert _staged_files(tmp_path) == []


def test_staging_a_source_without_messages_writes_nothing(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.append("only-system", Message(id="sys", role="system", content="persona"))

    missing = store.stage_fork("nobody")
    empty = store.stage_fork("only-system")

    assert (missing.path, missing.count, missing.source_found) == (None, 0, False)
    assert (empty.path, empty.count, empty.source_found) == (None, 0, True)
    assert store.install_fork(missing, "child") == 0
    assert not (tmp_path / "child.jsonl").exists()


def test_has_messages_needs_one_readable_message(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)

    assert store.has_messages("session") is False
    (tmp_path / "session.jsonl").write_text("\n   \nnot json\n", encoding="utf-8")
    assert store.has_messages("session") is False
    store.append("session", Message(id="m", role="user", content="x"))
    assert store.has_messages("session") is True


def test_fork_still_copies_everything_but_system_messages_once(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)

    assert store.fork("parent", "child") == 2
    assert store.fork("parent", "child") == 0
    assert [message.id for message in ConversationStore(tmp_path).load("child")] == ["ask", "answer"]
    assert _staged_files(tmp_path) == []


def _count_parsing(monkeypatch) -> list[str]:
    """从这以后每解析出一条消息就记下它的 id。"""
    parse = Message.from_dict.__func__
    parsed: list[str] = []

    def counting(cls, data):
        message = parse(cls, data)
        parsed.append(message.id)
        return message

    monkeypatch.setattr(Message, "from_dict", classmethod(counting))
    return parsed


def test_copy_history_copies_the_file_as_it_is_system_messages_included(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)

    copied = store.copy_history("parent", "branch")

    assert copied == HistoryCopy(count=3, last_id="answer", copied=3)
    assert (tmp_path / "branch.jsonl").read_bytes() == (tmp_path / "parent.jsonl").read_bytes()
    assert [message.id for message in ConversationStore(tmp_path).load("branch")] == ["sys", "ask", "answer"]
    assert _staged_files(tmp_path) == []


def test_copy_history_counts_exactly_what_load_will_read(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    # 内容里原样的行分隔符不算换行，\r\n 算；空行、读不出的行和半行都跳过
    separator = json.dumps({"id": "sep", "role": "user", "content": "a\u2028b"}, ensure_ascii=False)
    with (tmp_path / "parent.jsonl").open("ab") as stream:
        stream.write(b"\n   \nnot json\n[1, 2]\n" + separator.encode("utf-8") + b"\n")
        stream.write(b'{"id": "crlf", "role": "user", "content": "x"}\r\n{"id": "torn", "role": "us')

    copied = store.copy_history("parent", "branch")

    loaded = ConversationStore(tmp_path).load("branch")
    assert [message.id for message in loaded] == ["sys", "ask", "answer", "sep", "crlf"]
    assert (copied.count, copied.last_id) == (len(loaded), loaded[-1].id)


def test_copy_history_after_appends_only_reads_the_new_lines(tmp_path: Path, monkeypatch) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    store.copy_history("parent", "first")
    store.append_batch("parent", [
        Message(id="more", role="user", content="接着问"), Message(id="reply", role="assistant", content="接着答"),
    ])
    parsed = _count_parsing(monkeypatch)

    copied = store.copy_history("parent", "second")

    assert parsed == ["more", "reply"]
    assert copied == HistoryCopy(count=5, last_id="reply", copied=5)


def test_a_rewritten_source_is_counted_from_what_was_written(tmp_path: Path, monkeypatch) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    store.copy_history("parent", "first")
    kept = store.load("parent")[1:]
    store.replace_all("parent", [Message(id="summary", role="user", content="前情提要"), *kept])
    parsed = _count_parsing(monkeypatch)

    copied = store.copy_history("parent", "second")

    assert parsed == []
    assert copied == HistoryCopy(count=3, last_id="answer", copied=3)
    assert [message.id for message in ConversationStore(tmp_path).load("second")] == ["summary", "ask", "answer"]


def test_a_file_rewritten_on_the_same_inode_is_counted_again(tmp_path: Path) -> None:
    # 删了再建可能拿回同一个 inode 号：之前数过的那段换了内容，就得从头数
    store = ConversationStore(tmp_path)
    _parent_history(store)
    store.copy_history("parent", "first")
    replacement = "".join(
        json.dumps(Message(id=f"new-{index}", role="user", content="换过的内容" * 20).to_dict(), ensure_ascii=False) + "\n"
        for index in range(4)
    ).encode("utf-8")
    with (tmp_path / "parent.jsonl").open("r+b") as stream:
        stream.write(replacement)
        stream.truncate()

    copied = store.copy_history("parent", "second")

    assert copied == HistoryCopy(count=4, last_id="new-3", copied=4)


def test_copy_history_leaves_a_target_with_messages_alone(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    _parent_history(store)
    store.append("branch", Message(id="own", role="user", content="自己的"))

    copied = store.copy_history("parent", "branch")

    assert copied == HistoryCopy(count=1, last_id="own", copied=0)
    assert [message.id for message in ConversationStore(tmp_path).load("branch")] == ["own"]
    assert _staged_files(tmp_path) == []


def test_copy_history_from_a_source_without_messages_writes_nothing(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    (tmp_path / "blank.jsonl").write_text("\nnot json\n", encoding="utf-8")

    assert store.copy_history("nobody", "first") == HistoryCopy(0, "", 0)
    assert store.copy_history("blank", "second") == HistoryCopy(0, "", 0)
    assert not (tmp_path / "first.jsonl").exists() and not (tmp_path / "second.jsonl").exists()
    assert _staged_files(tmp_path) == []


def test_an_installed_fork_is_counted_from_what_was_staged(tmp_path: Path, monkeypatch) -> None:
    # 异步 fork 委派的 agent 会话一装好，紧接着就要从它建入口分支
    store = ConversationStore(tmp_path)
    _parent_history(store)
    staged = store.stage_fork("parent")
    store.install_fork(staged, "agent")
    ConversationStore.discard_fork(staged)
    parsed = _count_parsing(monkeypatch)

    copied = store.copy_history("agent", "branch")

    assert parsed == []
    assert copied == HistoryCopy(count=2, last_id="answer", copied=2)
