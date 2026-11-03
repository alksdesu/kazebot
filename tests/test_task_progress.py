"""任务查询的隔离、投递事实和发送前的版本复核。"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from engine.inference.task_progress import prepare_progress_reply, query_status
from engine.inference.pseudo_tools import _finish_spec, _task_status_spec
from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, turn
from supervisor import task_progress
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.types import RouteStatus, TaskStatus
from test_concurrent_turns import (
    _admin_token, state, _session, _round, _async_child, _ALICE, _BOB, _ADMIN, _PRIVATE, _TOKEN,
)


def receipt(key="a", status="sent", **overrides):
    return {"receipt_id": key * 64, "kind": "file", "status": status, "count": 1,
            "destination": "private", "destination_scope": "b" * 64, **overrides}


def query(state, caller, ids=None):
    with state._lock:
        return task_progress.snapshot_locked(state, caller, ids or [])


def app_of(state):
    return create_app(state=state, process_manager=None,
                      config_store=ConfigStore(path=state.workspace_root / "config/models.yaml", workspace_root=state.workspace_root))


def test_live_query_includes_children_and_recent_completion_but_not_other_people(state):
    sid = _session(state)
    alice = _round(state, sid, _ALICE)
    child = _async_child(state, alice)
    bob = _round(state, sid, _BOB)
    caller = _round(state, sid, _ALICE)
    alice.status = TaskStatus.completed
    alice.route_status = RouteStatus.routed
    alice.result = {"action": "finish"}
    rows = {t["task_id"]: t for t in query(state, caller)["tasks"]}
    assert set(rows) == {alice.task_id, child.task_id}
    assert rows[alice.task_id]["status"] == "completed"
    assert rows[alice.task_id]["delivery"]["unknown"] is True
    assert query(state, caller, [bob.task_id])["tasks"] == [{"task_id": bob.task_id, "status": "unavailable"}]


def test_admin_still_cannot_query_another_private_conversation(state):
    mine = _session(state, _PRIVATE)
    caller = _round(state, mine, _ADMIN)
    foreign = _round(state, _session(state, "qq_private:other"), _BOB)
    assert query(state, caller, [foreign.task_id])["tasks"][0]["status"] == "unavailable"


def test_successful_tool_is_not_a_delivery_receipt(state):
    sid = _session(state)
    task = _round(state, sid, _ALICE)
    caller = _round(state, sid, _ALICE)
    task_progress.activity(task, "tool_call_end", {"tool_name": "qq_forward", "status": "success"})
    item = query(state, caller)["tasks"][0]
    assert item["last_tool"]["name"] == "qq_forward"
    assert item["delivery"]["records"] == [] and item["delivery"]["unknown"] is True
    assert item["delivery"]["counts"]["file"]["sent"] == 0


def test_partial_delivery_receipts_are_idempotent_and_do_not_finish_the_task(state):
    sid = _session(state)
    task = _round(state, sid, _ALICE)
    caller = _round(state, sid, _ALICE)
    for row in (receipt(), receipt(), receipt(status="pending"), receipt("c", "unknown"), receipt("d", "failed")):
        task_progress.record_delivery_locked(state, task, row)
    item = query(state, caller)["tasks"][0]
    assert item["status"] == "running"
    assert sorted(r["status"] for r in item["delivery"]["records"]) == ["failed", "sent", "unknown"]
    events = [e for e in state.eventlog.iter_persisted_events() if e["type"] == "task_delivery_receipt"]
    assert len(events) == 3


def test_parallel_tools_remain_active_until_each_finishes(state):
    task = _round(state, _session(state), _ALICE)
    task_progress.activity(task, "tool_call_start", {"tool_name": "read_file", "tool_call_id": "a"})
    task_progress.activity(task, "tool_call_start", {"tool_name": "web_search", "tool_call_id": "b"})
    task.current_phase = ""
    task_progress.activity(task, "tool_call_end", {"tool_name": "read_file", "tool_call_id": "a", "status": "success"})
    assert task.current_phase == "tool_call" and task.current_detail == "web_search"


def test_model_wait_has_a_phase_without_inventing_a_final_round(state):
    task = _round(state, _session(state), _ALICE)
    task_progress.activity(task, "model_request_started", {})
    assert task.current_phase == "model_request"
    task_progress.activity(task, "model_request_ended", {})
    assert task.current_phase == ""


def test_version_ignores_elapsed_time_and_changes_for_receipts_and_completion(state):
    sid = _session(state)
    task = _round(state, sid, _ALICE)
    caller = _round(state, sid, _ALICE)
    snapshot = query(state, caller)
    task.created_at -= timedelta(seconds=1)
    assert task_progress.check_locked(state, caller, snapshot["query_id"])["changed"] is False
    task_progress.record_delivery_locked(state, task, receipt())
    checked = task_progress.check_locked(state, caller, snapshot["query_id"])
    assert checked["changed"] is True
    assert task_progress.check_locked(state, task, checked["snapshot"]["query_id"]) is None
    task.status = TaskStatus.completed
    assert task_progress.check_locked(state, caller, checked["snapshot"]["query_id"])["changed"] is True


def test_api_requires_auth_and_rejects_malformed_receipts(state):
    task = _round(state, _session(state), _ALICE)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_of(state)), base_url="http://test") as client:
            url = f"/v1/tasks/{task.task_id}/deliveries"
            assert (await client.post(url, json=receipt())).status_code in (401, 403)
            client.headers["Authorization"] = f"Bearer {_TOKEN}"
            assert (await client.post(url, json=receipt(count=True))).status_code == 422
            assert (await client.post(url, json=receipt())).status_code == 200
    asyncio.run(run())


def test_progress_refreshes_once_then_uses_the_original_query_time(state):
    sid = _session(state)
    target = _round(state, sid, _ALICE)
    caller = _round(state, sid, _ALICE)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_of(state)), base_url="http://test",
                                    headers={"Authorization": f"Bearer {_TOKEN}"}) as client:
            ls = SimpleNamespace(rctx=SimpleNamespace(http=client, supervisor_url="http://test", task_id=caller.task_id),
                                 status_snapshot=None, status_refresh_count=0)
            answer = await query_status(ls, {})
            assert target.task_id in answer
            original = ls.status_snapshot["query_id"]
            target.status = TaskStatus.completed
            text, error = await prepare_progress_reply(ls, original, "任务还在跑")
            assert error and ls.status_refresh_count == 1
            updated = ls.status_snapshot["query_id"]
            target.status = TaskStatus.failed
            text, error = await prepare_progress_reply(ls, updated, "任务完成了")
            assert not error and "查询时" in text and text.endswith("任务完成了")
            assert ls.status_refresh_count == 1
    asyncio.run(run())


def test_progress_check_failure_is_dated_not_silenced():
    snapshot = {"query_id": "id", "observed_at": "2026-11-03T05:30:51+00:00", "version": "v", "tasks": []}
    async def fail(*args, **kwargs): raise httpx.ConnectError("offline")
    ls = SimpleNamespace(rctx=SimpleNamespace(http=SimpleNamespace(post=fail), supervisor_url="http://test", task_id="caller"),
                         status_snapshot=snapshot)
    text, error = asyncio.run(prepare_progress_reply(ls, "id", "进度与其他说明"))
    assert not error and text.endswith("进度与其他说明") and "05:30:51+00:00" in text
    _, error = asyncio.run(prepare_progress_reply(ls, "forged", "text"))
    assert error


def test_tool_schemas_expose_query_and_single_delivery():
    assert _task_status_spec()["function"]["name"] == "get_task_status"
    finish = _finish_spec()["function"]
    assert "status_query_id" in finish["parameters"]["properties"]
    assert "attachment_paths" in finish["parameters"]["properties"]


def test_engine_rewrites_a_stale_report_without_repeating_work(monkeypatch, tmp_path):
    def snapshot(n):
        return {"query_id": f"q{n}", "observed_at": f"2026-11-03T05:31:0{n}+00:00", "version": f"v{n}",
                "tasks": [{"task_id": "other", "status": "running" if n == 1 else "completed"}]}
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/check"):
            return httpx.Response(200, json={"changed": len(requests) == 2, "snapshot": snapshot(2)})
        return httpx.Response(200, json=snapshot(1))
    runner = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry())
    runner.rctx.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    result = runner.run(
        turn(call("get_task_status", {}, call_id="lookup")),
        turn(call("finish", {"text": "还在跑", "status_query_id": "q1"}, call_id="before")),
        turn(call("finish", {"text": "已经结束", "status_query_id": "q2"}, call_id="after")),
        instruction="进度呢",
    )
    assert result.action == "finish" and result.result["text"].endswith("已经结束")
    assert "还在跑" not in result.result["text"]
    assert len(runner.requests) == 3 and not runner.registry.executed
    assert "get_task_status" in runner.tool_names(0)
    asyncio.run(runner.rctx.http.aclose())


def test_greeting_finishes_without_query_or_progress_annotation(monkeypatch, tmp_path):
    runner = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(),
                     task_context={"concurrent_tasks": {"count": 1}})
    result = runner.run(turn(call("finish", {"text": "在呢"}, call_id="greeting")), instruction="你好")
    assert result.result["text"] == "在呢" and len(runner.requests) == 1


def test_an_existing_file_can_be_delivered_in_one_finish(monkeypatch, tmp_path):
    from engine.inference import attachment_access
    from unittest.mock import AsyncMock
    path = tmp_path / "data/attachments/report.txt"
    path.parent.mkdir(parents=True)
    path.write_text("report", encoding="utf-8")
    guard = AsyncMock(return_value=({}, None))
    monkeypatch.setattr(attachment_access, "request_guard", guard)
    runner = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry())
    result = runner.run(turn(call("finish", {"text": "文档在这里", "attachment_paths": ["data/attachments/report.txt"]}, call_id="file")))
    assert result.action == "finish" and len(result.result["attachments"]) == 1
    assert len(runner.requests) == 1 and not runner.registry.executed
    assert guard.await_count == 1


def test_attachment_disappearing_after_approval_does_not_claim_delivery(monkeypatch, tmp_path):
    from engine.inference import attachment_access
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    async def approve(*args):
        path.unlink()
        return {}, None
    monkeypatch.setattr(attachment_access, "request_guard", approve)
    runner = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry())
    result = runner.run(
        turn(call("finish", {"text": "文档在这里", "attachment_paths": ["report.txt"]}, call_id="file")),
        turn(call("finish", {"text": "文件已不存在，没能发送"}, call_id="failure")),
    )
    assert result.result["text"] == "文件已不存在，没能发送" and not result.result["attachments"]


def test_no_steps_left_delivers_a_dated_report_instead_of_rejecting_it(state):
    sid = _session(state)
    target = _round(state, sid, _ALICE)
    caller = _round(state, sid, _ALICE)
    snapshot = query(state, caller)
    target.status = TaskStatus.completed
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_of(state)), base_url="http://test",
                                    headers={"Authorization": f"Bearer {_TOKEN}"}) as client:
            ls = SimpleNamespace(rctx=SimpleNamespace(http=client, supervisor_url="http://test", task_id=caller.task_id),
                                 status_snapshot=snapshot, status_refresh_count=0, status_can_refresh=False)
            text, error = await prepare_progress_reply(ls, snapshot["query_id"], "查询时仍在运行")
            assert not error and text.endswith("查询时仍在运行") and "以下进度截至" in text
    asyncio.run(run())


def test_interim_progress_reply_uses_the_same_freshness_gate(monkeypatch, tmp_path):
    from _engine_loop_fixture import make_loop_state
    from engine.inference.pseudo_handlers import _handle_pseudo_tool
    snapshot = {"query_id": "q1", "observed_at": "2026-11-03T05:31:00+00:00", "version": "v", "tasks": []}
    ls = make_loop_state(tmp_path, ScriptedRegistry())
    ls.status_snapshot = snapshot
    ls.rctx.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"changed": False, "snapshot": snapshot},
    )))
    async def run():
        await _handle_pseudo_tool(ls, call("reply", {"text": "当前进度", "status_query_id": "q1"}, call_id="reply"), 0)
        await ls.rctx.http.aclose()
    asyncio.run(run())
    [event] = ls.rctx.events.of("intermediate_reply")
    assert event["text"].endswith("当前进度") and "查询时" in event["text"]
