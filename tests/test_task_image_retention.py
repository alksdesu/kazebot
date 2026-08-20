"""Regression tests for task-scoped multimodal history retention.

[AutoC 2026-06-01] Why: image attachments used to be stripped from every
ConversationStore reload, which made a later step in the same task unable to see
the original image. How: these tests assert that only cross-task history is
reduced to text and that multimodal list content survives JSONL and shadow
writes. Purpose: protect the task-local image locking behavior requested for the
runner.
"""
from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.inference.ai_step import _shadow_write  # noqa: E402
from engine.runner import _message_to_history_dict, _strip_images_from_content  # noqa: E402
from engine.builtin.finish_guard import (  # noqa: E402
    FinishGuardHandler,
    _has_unresolved_vision_failure,
)
from engine.inference.ai_step import (  # noqa: E402
    _async_tool_failure,
    _build_async_result_payload,
    _run_async_deliver,
)
from tools.read_image import SPEC as READ_IMAGE_SPEC  # noqa: E402

_POLICY_PATH = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "attachment_policy.py"
_POLICY_SPEC = importlib.util.spec_from_file_location("clonoth_onebot_attachment_policy", _POLICY_PATH)
assert _POLICY_SPEC is not None and _POLICY_SPEC.loader is not None
_ATTACHMENT_POLICY = importlib.util.module_from_spec(_POLICY_SPEC)
_POLICY_SPEC.loader.exec_module(_ATTACHMENT_POLICY)


def _image_content() -> list[dict[str, object]]:
    """Build a minimal multimodal content list used by all regression cases."""
    return [
        {"type": "text", "text": "please inspect this image"},
        {"type": "image_url", "image_url": {"url": "file://data/attachments/example.png"}},
    ]


def test_message_to_history_dict_keeps_current_task_images_and_strips_others() -> None:
    """Current task history keeps images; external or other task history strips them."""
    message = Message(
        id="msg_1",
        role="user",
        content=_image_content(),
        source_task_id="task-a",
    )

    same_task = _message_to_history_dict(message, current_task_id="task-a")
    other_task = _message_to_history_dict(message, current_task_id="task-b")
    default_call = _message_to_history_dict(message)

    assert same_task["content"] == _image_content()
    assert same_task["_meta"]["source_task_id"] == "task-a"
    # Why: cross-task image bytes are still stripped, but the local file path is
    # retained. How: assert the placeholder contains the read_file-compatible
    # path instead of the old generic marker. Purpose: allow later tasks to reopen
    # historical attachments without re-sending image data every turn.
    assert other_task["content"] == "please inspect this image\n[图片: data/attachments/example.png]"
    assert default_call["content"] == "please inspect this image\n[图片: data/attachments/example.png]"


def test_strip_images_from_content_keeps_file_paths_and_marks_inline_images() -> None:
    """Historical image stripping records file paths and inline image markers."""
    content = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "file://data/attachments/a.png"}},
        {"type": "image_url", "image_url": "file://data/attachments/b.png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        {"type": "text", "text": "second"},
    ]

    # Why: the runner must remove historical image payloads while preserving the
    # location of file-backed attachments. How: cover dict and string image_url
    # shapes plus an inline data URL fallback. Purpose: future model turns can use
    # read_file for saved attachments and still avoid leaking inline payloads.
    assert _strip_images_from_content(content) == (
        "first\nsecond\n"
        "[图片: data/attachments/a.png]\n"
        "[图片: data/attachments/b.png]\n"
        "[图片: <inline>]"
    )


def test_conversation_store_round_trips_multimodal_content(tmp_path: Path) -> None:
    """ConversationStore must serialize and reload list content without stringifying it."""
    store = ConversationStore(tmp_path)
    store.append(
        "session-a",
        Message(
            id="msg_1",
            role="user",
            content=_image_content(),
            message_type=MessageType.USER_INPUT,
            source_task_id="task-a",
        ),
    )

    loaded = store.load("session-a")

    assert loaded[0].content == _image_content()


def test_shadow_write_preserves_multimodal_content(tmp_path: Path) -> None:
    """Shadow writes must keep list content so a task reload can still see images."""
    store = ConversationStore(tmp_path)
    rctx = SimpleNamespace(
        conversation_store=store,
        child_session_id="",
        session_id="session-a",
        task_id="task-a",
        first_shadow_message_id="",
        last_shadow_message_id="",
    )
    loop_state = SimpleNamespace(
        rctx=rctx,
        node=SimpleNamespace(id="node-a"),
        last_shadow_message_id="",
    )

    _shadow_write(
        loop_state,
        {"role": "user", "content": _image_content()},
        MessageType.USER_INPUT,
    )

    loaded = store.load("session-a")
    assert loaded[0].content == _image_content()


@dataclass
class _RecentEntry:
    attachment: dict[str, Any]
    created_at: float
    sender_id: str
    message_id: str


def test_plain_text_request_is_not_misclassified_as_image_query() -> None:
    assert not _ATTACHMENT_POLICY.looks_like_image_query("帮我润色这段文字")
    assert not _ATTACHMENT_POLICY.looks_like_image_query("识别一下这个概念")
    assert not _ATTACHMENT_POLICY.looks_like_image_query("帮我解释这个意图")
    assert not _ATTACHMENT_POLICY.looks_like_image_query("今天去图书馆吗")
    assert not _ATTACHMENT_POLICY.looks_like_image_query("解释绘图算法和地图数据")
    assert _ATTACHMENT_POLICY.looks_like_image_query("再仔细看看图里的日文")


def test_reply_image_query_never_uses_unrelated_recent_image() -> None:
    assert not _ATTACHMENT_POLICY.should_fallback_to_recent_attachments(
        has_attachments=False,
        input_enabled=True,
        looks_like_query=True,
        reply_message_id="bot-reply-1",
    )


def test_recent_image_policy_keeps_latest_same_sender_message_batch() -> None:
    entries = [
        _RecentEntry({"type": "image", "path": "old-same.png"}, 95.0, "user-a", "msg-old"),
        _RecentEntry({"type": "image", "path": "other.gif"}, 98.0, "user-b", "msg-other"),
        _RecentEntry({"type": "image", "path": "new-1.png"}, 99.0, "user-a", "msg-new"),
        _RecentEntry({"type": "image", "path": "new-2.png"}, 99.0, "user-a", "msg-new"),
    ]
    selected = _ATTACHMENT_POLICY.select_recent_attachment_entries(
        entries,
        sender_id="user-a",
        now=100.0,
        max_age_seconds=10.0,
        max_items=4,
    )
    assert [item["path"] for item in selected] == ["new-1.png", "new-2.png"]


def test_queue_source_attachment_binding_keeps_all_merged_images() -> None:
    merged = _ATTACHMENT_POLICY.source_attachments_from_merged([
        {"type": "image", "path": "first.png"},
        {"type": "image", "path": "second.png"},
        {"type": "image", "path": "first.png"},
    ])
    assert [item["path"] for item in merged] == ["first.png", "second.png"]


def test_recent_image_policy_never_crosses_sender() -> None:
    selected = _ATTACHMENT_POLICY.select_recent_attachment_entries(
        [_RecentEntry({"type": "image", "path": "other.gif"}, 99.0, "user-b", "msg")],
        sender_id="user-a",
        now=100.0,
        max_age_seconds=10.0,
        max_items=4,
    )
    assert selected == []


def test_read_image_is_synchronous_and_failed_envelope_is_detected() -> None:
    assert READ_IMAGE_SPEC["async_mode"] is False
    assert _async_tool_failure({
        "ok": False,
        "error": "API HTTP 500",
        "data": {"result": "ERROR: API HTTP 500", "must_not_guess": True},
    }) == "API HTTP 500"
    assert _async_tool_failure({"ok": True, "data": {"result": "description"}}) == ""


def test_async_result_payload_carries_structured_failure_metadata() -> None:
    payload = _build_async_result_payload(
        message="failed",
        tool_name="read_image",
        task_id="task-1",
        node_id="qq.orchestrator",
        failure_error="API HTTP 500",
        attachments=["data/attachments/a.png"],
    )
    assert payload == {
        "message": "failed",
        "tool_name": "read_image",
        "task_id": "task-1",
        "node_id": "qq.orchestrator",
        "success": False,
        "error": "API HTTP 500",
        "attachment_paths": ["data/attachments/a.png"],
    }


class _AsyncRegistry:
    def get_spec(self, name: str):
        return None


class _AsyncHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]):
        self.posts.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200)


def test_adaptive_async_delivery_marks_ok_false_as_failure() -> None:
    http = _AsyncHttp()
    asyncio.run(_run_async_deliver(
        registry=_AsyncRegistry(),
        http=http,
        supervisor_url="http://supervisor",
        task_id="task-1",
        session_id="session-1",
        tool_name="read_image",
        tool_args={},
        async_tool_id="async-1",
        started_at=time.monotonic(),
        node_id="qq.orchestrator",
        result={"ok": False, "error": "API HTTP 500", "data": {"result": "ERROR: API HTTP 500"}},
    ))
    assert http.posts[-1]["json"]["success"] is False
    assert http.posts[-1]["json"]["error"] == "API HTTP 500"
    assert http.posts[-1]["json"]["node_id"] == "qq.orchestrator"
    assert http.posts[-1]["json"]["message"].startswith('❌ Async tool "read_image"')


def test_adaptive_async_delivery_keeps_success_successful() -> None:
    http = _AsyncHttp()
    asyncio.run(_run_async_deliver(
        registry=_AsyncRegistry(),
        http=http,
        supervisor_url="http://supervisor",
        task_id="task-2",
        session_id="session-2",
        tool_name="demo",
        tool_args={},
        async_tool_id="async-2",
        started_at=time.monotonic(),
        node_id="node-a",
        result={"ok": True, "data": {"result": "done"}},
    ))
    assert http.posts[-1]["json"]["success"] is True
    assert "error" not in http.posts[-1]["json"]
    assert http.posts[-1]["json"]["message"].startswith('✅ Async tool "demo"')


def _finish_guard_ctx(
    *,
    messages: list[dict[str, Any]],
    finish_text: str,
    succeeded: set[str] | None = None,
) -> SimpleNamespace:
    call = SimpleNamespace(name="finish", arguments={"text": finish_text})
    return SimpleNamespace(
        node=SimpleNamespace(id="qq.orchestrator", extra={}),
        step=1,
        tool_calls=[call],
        messages=messages,
        extra={
            "pseudo_calls": [call],
            "real_tool_calls": [],
            "loop_state": SimpleNamespace(
                succeeded_real_tools=set(succeeded or set()),
                failed_real_tools={"read_image"},
            ),
        },
    )


def test_finish_guard_blocks_image_claim_after_read_image_failure() -> None:
    ctx = _finish_guard_ctx(
        messages=[{"role": "user", "content": 'Tool result for "read_image":\nERROR: API HTTP 500'}],
        finish_text="我看到图中是一名少女，翻译如下……",
    )
    result = asyncio.run(FinishGuardHandler().handle(ctx))
    assert result is not None and result.block is True
    assert result.reason == "vision_failure_ungrounded_finish"


def test_finish_guard_clears_failure_after_sync_read_image_success() -> None:
    ctx = _finish_guard_ctx(
        messages=[
            {"role": "user", "content": 'Tool result for "read_image":\nERROR: timeout'},
            {"role": "user", "content": 'Tool result for "read_image":\nA white cat on a chair.'},
        ],
        finish_text="图中是一只坐在椅子上的白猫。",
        succeeded={"read_image"},
    )
    assert asyncio.run(FinishGuardHandler().handle(ctx)) is None

def test_vision_failure_cannot_be_pushed_out_by_many_tool_messages() -> None:
    messages = [
        {"role": "user", "content": "请识别当前图片"},
        {"role": "tool", "content": 'Tool result for "read_image":\nERROR: timeout'},
    ]
    messages.extend(
        {"role": "tool", "content": f'Tool result for "other_{index}": ok'}
        for index in range(20)
    )
    assert _has_unresolved_vision_failure(messages) is True


def test_old_vision_failure_does_not_poison_later_user_turn() -> None:
    messages = [
        {"role": "tool", "content": 'Tool result for "read_image":\nERROR: timeout'},
        {"role": "user", "content": "现在解释一个普通概念"},
    ]
    assert _has_unresolved_vision_failure(messages) is False


def test_finish_guard_blocks_latest_failure_after_prior_success() -> None:
    ctx = _finish_guard_ctx(
        messages=[
            {"role": "tool", "content": 'Tool result for "read_image":\nA cat.'},
            {"role": "tool", "content": 'Tool result for "read_image":\nERROR: timeout'},
        ],
        finish_text="图中是一只猫。",
        succeeded={"read_image"},
    )
    result = asyncio.run(FinishGuardHandler().handle(ctx))
    assert result is not None and result.block is True
    assert result.reason == "vision_failure_ungrounded_finish"


def test_finish_guard_allows_honest_read_image_failure_notice() -> None:
    ctx = _finish_guard_ctx(
        messages=[{"role": "user", "content": '❌ Async tool "read_image" (id: x) failed: timeout'}],
        finish_text="这次识图失败了，请重新发送或直接引用原图后再试。",
    )
    assert asyncio.run(FinishGuardHandler().handle(ctx)) is None
