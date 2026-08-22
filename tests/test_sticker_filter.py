"""表情包入库判定。

宽高解析全部拿 Pillow 生成的真实图片交叉验证 —— 手搓字节串测不出偏移量抄错。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from stickers import filter as sf
from stickers.filter import (
    DEFAULT_STRATEGY,
    ImageShape,
    decide,
    looks_like_screenshot,
    marked_as_sticker,
    normalize_strategy,
    read_shape,
)


def _encode(fmt: str, size: tuple[int, int], **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 30, 30)).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


# ── 宽高解析 ──

@pytest.mark.parametrize("fmt", ["PNG", "GIF", "JPEG", "WEBP", "BMP"])
@pytest.mark.parametrize("size", [(1, 1), (64, 64), (300, 180), (1080, 1920)])
def test_shape_matches_pillow(fmt: str, size: tuple[int, int]) -> None:
    shape = read_shape(_encode(fmt, size))
    assert shape is not None, f"{fmt} 认不出来"
    assert (shape.width, shape.height) == size


def test_non_square_orientation_is_not_swapped() -> None:
    # 宽高读反了在方图上测不出来，必须用长方形钉住。
    for fmt in ("PNG", "GIF", "JPEG", "WEBP", "BMP"):
        shape = read_shape(_encode(fmt, (400, 100)))
        assert shape is not None and shape.width == 400 and shape.height == 100, fmt


def test_animated_gif_is_flagged() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (50, 50), (i * 40, 0, 0)) for i in range(3)]
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    shape = read_shape(buffer.getvalue())
    assert shape is not None and shape.animated


def test_apng_is_flagged_as_animated() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (50, 50), (i * 40, 0, 0)) for i in range(3)]
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    shape = read_shape(buffer.getvalue())
    assert shape is not None and shape.animated


def test_animated_webp_is_flagged() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (50, 50), (i * 40, 0, 0)) for i in range(3)]
    frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    shape = read_shape(buffer.getvalue())
    assert shape is not None and shape.animated


def test_still_png_is_not_animated() -> None:
    shape = read_shape(_encode("PNG", (60, 60)))
    assert shape is not None and not shape.animated


def test_lossless_webp_is_parsed() -> None:
    # VP8L 的宽高塞在位域里，和 VP8X/VP8 是三套完全不同的布局。
    shape = read_shape(_encode("WEBP", (321, 123), lossless=True))
    assert shape is not None and (shape.width, shape.height) == (321, 123)


def test_only_the_header_is_needed() -> None:
    # 判定跑在每条群消息上，不能为了取宽高读整个文件。
    data = _encode("PNG", (800, 600))
    assert read_shape(data[:2048]) == read_shape(data)


@pytest.mark.parametrize("blob", [
    b"", b"x", b"not an image at all", b"\x89PNG\r\n\x1a\n",
    b"GIF89a", b"\xff\xd8", b"RIFF0000WEBP", b"BM",
    b"%PDF-1.4 fake", b"\x00" * 64,
])
def test_unrecognised_bytes_yield_none(blob: bytes) -> None:
    assert read_shape(blob) is None


def test_truncated_header_does_not_raise() -> None:
    data = _encode("JPEG", (500, 500))
    for cut in range(10, 200, 7):
        read_shape(data[:cut])


# ── QQ 自己的表情标记 ──

@pytest.mark.parametrize("seg_type", ["mface", "marketface", "MFace"])
def test_sticker_segment_types_are_marked(seg_type: str) -> None:
    assert marked_as_sticker(seg_type, {})


def test_sub_type_one_is_marked() -> None:
    # 群友发收藏表情时段类型仍是 image，只有 sub_type 能区分。
    assert marked_as_sticker("image", {"sub_type": 1})
    assert marked_as_sticker("image", {"subType": "1"})


def test_plain_image_is_not_marked() -> None:
    assert not marked_as_sticker("image", {})
    assert not marked_as_sticker("image", {"sub_type": 0})
    assert not marked_as_sticker("image", {"sub_type": "not a number"})


def test_summary_and_emoji_id_are_marked() -> None:
    assert marked_as_sticker("image", {"summary": "[表情]"})
    assert marked_as_sticker("image", {"summary": "[Sticker]"})
    assert marked_as_sticker("image", {"emoji_id": "abc123"})
    assert not marked_as_sticker("image", {"emoji_id": "   "})


# ── 截图判定 ──

def _shape(width: int, height: int, *, animated: bool = False) -> ImageShape:
    return ImageShape("png", width, height, animated)


@pytest.mark.parametrize("size", [
    (2400, 800),      # 长宽比 3
    (1920, 1080),     # 横屏截图
    (1080, 2340),     # 竖屏手机截图
    (2000, 1500),     # 大照片
    (1800, 520),      # 长条拼图
])
def test_screenshots_are_rejected(size: tuple[int, int]) -> None:
    assert looks_like_screenshot(_shape(*size))


@pytest.mark.parametrize("size", [
    (100, 100), (240, 240), (300, 200), (512, 512), (400, 300), (640, 480),
])
def test_typical_sticker_sizes_pass(size: tuple[int, int]) -> None:
    assert not looks_like_screenshot(_shape(*size))


def test_animated_images_are_exempt_regardless_of_size() -> None:
    # 表情包大量是 GIF，而截图不会是动的；尺寸规则对动图一律不适用。
    assert not looks_like_screenshot(_shape(1920, 1080, animated=True))
    assert not looks_like_screenshot(_shape(2400, 600, animated=True))


def test_unknown_shape_is_not_rejected() -> None:
    # 认不出格式时宁可收进来，让待审池去筛，别静默丢图。
    assert not looks_like_screenshot(None)


def test_zero_dimensions_are_not_rejected() -> None:
    assert not looks_like_screenshot(_shape(0, 0))


# ── 三档策略 ──

def test_none_strategy_takes_everything() -> None:
    assert decide("none", marked=False, shape=_shape(1920, 1080)) == (True, "")


def test_strict_strategy_needs_the_qq_mark() -> None:
    assert decide("strict", marked=True, shape=None) == (True, "")
    ok, why = decide("strict", marked=False, shape=_shape(240, 240))
    assert not ok and why == "not_marked_sticker"


def test_loose_strategy_trusts_the_mark_over_the_size() -> None:
    # 官方表情包也可能是长条的，标了就别再拿尺寸否决。
    assert decide("loose", marked=True, shape=_shape(2400, 600)) == (True, "")


def test_loose_strategy_drops_unmarked_screenshots() -> None:
    ok, why = decide("loose", marked=False, shape=_shape(1080, 2340))
    assert not ok and why == "looks_like_screenshot"


def test_loose_strategy_keeps_unmarked_small_images() -> None:
    assert decide("loose", marked=False, shape=_shape(300, 300)) == (True, "")


@pytest.mark.parametrize("raw", ["", "  ", "bogus", None, "LOOSE", "Strict"])
def test_strategy_normalisation(raw: object) -> None:
    value = normalize_strategy(raw)
    assert value in ("none", "loose", "strict")
    if str(raw or "").strip().lower() not in ("none", "loose", "strict"):
        assert value == DEFAULT_STRATEGY


def test_unknown_strategy_behaves_like_the_default() -> None:
    assert decide("bogus", marked=False, shape=_shape(1080, 2340)) == decide(
        DEFAULT_STRATEGY, marked=False, shape=_shape(1080, 2340)
    )
