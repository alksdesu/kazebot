"""Tests for attaching approval events to tool executions."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest


# Why: these tests run from a source checkout rather than an installed package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules directly and validate the event payload contract.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import SessionInfo, _now  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, Task, TaskKind, TaskStatus  # noqa: E402


def _state(tmp_path: Path) -> SupervisorState:
    """Build a supervisor state with the default policy engine."""
    # Why: execute_command requires approval by default. How: use the real default
    # PolicyEngine with a temporary workspace and EventLog. Purpose: exercise the
    # same create_approval path used by /v1/ops/request.
    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="test-run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _attach_task_to_session(
    state: SupervisorState,
    *,
    session_id: str,
    conversation_key: str,
    task_id: str,
) -> None:
    """Register a minimal task/session pair for policy routing tests."""
    # Why: scheduler approval bypass is keyed by the task's owning session. How:
    # store a real SessionInfo and Task in the same in-memory maps that production
    # request_operation reads. Purpose: verify the routing decision without
    # involving the scheduler loop or engine workers.
    now = _now()
    state.sessions[session_id] = SessionInfo(
        session_id=session_id,
        channel="test",
        conversation_key=conversation_key,
        created_at=now,
        updated_at=now,
    )
    state.tasks[task_id] = Task(
        task_id=task_id,
        session_id=session_id,
        session_generation=1,
        kind=TaskKind.node,
        node_id="system.dream",
        input={},
        continuation={},
        source_inbound_seq=None,
        status=TaskStatus.running,
        created_at=now,
        updated_at=now,
    )


def test_request_operation_emits_approval_tool_identity(tmp_path: Path) -> None:
    state = _state(tmp_path)

    out = state.request_operation(
        session_id="session-1",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-1",
        node_id="node-1",
        task_id="task-1",
    )

    assert out.approval_id
    approval = state.approvals[out.approval_id]
    assert approval.tool_call_id == "call-1"
    assert approval.node_id == "node-1"
    assert approval.task_id == "task-1"

    events = state.eventlog.list_events(session_id="session-1", after_seq=0)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["tool_call_id"] == "call-1"
    assert payload["node_id"] == "node-1"
    assert payload["task_id"] == "task-1"


def test_decide_approval_emits_tool_identity(tmp_path: Path) -> None:
    state = _state(tmp_path)
    out = state.request_operation(
        session_id="session-1",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-1",
        node_id="node-1",
        task_id="task-1",
    )

    state.decide_approval(approval_id=out.approval_id or "", decision="allow", comment="ok")

    events = state.eventlog.list_events(session_id="session-1", after_seq=0)
    decided = [event for event in events if event["type"] == "approval_decided"]
    assert len(decided) == 1
    payload = decided[0]["payload"]
    assert payload["approval_id"] == out.approval_id
    assert payload["decision"] == "allow"
    assert payload["tool_call_id"] == "call-1"
    assert payload["node_id"] == "node-1"
    assert payload["task_id"] == "task-1"


def test_scheduler_task_execute_command_auto_approves_without_event(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_task_to_session(
        state,
        session_id="session-scheduler",
        conversation_key="scheduler:dream",
        task_id="task-scheduler",
    )

    out = state.request_operation(
        session_id="session-scheduler",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-scheduler",
        node_id="system.dream",
        task_id="task-scheduler",
    )

    # Why: scheduler-triggered tasks have no Discord approval route. How: the
    # supervisor returns an auto decision instead of creating an approval event.
    # Purpose: scheduled maintenance jobs do not block forever on silent approvals.
    assert out.safety_level == SafetyLevel.auto
    assert out.reason == "auto-approved: scheduler task"
    assert out.approval_id is None
    assert state.approvals == {}
    assert state.eventlog.list_events(session_id="session-scheduler", after_seq=0) == []


def test_scheduler_task_does_not_bypass_denied_command(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_task_to_session(
        state,
        session_id="session-scheduler",
        conversation_key="scheduler:dream",
        task_id="task-scheduler",
    )

    out = state.request_operation(
        session_id="session-scheduler",
        op="execute_command",
        parameters={"command": "rm -rf /"},
        tool_call_id="call-scheduler",
        node_id="system.dream",
        task_id="task-scheduler",
    )

    # Why: the scheduler bypass must only replace human approval, not hard policy
    # denial. How: denied commands return SafetyLevel.deny before scheduler
    # auto-approval is considered. Purpose: destructive commands remain blocked.
    assert out.safety_level == SafetyLevel.deny
    assert out.approval_id is None
    assert state.approvals == {}


def _tool_context(
    workspace: Path, registry: ToolRegistry, http: httpx.AsyncClient, tool_call_id: str,
) -> ToolContext:
    return ToolContext(
        supervisor_url="http://supervisor.invalid",
        session_id="session-script",
        run_id="engine-run-shared-by-many-calls",
        worker_id="worker-test",
        workspace_root=workspace,
        http=http,
        registry=registry,
        task_id="task-script",
        tool_call_id=tool_call_id,
        node_id="node-script",
    )


def test_real_tool_context_registry_injects_only_explicit_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "identity_probe.py").write_text(
        "SPEC = {'name': 'identity_probe', 'description': 'probe', "
        "'input_schema': {'type': 'object', 'properties': {}}}\n"
        "if __name__ == '__main__':\n"
        " import json, os, sys\n"
        " json.load(sys.stdin)\n"
        " names = ('CLONOTH_TOOL_CALL_ID', 'CLONOTH_REQUEST_ID', "
        "'CLONOTH_TOOL_INVOCATION_ID', 'CLONOTH_UNRELATED_PROVIDER_CONTEXT')\n"
        " values = {name: os.environ[name] for name in names if name in os.environ}\n"
        " print(json.dumps({'ok': True, 'data': {'result': 'ok', 'env': values}}))\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir)
    monkeypatch.setenv("CLONOTH_TOOL_CALL_ID", "stale-parent-call")
    monkeypatch.setenv("CLONOTH_REQUEST_ID", "stale-parent-request")
    monkeypatch.setenv("CLONOTH_TOOL_INVOCATION_ID", "stale-parent-invocation")

    async def exercise() -> None:
        async with httpx.AsyncClient() as http:
            ctx = _tool_context(tmp_path, registry, http, "provider-call-17")
            ctx.request_id = "provider-request-4"  # type: ignore[attr-defined]
            ctx.invocation_id = "provider-invocation-9"  # type: ignore[attr-defined]
            ctx.unrelated_provider_context = "must-not-leak"  # type: ignore[attr-defined]
            result = await registry.execute(name="identity_probe", arguments={}, ctx=ctx)
            empty_ctx = _tool_context(tmp_path, registry, http, "")
            empty_result = await registry.execute(
                name="identity_probe", arguments={}, ctx=empty_ctx,
            )
        assert result["data"]["env"] == {
            "CLONOTH_TOOL_CALL_ID": "provider-call-17",
            "CLONOTH_REQUEST_ID": "provider-request-4",
            "CLONOTH_TOOL_INVOCATION_ID": "provider-invocation-9",
        }
        assert empty_result["data"]["env"] == {}

    asyncio.run(exercise())


def test_qq_forward_registry_replay_uses_provider_tool_call_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    received_ids: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP contract
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            received_ids.append(str(payload["request_id"]))
            body = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ONEBOT_FORWARD_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("ONEBOT_FORWARD_BRIDGE_PORT", str(server.server_port))
    monkeypatch.setenv("ONEBOT_FORWARD_HTTP_ATTEMPTS", "1")
    registry = ToolRegistry(workspace_root=root, tools_dir=root / "tools")
    arguments = {"op": "remind", "target_type": "self", "text": "same"}

    async def exercise() -> None:
        async with httpx.AsyncClient() as http:
            ctx = _tool_context(root, registry, http, "provider-call-same")
            first = await registry.execute(name="qq_forward", arguments=arguments, ctx=ctx)
            second = await registry.execute(name="qq_forward", arguments=arguments, ctx=ctx)
            ctx.tool_call_id = "provider-call-different"
            third = await registry.execute(name="qq_forward", arguments=arguments, ctx=ctx)
        assert first["ok"] and second["ok"] and third["ok"]

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(received_ids) == 3
    assert received_ids[0] == received_ids[1]
    assert received_ids[0] != received_ids[2]
