"""挂在 /i/<实例名> 下的实例：域名根路径转进本实例，外层挂载壳不摊开接口文档。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.main import _mount_instance  # noqa: E402

PREFIX = "/i/beta_bot"


@pytest.fixture()
def client() -> TestClient:
    inner = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @inner.get("/")
    async def inner_root() -> dict[str, str]:
        return {"at": "inner"}

    return TestClient(_mount_instance(inner, PREFIX))


def test_the_domain_root_goes_into_this_instance(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"{PREFIX}/"
    assert response.headers["cache-control"] == "no-store"


def test_the_query_rides_along(client: TestClient) -> None:
    response = client.get("/?view=settings&tab=account", follow_redirects=False)

    assert response.headers["location"] == f"{PREFIX}/?view=settings&tab=account"


def test_following_it_reaches_the_instance(client: TestClient) -> None:
    assert client.get("/").json() == {"at": "inner"}


# 只接根路径：旧的 /web/ 是根实例的控制台，转进这里会让人以为切到了根实例。
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/web/"])
def test_the_shell_serves_nothing_else(client: TestClient, path: str) -> None:
    assert client.get(path, follow_redirects=False).status_code == 404


def test_an_unprefixed_instance_is_served_as_is() -> None:
    inner = FastAPI()

    assert _mount_instance(inner, "") is inner
