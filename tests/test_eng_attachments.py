"""附件只有一道门：文件和图片都按解析到底的真实位置判，工具产出的附件进上下文、外发之前也要过这道门。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.attachments import (
    _resolve_file_url,
    attachments_to_content_parts,
    contains_image_blocks,
    resolve_attachment_file,
    screen_attachments,
    strip_image_blocks,
)
from engine.builtin.attachment import AttachmentCollector
from engine.inference.ai_step import _async_attachment_paths
from engine.inference.resume_builder import _select_attachments


def _file(root: Path, relative: str, data: bytes = b"x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _link_or_skip(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_only_real_files_under_data_get_through(tmp_path: Path) -> None:
    _file(tmp_path, "data/a.png")
    _file(tmp_path, "config/secrets.env")

    assert resolve_attachment_file(tmp_path, "data/a.png") == (tmp_path / "data" / "a.png").resolve()
    assert resolve_attachment_file(tmp_path, "data/../config/secrets.env") is None
    assert resolve_attachment_file(tmp_path, "config/secrets.env") is None
    assert resolve_attachment_file(tmp_path, "data/missing.png") is None
    assert resolve_attachment_file(tmp_path, "data") is None


def test_a_link_under_data_pointing_outside_is_refused_for_files_and_images(tmp_path: Path) -> None:
    secret = _file(tmp_path, "secrets/private.key", b"-----BEGIN KEY-----")
    _link_or_skip(secret, tmp_path / "data" / "innocent.png")

    assert resolve_attachment_file(tmp_path, "data/innocent.png") is None
    assert _resolve_file_url("file://data/innocent.png", tmp_path) is None
    parts = attachments_to_content_parts([{"type": "file", "path": "data/innocent.png"}], workspace_root=tmp_path)
    assert all("BEGIN KEY" not in str(part) for part in parts)
    assert "not found or not readable" in parts[0]["text"]


def test_a_traversal_path_produces_no_part_at_all(tmp_path: Path) -> None:
    _file(tmp_path, "config/secret.txt", b"sk-secret")
    assert attachments_to_content_parts(
        [{"type": "file", "path": "data/../config/secret.txt"}], workspace_root=tmp_path,
    ) == []


def test_screening_drops_paths_outside_data_and_missing_files(tmp_path: Path) -> None:
    _file(tmp_path, "data/ok.png")
    kept = screen_attachments(tmp_path, [
        {"path": "data/ok.png", "type": "image"},
        {"path": "data/gone.png", "type": "image"},
        {"path": "../outside.png", "type": "image"},
        "data/ok.png",
    ])
    assert kept == [{"path": "data/ok.png", "type": "image"}]


def test_tool_attachments_are_screened_before_they_are_collected(tmp_path: Path) -> None:
    _file(tmp_path, "data/out.png")
    _file(tmp_path, "config/secrets.env")
    state = SimpleNamespace(collected_attachments=[], tool_produced_attachments=[])
    batch: list = []
    ctx = SimpleNamespace(rctx=SimpleNamespace(workspace_root=tmp_path), extra={
        "tool_result": {"ok": True, "data": {"attachments": [
            {"path": "data/out.png", "type": "image"},
            {"path": "config/secrets.env", "type": "file"},
        ]}},
        "tool_attachments": batch,
        "loop_state": state,
    })

    assert asyncio.run(AttachmentCollector().handle(ctx)) is not None

    expected = [{"path": "data/out.png", "type": "image"}]
    assert batch == expected
    assert state.collected_attachments == expected
    assert state.tool_produced_attachments == expected


def test_a_tool_result_with_only_refused_attachments_collects_nothing(tmp_path: Path) -> None:
    state = SimpleNamespace(collected_attachments=[], tool_produced_attachments=[])
    ctx = SimpleNamespace(rctx=SimpleNamespace(workspace_root=tmp_path), extra={
        "tool_result": {"ok": True, "attachments": [{"path": "data/missing.png", "type": "image"}]},
        "tool_attachments": [],
        "loop_state": state,
    })

    assert asyncio.run(AttachmentCollector().handle(ctx)) is None
    assert state.tool_produced_attachments == []


def test_async_tool_results_only_forward_screened_paths(tmp_path: Path) -> None:
    _file(tmp_path, "data/out.png")
    result = {"ok": True, "data": {"attachments": [
        {"path": "data/out.png"}, "data/../config/x", {"path": "data/none.png"},
    ]}}
    assert _async_attachment_paths(result, tmp_path) == ["data/out.png"]


@pytest.mark.parametrize("selected", [None, [], "data/a.png", ["data/missing.png"], [""]])
def test_naming_nothing_or_nothing_found_selects_nothing(tmp_path: Path, selected: object) -> None:
    collected = [{"path": "data/a.png", "type": "image"}]
    assert _select_attachments(collected, selected, workspace_root=tmp_path, session_id="session") == []


def test_named_attachments_are_selected(tmp_path: Path) -> None:
    collected = [{"path": "data/a.png"}, {"path": "data/b.png"}]
    assert _select_attachments(collected, ["data/b.png"]) == [{"path": "data/b.png"}]


def test_image_blocks_are_detected_and_stripped_in_one_place() -> None:
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
        {"role": "assistant", "content": "plain"},
    ]

    stripped = strip_image_blocks(messages)

    assert contains_image_blocks(messages) and not contains_image_blocks(stripped)
    assert stripped[0]["content"] == "look"
    assert stripped[1]["content"] == "[image content removed]"
    assert stripped[2] is messages[2]
    assert messages[0]["content"][1]["type"] == "image_url"
