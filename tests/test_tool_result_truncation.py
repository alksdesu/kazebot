from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# Why: this focused regression test may be run directly from the checkout.
# How: put the repository root on sys.path before importing engine modules.
# Purpose: ensure the edited source files are exercised without package install steps.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from engine.context import RunContext  # noqa: E402
from engine.conversation_store import ConversationStore  # noqa: E402
from engine.hooks.registry import HookRegistry  # noqa: E402
from engine.inference.ai_step import _execute_real_tools  # noqa: E402
import engine.inference.ai_step as ai_step_module  # noqa: E402
from engine.inference.loop_state import _LoopState  # noqa: E402
from engine.inference.tool_format import NativeToolFormatter  # noqa: E402
from engine.node import Node  # noqa: E402
from providers.base import BaseProvider, ProviderResponse  # noqa: E402
from toolbox.builtins.read_file import read_file  # noqa: E402


class _AllowedReadContext:
    def __init__(self, workspace_root: Path) -> None:
        # Why: read_file still asks the policy guard before reading.
        # How: this test context grants the read operation and exposes a workspace root.
        # Purpose: test truncation behavior without starting a supervisor service.
        self.workspace_root = workspace_root

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "allow"}

    async def check_cancelled(self) -> bool:
        return False


class _DummyProvider(BaseProvider):
    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        # Why: _execute_real_tools never calls the provider in these tests.
        # How: return a valid empty response anyway.
        # Purpose: satisfy the BaseProvider interface for _LoopState construction.
        return ProviderResponse(ok=True, text="")

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Any = None,
        on_thinking: Any = None,
    ) -> ProviderResponse:
        return ProviderResponse(ok=True, text="")


class _Registry:
    def get_spec(self, name: str) -> dict[str, Any]:
        return {"name": name, "async_mode": False}

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True}


async def _make_loop_state(tmp_path: Path) -> tuple[_LoopState, httpx.AsyncClient, ConversationStore]:
    http = httpx.AsyncClient()
    rctx = RunContext(
        workspace_root=tmp_path,
        supervisor_url="http://127.0.0.1:9",
        session_id="session-1",
        worker_id="worker-1",
        http=http,
        llm_http=http,
        task_id="task-1",
        session_generation=1,
    )
    store = ConversationStore(tmp_path / "conversations")
    rctx.conversation_store = store  # type: ignore[attr-defined]

    async def _record_event(event_type: str, payload: dict[str, Any]) -> None:
        # Why: truncation tests only need tool execution side effects.
        # How: replace network event delivery with a no-op coroutine.
        # Purpose: keep tests fast and independent from supervisor availability.
        return None

    async def _not_cancelled() -> bool:
        return False

    rctx.emit_event = _record_event  # type: ignore[method-assign]
    rctx.check_cancelled = _not_cancelled  # type: ignore[method-assign]

    ls = _LoopState(
        rctx=rctx,
        node=Node(id="node-1", type="ai", tool_mode="native"),
        provider=_DummyProvider(model="test", name="dummy"),
        registry=_Registry(),  # type: ignore[arg-type]
        run_id="run-1",
        context_ref="",
        runtime_cfg={},
        streaming=False,
        messages=[],
        system_prompt=[],
        is_block_mode=False,
        openai_tools=[],
        history=[],
        collected_attachments=[],
        tool_produced_attachments=[],
        formatter=NativeToolFormatter(),
        allowed_real_tools={"demo_tool"},
    )
    return ls, http, store


def test_read_file_without_range_auto_truncates_large_text_file(tmp_path: Path) -> None:
    """Large unbounded reads should return only the first 500 numbered lines."""
    large_file = tmp_path / "large.txt"
    large_file.write_text("\n".join(f"line-{i}" for i in range(1, 601)), encoding="utf-8")
    ctx = _AllowedReadContext(tmp_path)

    result = asyncio.run(read_file({"path": "large.txt"}, ctx))  # type: ignore[arg-type]
    entry = result["data"]["results"][0]

    # Why: unbounded large reads repeatedly exhausted model context.
    # How: assert the public read_file response carries a 500-line slice and a hint.
    # Purpose: future changes cannot reintroduce full-file reads by default.
    assert entry["success"] is True
    assert entry["lineCount"] == 500
    assert entry["truncated"] is True
    assert entry["totalLines"] == 600
    assert entry["shownLines"] == 500
    assert "Use startLine/endLine" in entry["hint"]
    assert "line-500" in entry["content"]
    assert "line-501" not in entry["content"]


def test_read_file_explicit_range_is_not_auto_truncated(tmp_path: Path) -> None:
    """Explicit line ranges must keep working even when the file is large."""
    large_file = tmp_path / "large.txt"
    large_file.write_text("\n".join(f"line-{i}" for i in range(1, 601)), encoding="utf-8")
    ctx = _AllowedReadContext(tmp_path)

    result = asyncio.run(read_file({"path": "large.txt", "start_line": 550, "end_line": 555}, ctx))  # type: ignore[arg-type]
    entry = result["data"]["results"][0]

    # Why: the default cap should guide callers toward precise ranges, not block them.
    # How: request a small range past line 500 and verify it is returned unchanged.
    # Purpose: preserve targeted read_file usage for large files.
    assert entry["success"] is True
    assert entry["lineCount"] == 6
    assert entry["startLine"] == 550
    assert entry["endLine"] == 555
    assert entry["totalLines"] == 600
    assert "truncated" not in entry
    assert "line-550" in entry["content"]
    assert "line-555" in entry["content"]


def test_execute_real_tools_truncates_large_string_result_for_messages_and_store(tmp_path: Path, monkeypatch: Any) -> None:
    """String tool results over 32,000 chars should be clipped before message storage."""
    monkeypatch.setattr(ai_step_module, "hook_registry", HookRegistry())
    # [AutoC 2026-05-31] Why: result_to_raw now accepts optional tool_spec in
    # production. How: keep this monkeypatch compatible with the expanded
    # signature. Purpose: this truncation test remains focused on message storage,
    # not formatter routing.
    monkeypatch.setattr(ai_step_module, "result_to_raw", lambda tool_name, result, **kwargs: ("text", "x" * 33010))

    async def _run() -> tuple[str, str]:
        ls, http, store = await _make_loop_state(tmp_path)
        try:
            await _execute_real_tools(ls, [{"id": "call-1", "name": "demo_tool", "arguments": {}}], step=1)
            stored = store.load("session-1")[-1].content
            return ls.messages[-1]["content"], stored
        finally:
            await http.aclose()

    message_content, stored_content = asyncio.run(_run())

    # Why: the truncation point must feed both the formatter and shadow JSONL writer.
    # How: compare the in-memory tool message with the ConversationStore content.
    # Purpose: prevent a hidden full-size result from still being persisted.
    assert message_content == stored_content
    assert message_content.startswith("x" * 32000)
    assert "...[truncated, showing 32,000 of 33,010 chars." in message_content
    assert "Use more specific parameters" in message_content
    assert len(message_content) < 33010


def test_execute_real_tools_does_not_truncate_non_string_result_body(tmp_path: Path, monkeypatch: Any) -> None:
    """Structured raw result bodies should pass through the new guard unchanged."""
    raw_body = {"payload": "x" * 33010}
    monkeypatch.setattr(ai_step_module, "hook_registry", HookRegistry())
    # [AutoC 2026-05-31] Why: result_to_raw now accepts optional tool_spec in
    # production. How: keep this monkeypatch compatible with the expanded
    # signature. Purpose: this test remains focused on preserving structured raw
    # bodies, not formatter routing.
    monkeypatch.setattr(ai_step_module, "result_to_raw", lambda tool_name, result, **kwargs: ("json", raw_body))

    async def _run() -> Any:
        ls, http, _store = await _make_loop_state(tmp_path)
        try:
            await _execute_real_tools(ls, [{"id": "call-1", "name": "demo_tool", "arguments": {}}], step=1)
            return ls.messages[-1]["content"]
        finally:
            await http.aclose()

    message_content = asyncio.run(_run())

    # Why: only string payloads are safe to character-truncate without changing shape.
    # How: feed a dict raw body through the same execution path and check object equality.
    # Purpose: keep structured tool results available for future formatters that consume them.
    assert message_content == raw_body
    assert "truncated" not in str(message_content)
