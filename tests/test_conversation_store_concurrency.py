from __future__ import annotations

import multiprocessing
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine.conversation_store as store_module
from engine.conversation_store import ConversationChangedError, ConversationStore, Message


def _message(message_id: str) -> Message:
    return Message(id=message_id, role="user", content=message_id)


def _ids(store: ConversationStore) -> list[str]:
    return [message.id for message in store.load("s")]


def _append_from_process(directory: str, prefix: str) -> None:
    store = ConversationStore(directory)
    for index in range(12):
        store.append("s", _message(f"{prefix}-{index}"))


@pytest.mark.parametrize("external_action", ["append", "replace", "delete"])
def test_append_never_renews_an_outdated_cache(tmp_path: Path, external_action: str) -> None:
    reader = ConversationStore(tmp_path)
    writer = ConversationStore(tmp_path)
    reader.append("s", _message("old"))
    assert _ids(reader) == ["old"]
    if external_action == "append":
        writer.append("s", _message("external"))
        expected = ["old", "external", "new"]
    elif external_action == "replace":
        writer.replace_all("s", [_message("summary")])
        expected = ["summary", "new"]
    else:
        writer.delete("s")
        expected = ["new"]

    reader.append("s", _message("new"))
    assert _ids(reader) == expected
    reader.replace_all("s", reader.load("s"))
    assert _ids(ConversationStore(tmp_path)) == expected


def test_replacement_rejects_a_snapshot_changed_by_another_writer(tmp_path: Path) -> None:
    reader = ConversationStore(tmp_path)
    writer = ConversationStore(tmp_path)
    reader.append("s", _message("old"))
    reader.load("s")
    writer.append("s", _message("new"))

    with pytest.raises(ConversationChangedError):
        reader.replace_all("s", [_message("summary")])

    assert _ids(reader) == ["old", "new"]
    assert _ids(ConversationStore(tmp_path)) == ["old", "new"]


@pytest.mark.parametrize("failure", ["serialize", "flush", "replace"])
def test_failed_replacement_preserves_existing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    store = ConversationStore(tmp_path)
    store.append("s", _message("old"))
    store.load("s")
    messages = [_message("summary")]

    def fail(*args, **kwargs):
        raise OSError("injected write failure")

    if failure == "serialize":
        messages.append(Message(id="invalid", role="user", content=object()))
    else:
        monkeypatch.setattr(store_module.os, "fsync" if failure == "flush" else "replace", fail)

    with pytest.raises((OSError, TypeError)):
        store.replace_all("s", messages)

    assert _ids(store) == ["old"]
    assert _ids(ConversationStore(tmp_path)) == ["old"]
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_replacement_and_ephemeral_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.append("s", _message("old"))
    store.replace_all("s", [Message(id="transient", role="user", content="", ephemeral=True)])
    assert _ids(store) == []
    store.append("s", _message("new"))
    store.replace_all("s", [])
    assert _ids(ConversationStore(tmp_path)) == []


def test_failed_replacement_reloads_original_content_after_cached_messages_are_edited(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    store.append("s", _message("old"))
    messages = store.load("s")
    messages[0].content = "modified"
    messages.append(Message(id="invalid", role="user", content=object()))

    with pytest.raises(TypeError):
        store.replace_all("s", messages)

    assert len(store.load("s")) == 1
    assert store.load("s")[0].content == "old"


def test_append_waits_for_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConversationStore(tmp_path)
    store.append("s", _message("old"))
    replacing = threading.Event()
    release = threading.Event()
    append_started = threading.Event()
    original_replace = store_module.os.replace

    def paused_replace(source, destination):
        replacing.set()
        assert release.wait(5)
        original_replace(source, destination)

    def append() -> None:
        append_started.set()
        ConversationStore(tmp_path).append("s", _message("new"))

    monkeypatch.setattr(store_module.os, "replace", paused_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement = pool.submit(store.replace_all, "s", [_message("summary")])
        assert replacing.wait(5)
        addition = pool.submit(append)
        try:
            assert append_started.wait(5)
            assert not addition.done()
        finally:
            release.set()
        replacement.result(timeout=5)
        addition.result(timeout=5)

    assert _ids(ConversationStore(tmp_path)) == ["summary", "new"]


def test_processes_append_without_losing_or_corrupting_messages(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_from_process, args=(str(tmp_path), str(index)))
        for index in range(3)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            process.close()

    assert set(_ids(ConversationStore(tmp_path))) == {
        f"{prefix}-{index}" for prefix in range(3) for index in range(12)
    }
    assert len(_ids(ConversationStore(tmp_path))) == 36
