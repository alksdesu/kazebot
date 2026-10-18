from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from engine.inference.llm_call import _call_llm_with_retry
from engine.node import Node
from plugin_runtime import get_manager
from plugin_runtime.store import PluginStore
from providers.openai import OpenAIProvider


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = 0

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self):
        self.closed += 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("interruption", ["parent", "cancel_check", "preempt_check"])
def test_interrupted_llm_wait_closes_http_and_plugin_request(tmp_path, streaming, interruption):
    store = PluginStore(tmp_path)
    installed = store.install("cleanup.py", '''
import asyncio
outcomes = []
def setup(api):
    @api.on_request_end
    async def end(ctx, outcome):
        await asyncio.sleep(0)
        outcomes.append(outcome)
''')
    store.configure("cleanup", enabled=True, config={}, selectors={}, base_hash=installed["desired_hash"])

    async def run():
        source = WaitingStream()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source),
        )) as client:
            provider = OpenAIProvider(http=client, api_key="test", base_url="https://example.invalid", model="test")
            rctx = SimpleNamespace(
                workspace_root=tmp_path, task_id="task", begin_llm_request=lambda: "request",
                emit_event=AsyncMock(), check_cancelled=AsyncMock(return_value=False),
                check_preempted=AsyncMock(return_value={"preempted": False}),
            )
            if interruption == "cancel_check":
                rctx.check_cancelled.side_effect = RuntimeError("cancel check failed")
            if interruption == "preempt_check":
                rctx.check_preempted.side_effect = RuntimeError("preempt check failed")
            state = SimpleNamespace(
                messages=[{"role": "user", "content": "hello"}], formatter=None, provider=provider,
                node=Node(id="entry", type="ai"), openai_tools=None, use_stream=streaming,
                retry_max=0, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
                preempt_after_step=False, preempt_inject_info=None, rctx=rctx,
            )
            task = asyncio.create_task(_call_llm_with_retry(state, 0))
            await asyncio.wait_for(source.entered.wait(), timeout=2)
            if interruption == "parent":
                task.cancel()
                expected = asyncio.CancelledError
            else:
                expected = RuntimeError
            with pytest.raises(expected):
                await asyncio.wait_for(task, timeout=2)
            manager = get_manager(tmp_path)
            outcomes = manager._versions["cleanup"].module.outcomes
            assert len(outcomes) == 1
            assert outcomes[0]["cancelled"] is True
            assert manager.status()["plugins"][0]["active_requests"] == 0
            assert source.closed == 1

    try:
        asyncio.run(run())
    finally:
        get_manager(tmp_path).close()
