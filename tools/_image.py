"""工具子进程共用的图片载荷构建。

各家收的格式不一样（Gemini 不收 GIF/BMP，OpenAI 不收 BMP/HEIC），扩展名又经常和真实内容
对不上。这里按文件头认格式，目标家不收的就地转成 PNG，转不动才报错——别拿去换一个 400。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import NamedTuple

# 各家 vision 接口收的图片格式。GIF 一律不列：动图行为各家不一致（OpenAI 只收静态），
# 取第一帧转 PNG 对谁都成立，比逐家分情况可靠。
ACCEPTED_IMAGE_MIMES: dict[str, frozenset[str]] = {
    "openai": frozenset({"image/png", "image/jpeg", "image/webp"}),
    "anthropic": frozenset({"image/png", "image/jpeg", "image/webp"}),
    "gemini": frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}),
}

# 转不成 PNG 的格式，报错时照这份给人话。
_FORMAT_NAMES: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
    "image/bmp": "BMP",
    "image/tiff": "TIFF",
    "image/heic": "HEIC",
    "image/heif": "HEIF",
    "image/avif": "AVIF",
    "image/svg+xml": "SVG",
}

# 官方域名 → 收哪套格式。中转站的地址看不出出身，认不出按 openai 算就行。
_HOST_FAMILIES: dict[str, str] = {
    "generativelanguage.googleapis.com": "gemini",
    "api.anthropic.com": "anthropic",
}

_HEIF_BRANDS = frozenset({
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1",
})
_AVIF_BRANDS = frozenset({b"avif", b"avis"})


class ImagePayloadError(Exception):
    """图片没法交给目标接口。message 是给用户看的，工具直接 fail() 出去。"""


class ImagePart(NamedTuple):
    """一张已经确认目标家收得下的图。"""

    mime: str
    b64: str

    def data_url(self) -> str:
        return "data:" + self.mime + ";base64," + self.b64


def sniff_image_mime(data: bytes) -> str:
    """按文件头判断图片格式，认不出返回空串。

    只认文件头：QQ 落盘的 .png 里装着 GIF 是常事，扩展名说了不算。
    """
    head = bytes(data[:32])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II\x2a\x00", b"MM\x00\x2a")):
        return "image/tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIF_BRANDS:
            return "image/heic"
        if brand in _AVIF_BRANDS:
            return "image/avif"
    if b"<svg" in data[:1024].lower():
        return "image/svg+xml"
    return ""


def format_name(mime: str) -> str:
    return _FORMAT_NAMES.get(mime, mime or "未知格式")


def accepted_mimes(family: str) -> frozenset[str]:
    """目标接口收的格式。不认识的族按 OpenAI 那套算——它是最小公约数。"""
    return ACCEPTED_IMAGE_MIMES.get((family or "").strip().lower(), ACCEPTED_IMAGE_MIMES["openai"])


def family_for_base_url(base_url: str) -> str:
    """从接口地址认出对面收哪套格式。

    认不出按 openai 算：它的接受集最小，多转一次 PNG 不会错。反过来把 Gemini 认成 OpenAI
    就会去转 HEIC，而 Pillow 默认读不了 HEIC，本来能直发的图会白白失败。
    """
    text = (base_url or "").strip().lower()
    if not text:
        return "openai"
    if "://" in text:
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split("@")[-1].split(":")[0]
    return _HOST_FAMILIES.get(host, "openai")


def _to_png(data: bytes) -> bytes:
    """转 PNG。无损、带透明、三家都收，所以不挑目标家。"""
    from PIL import Image  # 只在真要转的时候才需要，装没装都不影响直发路径。

    img = Image.open(io.BytesIO(data))
    if getattr(img, "is_animated", False):
        img.seek(0)  # 读 is_animated 会让 Pillow 遍历全部帧，指针不保证停在第 0 帧。
    # PNG 自己就支持调色板和灰度，别顺手转成 RGB —— 一张索引色 GIF 转 RGB 能大出一个数量级。
    if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        img = img.convert("RGBA" if "A" in img.mode else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_image_part(path: str | Path, family: str) -> ImagePart:
    """读一张本地图片，转成目标家收得下的 base64 载荷。

    转不了就抛 ImagePayloadError —— 发出去吃 400 的话，用户只会看到一句 API 报错。
    """
    file = Path(path)
    try:
        data = file.read_bytes()
    except Exception as exc:
        raise ImagePayloadError("读不了图片 " + str(path) + "：" + str(exc)) from exc
    if not data:
        raise ImagePayloadError("图片是空文件：" + str(path))

    mime = sniff_image_mime(data)
    if not mime:
        raise ImagePayloadError(
            "认不出 " + file.name + " 是什么图片格式（文件头不匹配任何已知格式）。"
            "如果它其实不是图片，别把它当垫图传。"
        )
    if mime in accepted_mimes(family):
        return ImagePart(mime, base64.b64encode(data).decode("ascii"))

    if mime == "image/svg+xml":
        raise ImagePayloadError(
            "SVG 是矢量图，没有哪家视觉接口收它。请先导出成 PNG 再传：" + file.name
        )
    try:
        converted = _to_png(data)
    except ImportError as exc:
        raise ImagePayloadError(
            format_name(mime) + " 要转成 PNG 才能发给 " + (family or "目标接口")
            + "，但当前环境没装 Pillow。请先手动转成 PNG 或 JPEG：" + file.name
        ) from exc
    except Exception as exc:
        raise ImagePayloadError(
            format_name(mime) + " 转 PNG 失败（" + str(exc) + "）："
            + file.name + "。请换一张 PNG 或 JPEG。"
        ) from exc
    return ImagePart("image/png", base64.b64encode(converted).decode("ascii"))
