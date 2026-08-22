"""表情包入库判定：这张图收不收。

只读文件头前 64KB 取宽高，不解码整图 —— 判定跑在每条群消息的路径上。
零平台依赖，可脱 NoneBot 单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 收得越松，垃圾越多；收得越严，漏得越多。三档由配置选。
STRATEGIES = ("none", "loose", "strict")
DEFAULT_STRATEGY = "loose"

_HEADER_BYTES = 64 * 1024

# QQ 把收藏表情发成带这个子类型的 image 段，商城表情则另有段类型。
_STICKER_SUB_TYPE = 1
_STICKER_SEGMENT_TYPES = frozenset({"mface", "marketface"})
_STICKER_SUMMARY_HINTS = ("表情", "emoji", "sticker", "贴纸")


@dataclass(frozen=True)
class ImageShape:
    fmt: str
    width: int
    height: int
    animated: bool


def normalize_strategy(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in STRATEGIES else DEFAULT_STRATEGY


def marked_as_sticker(seg_type: str, data: Mapping[str, Any]) -> bool:
    """QQ 自己说这是表情包吗。

    命中任一即算：商城/群友表情段、收藏表情的子类型、摘要文案、表情资源 id。
    """
    if str(seg_type or "").strip().lower() in _STICKER_SEGMENT_TYPES:
        return True
    raw_sub = data.get("sub_type", data.get("subType"))
    try:
        if raw_sub is not None and int(raw_sub) == _STICKER_SUB_TYPE:
            return True
    except (TypeError, ValueError):
        pass
    summary = str(data.get("summary") or "").lower()
    if any(hint in summary for hint in _STICKER_SUMMARY_HINTS):
        return True
    return bool(str(data.get("emoji_id") or data.get("emojiId") or "").strip())


def _png(data: bytes) -> ImageShape | None:
    if len(data) < 24:
        return None
    return ImageShape(
        "png",
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
        # acTL 是 APNG 的动画控制块，出现即多帧。
        b"acTL" in data,
    )


def _gif(data: bytes) -> ImageShape | None:
    if len(data) < 10:
        return None
    # 单帧 GIF 极少见，且表情包几乎都是动图，一律按动图豁免更省事。
    return ImageShape(
        "gif",
        int.from_bytes(data[6:8], "little"),
        int.from_bytes(data[8:10], "little"),
        True,
    )


def _bmp(data: bytes) -> ImageShape | None:
    if len(data) < 26:
        return None
    dib = int.from_bytes(data[14:18], "little")
    if dib == 12:
        width = int.from_bytes(data[18:20], "little")
        height = int.from_bytes(data[20:22], "little")
    elif len(data) >= 30:
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
    else:
        return None
    # 高度为负表示自上而下存储，尺寸取绝对值。
    return ImageShape("bmp", abs(width), abs(height), False)


# SOF0..SOF15，跳过 SOF4/8/12（DHT/JPG/DAC，不是帧头）。
_JPEG_FRAME_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg(data: bytes) -> ImageShape | None:
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        # 填充字节 0xFF 可以连续出现任意多个。
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2:
            break
        if marker in _JPEG_FRAME_MARKERS and index + 7 <= len(data):
            return ImageShape(
                "jpeg",
                int.from_bytes(data[index + 5 : index + 7], "big"),
                int.from_bytes(data[index + 3 : index + 5], "big"),
                False,
            )
        index += length
    return None


def _webp(data: bytes) -> ImageShape | None:
    index = 12
    animated = False
    while index + 8 <= len(data):
        chunk = data[index : index + 4]
        size = int.from_bytes(data[index + 4 : index + 8], "little")
        payload = index + 8
        end = payload + size
        if end > len(data):
            break
        if chunk == b"VP8X" and size >= 10:
            return ImageShape(
                "webp",
                int.from_bytes(data[payload + 4 : payload + 7], "little") + 1,
                int.from_bytes(data[payload + 7 : payload + 10], "little") + 1,
                bool(data[payload] & 0x02),
            )
        if chunk == b"VP8 " and size >= 10:
            at = data.find(b"\x9d\x01\x2a", payload, end)
            if at >= 0 and at + 7 <= len(data):
                return ImageShape(
                    "webp",
                    int.from_bytes(data[at + 3 : at + 5], "little") & 0x3FFF,
                    int.from_bytes(data[at + 5 : at + 7], "little") & 0x3FFF,
                    animated,
                )
        if chunk == b"VP8L" and size >= 5 and data[payload] == 0x2F:
            bits = int.from_bytes(data[payload + 1 : payload + 5], "little")
            return ImageShape(
                "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, animated
            )
        if chunk == b"ANIM":
            animated = True
        # RIFF 块按偶数字节对齐，奇数长度后面跟一个填充字节。
        index = end + (size % 2)
    return None


def read_shape(data: bytes) -> ImageShape | None:
    """按魔数认格式取宽高。认不出返回 None，判定方按"不确定"处理。"""
    if len(data) < 10:
        return None
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return _png(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _gif(data)
        if data.startswith(b"BM"):
            return _bmp(data)
        if data.startswith(b"\xff\xd8"):
            return _jpeg(data)
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return _webp(data)
    except (IndexError, ValueError):
        return None
    return None


def looks_like_screenshot(shape: ImageShape | None) -> bool:
    """像截图或大照片而不是表情包。

    动图直接豁免：表情包绝大多数是 GIF/APNG，而截图不会是动的。
    """
    if shape is None or shape.animated:
        return False
    width, height = shape.width, shape.height
    if width <= 0 or height <= 0:
        return False
    long_side, short_side = max(width, height), min(width, height)
    ratio = long_side / max(short_side, 1)

    if ratio > 2.5:
        return True
    if width * height >= 1_600_000 and long_side >= 1600 and short_side >= 900:
        return True
    # 竖屏手机截图
    if height >= 1600 and width >= 900 and 1.7 <= height / max(width, 1) <= 2.4:
        return True
    # 横屏/桌面截图
    if width >= 1600 and height >= 900 and 1.55 <= width / max(height, 1) <= 2.2:
        return True
    # 长条拼图、聊天记录长图
    return long_side >= 1800 and short_side >= 500 and ratio >= 3.0


def decide(strategy: str, *, marked: bool, shape: ImageShape | None) -> tuple[bool, str]:
    """收不收，以及不收的原因。原因串会进日志和控制台，别改字面量。"""
    mode = normalize_strategy(strategy)
    if mode == "strict":
        return (True, "") if marked else (False, "not_marked_sticker")
    if mode == "loose":
        # QQ 已经标了的直接放行，不必再拿尺寸猜。
        if marked:
            return True, ""
        if looks_like_screenshot(shape):
            return False, "looks_like_screenshot"
    return True, ""
