"""内部的 OpenAI 式图片块转成各家原生格式。

内部表示只有一种（image_url + data URI），各家的线上格式各不相同。键名写错不会报错，
只会被对面静默忽略成「没收到图」——所以这里逐字盯键名。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from providers.anthropic import _convert_image_part  # noqa: E402
from providers.base import image_part_url, split_data_url  # noqa: E402
from providers.gemini import _user_parts  # noqa: E402
from providers.openai_responses import _build_user_content  # noqa: E402

# base64 里的 + / = 都是合法字符，正则写窄了会把载荷截断。
B64 = "iVBORw0KGgo+/aGVsbG8=="
DATA_URL = "data:image/jpeg;base64," + B64


def _part(url: str = DATA_URL) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _msg(*parts: dict) -> dict:
    return {"role": "user", "content": list(parts)}


class TestDataUrlSplit:
    def test_it_keeps_the_whole_payload(self) -> None:
        assert split_data_url(DATA_URL) == ("image/jpeg", B64)

    def test_a_payload_containing_a_comma_is_not_a_thing_but_the_first_one_wins(self) -> None:
        assert split_data_url("data:image/png;base64,AA,BB") == ("image/png", "AA,BB")

    @pytest.mark.parametrize("url", ["", "https://x/y.png", "data:image/png;base64,", "file://a.png"])
    def test_anything_else_is_not_a_data_url(self, url: str) -> None:
        assert split_data_url(url) is None

    def test_a_mime_less_data_url_still_parses(self) -> None:
        assert split_data_url("data:;base64,AA") == ("application/octet-stream", "AA")


class TestUrlExtraction:
    def test_the_normal_shape(self) -> None:
        assert image_part_url(_part()) == DATA_URL

    def test_a_bare_string_does_not_crash(self) -> None:
        # 外部导入的历史里 image_url 直接是字符串。三家原来都会在 .get 上抛 AttributeError。
        assert image_part_url({"type": "image_url", "image_url": DATA_URL}) == DATA_URL

    @pytest.mark.parametrize("part", [
        {"type": "image_url"},
        {"type": "image_url", "image_url": None},
        {"type": "image_url", "image_url": {}},
        {"type": "image_url", "image_url": 42},
        "not a dict",
    ])
    def test_broken_shapes_give_an_empty_url(self, part: object) -> None:
        assert image_part_url(part) == ""


class TestAnthropic:
    def test_the_native_shape(self) -> None:
        assert _convert_image_part(_part()) == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": B64},
        }

    def test_the_media_type_comes_from_the_url_not_a_default(self) -> None:
        block = _convert_image_part(_part("data:image/webp;base64," + B64))
        assert block["source"]["media_type"] == "image/webp"

    def test_a_plain_url_uses_the_url_source(self) -> None:
        assert _convert_image_part(_part("https://cdn.example/a.png")) == {
            "type": "image",
            "source": {"type": "url", "url": "https://cdn.example/a.png"},
        }

    def test_a_bare_string_url_still_converts(self) -> None:
        block = _convert_image_part({"type": "image_url", "image_url": DATA_URL})
        assert block["source"]["data"] == B64

    def test_an_unusable_part_degrades_to_text(self) -> None:
        assert _convert_image_part({"type": "image_url"}) == {
            "type": "text", "text": "[image could not be converted]",
        }


class TestGemini:
    def test_the_native_shape_is_camel_case(self) -> None:
        # 蛇形键 Gemini 会当未知字段丢掉，请求照样 200，只是模型看不见图。
        assert _user_parts(_msg(_part())) == [{"inlineData": {"mimeType": "image/jpeg", "data": B64}}]

    def test_text_and_image_keep_their_order(self) -> None:
        parts = _user_parts(_msg({"type": "text", "text": "看这个"}, _part()))
        assert [next(iter(p)) for p in parts] == ["text", "inlineData"]

    def test_a_plain_url_falls_back_to_text(self) -> None:
        # Gemini 不收任意 http 地址，硬塞 inlineData 会 400。
        assert _user_parts(_msg(_part("https://cdn.example/a.png"))) == [{"text": "[Image: https://cdn.example/a.png]"}]

    def test_a_bare_string_url_still_converts(self) -> None:
        parts = _user_parts(_msg({"type": "image_url", "image_url": DATA_URL}))
        assert parts[0]["inlineData"]["data"] == B64

    def test_an_unusable_part_degrades_to_text(self) -> None:
        assert _user_parts(_msg({"type": "image_url"})) == [{"text": "[image could not be converted]"}]

    def test_a_non_dict_block_does_not_crash(self) -> None:
        assert _user_parts({"role": "user", "content": ["hello"]}) == [{"text": "hello"}]

    def test_a_string_message_still_works(self) -> None:
        assert _user_parts({"role": "user", "content": "hi"}) == [{"text": "hi"}]


class TestOpenAIResponses:
    def test_the_native_shape(self) -> None:
        # Responses 用 input_image，且 image_url 是裸字符串而不是对象。
        assert _build_user_content(_msg(_part())) == [{"type": "input_image", "image_url": DATA_URL}]

    def test_text_becomes_input_text(self) -> None:
        blocks = _build_user_content(_msg({"type": "text", "text": "看这个"}, _part()))
        assert [b["type"] for b in blocks] == ["input_text", "input_image"]

    def test_a_bare_string_url_still_converts(self) -> None:
        blocks = _build_user_content(_msg({"type": "image_url", "image_url": DATA_URL}))
        assert blocks[0] == {"type": "input_image", "image_url": DATA_URL}

    def test_an_empty_url_is_not_sent_as_an_image(self) -> None:
        # {"type":"input_image","image_url":""} 是一个 400，不是一张空图。
        assert _build_user_content(_msg({"type": "image_url"})) == [
            {"type": "input_text", "text": "[image could not be converted]"},
        ]


class TestCrossProvider:
    def test_every_family_carries_the_payload_through_intact(self) -> None:
        # 同一张图喂给三家，base64 一个字符都不能少。
        assert _convert_image_part(_part())["source"]["data"] == B64
        assert _user_parts(_msg(_part()))[0]["inlineData"]["data"] == B64
        assert _build_user_content(_msg(_part()))[0]["image_url"].endswith(B64)

    @pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp", "image/heic"])
    def test_the_declared_mime_is_never_rewritten(self, mime: str) -> None:
        # 主链恒定送 JPEG，但工具产物和外部导入的历史带着别的 mime，谁都不该改写它。
        url = "data:" + mime + ";base64," + B64
        assert _convert_image_part(_part(url))["source"]["media_type"] == mime
        assert _user_parts(_msg(_part(url)))[0]["inlineData"]["mimeType"] == mime
