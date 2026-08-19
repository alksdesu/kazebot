"""群历史高水位：每条群消息只进 durable history 一次。

inbound 文本里的【群聊上下文记录】会被 engine 原样存成一条 role=user，而缓存保留最近
20 行 —— 不做水位就是同一条群消息被 20 轮 inbound 各带一遍，在存储和后续每次推理的
上下文里都是 20 份。水位反过来的风险是历史丢失：推进之后 engine 侧若被清空，那段群聊
再也不会重发，所以每条清空路径都必须一起重置。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clonoth_sdk.state import SessionState
from tests._onebot_harness import load_runtime, set_live_config

_GROUP_ID = 998877
_REAL_KEY = f"qq_group:{_GROUP_ID}"


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    module._session_state = SessionState()
    return module


def _event(runtime, user_id: int = 10001, group_id: int = _GROUP_ID) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        group_id=group_id,
        message_id=1,
        time=1_700_000_000,
        sender=SimpleNamespace(nickname="某人", card="", user_id=user_id),
        get_message=lambda: [],
    )


def _build(runtime, user_id: int = 10001, group_id: int = _GROUP_ID, *, exclude_seq: int = 0) -> tuple[str, int]:
    return asyncio.run(runtime._build_inbound_text(
        _event(runtime, user_id, group_id),
        SimpleNamespace(self_id="90000"),
        "在吗",
        "conv_abc",
        [],
        exclude_seq=exclude_seq,
    ))


def _record(runtime, text: str = "在吗", group_id: int = _GROUP_ID) -> int:
    """走真实记录路径写一条群消息，返回它的序号。"""
    return runtime._record_group_message(
        _event(runtime, group_id=group_id),
        SimpleNamespace(self_id="90000"),
        override_text=text,
    )


def _history_block(text: str) -> list[str]:
    """取出【群聊上下文记录】到下一个块标记之前的行。"""
    lines = text.splitlines()
    start = lines.index("【群聊上下文记录】") + 1
    out: list[str] = []
    for line in lines[start:]:
        if not line or line.startswith("【") or line.startswith("当前时间"):
            break
        out.append(line)
    return out


class TestSequenceAllocation:
    def test_sequences_increase_within_a_group(self, runtime) -> None:
        first = runtime._append_group_history(_GROUP_ID, "a")
        second = runtime._append_group_history(_GROUP_ID, "b")

        assert (first, second) == (1, 2)

    def test_groups_number_their_lines_independently(self, runtime) -> None:
        runtime._append_group_history(_GROUP_ID, "a")

        assert runtime._append_group_history(_GROUP_ID + 1, "a") == 1

    def test_bot_replies_are_numbered_too(self, runtime) -> None:
        # Bot 的回复行留在历史块里：compact 重置水位后重发的 20 行需要完整时序。
        runtime._record_bot_reply(_GROUP_ID, "好的")

        assert [entry.seq for entry in runtime._group_history[_GROUP_ID]] == [1]

    def test_sequences_survive_a_cache_reset(self, runtime) -> None:
        # 计数器不跟着缓存清零：万一水位没能一起重置，重开的小序号会被旧水位吞掉。
        runtime._append_group_history(_GROUP_ID, "a")
        runtime._group_history.pop(_GROUP_ID, None)

        assert runtime._append_group_history(_GROUP_ID, "b") == 2

    def test_content_records_share_the_history_sequence(
        self, runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 三条写入路径都要把 seq 接进结构化副本，漏一个那条消息就永远挑不中。
        set_live_config(runtime, allowed_groups=[_GROUP_ID])

        runtime._record_group_message(
            _event(runtime), SimpleNamespace(self_id="90000"), override_text="hi",
        )
        assert (
            runtime._group_content_records[_GROUP_ID][-1].seq
            == runtime._group_history[_GROUP_ID][-1].seq
        )

        runtime._record_bot_reply(_GROUP_ID, "好的")
        assert (
            runtime._group_content_records[_GROUP_ID][-1].seq
            == runtime._group_history[_GROUP_ID][-1].seq
        )

        async def _no_files(*args, **kwargs):
            return [], []

        monkeypatch.setattr(runtime, "_file_sources_to_attachments", _no_files)
        upload = SimpleNamespace(
            group_id=_GROUP_ID, user_id=10001, message_id=7, time=1_700_000_000,
            sender=SimpleNamespace(nickname="某人", card="", user_id=10001),
            file={"name": "report.pdf", "url": ""},
            get_message=lambda: [],
        )
        asyncio.run(runtime._handle_group_upload_notice(SimpleNamespace(self_id="90000"), upload))
        assert (
            runtime._group_content_records[_GROUP_ID][-1].seq
            == runtime._group_history[_GROUP_ID][-1].seq
        )


class TestHistoryFiltering:
    def test_a_first_round_carries_everything(self, runtime) -> None:
        for line in ("a", "b", "c"):
            runtime._append_group_history(_GROUP_ID, line)

        text, covered = _build(runtime)

        assert _history_block(text) == ["a", "b", "c"]
        assert covered == 3

    def test_an_empty_group_says_nothing_yet(self, runtime) -> None:
        text, covered = _build(runtime)

        assert _history_block(text) == ["（暂无）"]
        assert covered == -1

    def test_only_lines_past_the_watermark_are_carried(self, runtime) -> None:
        for line in ("a", "b", "c"):
            runtime._append_group_history(_GROUP_ID, line)
        runtime._session_state.advance_watermark(_GROUP_ID, 2)

        text, covered = _build(runtime)

        assert _history_block(text) == ["c"]
        assert covered == 3

    def test_no_new_lines_reads_differently_from_never_spoke(self, runtime) -> None:
        runtime._append_group_history(_GROUP_ID, "a")
        runtime._session_state.advance_watermark(_GROUP_ID, 1)

        text, covered = _build(runtime)

        assert _history_block(text) == ["（无新消息）"]
        assert covered == 1

    def test_a_line_is_never_carried_twice(self, runtime) -> None:
        runtime._append_group_history(_GROUP_ID, "只说一次")
        _text, covered = _build(runtime)
        runtime._session_state.advance_watermark(_GROUP_ID, covered)

        assert "只说一次" not in _history_block(_build(runtime)[0])

    def test_one_group_watermark_does_not_hide_another_group(self, runtime) -> None:
        runtime._append_group_history(_GROUP_ID, "a")
        runtime._append_group_history(_GROUP_ID + 1, "b")
        runtime._session_state.advance_watermark(_GROUP_ID, 1)

        assert _history_block(_build(runtime, group_id=_GROUP_ID + 1)[0]) == ["b"]

    def test_a_missing_session_state_carries_full_history(self, runtime) -> None:
        # 插件还没启动完时读不到水位，宁可重复也不能漏。
        runtime._append_group_history(_GROUP_ID, "a")
        runtime._session_state = None

        assert _history_block(_build(runtime)[0]) == ["a"]

    def test_the_trigger_message_is_not_repeated_in_the_history_block(self, runtime) -> None:
        # 触发消息由【当前用户指令】带出，历史块再放一遍就是让模型看两遍。
        runtime._append_group_history(_GROUP_ID, "别人说的")
        seq = _record(runtime)

        text, _covered = _build(runtime, exclude_seq=seq)

        assert _history_block(text) == ["别人说的"]
        assert text.count("在吗") == 1

    def test_the_excluded_line_is_still_covered_by_the_watermark(self, runtime) -> None:
        # 水位不盖过它，它下一轮就会以历史行的身份再来一次。
        seq = _record(runtime)

        _text, covered = _build(runtime, exclude_seq=seq)

        assert covered == seq

    def test_a_recorded_message_reports_its_sequence(self, runtime) -> None:
        seq = _record(runtime)

        assert seq == runtime._group_history[_GROUP_ID][-1].seq

    def test_an_empty_message_reports_no_sequence(self, runtime) -> None:
        assert _record(runtime, text=" ") == 0


class TestOverflowIsNotSilent:
    """缓存上限吃掉还没送到 engine 的行时，prompt 里必须看得见少了几条。"""

    def _fill(self, runtime, count: int, *, first: int = 0) -> None:
        for index in range(first, first + count):
            runtime._append_group_history(_GROUP_ID, f"line{index}")

    def test_evicting_an_undelivered_line_is_reported_in_the_prompt(self, runtime) -> None:
        set_live_config(runtime, group_history_max=3)
        self._fill(runtime, 5)

        block = _history_block(_build(runtime)[0])

        assert block[0] == "（此前 2 条群消息超出缓存上限，未包含在内）"
        assert block[1:] == ["line2", "line3", "line4"]

    def test_a_delivered_line_falling_out_is_not_reported(self, runtime) -> None:
        set_live_config(runtime, group_history_max=3)
        self._fill(runtime, 3)
        runtime._session_state.advance_watermark(_GROUP_ID, 3)
        self._fill(runtime, 2, first=3)

        block = _history_block(_build(runtime)[0])

        assert block == ["line3", "line4"]

    def test_the_notice_disappears_once_the_watermark_passes_the_gap(self, runtime) -> None:
        set_live_config(runtime, group_history_max=3)
        self._fill(runtime, 5)
        _text, covered = _build(runtime)
        runtime._session_state.advance_watermark(_GROUP_ID, covered)

        assert _history_block(_build(runtime)[0]) == ["（无新消息）"]

    def test_shrinking_the_capacity_records_the_gap(self, runtime) -> None:
        set_live_config(runtime, group_history_max=10)
        self._fill(runtime, 5)
        set_live_config(runtime, group_history_max=2)

        assert runtime._reconcile_group_history_capacity() is True
        assert runtime._group_history_lost(_GROUP_ID, -1) == 3
        assert _history_block(_build(runtime)[0])[0].startswith("（此前 3 条")

    def test_a_watermark_reset_clears_the_gap(self, runtime) -> None:
        # compact 保留缓存只重置水位，下一轮整份重发，按旧水位算的缺口不再成立。
        set_live_config(runtime, group_history_max=3)
        self._fill(runtime, 5)

        runtime._reset_group_history_watermark(_GROUP_ID)

        assert runtime._group_history_gap.get(_GROUP_ID, 0) == 0
        assert not _history_block(_build(runtime)[0])[0].startswith("（此前")

    def test_history_is_off_when_the_cap_is_zero(self, runtime) -> None:
        # maxlen=0 时 lines[0] 会 IndexError，而关掉群历史不是缺口。
        set_live_config(runtime, group_history_max=0)
        self._fill(runtime, 2)

        assert not runtime._group_history[_GROUP_ID]
        assert _history_block(_build(runtime)[0]) == ["（暂无）"]


class TestUndeliveredLinesCanOverflowTheSoftCap:
    """ONEBOT_GROUP_HISTORY_UNDELIVERED_RATIO 只放宽还没送出去的那些行。"""

    @pytest.fixture()
    def doubled(self, runtime, monkeypatch: pytest.MonkeyPatch):
        set_live_config(runtime, group_history_max=3, group_history_undelivered_ratio=2.0)
        return runtime

    def test_undelivered_lines_are_kept_past_the_soft_cap(self, doubled) -> None:
        for index in range(6):
            doubled._append_group_history(_GROUP_ID, f"line{index}")

        block = _history_block(_build(doubled)[0])

        assert block == [f"line{index}" for index in range(6)]

    def test_delivered_lines_do_not_get_the_extra_room(self, doubled) -> None:
        for index in range(6):
            doubled._append_group_history(_GROUP_ID, f"line{index}")
        doubled._session_state.advance_watermark(_GROUP_ID, 6)
        doubled._append_group_history(_GROUP_ID, "line6")

        assert [entry.text for entry in doubled._group_history[_GROUP_ID]] == ["line4", "line5", "line6"]

    def test_the_hard_cap_still_reports_its_gap(self, doubled) -> None:
        for index in range(8):
            doubled._append_group_history(_GROUP_ID, f"line{index}")

        assert doubled._group_history_lost(_GROUP_ID, -1) == 2


class TestWatermarkAdvance:
    @pytest.fixture()
    def submitted(self, runtime, monkeypatch: pytest.MonkeyPatch):
        """把 submit 的返回值做成可调参数，返回一个执行提交的函数。"""
        outcome = SimpleNamespace(session_id="sess-1", accepted=True, inbound_seq=77)

        async def _submit(**kwargs: Any):
            return outcome

        monkeypatch.setattr(runtime, "_client", SimpleNamespace(submit_inbound=_submit))
        set_live_config(runtime, enable_preempt=False, enable_reactions=False)

        async def _noop(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr(runtime, "_remember_route_state", _noop)

        def _run(watermark: int = 3, *, is_dm: bool = False) -> bool:
            updates = (
                {"type": "private", "user_id": 10001}
                if is_dm
                else {"type": "group", "group_id": _GROUP_ID}
            )
            return asyncio.run(runtime._submit_or_preempt_inbound(
                bot=SimpleNamespace(self_id="90000"),
                event=_event(runtime),
                channel="qq_private" if is_dm else "qq_group",
                real_conversation_key=_REAL_KEY,
                stable_conversation_key="conv_abc",
                inbound_text="正文",
                user_text="在吗",
                attachments=[],
                is_dm=is_dm,
                platform_updates=updates,
                history_watermark=watermark,
            ))

        return SimpleNamespace(run=_run, outcome=outcome)

    def test_a_submit_only_registers_a_pending_watermark(self, runtime, submitted) -> None:
        assert submitted.run(3) is True

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1
        assert runtime._session_state.pending_watermarks[77] == (_GROUP_ID, 3)

    def test_the_watermark_advances_once_the_inbound_is_accepted(self, runtime, submitted) -> None:
        submitted.run(3)

        runtime._session_state.accept_watermark(77)

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == 3

    def test_a_rejected_submit_leaves_the_watermark_alone(self, runtime, submitted) -> None:
        submitted.outcome.accepted = False

        assert submitted.run(3) is False
        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1
        assert runtime._session_state.pending_watermarks == {}

    def test_a_submit_without_an_inbound_seq_advances_immediately(self, runtime, submitted) -> None:
        # 没有 seq 就等不到 inbound_accepted，只能就地推进。
        submitted.outcome.inbound_seq = 0

        submitted.run(3)

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == 3

    def test_a_private_message_never_touches_a_group_watermark(self, runtime, submitted) -> None:
        submitted.run(3, is_dm=True)

        assert runtime._session_state.pending_watermarks == {}
        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1

    def test_a_round_that_carried_no_history_does_not_advance(self, runtime, submitted) -> None:
        # /生图 直达命令不带历史块，那些行要留给下一条正常消息。
        submitted.run(-1)

        assert runtime._session_state.pending_watermarks == {}
        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1

    def test_out_of_order_confirmations_never_pull_the_watermark_back(self, runtime, submitted) -> None:
        """两条 inbound 同时在飞时，inbound_accepted 的到达顺序不受保证。

        先确认较小的那个就会把水位拉回去，那批已经带过的历史会被第二次带出。
        """
        state = runtime._session_state
        state.register_watermark(101, _GROUP_ID, 9)
        state.register_watermark(102, _GROUP_ID, 4)

        state.accept_watermark(101)
        state.accept_watermark(102)

        assert state.get_high_watermark(_GROUP_ID) == 9

    def test_a_preempt_advances_without_waiting(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # preempt 注入的消息会被写进 ConversationStore，但不产生新的 inbound_seq。
        async def _preempt(**kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(runtime, "_client", SimpleNamespace())
        set_live_config(runtime, enable_preempt=True)
        monkeypatch.setattr(runtime, "_try_preempt_running_task", _preempt)

        ok = asyncio.run(runtime._submit_or_preempt_inbound(
            bot=SimpleNamespace(self_id="90000"),
            event=_event(runtime),
            channel="qq_group",
            real_conversation_key=_REAL_KEY,
            stable_conversation_key="conv_abc",
            inbound_text="正文",
            user_text="在吗",
            attachments=[],
            is_dm=False,
            platform_updates={"type": "group", "group_id": _GROUP_ID},
            history_watermark=5,
        ))

        assert ok is True
        assert runtime._session_state.get_high_watermark(_GROUP_ID) == 5


class TestQueueMerge:
    def test_merging_keeps_the_higher_watermark(self, runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # 合并后的文本含两条各自的历史块，取低的那个会让高的那批行第二次被带出去。
        set_live_config(runtime, enable_queue=True)

        def _item(watermark: int):
            return runtime.QueuedInbound(
                matcher=None, bot=None, event=None, channel="qq_group",
                real_conversation_key=_REAL_KEY, stable_conversation_key="conv_abc",
                text="正文", attachments=[], is_dm=False, platform_updates={},
                user_text="在吗", history_watermark=watermark,
            )

        first = _item(4)
        asyncio.run(runtime._enqueue_or_submit_inbound(first))
        asyncio.run(runtime._enqueue_or_submit_inbound(_item(9)))

        assert first.history_watermark == 9


class TestResetPaths:
    @pytest.fixture()
    def carried_history(self, runtime):
        runtime._real_conversation_keys["conv_abc"] = _REAL_KEY
        for line in ("a", "b"):
            runtime._append_group_history(_GROUP_ID, line)
        runtime._session_state.advance_watermark(_GROUP_ID, 2)

    def _reset(self, runtime, reason: str) -> None:
        asyncio.run(runtime.TangQiuCallbacks().on_context_reset("conv_abc", reason, []))

    def test_clear_drops_both_the_cache_and_the_watermark(self, runtime, carried_history) -> None:
        self._reset(runtime, "clear")

        assert _GROUP_ID not in runtime._group_history
        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1

    def test_compact_keeps_the_cache_and_resends_it(self, runtime, carried_history) -> None:
        # compact 把早期原文换成摘要，重发这 20 行是把丢掉的近期细节补回来最便宜的办法。
        self._reset(runtime, "compact")

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1
        assert _history_block(_build(runtime)[0]) == ["a", "b"]

    def test_a_private_conversation_reset_is_a_no_op(self, runtime) -> None:
        runtime._real_conversation_keys["conv_dm"] = "qq_private:10001"
        runtime._append_group_history(_GROUP_ID, "a")
        runtime._session_state.advance_watermark(_GROUP_ID, 1)

        asyncio.run(runtime.TangQiuCallbacks().on_context_reset("conv_dm", "clear", []))

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == 1

    def test_an_engine_restart_resets_every_group(self, runtime) -> None:
        for gid in (_GROUP_ID, _GROUP_ID + 1):
            runtime._append_group_history(gid, "a")
            runtime._session_state.advance_watermark(gid, 1)

        asyncio.run(runtime.TangQiuCallbacks().on_engine_restarted({}))

        assert runtime._session_state.get_high_watermark(_GROUP_ID) == -1
        assert runtime._session_state.get_high_watermark(_GROUP_ID + 1) == -1


class TestSdkResetPathDoesNotCoverQq:
    def test_the_sdk_cannot_parse_a_qq_conversation_key(self) -> None:
        """SDK 的 context_reset 处理按 `prefix:channel_id` 取群号，QQ 用的是 sha256 哈希。

        这条不通，才需要 onebot 自己在 on_context_reset 里重置 —— 谁看到两处都在重置
        想删掉一处时，这个测试会指出该留哪一处。
        """
        from clonoth_sdk.event_router import EventRouter

        state = SessionState()
        state.advance_watermark(_GROUP_ID, 5)

        router = EventRouter.__new__(EventRouter)
        router._state = state
        router._cb = SimpleNamespace(on_context_reset=_async_noop)

        asyncio.run(router._handle_context_reset(SimpleNamespace(payload={
            "conversation_key": "conv_bc3f12b0621298e191a49fa7",
            "reason": "compact",
        })))

        assert state.get_high_watermark(_GROUP_ID) == 5


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None
