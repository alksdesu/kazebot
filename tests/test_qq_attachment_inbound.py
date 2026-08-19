"""QQ 附件入站：引用回退补全、图片魔数嗅探、下载体积上限。

这三条都在“拿到字节之前/之后如何判定”上做闸门，回归失效在日志里看不出来，
只会表现为“引用没内容 / 存了个说谎后缀 / 大图被完整下载”，所以用行为测试钉住。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from engine.attachments import sniff_image_mime  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_GIF = b"GIF89a" + b"\x00" * 64
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
_BMP = b"BM" + b"\x00" * 64
_WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 64
_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 64


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _make_fake_httpx(chunks: list[bytes], headers: dict[str, str], *, fail_on_iter: bool = False):
    tracker = {"yielded": 0, "get_calls": 0, "iterated": False}

    class _Resp:
        def __init__(self) -> None:
            self.headers = dict(headers)

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            tracker["iterated"] = True
            if fail_on_iter:
                raise AssertionError("body must not be read once Content-Length rejects")
            for chunk in chunks:
                tracker["yielded"] += len(chunk)
                yield chunk

        @property
        def content(self) -> bytes:
            return b"".join(chunks)

    class _StreamCtx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method: str, url: str):
            return _StreamCtx()

        async def get(self, url: str):
            tracker["get_calls"] += 1
            return _Resp()

    return types.SimpleNamespace(AsyncClient=_Client, tracker=tracker)


def _files(workspace: Path, conversation_key: str) -> list[Path]:
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    if not att_dir.exists():
        return []
    return [p for p in att_dir.rglob("*") if p.is_file()]


def _patch_no_get_msg(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(bot: Any, reply_message_id: Any):
        return None

    monkeypatch.setattr(runtime, "_get_reply_message", _none)


class TestReplyPayloadFallback:
    def test_event_reply_raw_message_survives_get_msg_failure(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_no_get_msg(runtime, monkeypatch)
        bot = types.SimpleNamespace(self_id="90000")
        reply_obj = types.SimpleNamespace(
            message=None, raw_message="被引用的原话", sender=None, user_id=None, time=1700000000
        )
        event = types.SimpleNamespace(reply=reply_obj, raw_message="[CQ:reply,id=777]")

        text, _atts = asyncio.run(runtime._build_reply_context(event, bot, "qq_group:1"))

        assert "被引用的原话" in text
        assert "无法获取引用消息内容" not in text

    def test_cached_reply_renders_when_event_reply_absent(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_no_get_msg(runtime, monkeypatch)
        runtime._reply_message_cache["555"] = {
            "message": None,
            "raw_message": "缓存里的原话",
            "sender": {"user_id": 12345, "nickname": "甲", "card": "甲"},
            "user_id": 12345,
            "time": 1700000000,
        }
        bot = types.SimpleNamespace(self_id="90000")
        event = types.SimpleNamespace(reply=None, raw_message="[CQ:reply,id=555]")

        text, _atts = asyncio.run(runtime._build_reply_context(event, bot, "qq_group:1"))

        assert "缓存里的原话" in text
        assert text.startswith("[")
        assert "):" in text
        assert "无法获取引用消息内容" not in text

    def test_forward_nodes_use_event_reply_when_get_msg_fails(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_no_get_msg(runtime, monkeypatch)
        bot = types.SimpleNamespace(self_id="90000")
        reply_obj = types.SimpleNamespace(
            message=None, raw_message="转发的原话", sender=None, user_id=None, time=1700000000
        )
        event = types.SimpleNamespace(reply=reply_obj, raw_message="[CQ:reply,id=888]")

        nodes = asyncio.run(runtime._forward_nodes_from_reply(bot, event, "qq_group:1"))

        assert nodes
        assert any("转发的原话" in str(n) for n in nodes)

    def test_forward_nodes_read_a_card_that_only_carries_inline_content(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 内嵌形态没有 res_id，只认 id 的话这张卡片会整个丢掉。
        _patch_no_get_msg(runtime, monkeypatch)
        bot = types.SimpleNamespace(self_id="90000")
        inner = [{
            "sender": {"user_id": 12345, "nickname": "甲"},
            "time": 1700000000,
            "content": [{"type": "text", "data": {"text": "内嵌里的原话"}}],
        }]
        reply_obj = types.SimpleNamespace(
            message=[{"type": "forward", "data": {"content": inner}}],
            raw_message="", sender=None, user_id=None, time=1700000000,
        )
        event = types.SimpleNamespace(reply=reply_obj, raw_message="[CQ:reply,id=888]")

        nodes = asyncio.run(runtime._forward_nodes_from_reply(bot, event, "qq_group:1"))

        assert any("内嵌里的原话" in str(n) for n in nodes)


class TestImageHeaderSniffing:
    def test_pure_sniffer_recognizes_supported_formats(self) -> None:
        assert sniff_image_mime(_PNG) == "image/png"
        assert sniff_image_mime(_JPEG) == "image/jpeg"
        assert sniff_image_mime(_GIF) == "image/gif"
        assert sniff_image_mime(_WEBP) == "image/webp"
        assert sniff_image_mime(_BMP) == "image/bmp"

    def test_pure_sniffer_rejects_non_images(self) -> None:
        assert sniff_image_mime(_PDF) == ""
        assert sniff_image_mime(_WAV) == ""
        assert sniff_image_mime(b"") == ""

    def test_disguised_pdf_is_refused_and_not_saved(
        self, runtime, tmp_path: Path
    ) -> None:
        incoming = tmp_path / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fake = incoming / "fake.jpg"
        fake.write_bytes(_PDF)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource(f"file://{fake.as_posix()}")], "qq_group_x")
        )

        assert result == []
        assert any("不是我能识别的图片格式" in e for e in errors)
        assert _files(tmp_path, "qq_group_x") == []

    def test_extension_follows_magic_not_url_suffix(
        self, runtime, tmp_path: Path
    ) -> None:
        incoming = tmp_path / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        # 后缀说是 png，字节是 gif：后缀必须由魔数决定。
        source_file = incoming / "真.png"
        source_file.write_bytes(_GIF)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource(f"file://{source_file.as_posix()}")], "qq_group_y")
        )

        assert errors == []
        assert len(result) == 1
        assert result[0]["mime_type"] == "image/gif"
        saved = _files(tmp_path, "qq_group_y")
        assert len(saved) == 1
        assert saved[0].suffix == ".gif"


class TestDownloadSizeLimits:
    def test_oversized_image_stops_streaming_and_never_calls_get(
        self, runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        set_live_config(runtime, image_max_bytes=4096)
        fake = _make_fake_httpx([b"\x00" * 1024] * 1024, {})
        monkeypatch.setattr(runtime, "httpx", fake)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource("https://cdn.example/big.png")], "qq_group:1")
        )

        assert result == []
        assert any("图片太大" in e for e in errors)
        assert fake.tracker["yielded"] <= 4096 + 1024
        assert fake.tracker["get_calls"] == 0
        assert _files(tmp_path, "qq_group:1") == []

    def test_content_length_precheck_rejects_without_reading_body(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_live_config(runtime, image_max_bytes=4096)
        fake = _make_fake_httpx([], {"content-length": "99999999"}, fail_on_iter=True)
        monkeypatch.setattr(runtime, "httpx", fake)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource("https://cdn.example/huge.png")], "qq_group:1")
        )

        assert result == []
        assert any("图片太大" in e for e in errors)
        assert fake.tracker["iterated"] is False

    def test_small_image_is_saved(
        self, runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = _make_fake_httpx([_PNG], {"content-type": "image/png"})
        monkeypatch.setattr(runtime, "httpx", fake)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource("https://cdn.example/small.png")], "qq_group:2")
        )

        assert errors == []
        assert len(result) == 1
        assert result[0]["mime_type"] == "image/png"
        assert len(_files(tmp_path, "qq_group:2")) == 1

    def test_oversized_file_reports_too_large(
        self, runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_live_config(runtime, file_max_bytes=4096)
        fake = _make_fake_httpx([b"\x00" * 1024] * 64, {})
        monkeypatch.setattr(runtime, "httpx", fake)

        result, errors = asyncio.run(
            runtime._file_sources_to_attachments(
                [{"source": "https://cdn.example/big.zip", "name": "big.zip", "size": 0}],
                "qq_group:3",
            )
        )

        assert result == []
        assert any("文件太大" in e for e in errors)


class TestAttachmentCleanup:
    def _write_old(self, path: Path, age_days: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG)
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))

    def test_cleanup_only_touches_qq_dirs(self, runtime, tmp_path: Path) -> None:
        att = tmp_path / "data" / "attachments"
        qq_file = att / "qq_group_abc" / "old.png"
        engine_file = att / "engine_session_1" / "old.png"
        self._write_old(qq_file, 2)
        self._write_old(engine_file, 2)

        runtime._cleanup_old_qq_attachments(now=time.time())

        assert not qq_file.exists()
        assert engine_file.exists()

    def test_reference_renews_mtime(self, runtime, tmp_path: Path) -> None:
        f = tmp_path / "data" / "attachments" / "qq_group_1" / "img.png"
        self._write_old(f, 2)
        rel = f.relative_to(tmp_path).as_posix()

        runtime._remember_reply_attachments("m1", "qq_group:1", "1", [{"type": "image", "path": rel}])

        cutoff = time.time() - runtime.IMAGE_CACHE_TTL_SECONDS
        assert f.stat().st_mtime > cutoff

    def test_periodic_cleanup_runs_without_downloads(
        self, runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        set_live_config(runtime, enable_image_input=False)
        f = tmp_path / "data" / "attachments" / "qq_group_z" / "old.png"
        self._write_old(f, 2)

        async def _cancel(*args: Any, **kwargs: Any) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(runtime.asyncio, "sleep", _cancel)

        async def _run_once() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await runtime._attachment_cleanup_forever()

        asyncio.run(_run_once())

        assert not f.exists()

    def test_dead_path_not_returned(self, runtime) -> None:
        runtime._reply_attachment_cache["m9"] = {
            "conversation_key": "qq_group:1",
            "sender_id": "1",
            "created_at": time.time(),
            "attachments": [{"type": "image", "path": "data/attachments/qq_group_1/missing.png"}],
        }

        assert runtime._cached_reply_image_attachments("m9", "qq_group:1") == []


class TestInboundFilePolicy:
    def test_pure_policy_rules(self, runtime) -> None:
        reject = runtime.inbound_file_reject_reason
        assert reject("a.exe") == "unsupported_type"
        assert reject("a.pdf", b"MZ\x90\x00") == "executable_content"
        assert reject("a.pdf", b"%PDF-1.7") == ""
        assert reject("a.ipynb") == "unsupported_type"
        assert reject("a.ipynb", extra_allowed=("ipynb",)) == ""
        assert reject("a.exe", extra_allowed=("exe",)) == "unsupported_type"
        assert reject("noext") == "unsupported_type"
        assert reject("a.doc", b"\xd0\xcf\x11\xe0") == ""

    def test_blocked_extension_refused_before_download(self, runtime, tmp_path: Path) -> None:
        incoming = tmp_path / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        bad = incoming / "x.exe"
        bad.write_bytes(b"MZ" + b"\x00" * 64)

        result, errors = asyncio.run(
            runtime._file_sources_to_attachments(
                [{"source": f"file://{bad.as_posix()}", "name": "x.exe", "size": 0}],
                "qq_group:e",
            )
        )

        assert result == []
        assert any("这个文件类型我不接收" in e for e in errors)
        assert _files(tmp_path, "qq_group:e") == []

    def test_executable_disguised_as_pdf_refused(self, runtime, tmp_path: Path) -> None:
        incoming = tmp_path / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        bad = incoming / "report.pdf"
        bad.write_bytes(b"MZ\x90\x00" + b"\x00" * 64)

        result, errors = asyncio.run(
            runtime._file_sources_to_attachments(
                [{"source": f"file://{bad.as_posix()}", "name": "report.pdf", "size": 0}],
                "qq_group:d",
            )
        )

        assert result == []
        assert any("可执行程序" in e for e in errors)
        assert _files(tmp_path, "qq_group:d") == []

    def test_workspace_internal_file_is_accepted(self, runtime, tmp_path: Path) -> None:
        incoming = tmp_path / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        note = incoming / "note.txt"
        note.write_text("hello", encoding="utf-8")

        result, errors = asyncio.run(
            runtime._file_sources_to_attachments(
                [{"source": f"file://{note.as_posix()}", "name": "note.txt", "size": 0}],
                "qq_group:ok",
            )
        )

        assert errors == []
        assert len(result) == 1
        assert len(_files(tmp_path, "qq_group:ok")) == 1

    def test_workspace_external_path_is_denied(
        self, runtime, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        external = tmp_path_factory.mktemp("outside")
        secret = external / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")

        result, errors = asyncio.run(
            runtime._file_sources_to_attachments(
                [{"source": f"file://{secret.as_posix()}", "name": "secret.txt", "size": 0}],
                "qq_group:x",
            )
        )

        assert result == []
        assert any("工作区之外" in e for e in errors)
        assert _files(tmp_path, "qq_group:x") == []

    def test_image_external_path_is_denied(
        self, runtime, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        external = tmp_path_factory.mktemp("outside_img")
        real = external / "real.png"
        real.write_bytes(_PNG)

        result, errors = asyncio.run(
            runtime._image_sources_to_attachments([runtime.ImageSource(f"file://{real.as_posix()}")], "qq_group:xi")
        )

        assert result == []
        assert any("工作区之外" in e for e in errors)
        assert _files(tmp_path, "qq_group:xi") == []

    def test_local_source_roots_opens_external_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        external = tmp_path_factory.mktemp("allowed_ext")
        note = external / "note.txt"
        note.write_text("ok", encoding="utf-8")
        monkeypatch.setenv("ONEBOT_LOCAL_SOURCE_ROOTS", str(external))
        workspace = tmp_path_factory.mktemp("ws")
        rt = load_runtime(monkeypatch, workspace)

        result, errors = asyncio.run(
            rt._file_sources_to_attachments(
                [{"source": f"file://{note.as_posix()}", "name": "note.txt", "size": 0}],
                "qq_group:root",
            )
        )

        assert errors == []
        assert len(result) == 1
