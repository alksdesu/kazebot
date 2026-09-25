from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clonoth_sdk.feature_paths import validate_feature_path
from engine.eventlog_rotation import eventlog_file_lock
from providers import registry
from providers.base import ProviderResponse
from supervisor.admin_api import create_admin_router
from supervisor.operations.service import OperationsService


TEMPLATE_ROUTES = [
    ("GET", "/v1/operations/templates"),
    ("POST", "/v1/operations/templates"),
    ("POST", "/v1/operations/templates/preview"),
    ("POST", "/v1/operations/templates/apply"),
]


@pytest.fixture
def operations_api(tmp_path, monkeypatch):
    from supervisor.operations.api import create_router

    monkeypatch.setattr("supervisor.admin_api._admin_token", "synthetic-operations-token")
    monkeypatch.setattr("supervisor.admin_api._auth_failures", OrderedDict())
    operations, _ = service(tmp_path)
    app = FastAPI()
    app.include_router(create_router(operations.state))
    with TestClient(app) as client:
        yield client, operations.state


def workspace_state(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("body", [
    {"name": "旧模板", "settings": {"queue.workers": 3}, "expected_revision": "old"},
    {"settings": {"token": "synthetic-secret", "admin_users": ["42"]}},
    None,
])
def test_retired_template_routes_preserve_all_existing_files(operations_api, tmp_path, method, path, existing, body):
    client, _ = operations_api
    if existing:
        config = tmp_path / "config"
        config.mkdir()
        (config / "qq.yaml").write_text("queue:\n  workers: 1\n", encoding="utf-8")
        (config / "instance_templates.yaml").write_text("templates:\n  - name: 旧模板\n    settings: {queue.workers: 2}\n", encoding="utf-8")
        (config / "instances.yaml").write_text("instances: []\n", encoding="utf-8")
    before = workspace_state(tmp_path)
    response = client.request(method, path, headers={"Authorization": "Bearer synthetic-operations-token"}, json=body)
    assert response.status_code == 410
    assert response.json() == {"detail": "模板功能已移除，请切换实例独立设置"}
    assert workspace_state(tmp_path) == before


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_retired_template_routes_still_require_authentication(operations_api, tmp_path, method, path, authorization):
    client, _ = operations_api
    headers = {"Authorization": authorization} if authorization else {}
    response = client.request(method, path, headers=headers, json={})
    assert response.status_code == 401
    assert workspace_state(tmp_path) == {}


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
def test_retired_template_routes_still_reject_non_admin_actors(operations_api, tmp_path, method, path):
    client, _ = operations_api
    headers = {
        "Authorization": "Bearer synthetic-operations-token",
        "X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:own", "user_id": "42", "is_admin": False}),
    }
    response = client.request(method, path, headers=headers, json={})
    assert response.status_code == 403
    assert workspace_state(tmp_path) == {}


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
def test_retired_template_routes_keep_model_actor_confirmation_boundary(operations_api, tmp_path, method, path):
    client, state = operations_api
    state.tasks["active"] = SimpleNamespace(cancel_requested=False, input={"task_context": {
        "conversation_key": "qq_group:own", "channel": "qq_group",
        "platform_auth": {"is_admin": True, "user_id": "42"},
    }})
    response = client.request(method, path, headers={
        "Authorization": "Bearer synthetic-operations-token", "X-Clonoth-Task-Id": "active",
    }, json={"name": "模型模板", "settings": {"queue.workers": 3}})
    expected = 403 if method == "POST" and not path.endswith("/preview") else 410
    assert response.status_code == expected
    assert workspace_state(tmp_path) == {}


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
def test_retired_template_routes_do_not_parse_legacy_files_or_request_bodies(operations_api, tmp_path, method, path):
    client, _ = operations_api
    config = tmp_path / "config"
    config.mkdir()
    (config / "instance_templates.yaml").write_bytes(b"not: [valid yaml")
    before = workspace_state(tmp_path)
    response = client.request(method, path, headers={
        "Authorization": "Bearer synthetic-operations-token", "Content-Type": "application/json",
    }, content=b"not valid json")
    assert response.status_code == 410
    assert workspace_state(tmp_path) == before


@pytest.mark.parametrize("method,path", TEMPLATE_ROUTES)
def test_retired_template_routes_cannot_write_explicit_instance_config(operations_api, tmp_path, monkeypatch, method, path):
    from supervisor.operations.api import create_router

    _, state = operations_api
    target = tmp_path / "secondary/config/qq-custom.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("queue:\n  workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("CLONOTH_QQ_CONFIG_PATH", str(target))
    app = FastAPI()
    app.include_router(create_router(state))
    assert state.operations.qq_file == target
    before = workspace_state(tmp_path)
    with TestClient(app) as client:
        response = client.request(method, path, headers={"Authorization": "Bearer synthetic-operations-token"},
            json={"settings": {"queue.workers": 3}, "expected_revision": "old"})
    assert response.status_code == 410
    assert workspace_state(tmp_path) == before


def test_operations_status_instances_and_diagnostics_remain_available(operations_api, tmp_path, monkeypatch):
    client, _ = operations_api
    config = tmp_path / "config"
    config.mkdir()
    instances = config / "instances.yaml"
    instances.write_text("instances:\n  - {uin: '111', label: 主实例, path: '', idx: 0}\n  - {uin: '222', label: 副实例, path: /i/222, idx: 1}\n", encoding="utf-8")
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(instances))
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/222")
    headers = {"Authorization": "Bearer synthetic-operations-token"}
    before = workspace_state(tmp_path)
    status = client.get("/v1/operations/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["supervisor"] == "online"
    assert status.json()["instance_prefix"] == "/i/222"
    rows = client.get("/v1/operations/instances", headers=headers)
    assert rows.status_code == 200
    assert rows.json()["instances"] == [
        {"uin": "111", "label": "主实例", "path": "", "idx": 0, "current": False},
        {"uin": "222", "label": "副实例", "path": "/i/222", "idx": 1, "current": True},
    ]
    assert workspace_state(tmp_path) == before
    diagnosed = client.post("/v1/operations/diagnostics", headers=headers, json={"test_model": False})
    assert diagnosed.status_code == 200
    report = diagnosed.json()
    assert next(check for check in report["checks"] if check["name"] == "模型响应")["status"] == "skipped"
    for endpoint in ("", "/export"):
        response = client.get(f"/v1/operations/diagnostics/{report['id']}{endpoint}", headers=headers)
        assert response.status_code == 200
        assert response.json() == report
    assert not (config / "instance_templates.yaml").exists()
    assert not (config / "qq.yaml").exists()
    assert all(workspace_state(tmp_path)[path] == value for path, value in before.items())


def service(root):
    secret = SimpleNamespace(provider="missing", model="", base_url="https://example.invalid/v1", api_key="synthetic-config-secret")
    state = SimpleNamespace(workspace_root=root, tasks={}, _lock=threading.RLock(), _task_terminal=lambda _: False,
        feature_config_store=SimpleNamespace(get_openai_secret=lambda: secret))
    return OperationsService(state), secret


@pytest.mark.parametrize("age", [120, -3600])
def test_stale_or_future_heartbeat_does_not_claim_live_qq_connection(tmp_path, age):
    operations, _ = service(tmp_path)
    live = tmp_path / "data/qq_live_state.json"
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({"published_at": time.time() - age, "runtime": {"onebot_connected": True}}))
    status = operations.status()
    assert status["adapter_online"] is False
    assert status["onebot_connected"] is not True
    report = asyncio.run(operations.diagnose())
    assert next(item for item in report["checks"] if item["name"] == "OneBot连接")["status"] != "passed"


@pytest.mark.parametrize("provider_name,limit_option", [("openai", "max_completion_tokens"), ("gemini", "max_output_tokens"), ("anthropic", "max_tokens")])
def test_diagnostic_probe_sets_effective_bounded_provider_output(tmp_path, monkeypatch, provider_name, limit_option):
    operations, secret = service(tmp_path)
    secret.provider = provider_name
    secret.model = "synthetic-model"
    provider_class = registry.get(provider_name)
    assert provider_class is not None
    observed = []
    async def fake_chat(self, *, messages, tools, **kwargs):
        observed.append(self._options.get(limit_option))
        return ProviderResponse(ok=True, text="OK")
    monkeypatch.setattr(provider_class, "chat", fake_chat)
    report = asyncio.run(operations.diagnose(test_model=True))
    assert next(item for item in report["checks"] if item["name"] == "模型响应")["status"] == "passed"
    assert len(observed) == 1
    assert observed[0] is not None and 0 < observed[0] <= 1024


def test_export_scrubs_sensitive_log_filenames_and_url_userinfo(tmp_path):
    operations, secret = service(tmp_path)
    logs = tmp_path / "data/logs"
    logs.mkdir(parents=True)
    (logs / f"{secret.api_key}.log").write_text("request failed: https://synthetic-user:synthetic-url-pass@example.invalid/v1", encoding="utf-8")
    report = asyncio.run(operations.diagnose(include_logs=True))
    stored = json.dumps(operations.get_report(report["id"]))
    assert secret.api_key not in stored
    assert "synthetic-url-pass" not in stored


@pytest.mark.parametrize("path", [
    "/v1/community/notifications/activity%3AAabc123%3Aconfirm/ack",
    "/v1/community/notifications/qq_group%3Ahash%3Awelcome%3A123%3A456/ack",
])
def test_encoded_notification_identity_can_be_acknowledged(path):
    validate_feature_path(path, operations=True)


@pytest.mark.parametrize("path", [
    "/v1/community/notifications/%2F..%2Fadmin/ack",
    "/v1/community/%252e%252e/admin", "/v1/community/%00/ack",
    "/v1/community/%5C..%5Cadmin", "/v1/community/notifications/id?redirect=/admin",
    "/v1/community/notifications/id#fragment", "https://example.invalid/v1/community/state",
])
def test_encoded_feature_paths_still_cannot_change_routing(path):
    with pytest.raises(ValueError):
        validate_feature_path(path, operations=True)


def test_existing_raw_config_writer_keeps_transaction_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("supervisor.admin_api.verify_admin_token", lambda: None)
    app = FastAPI()
    app.include_router(create_admin_router(tmp_path))
    path = tmp_path / "config/qq.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("queue:\n  workers: 1\n")
    started = threading.Event()
    def update():
        started.set()
        with TestClient(app) as client:
            return client.put("/qq/raw", json={"content": "queue:\n  workers: 3\n"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with eventlog_file_lock(path):
            pending = pool.submit(update)
            assert started.wait(1)
            time.sleep(0.2)
            premature = pending.done()
        response = pending.result(timeout=5)
    assert response.status_code == 200
    assert premature is False


def test_report_scrubs_url_userinfo_in_ordinary_log_name(tmp_path):
    operations, _ = service(tmp_path)
    logs = tmp_path / "data/logs"
    logs.mkdir(parents=True)
    (logs / "runtime.log").write_text("request failed: https://synthetic-user:synthetic-url-pass@example.invalid/v1", encoding="utf-8")
    report = asyncio.run(operations.diagnose(include_logs=True))
    assert "synthetic-url-pass" not in json.dumps(operations.get_report(report["id"]))


def test_model_actor_cannot_self_authorize_paid_diagnostics_or_template_apply(tmp_path, monkeypatch):
    from supervisor.operations.api import create_router
    operations, _ = service(tmp_path)
    state = operations.state
    state.tasks = {"active": SimpleNamespace(cancel_requested=False, input={"task_context": {
        "conversation_key": "qq_group:own", "channel": "qq_group", "platform_auth": {"is_admin": True, "user_id": "1"},
    }})}
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda _: None)
    app = FastAPI()
    app.include_router(create_router(state))
    headers = {"X-Clonoth-Task-Id": "active", "X-Clonoth-Adapter-Actor": json.dumps({"scope": "qq_group:own", "user_id": "1", "is_admin": True})}
    with TestClient(app) as client:
        for endpoint, body in [("diagnostics", {"test_model": True}), ("templates/apply", {"settings": {"queue.workers": 3}, "expected_revision": "any"}), ("templates", {"name": "model", "settings": {"queue.workers": 3}})]:
            assert client.post(f"/v1/operations/{endpoint}", headers=headers, json=body).status_code == 403
    assert not (tmp_path / "data/diagnostics").exists()
    assert not (tmp_path / "config/qq.yaml").exists()


def test_feature_actor_owner_is_instance_scoped_even_for_same_platform_identity(tmp_path, monkeypatch):
    from starlette.requests import Request
    from supervisor.feature_auth import resolve_actor
    monkeypatch.setattr("supervisor.feature_auth.verify_admin_token", lambda _: None)
    def actor(root):
        raw = json.dumps({"scope": "qq_group:own", "channel": "qq_group", "user_id": "123"})
        request = Request({"type": "http", "headers": [(b"x-clonoth-adapter-actor", raw.encode())], "query_string": b""})
        return resolve_actor(request, SimpleNamespace(workspace_root=root))
    assert actor(tmp_path / "one").owner != actor(tmp_path / "two").owner
    assert actor(tmp_path / "one").owner == actor(tmp_path / "one").owner
