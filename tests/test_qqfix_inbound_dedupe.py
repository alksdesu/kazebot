"""入站去重：OneBot 重连补发的同一条消息只处理一次；同一个群文件的消息和上传通知只处理一次。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adapters._shared.recent_keys import RecentKeys
from tests._onebot_harness import load_runtime, set_live_config

_GROUP_ID = 70001
_BOT_ID = "900001"


class _UploadEvent(SimpleNamespace):
    pass


class _Finished(Exception):
    pass


class _Matcher:
    def __init__(self) -> None:
        self.finished = 0

    async def finish(self, message: Any = None, **kwargs: Any) -> None:
        self.finished += 1
        raise _Finished


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[_GROUP_ID], enable_file_input=True, allow_private_friends=False)
    monkeypatch.setattr(module.handlers, "GroupUploadNoticeEvent", _UploadEvent)
    return module


def _group_event(message_id: Any = 1, *, group_id: int = _GROUP_ID, segments: list | None = None) -> SimpleNamespace:
    message = segments if segments is not None else [{"type": "text", "data": {"text": "hello"}}]
    return SimpleNamespace(
        user_id=10001, group_id=group_id, message_id=message_id, time=1_700_000_000, anonymous=None,
        sender=SimpleNamespace(nickname="某人", card="", user_id=10001, role="member"),
        get_message=lambda: message,
    )


class TestFirstDelivery:
    def test_the_same_message_is_only_handled_once(self, runtime: Any) -> None:
        bot = SimpleNamespace(self_id=_BOT_ID)
        event = _group_event(5)

        assert runtime.inbound._first_delivery(bot, event) is True
        assert runtime.inbound._first_delivery(bot, _group_event(5)) is False

    @pytest.mark.parametrize("other", ["group", "account", "private"])
    def test_the_key_is_account_conversation_and_message(self, runtime: Any, other: str) -> None:
        bot = SimpleNamespace(self_id=_BOT_ID)
        runtime.inbound._first_delivery(bot, _group_event(5))
        if other == "group":
            assert runtime.inbound._first_delivery(bot, _group_event(5, group_id=_GROUP_ID + 1)) is True
        elif other == "account":
            assert runtime.inbound._first_delivery(SimpleNamespace(self_id="900002"), _group_event(5)) is True
        else:
            assert runtime.inbound._first_delivery(bot, SimpleNamespace(user_id=10001, message_id=5)) is True

    @pytest.mark.parametrize("message_id", [None, "", "  "])
    def test_a_message_without_an_id_is_never_dropped(self, runtime: Any, message_id: Any) -> None:
        bot = SimpleNamespace(self_id=_BOT_ID)

        assert runtime.inbound._first_delivery(bot, _group_event(message_id)) is True
        assert runtime.inbound._first_delivery(bot, _group_event(message_id)) is True

    def test_history_records_a_redelivered_message_once(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[Any] = []

        async def record(bot: Any, event: Any) -> None:
            recorded.append(event)

        monkeypatch.setattr(runtime.handlers, "_record_non_trigger_message", record)
        bot = SimpleNamespace(self_id=_BOT_ID)

        asyncio.run(runtime.handlers._handle_history(bot, _group_event(8)))
        asyncio.run(runtime.handlers._handle_history(bot, _group_event(8)))

        assert len(recorded) == 1

    def test_a_redelivered_private_message_does_not_run_twice(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        matcher = _Matcher()
        monkeypatch.setattr(runtime.handlers, "_private_matcher", matcher)
        event = SimpleNamespace(user_id=40004, message_id=3, sub_type="", sender=SimpleNamespace(role=""), get_message=lambda: [])

        for _ in range(2):
            try:
                asyncio.run(runtime.handlers._handle_private_agent(None, event))
            except _Finished:
                pass

        assert matcher.finished == 1


class TestRecentKeys:
    def test_a_key_is_new_again_after_the_window(self) -> None:
        now = [100.0]
        keys: RecentKeys[str] = RecentKeys(window_sec=10, max_items=10, clock=lambda: now[0])

        assert keys.first("m") is True
        now[0] = 109.0
        assert keys.first("m") is False
        now[0] = 125.0
        assert keys.first("m") is True

    def test_the_oldest_keys_are_evicted_first(self) -> None:
        keys: RecentKeys[int] = RecentKeys(window_sec=1000, max_items=2)

        keys.first(1)
        keys.first(2)
        keys.first(3)

        assert len(keys) == 2
        assert keys.first(1) is True


class TestGroupFiles:
    @staticmethod
    def _file_message(file_id: str = "file-1") -> SimpleNamespace:
        return _group_event(11, segments=[{"type": "file", "data": {"file_id": file_id, "name": "report.txt", "size": 12}}])

    @staticmethod
    def _notice(file_id: str = "file-1") -> _UploadEvent:
        return _UploadEvent(group_id=_GROUP_ID, user_id=10001, time=1_700_000_000,
                            file={"id": file_id, "name": "report.txt", "size": 12})

    def test_the_first_sighting_wins(self, runtime: Any) -> None:
        assert runtime.media._first_group_file(_GROUP_ID, {"file_id": "f"}) is True
        assert runtime.media._first_group_file(_GROUP_ID, {"file_id": "f"}) is False
        assert runtime.media._first_group_file(_GROUP_ID + 1, {"file_id": "f"}) is True
        assert runtime.media._first_group_file(_GROUP_ID, {"file_id": ""}) is True
        assert runtime.media._first_group_file(None, {"file_id": "f"}) is True

    def test_a_notice_after_the_message_is_skipped(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        download = AsyncMock(return_value=([], []))
        monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)
        bot = SimpleNamespace(self_id=_BOT_ID, call_api=AsyncMock())

        asyncio.run(runtime.media._collect_qq_attachments(bot, self._file_message(), "qq_group:conv"))
        before = len(runtime.state._adapter.group_content_records[_GROUP_ID])
        asyncio.run(runtime.handlers._handle_group_upload_notice(bot, self._notice()))

        assert [len(call.args[0]) for call in download.await_args_list] == [1]
        assert len(runtime.state._adapter.group_content_records[_GROUP_ID]) == before

    def test_a_message_after_the_notice_does_not_download_again(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        download = AsyncMock(return_value=([], []))
        monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)
        bot = SimpleNamespace(self_id=_BOT_ID, call_api=AsyncMock())

        asyncio.run(runtime.handlers._handle_group_upload_notice(bot, self._notice()))
        asyncio.run(runtime.media._collect_qq_attachments(bot, self._file_message(), "qq_group:conv"))

        assert [len(call.args[0]) for call in download.await_args_list] == [1, 0]

    def test_a_different_file_is_still_downloaded(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        download = AsyncMock(return_value=([], []))
        monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)
        bot = SimpleNamespace(self_id=_BOT_ID, call_api=AsyncMock())

        asyncio.run(runtime.handlers._handle_group_upload_notice(bot, self._notice("file-1")))
        asyncio.run(runtime.media._collect_qq_attachments(bot, self._file_message("file-2"), "qq_group:conv"))

        assert [len(call.args[0]) for call in download.await_args_list] == [1, 1]

    def test_a_quoted_file_is_never_deduplicated(self, runtime: Any) -> None:
        # 引用一条群文件消息是最明确的「读这个」，引用块直接调下载，不经过去重。
        assert runtime.media._first_group_file(_GROUP_ID, {"file_id": "quoted"}) is True
        assert "_first_group_file" not in runtime.history._build_reply_context.__code__.co_names
