"""复读指纹的段类型判定。

echo_policy 只管「几条一样、来自几个人」，一条消息能不能跟由这里决定。
真实群消息里的表情是独立的 face 段，漏掉它等于整个功能不触发。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._onebot_harness import load_runtime


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def _face(face_id: str) -> dict:
    return {"type": "face", "data": {"id": face_id}}


def _image(**data: str) -> dict:
    return {"type": "image", "data": dict(data)}


class TestTextAndFace:
    def test_plain_text(self, runtime) -> None:
        key = runtime._echo_key_for_message([_text("+1")], "+1")
        assert key == "txt:+1"

    def test_text_with_a_face_is_echoable(self, runtime) -> None:
        # 生产上的真实形态：句子里夹着 [face:id=264]，曾经在这里被判成不可跟。
        message = [_text("我笑了"), _face("264")]
        key = runtime._echo_key_for_message(message, "我笑了[QQ表情:捂脸]")

        assert key == "txt:我笑了[QQ表情:捂脸]|face:264"

    def test_a_face_on_its_own(self, runtime) -> None:
        key = runtime._echo_key_for_message([_face("264")], "[QQ表情:捂脸]")
        assert key == "txt:[QQ表情:捂脸]|face:264"

    def test_the_same_sentence_with_a_different_face_is_a_different_key(self, runtime) -> None:
        # 显示名是查表来的，撞名时只有 id 能把两条分开。
        one = runtime._echo_key_for_message([_text("哈"), _face("264")], "哈[QQ表情:捂脸]")
        two = runtime._echo_key_for_message([_text("哈"), _face("178")], "哈[QQ表情:捂脸]")

        assert one != two

    def test_face_order_is_part_of_the_key(self, runtime) -> None:
        one = runtime._echo_key_for_message([_face("1"), _face("2")], "[QQ表情:a][QQ表情:b]")
        two = runtime._echo_key_for_message([_face("2"), _face("1")], "[QQ表情:a][QQ表情:b]")

        assert one != two

    def test_whitespace_only_text_does_not_count_as_content(self, runtime) -> None:
        assert runtime._echo_key_for_message([_text("   ")], "   ") == ""


class TestImages:
    def test_an_image_uses_its_file_id(self, runtime) -> None:
        key = runtime._echo_key_for_message([_image(file="abc.jpg")], "[图片]")
        assert key == "img:abc.jpg"

    def test_a_market_sticker_prefers_emoji_id(self, runtime) -> None:
        # url 每次取都可能带不同的鉴权参数，拿它当指纹永远比不中。
        message = [{"type": "mface", "data": {"emoji_id": "E42", "url": "https://x/y?token=1"}}]
        assert runtime._echo_key_for_message(message, "[图片]") == "img:E42"

    def test_an_image_without_any_id_is_refused(self, runtime) -> None:
        assert runtime._echo_key_for_message([_image()], "[图片]") == ""


class TestRefused:
    @pytest.mark.parametrize("segment", [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "reply", "data": {"id": "9"}},
        {"type": "record", "data": {}},
        {"type": "video", "data": {}},
        {"type": "file", "data": {"name": "a.zip"}},
        {"type": "forward", "data": {"id": "x"}},
        {"type": "json", "data": {}},
    ])
    def test_anything_with_a_target_or_a_payload_is_refused(self, runtime, segment: dict) -> None:
        assert runtime._echo_key_for_message([_text("跟我"), segment], "跟我") == ""

    def test_text_mixed_with_an_image_is_refused(self, runtime) -> None:
        # 两种指纹合不到一起，也没必要为图文混排的复读服务。
        message = [_text("看"), _image(file="a.jpg")]
        assert runtime._echo_key_for_message(message, "看[图片]") == ""

    def test_an_empty_message(self, runtime) -> None:
        assert runtime._echo_key_for_message([], "") == ""

    def test_no_message_at_all(self, runtime) -> None:
        assert runtime._echo_key_for_message(None, "") == ""
