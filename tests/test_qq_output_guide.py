"""QQ 这一轮的输出说明：提交或打断那一刻算一次，随 output_guide 单独带，不拼进会写进对话历史的正文。"""
from __future__ import annotations

import asyncio
import contextlib
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

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from adapters._shared.output_guide import SECTION_EMOJI, SECTION_REACTIONS, SECTION_SPLIT  # noqa: E402
from clonoth_sdk.state import SessionState, TriggerInfo  # noqa: E402

_GROUP_ID = 700001
_USER_QQ = 30003
_GROUP_KEY = "conv_group"
_PRIVATE_KEY = "conv_private"


class _Core:
    """假的 Clonoth 核心：记下提交和打断；running 是会话里正在跑的任务，preempt_ok 决定打断收不收。"""

    def __init__(self, *, running: tuple[Any, ...] = (), preempt_ok: bool = True) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.preempted: list[dict[str, Any]] = []
        self.running = running
        self.preempt_ok = preempt_ok

    async def submit_inbound(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        return SimpleNamespace(session_id="session", accepted=True, inbound_seq=10 + len(self.submitted))

    async def get_running_tasks(self, session_id: str) -> list[Any]:
        return list(self.running)

    async def preempt_task(self, task_id: str, **kwargs: Any) -> bool:
        self.preempted.append({"task_id": task_id, **kwargs})
        return self.preempt_ok


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    module.state._adapter.session_state = SessionState()
    set_live_config(module, enable_queue=False, enable_preempt=False, enable_reactions=True, sticker_send_probability=0.0)
    monkeypatch.setattr(module.emotes, "_current_custom_face_names", lambda: ["收藏甲"])

    async def remember(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module.routing, "_remember_route_state", remember)
    yield module
    module.state._adapter.outbound_idempotency.close()


def _event(*, private: bool = False, message_id: int = 42) -> SimpleNamespace:
    event = SimpleNamespace(
        user_id=_USER_QQ, message_id=message_id, time=1_700_000_000,
        sender=SimpleNamespace(user_id=_USER_QQ, nickname="某人", card="", role="member"),
        get_message=lambda: [],
    )
    if not private:
        event.group_id = _GROUP_ID
    return event


def _platform(event: SimpleNamespace, *, private: bool = False) -> dict[str, Any]:
    if private:
        return {"type": "private", "user_id": _USER_QQ, "event": event, "conversation_key": _PRIVATE_KEY}
    return {"type": "group", "group_id": _GROUP_ID, "user_id": _USER_QQ, "event": event, "conversation_key": _GROUP_KEY}


def _submit(runtime: Any, core: _Core, *, private: bool = False, entry_node_id: str = "") -> bool:
    runtime.state._adapter.client = core
    event = _event(private=private)
    return asyncio.run(runtime.queueing._submit_or_preempt_inbound(
        bot=SimpleNamespace(self_id="90000"),
        event=event,
        channel="qq_private" if private else "qq_group",
        real_conversation_key=f"qq_private:{_USER_QQ}" if private else f"qq_group:{_GROUP_ID}",
        stable_conversation_key=_PRIVATE_KEY if private else _GROUP_KEY,
        inbound_text="正文",
        user_text="在吗",
        attachments=[],
        is_dm=private,
        platform_updates=_platform(event, private=private),
        entry_node_id=entry_node_id,
    ))


def _sections(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {section["key"]: section for section in payload["sections"]}


def _counted_guides(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    build = runtime.output_guide._turn_output_guide

    def counted(conversation_key: str, **kwargs: Any) -> Any:
        calls.append(conversation_key)
        return build(conversation_key, **kwargs)

    monkeypatch.setattr(runtime.output_guide, "_turn_output_guide", counted)
    return calls


class TestSubmission:
    def test_a_group_turn_carries_splitting_reactions_and_emoji(self, runtime: Any) -> None:
        core = _Core()

        assert _submit(runtime, core) is True
        [call] = core.submitted
        sections = _sections(call["output_guide"])

        assert list(sections) == [SECTION_SPLIT, SECTION_REACTIONS, SECTION_EMOJI]
        assert "收藏表情（只能用这些名字）：收藏甲" in sections[SECTION_EMOJI]["text"]
        assert call["text"] == "正文"

    def test_the_catalogs_are_stable_and_the_emoji_roster_changes_every_turn(self, runtime: Any) -> None:
        core = _Core()

        _submit(runtime, core)
        sections = _sections(core.submitted[0]["output_guide"])

        # 稳定的进缓存前缀；表情清单每轮按概率抽，放动态区。
        assert {key: section["volatile"] for key, section in sections.items()} == {
            SECTION_SPLIT: False, SECTION_REACTIONS: False, SECTION_EMOJI: True,
        }

    def test_a_private_turn_carries_no_reactions(self, runtime: Any) -> None:
        core = _Core()

        _submit(runtime, core, private=True)

        assert list(_sections(core.submitted[0]["output_guide"])) == [SECTION_SPLIT, SECTION_EMOJI]

    def test_a_draw_turn_only_teaches_splitting_and_rolls_no_stickers(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rolled: list[str] = []
        monkeypatch.setattr(runtime.emotes, "_sticker_prompt_entries", lambda key: rolled.append(key) or [])
        core = _Core()

        _submit(runtime, core, entry_node_id=runtime.common.DRAW_NODE_ID)

        assert list(_sections(core.submitted[0]["output_guide"])) == [SECTION_SPLIT]
        assert rolled == []

    def test_switching_reactions_off_takes_them_out_of_the_next_turn(self, runtime: Any) -> None:
        set_live_config(runtime, enable_reactions=False)
        core = _Core()

        _submit(runtime, core)

        assert SECTION_REACTIONS not in _sections(core.submitted[0]["output_guide"])


class TestPreempt:
    @staticmethod
    def _running(runtime: Any) -> _Core:
        set_live_config(runtime, enable_preempt=True)
        previous = _event(message_id=41)
        runtime.state._adapter.session_state.register_session(_GROUP_KEY, "session")
        runtime.state._adapter.session_state.register_trigger(
            TriggerInfo(10, _GROUP_KEY, "session", False, platform_data=_platform(previous)),
        )
        return _Core(running=(SimpleNamespace(task_id="task-1", is_user_entry=True, source_inbound_seq=10),))

    def test_the_running_task_gets_this_turn_guide(self, runtime: Any) -> None:
        core = self._running(runtime)

        assert _submit(runtime, core) is True

        [preempt] = core.preempted
        assert preempt["task_id"] == "task-1" and core.submitted == []
        assert list(_sections(preempt["output_guide"])) == [SECTION_SPLIT, SECTION_REACTIONS, SECTION_EMOJI]

    def test_a_refused_preempt_submits_the_same_guide_without_rolling_again(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = self._running(runtime)
        core.preempt_ok = False
        built = _counted_guides(runtime, monkeypatch)

        _submit(runtime, core)

        [preempt], [call] = core.preempted, core.submitted
        assert call["output_guide"] == preempt["output_guide"]
        assert built == [_GROUP_KEY]


class TestQueueMerge:
    @staticmethod
    def _queued(runtime: Any, message_id: int, text: str) -> Any:
        event = _event(message_id=message_id)
        return runtime.models.QueuedInbound(
            matcher=None, bot=SimpleNamespace(self_id="90000"), event=event, channel="qq_group",
            real_conversation_key=f"qq_group:{_GROUP_ID}", stable_conversation_key=_GROUP_KEY,
            text=text, user_text=text, attachments=[], is_dm=False, platform_updates=_platform(event),
        )

    def test_messages_merged_in_the_queue_share_one_guide(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, enable_queue=True, queue_workers=1, queue_interval=0)
        core = _Core()
        runtime.state._adapter.client = core
        built = _counted_guides(runtime, monkeypatch)

        async def exercise() -> None:
            await runtime.queueing._enqueue_or_submit_inbound(self._queued(runtime, 41, "第一句"))
            await runtime.queueing._enqueue_or_submit_inbound(self._queued(runtime, 42, "第二句"))
            worker = asyncio.create_task(runtime.queueing._qq_queue_worker_forever(0))
            try:
                for _ in range(25):
                    await asyncio.sleep(0)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

        asyncio.run(exercise())

        [call] = core.submitted
        assert "第一句" in call["text"] and "第二句" in call["text"]
        # 说明拼在正文里时，合并一次就多带一份。
        assert [section["key"] for section in call["output_guide"]["sections"]].count(SECTION_SPLIT) == 1
        assert built == [_GROUP_KEY]
