from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api
import supervisor.api as api
from supervisor.config_store import ConfigStore
from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import TaskKind, TaskStatus


@pytest.fixture()
def app_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-token")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = api.create_app(
        state=state, process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    return app, state


def append(log, text, event_type="outbound_message", **kwargs):
    return log.append(
        session_id="session", component="supervisor", type_=event_type,
        payload={"text": text, "conversation_key": "qq_group:test"}, **kwargs,
    )


class Socket:
    def __init__(self, initial, *, stop, during_handshake=None, token="test-token"):
        self.initial = initial
        self.stop = stop
        self.during_handshake = during_handshake
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.query_params = {}
        self.client = SimpleNamespace(host="test")
        self.sent = []
        self.closed = None
        self.received = False

    async def accept(self):
        return None

    async def receive_text(self):
        if self.received:
            await asyncio.Event().wait()
        self.received = True
        if self.during_handshake:
            self.during_handshake()
        return json.dumps(self.initial)

    async def send_text(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        if self.stop(frame):
            raise WebSocketDisconnect(1000)

    async def close(self, code, reason):
        self.closed = (code, reason)


def run_socket(app, socket):
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/ws")
    asyncio.run(endpoint(socket))
    return socket.sent


def test_replay_is_paged_and_never_replays_approvals(app_state, monkeypatch):
    app, state = app_state
    append(state.eventlog, "one")
    append(state.eventlog, "approval", "approval_requested")
    append(state.eventlog, "two", "intermediate_reply")
    last = append(state.eventlog, "three")
    monkeypatch.setattr(api, "_OUTBOUND_REPLAY_PAGE_SIZE", 1)
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": 0},
        stop=lambda frame: frame.get("type") == "outbound_checkpoint" and frame["seq"] == last["seq"],
    ))
    assert [f["payload"]["text"] for f in frames if "payload" in f] == ["one", "two", "three"]
    assert sum(f["type"] == "outbound_checkpoint" for f in frames) == 3
    assert state.eventlog._global_subscribers == []


def test_first_connection_skips_history_but_captures_handshake_window(app_state):
    app, state = app_state
    old = append(state.eventlog, "old")
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": None},
        during_handshake=lambda: append(state.eventlog, "during-handshake"),
        stop=lambda frame: frame.get("type") == "outbound_message",
    ))
    assert frames[1] == {"type": "outbound_checkpoint", "seq": old["seq"]}
    assert [f["payload"]["text"] for f in frames if "payload" in f] == ["during-handshake"]


def test_reconnect_replays_offline_reply_then_live_reply_once(app_state, monkeypatch):
    app, state = app_state
    before = append(state.eventlog, "before-disconnect")
    append(state.eventlog, "while-offline")
    original = api._outbound_replay_page

    def concurrent_append(*args):
        page = original(*args)
        append(state.eventlog, "during-replay")
        return page

    monkeypatch.setattr(api, "_outbound_replay_page", concurrent_append)
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": before["seq"]},
        stop=lambda frame: (frame.get("payload") or {}).get("text") == "during-replay",
    ))
    assert [f["payload"]["text"] for f in frames if "payload" in f] == ["while-offline", "during-replay"]


def test_replay_requires_token_while_web_remains_live_only(app_state):
    app, state = app_state
    append(state.eventlog, "old")
    unauthorized = Socket({"replay_outbound": True, "last_seq": 0}, stop=lambda _: False, token="")
    run_socket(app, unauthorized)
    assert unauthorized.closed[0] == 1008
    assert unauthorized.sent == []
    frames = run_socket(app, Socket(
        {"last_seq": 0}, token="",
        during_handshake=lambda: append(state.eventlog, "new"),
        stop=lambda frame: frame["type"] == "outbound_message",
    ))
    assert [frame["payload"]["text"] for frame in frames] == ["new"]


def test_trimmed_logs_report_gap_and_replay_retained_replies(app_state):
    app, state = app_state
    events = [append(state.eventlog, str(i)) for i in range(5)]
    state.eventlog.path.write_text(
        "".join(json.dumps(event) + "\n" for event in events[3:]), encoding="utf-8",
    )
    state.eventlog = EventLog(state.eventlog.path, run_id="restarted")
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": 1},
        stop=lambda frame: frame["type"] == "outbound_checkpoint",
    ))
    assert frames[1] == {"type": "outbound_replay_gap", "after_seq": 1, "oldest_seq": 4}
    assert [f["payload"]["text"] for f in frames if "payload" in f] == ["3", "4"]


def test_checkpoints_exclude_transient_sequences_across_restart(app_state):
    app, state = app_state
    durable = append(state.eventlog, "durable")
    append(state.eventlog, "accepted", "inbound_accepted", transient=True)
    assert state.eventlog.durable_seq == durable["seq"]
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": None},
        stop=lambda frame: frame["type"] == "outbound_checkpoint",
    ))
    assert frames[-1]["seq"] == durable["seq"]
    state.eventlog = EventLog(state.eventlog.path, run_id="restarted")
    assert state.eventlog.durable_seq == durable["seq"]
    new = append(state.eventlog, "new-after-restart")
    frames = run_socket(app, Socket(
        {"replay_outbound": True, "last_seq": durable["seq"]},
        stop=lambda frame: frame["type"] == "outbound_checkpoint",
    ))
    assert [f["seq"] for f in frames if "payload" in f] == [new["seq"]]


def test_subscription_and_initial_checkpoint_are_atomic(app_state):
    _, state = app_state
    baseline = append(state.eventlog, "baseline")
    started = threading.Event()
    workers = []

    def publish():
        started.set()
        append(state.eventlog, "concurrent")

    class Subscribers(list):
        def append(self, queue):
            super().append(queue)
            worker = threading.Thread(target=publish)
            workers.append(worker)
            worker.start()
            assert started.wait(2)

    state.eventlog._global_subscribers = Subscribers()
    queue, checkpoint = state.eventlog.subscribe_global_with_cursor()
    workers[0].join(timeout=2)
    assert not workers[0].is_alive()
    assert checkpoint == baseline["seq"]
    assert queue.get_nowait()["seq"] > checkpoint


def test_concurrent_publishers_preserve_sequence_for_every_subscriber(app_state, monkeypatch):
    _, state = app_state
    entered = threading.Event()
    release = threading.Event()

    async def exercise():
        global_queue = state.eventlog.subscribe_global()
        session_queue = state.eventlog.subscribe("session")
        publish = global_queue._publish_locked

        def delay_first(event):
            if event["seq"] == 1:
                entered.set()
                assert release.wait(2)
            publish(event)

        monkeypatch.setattr(global_queue, "_publish_locked", delay_first)
        first = threading.Thread(target=lambda: append(state.eventlog, "first"))
        second = threading.Thread(target=lambda: append(state.eventlog, "second"))
        first.start()
        assert await asyncio.to_thread(entered.wait, 2)
        second.start()
        release.set()
        await asyncio.to_thread(first.join, 2)
        await asyncio.to_thread(second.join, 2)
        assert not first.is_alive() and not second.is_alive()
        for queue in (global_queue, session_queue):
            assert [(await asyncio.wait_for(queue.get(), 1))["seq"] for _ in range(2)] == [1, 2]

    asyncio.run(exercise(), debug=True)


def test_worker_thread_can_wake_a_subscriber_created_without_a_loop(app_state):
    _, state = app_state
    queue = state.eventlog.subscribe_global()

    async def exercise():
        pending = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        await asyncio.to_thread(append, state.eventlog, "from-thread")
        assert (await asyncio.wait_for(pending, 1))["payload"]["text"] == "from-thread"

    asyncio.run(exercise(), debug=True)


def test_loop_publisher_cannot_overtake_queued_thread_publication(app_state):
    _, state = app_state

    async def exercise():
        queue = state.eventlog.subscribe_global()
        worker = threading.Thread(target=lambda: append(state.eventlog, "from-thread"))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        append(state.eventlog, "from-loop")
        assert [(await asyncio.wait_for(queue.get(), 1))["seq"] for _ in range(2)] == [1, 2]

    asyncio.run(exercise(), debug=True)


def test_replay_reads_rotated_logs_beyond_the_memory_cache(app_state):
    _, state = app_state
    first = append(state.eventlog, "from-backup")
    path = state.eventlog.path
    path.rename(path.with_name(path.name + ".1"))
    last = append(state.eventlog, "from-current")
    state.eventlog._events = [last]
    page, checkpoint = api._outbound_replay_page(state.eventlog, 0, last["seq"])
    assert [event["seq"] for event in page] == [first["seq"], last["seq"]]
    assert checkpoint == last["seq"]


def post(app, path, content):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(path, content=content)
    return asyncio.run(exercise())


@pytest.mark.parametrize("hints", [[], {"subjects": "invalid"}])
def test_preempt_rejects_invalid_memory_hints(app_state, hints):
    app, _ = app_state
    response = post(app, "/v1/tasks/task/preempt", json.dumps({"memory_hints": hints}))
    assert response.status_code == 422


def test_preempt_forwards_memory_hints_and_optional_consumed_revision(app_state, monkeypatch):
    app, state = app_state
    calls = []
    monkeypatch.setattr(state, "preempt_task", lambda task_id, **kw: calls.append((task_id, kw)) or True)
    monkeypatch.setattr(state, "consume_preempt_message", lambda task_id, **kw: calls.append((task_id, kw)) or {})
    assert post(app, "/v1/tasks/task/preempt", json.dumps({"memory_hints": {"subjects": ["user"]}})).status_code == 200
    assert calls[-1][1]["memory_hints"] == {"subjects": ["user"]}
    assert post(app, "/v1/tasks/task/preempt_consumed", '{"revision":2}').status_code == 200
    assert calls[-1] == ("task", {"expected_revision": 2})
    assert post(app, "/v1/tasks/task/preempt_consumed", "").status_code == 200
    assert calls[-1] == ("task", {})


def test_delete_session_cancels_active_tasks_through_reset(app_state):
    app, state = app_state
    session_id = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:delete")
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id, session_generation=1, kind=TaskKind.node,
            node_id="test", input_data={}, continuation={}, source_inbound_seq=None,
            caller_task_id=None,
        )
        task.status = TaskStatus.running

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/v1/sessions/{session_id}")
            assert response.status_code == 200
            assert response.json() == {"ok": True, "session_id": session_id}
            assert (await client.delete(f"/v1/sessions/{session_id}")).status_code == 404

    asyncio.run(exercise())
    assert task.status == TaskStatus.cancelled
    assert session_id not in state.sessions
