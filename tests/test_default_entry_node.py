"""没带入口节点的消息交给谁：runtime 的 shell.entry_node_id。

supervisor 建任务、TUI 一个根节点都没有时、/v1/config 发给网页的默认入口，都从 default_entry_node_id 取同一个值。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
from clonoth_runtime import default_entry_node_id  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_store import TaskStoreMixin  # noqa: E402

_TOKEN = "test-default-entry-token"
# defaults/runtime.yaml 发版带的那个
_SHIPPED = "bootstrap.shell_orchestrator"


def _write_runtime(workspace: Path, text: str) -> None:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _supervisor_default(workspace: Path) -> str:
    return TaskStoreMixin._default_entry_node(SimpleNamespace(workspace_root=workspace))


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-default-entry"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str) -> Any:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = asyncio.run(go())
        assert response.status_code == 200, response.text
        return response.json()

    return call


def test_an_untouched_workspace_uses_the_shipped_default(tmp_path: Path, api) -> None:
    assert default_entry_node_id(tmp_path) == _SHIPPED
    assert _supervisor_default(tmp_path) == _SHIPPED
    assert api("GET", "/v1/config")["default_entry_node_id"] == _SHIPPED


def test_the_instance_runtime_overrides_it_everywhere(tmp_path: Path, api) -> None:
    _write_runtime(tmp_path, "shell:\n  entry_node_id: qq.orchestrator\n")

    assert default_entry_node_id(tmp_path) == "qq.orchestrator"
    assert _supervisor_default(tmp_path) == "qq.orchestrator"
    assert api("GET", "/v1/config")["default_entry_node_id"] == "qq.orchestrator"
    # 改写渠道、重载配置回来的也是同一份，不是空的默认值
    assert api("POST", "/v1/config/reload")["config"]["default_entry_node_id"] == "qq.orchestrator"


def test_a_blank_value_falls_back_to_the_shipped_default(tmp_path: Path) -> None:
    _write_runtime(tmp_path, 'shell:\n  entry_node_id: "  "\n')

    assert default_entry_node_id(tmp_path) == _SHIPPED
    assert _supervisor_default(tmp_path) == _SHIPPED
