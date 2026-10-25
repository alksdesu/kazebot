"""本机来源只有一份解析：入站图片和文件、出站附件、/发文件 都走 adapters.onebot.local_source。

file:// 按标准 URL 解码，入站读取限定根目录并封顶，下载失败按实际原因提示。
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from adapters.onebot import local_source  # noqa: E402

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestSourcePath:
    def test_a_standard_file_url_is_percent_decoded(self, tmp_path: Path) -> None:
        target = tmp_path / "空 格#1.png"
        assert local_source.source_path(target.as_uri()) == target

    def test_localhost_is_the_same_machine(self) -> None:
        assert local_source.source_path("file://localhost/srv/a.png") == Path("/srv/a.png")

    @pytest.mark.parametrize("raw", ["file://C:/data/a.png", "file://C:\\data\\a.png"])
    def test_a_drive_letter_right_after_the_scheme_is_kept(self, raw: str) -> None:
        assert local_source.source_path(raw) == Path(raw[len("file://"):])

    def test_the_engine_style_relative_url_stays_relative(self) -> None:
        # 引擎记附件就是 file://data/attachments/...，那不是主机名。
        path = local_source.source_path("file://data/attachments/qq_group_1/x.png")

        assert path == Path("data/attachments/qq_group_1/x.png")
        assert local_source.workspace_path("file://data/attachments/qq_group_1/x.png", "/ws") == Path("/ws/data/attachments/qq_group_1/x.png")

    def test_plain_paths_pass_through(self) -> None:
        assert local_source.source_path("/abs/a.png") == Path("/abs/a.png")
        assert local_source.workspace_path("rel/a.png", "/ws") == Path("/ws/rel/a.png")

    @pytest.mark.parametrize("source,local", [
        ("file:///a", True), ("/a/b", True), ("C:\\a", True), ("D:/a", True),
        ("https://x/a.png", False), ("base64://AAAA", False), ("a.png", False),
    ])
    def test_local_sources_are_recognised(self, source: str, local: bool) -> None:
        assert local_source.is_local_source(source) is local


class TestInboundRead:
    def test_inside_the_roots_is_read(self, tmp_path: Path) -> None:
        image = tmp_path / "in.png"
        image.write_bytes(_PNG)

        assert local_source.read_inbound_source(image.as_uri(), roots=[tmp_path], max_bytes=1024) == _PNG

    def test_outside_the_roots_is_denied(self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
        secret = tmp_path_factory.mktemp("outside") / "secret.txt"
        secret.write_bytes(b"secret")

        with pytest.raises(local_source.LocalSourceError) as caught:
            local_source.read_inbound_source(secret.as_uri(), roots=[tmp_path], max_bytes=1024)

        assert str(caught.value) == local_source.LOCAL_DENIED

    def test_a_missing_file_is_unavailable_not_denied(self, tmp_path: Path) -> None:
        with pytest.raises(local_source.LocalSourceError) as caught:
            local_source.read_inbound_source((tmp_path / "gone.png").as_uri(), roots=[tmp_path], max_bytes=1024)

        assert str(caught.value) == local_source.LOCAL_UNAVAILABLE

    @pytest.mark.parametrize("raw", ["file://evil-host/share/a.png", "file://data/attachments/x.png", "relative/a.png"])
    def test_hosts_and_relative_paths_are_never_read_inbound(self, tmp_path: Path, raw: str) -> None:
        with pytest.raises(local_source.LocalSourceError) as caught:
            local_source.resolve_inbound_source(raw, roots=[tmp_path])

        assert str(caught.value) == local_source.LOCAL_DENIED

    def test_the_read_is_capped(self, tmp_path: Path) -> None:
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 2048)

        with pytest.raises(local_source.LocalSourceError) as caught:
            local_source.read_inbound_source(str(big), roots=[tmp_path], max_bytes=1024)

        assert str(caught.value) == local_source.TOO_LARGE

    def test_a_symlink_out_of_the_roots_is_denied(self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
        secret = tmp_path_factory.mktemp("outside-link") / "secret.txt"
        secret.write_bytes(b"secret")
        link = tmp_path / "link.txt"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")

        with pytest.raises(local_source.LocalSourceError) as caught:
            local_source.read_inbound_source(str(link), roots=[tmp_path], max_bytes=1024)

        assert str(caught.value) == local_source.LOCAL_DENIED


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ONEBOT_LOCAL_SOURCE_ROOTS", raising=False)
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, enable_image_input=True, image_max_bytes=4096)
    original_client = httpx.AsyncClient
    module.download_handler = lambda request: httpx.Response(404)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(lambda request: module.download_handler(request)), **kwargs)

    monkeypatch.setattr(module.media.httpx, "AsyncClient", client)
    return module


def _images(runtime: Any, *sources: str) -> tuple[list[dict], list[str]]:
    return asyncio.run(runtime.media._image_sources_to_attachments(
        [runtime.models.ImageSource(source) for source in sources], "qq_group:local",
    ))


def _saved(tmp_path: Path) -> list[Path]:
    root = tmp_path / "data" / "attachments" / "qq_group_local"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


class TestInboundImages:
    def test_a_percent_encoded_workspace_image_is_read(self, runtime: Any, tmp_path: Path) -> None:
        image = tmp_path / "data" / "incoming" / "带 空格.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(_PNG)

        attachments, errors = _images(runtime, image.as_uri())

        assert errors == [] and len(attachments) == 1

    def test_a_missing_local_image_says_the_path_is_unreachable(self, runtime: Any, tmp_path: Path) -> None:
        attachments, errors = _images(runtime, (tmp_path / "nope.png").as_uri())

        assert attachments == []
        assert "访问不到" in errors[0] and "过期" not in errors[0]

    def test_a_large_local_image_is_refused_without_reading_it_whole(self, runtime: Any, tmp_path: Path) -> None:
        image = tmp_path / "data" / "big.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(_PNG + b"\0" * 8192)

        attachments, errors = _images(runtime, str(image))

        assert attachments == [] and "图片太大" in errors[0]
        assert _saved(tmp_path) == []

    @pytest.mark.parametrize("status,fragment", [(404, "可能是 QQ 临时链接已过期"), (500, "HTTP 500")])
    def test_http_failures_report_what_happened(self, runtime: Any, status: int, fragment: str) -> None:
        runtime.download_handler = lambda request: httpx.Response(status)

        attachments, errors = _images(runtime, "https://multimedia.nt.qq.com.cn/download?rkey=x")

        assert attachments == [] and fragment in errors[0]

    def test_a_timeout_is_not_called_an_expired_link(self, runtime: Any) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        runtime.download_handler = slow

        attachments, errors = _images(runtime, "https://multimedia.nt.qq.com.cn/download?rkey=x")

        assert "超时" in errors[0] and "过期" not in errors[0]

    def test_base64_images_are_accepted(self, runtime: Any) -> None:
        attachments, errors = _images(runtime, "base64://" + base64.b64encode(_PNG).decode("ascii"))

        assert errors == [] and attachments[0]["mime_type"] == "image/png"

    def test_a_failed_write_leaves_no_half_file(self, runtime: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        image = tmp_path / "data" / "ok.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(_PNG)
        original = Path.write_bytes

        def half_write(path: Path, data: bytes) -> int:
            if "qq_group_local" in str(path):
                original(path, data[:3])
                raise OSError("disk full")
            return original(path, data)

        monkeypatch.setattr(Path, "write_bytes", half_write)

        attachments, errors = _images(runtime, str(image))

        assert attachments == [] and "保存时失败" in errors[0]
        assert _saved(tmp_path) == []


class TestOutboundPaths:
    @pytest.mark.parametrize("raw", ["file://data/out/a.png", "data/out/a.png"])
    def test_relative_attachments_resolve_inside_the_workspace(self, runtime: Any, tmp_path: Path, raw: str) -> None:
        resolved = runtime.outbound_attachments._resolve_attachment_path({"path": raw})

        assert resolved == Path(runtime.outbound_attachments.CLONOTH_WORKSPACE) / "data" / "out" / "a.png"

    def test_an_absolute_file_url_is_decoded(self, runtime: Any, tmp_path: Path) -> None:
        target = tmp_path / "out dir" / "图.png"

        assert runtime.outbound_attachments._resolve_attachment_path(target.as_uri()) == target
        assert runtime.outbound_attachments._attachment_filename(target.as_uri()) == "图.png"

    def test_the_file_command_accepts_file_urls(self, runtime: Any, tmp_path: Path) -> None:
        report = tmp_path / "data" / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# r", encoding="utf-8")

        found = runtime.proactive._workspace_file(report.as_uri())

        assert found is not None and found[1] == "data/report.md"
