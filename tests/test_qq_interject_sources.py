"""插话（没人叫 bot）和直接找 bot 按触发来源分开：引用和 @、点赞、剥前缀、尾句、/生图、排队和重启恢复。

触发来源从判定一路带到发送，这里在 _process_group_message 交给排队的那一件、排队合并、真正发出去的消息段三处各钉一遍。
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
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

from _onebot_harness import load_runtime, patch_body, patch_imported, set_live_config  # noqa: E402
from adapters._shared import outbound  # noqa: E402
from clonoth_sdk.state import SessionState  # noqa: E402

GROUP_ID = 7
USER_QQ = 10001
BOT_QQ = "900001"
MESSAGE_ID = 42
INBOUND_SEQ = 31
REAL_KEY = f"qq_group:{GROUP_ID}"
QUEUE_KEY = "conv-interject"
EXPLICIT_SOURCES = ("at", "reply", "prefix", "name")
SOFT_SOURCES = ("keyword", "random", "llm_intent", "all")


class _Sender:
    def __init__(self, name: str = "小明") -> None:
        self.user_id = USER_QQ
        self.card = name
        self.nickname = name
        self.role = "member"


class _Event:
    """够判定、handler 和发送用的最小群消息事件。to_me 就是被 @。"""

    def __init__(self, text: str, *, at_me: bool = False, message_id: int = MESSAGE_ID) -> None:
        self.group_id = GROUP_ID
        self.user_id = USER_QQ
        self.message_type = "group"
        self.message_id = message_id
        self.time = 1_700_000_000
        self.sender = _Sender()
        self.to_me = at_me
        self.reply = None
        self._message = [SimpleNamespace(type="text", data={"text": text})]

    def get_message(self):
        return self._message


class _Bot:
    self_id = BOT_QQ

    def __init__(self) -> None:
        self.sent: list[list[dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_member_list":
            return [{"user_id": USER_QQ, "nickname": "小明", "card": ""}]
        raise AssertionError(f"unexpected API call: {api}")

    async def send_group_msg(self, *, group_id: int, message: Any) -> dict[str, str]:
        assert group_id == GROUP_ID
        self.sent.append(list(message))
        return {"message_id": str(100 + len(self.sent))}


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    patch_imported(monkeypatch, module, "GroupMessageEvent", _Event, raising=False)
    set_live_config(module, allowed_groups=[GROUP_ID], reply_to_trigger=True)
    yield module
    module.state._adapter.outbound_idempotency.close()


async def _nothing(*args: Any, **kwargs: Any) -> None:
    return None


def _returning(value: Any):
    async def call(*args: Any, **kwargs: Any) -> Any:
        return value

    return call


def _types(message: list[dict[str, Any]]) -> list[str]:
    return [str(segment["type"]) for segment in message]


def _text_of(message: list[dict[str, Any]]) -> str:
    return "".join(str(segment["data"].get("text") or "") for segment in message if segment["type"] == "text").strip()


def _last_line(text: str) -> str:
    return text.rstrip().splitlines()[-1]


def _target(**extra: Any) -> dict[str, Any]:
    return {
        "type": "group", "group_id": GROUP_ID, "conversation_key": REAL_KEY,
        "reply_message_id": MESSAGE_ID, "reply_sender_id": USER_QQ, **extra,
    }


def _context(runtime: Any, name: str = "interject") -> Any:
    return runtime.state.OutboundSendContext(
        event_id=name, idempotency_key=f"delivery:{name}",
        source_inbound_seq=INBOUND_SEQ, conversation_key=REAL_KEY, reply_message_id=str(MESSAGE_ID),
    )


class TestWhoGetsQuoted:
    @pytest.mark.parametrize("source", EXPLICIT_SOURCES)
    def test_someone_calling_the_bot_is_quoted(self, runtime, source: str) -> None:
        assert runtime.sending._quotes_trigger(_target(_trigger_source=source)) is True

    @pytest.mark.parametrize("source", SOFT_SOURCES)
    def test_an_interjection_is_not_quoted(self, runtime, source: str) -> None:
        # 没人叫 bot 的回复也引用并 @ 的话，像是在点名回人家。
        assert runtime.sending._quotes_trigger(_target(_trigger_source=source)) is False

    def test_a_target_without_a_source_follows_the_master_switch(self, runtime) -> None:
        # 主动推送和重启前排进去的触发没记来源，照旧只看总开关。
        assert runtime.sending._quotes_trigger(_target()) is True

        set_live_config(runtime, reply_to_trigger=False)

        assert runtime.sending._quotes_trigger(_target()) is False

    def test_the_master_switch_still_wins(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger=False)

        assert runtime.sending._quotes_trigger(_target(_trigger_source="at")) is False

    def test_there_is_nothing_to_quote_without_the_trigger_message(self, runtime) -> None:
        target = _target(_trigger_source="at")
        del target["reply_message_id"]

        assert runtime.sending._quotes_trigger(target) is False

    def test_the_source_list_takes_effect_without_a_restart(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger_sources=["llm_intent"])

        assert runtime.sending._quotes_trigger(_target(_trigger_source="llm_intent")) is True
        assert runtime.sending._quotes_trigger(_target(_trigger_source="at")) is False

    def test_an_empty_source_list_quotes_nobody(self, runtime) -> None:
        set_live_config(runtime, reply_to_trigger_sources=[])

        assert runtime.sending._quotes_trigger(_target(_trigger_source="at")) is False


class TestWhatGoesOut:
    @pytest.mark.parametrize(("source", "quoted"), [
        ("at", True), ("prefix", True), ("llm_intent", False), ("keyword", False),
    ])
    def test_only_the_first_part_can_quote_and_only_when_someone_called(
        self, runtime, source: str, quoted: bool,
    ) -> None:
        bot = _Bot()

        asyncio.run(runtime.sending._send_split_text(
            bot, _target(_trigger_source=source), f"第一句{outbound.SPLIT_MARKER}第二句",
            send_context=_context(runtime),
        ))

        first, second = bot.sent
        assert ("reply" in _types(first)) is quoted
        assert ("at" in _types(first)) is quoted
        assert "reply" not in _types(second) and "at" not in _types(second)
        assert [_text_of(first), _text_of(second)] == ["第一句", "第二句"]

    @pytest.mark.parametrize(("source", "purpose", "quoted"), [
        ("llm_intent", "ambient", False),
        ("at", "direct", True),
    ])
    def test_the_final_reply_follows_the_source_recorded_on_the_trigger(
        self, runtime, source: str, purpose: str, quoted: bool,
    ) -> None:
        bot = _Bot()
        trigger = runtime.queueing.TriggerInfo(
            INBOUND_SEQ, REAL_KEY, "session-interject", False,
            platform_data={
                "type": "group", "group_id": GROUP_ID, "conversation_key": REAL_KEY,
                "bot": bot, "event": _Event("今天好热"), "_trigger_source": source, "_response_purpose": purpose,
            },
        )

        asyncio.run(runtime.callbacks.TangQiuCallbacks().send_reply(
            trigger, "是挺热的", [], delivery_context=_context(runtime, f"final-{source}"),
        ))

        assert len(bot.sent) == 1
        assert ("reply" in _types(bot.sent[0])) is quoted
        assert ("at" in _types(bot.sent[0])) is quoted
        assert _text_of(bot.sent[0]) == "是挺热的"


class TestTheSourceReachesTheTarget:
    def test_a_group_target_carries_the_source(self, runtime) -> None:
        target = runtime.queueing._target_from_platform_data({
            "type": "group", "group_id": GROUP_ID, "event": _Event("x"),
            "_trigger_source": "keyword", "_response_purpose": "ambient",
        })

        assert target["_trigger_source"] == "keyword"
        assert target["_response_purpose"] == "ambient"
        assert target["reply_message_id"] == MESSAGE_ID

    def test_an_untyped_legacy_group_target_carries_it_too(self, runtime) -> None:
        target = runtime.queueing._target_from_platform_data({"group_id": GROUP_ID, "_trigger_source": "at"})

        assert target["_trigger_source"] == "at"

    def test_an_empty_source_is_left_out(self, runtime) -> None:
        target = runtime.queueing._target_from_platform_data({
            "type": "group", "group_id": GROUP_ID, "_trigger_source": "",
        })

        assert "_trigger_source" not in target


class _Finished(Exception):
    """站 nonebot FinishedException 的位：matcher.finish 之后不会再有代码执行。"""


class _Matcher:
    def __init__(self) -> None:
        self.finished: list[str] = []
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def finish(self, message: str = "") -> None:
        self.finished.append(message)
        raise _Finished


@dataclass
class _Turn:
    item: Any
    liked: list[int]
    matcher: _Matcher


class _Pipeline:
    """跑一遍 _process_group_message，交给排队那一步的那一件留下来看；提交的结果由 outcome 决定。"""

    def __init__(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runtime = runtime
        self.monkeypatch = monkeypatch
        self.liked: list[int] = []
        self.outcome: Any = True
        runtime.state._adapter.client = SimpleNamespace()
        runtime.state._adapter.session_state = SessionState()
        # 第一次认账号会清掉按账号算的进程内状态，主动回复的戳也在里面：先认好，测试里记下的戳才不会被冲掉。
        runtime.routing._adopt_bot_scope(_Bot())

        def like(bot: Any, user_id: int):
            self.liked.append(user_id)
            return _nothing()

        monkeypatch.setattr(runtime.inbound, "_auto_like_user", like)
        monkeypatch.setattr(runtime.message_text, "_remember_message_for_reply_context", lambda *a, **k: None)
        monkeypatch.setattr(runtime.media, "_collect_qq_attachments", _returning(([], [])))
        monkeypatch.setattr(runtime.media, "_remember_recent_images", lambda *a, **k: None)
        monkeypatch.setattr(runtime.media, "_merge_recent_attachments_after_text", _nothing)
        # 这几条本地命令要读写别的状态，和触发来源无关。
        for name in (
            "_maybe_handle_clear_group_memory_command", "_maybe_handle_model_command",
            "_maybe_handle_drawtools_command", "_maybe_handle_custom_face_command",
            "_maybe_handle_proactive_command",
        ):
            patch_body(monkeypatch, runtime, name, _nothing)

    def run(
        self, text: str, *, at_me: bool = False, trigger_source: str = "", proactive_stamp: float | None = None,
    ) -> _Turn:
        submitted: list[Any] = []

        async def enqueue(item: Any) -> bool:
            submitted.append(item)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

        self.monkeypatch.setattr(self.runtime.queueing, "_enqueue_or_submit_inbound", enqueue)
        self.monkeypatch.setattr(self.runtime.message_text, "_event_text_with_forward", _returning(text))
        self.liked.clear()
        matcher = _Matcher()
        with pytest.raises(_Finished):
            asyncio.run(self.runtime.handlers._process_group_message(
                _Bot(), _Event(text, at_me=at_me), matcher,
                trigger_source=trigger_source, proactive_stamp=proactive_stamp,
            ))
        return _Turn(submitted[0] if submitted else None, list(self.liked), matcher)


@pytest.fixture()
def pipeline(runtime, monkeypatch: pytest.MonkeyPatch) -> _Pipeline:
    return _Pipeline(runtime, monkeypatch)


class TestTheTurnHandedToTheQueue:
    def test_an_interjection_ends_with_the_interject_tail(self, runtime, pipeline: _Pipeline) -> None:
        turn = pipeline.run("今天好热", trigger_source="llm_intent")

        assert _last_line(turn.item.text) == runtime.inbound._INTERJECT_REQUEST
        assert runtime.inbound._REPLY_REQUEST not in turn.item.text
        assert turn.item.platform_updates["_response_purpose"] == "ambient"
        assert turn.item.platform_updates["_trigger_source"] == "llm_intent"

    def test_being_called_keeps_the_reply_tail(self, runtime, pipeline: _Pipeline) -> None:
        turn = pipeline.run("今天好热", at_me=True, trigger_source="at")

        assert _last_line(turn.item.text) == runtime.inbound._REPLY_REQUEST
        assert runtime.inbound._INTERJECT_REQUEST not in turn.item.text
        assert turn.item.platform_updates["_response_purpose"] == "direct"
        assert turn.item.platform_updates["_trigger_source"] == "at"

    @pytest.mark.parametrize("source", ["keyword", "random"])
    def test_the_other_soft_triggers_are_interjections_too(self, runtime, pipeline: _Pipeline, source: str) -> None:
        turn = pipeline.run("今天好热", trigger_source=source)

        assert _last_line(turn.item.text) == runtime.inbound._INTERJECT_REQUEST
        assert turn.item.platform_updates["_response_purpose"] == "ambient"
        assert turn.liked == []

    def test_an_interjection_never_likes_the_speaker(self, pipeline: _Pipeline) -> None:
        # 对方根本没在找 bot。
        assert pipeline.run("今天好热", trigger_source="llm_intent").liked == []

    def test_being_called_likes_the_speaker_as_before(self, pipeline: _Pipeline) -> None:
        assert pipeline.run("在吗", at_me=True, trigger_source="at").liked == [USER_QQ]

    def test_a_caller_without_a_source_gets_one_worked_out(self, runtime, pipeline: _Pipeline) -> None:
        # 连发续接和旧调用方不带判定结论进来，照判定规则补算。
        set_live_config(runtime, llm_intent_enabled=True)

        called = pipeline.run("在吗", at_me=True)
        chatted = pipeline.run("今天好热")

        assert called.item.platform_updates["_trigger_source"] == "at"
        assert called.item.platform_updates["_response_purpose"] == "direct"
        assert chatted.item.platform_updates["_trigger_source"] == "llm_intent"
        assert chatted.item.platform_updates["_response_purpose"] == "ambient"

    def test_the_proactive_stamp_rides_with_the_turn(self, runtime, pipeline: _Pipeline) -> None:
        presence = runtime.state._adapter.trigger_presence
        stamp = presence.record_proactive(GROUP_ID, 1000.0)

        turn = pipeline.run("今天好热", trigger_source="llm_intent", proactive_stamp=stamp)

        assert turn.item.platform_updates["_proactive_stamp"] == stamp
        assert presence.proactive_count(GROUP_ID, 1000.0, 300.0) == 1


class TestTheLeadingPrefix:
    def test_a_prefix_trigger_is_stripped(self, runtime, pipeline: _Pipeline) -> None:
        set_live_config(runtime, signal_prefix=True)

        turn = pipeline.run("！帮我看看")

        assert turn.item.platform_updates["_trigger_source"] == "prefix"
        assert turn.item.user_text == "帮我看看"

    def test_an_exclamation_stays_while_the_prefix_signal_is_off(self, runtime, pipeline: _Pipeline) -> None:
        # 前缀信号关着时句首的「！」是语气，剥了就改了原话。
        set_live_config(runtime, llm_intent_enabled=True)

        turn = pipeline.run("！今天好热", trigger_source="llm_intent")

        assert turn.item.user_text == "！今天好热"
        assert "！今天好热" in turn.item.text

    def test_a_mention_that_starts_with_a_live_prefix_is_still_stripped(self, runtime, pipeline: _Pipeline) -> None:
        # 判定顺序里 @ 排在前缀前面，来源报的是 at；前缀信号开着，那个「！」照样是触发标记。
        set_live_config(runtime, signal_prefix=True)

        turn = pipeline.run("！帮我看看", at_me=True, trigger_source="at")

        assert turn.item.user_text == "帮我看看"


class TestTheDrawShortcut:
    def test_being_called_goes_straight_to_the_draw_node(self, runtime, pipeline: _Pipeline) -> None:
        turn = pipeline.run("/生图 一只猫", at_me=True, trigger_source="at")

        assert turn.item.entry_node_id == runtime.common.DRAW_NODE_ID

    def test_an_interjection_goes_through_the_entry_node(self, runtime, pipeline: _Pipeline) -> None:
        # 默认前缀表里有 /，以 / 开头的消息本身就算直接找 bot；去掉 / 之后它才可能是插话接住的。
        set_live_config(runtime, trigger_prefixes=["!", "！"], llm_intent_enabled=True)

        turn = pipeline.run("/生图 一只猫", trigger_source="llm_intent")

        assert turn.item.entry_node_id == ""
        assert turn.item.platform_updates["_response_purpose"] == "ambient"
        assert _last_line(turn.item.text) == runtime.inbound._INTERJECT_REQUEST


class TestWhenTheTurnIsNotTaken:
    def test_a_refused_interjection_finishes_quietly(self, pipeline: _Pipeline) -> None:
        pipeline.outcome = False

        turn = pipeline.run("今天好热", trigger_source="llm_intent")

        assert turn.matcher.finished == [""]

    def test_a_refused_request_still_says_so(self, pipeline: _Pipeline) -> None:
        pipeline.outcome = False

        turn = pipeline.run("在吗", at_me=True, trigger_source="at")

        assert turn.matcher.finished == ["Clonoth Agent 未接受本次请求。"]

    def test_a_broken_connection_is_not_announced_for_an_interjection(self, pipeline: _Pipeline) -> None:
        # 群里冒出一句没人问过的报错，比没接话更糟。
        pipeline.outcome = RuntimeError("supervisor down")

        turn = pipeline.run("今天好热", trigger_source="llm_intent")

        assert turn.matcher.finished == [""]

    def test_a_broken_connection_is_reported_when_someone_asked(self, pipeline: _Pipeline) -> None:
        pipeline.outcome = RuntimeError("supervisor down")

        turn = pipeline.run("在吗", at_me=True, trigger_source="at")

        assert turn.matcher.finished == ["无法连接到 Clonoth Agent：supervisor down"]

    @pytest.mark.parametrize("outcome", [False, RuntimeError("supervisor down")])
    def test_the_proactive_reply_is_given_back(self, runtime, pipeline: _Pipeline, outcome: Any) -> None:
        presence = runtime.state._adapter.trigger_presence
        stamp = presence.record_proactive(GROUP_ID, 1000.0)
        pipeline.outcome = outcome

        pipeline.run("今天好热", trigger_source="llm_intent", proactive_stamp=stamp)

        assert presence.proactive_count(GROUP_ID, 1000.0, 300.0) == 0


class TestTheTail:
    def test_the_interject_tail_leaves_length_and_delivery_to_the_engine(self, runtime) -> None:
        # 字数上限和「直接用 finish」由 engine 补在同一条 user 消息里，两边各写一份会互相打架。
        tail = runtime.inbound._INTERJECT_REQUEST

        assert not any(char.isdigit() for char in tail)
        assert "finish" not in tail

    def test_only_the_last_tail_is_swapped_back(self, runtime) -> None:
        inbound = runtime.inbound
        quoted = f"引用里恰好有这句：{inbound._INTERJECT_REQUEST}"
        text = f"{quoted}\n\n{inbound._INTERJECT_REQUEST}\n\n【排队期间追加消息】\n后来那句"

        assert inbound._as_direct_request(text) == (
            f"{quoted}\n\n{inbound._REPLY_REQUEST}\n\n【排队期间追加消息】\n后来那句"
        )

    def test_text_without_the_tail_is_left_alone(self, runtime) -> None:
        assert runtime.inbound._as_direct_request("正文") == "正文"

    def test_the_builder_switches_the_tail_on_the_purpose(self, runtime) -> None:
        inbound = runtime.inbound

        async def build(ambient: bool) -> str:
            built = await inbound._build_inbound_text(
                _Event("今天好热"), _Bot(), "今天好热", REAL_KEY, [], ambient=ambient,
            )
            return built.text

        assert _last_line(asyncio.run(build(True))) == inbound._INTERJECT_REQUEST
        assert _last_line(asyncio.run(build(False))) == inbound._REPLY_REQUEST


def _queued(
    runtime: Any, purpose: str, *, ref: int, source: str = "", stamp: float | None = None, text: str = "",
) -> Any:
    updates: dict[str, Any] = {
        "type": "group", "group_id": GROUP_ID, "_response_purpose": purpose,
        "_route_hints": {"source_message_refs": [str(ref)]},
    }
    if source:
        updates["_trigger_source"] = source
    if stamp is not None:
        updates["_proactive_stamp"] = stamp
    body = text or f"正文{ref}"
    return runtime.models.QueuedInbound(
        matcher=None, bot=SimpleNamespace(self_id=BOT_QQ), event=_Event(body, message_id=ref), channel="qq_group",
        real_conversation_key=REAL_KEY, stable_conversation_key=QUEUE_KEY,
        text=body, attachments=[], is_dm=False, platform_updates=updates, user_text=body,
    )


def _enqueue(runtime: Any, item: Any) -> bool:
    return asyncio.run(runtime.queueing._enqueue_or_submit_inbound(item))


class TestTheQueue:
    """开着队列时插话让给排着的正式请求；反过来正式请求并进插话，这一轮升格成在回人家的话。"""

    @pytest.fixture(autouse=True)
    def _queue_on(self, runtime) -> None:
        set_live_config(runtime, enable_queue=True)

    def test_an_interjection_behind_a_queued_request_is_dropped(self, runtime) -> None:
        presence = runtime.state._adapter.trigger_presence
        stamp = presence.record_proactive(GROUP_ID, 1000.0)
        request = _queued(runtime, "direct", ref=1, source="at")
        interjection = _queued(runtime, "ambient", ref=2, source="llm_intent", stamp=stamp)

        assert _enqueue(runtime, request) is True
        # 让出去不算没收下：返回 False 会让调用方在群里报「未接受本次请求」。
        assert _enqueue(runtime, interjection) is True

        queue = runtime.state._adapter.qq_queue
        assert len(queue) == 1 and queue.open_item(QUEUE_KEY) is request
        assert request.user_text == "正文1"
        assert request.platform_updates["_trigger_source"] == "at"
        assert presence.proactive_count(GROUP_ID, 1000.0, 300.0) == 0

    def test_requests_still_merge_with_requests(self, runtime) -> None:
        first = _queued(runtime, "direct", ref=1, source="at")

        _enqueue(runtime, first)
        _enqueue(runtime, _queued(runtime, "direct", ref=2, source="reply"))

        assert len(runtime.state._adapter.qq_queue) == 1
        assert first.user_text == "正文1\n正文2"

    def test_interjections_merge_and_count_as_one_reply(self, runtime) -> None:
        presence = runtime.state._adapter.trigger_presence
        first_stamp = presence.record_proactive(GROUP_ID, 1000.0)
        second_stamp = presence.record_proactive(GROUP_ID, 1001.0)
        first = _queued(runtime, "ambient", ref=1, source="llm_intent", stamp=first_stamp)

        _enqueue(runtime, first)
        _enqueue(runtime, _queued(runtime, "ambient", ref=2, source="keyword", stamp=second_stamp))

        assert len(runtime.state._adapter.qq_queue) == 1
        assert first.platform_updates["_response_purpose"] == "ambient"
        assert first.platform_updates["_proactive_stamp"] == second_stamp
        assert presence.proactive_count(GROUP_ID, 1001.0, 300.0) == 1

    def test_a_request_joining_an_interjection_turns_it_into_a_reply(self, runtime) -> None:
        inbound = runtime.inbound
        presence = runtime.state._adapter.trigger_presence
        stamp = presence.record_proactive(GROUP_ID, 1000.0)
        interjection = _queued(
            runtime, "ambient", ref=1, source="llm_intent", stamp=stamp,
            text=f"插话正文\n\n{inbound._INTERJECT_REQUEST}",
        )

        _enqueue(runtime, interjection)
        _enqueue(runtime, _queued(runtime, "direct", ref=2, source="at", text="正式请求"))

        queue = runtime.state._adapter.qq_queue
        assert len(queue) == 1 and queue.open_item(QUEUE_KEY) is interjection
        assert inbound._INTERJECT_REQUEST not in interjection.text
        assert inbound._REPLY_REQUEST in interjection.text
        assert interjection.platform_updates["_response_purpose"] == "direct"
        assert interjection.platform_updates["_route_hints"]["delivery_purpose"] == "direct"
        assert interjection.platform_updates["_trigger_source"] == "at"
        assert "_proactive_stamp" not in interjection.platform_updates
        assert presence.proactive_count(GROUP_ID, 1000.0, 300.0) == 0

    def test_a_dropped_interjection_leaves_no_staged_rows(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        removed: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(runtime.state._adapter, "features", SimpleNamespace(
            available=True, burst_store=SimpleNamespace(remove=lambda key, refs: removed.append((key, list(refs)))),
        ))

        asyncio.run(runtime.queueing._queue_or_submit_ready(_queued(runtime, "direct", ref=1, source="at")))
        asyncio.run(runtime.queueing._queue_or_submit_ready(_queued(runtime, "ambient", ref=2, source="llm_intent")))

        assert removed == [(QUEUE_KEY, ["2"])]
        assert len(runtime.state._adapter.qq_queue) == 1


class TestRestoredAfterARestart:
    @staticmethod
    def _restore(runtime: Any, **extra: Any) -> Any:
        data = {
            "is_dm": False, "scope": QUEUE_KEY, "refs": ["7"], "attachments": [], "group_id": GROUP_ID,
            "channel": "qq_group", "real_key": REAL_KEY, "text": "正文", "user_text": "正文",
            "entry_node_id": "", "direct_interaction": False, "memory_user_ids": [], **extra,
        }
        return runtime.queueing._restore_staged_inbound(
            _Bot(), _Event("正文", message_id=7), data, None, {"scope": QUEUE_KEY},
        )

    def test_the_recorded_source_comes_back(self, runtime) -> None:
        item = self._restore(runtime, response_purpose="ambient", trigger_source="keyword")

        assert item.platform_updates["_trigger_source"] == "keyword"

    def test_an_old_interjection_row_counts_as_a_model_judgement(self, runtime) -> None:
        # 旧暂存行没记来源：插话那种按模型判断算，发出去不引用。
        item = self._restore(runtime, response_purpose="ambient")

        assert item.platform_updates["_trigger_source"] == "llm_intent"
        target = runtime.queueing._target_from_platform_data(item.platform_updates)
        assert runtime.sending._quotes_trigger(target) is False

    def test_an_old_request_row_keeps_quoting(self, runtime) -> None:
        item = self._restore(runtime, response_purpose="direct")

        assert "_trigger_source" not in item.platform_updates
        target = runtime.queueing._target_from_platform_data(item.platform_updates)
        assert runtime.sending._quotes_trigger(target) is True

    def test_a_staged_interjection_keeps_its_source_across_a_restart(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bot = _Bot()
        event = _Event("今天好热", message_id=7)
        features = runtime.state._adapter.features
        scope = features.actor(bot, event)["scope"]
        features.stage_burst(runtime.models.QueuedInbound(
            None, bot, event, "qq_group", REAL_KEY, scope, "正文", [], False,
            {
                "type": "group", "group_id": GROUP_ID, "_route_hints": {"source_message_refs": ["7"]},
                "_response_purpose": "ambient", "_trigger_source": "keyword",
            },
            "今天好热",
        ))
        features.started_at = time.time() + 1
        recovered: list[Any] = []

        async def submit(item: Any) -> bool:
            recovered.append(item)
            return True

        monkeypatch.setattr(runtime.queueing, "_queue_or_submit_ready", submit)
        asyncio.run(features.recover_bursts(bot))

        assert len(recovered) == 1
        assert recovered[0].platform_updates["_trigger_source"] == "keyword"
        assert recovered[0].platform_updates["_response_purpose"] == "ambient"
