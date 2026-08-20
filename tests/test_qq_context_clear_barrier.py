"""清空上下文之后，还在飞的那一轮不能把旧产物落回缓存。

清空只动得了当下的缓存，动不了已经派出去的任务。它回来投递时照发消息，但回写要挡住 ——
否则下一轮 inbound 又把它带给模型，那次清空等于没做。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._onebot_harness import load_runtime

_GROUP = 778899


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    module._context_clear_barrier.clear()
    module._last_inbound_seq = 0
    return module


def _ctx(seq: int) -> SimpleNamespace:
    return SimpleNamespace(source_inbound_seq=seq)


class TestBarrier:
    def test_the_barrier_lands_on_the_highest_seq_seen(self, runtime) -> None:
        runtime._note_inbound_seq(40)
        runtime._note_inbound_seq(12)
        runtime._raise_context_clear_barrier(_GROUP)

        assert runtime._context_clear_barrier[_GROUP] == 40

    def test_triggers_still_in_flight_raise_it_further(self, runtime) -> None:
        # 被清掉的 trigger 可能比 adapter 记下的最大序号还新。
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP, [SimpleNamespace(inbound_seq=57)])

        assert runtime._context_clear_barrier[_GROUP] == 57

    def test_a_seq_at_or_below_the_barrier_is_stale(self, runtime) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)

        assert runtime._is_stale_delivery(_GROUP, _ctx(40))
        assert runtime._is_stale_delivery(_GROUP, _ctx(9))

    def test_anything_newer_goes_through(self, runtime) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)

        assert not runtime._is_stale_delivery(_GROUP, _ctx(41))

    def test_without_a_barrier_nothing_is_stale(self, runtime) -> None:
        assert not runtime._is_stale_delivery(_GROUP, _ctx(1))

    def test_a_missing_seq_is_let_through(self, runtime) -> None:
        # 判断不了就放行：宁可多留一条，也不能把正常回复从群历史里抹掉。
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)

        assert not runtime._is_stale_delivery(_GROUP, _ctx(0))
        assert not runtime._is_stale_delivery(_GROUP, SimpleNamespace())
        assert not runtime._is_stale_delivery(_GROUP, None)

    def test_private_chats_have_no_group_to_gate(self, runtime) -> None:
        assert not runtime._is_stale_delivery(None, _ctx(1))

    def test_another_group_is_unaffected(self, runtime) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)

        assert not runtime._is_stale_delivery(_GROUP + 1, _ctx(3))


class TestWriteBack:
    """走一遍真实发送路径，确认挡的是回写而不是发送。"""

    def _send(self, runtime, monkeypatch: pytest.MonkeyPatch, seq: int) -> list[str]:
        sent: list[str] = []

        async def _fake_split(bot, target, text, **kwargs):
            sent.append(text)
            return True

        monkeypatch.setattr(runtime, "_send_split_text", _fake_split)
        target = {"type": "group", "group_id": _GROUP, "conversation_key": "qq_group:x"}
        asyncio.run(runtime._send_text_and_attachments(
            None, target, "回复内容", [], send_context=_ctx(seq)))
        return sent

    def test_a_stale_reply_is_still_delivered(self, runtime, monkeypatch) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)

        assert self._send(runtime, monkeypatch, 40) == ["回复内容"]

    def test_a_stale_reply_never_reaches_the_cache(self, runtime, monkeypatch) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)
        self._send(runtime, monkeypatch, 40)

        assert len(runtime._group_history[_GROUP]) == 0
        assert len(runtime._group_content_records[_GROUP]) == 0

    def test_a_fresh_reply_is_recorded_as_usual(self, runtime, monkeypatch) -> None:
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)
        self._send(runtime, monkeypatch, 41)

        assert len(runtime._group_history[_GROUP]) == 1
        assert len(runtime._group_content_records[_GROUP]) == 1

    def test_no_barrier_means_business_as_usual(self, runtime, monkeypatch) -> None:
        self._send(runtime, monkeypatch, 7)

        assert len(runtime._group_history[_GROUP]) == 1


class TestEngineRestart:
    def test_restarting_drops_every_barrier(self, runtime) -> None:
        # 序号由 supervisor 分配，重启后不保证还在原来那条线上；留着旧门槛会把之后
        # 每一条正常回复都判成过期。
        runtime._note_inbound_seq(40)
        runtime._raise_context_clear_barrier(_GROUP)
        runtime._group_history[_GROUP]

        asyncio.run(runtime.TangQiuCallbacks().on_engine_restarted({}))

        assert runtime._context_clear_barrier == {}
