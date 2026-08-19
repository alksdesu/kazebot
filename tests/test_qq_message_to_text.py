"""消息段转文本：同一条消息的三种到达形态必须给出同一份文本。

对象段 list、dict 段 list、CQ 字符串是同一条 QQ 消息的三种到达形态，渲染只有一份实现，
所以核心断言是三者逐字相等 —— 任何一处退回内联复制，等式立刻不成立。

另一件事：进模型的正文不该出现裸 CQ 码、裸 message_id 和裸 QQ 号。
"""
from __future__ import annotations

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

from _onebot_harness import load_runtime  # noqa: E402

_BOT_ID = "900001"
_OTHER_ID = "10001"


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _object_segments() -> list[Any]:
    """一条把每种分支都走一遍的消息：文本、@他人、@Bot、图片、无 id 表情、视频、引用、未知段。"""
    return [
        SimpleNamespace(type="text", data={"text": "你好"}),
        SimpleNamespace(type="at", data={"qq": _OTHER_ID}),
        SimpleNamespace(type="at", data={"qq": _BOT_ID}),
        SimpleNamespace(type="image", data={"file": "a.jpg"}),
        SimpleNamespace(type="face", data={}),
        SimpleNamespace(type="video", data={"file": "v.mp4"}),
        SimpleNamespace(type="reply", data={"id": "42"}),
        SimpleNamespace(type="json", data={"data": '{"url":"http://leak"}'}),
    ]


def _dict_segments() -> list[dict[str, Any]]:
    return [{"type": seg.type, "data": dict(seg.data)} for seg in _object_segments()]


def _cq_message() -> str:
    return (
        "你好"
        f"[CQ:at,qq={_OTHER_ID}]"
        f"[CQ:at,qq={_BOT_ID}]"
        "[CQ:image,file=a.jpg]"
        "[CQ:face]"
        "[CQ:video,file=v.mp4]"
        "[CQ:reply,id=42]"
        '[CQ:json,data={"url":"http://leak"}]'
    )


def _all_shapes(runtime: Any) -> list[str]:
    return [
        runtime._message_to_text(_object_segments(), _BOT_ID),
        runtime._message_to_text_generic(_dict_segments(), _BOT_ID),
        runtime._message_to_text_generic(_cq_message(), _BOT_ID),
    ]


class TestThreeShapesOneRenderer:
    def test_three_shapes_agree(self, runtime) -> None:
        alias = runtime._anonymize_user_id(_OTHER_ID)
        expected = (
            f"你好@{alias}"
            f"{runtime.IMAGE_PLACEHOLDER}"
            f"{runtime.FACE_PLACEHOLDER}"
            f"{runtime.VIDEO_PLACEHOLDER}"
            "[json]"
        )

        assert _all_shapes(runtime) == [expected, expected, expected]

    def test_at_bot_is_dropped_in_every_shape(self, runtime) -> None:
        # @Bot 是触发前缀不是用户内容；留着会让模型以为用户在 @ 第三个人。
        objects = [
            SimpleNamespace(type="text", data={"text": "在吗"}),
            SimpleNamespace(type="at", data={"qq": _BOT_ID}),
        ]
        dicts = [{"type": seg.type, "data": dict(seg.data)} for seg in objects]

        rendered = [
            runtime._message_to_text(objects, _BOT_ID),
            runtime._message_to_text_generic(dicts, _BOT_ID),
            runtime._message_to_text_generic(f"在吗[CQ:at,qq={_BOT_ID}]", _BOT_ID),
        ]

        assert rendered == ["在吗", "在吗", "在吗"]

    def test_an_at_never_carries_a_bare_qq_number(self, runtime) -> None:
        for text in _all_shapes(runtime):
            assert _OTHER_ID not in text

    def test_reply_segment_never_reaches_the_model(self, runtime) -> None:
        for text in _all_shapes(runtime):
            assert "[回复:" not in text
            assert "42" not in text

    def test_unknown_cq_segment_never_leaks_its_params(self, runtime) -> None:
        rendered = runtime._message_to_text_generic('[CQ:json,data={"url":"http://leak"}]', _BOT_ID)

        assert rendered == "[json]"
        assert "http://leak" not in rendered


class TestSegmentShapes:
    def test_text_none_is_not_the_literal_none(self, runtime) -> None:
        assert runtime._message_to_text_generic([{"type": "text", "data": {"text": None}}]) == ""

    def test_object_segments_in_a_plain_list_are_read(self, runtime) -> None:
        # Message 是 list 子类，但普通 list 装 MessageSegment 对象也是真实到达形态。
        message = [SimpleNamespace(type="text", data={"text": "在吗"})]

        assert runtime._message_to_text_generic(message, _BOT_ID) == "在吗"


class TestImagePlaceholder:
    def test_mface_uses_the_image_placeholder(self, runtime) -> None:
        # mface 走图片下载通道，占位符和 image 不一致就会让附件路径回填不到正文。
        message = [{"type": "mface", "data": {"url": "http://x/b.gif"}}]

        assert runtime._message_to_text_generic(message, _BOT_ID) == runtime.IMAGE_PLACEHOLDER
        assert runtime._iter_qq_image_sources(message) == [runtime.ImageSource("http://x/b.gif", sticker=True)]

    def test_a_cq_mface_uses_the_image_placeholder(self, runtime) -> None:
        assert runtime._message_to_text_generic(
            "[CQ:mface,url=http://x/b.gif]", _BOT_ID,
        ) == runtime.IMAGE_PLACEHOLDER

    def test_an_attachment_path_is_backfilled_over_an_mface(self, runtime) -> None:
        text = runtime._message_to_text_generic([{"type": "mface", "data": {"url": "http://x/b.gif"}}], _BOT_ID)

        assert text.replace(runtime.IMAGE_PLACEHOLDER, "[图片: out/b.gif]", 1) == "[图片: out/b.gif]"

    def test_forward_media_text_strips_the_image_placeholder(self, runtime) -> None:
        message = [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "http://x/a.jpg"}},
            {"type": "mface", "data": {"url": "http://x/b.gif"}},
        ]

        text = runtime._forward_media_text(message, _BOT_ID)

        assert text == "看这个"
        assert runtime.IMAGE_PLACEHOLDER not in text
        assert "[mface]" not in text
