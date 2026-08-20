"""群文件要能被读到：QQ 的文件消息带不了 @，只能靠引用或接住最近那一批。"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_spec = importlib.util.spec_from_file_location(
    "_test_attachment_policy", _ROOT / "adapters" / "onebot" / "attachment_policy.py",
)
assert _spec and _spec.loader
policy = importlib.util.module_from_spec(_spec)
sys.modules["_test_attachment_policy"] = policy
_spec.loader.exec_module(policy)

from engine.attachments import _decode_text_file, attachments_to_content_parts  # noqa: E402


@dataclass
class Entry:
    attachment: dict
    created_at: float
    sender_id: str
    message_id: str


def _att(name: str, kind: str = "file") -> dict:
    return {"type": kind, "path": f"data/attachments/qq_group_x/{name}", "name": name}


class TestFileQueryDetection:
    @pytest.mark.parametrize("text", [
        "读一下上面的文件", "这个 json 是什么", "刚发的文件看一下",
        "帮我看看这份文档", "解析一下附件", "read the file please",
    ])
    def test_file_questions_are_recognised(self, text: str) -> None:
        assert policy.looks_like_file_query(text)

    @pytest.mark.parametrize("text", [
        "今天天气不错", "帮我润色这段文字", "画一只猫", "",
    ])
    def test_ordinary_chat_is_not_a_file_question(self, text: str) -> None:
        assert not policy.looks_like_file_query(text)

    def test_a_vague_pointer_is_not_enough(self) -> None:
        # 「看看这个」可能指任何东西，凭它就把 50MB 文件塞进去代价太大。
        assert not policy.looks_like_file_query("看看这个")


class TestRecentSelection:
    def test_only_the_current_sender_is_eligible(self) -> None:
        entries = [Entry(_att("a.json"), 100.0, "userA", "m1")]
        assert policy.select_recent_attachment_entries(
            entries, sender_id="userB", now=101.0, max_age_seconds=180.0, max_items=3,
        ) == []

    def test_stale_batches_are_dropped(self) -> None:
        entries = [Entry(_att("a.json"), 100.0, "userA", "m1")]
        assert policy.select_recent_attachment_entries(
            entries, sender_id="userA", now=100000.0, max_age_seconds=180.0, max_items=3,
        ) == []

    def test_never_combines_two_messages(self) -> None:
        # 跨消息合并会让「读一下那个文件」顺带捎上更早的无关文件。
        entries = [
            Entry(_att("old.json"), 100.0, "userA", "m1"),
            Entry(_att("new.json"), 101.0, "userA", "m2"),
        ]
        picked = policy.select_recent_attachment_entries(
            entries, sender_id="userA", now=102.0, max_age_seconds=180.0, max_items=3,
        )
        assert [p["name"] for p in picked] == ["new.json"]

    def test_max_items_caps_one_batch(self) -> None:
        entries = [Entry(_att(f"f{i}.json"), 100.0, "userA", "m1") for i in range(5)]
        picked = policy.select_recent_attachment_entries(
            entries, sender_id="userA", now=101.0, max_age_seconds=180.0, max_items=2,
        )
        assert len(picked) == 2


class TestFallbackGate:
    def test_a_reply_never_falls_back(self) -> None:
        # 引用有自己的消息链，回退会让它拿到无关的东西。
        assert not policy.should_fallback_to_recent_attachments(
            has_attachments=False, input_enabled=True,
            looks_like_query=True, reply_message_id="123",
        )

    def test_existing_attachments_win(self) -> None:
        assert not policy.should_fallback_to_recent_attachments(
            has_attachments=True, input_enabled=True,
            looks_like_query=True, reply_message_id=None,
        )

    def test_disabled_input_blocks_fallback(self) -> None:
        assert not policy.should_fallback_to_recent_attachments(
            has_attachments=False, input_enabled=False,
            looks_like_query=True, reply_message_id=None,
        )

    def test_the_normal_case_passes(self) -> None:
        assert policy.should_fallback_to_recent_attachments(
            has_attachments=False, input_enabled=True,
            looks_like_query=True, reply_message_id=None,
        )


class TestEngineDecoding:
    def test_utf8_text_decodes(self) -> None:
        assert _decode_text_file("你好 hi".encode("utf-8")) == "你好 hi"

    def test_nul_marks_binary(self) -> None:
        assert _decode_text_file(bytes([0x25, 0x50, 0x44, 0x46, 0x00, 0x01])) is None

    def test_invalid_utf8_marks_binary(self) -> None:
        assert _decode_text_file(bytes([0xFF, 0xFE, 0xFD])) is None

    def test_empty_file_is_text_not_binary(self) -> None:
        assert _decode_text_file(b"") == ""


class TestEngineInjection:
    def test_a_text_file_is_injected_inline(self, tmp_path: Path) -> None:
        rel = "data/attachments/conv/a.json"
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text('{"k": 1}', encoding="utf-8")

        parts = attachments_to_content_parts(
            [{"type": "file", "path": rel, "name": "a.json"}], workspace_root=tmp_path,
        )
        assert len(parts) == 1
        assert '{"k": 1}' in parts[0]["text"]

    def test_a_binary_file_is_not_injected_as_garbled_text(self, tmp_path: Path) -> None:
        # 入站白名单放行 pdf/docx/zip，强解会把整块替换字符灌进 prompt。
        rel = "data/attachments/conv/a.pdf"
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_bytes(bytes([0x25, 0x50, 0x44, 0x46, 0x00]) + b"\xff\xfe binary")

        parts = attachments_to_content_parts(
            [{"type": "file", "path": rel, "name": "a.pdf"}], workspace_root=tmp_path,
        )
        assert len(parts) == 1
        text = parts[0]["text"]
        assert "Binary file" in text
        assert "read_file" in text
        assert "�" not in text

    def test_an_oversized_file_degrades_to_a_path(self, tmp_path: Path) -> None:
        rel = "data/attachments/conv/big.json"
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text("x" * 200_000, encoding="utf-8")

        parts = attachments_to_content_parts(
            [{"type": "file", "path": rel, "name": "big.json"}], workspace_root=tmp_path,
        )
        assert "too large" in parts[0]["text"].lower()
        assert rel in parts[0]["text"]
