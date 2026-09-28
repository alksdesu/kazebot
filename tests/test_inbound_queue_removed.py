"""inbound 拉取队列已经删干净了。

engine 从来没走过 /v1/inbound/next：inbound 事件一到，supervisor 直接建入口 task。留着那套
租约、游标和 ack 只会让人以为还有第二条投递路径。旧工作区的 events.jsonl 里仍有
inbound_processed 事件，回放时必须当没看见，不能炸。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

import supervisor.types as supervisor_types  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="inbound-removed-test"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _app(workspace: Path):
    return create_app(
        state=_make_state(workspace),
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def test_the_inbound_work_queue_endpoints_are_gone(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def _call() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            nxt = await client.get("/v1/inbound/next", params={"worker_id": "w-1"})
            ack = await client.post("/v1/inbound/7/ack", json={"worker_id": "w-1"})
            return nxt, ack

    nxt, ack = asyncio.run(_call())

    assert nxt.status_code in (404, 405)
    assert ack.status_code in (404, 405)


def test_the_inbound_post_endpoint_still_exists(tmp_path: Path) -> None:
    """删的是拉取侧；投递侧还在，否则上面两条断言就没有意义了。"""
    paths = {getattr(route, "path", "") for route in _app(tmp_path).routes}

    assert "/v1/inbound" in paths
    assert "/v1/inbound/next" not in paths
    assert "/v1/inbound/{inbound_seq}/ack" not in paths


@pytest.mark.parametrize("name", ["InboundWorkItem", "InboundAckIn", "InboundAckOut"])
def test_the_work_queue_models_are_gone(name: str) -> None:
    assert not hasattr(supervisor_types, name)


@pytest.mark.parametrize("name", [
    "assign_next_inbound",
    "ack_inbound",
    "_advance_inbound_cursor",
    "_apply_inbound_processed",
])
def test_the_work_queue_methods_are_gone(name: str) -> None:
    assert not hasattr(SupervisorState, name)


@pytest.mark.parametrize("attr", [
    "_inbound_order",
    "_inbound_processed",
    "_inbound_cursor",
    "_InboundLease",
    "_inbound_leases",
])
def test_the_work_queue_state_fields_are_gone(tmp_path: Path, attr: str) -> None:
    assert not hasattr(_make_state(tmp_path), attr)


@pytest.mark.parametrize("attr", ["_inbound_events", "_inbound_routed"])
def test_the_still_used_inbound_records_survive(tmp_path: Path, attr: str) -> None:
    assert hasattr(_make_state(tmp_path), attr)


def test_replaying_a_log_with_old_inbound_processed_events_still_loads(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    events = [
        {
            "seq": 1,
            "type": "session_created",
            "session_id": "s-1",
            "payload": {"session_id": "s-1", "channel": "cli", "conversation_key": "cli:old"},
        },
        {
            "seq": 2,
            "type": "inbound_message",
            "session_id": "s-1",
            "payload": {"conversation_key": "cli:old", "channel": "cli", "text": "hello"},
        },
        {
            "seq": 3,
            "type": "inbound_processed",
            "session_id": "__system__",
            "payload": {"inbound_seq": 2, "worker_id": "w-1", "ts": "2026-01-01T00:00:00+00:00"},
        },
        {
            "seq": 4,
            "type": "outbound_message",
            "session_id": "s-1",
            "payload": {"text": "hi", "delivery_id": "d-1"},
        },
    ]

    state.rebuild_from_events(events)

    assert state.conversation_map["cli:old"] == "s-1"
    assert 2 in state._inbound_events
