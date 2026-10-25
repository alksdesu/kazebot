"""带管理员令牌的 supervisor 请求里，任务、会话和审批 ID 只放行 uuid 这一类字符，拼不出别的端点。"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
import pytest

from clonoth_sdk.client import ClonothClient

_UUID = "3f2b8c4e-1d2a-4b7e-9c1f-0a1b2c3d4e5f"
_UNSAFE = ["../admin/state", "a/b", "", "   ", "x?y=1", "%2e%2e", "-dash", "a#b", "x" * 200]

_CALLS: dict[str, Callable[[ClonothClient, str], Awaitable[Any]]] = {
    "cancel_task": lambda client, value: client.cancel_task(value),
    "cancel_active_tasks": lambda client, value: client.cancel_active_tasks(value),
    "preempt_task": lambda client, value: client.preempt_task(value, message="m"),
    "get_running_tasks": lambda client, value: client.get_running_tasks(value),
    "get_session_provider_override": lambda client, value: client.get_session_provider_override(value),
    "set_session_provider_override": lambda client, value: client.set_session_provider_override(value, {}),
    "clear_session_provider_override": lambda client, value: client.clear_session_provider_override(value),
    "decide_approval": lambda client, value: client.decide_approval(value, decision="allow"),
    "get_approval": lambda client, value: client.get_approval(value),
    "get_task_origin": lambda client, value: client.get_task_origin(value),
}


async def _call(method: str, value: str, seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "tasks": [], "status": "allowed", "task_id": value,
                                         "origin_task_id": value})

    client = ClonothClient("http://supervisor")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._client = http
        return await _CALLS[method](client, value)


@pytest.mark.parametrize("method", sorted(_CALLS))
@pytest.mark.parametrize("value", _UNSAFE)
def test_an_unsafe_id_never_reaches_the_supervisor(method: str, value: str) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ValueError):
        asyncio.run(_call(method, value, seen))

    assert seen == []


@pytest.mark.parametrize("method", sorted(_CALLS))
@pytest.mark.parametrize("value", [_UUID, f"{_UUID}:branch:1", "task-forward-1"])
def test_supervisor_ids_stay_in_their_own_path_segment(method: str, value: str) -> None:
    seen: list[httpx.Request] = []

    asyncio.run(_call(method, value, seen))

    assert len(seen) == 1
    segments = seen[0].url.path.split("/")
    assert value in segments
    assert segments[1:3] in (["v1", "tasks"], ["v1", "sessions"], ["v1", "approvals"])
