from __future__ import annotations

import json
import asyncio
import zipfile
from collections import OrderedDict
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import supervisor.admin_api as admin_api
from engine.runner import _run_tool_task
from supervisor.eventlog import EventLog
from supervisor.execution import ExecutionService, create_router
from supervisor.execution.models import PlanDraft
from supervisor.execution.store import ConflictError
from supervisor.feature_auth import FeatureActor
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState
from supervisor.types import TaskKind, TaskStatus
from toolbox.registry import ToolRegistry


@pytest.fixture
def state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "feature-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    nodes = tmp_path / "config/nodes"
    nodes.mkdir(parents=True)
    (nodes / "test.entry.yaml").write_text("id: test.entry\ntype: ai\ntool_access:\n  mode: allowlist\n  allow: [read_file, write_file]\n", encoding="utf-8")
    state = SupervisorState(workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data/events.jsonl", run_id="execution"), policy=PolicyEngine(workspace_root=tmp_path))
    state._defer_post_work_enqueue = True
    monkeypatch.setattr(state, "_default_entry_node", lambda: "test.entry")
    state.execution = ExecutionService(state)
    yield state
    state.execution.close()


@pytest.fixture
def actor():
    return FeatureActor(scope="qq_group:test", owner="alice", bot_scope="bot", channel="qq_group")


def finish(state, task_id, result=None):
    task = state.tasks[task_id]
    task.status = TaskStatus.running
    task.worker_id = "worker"
    state.complete_task(task_id=task_id, worker_id="worker", result=result or {"action": "finish", "result": {"text": "processed output"}})


def test_plan_requires_current_human_confirmation(state, actor):
    service = state.execution
    plan = service.create(actor, PlanDraft(goal="总结", inputs=[{"kind": "text", "value": "source"}]))
    service.tick()
    assert not state.tasks
    plan = service.edit(actor, plan["id"], 1, PlanDraft(goal="翻译", work_scope="只处理这份资料"))
    with pytest.raises(ConflictError):
        service.confirm(actor, plan["id"], 1)
    with pytest.raises(HTTPException):
        service.confirm(FeatureActor(**{**actor.__dict__, "task_id": "model"}), plan["id"], 2)
    service.confirm(actor, plan["id"], 2)
    service.confirm(actor, plan["id"], 2)
    service.tick()
    service.tick()
    assert len(state.tasks) == 1
    assert service.view(actor, plan["id"])["steps"][1]["status"] == "queued"


def test_batch_runs_each_item_and_downloads_manifest(state, actor):
    service = state.execution
    plan = service.create(actor, PlanDraft(goal="整理", inputs=[{"value": "one", "label": "第一项"}, {"value": "two", "label": "第二项"}]))
    service.confirm(actor, plan["id"], 1)
    service.tick(); service.tick()
    current = service.get(actor, plan["id"])
    jobs = [step for step in current["steps"] if step["kind"] == "model"]
    finish(state, jobs[0]["task_id"], {"action": "finish", "result": {"text": "result one"}})
    finish(state, jobs[1]["task_id"], {"action": "fail", "error": "temporary model error"})
    service.tick()
    result = service.view(actor, plan["id"])
    assert result["status"] == "partial"
    assert result["items"][0]["status"] == "succeeded"
    first_receipt = result["steps"][1]["effect_receipt"]
    service.retry(actor, plan["id"], [jobs[1]["id"]])
    service.tick()
    retry = service.get(actor, plan["id"])
    retry_task = next(step for step in retry["steps"] if step["id"] == jobs[1]["id"])["task_id"]
    assert retry_task != jobs[1]["task_id"]
    assert retry["steps"][1]["effect_receipt"] == first_receipt
    finish(state, retry_task)
    service.tick()
    result = service.view(actor, plan["id"])
    assert result["status"] == "completed"
    assert len(result["artifacts"]) == 2
    with zipfile.ZipFile(service.bundle(actor, plan["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["items"]) == 2
        assert len(archive.namelist()) == 3
    deliveries = [event for event in state.eventlog.events if event["type"] == "outbound_message"]
    assert len(deliveries) == 2
    service.tick()
    assert len([event for event in state.eventlog.events if event["type"] == "outbound_message"]) == 2


@pytest.mark.asyncio
async def test_real_tool_execution_keeps_grants_and_policy_and_does_not_repeat_success(state, actor):
    plan = state.execution.create(actor, PlanDraft(goal="保存并读取文件", steps=[
        {"id": "save", "title": "保存文件", "kind": "tool", "operation": "write_file", "arguments": {"path": "data/created.txt", "content": "persisted"}},
        {"id": "read", "title": "读取另一文件", "kind": "tool", "operation": "read_file", "dependencies": ["save"], "arguments": {"path": "data/missing.txt"}},
    ]))
    state.execution.confirm(actor, plan["id"], 1)
    requests = []
    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"safety_level": "auto", "cancelled": False, "ok": True})
    registry = ToolRegistry(workspace_root=state.workspace_root, tools_dir=state.workspace_root / "tools")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        for index in range(2):
            state.execution.tick()
            step = state.execution.get(actor, plan["id"])["steps"][index]
            task = state.tasks[step["task_id"]]
            result = await _run_tool_task(http=http, sup_url="http://supervisor", ws_root=state.workspace_root, registry=registry, task=task.model_dump(mode="json"), worker_id="worker", session_id=task.session_id, session_generation=task.session_generation, task_id=task.task_id)
            finish(state, task.task_id, result)
    state.execution.tick()
    assert (state.workspace_root / "data/created.txt").read_text() == "persisted"
    assert "/v1/ops/request" in requests
    before = state.execution.get(actor, plan["id"])
    (state.workspace_root / "data/missing.txt").write_text("now available")
    state.execution.retry(actor, plan["id"], ["read"])
    state.execution.tick()
    after = state.execution.get(actor, plan["id"])
    assert after["steps"][0]["attempt"] == 1
    assert after["steps"][0]["effect_receipt"] == before["steps"][0]["effect_receipt"]
    assert after["steps"][1]["attempt"] == 2


@pytest.mark.asyncio
async def test_mixed_file_and_link_plan_runs_real_engine_loop_with_injected_model(state, actor, monkeypatch):
    import shutil
    from engine import runner
    from engine.hooks.registry import HookRegistry
    from engine.inference import ai_step
    from engine.signals.bus import get_bus
    from providers.base import ProviderResponse, ToolCall
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore
    import supervisor.execution.service as execution_module

    system_nodes = state.workspace_root / "engine/system_nodes"
    system_nodes.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "engine/system_nodes/system.execution.yaml", system_nodes / "system.execution.yaml")
    source = state.workspace_root / "data/attachments/qq_group_test/source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("file content", encoding="utf-8")
    monkeypatch.setattr(execution_module, "fetch_url", lambda url, limit: "link content")
    monkeypatch.setattr(ai_step, "hook_registry", HookRegistry())
    monkeypatch.setattr(ai_step, "auto_discover_and_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "load_runtime_config", lambda *args: {"engine": {"signals": {"enabled": False}}})
    monkeypatch.setattr(get_bus(), "enabled", get_bus().enabled)
    observed = []
    async def model_response(loop, step):
        prompt = json.dumps(loop.messages, ensure_ascii=False)
        observed.append(prompt)
        assert not loop.allowed_real_tools
        value = "file result" if "file content" in prompt else "link result"
        return ProviderResponse(ok=True, tool_calls=[ToolCall(id=f"finish-{len(observed)}", name="finish", arguments={"text": value})])
    monkeypatch.setattr(ai_step, "_call_llm_with_retry", model_response)
    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=state.workspace_root / "data/config.yaml"))
    plan = state.execution.create(actor, PlanDraft(goal="summarize sources", inputs=[{"kind": "file", "value": source.relative_to(state.workspace_root).as_posix(), "label": "文件"}, {"kind": "url", "value": "https://example.test/source", "label": "链接"}]))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick(); state.execution.tick()
    def no_external_request(request):
        raise AssertionError(f"unexpected network request {request.url}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer feature-test"}) as client, httpx.AsyncClient(transport=httpx.MockTransport(no_external_request)) as llm:
        for _ in range(2):
            assigned = state.assign_next_task(worker_id="worker")
            assert assigned and assigned["node_id"] == "system.execution"
            result = await runner._run_node_task(http=client, llm_http=llm, sup_url="http://test", ws_root=state.workspace_root, registry=ToolRegistry(workspace_root=state.workspace_root, tools_dir=state.workspace_root / "tools"), task=assigned, worker_id="worker", session_id=assigned["session_id"], session_generation=assigned["session_generation"], api_key="synthetic-test-only", base_url="http://model.invalid", default_model="test-model")
            assert result["action"] == "finish"
            finish(state, assigned["task_id"], result)
    state.execution.tick()
    completed = state.execution.view(actor, plan["id"])
    assert completed["status"] == "completed"
    assert len(observed) == 2 and any("file content" in prompt for prompt in observed) and any("link content" in prompt for prompt in observed)
    for item, expected in zip(completed["items"], ["file result", "link result"]):
        path, _ = state.execution.artifact_path(actor, item["artifacts"][0]["id"])
        assert path.read_text(encoding="utf-8") == expected
        assert item["status"] == "succeeded" and item["errors"] == []


def test_tool_timeout_requires_result_reconciliation(state, actor):
    plan = state.execution.create(actor, PlanDraft(goal="写文件", steps=[{"id": "write", "title": "写文件", "kind": "tool", "operation": "write_file", "arguments": {"path": "data/result.txt", "content": "x"}}]))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick()
    task_id = state.execution.get(actor, plan["id"])["steps"][0]["task_id"]
    finish(state, task_id, {"action": "fail", "error": "connection lost after operation"})
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["steps"][0]["status"] == "outcome_unknown"
    with pytest.raises(ConflictError):
        state.execution.retry(actor, plan["id"], ["write"])
    state.execution.resolve_step(actor, plan["id"], "write", "not_applied", "已检查目标文件不存在")
    state.execution.retry(actor, plan["id"], ["write"])
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["steps"][0]["attempt"] == 2


@pytest.mark.parametrize("operation,expected", [("write_file", "outcome_unknown"), ("read_file", "failed")])
def test_expired_worker_lease_and_restart_never_replay_effect_automatically(state, actor, operation, expected):
    from datetime import datetime, timedelta, timezone

    state.register_engine("worker", "generation-one")
    plan = state.execution.create(actor, PlanDraft(goal="operation", steps=[{"id": "operation", "title": "文件操作", "kind": "tool", "operation": operation, "arguments": {"path": "data/result.txt", "content": "x"}}]))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick()
    assigned = state.assign_next_task(worker_id="worker")
    task = state.tasks[assigned["task_id"]]
    task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert state.assign_next_task(worker_id="replacement") is None
    state.register_engine("worker", "generation-two")
    state.execution.tick()
    step = state.execution.get(actor, plan["id"])["steps"][0]
    assert step["status"] == expected and step["attempt"] == 1
    assert state.assign_next_task(worker_id="replacement") is None
    if operation == "write_file":
        with pytest.raises(ConflictError):
            state.execution.retry(actor, plan["id"], ["operation"])
    else:
        state.execution.retry(actor, plan["id"], ["operation"])
        state.execution.tick()
        replacement = state.assign_next_task(worker_id="replacement")
        assert replacement and replacement["task_id"] != task.task_id


def test_disallowed_tool_and_other_owner_are_rejected(state, actor):
    with pytest.raises(PermissionError):
        state.execution.create(actor, PlanDraft(goal="bad", steps=[{"id": "cmd", "title": "命令", "kind": "tool", "operation": "execute_command", "arguments": {"command": "whoami"}}]))
    plan = state.execution.create(actor, PlanDraft(goal="private"))
    with pytest.raises(HTTPException):
        state.execution.get(FeatureActor(scope=actor.scope, owner="bob", bot_scope=actor.bot_scope), plan["id"])


def test_restart_recovers_completed_attempt_from_eventlog(state, actor):
    plan = state.execution.create(actor, PlanDraft(goal="summarize"))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick(); state.execution.tick()
    task_id = state.execution.get(actor, plan["id"])["steps"][1]["task_id"]
    state.execution = None
    finish(state, task_id)
    state.tasks.pop(task_id)
    state.execution = ExecutionService(state)
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["status"] == "completed"
    assert state.execution.get(actor, plan["id"])["steps"][1]["attempt"] == 1


def test_reset_cancels_plan_without_touching_another_scope(state, actor):
    plan = state.execution.create(actor, PlanDraft(goal="first"))
    other_actor = FeatureActor(scope="qq_group:other", owner="alice", bot_scope="bot")
    other = state.execution.create(other_actor, PlanDraft(goal="second"))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.confirm(other_actor, other["id"], 1)
    state.execution.tick()
    state.reset_session(session_id=state.execution.get(actor, plan["id"])["session_id"])
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["status"] == "cancelled"
    assert state.execution.get(other_actor, other["id"])["status"] in {"queued", "running"}


@pytest.mark.asyncio
async def test_plan_api_requires_transport_auth_and_does_not_trust_owner_body(state):
    app = FastAPI()
    app.include_router(create_router(state))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/execution/plans")).status_code == 401
        headers = {"Authorization": "Bearer feature-test", "X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:1", "user_id": "alice", "channel": "qq_group"})}
        created = await client.post("/v1/execution/plans", headers=headers, json={"goal": "test", "owner": "evil", "scope": "other"})
        assert created.status_code == 200
        plan = created.json()
        assert plan["scope"] == "qq_group:1" and plan["owner"] != "evil"
        wrong = {**headers, "X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:1", "user_id": "bob"})}
        assert (await client.get(f"/v1/execution/plans/{plan['id']}", headers=wrong)).status_code == 403


def test_file_snapshot_preserves_confirmed_input_when_upload_changes(state, actor):
    directory = state.workspace_root / "data/attachments/qq_group_test"
    directory.mkdir(parents=True)
    path = directory / "file.txt"
    path.write_text("before")
    plan = state.execution.create(actor, PlanDraft(goal="read", inputs=[{"kind": "file", "value": path.relative_to(state.workspace_root).as_posix()}]))
    state.execution.confirm(actor, plan["id"], 1)
    path.write_text("changed")
    state.execution.tick()
    step = state.execution.get(actor, plan["id"])["steps"][0]
    assert step["status"] == "succeeded" and step["result"] == "before"


def test_file_snapshot_survives_upload_cleanup_and_rejects_snapshot_tampering(state, actor):
    directory = state.workspace_root / "data/attachments/qq_group_test"
    directory.mkdir(parents=True)
    path = directory / "file.txt"
    path.write_text("original")
    draft = PlanDraft(goal="read", inputs=[{"kind": "file", "value": path.relative_to(state.workspace_root).as_posix()}])
    plan = state.execution.create(actor, draft)
    path.unlink()
    state.execution = ExecutionService(state)
    edited = state.execution.edit(actor, plan["id"], 1, draft.model_copy(update={"goal": "translate"}))
    state.execution.confirm(actor, plan["id"], 2)
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["steps"][0]["result"] == "original"
    snapshot = state.workspace_root / edited["inputs"][0]["snapshot_path"]
    snapshot.write_text("tampered")
    with pytest.raises(ValueError, match="改变"):
        state.execution._input_path(edited["inputs"][0])


@pytest.mark.parametrize("error", ["written; subsequent user denied approval", "远端执行成功，但无权查询回执", "拒绝执行结果查询"])
def test_post_execution_error_words_do_not_prove_effect_was_not_applied(state, actor, error):
    plan = state.execution.create(actor, PlanDraft(goal="写文件", steps=[{"id": "write", "title": "写文件", "kind": "tool", "operation": "write_file", "arguments": {"path": "data/result.txt", "content": "x"}}]))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick()
    task_id = state.execution.get(actor, plan["id"])["steps"][0]["task_id"]
    finish(state, task_id, {"action": "finish", "result": {"tool_ok": False, "tool_error": error}})
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["steps"][0]["status"] == "outcome_unknown"
    with pytest.raises(ConflictError):
        state.execution.retry(actor, plan["id"], ["write"])


@pytest.mark.parametrize("result", [{"action": "ask", "result": {"text": "Please provide more information"}}, {"action": "finish", "result": {"text": ""}}])
def test_followup_question_or_empty_output_does_not_count_as_completed_processing(state, actor, result):
    plan = state.execution.create(actor, PlanDraft(goal="summarize"))
    state.execution.confirm(actor, plan["id"], 1)
    state.execution.tick(); state.execution.tick()
    task_id = state.execution.get(actor, plan["id"])["steps"][1]["task_id"]
    finish(state, task_id, result)
    state.execution.tick()
    current = state.execution.get(actor, plan["id"])
    assert current["steps"][1]["status"] == "failed"
    assert current["artifacts"] == []


def test_confirmation_does_not_keep_revoked_group_role(state, actor):
    privileged = FeatureActor(**{**actor.__dict__, "role": "admin"})
    plan = state.execution.create(privileged, PlanDraft(goal="summarize"))
    state.execution.confirm(actor, plan["id"], 1)
    assert state.execution.store.get(plan["id"])["task_context"]["platform_auth"]["group_role"] == "member"


def test_plan_listing_filters_owner_before_limit_and_scheduler_has_no_display_limit(state, actor):
    plan = state.execution.create(actor, PlanDraft(goal="old plan"))
    current = state.execution.store.get(plan["id"])
    with state.execution.store.connection() as db:
        rows = []
        for index in range(501):
            other = {**current, "id": f"P{index:012x}", "owner": "other", "status": "queued"}
            rows.append((other["id"], other["scope"], other["owner"], other["status"], json.dumps(other)))
        db.executemany("INSERT INTO plans VALUES (?,?,?,?,?)", rows)
    assert [item["id"] for item in state.execution.list(actor)] == [plan["id"]]
    assert len(state.execution.store.list(active=True)) == 501


def test_cancel_during_input_read_does_not_launch_downstream_task(state, actor, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import supervisor.execution.service as module

    entered, release = Event(), Event()
    def read(*args):
        entered.set()
        assert release.wait(5)
        return "source"
    monkeypatch.setattr(module, "fetch_url", read)
    plan = state.execution.create(actor, PlanDraft(goal="summarize", inputs=[{"kind": "url", "value": "https://example.test/a"}]))
    state.execution.confirm(actor, plan["id"], 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(state.execution.tick)
        try:
            assert entered.wait(5)
            state.execution.cancel(actor, plan["id"])
        finally:
            release.set()
        pending.result(timeout=5)
    state.execution.tick()
    assert state.execution.get(actor, plan["id"])["status"] == "cancelled"
    assert not state.tasks


@pytest.mark.asyncio
async def test_full_api_plan_tool_waits_for_real_approval_before_writing(state):
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore

    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=state.workspace_root / "data/config.yaml"))
    headers = {"Authorization": "Bearer feature-test"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        response = await client.post("/v1/execution/plans?scope=web:console", json={"goal": "write config", "steps": [{"id": "save", "title": "写入指定配置", "kind": "tool", "operation": "write_file", "arguments": {"path": "config/result.yaml", "content": "approved: true"}}]})
        assert response.status_code == 200, response.text
        plan = response.json()
        confirmation = await client.post(f"/v1/execution/plans/{plan['id']}/confirm?scope=web:console", json={"expected_revision": 1})
        assert confirmation.status_code == 200
        state.execution.tick()
        assigned = state.assign_next_task(worker_id="worker")
        assert assigned is not None
        pending = asyncio.create_task(_run_tool_task(
            http=client, sup_url="http://test", ws_root=state.workspace_root,
            registry=ToolRegistry(workspace_root=state.workspace_root, tools_dir=state.workspace_root / "tools"),
            task=assigned, worker_id="worker", session_id=assigned["session_id"], session_generation=assigned["session_generation"], task_id=assigned["task_id"],
        ))
        try:
            for _ in range(100):
                if state.approvals:
                    break
                await asyncio.sleep(0.01)
            assert state.approvals
            assert not (state.workspace_root / "config/result.yaml").exists()
            state.execution.tick()
            current = (await client.get(f"/v1/execution/plans/{plan['id']}?scope=web:console")).json()
            assert current["steps"][0]["status"] == "awaiting_approval"
            approval = next(iter(state.approvals.values()))
            decision = await client.post(f"/v1/approvals/{approval.approval_id}", json={"decision": "allow"})
            assert decision.status_code == 200
            result = await asyncio.wait_for(pending, timeout=3)
            assert result["result"]["tool_ok"] is True
            completion = await client.post(f"/v1/tasks/{assigned['task_id']}/complete", json={"worker_id": "worker", "result": result})
            assert completion.status_code == 200
            state.execution.tick()
            assert state.execution.store.get(plan["id"])["status"] == "completed"
            assert (state.workspace_root / "config/result.yaml").read_text() == "approved: true"
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending


def test_step_claim_and_actor_identity_cannot_be_forged(state, actor):
    from concurrent.futures import ThreadPoolExecutor

    plan = state.execution.create(actor, PlanDraft(goal="once", steps=[{"id": "read", "title": "读取", "kind": "tool", "operation": "read_file", "arguments": {"path": "x"}}]))
    state.execution.confirm(actor, plan["id"], 1)
    plan = state.execution.store.get(plan["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: state.execution._execute(plan, plan["steps"][0]), range(2)))
    assert len(state.tasks) == 1
    task = next(iter(state.tasks.values()))
    bound = state.execution.actor_for_task(task)
    assert bound.owner == actor.owner and bound.scope == actor.scope
    assert bound.interactive is False
    task.input["execution_effect_key"] = "forged"
    with pytest.raises(HTTPException) as error:
        state.execution.actor_for_task(task)
    assert error.value.status_code == 403


def test_link_without_path_preserves_query_and_still_blocks_private_redirect(monkeypatch):
    import socket
    from supervisor.execution import inputs

    seen = []
    locations = [None]
    class Response:
        @property
        def status(self):
            return 302 if locations[0] else 200

        def getheader(self, name, default=None):
            return locations[0] if name == "Location" else "text/plain; charset=utf-8" if name == "Content-Type" else default

        def read(self, count):
            return b"page text"

    class Connection:
        def __init__(self, *args, **kwargs): pass
        def connect(self): pass
        def close(self): pass
        def request(self, method, path, headers): seen.append(path)
        def getresponse(self): return Response()

    monkeypatch.setattr(inputs.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(inputs.socket, "getaddrinfo", lambda host, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1" if host == "localhost" else "93.184.216.34", 80))])
    assert inputs.fetch_url("http://example.test?question=hello", 100) == "page text"
    assert seen == ["/?question=hello"]
    assert inputs.fetch_url("http://example.test/中文?word=你好&value=%20", 100) == "page text"
    assert seen[-1] == "/%E4%B8%AD%E6%96%87?word=%E4%BD%A0%E5%A5%BD&value=%20"
    locations[0] = "http://localhost/private"
    with pytest.raises(ValueError, match="公网"):
        inputs.fetch_url("http://example.test/redirect", 100)
    assert seen[-1] == "/redirect"
