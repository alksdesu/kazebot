"""要拿全局锁的端点在线程池里拿：路由占着锁的时候，事件循环上的别的请求照常处理。"""
from __future__ import annotations

import asyncio
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _lock_probe import ThreadRecordingLock  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskStatus  # noqa: E402

CONV = "test:lock-threads"
GROUP = "qq_group:lock-threads"
_TOKEN = "lock-threads-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="lock-threads"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _probe(state: SupervisorState) -> ThreadRecordingLock:
    lock = ThreadRecordingLock(state._lock)
    state._lock = lock
    return lock


def _intent_task(state: SupervisorState) -> Task | None:
    # 任务是在线程池里建的：先拷一份再挑，别在另一个线程往里加的时候遍历
    return next((task for task in list(state.tasks.values()) if task.input.get("_reply_intent")), None)


def _answer(state: SupervisorState, task_id: str) -> None:
    """替 engine worker 领走判定任务，再交回答案。"""
    with state._lock:
        task = state.tasks[task_id]
        task.status = TaskStatus.running
        task.worker_id = "worker-1"
    state.complete_task(
        task_id=task_id, worker_id="worker-1", result={"action": "finish", "result": {"text": "yes|在叫它"}},
    )


def test_reads_that_take_the_lock_run_in_the_threadpool(tmp_path: Path) -> None:
    state = _state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key=CONV)
    app = _app(state, tmp_path)
    lock = _probe(state)
    paths = (f"/v1/sessions/{session_id}/context_window", "/v1/admin/tasks/active", "/v1/admin/runtime/status")

    async def run() -> tuple[int, list[int]]:
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=_AUTH,
        ) as client:
            statuses = [(await client.get(path)).status_code for path in paths]
        return loop_thread, statuses

    loop_thread, statuses = asyncio.run(run())

    assert statuses == [200, 200, 200]
    assert len(lock.threads) >= len(paths) and loop_thread not in lock.threads


@pytest.mark.parametrize("answered", [True, False], ids=["answered", "timed-out"])
def test_a_reply_intent_judge_takes_the_lock_off_the_event_loop(tmp_path: Path, answered: bool) -> None:
    state = _state(tmp_path)
    app = _app(state, tmp_path)
    lock = _probe(state)
    body = {"conversation_key": GROUP, "text": "bot 在吗", "timeout_sec": 10 if answered else 1}

    async def run() -> tuple[int, httpx.Response]:
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=_AUTH,
        ) as client:
            judge = asyncio.create_task(client.post("/v1/intent/judge", json=body))
            if answered:
                task = None
                for _ in range(500):
                    task = _intent_task(state)
                    if task is not None:
                        break
                    await asyncio.sleep(0.01)
                assert task is not None
                await asyncio.to_thread(_answer, state, task.task_id)
            response = await asyncio.wait_for(judge, 15)
        return loop_thread, response

    loop_thread, response = asyncio.run(run())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["agreed"] is answered
    assert payload["error"] == ("" if answered else "timeout")
    assert state.reply_intent.inflight() == 0 and not state.reply_intent.has_conversation(GROUP)
    if not answered:
        judged = state.tasks[payload["task_id"]]
        assert judged.status == TaskStatus.cancelled or judged.cancel_requested
    assert lock.threads and loop_thread not in lock.threads
