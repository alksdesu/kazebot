"""/生图 直达命令的入口节点必须钉死：不被 preempt、队列合并或历史图抢走。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from clonoth_sdk.state import SessionState  # noqa: E402
from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_GROUP_ID = 778899
_REAL_KEY = f"qq_group:{_GROUP_ID}"
_USER_QQ = 30003


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return load_runtime(monkeypatch, tmp_path)


def _event() -> SimpleNamespace:
    return SimpleNamespace(user_id=_USER_QQ, group_id=_GROUP_ID, message_id=42, get_message=lambda: [])


def _resolved(value: Any):
    async def call(*args: Any, **kwargs: Any) -> Any:
        return value
    return call


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    return None


def _submit_capture(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> dict:
    runtime._session_state = SessionState()
    captured: dict[str, Any] = {}

    async def _submit(**kwargs: Any):
        captured.update(kwargs)
        return SimpleNamespace(session_id="sess-1", accepted=True, inbound_seq=1)

    monkeypatch.setattr(runtime, "_client", SimpleNamespace(submit_inbound=_submit))
    monkeypatch.setattr(runtime, "_remember_route_state", _noop_async)
    return captured


def _run_submit(runtime: Any, entry_node_id: str) -> bool:
    return asyncio.run(runtime._submit_or_preempt_inbound(
        bot=SimpleNamespace(self_id="90000"),
        event=_event(),
        channel="qq_group",
        real_conversation_key=_REAL_KEY,
        stable_conversation_key="conv_draw",
        inbound_text="正文",
        user_text="/生图 画一只猫",
        attachments=[],
        is_dm=False,
        platform_updates={"type": "group", "group_id": _GROUP_ID},
        entry_node_id=entry_node_id,
    ))


def test_draw_command_pins_entry_node(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, enable_preempt=False, enable_reactions=False)
    captured = _submit_capture(runtime, monkeypatch)

    assert _run_submit(runtime, "draw.novelai_planner") is True
    assert captured["entry_node_id"] == "draw.novelai_planner"
    assert captured["entry_node_pinned"] is True


def test_normal_message_does_not_pin_entry_node(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, enable_preempt=False, enable_reactions=False)
    captured = _submit_capture(runtime, monkeypatch)

    assert _run_submit(runtime, "") is True
    assert captured["entry_node_pinned"] is False


def test_preempt_is_skipped_when_entry_node_is_pinned(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, enable_preempt=True, enable_reactions=False)
    captured = _submit_capture(runtime, monkeypatch)
    preempt_calls: list[dict] = []

    async def _preempt(**kwargs: Any) -> bool:
        preempt_calls.append(kwargs)
        return True

    monkeypatch.setattr(runtime, "_try_preempt_running_task", _preempt)

    assert _run_submit(runtime, "draw.novelai_planner") is True
    assert preempt_calls == []
    assert captured.get("entry_node_id") == "draw.novelai_planner"


def test_preempt_still_runs_for_normal_messages(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, enable_preempt=True, enable_reactions=False)
    _submit_capture(runtime, monkeypatch)
    preempt_calls: list[dict] = []

    async def _preempt(**kwargs: Any) -> bool:
        preempt_calls.append(kwargs)
        return True

    monkeypatch.setattr(runtime, "_try_preempt_running_task", _preempt)

    assert _run_submit(runtime, "") is True
    assert len(preempt_calls) == 1


def test_queue_does_not_merge_across_entry_nodes(runtime: Any) -> None:
    set_live_config(runtime, enable_queue=True)

    def _item(entry_node_id: str):
        return runtime.QueuedInbound(
            matcher=None, bot=None, event=None, channel="qq_group",
            real_conversation_key=_REAL_KEY, stable_conversation_key="conv_draw",
            text="正文", attachments=[], is_dm=False, platform_updates={},
            user_text="x", entry_node_id=entry_node_id,
        )

    first = _item("")
    second = _item("draw.novelai_planner")
    asyncio.run(runtime._enqueue_or_submit_inbound(first))
    asyncio.run(runtime._enqueue_or_submit_inbound(second))

    assert len(runtime._qq_queue) == 2
    assert first.entry_node_id == ""
    assert runtime._qq_queue_by_key["conv_draw"] is second


def test_draw_command_does_not_absorb_recent_history_images(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, enable_reactions=False, enable_image_input=True)
    set_live_config(runtime, image_wait_after_text_sec=0)
    runtime._client = SimpleNamespace()
    runtime._session_state = SessionState()

    monkeypatch.setattr(runtime, "_remember_message_for_reply_context", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_event_text_with_forward", _resolved("/生图 画一张图片"))
    monkeypatch.setattr(runtime, "_is_direct_bot_interaction", lambda *a, **k: True)
    monkeypatch.setattr(runtime, "_strip_trigger_prefix", lambda text: text)
    monkeypatch.setattr(runtime, "_auto_like_user", _noop_async)
    monkeypatch.setattr(runtime, "_collect_qq_attachments", _resolved(([], [])))
    monkeypatch.setattr(runtime, "_remember_recent_images", lambda *a, **k: None)
    for name in (
        "_maybe_handle_clear_group_memory_command", "_maybe_handle_model_command",
        "_maybe_handle_drawtools_command", "_maybe_handle_custom_face_command",
        "_maybe_handle_proactive_command",
    ):
        monkeypatch.setattr(runtime, name, _noop_async)
    monkeypatch.setattr(runtime, "_record_group_message", lambda *a, **k: 1)
    monkeypatch.setattr(runtime, "_build_draw_direct_inbound_text", _resolved("draw text"))

    # 历史图有货：只有当 draw 判定漏掉、去调 merge 时才会被查询并并进来。
    recent_calls: list[Any] = []

    def _recent(*args: Any, **kwargs: Any):
        recent_calls.append(args)
        return [{"type": "image", "path": "hist.png"}]

    monkeypatch.setattr(runtime, "_recent_images_for_text", _recent)

    sends: list[str] = []

    async def _send(message: Any) -> None:
        sends.append(message)

    captured: dict[str, Any] = {}

    async def _capture(item: Any) -> bool:
        captured["item"] = item
        return True

    monkeypatch.setattr(runtime, "_enqueue_or_submit_inbound", _capture)

    bot = SimpleNamespace(self_id="90000")
    matcher = SimpleNamespace(finish=_noop_async, send=_send)
    asyncio.run(runtime._process_group_message(bot, _event(), matcher))

    assert captured["item"].entry_node_id == runtime.DRAW_NODE_ID
    # merge 被跳过：历史图从未被查询，attachments 保持空，也不会误发“忽略参考图”提示。
    assert recent_calls == []
    assert captured["item"].attachments == []
    assert sends == []
