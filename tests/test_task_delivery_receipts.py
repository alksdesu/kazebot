"""投递回执只跟随平台结果，不改变发送重试或任务归属。"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clonoth_sdk.types import DeliveryContext
from _onebot_harness import load_runtime, set_live_config


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, sticker_send_probability=0.0)
    yield module
    module.state._adapter.outbound_idempotency.close()


def receipts_module(runtime):
    return importlib.import_module(runtime.sending.__package__ + ".task_receipts")


@pytest.mark.parametrize("outcome", ["success", "failed", "unknown"])
def test_send_outcome_is_reported_without_contents_or_credentials(runtime, outcome):
    module = receipts_module(runtime)
    events = []
    async def record(task_id, row): events.append((task_id, row))
    runtime.state._adapter.client = SimpleNamespace(report_task_delivery=record)
    from adapters._shared.send_guard import SendNotStartedError, AmbiguousAckError
    async def run():
        guard = runtime.state._adapter.outbound_idempotency
        claim = await guard.begin("send-content-secret")
        async def send():
            if outcome == "failed": raise SendNotStartedError("not connected")
            if outcome == "unknown": raise AmbiguousAckError("ack lost")
            return "message-1"
        kwargs = dict(context=DeliveryContext(task_id="task-a"), target={"type": "private", "user_id": "10001"}, kind="file")
        if outcome == "success":
            assert await module.claimed_send(guard, claim, send, **kwargs) == "message-1"
        else:
            with pytest.raises((SendNotStartedError, AmbiguousAckError)):
                await module.claimed_send(guard, claim, send, **kwargs)
    asyncio.run(run())
    assert [row[1]["status"] for row in events] == ["pending", "sent" if outcome == "success" else outcome]
    assert all(task_id == "task-a" for task_id, _ in events)
    assert "send-content-secret" not in json.dumps(events)
    assert '"10001"' not in json.dumps(events)


def test_a_lost_supervisor_receipt_does_not_resend_a_file(runtime):
    module = receipts_module(runtime)
    runtime.state._adapter.client = SimpleNamespace(report_task_delivery=AsyncMock(side_effect=RuntimeError("offline")))
    async def run():
        guard = runtime.state._adapter.outbound_idempotency
        claim = await guard.begin("file-key")
        send = AsyncMock(return_value="sent")
        result = await module.claimed_send(guard, claim, send, context=DeliveryContext(task_id="task"),
                                          target={"type": "private", "user_id": "10001"}, kind="file")
        assert result == "sent" and send.await_count == 1
    asyncio.run(run())


def test_real_file_send_and_replay_report_one_logical_delivery(runtime, tmp_path):
    events = []
    async def record(task_id, row): events.append((task_id, row))
    runtime.state._adapter.client = SimpleNamespace(report_task_delivery=record)
    bot = SimpleNamespace(self_id="1000000001", call_api=AsyncMock(return_value={}))
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    async def run():
        for _ in range(2):
            await runtime.outbound_attachments._send_attachment_path(
                bot, {"type": "private", "user_id": "10001"}, path,
                send_context=DeliveryContext(task_id="task-file", idempotency_key="delivery-1"),
            )
    asyncio.run(run())
    assert bot.call_api.await_count == 1
    assert [r[1]["status"] for r in events] == ["pending", "sent", "sent"]
    assert len({r[1]["receipt_id"] for r in events}) == 1
    assert all(r[0] == "task-file" and r[1]["kind"] == "file" for r in events)


def test_real_message_send_preserves_parent_task(runtime):
    events = []
    async def record(task_id, row): events.append((task_id, row))
    runtime.state._adapter.client = SimpleNamespace(report_task_delivery=record)
    bot = SimpleNamespace(self_id="1000000001", send_private_msg=AsyncMock(return_value={"message_id": 7}))
    async def run():
        await runtime.sending._send_qq_message(
            bot, {"type": "private", "user_id": "10001"}, "hello",
            send_context=DeliveryContext(task_id="task-reply", idempotency_key="reply-1").child("part-1"),
        )
    asyncio.run(run())
    assert events[-1][0] == "task-reply" and events[-1][1]["status"] == "sent"
    assert events[-1][1]["kind"] == "message"


def test_forward_bridge_keeps_task_identity_and_partial_file_failures(monkeypatch, tmp_path):
    from test_qq_forward_bridge_authz import _harness, _run, _ADMIN_QQ, _TASK
    runtime, bot = _harness(monkeypatch, tmp_path, requester=_ADMIN_QQ)
    events = []
    async def record(task_id, row): events.append((task_id, row))
    runtime.state._adapter.client.report_task_delivery = record
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    monkeypatch.setattr(runtime.forward_bridge, "_forward_bridge_resolve_files", lambda *args: (
        [{"path": str(path), "name": "report.txt"}], ["missing.txt not found"],
    ))
    result = _run(runtime, {"action": "file", "target_type": "current", "file_paths": ["report.txt", "missing.txt"]})
    assert result["ok"] is True
    assert {task_id for task_id, _ in events} == {_TASK}
    assert [r[1]["status"] for r in events] == ["failed", "pending", "sent"]
    assert bot.api_calls == ["upload_group_file"]
    runtime.state._adapter.outbound_idempotency.close()
