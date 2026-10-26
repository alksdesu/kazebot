"""工具结果进上下文前的上限：脚本、MCP 结果的保险上限截完不超上限、比原文短、省略数准确；
普通推理、批次里的独立工具任务、异步回传都在 engine 落盘换成带 ref 的预览；计划执行的工具步骤存成摘录。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _path in (_ROOT, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import supervisor.execution.service as execution_service  # noqa: E402
from _engine_loop_fixture import ScriptedRegistry, call, make_loop_state, make_rctx, real_calls, use_hooks  # noqa: E402
from engine.inference.ai_step import _execute_real_tools, _resume_data_for_context, _run_async_deliver  # noqa: E402
from engine.inference.loop_state import compact_target_session_id  # noqa: E402
from engine.inference.resume_builder import _build_resume_messages  # noqa: E402
from engine.runner import _run_tool_task  # noqa: E402
from engine.tool_outputs import LATEST_ROUND_VIEW_CHARS, ToolOutputStore, tool_output_limits  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.execution import ExecutionService  # noqa: E402
from supervisor.execution.models import PlanDraft  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402
from toolbox._common import ProcessOutcome, StreamCapture  # noqa: E402
from toolbox.registry import _SCRIPT_RESULT_LIMIT_CHARS, ToolRegistry, _clip_result, _clip_text, _script_response  # noqa: E402

_NOTE = re.compile(r"\n\.\.\.<truncated (\d+) chars>\Z")
_REF = re.compile(r"ref=(to_[0-9a-f]{24})")


def _numbered(count: int) -> str:
    return "\n".join(f"line-{index:05d} " + "x" * 40 for index in range(1, count + 1))


def _split_clipped(text: str) -> tuple[str, int]:
    """截过的文本拆成 (留下的开头, 说明里写的省略字数)。"""
    match = _NOTE.search(text)
    assert match is not None, text[-120:]
    return text[:match.start()], int(match.group(1))


def _ref(text: str) -> str:
    match = _REF.search(text)
    assert match is not None, text[:300]
    return match.group(1)


def _supervisor_state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="output-bounds"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _complete(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "worker-1"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)


class TestClipText:
    @pytest.mark.parametrize("limit", [40, 100, 1000, 16_000])
    @pytest.mark.parametrize("extra", [1, 2, 9, 30, 1000])
    def test_a_clipped_text_fits_the_limit_is_shorter_and_counts_what_it_dropped(self, limit: int, extra: int) -> None:
        text = "".join(chr(0x4E00 + index % 300) for index in range(limit + extra))

        clipped = _clip_text(text, limit)

        kept, omitted = _split_clipped(clipped)
        assert len(clipped) <= limit
        assert len(clipped) < len(text)
        assert text.startswith(kept)
        assert omitted == len(text) - len(kept)

    def test_just_over_the_default_limit_comes_out_shorter_than_the_original(self) -> None:
        text = "y" * (_SCRIPT_RESULT_LIMIT_CHARS + 10)

        clipped = _clip_text(text, _SCRIPT_RESULT_LIMIT_CHARS)

        kept, omitted = _split_clipped(clipped)
        assert len(clipped) <= _SCRIPT_RESULT_LIMIT_CHARS < len(text)
        assert omitted == len(text) - len(kept)

    @pytest.mark.parametrize("length", [0, 1, 99, 100])
    def test_text_within_the_limit_is_returned_as_is(self, length: int) -> None:
        text = "w" * length

        assert _clip_text(text, 100) is text

    @pytest.mark.parametrize("limit", [0, 5, 20])
    def test_a_limit_too_small_for_the_note_cuts_without_one(self, limit: int) -> None:
        text = "z" * 100

        assert _clip_text(text, limit) == text[:limit]


class TestClipResult:
    def test_only_the_text_for_the_model_is_clipped(self) -> None:
        attachments = [{"path": "out.png"}]
        response = {
            "ok": True,
            "data": {"result": "r" * 120, "attachments": attachments, "value": {"k": 1}},
            "attachments": attachments,
        }

        clipped = _clip_result(response, limit=100)

        assert len(clipped["data"]["result"]) <= 100
        assert clipped["data"]["attachments"] is attachments
        assert clipped["data"]["value"] == {"k": 1}
        assert clipped["attachments"] is attachments
        assert response["data"]["result"] == "r" * 120

    def test_an_output_field_holding_the_same_text_is_clipped_with_it(self) -> None:
        text = "o" * 150

        clipped = _clip_result({"ok": True, "data": {"result": text, "output": text}}, limit=100)

        assert clipped["data"]["output"] == clipped["data"]["result"]
        assert len(clipped["data"]["output"]) <= 100

    def test_a_different_output_field_is_left_alone(self) -> None:
        clipped = _clip_result({"ok": True, "data": {"result": "o" * 150, "output": "raw"}}, limit=100)

        assert clipped["data"]["output"] == "raw"

    @pytest.mark.parametrize("response", [
        {"ok": True, "data": {"result": "short"}},
        {"ok": True, "data": {"result": 12345}},
        {"ok": True, "data": "not a mapping"},
        {"ok": True},
    ])
    def test_nothing_to_clip_gives_back_the_same_object(self, response: dict[str, Any]) -> None:
        assert _clip_result(response, limit=100) is response

    def test_the_default_cap_leaves_long_results_for_the_engine_to_spill(self) -> None:
        response = {"ok": True, "data": {"result": "a" * (tool_output_limits({}).max_inline_chars * 4)}}

        assert _clip_result(response) is response


class TestNormalInference:
    def test_a_long_script_result_reaches_the_engine_whole_and_is_spilled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        use_hooks(monkeypatch)
        text = _numbered(1500)
        assert len(text) > tool_output_limits({}).max_inline_chars
        stdout = json.dumps({"ok": True, "data": {"result": text}}).encode("utf-8")
        response = _script_response(ProcessOutcome(0, StreamCapture(head=stdout)))
        assert response["data"]["result"] == text

        async def long_script(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return response

        rctx = make_rctx(tmp_path, session_id="branch-1", parent_session_id="parent-1")
        ls = make_loop_state(tmp_path, ScriptedRegistry(tools={"long_script": long_script}), rctx=rctx)

        asyncio.run(_execute_real_tools(ls, real_calls(call("long_script", {}, call_id="s")), step=1))

        content = ls.messages[-1]["content"]
        assert ToolOutputStore(tmp_path).load(compact_target_session_id(ls), _ref(content)) == text
        assert len(content) <= LATEST_ROUND_VIEW_CHARS


class _AsyncHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> Any:
        self.posts.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200)


def _deliver_async(workspace: Path | None, **outcome: Any) -> dict[str, Any]:
    http = _AsyncHttp()
    asyncio.run(_run_async_deliver(
        registry=SimpleNamespace(get_spec=lambda name: None),
        http=http,
        supervisor_url="http://supervisor",
        task_id="task-1",
        session_id="parent-session",
        tool_name="demo",
        tool_args={},
        async_tool_id="async-1",
        started_at=time.monotonic(),
        node_id="node-a",
        workspace_root=workspace,
        **outcome,
    ))
    return http.posts[-1]["json"]


class TestAsyncFailures:
    def test_a_long_exception_is_saved_and_previewed(self, tmp_path: Path) -> None:
        failure = _numbered(1500)

        payload = _deliver_async(tmp_path, error=RuntimeError(failure))

        message = payload["message"]
        # 状态行原样留着：finish_guard 按这一行认异步结果
        assert message.startswith('❌ Async tool "demo" (id: async-1) failed: ')
        assert ToolOutputStore(tmp_path).load("parent-session", _ref(message)) == failure
        assert len(message) < 9000
        assert payload["success"] is False

    def test_a_long_tool_error_is_saved_and_previewed(self, tmp_path: Path) -> None:
        failure = _numbered(1500)

        payload = _deliver_async(tmp_path, result={"ok": False, "error": failure, "data": {"result": f"ERROR: {failure}"}})

        message = payload["message"]
        assert message.startswith('❌ Async tool "demo" (id: async-1) failed in ')
        assert ToolOutputStore(tmp_path).load("parent-session", _ref(message)) == failure
        assert len(message) < 9000

    def test_a_short_failure_is_shown_as_it_is(self, tmp_path: Path) -> None:
        payload = _deliver_async(tmp_path, error=RuntimeError("disk full"))

        assert "failed: disk full\n" in payload["message"]
        assert not (tmp_path / "data" / "tool_outputs").exists()

    def test_without_a_workspace_a_long_failure_is_still_bounded(self) -> None:
        payload = _deliver_async(None, error=RuntimeError(_numbered(1500)))

        assert "ref=" not in payload["message"]
        assert len(payload["message"]) < 9000

    def test_the_supervisor_keeps_only_a_short_error_beside_the_message(self, tmp_path: Path) -> None:
        state = _supervisor_state(tmp_path)
        session = state.get_or_create_session(channel="qq", conversation_key="qq_group:1001")

        outcome = state.inject_async_result(session, "previewed message", tool_name="demo", success=False, error="e" * 5000)

        assert outcome["strategy"] == "inbound"
        [event] = [
            event for event in state.eventlog.events
            if event["type"] == "inbound_message" and event["payload"].get("text") == "previewed message"
        ]
        assert len(event["payload"]["route_hints"]["async_tool_error"]) == 1000


def _orchestrator(workspace: Path, *tools: str) -> None:
    nodes = workspace / "config" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    allowed = ", ".join(tools)
    (nodes / "test.orchestrator.yaml").write_text(
        f"id: test.orchestrator\ntype: ai\ntool_access:\n  mode: allowlist\n  allow: [{allowed}]\n", encoding="utf-8",
    )


def _waiting_caller(state: SupervisorState, session_id: str, *items: dict[str, Any]) -> tuple[Task, list[Task]]:
    with state._lock:
        caller = state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": "qq_group:1001", "channel": "qq"}},
            continuation={},
            source_inbound_seq=None,
        )
        caller.status = TaskStatus.completed
        state._route_dispatch_locked(caller, {"action": "dispatch", "dispatch_batch": list(items)})
    children = sorted(
        (task for task in state.tasks.values() if task.caller_task_id == caller.task_id),
        key=lambda task: task.batch_index,
    )
    return caller, children


async def _run_child_tool(registry: ToolRegistry, workspace: Path, child: Task) -> dict[str, Any]:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient(transport=transport) as http:
        return await _run_tool_task(
            http=http, sup_url="http://supervisor", ws_root=workspace, registry=registry,
            task=child.model_dump(mode="json"), worker_id="worker-1", session_id=child.session_id,
            session_generation=child.session_generation, task_id=child.task_id,
        )


class TestStandaloneToolTasks:
    def test_a_long_result_travels_whole_and_reaches_the_model_as_a_preview(self, tmp_path: Path) -> None:
        text = _numbered(1500)
        _orchestrator(tmp_path, "probe")
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)

        async def probe(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True, "data": {"result": text}}

        registry.register_builtin_tool("probe", "probe tool", {"type": "object", "properties": {}}, probe)
        state = _supervisor_state(tmp_path)
        session = state.get_or_create_session(channel="qq", conversation_key="qq_group:1001")
        caller, [child] = _waiting_caller(
            state, session, {"kind": "tool", "target": "probe", "arguments": {"q": 1}, "call_id": "t1"},
        )
        assert child.kind == TaskKind.tool

        result = asyncio.run(_run_child_tool(registry, tmp_path, child))
        # 独立工具任务和 supervisor 打包批次结果都不截：全文得送到 engine 那边落盘，模型才翻得到
        assert result["result"]["raw_inline"] == text
        _complete(state, child, result)
        [entry] = caller.input["resume_data"]["entries"]
        assert entry["raw_inline"] == text

        rctx = SimpleNamespace(workspace_root=tmp_path, child_session_id="", parent_session_id="", session_id=session)
        shown = asyncio.run(_resume_data_for_context(caller.input["resume_data"], rctx, {}))
        [message] = _build_resume_messages(shown)

        assert message["content"].startswith('Tool result for "probe":\n')
        assert ToolOutputStore(tmp_path).load(session, _ref(message["content"])) == text
        assert len(message["content"]) <= LATEST_ROUND_VIEW_CHARS + len('Tool result for "probe":\n')
        assert caller.input["resume_data"]["entries"][0]["raw_inline"] == text


_ACTOR = FeatureActor(scope="qq_group:test", owner="alice", bot_scope="bot", channel="qq_group")


@pytest.fixture
def plan_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "test.entry.yaml").write_text(
        "id: test.entry\ntype: ai\ntool_access:\n  mode: allowlist\n  allow: [read_file]\n", encoding="utf-8",
    )
    state = _supervisor_state(tmp_path)
    monkeypatch.setattr(state, "_default_entry_node", lambda: "test.entry")
    state.execution = ExecutionService(state)
    yield state
    state.execution.close()


def _tool_step() -> dict[str, Any]:
    return {"id": "read", "title": "读取", "kind": "tool", "operation": "read_file", "arguments": {"path": "data/big.txt"}}


def _started_plan(state: SupervisorState, *steps: dict[str, Any]) -> str:
    plan = state.execution.create(_ACTOR, PlanDraft(goal="整理大文件", steps=list(steps)))
    state.execution.confirm(_ACTOR, plan["id"], 1)
    state.execution.tick()
    return plan["id"]


def _finish_step(state: SupervisorState, plan_id: str, index: int, result: dict[str, Any]) -> None:
    task_id = state.execution.get(_ACTOR, plan_id)["steps"][index]["task_id"]
    _complete(state, state.tasks[task_id], result)


class TestPlanToolSteps:
    def test_a_long_tool_result_is_stored_as_an_excerpt_and_handed_on_as_one(self, plan_state: SupervisorState) -> None:
        text = "".join(f"line {index:06d} " + "z" * 40 + "\n" for index in range(6000))
        assert len(text) > execution_service._STEP_TEXT_MAX_CHARS
        plan_id = _started_plan(plan_state, _tool_step(), {
            "id": "digest", "title": "整理", "kind": "model", "dependencies": ["read"], "instruction": "提炼要点",
        })

        _finish_step(plan_state, plan_id, 0, {"action": "finish", "result": {"text": text, "tool_ok": True}})
        plan_state.execution.tick()

        read, digest = plan_state.execution.get(_ACTOR, plan_id)["steps"]
        stored = read["result"]
        assert read["status"] == "succeeded"
        assert len(stored) <= execution_service._STEP_TEXT_MAX_CHARS
        assert stored.startswith("line 000000 ")
        assert stored.endswith(f"line {5999:06d} " + "z" * 40 + "\n")
        assert "中间省略" in stored
        instruction = plan_state.tasks[digest["task_id"]].input["instruction"]
        assert stored in instruction
        assert len(instruction) < len(text)

    def test_a_long_failure_still_counts_as_one_and_its_error_is_short(self, plan_state: SupervisorState) -> None:
        reason = "quota exceeded " + "q" * 5000
        text = json.dumps({"ok": False, "error": reason, "padding": "p" * 300_000})
        plan_id = _started_plan(plan_state, _tool_step())

        _finish_step(plan_state, plan_id, 0, {"action": "finish", "result": {"text": text}})

        [read] = plan_state.execution.get(_ACTOR, plan_id)["steps"]
        # 认失败用的是截之前的全文：截成摘录的 JSON 解析不出 ok=false
        assert read["status"] == "failed"
        assert read["error"].startswith("quota exceeded ")
        assert len(read["error"]) <= execution_service._STEP_ERROR_MAX_CHARS
        assert len(read["result"]) <= execution_service._STEP_TEXT_MAX_CHARS
        updates = [event["payload"] for event in plan_state.eventlog.events if event["type"] == "execution_updated"]
        assert all(len(step["error"] or "") <= execution_service._STEP_ERROR_MAX_CHARS for update in updates for step in update["steps"])

    def test_a_short_result_is_stored_unchanged(self, plan_state: SupervisorState) -> None:
        plan_id = _started_plan(plan_state, _tool_step())

        _finish_step(plan_state, plan_id, 0, {"action": "finish", "result": {"text": "small file", "tool_ok": True}})

        [read] = plan_state.execution.get(_ACTOR, plan_id)["steps"]
        assert (read["status"], read["result"]) == ("succeeded", "small file")
