"""推理循环测试共用的脚手架：可编排的工具注册表、记事件的 RunContext、直接拼出的循环状态和跑整个节点的入口。"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable
from unittest.mock import AsyncMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.context import RunContext  # noqa: E402
from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.hooks.registry import HookRegistry  # noqa: E402
from engine.inference import ai_step  # noqa: E402
from engine.inference.loop_state import _LoopState  # noqa: E402
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import TaskAction  # noqa: E402
from providers.base import ProviderResponse, ToolCall  # noqa: E402

ToolFunc = Callable[[dict[str, Any], Any], Awaitable[Any]]

TASK_ID = "task-1"
SESSION_ID = "session-1"


def ok(text: str) -> dict[str, Any]:
    return {"ok": True, "data": {"result": text}}


def failed(text: str) -> dict[str, Any]:
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


def cancelled() -> dict[str, Any]:
    return {"ok": False, "error": "task cancelled", "cancelled": True}


def echo(text: str = "done") -> ToolFunc:
    async def tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return ok(text)

    return tool


class ScriptedRegistry:
    """注册表替身：工具实现和额外的 spec 字段由测试给出，每次执行都记下 (工具名, 参数, ctx)。"""

    def __init__(
        self,
        tools: dict[str, ToolFunc] | None = None,
        specs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.tools: dict[str, ToolFunc] = dict(tools or {})
        self.specs: dict[str, dict[str, Any]] = {name: dict(spec) for name, spec in (specs or {}).items()}
        self.executed: list[tuple[str, dict[str, Any], Any]] = []

    @property
    def names(self) -> list[str]:
        return list(dict.fromkeys([*self.tools, *self.specs]))

    def _spec(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": f"{name} tool",
            "input_schema": {"type": "object", "properties": {}},
            **self.specs.get(name, {}),
        }

    def list_specs(self) -> list[dict[str, Any]]:
        return [self._spec(name) for name in self.names]

    def get_spec(self, name: str) -> dict[str, Any] | None:
        return self._spec(name) if name in self.names else None

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        self.executed.append((name, dict(arguments), ctx))
        tool = self.tools.get(name)
        if tool is None:
            return failed(f"tool not found: {name}")
        return await tool(dict(arguments), ctx)

    def executed_names(self) -> list[str]:
        return [name for name, _arguments, _ctx in self.executed]


class EventRecorder:
    """顶替 RunContext.emit_event：按顺序记下事件，一律当作 supervisor 收下了。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, payload: dict[str, Any]) -> bool:
        self.events.append((event_type, dict(payload)))
        return True

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [payload for kind, payload in self.events if kind == event_type]


def make_rctx(
    workspace: Path,
    *,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    parent_session_id: str = "",
    child_session_id: str = "",
    task_context: dict[str, Any] | None = None,
) -> RunContext:
    rctx = RunContext(
        workspace_root=workspace,
        supervisor_url="http://supervisor.invalid",
        session_id=session_id,
        worker_id="worker-1",
        http=SimpleNamespace(),  # type: ignore[arg-type]
        llm_http=SimpleNamespace(),  # type: ignore[arg-type]
        parent_session_id=parent_session_id,
        task_id=task_id,
        task_context=dict(task_context or {}),
        child_session_id=child_session_id,
    )
    recorder = EventRecorder()
    rctx.events = recorder  # type: ignore[attr-defined]
    rctx.emit_event = recorder  # type: ignore[method-assign]
    rctx.check_cancelled = AsyncMock(return_value=False)  # type: ignore[method-assign]
    rctx.check_preempted = AsyncMock(return_value={"preempted": False})  # type: ignore[method-assign]
    rctx.conversation_store = ConversationStore(workspace / "data" / "conversations")  # type: ignore[attr-defined]
    return rctx


def make_loop_state(
    workspace: Path,
    registry: ScriptedRegistry,
    *,
    tool_mode: str = "native",
    runtime_cfg: dict[str, Any] | None = None,
    allowed: Iterable[str] | None = None,
    rctx: RunContext | None = None,
    messages: list[dict[str, Any]] | None = None,
    node: Node | None = None,
) -> _LoopState:
    node = node or Node(id="node-1", type="ai", tool_mode=tool_mode)
    return _LoopState(
        rctx=rctx or make_rctx(workspace),
        node=node,
        provider=SimpleNamespace(name="test"),  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        run_id="run-1",
        context_ref="",
        runtime_cfg=dict(runtime_cfg or {}),
        streaming=False,
        messages=list(messages or []),
        system_prompt=[],
        is_block_mode=False,
        openai_tools=[],
        history=[],
        collected_attachments=[],
        tool_produced_attachments=[],
        formatter=create_tool_formatter(node.tool_mode),
        allowed_real_tools=set(allowed) if allowed is not None else set(registry.names),
    )


def use_hooks(monkeypatch: Any, handlers: dict[str, list[Any]] | None = None) -> HookRegistry:
    """换掉 ai_step 用的 hook 表，只挂测试点名的 handler。"""
    hooks = HookRegistry()
    for point, items in (handlers or {}).items():
        for handler in items:
            hooks.register(point, handler)
    monkeypatch.setattr(ai_step, "hook_registry", hooks)
    return hooks


def call(name: str, arguments: dict[str, Any] | None = None, *, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=dict(arguments or {}))


def turn(*tool_calls: ToolCall, text: str | None = None) -> ProviderResponse:
    return ProviderResponse(ok=True, text=text, tool_calls=list(tool_calls))


def real_calls(*tool_calls: ToolCall) -> list[dict[str, Any]]:
    """_execute_real_tools 吃的形状。"""
    return [{"id": item.id, "name": item.name, "arguments": dict(item.arguments)} for item in tool_calls]


def results_for(messages: list[dict[str, Any]], call_id: str) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("role") == "tool" and message.get("tool_call_id") == call_id]


def answered_once(messages: list[dict[str, Any]]) -> bool:
    """每个 assistant 工具调用后面恰好跟着一条同 id 的结果：native 供应商就认这个。"""
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for item in message.get("tool_calls") or []:
            later = messages[index + 1:]
            if sum(1 for other in later if other.get("role") == "tool" and other.get("tool_call_id") == item["id"]) != 1:
                return False
    return True


def stored(workspace: Path, session_id: str = SESSION_ID) -> list[Message]:
    return ConversationStore(workspace / "data" / "conversations").load(session_id)


class NodeRun:
    """跑一整个 run_ai_node：模型回复按脚本给，每次请求时的消息和 tools 各留一份深拷贝。"""

    def __init__(
        self,
        monkeypatch: Any,
        workspace: Path,
        *,
        registry: ScriptedRegistry,
        node: Node | None = None,
        runtime_cfg: dict[str, Any] | None = None,
        task_context: dict[str, Any] | None = None,
        handlers: dict[str, list[Any]] | None = None,
        rctx: RunContext | None = None,
    ) -> None:
        self.monkeypatch = monkeypatch
        self.workspace = workspace
        self.registry = registry
        self.node = node or Node(
            id="node-1", type="ai", prompt="Work carefully.", tool_mode="native",
            tool_access=ToolAccess(mode="all"),
        )
        self.runtime_cfg = copy.deepcopy(runtime_cfg or {})
        self.rctx = rctx or make_rctx(workspace, task_context=task_context)
        self.hooks = use_hooks(monkeypatch, handlers)
        self.requests: list[dict[str, Any]] = []
        monkeypatch.setattr(ai_step, "auto_discover_and_register", lambda *args, **kwargs: None)
        monkeypatch.setattr(ai_step, "load_external_plugins", lambda *args, **kwargs: None)
        monkeypatch.setattr(ai_step, "load_runtime_config", lambda *args, **kwargs: copy.deepcopy(self.runtime_cfg))

    def run(
        self,
        *responses: ProviderResponse,
        instruction: str = "do the work",
        resume_data: dict[str, Any] | None = None,
        **loop_options: Any,
    ) -> TaskAction:
        """loop_options 原样交给 run_ai_node，比如 steps_used、run_budget、carried_todos。"""
        queue = list(responses)

        async def provider_call(ls: Any, step: int) -> ProviderResponse:
            self.requests.append({
                "step": step,
                "messages": copy.deepcopy(ls.messages),
                "tools": copy.deepcopy(ls.openai_tools),
            })
            return queue.pop(0)

        self.monkeypatch.setattr(ai_step, "_call_llm_with_retry", provider_call)
        return asyncio.run(ai_step.run_ai_node(
            rctx=self.rctx,
            provider=SimpleNamespace(name="test"),  # type: ignore[arg-type]
            registry=self.registry,  # type: ignore[arg-type]
            node=self.node,
            instruction=instruction,
            history=[],
            resume_data=resume_data,
            **loop_options,
        ))

    def tool_names(self, request_index: int) -> list[str]:
        names = []
        for tool in self.requests[request_index]["tools"]:
            function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            names.append(str(function.get("name") or tool.get("name") or ""))
        return names
