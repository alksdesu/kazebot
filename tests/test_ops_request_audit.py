"""/v1/ops/request 的调用方审计（观察期）。

这个端点是全部策略判定的入口，而 is_admin 由调用方自己塞在 parameters 里，端点本身
不认凭证——能连上 supervisor 端口的进程都可以把 approval_required 换成 auto。

engine 的 http client 一直带着 Authorization（runner.py 建 client 时就设了），
所以改成强制拒绝的风险不大；但先跑一段观察期，确认日志里除了 engine 没有别的调用方，
免得漏掉某条路径把工具调用全打死。观察期只记录，不拦。
"""
from __future__ import annotations

import asyncio
import json
import sys
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
    log_path = tmp_path / "data" / "events.jsonl"
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(log_path, run_id="run-ops-audit"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    return app, log_path


def _post(app, headers: dict[str, str] | None = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/v1/ops/request",
                json={
                    "session_id": "sess-1",
                    "op": "read_file",
                    "parameters": {"path": "notes.txt"},
                },
                headers=headers or {},
            )

    return asyncio.run(_run())


def _audit_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("type") == "ops_request_unauthenticated"]


def test_missing_token_is_recorded_but_not_blocked(app_and_log) -> None:
    app, log_path = app_and_log

    resp = _post(app)

    # 观察期的全部意义：照常放行，同时留下痕迹。
    assert resp.status_code == 200
    assert resp.json()["safety_level"]
    rows = _audit_rows(log_path)
    assert len(rows) == 1
    assert rows[0]["payload"]["op"] == "read_file"
    assert rows[0]["payload"]["token_presented"] is False


def test_valid_token_records_nothing(app_and_log) -> None:
    app, log_path = app_and_log

    resp = _post(app, {"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    assert _audit_rows(log_path) == []


def test_wrong_token_is_recorded(app_and_log) -> None:
    app, log_path = app_and_log

    _post(app, {"Authorization": "Bearer not-the-token"})

    rows = _audit_rows(log_path)
    assert len(rows) == 1
    assert rows[0]["payload"]["token_presented"] is True


def test_audit_never_records_the_token_itself(app_and_log) -> None:
    """凭证只报长度和有无。日志会被外传，记了值等于把 token 泄进日志。"""
    app, log_path = app_and_log
    secret = "super-secret-value-1234"

    _post(app, {"Authorization": f"Bearer {secret}"})

    raw = log_path.read_text(encoding="utf-8")
    assert secret not in raw
    assert _audit_rows(log_path)[0]["payload"]["token_len"] == len(secret)


def test_bad_token_does_not_trip_the_auth_backoff(app_and_log) -> None:
    """观察期不能有副作用：走 verify_admin_token 会把调用方记进退避表，
    连续几次之后合法请求也会被 429 挡下来，那等于提前进入强制模式。"""
    app, log_path = app_and_log

    for _ in range(6):
        assert _post(app, {"Authorization": "Bearer wrong"}).status_code == 200

    assert _post(app, {"Authorization": f"Bearer {_TOKEN}"}).status_code == 200
    assert len(_audit_rows(log_path)) == 6
