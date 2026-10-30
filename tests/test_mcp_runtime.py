"""Tests for MCP runtime result post-processing.

Why: MCP image content can contain multi-megabyte base64 payloads that would bloat
conversation history if returned inline. How: these tests patch the runtime
session boundary and exercise call_tool end to end. Purpose: keep the wrapper
contract executable before changing the implementation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

# Why: the repository is not installed as a package in the lightweight test
# runner. How: add the repository root explicitly. Purpose: keep this test bound
# to the checked-out source tree rather than any globally installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import toolbox.mcp_runtime as mcp_runtime  # noqa: E402


def test_call_tool_spills_large_mcp_image_content_to_attachment_path(tmp_path: Path, monkeypatch) -> None:
    """Large MCP image parts should be saved and replaced with path text."""
    # Why: the bug was caused by large base64 PNG data entering context. How:
    # create enough bytes for the encoded payload to exceed the runtime threshold.
    # Purpose: prove call_tool returns a small text marker plus an attachment.
    image_bytes = b"\x89PNG\r\n\x1a\n" + (b"image-bytes" * 800)
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    small_image = base64.b64encode(b"small-icon").decode("ascii")
    expected_name = f"{hashlib.md5(image_bytes).hexdigest()[:12]}.png"
    expected_rel_path = f"data/attachments/mcp/grok-bailai/{expected_name}"

    fake_result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="before"),
            SimpleNamespace(type="image", data=encoded_image, mimeType="image/png"),
            SimpleNamespace(type="image", data=small_image, mimeType="image/png"),
        ],
        isError=False,
    )

    class FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict) -> SimpleNamespace:
            # Why: this test targets result conversion, not MCP transport. How:
            # assert the call boundary and return a deterministic SDK-like object.
            # Purpose: catch regressions without starting an external MCP server.
            assert tool_name == "generate_image"
            assert arguments == {"prompt": "cat"}
            return fake_result

    # call_tool goes through the session pool, which reads the client config and connects via _connect.
    mcp_runtime.upsert_client(tmp_path, {"id": "grok-bailai", "transport": "stdio", "command": "fake-mcp-server"})

    @asynccontextmanager
    async def fake_connect(plan):
        assert plan.key == mcp_runtime._pool_key(tmp_path, "grok-bailai")
        assert plan.client_id == "grok-bailai"
        yield mcp_runtime._Connection(session=FakeSession())

    monkeypatch.setattr(mcp_runtime, "_connect", fake_connect)

    result = asyncio.run(mcp_runtime.call_tool(
        tmp_path,
        "grok-bailai",
        "generate_image",
        {"prompt": "cat"},
    ))

    assert (tmp_path / expected_rel_path).read_bytes() == image_bytes
    # [AutoC 2026-05-31] Why: MCP call_tool now follows the unified ok/data/error
    # schema but keeps top-level attachments during migration. How: assert both the
    # nested data.attachments contract and the temporary top-level mirror. Purpose:
    # preserve attachment delivery while result readers move to data.result.
    expected_attachments = [{"path": expected_rel_path, "name": expected_name, "type": "image"}]
    assert result["attachments"] == expected_attachments
    assert result["data"]["attachments"] == expected_attachments
    assert result["data"]["result"] == f"before\n[Image saved to disk: {expected_rel_path} ({len(image_bytes)} bytes)]\n[image: image/png]"
    assert result["data"]["mcp_result"]["content"][1] == {
        "type": "text",
        "text": f"[Image saved to disk: {expected_rel_path} ({len(image_bytes)} bytes)]",
    }
    assert result["data"]["mcp_result"]["content"][2] == {
        "type": "image",
        "data": small_image,
        "mimeType": "image/png",
    }


def test_call_tool_saves_large_images_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """Decoding and writing a multi-megabyte image must not stall the engine's event loop."""
    image_bytes = b"\x89PNG\r\n\x1a\n" + (b"image-bytes" * 800)
    expected_rel_path = f"data/attachments/mcp/painter/{hashlib.md5(image_bytes).hexdigest()[:12]}.png"
    fake_result = SimpleNamespace(
        content=[SimpleNamespace(type="image", data=base64.b64encode(image_bytes).decode("ascii"), mimeType="image/png")],
        isError=False,
    )

    class FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict) -> SimpleNamespace:
            return fake_result

    mcp_runtime.upsert_client(tmp_path, {"id": "painter", "transport": "stdio", "command": "fake-mcp-server"})

    @asynccontextmanager
    async def fake_connect(plan):
        yield mcp_runtime._Connection(session=FakeSession())

    loop_thread = threading.get_ident()
    saved_on: list[int] = []
    real_save = mcp_runtime._save_large_mcp_image_part

    def recorded_save(*args, **kwargs):
        saved_on.append(threading.get_ident())
        return real_save(*args, **kwargs)

    monkeypatch.setattr(mcp_runtime, "_connect", fake_connect)
    monkeypatch.setattr(mcp_runtime, "_save_large_mcp_image_part", recorded_save)

    result = asyncio.run(mcp_runtime.call_tool(tmp_path, "painter", "draw", {}))

    assert saved_on and loop_thread not in saved_on
    assert (tmp_path / expected_rel_path).read_bytes() == image_bytes
    assert result["data"]["attachments"] == [
        {"path": expected_rel_path, "name": expected_rel_path.rsplit("/", 1)[-1], "type": "image"},
    ]
