"""HTTP 200 不等于重启排上了，两个调用方都得看出来。

模型会把工具的结果当结论转述给用户；TUI 拿到 True 就自己退出，
被拒时还退的话 supervisor 会把它当成一次正常关闭。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shell.tui.services.supervisor_client import SupervisorClient  # noqa: E402
from supervisor.types import RESTART_IN_PROGRESS, RESTART_NO_PROCESS_MANAGER  # noqa: E402
from toolbox.builtins import system as system_tool  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_RESTART = "/v1/admin/restart"
_Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # 真跑 git 会在仓库里落下一个 checkpoint commit。
    monkeypatch.setattr(system_tool, "_git_snapshot", lambda ctx: {"git_ok": False})
    monkeypatch.setattr(system_tool, "_git_commit_all", lambda **_kwargs: {"ok": True, "committed": False})


def _routed(restart: _Handler) -> httpx.MockTransport:
    """工具跑一趟还要问策略、报事件、查取消，只有重启那一问由用例决定。"""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == _RESTART:
            return restart(request)
        if request.url.path == "/v1/ops/request":
            return httpx.Response(200, json={"safety_level": "allow"})
        if request.url.path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


async def _call_tool(tmp_path: Path, restart: _Handler) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=_routed(restart), base_url="http://sv") as http:
        ctx = ToolContext(
            supervisor_url="http://sv",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=tmp_path,
            http=http,
            registry=None,
        )
        return await system_tool.request_restart({"target": "engine", "reason": "test"}, ctx)


def _answer(**kwargs: Any) -> _Handler:
    return lambda _request: httpx.Response(200, **kwargs)


@asynccontextmanager
async def _tui(handler: _Handler) -> AsyncIterator[SupervisorClient]:
    client = SupervisorClient("http://sv")
    await client.close()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sv")
    try:
        yield client
    finally:
        await client.close()


class TestTheToolRepeatsWhatTheSupervisorSaid:
    @pytest.mark.asyncio
    async def test_a_scheduled_restart_is_reported_as_one(self, tmp_path: Path) -> None:
        result = await _call_tool(tmp_path, _answer(json={"scheduled": True, "target": "engine"}))

        assert result["ok"] is True
        assert result["data"]["scheduled"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", [RESTART_IN_PROGRESS, RESTART_NO_PROCESS_MANAGER])
    async def test_a_refusal_hands_the_reason_through(self, tmp_path: Path, reason: str) -> None:
        result = await _call_tool(
            tmp_path, _answer(json={"scheduled": False, "target": "engine", "reason": reason}))

        assert result["ok"] is False
        assert result["error"] == f"restart not scheduled: {reason}"
        assert result["data"]["result"] == f"ERROR: restart not scheduled: {reason}"

    @pytest.mark.asyncio
    async def test_a_refusal_names_the_restart_that_is_holding_it(self, tmp_path: Path) -> None:
        # 只说「已在进行中」的话，模型会建议用户再按一次，而它每次都会被同一次重启挡住。
        result = await _call_tool(tmp_path, _answer(json={
            "scheduled": False, "target": "engine", "reason": RESTART_IN_PROGRESS,
            "in_progress": {"target": "all", "since": "2026-09-01T10:11:12+00:00"},
        }))

        assert result["error"] == (
            f"restart not scheduled: {RESTART_IN_PROGRESS} (all restart since 2026-09-01T10:11:12+00:00)"
        )
        assert result["data"]["result"] == f"ERROR: {result['error']}"

    @pytest.mark.asyncio
    async def test_the_one_in_flight_is_handed_over_as_data_too(self, tmp_path: Path) -> None:
        blocker = {"target": "engine", "since": "2026-09-01T10:11:12+00:00"}

        result = await _call_tool(tmp_path, _answer(json={
            "scheduled": False, "target": "engine", "reason": RESTART_IN_PROGRESS, "in_progress": blocker,
        }))

        assert result["in_progress"] == blocker
        assert result["data"]["in_progress"] == blocker

    @pytest.mark.asyncio
    async def test_a_refusal_with_nothing_in_flight_says_nothing_extra(self, tmp_path: Path) -> None:
        result = await _call_tool(tmp_path, _answer(json={
            "scheduled": False, "target": "engine", "reason": RESTART_NO_PROCESS_MANAGER,
        }))

        assert result["error"] == f"restart not scheduled: {RESTART_NO_PROCESS_MANAGER}"
        assert "in_progress" not in result
        assert "in_progress" not in result["data"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocker", [
        pytest.param(None, id="明写的 null"),
        pytest.param("all", id="只有目标名"),
        pytest.param(["all"], id="列表"),
    ])
    async def test_an_in_flight_field_that_is_not_an_object_is_ignored(
        self, tmp_path: Path, blocker: Any,
    ) -> None:
        # 拼不出「几点开始」就退回原来那句，不能把 None.get 抛到工具调用上。
        result = await _call_tool(tmp_path, _answer(json={
            "scheduled": False, "target": "engine", "reason": RESTART_IN_PROGRESS, "in_progress": blocker,
        }))

        assert result["error"] == f"restart not scheduled: {RESTART_IN_PROGRESS}"
        assert "in_progress" not in result

    @pytest.mark.asyncio
    async def test_a_refusal_still_carries_the_git_checkpoint(self, tmp_path: Path) -> None:
        # 重启没排上，刚打的 checkpoint 还在，模型得知道它落在哪。
        result = await _call_tool(
            tmp_path, _answer(json={"scheduled": False, "reason": RESTART_IN_PROGRESS}))

        assert result["data"]["git"] == {"git_ok": False}
        assert result["data"]["git_commit"] == {"ok": True, "committed": False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [
        pytest.param({"json": {"scheduled": False}}, id="没给原因"),
        pytest.param({"json": {"target": "engine"}}, id="连 scheduled 都没有"),
        pytest.param({"json": {"scheduled": False, "reason": ""}}, id="原因是空串"),
        pytest.param({"text": "<html>bad gateway</html>"}, id="根本不是 JSON"),
        pytest.param({"json": ["scheduled"]}, id="JSON 但不是对象"),
        pytest.param({"json": "scheduled"}, id="JSON 字符串"),
    ])
    async def test_anything_short_of_a_yes_counts_as_not_scheduled(
        self, tmp_path: Path, body: dict[str, Any],
    ) -> None:
        result = await _call_tool(tmp_path, _answer(**body))

        assert result["ok"] is False
        assert result["error"] == "restart not scheduled: unknown"

    @pytest.mark.asyncio
    async def test_an_http_error_is_reported_as_before(self, tmp_path: Path) -> None:
        result = await _call_tool(tmp_path, lambda _r: httpx.Response(503, text="supervisor is down"))

        assert result["ok"] is False
        assert result["error"] == "supervisor is down"


class TestTheTuiOnlyLeavesWhenTheRestartIsReal:
    @pytest.mark.asyncio
    async def test_a_scheduled_restart_lets_it_go(self) -> None:
        async with _tui(_answer(json={"scheduled": True, "target": "all"})) as client:
            assert await client.restart() is True

    @pytest.mark.asyncio
    async def test_a_refused_restart_keeps_it_here(self) -> None:
        async with _tui(_answer(json={"scheduled": False, "target": "all", "reason": RESTART_IN_PROGRESS})) as client:
            assert await client.restart() is False

    @pytest.mark.asyncio
    async def test_an_http_error_keeps_it_here(self) -> None:
        async with _tui(lambda _r: httpx.Response(500, text="boom")) as client:
            assert await client.restart() is False

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_keeps_it_here(self) -> None:
        async with _tui(_answer(text="<html>")) as client:
            assert await client.restart() is False

    @pytest.mark.asyncio
    async def test_a_transport_failure_keeps_it_here(self) -> None:
        def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("supervisor 不在")

        async with _tui(_boom) as client:
            assert await client.restart() is False
