from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from supervisor.execution.service import ExecutionService
from supervisor.reminders.service import ReminderService


@pytest.mark.parametrize("service_type", [ExecutionService, ReminderService])
def test_pause_and_resume_during_busy_tick_keep_worker_running(tmp_path, service_type):
    service = service_type(SimpleNamespace(workspace_root=tmp_path))
    started, finish, repeated = threading.Event(), threading.Event(), threading.Event()
    count = 0

    def tick():
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            finish.wait(5)
        else:
            repeated.set()

    service.tick = tick
    service.start()
    try:
        assert started.wait(3)
        worker = service._thread
        service.pause()
        assert not service._stop.is_set()
        assert worker.is_alive()
        service.resume()
        assert service._thread is worker
        assert not service._stop.is_set()
        finish.set()
        assert repeated.wait(3)
        assert worker.is_alive()
    finally:
        finish.set()
        service.close()
    assert not worker.is_alive()


@pytest.mark.parametrize("service_type", [ExecutionService, ReminderService])
def test_paused_loop_does_not_start_new_tick_until_resumed(tmp_path, service_type):
    service = service_type(SimpleNamespace(workspace_root=tmp_path))
    entered = threading.Event()
    service.tick = entered.set
    service.pause()
    service.start()
    try:
        assert not entered.wait(1.1)
        assert service._thread.is_alive()
        service.resume()
        assert entered.wait(2)
    finally:
        service.close()
