"""QQ 表情进模型时的样子。

QQ 里「表情」是两种完全不同的东西：内置表情只有一个数字 id，商城表情是一张真图。
两种都当图片处理会丢情绪，都当文本处理会丢内容，所以这里逐条钉住各自的呈现。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime  # noqa: E402

# 直接加载文件：走包导入会先执行 adapters/onebot/__init__.py，那需要整套 nonebot。
_spec = importlib.util.spec_from_file_location(
    "onebot_faces", _ROOT / "adapters" / "onebot" / "faces.py",
)
_faces = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_faces)
FACE_NAMES = _faces.FACE_NAMES
face_display_name = _faces.face_display_name

_BOT_ID = "10000"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _local(tmp_path: Path, name: str) -> str:
    incoming = tmp_path / "data" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    target = incoming / name
    target.write_bytes(_PNG)
    return "file://" + target.as_posix()


class TestFaceNames:
    def test_a_known_face_reads_as_a_word(self, runtime) -> None:
        # [QQ表情:14] 对模型是个数字，[QQ表情:微笑] 才是情绪。
        message = [{"type": "face", "data": {"id": "14"}}]

        assert runtime._message_to_text_generic(message, _BOT_ID) == "[QQ表情:微笑]"

    @pytest.mark.parametrize("face_id,name", [
        ("9", "大哭"), ("182", "笑哭"), ("264", "捂脸"), ("271", "吃瓜"), ("179", "doge"),
    ])
    def test_the_common_ones_are_covered(self, runtime, face_id: str, name: str) -> None:
        message = [{"type": "face", "data": {"id": face_id}}]

        assert runtime._message_to_text_generic(message, _BOT_ID) == "[QQ表情:" + name + "]"

    def test_an_unknown_id_keeps_its_number(self, runtime) -> None:
        # QQ 还在加新表情。表里没有就照原样给出去，别编一个名字骗模型。
        message = [{"type": "face", "data": {"id": "99999"}}]

        assert runtime._message_to_text_generic(message, _BOT_ID) == "[QQ表情:99999]"

    def test_a_face_without_an_id_falls_back_to_the_placeholder(self, runtime) -> None:
        message = [{"type": "face", "data": {}}]

        assert runtime._message_to_text_generic(message, _BOT_ID) == runtime.FACE_PLACEHOLDER

    def test_a_cq_face_reads_the_same(self, runtime) -> None:
        assert runtime._message_to_text_generic("[CQ:face,id=63]", _BOT_ID) == "[QQ表情:玫瑰]"

    def test_a_face_is_not_downloaded(self, runtime) -> None:
        # 内置表情上报里没有地址，当图片收就是一次必然失败的下载。
        message = [{"type": "face", "data": {"id": "14"}}]

        assert runtime._iter_qq_image_sources(message) == []


class TestFaceTable:
    def test_lookup_tolerates_whitespace_and_numbers(self) -> None:
        assert face_display_name(" 14 ") == "微笑"
        assert face_display_name(14) == "微笑"

    def test_an_unknown_id_gives_an_empty_string(self) -> None:
        assert face_display_name("99999") == ""
        assert face_display_name(None) == ""

    def test_the_reaction_table_takes_its_names_from_here(self, runtime) -> None:
        # 同一个 id 在「收到表情」和「表态可选项」里必须是同一个词，否则模型学到两套叫法。
        for face_id, name in runtime._REACT_MODEL_EMOJIS.items():
            assert FACE_NAMES[face_id] == name

    def test_no_reaction_id_is_missing_a_name(self, runtime) -> None:
        assert all(runtime._REACT_MODEL_EMOJIS.values())


class TestTheInboundFaceFormatDoesNotEscape:
    """入站把内置表情渲染成 [QQ表情:名字] 给模型看，照抄回来就是群里一行裸标记。"""

    @pytest.mark.parametrize("written", [
        "[QQ表情:微笑]", "[QQ表情：笑哭]", "[QQ表情]", "[QQ表情:99999]", "[QQ表情: 带空格 ]",
    ])
    def test_it_is_stripped_before_sending(self, runtime, written: str) -> None:
        assert runtime.strip_output_markers("嗨" + written + "呀") == "嗨呀"

    @pytest.mark.parametrize("legal", [
        "[表情:开心]", "[emoji:抱抱]", "[收藏表情:狗头]", "[QQ_EMOJI:旧格式]",
    ])
    def test_the_sendable_formats_are_untouched(self, runtime, legal: str) -> None:
        # 这四个真能发出图来，跟着剥就是把功能一起剥了。
        assert runtime.strip_output_markers(legal) == legal

    def test_a_plain_emoji_character_survives(self, runtime) -> None:
        # 想表达情绪的正路：直接写字符，不需要任何标记。
        assert runtime.strip_output_markers("哈哈" + chr(0x1F602)) == "哈哈" + chr(0x1F602)

    def test_it_does_not_reach_the_group_history(self, runtime) -> None:
        # 历史会回喂给模型，留在里面等于教它下次接着写。
        runtime._record_bot_reply(123, "好的[QQ表情:微笑]")

        assert "QQ表情" not in str(list(runtime._group_history[123]))


class TestStickerSegments:
    @pytest.mark.parametrize("seg_type", ["mface", "marketface"])
    def test_a_market_sticker_is_collected_as_an_image(self, runtime, seg_type: str) -> None:
        message = [{"type": seg_type, "data": {"url": "http://x/s.gif"}}]

        assert runtime._iter_qq_image_sources(message) == [
            runtime.ImageSource("http://x/s.gif", sticker=True),
        ]

    def test_a_photo_is_not_marked_as_a_sticker(self, runtime) -> None:
        message = [{"type": "image", "data": {"url": "http://x/p.jpg"}}]

        assert runtime._iter_qq_image_sources(message) == [
            runtime.ImageSource("http://x/p.jpg", sticker=False),
        ]

    def test_both_still_use_the_same_placeholder(self, runtime) -> None:
        # 占位符必须一致：回填是按 [图片] 出现的先后对号入座的。
        message = [
            {"type": "image", "data": {"url": "http://x/p.jpg"}},
            {"type": "mface", "data": {"url": "http://x/s.gif"}},
        ]

        assert runtime._message_to_text_generic(message, _BOT_ID) == (
            runtime.IMAGE_PLACEHOLDER * 2
        )

    def test_a_cq_sticker_is_marked_too(self, runtime) -> None:
        sources = runtime._iter_qq_image_sources("[CQ:mface,url=http://x/s.gif]")

        assert sources == [runtime.ImageSource("http://x/s.gif", sticker=True)]


class TestStickerReachesTheAttachment:
    def _download(self, runtime, tmp_path: Path, sources: list[Any]) -> list[dict[str, Any]]:
        result, errors = asyncio.run(runtime._image_sources_to_attachments(sources, "qq_group:s"))
        assert errors == []
        return result

    def test_the_flag_survives_the_download(self, runtime, tmp_path: Path) -> None:
        sources = [
            runtime.ImageSource(_local(tmp_path, "photo.png"), sticker=False),
            runtime.ImageSource(_local(tmp_path, "sticker.png"), sticker=True),
        ]

        got = self._download(runtime, tmp_path, sources)

        assert [item["sticker"] for item in got] == [False, True]


class TestBackfillLabels:
    def _apply(self, runtime, text: str, attachments: list[dict[str, Any]]) -> str:
        return runtime._apply_attachment_hints(text, "看看", attachments, [])

    def _att(self, path: str, **extra: Any) -> dict[str, Any]:
        return {"type": "image", "path": path, "name": Path(path).name, **extra}

    def test_a_sticker_is_named_a_sticker(self, runtime) -> None:
        text = self._apply(runtime, runtime.IMAGE_PLACEHOLDER, [self._att("a/s.gif", sticker=True)])

        assert text == "[表情包: a/s.gif]"

    def test_a_photo_is_still_a_photo(self, runtime) -> None:
        text = self._apply(runtime, runtime.IMAGE_PLACEHOLDER, [self._att("a/p.jpg")])

        assert text == "[图片: a/p.jpg]"

    def test_mixed_order_is_preserved(self, runtime) -> None:
        # 标签跟着位置走。顺序错了模型会以为那张照片是个表情包。
        text = self._apply(
            runtime,
            runtime.IMAGE_PLACEHOLDER + "然后" + runtime.IMAGE_PLACEHOLDER,
            [self._att("a/p.jpg"), self._att("a/s.gif", sticker=True)],
        )

        assert text == "[图片: a/p.jpg]然后[表情包: a/s.gif]"

    def test_an_old_attachment_without_the_flag_reads_as_a_photo(self, runtime) -> None:
        # 重启前存下的附件记录没有这个键，不能因此变成表情包。
        text = self._apply(runtime, runtime.IMAGE_PLACEHOLDER, [{"type": "image", "path": "a/old.jpg"}])

        assert text == "[图片: a/old.jpg]"

    def test_a_file_attachment_does_not_take_an_image_slot(self, runtime) -> None:
        # 文件有自己的 [文件:名字] 占位。让它去顶 [图片] 会把路径安到后面那张图头上。
        text = self._apply(
            runtime,
            runtime.IMAGE_PLACEHOLDER,
            [{"type": "file", "path": "a/doc.pdf", "name": "doc.pdf"}, self._att("a/p.jpg")],
        )

        assert text == "[图片: a/p.jpg]"
