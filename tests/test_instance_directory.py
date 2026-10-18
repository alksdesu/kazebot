from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api
from supervisor import provisioning
from supervisor.api import create_app
from supervisor.config_store import ConfigStore
from supervisor.eventlog import EventLog
from supervisor.instances import load_instances
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(tmp_path / "instances.yaml"))
    monkeypatch.delenv("CLONOTH_URL_PREFIX", raising=False)
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "directory-test-token")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    web_dist = tmp_path / "adapters" / "web" / "frontend" / "dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text("<html>console</html>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def app(workspace: Path) -> FastAPI:
    return create_app(
        state=SupervisorState(
            workspace_root=workspace,
            eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="directory-test"),
            policy=PolicyEngine(workspace_root=workspace),
        ),
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _get(app: FastAPI, url: str, **kwargs: Any) -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            return await client.get(url, **kwargs)

    return asyncio.run(request())


def _write(workspace: Path, rows: list[Any]) -> None:
    (workspace / "instances.yaml").write_text(
        yaml.safe_dump({"instances": rows}, allow_unicode=True), encoding="utf-8",
    )


def test_directory_is_public_and_contains_only_navigation_fields(app: FastAPI, workspace: Path, monkeypatch) -> None:
    _write(workspace, [
        {"id": "1000000001", "uin": "1000000001", "path": "", "platform": "qq", "account": "1000000001"},
        {
            "id": "other", "label": "另一个实例", "path": "/i/other", "idx": 1,
            "platform": "telegram", "account": "private-account", "ports": {"supervisor": 8775},
            "workspace": "private-workspace", "token": "private-token", "provisioning": {"lines": ["private-log"]},
        },
    ])

    def unexpected_progress(*args, **kwargs):
        pytest.fail("公开目录不能读取开实例进度")

    monkeypatch.setattr(provisioning, "progress", unexpected_progress)
    response = _get(app, "/v1/instance-directory", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "current_path": "", "instances": [
            {"label": "主实例", "path": ""},
            {"label": "另一个实例", "path": "/i/other"},
        ],
    }
    assert _get(app, "/v1/instances").status_code == 401


def test_authenticated_listing_retains_its_private_fields(app: FastAPI, workspace: Path) -> None:
    _write(workspace, [{"id": "root", "label": "主号", "account": "private-account", "path": ""}])

    response = _get(app, "/v1/instances", headers={"Authorization": "Bearer directory-test-token"})

    assert response.status_code == 200
    row = response.json()["instances"][0]
    assert row["account"] == "private-account"
    assert row["id"] == "root"
    assert row["current"] is True


@pytest.mark.parametrize("rows", [None, []])
@pytest.mark.parametrize("prefix,label", [("", "主实例"), ("/i/local", "local"), ("/web/1", "1")])
def test_missing_and_empty_directories_include_current_instance(app, workspace, monkeypatch, rows, prefix, label) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", prefix)
    if rows is not None:
        _write(workspace, rows)

    response = _get(app, "/v1/instance-directory")

    assert response.status_code == 200
    assert response.json() == {"current_path": prefix, "instances": [{"label": label, "path": prefix}]}


def test_paths_are_deduplicated_and_current_instance_is_preserved(app, workspace, monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/current/")
    _write(workspace, [
        {"id": "root", "label": "主号", "path": "/"},
        {"id": "duplicate-root", "label": "重复根", "path": ""},
        {"id": "first", "label": "旧入口", "path": "web/1/"},
        {"id": "second", "label": "重复入口", "path": "/web/1"},
    ])

    assert _get(app, "/v1/instance-directory").json() == {
        "current_path": "/i/current", "instances": [
            {"label": "主号", "path": ""},
            {"label": "旧入口", "path": "/web/1"},
            {"label": "current", "path": "/i/current"},
        ],
    }


@pytest.mark.parametrize("path", [
    "//evil.example", "https://evil.example", "javascript:alert(1)", "/i/../other", "/./i/other",
    "/i\\other", "/i/other?token=secret", "/i/other#fragment", "/i/%2e%2e/other", "/i/%252f%252fevil",
    "/i/%5cother", "/i//other", "/i/a b", "/i/a\nb", "/i/实例", 123, {"private": "value"},
])
def test_unsafe_paths_fail_closed(app, workspace, path) -> None:
    _write(workspace, [{"id": "unsafe", "label": "private-label", "path": path}])

    response = _get(app, "/v1/instance-directory")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "实例目录包含无效路径"}
    root = _get(app, "/")
    assert root.status_code == 307
    assert root.headers["location"] == "/web/?entry=instances"


@pytest.mark.parametrize("body", [
    "instances: [broken: [yaml", "", "{}", "instances: {}", "instances: [null]",
    "instances: [{label: missing-id}]", "instances: [{id: root, label: {token: private-token}}]",
])
def test_malformed_files_return_service_unavailable_without_details(app, workspace, body) -> None:
    (workspace / "instances.yaml").write_text(body, encoding="utf-8")

    response = _get(app, "/v1/instance-directory")

    assert response.status_code == 503
    assert set(response.json()) == {"detail"}
    assert "private-token" not in response.text
    assert "instances.yaml" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_unreadable_directory_and_bad_encoding_return_service_unavailable(app, workspace) -> None:
    path = workspace / "instances.yaml"
    path.mkdir()
    assert _get(app, "/v1/instance-directory").status_code == 503
    path.rmdir()
    path.write_bytes(b"\xff\xfe\xff")
    assert _get(app, "/v1/instance-directory").status_code == 503


def test_legacy_list_format_and_loose_reader_remain_supported(app, workspace) -> None:
    path = workspace / "instances.yaml"
    path.write_text("- id: root\n  label: 主号\n  path: ''\n", encoding="utf-8")
    assert _get(app, "/v1/instance-directory").json()["instances"] == [{"label": "主号", "path": ""}]
    path.write_text("instances: [broken: [yaml", encoding="utf-8")
    assert load_instances(workspace) == []


@pytest.mark.parametrize("rows,expected", [
    ([], "/web/"),
    ([{"id": "root", "path": ""}], "/web/"),
    ([{"id": "root", "path": ""}, {"id": "duplicate", "path": "/"}], "/web/"),
    ([{"id": "root", "path": ""}, {"id": "other", "path": "/i/other"}], "/web/?entry=instances"),
    ([{"id": "other", "path": "/i/other"}], "/web/?entry=instances"),
])
def test_root_redirect_depends_on_navigable_entries(app, workspace, rows, expected) -> None:
    _write(workspace, rows)
    response = _get(app, "/")
    assert response.status_code == 307
    assert response.headers["location"] == expected
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("query", [
    "token=root%2Ftoken%3D&view=chat", "view=admin", "entry=direct", "entry=instances", "anything=",
    "next=https%3A%2F%2Fevil.example%2F&token=private", "token=a&token=b", "token=%23secret%0D%0AHeader%3Avalue",
])
def test_explicit_root_query_stays_on_current_console(app, workspace, query) -> None:
    _write(workspace, [{"id": "root", "path": ""}, {"id": "other", "path": "/i/other"}])

    response = _get(app, f"/?{query}")

    assert response.headers["location"] == f"/web/?{query}"
    assert response.headers["cache-control"] == "no-store"


def test_explicit_console_link_still_works_when_directory_is_broken(app, workspace) -> None:
    (workspace / "instances.yaml").write_text("instances: [broken: [yaml", encoding="utf-8")
    assert _get(app, "/?token=root-token").headers["location"] == "/web/?token=root-token"


def test_broken_directory_root_redirects_to_the_retry_screen(app, workspace) -> None:
    (workspace / "instances.yaml").write_text("instances: [broken: [yaml", encoding="utf-8")

    response = _get(app, "/")

    assert response.status_code == 307
    assert response.headers["location"] == "/web/?entry=instances"
    assert response.headers["cache-control"] == "no-store"
    assert _get(app, "/v1/instance-directory").status_code == 503


@pytest.mark.parametrize("prefix", ["/i/other", "/web/1", "/i/project.v2", "/i/~project"])
def test_mounted_instances_keep_their_own_routes_and_queries(app, workspace, monkeypatch, prefix) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", prefix)
    _write(workspace, [{"id": "root", "path": ""}, {"id": "other", "path": prefix, "label": "当前实例"}])
    mounted = FastAPI()
    mounted.mount(prefix, app)

    response = _get(mounted, f"{prefix}/")
    assert response.headers["location"] == f"{prefix}/web/"
    response = _get(mounted, f"{prefix}/?token=local&view=admin")
    assert response.headers["location"] == f"{prefix}/web/?token=local&view=admin"
    response = _get(mounted, f"{prefix}/v1/instance-directory")
    assert response.status_code == 200
    assert response.json()["current_path"] == prefix
    assert response.json()["instances"][1] == {"label": "当前实例", "path": prefix}


@pytest.mark.parametrize("prefix", ["//evil.example", "/i/../root", "/i/%2froot", "/i/实例"])
def test_invalid_current_prefix_is_not_returned_as_a_fallback(app, monkeypatch, prefix) -> None:
    monkeypatch.setenv("CLONOTH_URL_PREFIX", prefix)
    assert _get(app, "/v1/instance-directory").status_code == 503
