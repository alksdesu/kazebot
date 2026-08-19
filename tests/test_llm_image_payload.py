"""file:// 附件变成 provider 收得下的 base64 之前的那一段。

这里是所有渠道的共用瓶颈：压缩层恒定输出 JPEG，四家 provider 才永远撞不到
GIF/BMP/HEIC 这些各家支持列表不一致的边角。这条性质塌了，四家一起 400。
"""
from __future__ import annotations

import base64
import io
import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.attachments import prepare_messages_for_llm  # noqa: E402


def _png(size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _encode(fmt: str, size: tuple[int, int] = (8, 8)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format=fmt)
    return buf.getvalue()


def _attach(root: Path, name: str, data: bytes) -> str:
    target = root / "data" / "attachments" / "sess"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(data)
    return "file://data/attachments/sess/" + name


def _user(url: str, text: str = "看看这个") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def _parts(messages: list[dict], kind: str) -> list[dict]:
    return [p for m in messages for p in m["content"] if isinstance(p, dict) and p.get("type") == kind]


def _decode(part: dict) -> tuple[str, bytes]:
    url = part["image_url"]["url"]
    header, payload = url.split(",", 1)
    return header[len("data:"):-len(";base64")], base64.b64decode(payload)


class TestNormalisation:
    @pytest.mark.parametrize("fmt,name", [
        ("PNG", "a.png"), ("GIF", "a.gif"), ("BMP", "a.bmp"),
        ("WEBP", "a.webp"), ("TIFF", "a.tiff"), ("JPEG", "a.jpg"),
    ])
    def test_everything_leaves_as_jpeg(self, tmp_path: Path, fmt: str, name: str) -> None:
        # 这条性质是四家 provider 都安全的唯一原因，不是「每家转换都写对了」。
        url = _attach(tmp_path, name, _encode(fmt))
        out = prepare_messages_for_llm([_user(url)], tmp_path)
        mime, _data = _decode(_parts(out, "image_url")[0])
        assert mime == "image/jpeg"

    def test_the_extension_does_not_decide(self, tmp_path: Path) -> None:
        # 附件名是用户给的。.png 里装着 BMP 时按扩展名走会把错的 mime 报给对面。
        url = _attach(tmp_path, "liar.png", _encode("BMP"))
        mime, data = _decode(_parts(prepare_messages_for_llm([_user(url)], tmp_path), "image_url")[0])
        assert mime == "image/jpeg"
        assert data.startswith(b"\xff\xd8\xff")

    def test_an_oversized_image_is_scaled_down(self, tmp_path: Path) -> None:
        from PIL import Image

        url = _attach(tmp_path, "big.png", _png((3000, 1500)))
        _mime, data = _decode(_parts(prepare_messages_for_llm([_user(url)], tmp_path), "image_url")[0])
        assert max(Image.open(io.BytesIO(data)).size) == 1024

    def test_a_small_image_is_not_upscaled(self, tmp_path: Path) -> None:
        from PIL import Image

        url = _attach(tmp_path, "small.png", _png((40, 20)))
        _mime, data = _decode(_parts(prepare_messages_for_llm([_user(url)], tmp_path), "image_url")[0])
        assert Image.open(io.BytesIO(data)).size == (40, 20)


class TestRefusal:
    def test_svg_never_reaches_a_provider(self, tmp_path: Path) -> None:
        # 矢量图没有哪家收，发出去是整轮请求 400 —— 掉一张图比掉一整轮回复强。
        url = _attach(tmp_path, "logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        out = prepare_messages_for_llm([_user(url)], tmp_path)
        assert _parts(out, "image_url") == []
        assert any("Image unavailable" in p["text"] for p in _parts(out, "text"))

    def test_a_svg_wearing_a_png_name_is_stopped_by_its_content(self, tmp_path: Path, caplog) -> None:
        # 靠「压缩层反正打不开它」兜底不算数：黑名单里以后加的格式未必 PIL 也打不开。
        url = _attach(tmp_path, "sneaky.png", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with caplog.at_level(logging.WARNING, logger="engine.attachments"):
            out = prepare_messages_for_llm([_user(url)], tmp_path)
        assert _parts(out, "image_url") == []
        assert any("no provider accepts" in r.getMessage() for r in caplog.records)

    def test_a_non_image_becomes_a_placeholder(self, tmp_path: Path) -> None:
        url = _attach(tmp_path, "report.png", b"%PDF-1.7\n" + b"x" * 200)
        out = prepare_messages_for_llm([_user(url)], tmp_path)
        assert _parts(out, "image_url") == []
        assert any("Image unavailable" in p["text"] for p in _parts(out, "text"))

    def test_a_missing_file_becomes_a_placeholder(self, tmp_path: Path) -> None:
        out = prepare_messages_for_llm([_user("file://data/attachments/sess/gone.png")], tmp_path)
        assert _parts(out, "image_url") == []
        assert _parts(out, "text")[-1]["text"] == "[Image unavailable]"

    def test_the_user_text_survives_a_dropped_image(self, tmp_path: Path) -> None:
        # 图掉了模型至少要知道用户发过图，否则回复会答非所问。
        url = _attach(tmp_path, "logo.svg", b"<svg/>")
        out = prepare_messages_for_llm([_user(url, "这张图里写了什么")], tmp_path)
        assert [p["text"] for p in _parts(out, "text")] == ["这张图里写了什么", "[Image unavailable]"]


class TestPathGuard:
    @pytest.mark.parametrize("rel", [
        "file://../../.env",
        "file://data/../.env",
        "file://config/qq.yaml",
        "file:///etc/passwd",
    ])
    def test_paths_outside_data_are_refused(self, tmp_path: Path, rel: str) -> None:
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-real", encoding="utf-8")
        out = prepare_messages_for_llm([_user(rel)], tmp_path)
        assert _parts(out, "image_url") == []
        assert "sk-real" not in str(out)

    def test_a_real_image_outside_data_is_still_refused(self, tmp_path: Path) -> None:
        # 上面几条最终都被「不是图片，压缩失败」挡住了，验不到白名单本身。
        # 真正要拦的是 workspace 里 data/ 之外的合法图片。
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "leak.png").write_bytes(_png())
        out = prepare_messages_for_llm([_user("file://secrets/leak.png")], tmp_path)
        assert _parts(out, "image_url") == []


class TestMessageShape:
    def test_assistant_images_are_stripped(self, tmp_path: Path) -> None:
        # Claude 不收 assistant 角色里的图片块。
        url = _attach(tmp_path, "a.png", _png())
        out = prepare_messages_for_llm([{"role": "assistant", "content": [
            {"type": "text", "text": "画好了"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}], tmp_path)
        assert out[0]["content"] == "画好了"

    def test_an_image_only_assistant_turn_keeps_a_placeholder(self, tmp_path: Path) -> None:
        url = _attach(tmp_path, "a.png", _png())
        out = prepare_messages_for_llm(
            [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": url}}]}],
            tmp_path,
        )
        assert out[0]["content"] == "[image attachment]"

    def test_plain_string_content_is_untouched(self, tmp_path: Path) -> None:
        messages = [{"role": "user", "content": "没有图"}]
        assert prepare_messages_for_llm(messages, tmp_path) == messages

    def test_an_already_resolved_data_url_is_left_alone(self, tmp_path: Path) -> None:
        url = "data:image/jpeg;base64,/9j/4AAQ"
        out = prepare_messages_for_llm([_user(url)], tmp_path)
        assert _parts(out, "image_url")[0]["image_url"]["url"] == url

    def test_internal_flags_are_dropped_but_meta_stays(self, tmp_path: Path) -> None:
        # _meta 要活到 provider 层：Responses 靠它还原原生工具历史。
        out = prepare_messages_for_llm(
            [{"role": "user", "content": "hi", "_meta": {"id": "x"}, "_internal": 1}], tmp_path,
        )
        assert out[0]["_meta"] == {"id": "x"}
        assert "_internal" not in out[0]

    def test_the_same_file_is_only_encoded_once(self, tmp_path: Path) -> None:
        url = _attach(tmp_path, "same.png", _png())
        out = prepare_messages_for_llm([_user(url), _user(url)], tmp_path)
        first, second = _parts(out, "image_url")
        assert first["image_url"]["url"] == second["image_url"]["url"]

    def test_the_input_list_is_not_mutated(self, tmp_path: Path) -> None:
        url = _attach(tmp_path, "a.png", _png())
        messages = [_user(url)]
        prepare_messages_for_llm(messages, tmp_path)
        assert messages[0]["content"][1]["image_url"]["url"] == url
