"""QQ 分条提示、消息排版与分段重试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

GROUP_ID = 700001
USER_ID = 30003
MESSAGE_ID = 99
INBOUND_SEQ = 17


class SplitBot:
    self_id = "10000"

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.attempts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_member_list":
            assert kwargs["group_id"] == GROUP_ID
            return [{"user_id": USER_ID, "nickname": "测试用户", "card": ""}]
        raise AssertionError(f"Unexpected fake API: {api}")

    async def send_group_msg(self, *, group_id: int, message: Any) -> dict[str, str]:
        return self._record("group", group_id, message)

    async def send_private_msg(self, *, user_id: int, message: Any) -> dict[str, str]:
        return self._record("private", user_id, message)

    def _record(self, kind: str, target: int, message: Any) -> dict[str, str]:
        row = {"kind": kind, "target": target, "segments": list(message)}
        self.attempts.append(row)
        if self.failure is not None and len(self.attempts) == 2:
            raise self.failure
        self.sent.append(row)
        return {"message_id": str(len(self.sent))}


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    assert not module._features.available
    assert Path(module._outbound_idempotency.path).is_relative_to(tmp_path)
    set_live_config(module, reply_to_trigger=True)
    yield module
    module._outbound_idempotency.close()


def _event(kind: str) -> SimpleNamespace:
    event = SimpleNamespace(
        user_id=USER_ID, message_id=MESSAGE_ID, time=1_700_000_000,
        sender=SimpleNamespace(user_id=USER_ID, nickname="测试用户", card="", role="member"),
        get_message=lambda: [],
    )
    if kind == "group":
        event.group_id = GROUP_ID
    return event


def _target(kind: str) -> dict[str, Any]:
    field, identity = ("group_id", GROUP_ID) if kind == "group" else ("user_id", USER_ID)
    return {
        "type": kind, field: identity, "conversation_key": f"qq_{kind}:{identity}",
        "reply_message_id": MESSAGE_ID,
    }


def _context(runtime: Any, target: dict[str, Any]) -> Any:
    return runtime.OutboundSendContext(
        event_id="split-reply", idempotency_key="delivery:split-reply",
        source_inbound_seq=INBOUND_SEQ, conversation_key=target["conversation_key"],
        reply_message_id=str(MESSAGE_ID),
    )


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    return [
        "".join(segment["data"]["text"] for segment in row["segments"] if segment["type"] == "text").strip()
        for row in messages
    ]


def _replies(messages: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [str(segment["data"]["id"]) for segment in row["segments"] if segment["type"] == "reply"]
        for row in messages
    ]


@pytest.mark.parametrize("entry", ["group", "private", "draw_group", "draw_private"])
def test_every_inbound_injects_one_shared_split_prompt(runtime: Any, entry: str) -> None:
    kind = entry.removeprefix("draw_")
    event = _event(kind)
    target = _target(kind)

    async def build() -> str:
        if entry.startswith("draw_"):
            return await runtime._build_draw_direct_inbound_text(event, "画一片云", kind == "private")
        if kind == "group":
            text, _ = await runtime._build_inbound_text(
                event, SplitBot(), "在吗", target["conversation_key"], [],
            )
            return text
        return await runtime._build_private_inbound_text(
            event, SplitBot(), "在吗", target["conversation_key"], [],
        )

    text = asyncio.run(build())
    block = runtime._reply_format_prompt_block()
    assert text.count(block) == 1
    assert text.count("【QQ回复分条】") == 1
    assert runtime._SPLIT_SIGNAL in block


def test_split_prompt_preserves_single_reply_and_formatted_content(runtime: Any) -> None:
    block = runtime._reply_format_prompt_block()
    for required in (
        "普通换行", "一个消息气泡", "finish.text", "reply.text", "标记不会显示",
        "通常一条", "2–3 条", "不要逐句刷屏", "代码、列表、引用", "保留普通换行",
        "简短接话", "只回一句", "只发一条",
    ):
        assert required in block
    assert f"不使用 {runtime._SPLIT_SIGNAL}" in block


def test_prompt_and_sender_use_the_same_split_signal(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    signal = "[QQ_TEST_SPLIT]"
    monkeypatch.setattr(runtime, "_SPLIT_SIGNAL", signal)
    block = runtime._reply_format_prompt_block()
    assert signal in block
    assert "[SPLIT]" not in block
    bot = SplitBot()
    target = _target("private")

    asyncio.run(runtime._send_split_text(
        bot, target, f"收到{signal}我看看", send_context=_context(runtime, target),
    ))

    assert _texts(bot.sent) == ["收到", "我看看"]


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize(("text", "expected"), [
    pytest.param("第一句\n第二句", ["第一句\n第二句"], id="lf"),
    pytest.param("第一句\r\n第二句", ["第一句\r\n第二句"], id="crlf"),
    pytest.param("第一段\n\n第二段", ["第一段\n\n第二段"], id="blank-line"),
    pytest.param("第一句[SPLIT]第二句", ["第一句", "第二句"], id="explicit-split"),
    pytest.param("[SPLIT]第一句[SPLIT] [SPLIT]第二句[SPLIT]", ["第一句", "第二句"], id="empty-parts"),
    pytest.param("收到[SPLIT]收到", ["收到", "收到"], id="repeated-text"),
    pytest.param("1. 第一步\n2. 第二步", ["1. 第一步\n2. 第二步"], id="list"),
    pytest.param("```python\nx = 1\nprint(x)\n```", ["x = 1\nprint(x)"], id="code"),
    pytest.param("> 第一行引用\n> 第二行引用", ["> 第一行引用\n> 第二行引用"], id="quote"),
    pytest.param(" \n ", [], id="empty-text"),
    pytest.param("[SPLIT] [SPLIT]", [], id="only-separators"),
])
def test_sender_splits_only_explicit_markers(runtime: Any, kind: str, text: str, expected: list[str]) -> None:
    bot = SplitBot()
    target = _target(kind)

    sent = asyncio.run(runtime._send_split_text(
        bot, target, text, send_context=_context(runtime, target),
    ))

    assert sent is bool(expected)
    assert _texts(bot.sent) == expected
    assert _replies(bot.sent) == ([[str(MESSAGE_ID)]] + [[] for _ in expected[1:]] if expected else [])
    identity = GROUP_ID if kind == "group" else USER_ID
    assert all(row["kind"] == kind and row["target"] == identity for row in bot.sent)


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("enabled", [False, True])
def test_only_first_nonempty_part_can_quote_trigger(runtime: Any, kind: str, enabled: bool) -> None:
    set_live_config(runtime, reply_to_trigger=enabled)
    bot = SplitBot()
    target = _target(kind)

    asyncio.run(runtime._send_split_text(
        bot, target, "[SPLIT] \n [SPLIT]第一句[SPLIT]第二句",
        send_context=_context(runtime, target),
    ))

    assert _texts(bot.sent) == ["第一句", "第二句"]
    assert _replies(bot.sent) == ([[str(MESSAGE_ID)], []] if enabled else [[], []])


async def _deliver(runtime: Any, bot: SplitBot, kind: str, callback: str, text: str) -> None:
    target = _target(kind)
    context = _context(runtime, target)
    callbacks = runtime.TangQiuCallbacks()
    if callback == "send_to_channel":
        runtime._last_bot = bot
        await callbacks.send_to_channel(target["conversation_key"], text, [], delivery_context=context)
        return
    trigger = runtime.TriggerInfo(
        INBOUND_SEQ, target["conversation_key"], "split-session", kind == "private",
        platform_data={**target, "bot": bot, "event": _event(kind)},
    )
    if callback == "send_reply":
        await callbacks.send_reply(trigger, text, [], delivery_context=context)
    else:
        await callbacks.send_intermediate_reply(trigger, text, delivery_context=context)


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("callback", ["send_reply", "send_intermediate_reply", "send_to_channel"])
@pytest.mark.parametrize(("text", "expected"), [
    ("第一句\n第二句", ["第一句\n第二句"]),
    ("第一句[SPLIT]第二句", ["第一句", "第二句"]),
])
def test_callbacks_preserve_split_and_deduplicate_delivery(
    runtime: Any, kind: str, callback: str, text: str, expected: list[str],
) -> None:
    bot = SplitBot()

    async def exercise() -> None:
        await _deliver(runtime, bot, kind, callback, text)
        assert _texts(bot.sent) == expected
        await _deliver(runtime, bot, kind, callback, text)
        assert _texts(bot.sent) == expected
        assert len(bot.attempts) == len(expected)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("callback", ["send_reply", "send_intermediate_reply", "send_to_channel"])
def test_retry_sends_only_failed_part_and_final_waiter_tracks_completion(
    runtime: Any, kind: str, callback: str,
) -> None:
    failure = runtime.classify_send_exception(RuntimeError("ENOENT: no such file or directory"))
    assert failure.definitely_not_sent
    bot = SplitBot(failure)
    target = _target(kind)
    waiter = runtime.QueueReplyWaiter(asyncio.Event(), INBOUND_SEQ)
    other_waiter = runtime.QueueReplyWaiter(asyncio.Event(), INBOUND_SEQ + 1)
    runtime._qq_waiting_replies[target["conversation_key"]] = waiter
    runtime._qq_waiting_replies["unrelated-session"] = other_waiter

    async def exercise() -> None:
        with pytest.raises(type(failure), match="ENOENT"):
            await _deliver(runtime, bot, kind, callback, "第一句[SPLIT]第二句")
        assert _texts(bot.sent) == ["第一句"]
        assert _texts(bot.attempts) == ["第一句", "第二句"]
        assert not waiter.event.is_set()
        assert INBOUND_SEQ not in runtime._qq_completed_replies

        await _deliver(runtime, bot, kind, callback, "第一句[SPLIT]第二句")
        assert _texts(bot.sent) == ["第一句", "第二句"]
        assert _texts(bot.attempts) == ["第一句", "第二句", "第二句"]
        assert _replies(bot.sent) == [[str(MESSAGE_ID)], []]
        assert waiter.event.is_set() is (callback != "send_intermediate_reply")
        assert not other_waiter.event.is_set()

        await _deliver(runtime, bot, kind, callback, "第一句[SPLIT]第二句")
        assert len(bot.attempts) == 3

    asyncio.run(exercise())
