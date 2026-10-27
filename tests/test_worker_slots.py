"""engine 的并发名额：等人工审批的任务不占名额、恢复时可以短暂超出，连同它们在内的总数有上限；主循环领任务时把空位报给 supervisor。"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.worker_slots import MAX_ACTIVE_CEILING, SlotCounts, WorkerSlots, active_limit  # noqa: E402
from toolbox.approval_wait import report_approval_waits_to, waiting_for_approval  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_WAIT = 5.0


async def _until(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), _WAIT)


class TestActiveLimit:
    def test_unset_means_twice_max_workers(self) -> None:
        assert active_limit(4, 0) == 8
        assert active_limit(1, 0) == 2

    def test_never_below_max_workers_and_never_above_the_ceiling(self) -> None:
        assert active_limit(4, 2) == 4
        assert active_limit(4, 6) == 6
        assert active_limit(32, 0) == MAX_ACTIVE_CEILING
        assert active_limit(4, 1000) == MAX_ACTIVE_CEILING
        assert active_limit(0, 0) == 2

    def test_the_worker_reads_it_as_its_hard_cap(self) -> None:
        slots = WorkerSlots(max_workers=3)
        assert (slots.max_workers, slots.max_active) == (3, 6)
        assert slots.claim_params() == {"free_slots": 3, "capacity": 3}


class TestSlots:
    def test_a_task_waiting_for_approval_frees_its_slot_until_the_answer_comes(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=2, max_active=4)
            parked, decided, resumed, finish = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def tool_needs_approval() -> None:
                with waiting_for_approval():
                    parked.set()
                    await decided.wait()
                resumed.set()
                await finish.wait()

            first = slots.start(tool_needs_approval(), task_id="t1", lane="interactive")
            second = slots.start(finish.wait(), task_id="t2", lane="background")
            assert slots.free_slots() == 0

            await _until(parked)
            assert slots.counts() == SlotCounts(running=1, parked=1)
            assert slots.claim_params() == {"free_slots": 1, "capacity": 2}

            decided.set()
            await _until(resumed)
            assert slots.counts() == SlotCounts(running=2, parked=0)
            assert slots.free_slots() == 0

            finish.set()
            await asyncio.gather(first, second)
            assert slots.counts() == SlotCounts(running=0, parked=0)
            assert slots.free_slots() == 2

        asyncio.run(scenario())

    def test_parked_tasks_still_count_toward_the_hard_cap(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=2, max_active=3)
            finish = asyncio.Event()
            parked = [asyncio.Event(), asyncio.Event()]

            async def waits(flag: asyncio.Event) -> None:
                with waiting_for_approval():
                    flag.set()
                    await finish.wait()

            tasks = [slots.start(waits(flag), task_id=f"p{index}") for index, flag in enumerate(parked)]
            for flag in parked:
                await _until(flag)
            assert slots.free_slots() == 1

            tasks.append(slots.start(finish.wait(), task_id="r1"))
            assert slots.counts() == SlotCounts(running=1, parked=2)
            assert slots.free_slots() == 0

            finish.set()
            await asyncio.gather(*tasks)
            assert slots.free_slots() == 2

        asyncio.run(scenario())

    def test_a_task_back_from_approval_runs_on_even_when_the_worker_is_full(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=1, max_active=2)
            parked, decided, resumed, finish = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def tool_needs_approval() -> None:
                with waiting_for_approval():
                    parked.set()
                    await decided.wait()
                resumed.set()
                await finish.wait()

            waiting = slots.start(tool_needs_approval(), task_id="t1")
            await _until(parked)
            other = slots.start(finish.wait(), task_id="t2")
            assert slots.free_slots() == 0

            decided.set()
            await _until(resumed)
            assert slots.counts() == SlotCounts(running=2, parked=0)
            assert slots.free_slots() == 0

            finish.set()
            await asyncio.gather(waiting, other)
            assert slots.free_slots() == 1

        asyncio.run(scenario())

    def test_parallel_waits_in_one_task_park_it_once_until_the_last_answer(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=2, max_active=4)
            started = [asyncio.Event(), asyncio.Event()]
            answers = [asyncio.Event(), asyncio.Event()]

            async def one_tool(index: int) -> None:
                with waiting_for_approval():
                    started[index].set()
                    await answers[index].wait()

            async def two_tools() -> None:
                await asyncio.gather(one_tool(0), one_tool(1))

            task = slots.start(two_tools(), task_id="t1")
            for flag in started:
                await _until(flag)
            assert slots.counts() == SlotCounts(running=0, parked=1)

            answers[0].set()
            await asyncio.sleep(0.05)
            assert slots.counts() == SlotCounts(running=0, parked=1)

            answers[1].set()
            await task
            assert slots.counts() == SlotCounts(running=0, parked=0)

        asyncio.run(scenario())

    def test_a_wait_reported_after_the_task_ended_is_ignored(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=1, max_active=2)
            go, entered, leave = asyncio.Event(), asyncio.Event(), asyncio.Event()
            detached: list[asyncio.Task[Any]] = []

            async def background_tool() -> None:
                await go.wait()
                with waiting_for_approval():
                    entered.set()
                    await leave.wait()

            async def task_spawning_a_background_tool() -> None:
                detached.append(asyncio.create_task(background_tool()))

            await slots.start(task_spawning_a_background_tool(), task_id="t1")
            assert slots.counts() == SlotCounts(running=0, parked=0)

            go.set()
            await _until(entered)
            assert slots.counts() == SlotCounts(running=0, parked=0)
            assert slots.free_slots() == 1

            leave.set()
            await detached[0]
            assert slots.counts() == SlotCounts(running=0, parked=0)
            assert slots.free_slots() == 1

        asyncio.run(scenario())

    def test_a_failing_task_still_hands_its_slot_back(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=1)

            async def boom() -> None:
                with waiting_for_approval():
                    raise RuntimeError("supervisor went away")

            task = slots.start(boom(), task_id="t1")
            with pytest.raises(RuntimeError):
                await task
            assert slots.counts() == SlotCounts(running=0, parked=0)
            assert slots.free_slots() == 1

        asyncio.run(scenario())

    @pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
    def test_a_task_cancelled_before_it_ran_still_hands_its_slot_back(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=1)
            task = slots.start(asyncio.sleep(10), task_id="t1")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert slots.free_slots() == 1

        asyncio.run(scenario())


class TestApprovalWaitReporting:
    def test_the_listener_hears_the_end_even_when_the_wait_fails(self) -> None:
        heard: list[str] = []

        class Listener:
            def approval_wait_started(self) -> None:
                heard.append("start")

            def approval_wait_ended(self) -> None:
                heard.append("end")

        with report_approval_waits_to(Listener()):
            with pytest.raises(RuntimeError):
                with waiting_for_approval():
                    raise RuntimeError("boom")
        assert heard == ["start", "end"]

        with waiting_for_approval():
            pass
        assert heard == ["start", "end"]

    def test_waiting_outside_any_worker_task_is_a_no_op(self) -> None:
        with waiting_for_approval():
            pass

    def test_work_detached_from_its_task_reports_to_nobody(self) -> None:
        async def scenario() -> None:
            slots = WorkerSlots(max_workers=1, max_active=2)
            inside, leave = asyncio.Event(), asyncio.Event()

            async def detached_tool() -> None:
                with waiting_for_approval():
                    inside.set()
                    await leave.wait()

            async def task_body() -> None:
                with report_approval_waits_to(None):
                    tool = asyncio.create_task(detached_tool())
                await _until(inside)
                assert slots.counts() == SlotCounts(running=1, parked=0)
                leave.set()
                await tool

            await slots.start(task_body(), task_id="t1")
            assert slots.free_slots() == 1

        asyncio.run(scenario())


class _Response:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        return None


class TestToolContextReportsTheWait:
    def test_wait_for_approval_parks_the_task_while_it_polls(self, tmp_path: Path) -> None:
        seen: list[tuple[SlotCounts, int]] = []

        async def scenario() -> tuple[WorkerSlots, dict[str, Any]]:
            slots = WorkerSlots(max_workers=1, max_active=2)

            class Supervisor:
                polls = 0

                async def get(self, url: str, **_kwargs: Any) -> _Response:
                    if url.endswith("/cancelled"):
                        return _Response({"cancelled": False})
                    Supervisor.polls += 1
                    seen.append((slots.counts(), slots.free_slots()))
                    return _Response({"approval_id": "ap-1", "status": "pending" if Supervisor.polls < 3 else "allowed"})

                async def post(self, url: str, **_kwargs: Any) -> _Response:
                    return _Response({"ok": True})

            ctx = ToolContext(
                supervisor_url="http://supervisor", session_id="s", run_id="r", worker_id="w",
                workspace_root=tmp_path, http=Supervisor(), registry=None, approval_poll_interval_sec=0.01,
            )
            outcome: dict[str, Any] = {}

            async def tool() -> None:
                outcome.update(await ctx.wait_for_approval("ap-1"))

            await slots.start(tool(), task_id="t1")
            return slots, outcome

        slots, outcome = asyncio.run(scenario())

        assert outcome["status"] == "allowed"
        assert len(seen) == 3
        assert all(counts == SlotCounts(running=0, parked=1) and free == 1 for counts, free in seen)
        assert slots.counts() == SlotCounts(running=0, parked=0)


@dataclass
class _Script:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    parked: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: asyncio.Event = field(default_factory=asyncio.Event)
    commands: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)


class TestWorkerLoop:
    def test_the_loop_reports_its_free_slots_and_claims_past_parked_tasks_up_to_the_hard_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from engine import runner

        claims: list[dict[str, Any]] = []
        queue = ["t1", "t2", "t3", "t4"]

        class FakeSupervisor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeSupervisor":
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def get(self, url: str, **kwargs: Any) -> SimpleNamespace:
                if url.endswith("/v1/tasks/next"):
                    claims.append(dict(kwargs.get("params") or {}))
                    if queue:
                        item = {"task_id": queue.pop(0), "lane": "interactive"}
                        return SimpleNamespace(status_code=200, json=lambda: item)
                    return SimpleNamespace(status_code=204, json=lambda: {})
                return SimpleNamespace(status_code=404, json=lambda: {})

            async def post(self, url: str, **kwargs: Any) -> SimpleNamespace:
                return SimpleNamespace(status_code=200, json=lambda: {})

        class FakeTools:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def load_mcp_tools(self) -> int:
                return 0

        async def ready(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        monkeypatch.setattr(runner.httpx, "AsyncClient", FakeSupervisor)
        monkeypatch.setattr(runner, "create_llm_http_client", lambda _timeout: FakeSupervisor())
        monkeypatch.setattr(runner, "ToolRegistry", FakeTools)
        monkeypatch.setattr(runner, "wait_supervisor", ready)
        monkeypatch.setattr(runner, "_register_engine", ready)
        monkeypatch.setattr(runner, "_flush_outbox", ready)
        monkeypatch.setattr(runner, "load_runtime_config", lambda _root: {
            "engine": {"max_workers": 2, "max_active_tasks": 3, "poll_interval_sec": 0.1},
        })

        async def scenario() -> None:
            scripts = {task_id: _Script() for task_id in queue}

            async def handle(http: Any, llm_http: Any, url: str, root: Path, registry: Any, item: dict, wid: str) -> None:
                script = scripts[item["task_id"]]
                script.started.set()
                while True:
                    command = await script.commands.get()
                    if command == "finish":
                        return
                    if command == "park":
                        with waiting_for_approval():
                            script.parked.set()
                            while await script.commands.get() != "unpark":
                                pass
                        script.resumed.set()

            monkeypatch.setattr(runner, "_handle_task", handle)

            async def quiet_for(seconds: float) -> int:
                before = len(claims)
                await asyncio.sleep(seconds)
                return len(claims) - before

            worker = asyncio.create_task(runner.worker_loop(supervisor_url="http://supervisor", workspace_root=tmp_path))
            try:
                await _until(scripts["t1"].started)
                await _until(scripts["t2"].started)
                assert [(claim["free_slots"], claim["capacity"]) for claim in claims] == [(2, 2), (1, 2)]
                assert await quiet_for(0.3) == 0

                # 等审批的那个让出名额：再领一个，报的空位是 1
                await scripts["t1"].commands.put("park")
                await _until(scripts["t3"].started)
                assert claims[2]["free_slots"] == 1

                # 审批有了结论，在跑的变成 3 个、超过 max_workers 也照样往下跑，只是不再领
                await scripts["t1"].commands.put("unpark")
                await _until(scripts["t1"].resumed)
                assert await quiet_for(0.3) == 0

                # 在跑的只剩 1 个，可连同等审批的已经到了总数上限 3：还是不领
                await scripts["t2"].commands.put("park")
                await scripts["t3"].commands.put("park")
                await _until(scripts["t2"].parked)
                await _until(scripts["t3"].parked)
                assert await quiet_for(0.3) == 0
                assert queue == ["t4"]

                await scripts["t1"].commands.put("finish")
                await _until(scripts["t4"].started)
                assert claims[3]["free_slots"] == 1 and claims[3]["capacity"] == 2

                for task_id in ("t2", "t3"):
                    await scripts[task_id].commands.put("unpark")
                    await scripts[task_id].commands.put("finish")
                await scripts["t4"].commands.put("finish")
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())
