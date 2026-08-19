"""四个曾经只在启动时读一次的键，改完要真的生效。

群历史条数、队列开关/worker 数、转发 Bridge 开关分别绑在 deque 的 maxlen、一批 task 和
一个 HTTP server 上。只把值读新是不够的 —— 值变了而运行期对象没跟着变，界面会显示
「已生效」，实际什么都没发生。
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import sys
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

_ADAPTER = _ROOT / "adapters" / "onebot"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, group_history_max=20, enable_queue=False, enable_forward_bridge=False)
    return module


# ── 群历史条数 ───────────────────────────────────

class TestGroupHistoryCapacity:
    def _seed(self, runtime: Any, group_id: int, count: int) -> None:
        for index in range(count):
            runtime._append_group_history(group_id, f"line-{index}")

    def test_growing_the_cap_rebuilds_the_deque(self, runtime: Any) -> None:
        self._seed(runtime, 1, 5)
        assert runtime._group_history[1].maxlen == 20

        set_live_config(runtime, group_history_max=50)
        assert runtime._reconcile_group_history_capacity() is True

        assert runtime._group_history[1].maxlen == 50

    def test_growing_keeps_every_cached_line(self, runtime: Any) -> None:
        self._seed(runtime, 1, 5)
        before = [entry.text for entry in runtime._group_history[1]]

        set_live_config(runtime, group_history_max=50)
        runtime._reconcile_group_history_capacity()

        assert [entry.text for entry in runtime._group_history[1]] == before

    def test_shrinking_drops_the_oldest_lines(self, runtime: Any) -> None:
        self._seed(runtime, 1, 10)

        set_live_config(runtime, group_history_max=3)
        runtime._reconcile_group_history_capacity()

        assert [entry.text for entry in runtime._group_history[1]] == ["line-7", "line-8", "line-9"]
        assert runtime._group_history[1].maxlen == 3

    def test_shrinking_never_replays_history_the_engine_already_saw(self, runtime: Any) -> None:
        """序号水位和缓存是两份状态，重建 deque 不能把水位一起丢掉。

        丢了水位，下一轮 inbound 会把剩下那几行当新消息重新带一遍，engine 侧的
        durable history 里就多出一份重复。
        """
        self._seed(runtime, 1, 10)
        runtime._session_state = _StubSessionState()
        runtime._session_state.advance_watermark(1, 6)

        set_live_config(runtime, group_history_max=3)
        runtime._reconcile_group_history_capacity()

        assert runtime._group_history_seq[1] == 10
        assert runtime._group_history_watermark(1) == 6
        fresh = [e.text for e in runtime._group_history[1] if e.seq > 6]
        assert fresh == ["line-7", "line-8", "line-9"]

    def test_the_content_record_cache_keeps_a_floor_of_twenty(self, runtime: Any) -> None:
        """转发挑选要看更长的上文，比群历史条数低时也不能跌破 20。"""
        runtime._group_content_records[1].append(object())

        set_live_config(runtime, group_history_max=3)
        runtime._reconcile_group_history_capacity()

        assert runtime._group_content_records[1].maxlen == 20

    def test_an_unchanged_cap_is_a_no_op(self, runtime: Any) -> None:
        self._seed(runtime, 1, 5)
        before = runtime._group_history[1]

        assert runtime._reconcile_group_history_capacity() is False
        assert runtime._group_history[1] is before

    def test_a_group_seen_after_the_change_needs_no_reconcile(self, runtime: Any) -> None:
        """defaultdict 的工厂每次都读当前配置，新群自动是新容量。"""
        set_live_config(runtime, group_history_max=7)

        runtime._append_group_history(99, "hi")

        assert runtime._group_history[99].maxlen == 7

    def test_every_group_is_rebuilt_not_just_the_first(self, runtime: Any) -> None:
        self._seed(runtime, 1, 3)
        self._seed(runtime, 2, 3)

        set_live_config(runtime, group_history_max=4)
        runtime._reconcile_group_history_capacity()

        assert runtime._group_history[1].maxlen == 4
        assert runtime._group_history[2].maxlen == 4


class _StubSessionState:
    def __init__(self) -> None:
        self.last_ctx_seq: dict[int, int] = {}

    def advance_watermark(self, channel_id: int, watermark_seq: int) -> int:
        self.last_ctx_seq[channel_id] = max(self.last_ctx_seq.get(channel_id, -1), watermark_seq)
        return self.last_ctx_seq[channel_id]

    def get_high_watermark(self, channel_id: int) -> int:
        return self.last_ctx_seq.get(channel_id, -1)

    def reset_channel_watermark(self, channel_id: int) -> None:
        self.last_ctx_seq.pop(channel_id, None)


# ── 队列 worker ───────────────────────────────────

def _queued(runtime: Any, key: str) -> Any:
    return runtime.QueuedInbound(
        matcher=None, bot=None, event=None, channel="qq_group",
        real_conversation_key=f"qq_group:{key}", stable_conversation_key=key,
        text="hi", attachments=[], is_dm=False, platform_updates={}, user_text="hi",
    )


class TestQueueWorkers:
    def _record_submits(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        done: list[str] = []

        async def submit(**kwargs: Any) -> bool:
            done.append(kwargs["stable_conversation_key"])
            return True

        monkeypatch.setattr(runtime, "_submit_or_preempt_inbound", submit)
        return done

    def test_enabling_the_queue_starts_the_declared_number_of_workers(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=3, queue_interval=0)

        async def exercise() -> None:
            assert await runtime._reconcile_queue_workers() is True
            await asyncio.sleep(0)
            assert sorted(runtime._qq_queue_tasks) == [0, 1, 2]
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_a_disabled_queue_starts_nothing(self, runtime: Any) -> None:
        async def exercise() -> None:
            assert await runtime._reconcile_queue_workers() is False
            assert runtime._qq_queue_tasks == {}

        asyncio.run(exercise())

    def test_an_aligned_pool_is_a_no_op(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=2, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)

            assert await runtime._reconcile_queue_workers() is False
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_growing_the_pool_only_adds_the_missing_slots(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=2, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)
            kept = dict(runtime._qq_queue_tasks)

            set_live_config(runtime, queue_workers=4)
            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)

            assert sorted(runtime._qq_queue_tasks) == [0, 1, 2, 3]
            assert runtime._qq_queue_tasks[0] is kept[0]
            assert runtime._qq_queue_tasks[1] is kept[1]
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_shrinking_the_pool_lets_the_extras_exit(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=4, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)

            set_live_config(runtime, queue_workers=1)
            await runtime._reconcile_queue_workers()
            await _settle()

            assert await runtime._reconcile_queue_workers() is False
            assert sorted(runtime._qq_queue_tasks) == [0]
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_shrinking_does_not_cancel_a_worker_mid_submit(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """缩容用协作退出而不是 cancel：正在提交的那条消息必须发完。"""
        started = asyncio.Event()
        release = asyncio.Event()
        finished: list[str] = []

        async def submit(**kwargs: Any) -> bool:
            started.set()
            await release.wait()
            finished.append(kwargs["stable_conversation_key"])
            return True

        monkeypatch.setattr(runtime, "_submit_or_preempt_inbound", submit)
        set_live_config(runtime, enable_queue=True, queue_workers=2, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            await runtime._enqueue_or_submit_inbound(_queued(runtime, "conv-a"))
            await started.wait()

            set_live_config(runtime, enable_queue=False)
            await runtime._reconcile_queue_workers()
            release.set()
            await _settle()

            assert finished == ["conv-a"]
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_disabling_the_queue_drains_what_is_already_lined_up(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """关开关不该让排在队里的用户消息无声消失。"""
        done = self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=1, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            for key in ("conv-a", "conv-b", "conv-c"):
                await runtime._enqueue_or_submit_inbound(_queued(runtime, key))

            set_live_config(runtime, enable_queue=False)
            await runtime._reconcile_queue_workers()
            await _settle()

            assert sorted(done) == ["conv-a", "conv-b", "conv-c"]
            assert list(runtime._qq_queue) == []
            # 退出后的 task 留在字典里到下一个 tick 才被摘掉，摘干净也是 reconcile 的活。
            await runtime._reconcile_queue_workers()
            assert runtime._qq_queue_tasks == {}

        asyncio.run(exercise())

    def test_a_dead_worker_is_replaced_at_its_own_slot(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """补位补回原下标，否则会出现两个同下标 worker、少一个真实槽位。"""
        self._record_submits(runtime, monkeypatch)
        set_live_config(runtime, enable_queue=True, queue_workers=3, queue_interval=0)

        async def exercise() -> None:
            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)
            runtime._qq_queue_tasks[1].cancel()
            await _settle()

            await runtime._reconcile_queue_workers()
            await asyncio.sleep(0)

            assert sorted(runtime._qq_queue_tasks) == [0, 1, 2]
            await _stop_workers(runtime)

        asyncio.run(exercise())

    def test_a_worker_reads_its_exit_decision_from_the_live_value(self) -> None:
        """退出判定不能读钉住的快照：这个 task 活得比任何一条消息都长。"""
        source = (_ADAPTER / "__init__.py").read_text(encoding="utf-8")
        function = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_qq_queue_worker_forever"
        )
        pins = [
            node for node in ast.walk(function)
            if isinstance(node, ast.With)
            and any("pinned_live_config" in ast.unparse(item.context_expr) for item in node.items)
        ]
        assert pins, "worker 处理单条消息期间应当钉住配置"
        assert all("_queue_worker_should_exit" not in ast.unparse(pin) for pin in pins)


async def _settle(rounds: int = 12) -> None:
    """让协作退出的 worker 有机会跑到自己的退出点。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _stop_workers(runtime: Any) -> None:
    for task in runtime._qq_queue_tasks.values():
        task.cancel()
    for task in runtime._qq_queue_tasks.values():
        with contextlib.suppress(asyncio.CancelledError):
            await task
    runtime._qq_queue_tasks.clear()


# ── 转发 Bridge ───────────────────────────────────

class TestForwardBridge:
    def _spy(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []

        async def start() -> None:
            calls.append("start")
            runtime._forward_bridge_started = True

        async def stop() -> None:
            calls.append("stop")
            runtime._forward_bridge_started = False

        monkeypatch.setattr(runtime, "_start_forward_bridge", start)
        monkeypatch.setattr(runtime, "_stop_forward_bridge", stop)
        return calls

    def test_enabling_starts_the_server(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(runtime, monkeypatch)
        set_live_config(runtime, enable_forward_bridge=True)

        assert asyncio.run(runtime._reconcile_forward_bridge()) is True
        assert calls == ["start"]

    def test_disabling_stops_a_running_server(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(runtime, monkeypatch)
        runtime._forward_bridge_started = True
        set_live_config(runtime, enable_forward_bridge=False)

        assert asyncio.run(runtime._reconcile_forward_bridge()) is True
        assert calls == ["stop"]

    def test_an_already_running_server_is_left_alone(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(runtime, monkeypatch)
        runtime._forward_bridge_started = True
        set_live_config(runtime, enable_forward_bridge=True)

        assert asyncio.run(runtime._reconcile_forward_bridge()) is False
        assert calls == []

    def test_a_stopped_server_stays_stopped(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(runtime, monkeypatch)
        set_live_config(runtime, enable_forward_bridge=False)

        assert asyncio.run(runtime._reconcile_forward_bridge()) is False
        assert calls == []

    def test_the_starter_itself_still_honours_the_flag(self, runtime: Any) -> None:
        set_live_config(runtime, enable_forward_bridge=False)

        asyncio.run(runtime._start_forward_bridge())

        assert runtime._forward_bridge_started is False


# ── 生效快照公布 ───────────────────────────────────

class TestPublishedState:
    def _read(self, runtime: Any) -> dict[str, Any]:
        return json.loads(runtime._live_state_file().read_text(encoding="utf-8"))

    def test_it_writes_the_effective_values(self, runtime: Any) -> None:
        set_live_config(runtime, allowed_groups=[111], enable_reactions=False)

        runtime._publish_live_state(force=True)

        state = self._read(runtime)
        assert state["values"]["allowed_groups"] == [111]
        assert state["values"]["enable_reactions"] is False

    def test_it_reports_runtime_facts_the_file_cannot_tell(self, runtime: Any) -> None:
        runtime._append_group_history(1, "hi")

        runtime._publish_live_state(force=True)

        runtime_facts = self._read(runtime)["runtime"]
        assert runtime_facts["queue_workers_running"] == 0
        assert runtime_facts["forward_bridge_running"] is False
        assert runtime_facts["group_history_capacities"] == [20]
        assert runtime_facts["volatile"]["cached_groups"] == 1

    def test_it_stamps_when_it_was_published(self, runtime: Any) -> None:
        runtime._publish_live_state(force=True)

        assert self._read(runtime)["published_at"] > 0

    def _count_writes(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        """数真实落盘次数。

        不比 mtime 也不比 published_at：Windows 的时钟精度约 16ms，同一个 tick 里的两次
        写盘看起来完全一样，这类断言会时红时绿。
        """
        writes: list[Any] = []
        real_replace = runtime.os.replace

        def spy(src: Any, dst: Any) -> Any:
            writes.append(dst)
            return real_replace(src, dst)

        monkeypatch.setattr(runtime.os, "replace", spy)
        return writes

    def test_an_unchanged_snapshot_is_not_rewritten(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime._publish_live_state(force=True)
        writes = self._count_writes(runtime, monkeypatch)

        runtime._publish_live_state()

        assert writes == []

    def test_volatile_counters_alone_do_not_trigger_a_rewrite(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """队列长度每时每刻都在动，拿它当写盘条件等于每个 tick 都写一次。"""
        runtime._append_group_history(7, "hi")
        runtime._publish_live_state(force=True)
        writes = self._count_writes(runtime, monkeypatch)

        runtime._qq_queue.append(_queued(runtime, "conv-a"))
        runtime._append_group_history(7, "again")
        runtime._publish_live_state()

        assert writes == []

    def test_a_config_change_triggers_a_rewrite(self, runtime: Any) -> None:
        runtime._publish_live_state(force=True)

        set_live_config(runtime, allowed_groups=[222])
        runtime._publish_live_state()

        assert self._read(runtime)["values"]["allowed_groups"] == [222]

    def test_the_heartbeat_forces_a_rewrite_when_nothing_changed(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """supervisor 靠这个时间戳判断 bot 进程还活着，不能因为配置没变就一直不写。"""
        runtime._publish_live_state(force=True)
        writes = self._count_writes(runtime, monkeypatch)
        monkeypatch.setattr(runtime, "_live_state_published_at", 0.0)

        runtime._publish_live_state()

        assert len(writes) == 1

    def test_it_leaves_no_temp_file_behind(self, runtime: Any) -> None:
        runtime._publish_live_state(force=True)

        path = runtime._live_state_file()
        assert list(path.parent.glob(f"{path.name}.*")) == []

    def test_a_parse_error_is_visible_in_the_published_state(self, runtime: Any) -> None:
        """「文件写坏了，我还在用旧配置」必须传到界面上，否则就是无声降级。"""
        set_live_config(runtime, allowed_groups=[111])
        module = sys.modules[f"{runtime.__name__}.live_config"]
        module.config_path().write_text("channels: [oops\n", encoding="utf-8")
        module.invalidate()

        runtime._publish_live_state(force=True)

        state = self._read(runtime)
        assert "ParserError" in state["parse_error"]
        assert state["values"]["allowed_groups"] == [111]


# ── 端到端：改文件，不重启 ───────────────────────────────────

class TestTheLoopActuallyPicksUpChanges:
    """P1 的交付判据本身：改完 yaml 不重启进程，值和运行期对象都跟着变。

    上面那些测试都是手动调 reconcile，这里让真正的循环去发现改动 —— 循环自己钉住了
    快照、或者根本没在读文件，前面全绿它也不会生效。
    """

    def test_a_yaml_edit_reaches_the_deques_and_the_state_file(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "_LIVE_RECONCILE_INTERVAL_SEC", 0.01)
        runtime._append_group_history(1, "hi")
        assert runtime._group_history[1].maxlen == 20

        async def exercise() -> None:
            loop_task = asyncio.create_task(runtime._live_config_reconcile_forever())
            try:
                await _until(lambda: runtime._live_state_file().exists())
                set_live_config(runtime, group_history_max=6)

                await _until(lambda: runtime._group_history[1].maxlen == 6)
                await _until(
                    lambda: json.loads(runtime._live_state_file().read_text(encoding="utf-8"))
                    ["values"]["group_history_max"] == 6
                )
            finally:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task

        asyncio.run(exercise())

    def test_the_loop_survives_a_failing_reconcile_step(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """某一轮对齐抛异常不能让循环死掉。

        死了以后所有热载键都停在那一刻，而且没有任何征兆 —— 文件改得动、`/qq/state`
        照旧有内容（只是不再更新），运营者会以为配置生效了。
        """
        monkeypatch.setattr(runtime, "_LIVE_RECONCILE_INTERVAL_SEC", 0.01)
        runtime._append_group_history(1, "hi")
        calls: list[int] = []
        real = runtime._reconcile_live_config

        async def flaky() -> bool:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("对齐这一轮炸了")
            return await real()

        monkeypatch.setattr(runtime, "_reconcile_live_config", flaky)

        async def exercise() -> None:
            loop_task = asyncio.create_task(runtime._live_config_reconcile_forever())
            try:
                await _until(lambda: len(calls) >= 1)
                assert not loop_task.done()

                set_live_config(runtime, group_history_max=4)
                await _until(lambda: runtime._group_history[1].maxlen == 4)
            finally:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task

        asyncio.run(exercise())

    def test_the_loop_survives_a_broken_file(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """写坏 yaml 不能让对齐循环死掉 —— 死了以后修好文件也不会再生效。"""
        monkeypatch.setattr(runtime, "_LIVE_RECONCILE_INTERVAL_SEC", 0.01)
        module = sys.modules[f"{runtime.__name__}.live_config"]
        set_live_config(runtime, group_history_max=6)
        runtime._append_group_history(1, "hi")

        async def exercise() -> None:
            loop_task = asyncio.create_task(runtime._live_config_reconcile_forever())
            try:
                await _until(lambda: runtime._group_history[1].maxlen == 6)

                module.config_path().write_text("queue: [oops\n", encoding="utf-8")
                module.invalidate()
                await asyncio.sleep(0.05)
                assert not loop_task.done()

                set_live_config(runtime, group_history_max=9)
                await _until(lambda: runtime._group_history[1].maxlen == 9)
            finally:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task

        asyncio.run(exercise())


async def _until(predicate: Any, *, timeout: float = 3.0) -> None:
    """等条件成立。轮询而不是固定 sleep，慢机器上不会假红。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        assert asyncio.get_running_loop().time() < deadline, "等超时了，改动没被对齐循环发现"
        await asyncio.sleep(0.01)


# ── 接线 ───────────────────────────────────

class TestWiring:
    def _source(self) -> str:
        return (_ADAPTER / "__init__.py").read_text(encoding="utf-8")

    def _function(self, name: str) -> ast.AST:
        return next(
            node for node in ast.walk(ast.parse(self._source()))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    _ENTRY_POINTS = (
        "_allowed_group_rule",
        "_agent_group_rule",
        "_private_message_rule",
        "_handle_group_upload_notice",
        "_forward_bridge_http_handler",
    )

    def test_every_entry_point_pins_a_snapshot(self) -> None:
        offenders = [
            name for name in self._ENTRY_POINTS
            if "refresh_live_config" not in ast.unparse(self._function(name))
        ]

        assert offenders == [], f"这些入口没有钉住配置快照: {offenders}"

    def test_every_reconciled_key_has_someone_acting_on_it(self, runtime: Any) -> None:
        """标成 reconciled 却没人重建运行期对象 = 改了配置界面说生效、实际没有。"""
        body = "\n".join(
            ast.unparse(self._function(name)) for name in (
                "_reconcile_group_history_capacity",
                # 容量由它算，重建函数每次都重新调它；下一条测试盯住这条调用不断。
                "_group_history_capacity",
                "_reconcile_queue_workers",
                "_reconcile_forward_bridge",
            )
        )
        missing = [name for name in runtime.RECONCILED_KEYS if f"live.{name}" not in body]

        assert missing == []

    def test_history_capacity_is_recomputed_on_every_reconcile(self) -> None:
        """容量算在 helper 里，重建函数必须每次重算——缓存住就等于改了不生效。"""
        rebuild = ast.unparse(self._function("_reconcile_group_history_capacity"))

        assert "_group_history_capacity()" in rebuild

    def test_startup_builds_its_runtime_objects_through_the_reconciler(self) -> None:
        """启动和运行期改配置必须走同一条路径，否则两侧会漂移。"""
        startup = ast.unparse(self._function("_startup"))

        assert "_reconcile_live_config" in startup
        assert "_qq_queue_worker_forever" not in startup

    def test_the_reconcile_loop_does_not_pin_a_snapshot(self) -> None:
        """钉住以后这个 task 会一直拿着启动那一刻的配置，于是永远认为无事可做。"""
        loop = ast.unparse(self._function("_live_config_reconcile_forever"))

        assert "refresh_live_config" not in loop
        assert "pinned_live_config" not in loop

    def test_shutdown_stops_the_reconcile_loop(self) -> None:
        shutdown = ast.unparse(self._function("_shutdown"))

        assert "_live_reconcile_task" in shutdown and ".cancel()" in shutdown
