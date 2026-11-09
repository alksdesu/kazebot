"""没有内容的 QQ 消息事件：私聊直接丢掉，群里只 @ 了 bot 的照实写明，都不替人补「你好」。

NapCat 在私聊发完文件后会推来不带任何消息段的事件，以前被补成「你好」当成用户亲口说的。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _contract_drivers import QQDriver  # noqa: E402

_LOGGER = "nonebot.plugin.clonoth_agent"


class _Finished(Exception):
    pass


class _Matcher:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def finish(self, message: Any = None, **_kwargs: Any) -> None:
        if message is not None:
            self.said.append(str(message))
        raise _Finished

    async def send(self, message: Any = None, **_kwargs: Any) -> None:
        if message is not None:
            self.said.append(str(message))


@pytest.fixture()
def driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[QQDriver]:
    opened = QQDriver(monkeypatch, tmp_path)
    try:
        yield opened
    finally:
        opened.close()


def _event(driver: QQDriver, segments: list[Any], *, group: bool, message_id: int, to_me: bool) -> SimpleNamespace:
    sender = driver.member if group else driver.admin
    message = driver._message(segments)
    fields: dict[str, Any] = {"group_id": int(driver.room)} if group else {}
    return SimpleNamespace(
        **fields, user_id=int(sender), message_id=message_id, time=int(time.time()), self_id=int(driver.bot_account),
        message_type="group" if group else "private", sub_type="normal" if group else "friend",
        raw_message="", to_me=to_me, reply=None, anonymous=None,
        sender=SimpleNamespace(user_id=int(sender), nickname=f"member-{sender}", card="", role="member"),
        get_message=lambda: message,
    )


def _dropped_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "without content ignored" in record.getMessage()]


# --- 私聊 ---


def test_a_private_event_without_segments_is_dropped(
    driver: QQDriver, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    matcher = _Matcher()
    monkeypatch.setattr(driver.runtime.handlers, "_private_matcher", matcher)
    # NoneBot 给私聊一律置 to_me：照它判「@ 了 bot」的话，这条空事件会被当成在找 bot。
    event = _event(driver, [], group=False, message_id=301, to_me=True)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(_Finished):
            asyncio.run(driver.runtime.handlers._handle_private_agent(driver.bot, event))

    assert driver.client.submissions == []
    assert matcher.said == []
    assert _dropped_lines(caplog) == [
        "QQ message event without content ignored: kind=private message_id=301 segments=[]",
    ]


@pytest.mark.parametrize("text", ["", "   "])
def test_a_private_message_with_only_blank_text_is_dropped(driver: QQDriver, text: str) -> None:
    replies = asyncio.run(driver.private_message(text, sender=driver.admin, message_id="302"))

    assert driver.client.submissions == []
    assert replies == []


def test_a_private_message_with_words_is_still_submitted(driver: QQDriver) -> None:
    asyncio.run(driver.private_message("在吗", sender=driver.admin, message_id="303"))

    [call] = driver.client.submissions
    assert call["request_text"].endswith(": 在吗")
    assert "你好" not in call["text"]


# --- 群聊 ---


def test_a_bare_group_mention_is_submitted_as_a_bare_mention(driver: QQDriver) -> None:
    asyncio.run(driver.group_message("", sender=driver.member, message_id="304", mention_bot=True))

    [call] = driver.client.submissions
    bare = driver.runtime.inbound._BARE_MENTION_TEXT
    assert call["request_text"].endswith(f": {bare}")
    assert bare in call["text"]
    assert "你好" not in call["text"]


def test_a_contentless_group_event_that_got_through_is_dropped_and_refunded(
    driver: QQDriver, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    refunds: list[tuple[Any, Any]] = []
    monkeypatch.setattr(driver.runtime.inbound, "_refund_proactive", lambda group_id, stamp: refunds.append((group_id, stamp)))
    matcher = _Matcher()
    event = _event(driver, [], group=True, message_id=305, to_me=False)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(_Finished):
            asyncio.run(driver.runtime.handlers._process_group_message(
                driver.bot, event, matcher, trigger_source="random", proactive_stamp=12.5,
            ))

    assert driver.client.submissions == []
    assert matcher.said == []
    assert refunds == [(int(driver.room), 12.5)]
    assert _dropped_lines(caplog) == [
        "QQ message event without content ignored: kind=group message_id=305 segments=[]",
    ]
