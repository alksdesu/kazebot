"""收藏接口不可用时，本地表情包和回复正文仍可发送。"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DIGEST = "a" * 64
_LOCAL_URL = "base64://c3RpY2tlcg=="
_FACE_URL = "https://example.invalid/face.gif"


@pytest.fixture()
def emoji(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(
        "_qq_emoji_fallback_test", _ROOT / "adapters/onebot/emoji_handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


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
def test_emoji_marker_detection_preserves_aliases(emoji, text: str) -> None:
    assert emoji.has_emoji_markers(text)


@pytest.mark.parametrize("text", ["", "普通文字", "哈哈😂", "[QQ表情:微笑]", "[表情:]", "[表情:未闭合"])
def test_emoji_marker_detection_ignores_plain_text(emoji, text: str) -> None:
    assert not emoji.has_emoji_markers(text)


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
@pytest.mark.parametrize("typed", [False, True])
def test_collection_failure_falls_back_to_local_sticker(emoji, metadata, typed: bool) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))
    selected: list[str] = []

    async def resolve(name: str):
        selected.append(name)
        return emoji.ResolvedSticker(_LOCAL_URL, _DIGEST) if typed else _LOCAL_URL

    emoji.set_sticker_resolver(resolve)
    segments = asyncio.run(emoji.process_emojis("前文[表情:无语]后文", bot, [], metadata=metadata))

    image = {"type": "image", "url": _LOCAL_URL, "emoji": True}
    if typed:
        image["sticker_sha256"] = _DIGEST
    assert segments == [{"type": "text", "content": "前文"}, image, {"type": "text", "content": "后文"}]
    assert selected == ["无语"]
    assert bot.calls == ["fetch_custom_face_detail"]


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
def test_collection_failure_is_not_retried_for_every_marker(emoji, metadata) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    async def resolve(name: str):
        return emoji.ResolvedSticker(_LOCAL_URL, _DIGEST)

    emoji.set_sticker_resolver(resolve)
    segments = asyncio.run(emoji.process_emojis("[表情:开心][表情:无语]", bot, [], metadata=metadata))

    assert len(segments) == 2
    assert all(segment["sticker_sha256"] == _DIGEST for segment in segments)
    assert len(bot.calls) == 1


@pytest.mark.parametrize("resolved", ["", None, 42])
def test_unresolved_marker_does_not_remove_surrounding_text(emoji, resolved) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    async def resolve(name: str):
        return resolved

    emoji.set_sticker_resolver(resolve)
    segments = asyncio.run(emoji.process_emojis("前文[表情:未知]后文", bot, []))

    assert text_of(segments) == "前文后文"
    assert all(segment["type"] == "text" for segment in segments)


def test_local_failure_preserves_text(emoji) -> None:
    async def resolve(name: str):
        raise RuntimeError("synthetic local failure")

    emoji.set_sticker_resolver(resolve)
    segments = asyncio.run(emoji.process_emojis("前文[表情:未知]后文", FaceBot(), []))

    assert text_of(segments) == "前文后文"


@pytest.mark.parametrize("metadata", [None, [{"name": "其他收藏", "url": _FACE_URL}]])
def test_collection_cancellation_propagates_without_resolving(emoji, metadata) -> None:
    bot = FaceBot(error=asyncio.CancelledError())

    async def resolve(name: str):
        pytest.fail("取消的发送不能继续解析本地表情包")

    emoji.set_sticker_resolver(resolve)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(emoji.process_emojis("前文[表情:无语]", bot, [], metadata=metadata))


def test_local_cancellation_propagates(emoji) -> None:
    async def resolve(name: str):
        raise asyncio.CancelledError()

    emoji.set_sticker_resolver(resolve)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(emoji.process_emojis("[表情:无语]", FaceBot(), []))


@pytest.mark.parametrize("from_metadata", [False, True])
def test_collection_match_has_priority_and_no_local_digest(emoji, from_metadata: bool) -> None:
    metadata = [{"name": "开心", "url": _FACE_URL}] if from_metadata else None
    bot = FaceBot(faces=[{"name": "开心", "url": _FACE_URL}])

    async def resolve(name: str):
        pytest.fail("收藏表情命中后不能再使用同名本地表情包")

    emoji.set_sticker_resolver(resolve)
    segments = asyncio.run(emoji.process_emojis("[表情:开心]", bot, [], metadata=metadata))

    assert segments == [{"type": "image", "url": _FACE_URL, "emoji": True}]
    assert len(bot.calls) == (0 if from_metadata else 1)


def test_digest_does_not_leak_from_local_to_next_collection(emoji) -> None:
    async def resolve(name: str):
        return emoji.ResolvedSticker(_LOCAL_URL, _DIGEST)

    emoji.set_sticker_resolver(resolve)
    bot = FaceBot(faces=[{"name": "收藏", "url": _FACE_URL}])
    segments = asyncio.run(emoji.process_emojis("[表情:本地][表情:收藏]", bot, []))

    assert segments == [
        {"type": "image", "url": _LOCAL_URL, "emoji": True, "sticker_sha256": _DIGEST},
        {"type": "image", "url": _FACE_URL, "emoji": True},
    ]


def test_empty_typed_result_does_not_emit_image_or_receipt(emoji) -> None:
    async def resolve(name: str):
        return emoji.ResolvedSticker("", _DIGEST)

    emoji.set_sticker_resolver(resolve)
    assert asyncio.run(emoji.process_emojis("[表情:本地]", FaceBot(), [])) == []


def test_management_lookup_still_reports_collection_api_failure(emoji) -> None:
    bot = FaceBot(error=RuntimeError("synthetic collection failure"))

    with pytest.raises(RuntimeError, match="synthetic collection failure"):
        asyncio.run(emoji.fetch_custom_face_details(bot))
