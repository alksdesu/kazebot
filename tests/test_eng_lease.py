"""领取凭据：心跳、完成回报、取消查询都带上 claim_id；supervisor 不认这次领取时本地按用户取消的路径收手。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from engine import runner
from engine.context import RunContext, TaskLease
from engine.node import Node


class _Response:
    def __init__(self, status_code: int = 200, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Http:
    """按顺序回放预设应答，记下每一次 POST。"""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, object]] = []

    async def post(self, url: str, *, json: object = None, **_kwargs: object) -> object:
        self.calls.append((url, json))
        answer = self.answers.pop(0) if self.answers else _Response(200, {"ok": True})
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.parametrize("response, reason", [
    (_Response(404), "HTTP 404"),
    (_Response(409), "HTTP 409"),
    (_Response(410), "HTTP 410"),
    (_Response(200, {"ok": False, "reason": "stale_claim"}), "stale_claim"),
    (_Response(200, {"ok": False}), "rejected"),
    (_Response(200, {"ok": True}), ""),
    (_Response(500, {"ok": False}), ""),
    (_Response(200, ValueError("not json")), ""),
])
def test_only_a_definite_refusal_counts_as_a_lost_lease(response: _Response, reason: str) -> None:
    assert runner._lease_lost_reason(response) == reason


def test_the_heartbeat_sends_the_claim_and_stops_once_the_claim_is_gone() -> None:
    http = _Http(
        OSError("supervisor restarting"),
        _Response(200, {"ok": True}),
        _Response(200, {"ok": False, "reason": "stale_claim"}),
    )
    lease = TaskLease(task_id="task-1", worker_id="worker-1", claim_id="claim-1")

    asyncio.run(asyncio.wait_for(runner._heartbeat(http, "http://sup", lease, interval=0), timeout=5))

    assert lease.lost and lease.lost_reason == "stale_claim"
    assert len(http.calls) == 3
    url, body = http.calls[-1]
    assert url == "http://sup/v1/tasks/task-1/renew_lease"
    assert body == {"worker_id": "worker-1", "lease_sec": 120.0, "claim_id": "claim-1"}


def _run_context(tmp_path: Path, http: httpx.AsyncClient, lease: TaskLease | None) -> RunContext:
    return RunContext(
        workspace_root=tmp_path, supervisor_url="http://sup", session_id="session", worker_id="worker-1",
        http=http, llm_http=http, task_id="task-1", lease=lease,
    )


def test_a_lost_lease_reads_as_cancelled_without_asking_the_supervisor(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"cancelled": False})

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            lease = TaskLease("task-1", "worker-1", "claim-1", lost_reason="stale_claim")
            return await _run_context(tmp_path, client, lease).check_cancelled()

    assert asyncio.run(run()) is True
    assert requests == []


def test_the_cancel_probe_carries_the_claim(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"cancelled": True})

    async def run() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await _run_context(tmp_path, client, TaskLease("task-1", "worker-1", "claim-1")).check_cancelled()

    assert asyncio.run(run()) is True
    assert requests[0].url.path == "/v1/tasks/task-1/cancelled"
    assert requests[0].url.params["claim_id"] == "claim-1"


def _task(kind: str = "node") -> dict:
    return {
        "task_id": "task-1", "kind": kind, "session_id": "session", "session_generation": 1,
        "claim_id": "claim-1", "node_id": "entry",
    }


def _patch_runner(monkeypatch: pytest.MonkeyPatch, seen: dict) -> None:
    async def secret(http: object, url: str) -> dict:
        return {"api_key": "key", "base_url": "http://model.invalid", "model": "test"}

    async def node_task(**kwargs: object) -> dict:
        seen["lease"] = kwargs["lease"]
        return {"action": "finish", "node_id": "entry"}

    async def idle_heartbeat(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "fetch_openai_secret", secret)
    monkeypatch.setattr(runner, "_run_node_task", node_task)
    monkeypatch.setattr(runner, "_heartbeat", idle_heartbeat)


def test_the_completion_carries_the_claim_and_a_superseded_claim_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen: dict = {}
    _patch_runner(monkeypatch, seen)
    http = _Http(_Response(409, {"detail": "stale claim"}))

    asyncio.run(runner._handle_task(http, http, "http://sup", tmp_path, None, _task(), "worker-1"))

    assert seen["lease"].claim_id == "claim-1"
    assert len(http.calls) == 1
    url, body = http.calls[0]
    assert url == "http://sup/v1/tasks/task-1/complete"
    assert body["claim_id"] == "claim-1" and body["worker_id"] == "worker-1"
    assert body["result"]["action"] == "finish"


def test_server_errors_on_completion_are_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runner(monkeypatch, {})
    original_sleep = asyncio.sleep

    async def no_sleep(delay: float, *args: object) -> None:
        await original_sleep(0)

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    http = _Http(_Response(503), OSError("reset"), _Response(200, {"ok": True, "duplicate": True}))

    asyncio.run(runner._handle_task(http, http, "http://sup", tmp_path, None, _task(), "worker-1"))

    assert len(http.calls) == 3
    assert all(body["claim_id"] == "claim-1" for _url, body in http.calls)


def test_a_failing_channel_lookup_is_still_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen: dict = {}
    _patch_runner(monkeypatch, seen)

    async def broken_secret(http: object, url: str) -> dict:
        raise httpx.ConnectError("supervisor down")

    monkeypatch.setattr(runner, "fetch_openai_secret", broken_secret)
    http = _Http(_Response(200, {"ok": True}))

    asyncio.run(runner._handle_task(http, http, "http://sup", tmp_path, None, _task(), "worker-1"))

    assert "lease" not in seen
    assert http.calls[0][1]["result"]["action"] == "fail"


def test_tool_tasks_get_the_lease_and_never_fetch_the_model_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen: dict = {}
    _patch_runner(monkeypatch, seen)

    async def unexpected(*_args: object) -> dict:
        raise AssertionError("tool tasks must not fetch the model channel")

    async def tool_task(**kwargs: object) -> dict:
        seen["lease"] = kwargs["lease"]
        return {"action": "finish", "node_id": "demo"}

    monkeypatch.setattr(runner, "fetch_openai_secret", unexpected)
    monkeypatch.setattr(runner, "_run_tool_task", tool_task)
    http = _Http(_Response(200, {"ok": True}))

    asyncio.run(runner._handle_task(http, http, "http://sup", tmp_path, None, _task("tool"), "worker-1"))

    assert seen["lease"].claim_id == "claim-1"
    assert http.calls[0][1]["result"]["action"] == "finish"


class _ToolRegistry:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.contexts: list = []

    async def execute(self, *, name: str, arguments: dict, ctx: object) -> dict:
        self.contexts.append(ctx)
        return self.result

    def get_spec(self, name: str) -> None:
        return None


def _run_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: _ToolRegistry) -> dict:
    monkeypatch.setattr(runner, "load_node", lambda root, node_id: SimpleNamespace(id=node_id))
    monkeypatch.setattr(runner, "tool_allowed", lambda node, name: True)
    task = {"task_id": "task-1", "tool_name": "demo", "input": {"origin_node_id": "origin", "arguments": {}}}
    return asyncio.run(runner._run_tool_task(
        http=_Http(), sup_url="http://sup", ws_root=tmp_path, registry=registry, task=task,
        worker_id="worker-1", session_id="session", session_generation=1, task_id="task-1",
        lease=TaskLease("task-1", "worker-1", "claim-1"),
    ))


def test_tool_tasks_hand_the_claim_to_the_tool_and_screen_its_attachments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "out.png").write_bytes(b"png")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secrets.env").write_text("KEY=1", encoding="utf-8")
    registry = _ToolRegistry({"ok": True, "data": {"result": "done", "attachments": [
        {"type": "image", "path": "data/out.png"},
        {"type": "file", "path": "config/secrets.env"},
        {"type": "file", "path": "data/../config/secrets.env"},
    ]}})

    result = _run_tool(monkeypatch, tmp_path, registry)

    assert registry.contexts[0].claim_id == "claim-1"
    assert result["action"] == "finish"
    assert result["result"]["tool_ok"] is True
    assert result["result"]["attachments"] == [{"type": "image", "path": "data/out.png"}]


def test_a_cancelled_tool_result_ends_the_tool_task_as_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    registry = _ToolRegistry({"ok": False, "error": "task cancelled", "data": {"cancelled": True}})

    assert _run_tool(monkeypatch, tmp_path, registry)["action"] == "cancelled"


def test_a_missing_key_is_judged_on_the_resolved_channel_before_history_is_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def no_override(*_args: object) -> dict:
        return {}

    monkeypatch.setattr(runner, "load_node", lambda *_args: Node(id="entry", type="ai", prompt="test"))
    monkeypatch.setattr(runner, "_fetch_session_provider_override", no_override)
    monkeypatch.setattr(runner, "resolve_provider", lambda *_args, **_kwargs: SimpleNamespace(
        provider_type="openai", api_key=None, base_url=None, model="test",
        channel_name="backup", channel_options={},
    ))
    task = {"task_id": "task-1", "node_id": "entry", "input": {
        "child_session_id": "child", "context_mode": "fork", "fork_from_session_id": "parent",
    }}

    result = asyncio.run(runner._run_node_task(
        http=_Http(), llm_http=_Http(), sup_url="http://sup", ws_root=tmp_path, registry=None, task=task,
        worker_id="worker-1", session_id="parent", session_generation=1,
        api_key="", base_url="", default_model="test",
    ))

    assert result["action"] == "fail"
    assert "backup" in result["error"] and "api_key" in result["error"]
    assert not (tmp_path / "data" / "conversations" / "child.jsonl").exists()


def test_a_channel_key_is_enough_when_the_main_channel_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    created = []

    class _Provider:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    monkeypatch.setattr(runner.provider_registry, "get", lambda name: _Provider)
    rp = SimpleNamespace(provider_type="openai", api_key="channel-key", base_url="http://channel", model="m", channel_name="c")

    runner._create_provider_from_registry(rp, llm_http=None, api_key="", base_url="", provider_options={})

    assert created and created[0]["api_key"] == "channel-key"
