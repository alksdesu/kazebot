"""文件下载上下文和失败原因在群通知、引用及历史入口中保持完整。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel

from tests._onebot_harness import load_runtime, set_live_config


_GROUP_ID = 70001
_BOT_ID = "900001"
_ERROR = "文件 report.txt 下载失败，请重新上传。"


class _UploadFile(BaseModel):
    id: str
    name: str
    size: int
    busid: int


class _UploadEvent(SimpleNamespace):
    pass


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[_GROUP_ID], enable_file_input=True)
    monkeypatch.setattr(module.handlers, "GroupUploadNoticeEvent", _UploadEvent)
    return module


def _event(segments, *, group_id=_GROUP_ID):
    return SimpleNamespace(
        user_id=10001, group_id=group_id, message_id=71, time=1_700_000_000,
        sender=SimpleNamespace(nickname="某人", card="", user_id=10001),
        get_message=lambda: segments,
    )


@pytest.mark.parametrize("representation", ["dict", "model", "legacy_model"])
def test_group_upload_preserves_file_metadata_and_context(runtime, monkeypatch, representation):
    data = {"id": "group-file-id", "name": "report.txt", "size": 12, "busid": 102}
    if representation == "model":
        file_info = _UploadFile(**data)
    elif representation == "legacy_model":
        file_info = SimpleNamespace(dict=lambda: dict(data))
    else:
        file_info = dict(data)
    event = _UploadEvent(group_id=_GROUP_ID, user_id=10001, time=1_700_000_000, file=file_info)
    bot = SimpleNamespace(self_id=_BOT_ID, send=AsyncMock(), call_api=AsyncMock())
    attachment = {"type": "file", "path": "data/attachments/report.txt", "name": "report.txt"}
    download = AsyncMock(return_value=([attachment], [_ERROR, _ERROR]))
    monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)

    asyncio.run(runtime.handlers._handle_group_upload_notice(bot, event))

    sources, conversation_key = download.call_args.args
    assert sources == [runtime.media._segment_file_source(data)]
    assert sources[0]["file_id"] == "group-file-id"
    assert sources[0]["size"] == 12
    assert conversation_key == runtime.routing._stable_conversation_key(f"qq_group:{_GROUP_ID}")
    assert download.call_args.kwargs == {"bot": bot, "group_id": _GROUP_ID}
    record = runtime.state._adapter.group_content_records[_GROUP_ID][-1]
    assert record.attachments == [attachment]
    assert record.text.count(_ERROR) == 1
    assert runtime.state._adapter.group_history[_GROUP_ID][-1].text.count(_ERROR) == 1
    bot.send.assert_not_called()
    bot.call_api.assert_not_called()


@pytest.mark.parametrize("group_id", [None, _GROUP_ID])
@pytest.mark.parametrize("image", ["absent", "unresolved", "failed", "downloaded"])
def test_quoted_file_errors_are_visible_without_consuming_image_placeholders(
    runtime, monkeypatch, group_id, image,
):
    segments = [{"type": "file", "data": {"file_id": "file-id", "name": "report.txt"}}]
    if image != "absent":
        segments.insert(0, {"type": "image", "data": {"url": "https://example.org/a.png"}})
    event = _event([{"type": "reply", "data": {"id": "70"}}], group_id=group_id)
    bot = SimpleNamespace(self_id=_BOT_ID)
    reply = {"message": segments, "sender": {"user_id": "10002", "nickname": "原作者"}}
    monkeypatch.setattr(runtime.message_text, "_resolve_reply_payload", AsyncMock(return_value=reply))
    monkeypatch.setattr(runtime.message_text, "_get_reply_message", AsyncMock(return_value=None))
    image_attachment = {"type": "image", "name": "a.png", "path": "data/attachments/a.png"}
    image_error = "图片过大。"
    image_result = ([image_attachment] if image == "downloaded" else [], [image_error] if image == "failed" else [])
    monkeypatch.setattr(runtime.media, "_image_sources_to_attachments", AsyncMock(return_value=image_result))
    download = AsyncMock(return_value=([], [_ERROR, _ERROR]))
    monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)

    text, attachments = asyncio.run(runtime.history._build_reply_context(event, bot, "opaque-conversation-key"))

    assert download.call_args.kwargs == {"bot": bot, "group_id": group_id}
    assert download.call_args.args[1] == "opaque-conversation-key"
    assert text.count(_ERROR) == 1
    assert "report.txt" in text
    if image == "unresolved":
        assert runtime.message_text.IMAGE_PLACEHOLDER in text
    elif image == "failed":
        assert f"[{image_error}]" in text
        assert runtime.message_text.IMAGE_PLACEHOLDER not in text
    elif image == "downloaded":
        assert attachments == [image_attachment]
        assert image_attachment["path"] in text
    else:
        assert attachments == []


@pytest.mark.parametrize("original_text", ["读一下文件", ""])
def test_non_trigger_attachment_errors_enter_history_without_changing_echo(runtime, monkeypatch, original_text):
    event = _event([{"type": "text", "data": {"text": original_text}}])
    bot = SimpleNamespace(self_id=_BOT_ID, send=AsyncMock(), call_api=AsyncMock())
    download = AsyncMock(return_value=([], [_ERROR, _ERROR]))
    monkeypatch.setattr(runtime.media, "_collect_qq_attachments", download)
    echo = AsyncMock()
    combat = AsyncMock()
    monkeypatch.setattr(runtime.handlers, "_maybe_echo_group_message", echo)
    monkeypatch.setattr(runtime.emotes, "_maybe_join_sticker_combat", combat)

    asyncio.run(runtime.handlers._record_non_trigger_message(bot, event))

    assert download.call_args.kwargs == {"expand_forward": False}
    record = runtime.state._adapter.group_content_records[_GROUP_ID][-1]
    assert record.text.startswith(original_text)
    assert record.text.count(_ERROR) == 1
    assert runtime.state._adapter.group_history[_GROUP_ID][-1].text.count(_ERROR) == 1
    echo.assert_awaited_once_with(bot, event, original_text)
    combat.assert_awaited_once_with(bot, event, has_image=False, text=original_text)
    bot.send.assert_not_called()
    bot.call_api.assert_not_called()


@pytest.mark.parametrize("source", ["message", "notice"])
def test_history_errors_survive_compaction_and_anonymization(runtime, monkeypatch, source):
    set_live_config(runtime, history_text_limit=50)
    private_id = "1234567890"
    alias = runtime.anonymize._anonymize_user_id(private_id)
    error = f"文件 {private_id}.txt 下载失败，请重新上传。"
    expected_error = error.replace(private_id, alias)
    bot = SimpleNamespace(self_id=_BOT_ID, send=AsyncMock(), call_api=AsyncMock())
    download = AsyncMock(return_value=([], [error, error]))
    if source == "message":
        original_text = "长" * 1300
        event = _event([{"type": "text", "data": {"text": original_text}}])
        monkeypatch.setattr(runtime.media, "_collect_qq_attachments", download)
        stickers = Mock()
        echo = AsyncMock()
        combat = AsyncMock()
        monkeypatch.setattr(runtime.emotes, "_collect_stickers", stickers)
        monkeypatch.setattr(runtime.handlers, "_maybe_echo_group_message", echo)
        monkeypatch.setattr(runtime.emotes, "_maybe_join_sticker_combat", combat)

        asyncio.run(runtime.handlers._record_non_trigger_message(bot, event))

        assert stickers.call_args.args[0].text == original_text
        echo.assert_awaited_once_with(bot, event, original_text)
        combat.assert_awaited_once_with(bot, event, has_image=False, text=original_text)
        expected_body = "长" * 1200 + "…"
    else:
        event = _UploadEvent(
            group_id=_GROUP_ID, user_id=10001, time=1_700_000_000,
            file={"id": "file-id", "name": "长" * 1300 + ".txt", "size": 12},
        )
        monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", download)

        asyncio.run(runtime.handlers._handle_group_upload_notice(bot, event))

        file_name = runtime.media._segment_file_source(event.file)["name"]
        expected_body = runtime.message_text._segment_to_text("file", {"name": file_name}, set())
    record = runtime.state._adapter.group_content_records[_GROUP_ID][-1]
    formatted_line = runtime.state._adapter.group_history[_GROUP_ID][-1].text
    assert record.text == f"{expected_body} {expected_error}"
    assert formatted_line.endswith(f"{expected_body[:50]}… {expected_error}")
    assert record.formatted_line == formatted_line
    assert formatted_line.count(expected_error) == 1
    assert private_id not in record.text
    assert private_id not in formatted_line
    bot.send.assert_not_called()
    bot.call_api.assert_not_called()


def test_quoted_file_error_survives_long_message_compaction(runtime, monkeypatch):
    set_live_config(runtime, history_text_limit=50)
    sender_id = "1234567890"
    alias = runtime.anonymize._anonymize_user_id(sender_id)
    error = f"文件 {sender_id}.txt 下载失败，请重新上传。"
    reply = {"message": [
        {"type": "text", "data": {"text": "长消息" * 100}},
        {"type": "file", "data": {"id": "file-id", "name": "report.txt"}},
    ], "sender": {"user_id": "10002", "nickname": "原作者"}}
    monkeypatch.setattr(runtime.message_text, "_resolve_reply_payload", AsyncMock(return_value=reply))
    monkeypatch.setattr(runtime.message_text, "_get_reply_message", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", AsyncMock(return_value=([], [error])))
    event = _event([{"type": "reply", "data": {"id": "70"}}])

    text, attachments = asyncio.run(runtime.history._build_reply_context(
        event, SimpleNamespace(self_id=_BOT_ID), "opaque-conversation-key",
    ))

    assert error.replace(sender_id, alias) in text
    assert sender_id not in text
    assert "…" in text
    assert attachments == []


@pytest.mark.parametrize("refresh", ["message", "raw_message", "empty", "failure"])
def test_quoted_file_id_refresh_preserves_original_images(runtime, monkeypatch, refresh):
    event = _event([{"type": "reply", "data": {"id": "70"}}])
    event.reply = {"message": [
        {"type": "image", "data": {"url": "https://example.org/original.png"}},
        {"type": "file", "data": {"file_id": "old-id", "name": "report.txt"}},
    ], "sender": {"user_id": "10002", "nickname": "原作者"}}
    refreshed_segments = [
        {"type": "image", "data": {"url": "https://example.org/refreshed.png"}},
        {"type": "file", "data": {"file_id": "new-id", "name": "report.txt"}},
    ]
    if refresh == "failure":
        call_api = AsyncMock(side_effect=RuntimeError("get_msg unavailable"))
    else:
        call_api = AsyncMock(return_value={refresh: refreshed_segments} if refresh != "empty" else {})
    bot = SimpleNamespace(self_id=_BOT_ID, call_api=call_api)
    images = AsyncMock(return_value=([], []))
    files = AsyncMock(return_value=([], []))
    monkeypatch.setattr(runtime.media, "_image_sources_to_attachments", images)
    monkeypatch.setattr(runtime.media, "_file_sources_to_attachments", files)

    asyncio.run(runtime.history._build_reply_context(event, bot, "opaque-conversation-key"))

    call_api.assert_awaited_once_with("get_msg", message_id=70)
    expected_id = "new-id" if refresh in {"message", "raw_message"} else "old-id"
    assert files.call_args.args[0][0]["file_id"] == expected_id
    assert images.call_args.args[0][0].url == "https://example.org/original.png"
