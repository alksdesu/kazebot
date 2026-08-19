"""工具子进程发出去的图片载荷。

扩展名会撒谎，各家收的格式又不一样。这里盯的是「发出去之前就知道对面收不收」——
发出去再吃 400，用户只会看到一句看不懂的 API 报错。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import _image  # noqa: E402
from _image import (  # noqa: E402
    ImagePayloadError,
    accepted_mimes,
    build_image_part,
    family_for_base_url,
    sniff_image_mime,
)


def _encode(fmt: str, *, size: tuple[int, int] = (8, 8), mode: str = "RGB") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else 128).save(buf, format=fmt)
    return buf.getvalue()


_FRAME_COLOURS = ((220, 20, 20), (20, 220, 20), (20, 20, 220))


def _animated_gif() -> bytes:
    from PIL import Image

    frames = [Image.new("RGB", (8, 8), colour) for colour in _FRAME_COLOURS]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=40)
    return buf.getvalue()


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    target = tmp_path / name
    target.write_bytes(data)
    return target


class TestSniff:
    @pytest.mark.parametrize("fmt,mime", [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
        ("BMP", "image/bmp"),
        ("TIFF", "image/tiff"),
    ])
    def test_it_reads_the_file_header(self, fmt: str, mime: str) -> None:
        assert sniff_image_mime(_encode(fmt)) == mime

    def test_svg_is_recognised_even_behind_an_xml_prolog(self) -> None:
        head = b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'
        assert sniff_image_mime(head) == "image/svg+xml"

    def test_heic_is_recognised_by_its_brand(self) -> None:
        assert sniff_image_mime(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "image/heic"

    def test_avif_is_not_mistaken_for_heic(self) -> None:
        assert sniff_image_mime(b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00") == "image/avif"

    def test_a_non_image_is_not_forced_into_a_format(self) -> None:
        # 认不出就返回空串，让上层拒掉；猜一个 image/png 会把 PDF 当图片发出去。
        assert sniff_image_mime(b"%PDF-1.7\n") == ""
        assert sniff_image_mime(b"") == ""


class TestFamilyFromUrl:
    @pytest.mark.parametrize("url,family", [
        ("https://generativelanguage.googleapis.com/v1beta", "gemini"),
        ("generativelanguage.googleapis.com", "gemini"),
        ("https://api.anthropic.com/v1", "anthropic"),
        ("https://api.openai.com/v1", "openai"),
        ("https://api.deepseek.com", "openai"),
        ("", "openai"),
    ])
    def test_it_reads_the_host(self, url: str, family: str) -> None:
        assert family_for_base_url(url) == family

    def test_a_relay_falls_back_to_the_smallest_accept_set(self) -> None:
        # 中转站的域名看不出出身。按 openai 算最多多转一次 PNG，认错成 gemini 才会出事。
        assert family_for_base_url("https://relay.mycompany.cn/v1") == "openai"
        assert accepted_mimes("openai") <= accepted_mimes("gemini") | {"image/gif"}

    def test_a_port_or_credentials_do_not_confuse_it(self) -> None:
        assert family_for_base_url("https://api.anthropic.com:8443/v1") == "anthropic"
        assert family_for_base_url("https://user@api.anthropic.com/v1") == "anthropic"


class TestAcceptSets:
    def test_gemini_takes_heic_and_openai_does_not(self) -> None:
        # 这条差别是 family 参数存在的理由：Pillow 默认读不了 HEIC，转不了只能直发。
        assert "image/heic" in accepted_mimes("gemini")
        assert "image/heic" not in accepted_mimes("openai")

    def test_nobody_takes_gif_straight_through(self) -> None:
        # 动图各家行为不一致，一律取第一帧转 PNG 比逐家分情况可靠。
        for family in ("openai", "anthropic", "gemini"):
            assert "image/gif" not in accepted_mimes(family)

    def test_an_unknown_family_uses_the_smallest_set(self) -> None:
        assert accepted_mimes("whatever") == accepted_mimes("openai")


class TestBuildPart:
    def test_an_accepted_format_goes_out_untouched(self, tmp_path: Path) -> None:
        data = _encode("PNG")
        part = build_image_part(_write(tmp_path, "a.png", data), "openai")
        import base64
        assert part.mime == "image/png"
        assert base64.b64decode(part.b64) == data

    def test_the_extension_does_not_decide(self, tmp_path: Path) -> None:
        # QQ 落盘的 .png 里装着 GIF 是常事。按扩展名发出去就是一个 400。
        part = build_image_part(_write(tmp_path, "liar.png", _encode("GIF")), "gemini")
        assert part.mime == "image/png"
        assert sniff_image_mime(__import__("base64").b64decode(part.b64)) == "image/png"

    @pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
    def test_a_format_nobody_takes_is_converted(self, tmp_path: Path, fmt: str) -> None:
        part = build_image_part(_write(tmp_path, "x." + fmt.lower(), _encode(fmt)), "gemini")
        assert part.mime == "image/png"

    def test_an_animated_gif_sends_its_first_frame(self, tmp_path: Path) -> None:
        # 送第几帧不能碰运气：GIF 解出来的当前帧取决于上一次 seek 停在哪。
        from PIL import Image

        part = build_image_part(_write(tmp_path, "loop.gif", _animated_gif()), "openai")
        out = Image.open(io.BytesIO(__import__("base64").b64decode(part.b64))).convert("RGB")
        assert getattr(out, "is_animated", False) is False
        assert out.getpixel((4, 4)) == _FRAME_COLOURS[0]

    def test_transparency_survives_the_conversion(self, tmp_path: Path) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (8, 8), (10, 20, 30, 0)).save(buf, format="TIFF")
        part = build_image_part(_write(tmp_path, "t.tiff", buf.getvalue()), "openai")
        assert Image.open(io.BytesIO(__import__("base64").b64decode(part.b64))).mode in ("RGBA", "LA", "P")

    def test_an_indexed_gif_does_not_balloon(self, tmp_path: Path) -> None:
        # 索引色转 RGB 能让一张表情包大出一个数量级，转完反而撞上体积上限。
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 400), (12, 200, 90)).convert("P").save(buf, format="GIF")
        raw = buf.getvalue()

        part = build_image_part(_write(tmp_path, "big.gif", raw), "gemini")
        out = Image.open(io.BytesIO(__import__("base64").b64decode(part.b64)))

        assert out.mode == "P"
        assert len(__import__("base64").b64decode(part.b64)) < len(raw) * 4

    def test_a_cmyk_image_is_converted_to_rgb(self, tmp_path: Path) -> None:
        # PNG 收不下 CMYK，不转就是一个写文件时才炸的 OSError。
        from PIL import Image

        buf = io.BytesIO()
        Image.new("CMYK", (8, 8)).save(buf, format="TIFF")
        part = build_image_part(_write(tmp_path, "c.tiff", buf.getvalue()), "openai")

        assert Image.open(io.BytesIO(__import__("base64").b64decode(part.b64))).mode == "RGB"

    def test_heic_reaches_gemini_without_a_transcode(self, tmp_path: Path) -> None:
        # Pillow 读不了 HEIC，走转码分支就等于把一张 Gemini 本来收得下的图变成报错。
        raw = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 64
        part = build_image_part(_write(tmp_path, "p.heic", raw), "gemini")
        assert part.mime == "image/heic"


class TestRefusal:
    def test_svg_says_what_to_do_instead(self, tmp_path: Path) -> None:
        target = _write(tmp_path, "logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with pytest.raises(ImagePayloadError) as caught:
            build_image_part(target, "openai")
        assert "SVG" in str(caught.value)
        assert "PNG" in str(caught.value)
        assert "logo.svg" in str(caught.value)

    def test_a_non_image_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ImagePayloadError, match="report.pdf"):
            build_image_part(_write(tmp_path, "report.pdf", b"%PDF-1.7\n" + b"x" * 64), "openai")

    def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImagePayloadError, match="空文件"):
            build_image_part(_write(tmp_path, "e.png", b""), "openai")

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImagePayloadError):
            build_image_part(tmp_path / "nope.png", "openai")

    def test_heic_hitting_openai_explains_itself(self, tmp_path: Path, monkeypatch) -> None:
        # OpenAI 不收 HEIC，而 Pillow 默认也转不了。这时必须说人话，不能发出去吃 400。
        target = _write(tmp_path, "photo.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
        with pytest.raises(ImagePayloadError) as caught:
            build_image_part(target, "openai")
        assert "photo.heic" in str(caught.value)
        assert "HEIC" in str(caught.value)

    def test_a_missing_pillow_is_reported_as_such(self, tmp_path: Path, monkeypatch) -> None:
        def explode(_data: bytes) -> bytes:
            raise ImportError("No module named 'PIL'")

        monkeypatch.setattr(_image, "_to_png", explode)
        with pytest.raises(ImagePayloadError, match="Pillow"):
            build_image_part(_write(tmp_path, "x.bmp", _encode("BMP")), "openai")


class TestDataUrl:
    def test_it_is_the_shape_openai_expects(self, tmp_path: Path) -> None:
        part = build_image_part(_write(tmp_path, "a.png", _encode("PNG")), "openai")
        assert part.data_url().startswith("data:image/png;base64,")
        assert part.data_url().endswith(part.b64)
