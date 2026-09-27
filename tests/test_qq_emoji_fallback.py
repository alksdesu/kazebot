"""收藏接口不可用时，本地表情包和回复正文仍可发送。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.outbound import EMOJI_LIBRARY, EMOJI_NATIVE, ResolvedSticker  # noqa: E402
from adapters.onebot import emoji_handler as emoji  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402

_DIGEST = "a" * 64
_CONTENT = "b" * 64
_FACE_URL = "https://example.invalid/face.gif"
_PICKED = ResolvedSticker(_DIGEST, _CONTENT)
_STICKER = {"type": "sticker", "sha256": _DIGEST, "content_sha256": _CONTENT}
_FACE = {"type": "native_emoji", "platform": QQ_NAPCAT.id, "ref": _FACE_URL}


class FaceBot:
    def __init__(self, *, faces: list[Any] | None = None, error: BaseException | None = None):
        self.self_id = "10000"
        self.faces = faces or []
        self.error = error
        self.calls: list[str] = []

    async def call_api(self, name: str, **kwargs: Any) -> Any:
        assert name == "fetch_custom_face_detail"
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.faces


def text_of(segments: list[dict[str, Any]]) -> str:
    return "".join(segment["content"] for segment in segments if segment["type"] == "text")


@pytest.mark.parametrize("text", [
    "[表情:开心]", "[表情： 开心]", "前文[emoji:开心]后文", "[EMOJI:开心]",
    "[收藏表情:开心]", "[QQ_EMOJI:开心]", "[qq_emoji : 开心]",
])
def test_emoji_marker_detection_preserves_aliases(text: str) -> None:
    assert emoji.has_emoji_markers(text)


@pytest.mark.parametrize("text", ["", "普通文字", "哈哈😂", "[QQ表情:微笑]", "[表情:]", "[表情:未闭合"])
def test_emoji_marker_detection_ignores_plain_text(text: str) -> None:
    assert not emoji.has_emoji_markers(text)


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
def test_collection_failure_falls_back_to_local_sticker(metadata) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))
    selected: list[str] = []

    async def resolve(name: str):
        selected.append(name)
        return _PICKED

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("前文[表情:无语]后文", bot, [], metadata=metadata))

    assert segments == [{"type": "text", "content": "前文"}, _STICKER, {"type": "text", "content": "后文"}]
    assert selected == ["无语"]
    assert bot.calls == ["fetch_custom_face_detail"]


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
def test_collection_failure_is_not_retried_for_every_marker(metadata) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    async def resolve(name: str):
        return _PICKED

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("[表情:开心][表情:无语]", bot, [], metadata=metadata))

    assert segments == [_STICKER, _STICKER]
    assert len(bot.calls) == 1


# 只给个地址不算挑中：没有哈希，发送前既核对不了内容，也记不了账。
@pytest.mark.parametrize("resolved", ["", None, 42, "base64://c3RpY2tlcg=="])
def test_unresolved_marker_does_not_remove_surrounding_text(resolved) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    async def resolve(name: str):
        return resolved

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("前文[表情:未知]后文", bot, []))

    assert text_of(segments) == "前文后文"
    assert all(segment["type"] == "text" for segment in segments)


def test_local_failure_preserves_text() -> None:
    async def resolve(name: str):
        raise RuntimeError("synthetic local failure")

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("前文[表情:未知]后文", FaceBot(), []))

    assert text_of(segments) == "前文后文"


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
def test_collection_cancellation_propagates_without_resolving(metadata) -> None:
    bot = FaceBot(error=asyncio.CancelledError())

    async def resolve(name: str):
        pytest.fail("取消的发送不能继续解析本地表情包")

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(renderer.process_emojis("前文[表情:无语]", bot, [], metadata=metadata))


def test_local_cancellation_propagates() -> None:
    async def resolve(name: str):
        raise asyncio.CancelledError()

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(renderer.process_emojis("[表情:无语]", FaceBot(), []))


@pytest.mark.parametrize("from_metadata", [False, True])
def test_collection_match_has_priority_and_no_local_digest(from_metadata: bool) -> None:
    metadata = [{"name": "开心", "url": _FACE_URL}] if from_metadata else None
    bot = FaceBot(faces=[{"name": "开心", "url": _FACE_URL}])

    async def resolve(name: str):
        pytest.fail("收藏表情命中后不能再使用同名本地表情包")

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("[表情:开心]", bot, [], metadata=metadata))

    assert segments == [_FACE]
    assert len(bot.calls) == (0 if from_metadata else 1)


def test_digest_does_not_leak_from_local_to_next_collection() -> None:
    async def resolve(name: str):
        return _PICKED

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    bot = FaceBot(faces=[{"name": "收藏", "url": _FACE_URL}])
    segments = asyncio.run(renderer.process_emojis("[表情:本地][表情:收藏]", bot, []))

    assert segments == [_STICKER, _FACE]


@pytest.mark.parametrize("picked", [ResolvedSticker("", _CONTENT), ResolvedSticker(_DIGEST, "")])
def test_a_pick_missing_either_digest_emits_nothing(picked: ResolvedSticker) -> None:
    async def resolve(name: str):
        return picked

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    assert asyncio.run(renderer.process_emojis("[表情:本地]", FaceBot(), [])) == []


def test_management_lookup_still_reports_collection_api_failure() -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    with pytest.raises(RuntimeError, match="synthetic collection failure"):
        asyncio.run(emoji.EmojiRenderer().fetch_custom_face_details(bot))


def test_the_library_can_come_before_a_favourite_of_the_same_name() -> None:
    bot = FaceBot(faces=[{"name": "开心", "url": _FACE_URL}])

    async def resolve(name: str):
        return _PICKED

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("[表情:开心]", bot, [], lookup=(EMOJI_LIBRARY, EMOJI_NATIVE)))

    assert segments == [_STICKER]
    # 图库先命中，就不必再去拉收藏列表。
    assert bot.calls == []


def test_a_library_miss_still_falls_back_to_the_favourite() -> None:
    bot = FaceBot(faces=[{"name": "开心", "url": _FACE_URL}])

    async def resolve(name: str):
        return None

    renderer = emoji.EmojiRenderer(sticker_resolver=resolve)
    segments = asyncio.run(renderer.process_emojis("[表情:开心]", bot, [], lookup=(EMOJI_LIBRARY, EMOJI_NATIVE)))

    assert segments == [_FACE]
