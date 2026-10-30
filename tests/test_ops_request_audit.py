"""/v1/ops/request 和 /v1/ops/evaluate 的令牌门。

这两个端点是全部策略判定的入口，而 is_admin 由调用方自报。没有令牌的进程不能连上来把
approval_required 换成 auto，所以现在和别的端点一样走 require_admin：没带对令牌一律 401。
engine 的 http client 一直带着 Authorization（runner.py 建 client 时就设了），合法调用不受影响。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"


@pytest.fixture
def app_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
    log_path = tmp_path / "data" / "events.jsonl"
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(log_path, run_id="run-ops-audit"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )
    return app, log_path


def _post(app, path: str = "/v1/ops/request", headers: dict[str, str] | None = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        # 非回环来源：回环不进退避，退避那条用例要能观察到 429
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                path,
                json={
                    "session_id": "sess-1",
                    "op": "read_file",
                    "parameters": {"path": "notes.txt"},
                },
                headers=headers or {},
            )

    return asyncio.run(_run())


def _rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("path", ["/v1/ops/request", "/v1/ops/evaluate"])
def test_without_a_token_the_request_is_rejected(app_and_log, path: str) -> None:
    app, log_path = app_and_log

    resp = _post(app, path)

    # 观察期结束：不记日志放行，而是和别的端点一样当场 401。
    assert resp.status_code == 401
    assert _rows(log_path) == []


def test_a_valid_token_runs_the_policy(app_and_log) -> None:
    app, _ = app_and_log

    resp = _post(app, headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    assert resp.json()["safety_level"]


def test_a_query_token_also_passes(app_and_log) -> None:
    app, _ = app_and_log

    resp = _post(app, "/v1/ops/request?token=" + _TOKEN)

    assert resp.status_code == 200


def test_a_wrong_token_counts_toward_the_backoff(app_and_log) -> None:
    """门是真的了：错令牌记进退避表，连发几次之后非回环来源拿对令牌也先被 429 挡住。"""
    app, _ = app_and_log

    for _ in range(admin_api._AUTH_FREE_ATTEMPTS + 1):
        assert _post(app, headers={"Authorization": "Bearer wrong"}).status_code == 401

    # ASGITransport 的默认来源是 testclient，不是回环，照常退避
    assert _post(app, headers={"Authorization": f"Bearer {_TOKEN}"}).status_code == 429
