"""读合并转发：正文展开、卡片里的图、以及群触发判定看不看得见展开后的内容。

卡片内容要靠一次协议往返才拿得到，所以这里同时钉住「只拉一次」和「拉不动也不能卡住」。
"""
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

_BOT_ID = "900001"


class FakeBot:
    """记下每次 get_forward_msg，用来证明缓存真的省掉了重复往返。"""

    self_id = _BOT_ID

    def __init__(self, cards: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.cards = cards or {}
        self.calls: list[str] = []
        self.hang = False

    async def call_api(self, action: str, **params: Any) -> Any:
        assert action == "get_forward_msg", action
        forward_id = str(params.get("id") or "")
        self.calls.append(forward_id)
        if self.hang:
            await asyncio.sleep(30)
        if forward_id not in self.cards:
            raise RuntimeError("unknown forward id")
        return {"messages": self.cards[forward_id]}


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": value}}


def _image(url: str) -> dict[str, Any]:
    return {"type": "image", "data": {"url": url}}


def _file(url: str, name: str) -> dict[str, Any]:
    return {"type": "file", "data": {"url": url, "name": name}}


def _card(forward_id: str) -> dict[str, Any]:
    return {"type": "forward", "data": {"id": forward_id}}


def _inline_card(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "forward", "data": {"content": nodes}}


def _node(name: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sender": {"user_id": "10001", "nickname": name}, "time": 1_700_000_000, "content": segments}


def _event(message: list[dict[str, Any]], message_id: str = "77") -> SimpleNamespace:
    return SimpleNamespace(
        get_message=lambda: message,
        group_id=1001,
        user_id=10001,
        message_id=message_id,
        time=1_700_000_000,
    )


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _expand(runtime: Any, bot: FakeBot, message: Any) -> str:
    return asyncio.run(runtime._message_to_text_with_forward(bot, message, _BOT_ID))


class TestSourceExtraction:
    @pytest.mark.parametrize("key", ["id", "res_id", "resId", "forward_id", "forwardId"])
    def test_every_spelling_of_the_id_is_recognised(self, runtime, key: str) -> None:
        sources = runtime._extract_forward_sources([{"type": "forward", "data": {key: "abc"}}])

        assert [s.forward_id for s in sources] == ["abc"]

    def test_an_inline_card_is_recognised(self, runtime) -> None:
        # 有些实现把嵌套层直接内嵌成 node 列表而不给 res_id，不认这形态那层就只剩占位符。
        nodes = [_node("张三", [_text("里面")])]
        sources = runtime._extract_forward_sources([_inline_card(nodes)])

        assert len(sources) == 1
        assert sources[0].forward_id == ""
        assert sources[0].inline == tuple(nodes)

    def test_an_id_is_preferred_but_inline_content_is_kept_as_backup(self, runtime) -> None:
        # 接口拿到的是全量、内嵌的可能只是摘要，所以先拉接口；但丢掉内嵌就没有兜底了。
        nodes = [_node("张三", [])]
        source = runtime._extract_forward_sources(
            [{"type": "forward", "data": {"id": "abc", "content": nodes}}],
        )[0]

        assert source.forward_id == "abc"
        assert source.inline == tuple(nodes)

    def test_a_cq_forward_is_recognised(self, runtime) -> None:
        assert [s.forward_id for s in runtime._extract_forward_sources("[CQ:forward,id=abc]")] == ["abc"]

    def test_order_is_preserved_so_placeholders_line_up(self, runtime) -> None:
        sources = runtime._extract_forward_sources([_card("a"), _text("中间"), _card("b")])

        assert [s.forward_id for s in sources] == ["a", "b"]

    def test_a_card_with_neither_id_nor_content_is_dropped(self, runtime) -> None:
        assert runtime._extract_forward_sources([{"type": "forward", "data": {}}]) == []


class TestTextExpansion:
    def test_the_card_expands_in_place_of_its_placeholder(self, runtime) -> None:
        # 展开内容追加在末尾的话，正文里会先出现一个孤零零的 [合并转发]。
        bot = FakeBot({"a": [_node("张三", [_text("里面说的话")])]})

        text = _expand(runtime, bot, [_text("看这个"), _card("a"), _text("怎么样")])

        assert runtime.FORWARD_PLACEHOLDER not in text
        assert text.index("看这个") < text.index("里面说的话") < text.index("怎么样")

    def test_nested_cards_expand_too(self, runtime) -> None:
        bot = FakeBot({
            "outer": [_node("张三", [_card("inner")])],
            "inner": [_node("李四", [_text("最里面")])],
        })

        text = _expand(runtime, bot, [_card("outer")])

        assert "最里面" in text
        assert "李四" in text

    def test_an_inline_nested_card_expands_without_an_api_call(self, runtime) -> None:
        inner = [_node("李四", [_text("内嵌的一条")])]
        bot = FakeBot({"outer": [_node("张三", [_inline_card(inner)])]})

        text = _expand(runtime, bot, [_card("outer")])

        assert "内嵌的一条" in text
        assert bot.calls == ["outer"]

    def test_inline_content_rescues_a_card_the_api_cannot_fetch(self, runtime) -> None:
        nodes = [_node("张三", [_text("兜底内容")])]
        bot = FakeBot()

        text = _expand(runtime, bot, [{"type": "forward", "data": {"id": "gone", "content": nodes}}])

        assert "兜底内容" in text

    def test_an_unreadable_card_says_so_instead_of_vanishing(self, runtime) -> None:
        text = _expand(runtime, FakeBot(), [_card("gone")])

        assert "读取失败" in text

    def test_the_depth_limit_stops_the_recursion(self, runtime) -> None:
        set_live_config(runtime, forward_msg_max_depth=1)
        bot = FakeBot({
            "outer": [_node("张三", [_card("inner")])],
            "inner": [_node("李四", [_text("够不着")])],
        })

        text = _expand(runtime, bot, [_card("outer")])

        assert "够不着" not in text
        assert "深度上限" in text

    def test_the_message_count_limit_truncates(self, runtime) -> None:
        set_live_config(runtime, forward_msg_max_messages=2)
        bot = FakeBot({"a": [_node("张三", [_text(f"第{i}条")]) for i in range(5)]})

        text = _expand(runtime, bot, [_card("a")])

        assert "第0条" in text
        assert "第4条" not in text
        assert "已截断" in text

    def test_a_self_referencing_card_does_not_loop(self, runtime) -> None:
        bot = FakeBot({"a": [_node("张三", [_card("a")])]})

        text = _expand(runtime, bot, [_card("a")])

        assert "循环引用" in text

    def test_the_same_card_is_only_fetched_once(self, runtime) -> None:
        bot = FakeBot({"a": [_node("张三", [_text("hi")])]})

        _expand(runtime, bot, [_card("a")])
        _expand(runtime, bot, [_card("a")])

        assert bot.calls == ["a"]

    def test_a_hung_fetch_falls_back_instead_of_blocking(self, runtime) -> None:
        # 群触发判定也走这条路，干等下去会把后面的 matcher 一起压住。
        set_live_config(runtime, forward_msg_timeout_sec=0.5)
        bot = FakeBot({"a": []})
        bot.hang = True

        text = _expand(runtime, bot, [_text("看这个"), _card("a")])

        assert "读取超时" in text
        assert "看这个" in text

    def test_the_switch_keeps_the_placeholder(self, runtime) -> None:
        set_live_config(runtime, enable_forward_msg_input=False)
        bot = FakeBot({"a": [_node("张三", [_text("不该出现")])]})

        text = _expand(runtime, bot, [_card("a")])

        assert text == runtime.FORWARD_PLACEHOLDER
        assert bot.calls == []


class TestMediaInsideCards:
    def _media(self, runtime: Any, bot: FakeBot, message: Any) -> tuple[list[str], list[dict[str, Any]]]:
        sources, files = asyncio.run(runtime._collect_message_media(bot, message))
        return [source.url for source in sources], files

    def _media_without_forward(
        self, runtime: Any, bot: FakeBot, message: Any,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        sources, files = asyncio.run(
            runtime._collect_message_media(bot, message, expand_forward=False),
        )
        return [source.url for source in sources], files

    def test_an_image_inside_a_card_is_collected(self, runtime) -> None:
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        urls, _ = self._media(runtime, bot, [_card("a")])

        assert urls == ["http://x/in.jpg"]

    def test_images_come_back_in_the_order_the_text_shows_them(self, runtime) -> None:
        # 引用块按 [图片] 占位符出现的先后回填路径，顺序错了就配错图。
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        urls, _ = self._media(runtime, bot, [_card("a"), _image("http://x/top.jpg")])

        assert urls == ["http://x/in.jpg", "http://x/top.jpg"]

    def test_a_nested_card_gives_up_its_images_too(self, runtime) -> None:
        bot = FakeBot({
            "outer": [_node("张三", [_card("inner")])],
            "inner": [_node("李四", [_image("http://x/deep.jpg")])],
        })

        urls, _ = self._media(runtime, bot, [_card("outer")])

        assert urls == ["http://x/deep.jpg"]

    def test_a_file_inside_a_card_is_collected(self, runtime) -> None:
        bot = FakeBot({"a": [_node("张三", [_file("http://x/doc.pdf", "doc.pdf")])]})

        _, files = self._media(runtime, bot, [_card("a")])

        assert [f["name"] for f in files] == ["doc.pdf"]

    def test_the_switch_leaves_only_the_top_level(self, runtime) -> None:
        set_live_config(runtime, enable_forward_msg_media=False)
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        urls, _ = self._media(runtime, bot, [_card("a"), _image("http://x/top.jpg")])

        assert urls == ["http://x/top.jpg"]
        assert bot.calls == []

    def test_a_message_nobody_answers_does_not_pull_the_cards_images(self, runtime) -> None:
        # 历史 matcher 每条不触发的消息都会走。为一条不进模型的消息下四张图纯属白花。
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        urls, _ = self._media_without_forward(runtime, bot, [_card("a"), _image("http://x/top.jpg")])

        assert urls == ["http://x/top.jpg"]
        assert bot.calls == []

    def test_a_hung_fetch_falls_back_to_the_top_level(self, runtime) -> None:
        set_live_config(runtime, forward_msg_timeout_sec=0.5)
        bot = FakeBot({"a": []})
        bot.hang = True

        urls, _ = self._media(runtime, bot, [_card("a"), _image("http://x/top.jpg")])

        assert urls == ["http://x/top.jpg"]

    def test_the_card_content_is_reused_from_the_text_pass(self, runtime) -> None:
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        _expand(runtime, bot, [_card("a")])
        self._media(runtime, bot, [_card("a")])

        assert bot.calls == ["a"]


class TestTriggerSeesTheContent:
    def _trigger_text(self, runtime: Any, bot: FakeBot, message: Any) -> str:
        return asyncio.run(runtime._group_trigger_text(bot, _event(message)))

    def test_a_keyword_inside_a_card_reaches_the_trigger_check(self, runtime) -> None:
        # 判定那一步只看到 [合并转发] 的话，不 @ bot 就永远不会理这条消息。
        bot = FakeBot({"a": [_node("张三", [_text("小可爱在吗")])]})

        assert "小可爱在吗" in self._trigger_text(runtime, bot, [_card("a")])

    def test_the_switch_puts_the_trigger_check_back_on_the_placeholder(self, runtime) -> None:
        set_live_config(runtime, forward_msg_expand_for_trigger=False)
        bot = FakeBot({"a": [_node("张三", [_text("小可爱在吗")])]})

        text = self._trigger_text(runtime, bot, [_card("a")])

        assert text == runtime.FORWARD_PLACEHOLDER
        assert bot.calls == []

    def test_one_event_expands_once_no_matter_how_many_readers(self, runtime) -> None:
        # 两个 rule 加 handler 各要一份；判定缓存和冷却记账都拿正文当 key，两边不一样会判两遍。
        bot = FakeBot({"a": [_node("张三", [_text("hi")])]})
        event = _event([_card("a")])

        first = asyncio.run(runtime._event_text_with_forward(bot, event))
        runtime._forward_msg_cache.clear()
        second = asyncio.run(runtime._event_text_with_forward(bot, event))

        assert first == second
        assert bot.calls == ["a"]

    def test_the_history_matcher_asks_for_the_top_level_only(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 参数存在不等于用上了，这里钉的是调用点。
        seen: dict[str, Any] = {}

        async def spy(bot: Any, event: Any, key: str, **kwargs: Any):
            seen.update(kwargs)
            return [], []

        monkeypatch.setattr(runtime, "_collect_qq_attachments", spy)
        monkeypatch.setattr(runtime, "_remember_message_for_reply_context", lambda event: None)
        monkeypatch.setattr(runtime, "_record_group_message", lambda *a, **k: 0)
        bot = FakeBot({"a": [_node("张三", [_image("http://x/in.jpg")])]})

        asyncio.run(runtime._record_non_trigger_message(bot, _event([_card("a")])))

        assert seen == {"expand_forward": False}

    def test_a_plain_message_never_touches_the_cache(self, runtime) -> None:
        bot = FakeBot()

        text = asyncio.run(runtime._event_text_with_forward(bot, _event([_text("普通消息")])))

        assert text == "普通消息"
        assert runtime._event_text_cache == {}
