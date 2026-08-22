"""附件读取端点：只开放 data/attachments 子树，且必须带令牌。

img 标签带不了 Authorization，所以 token 走 query 也要认——这条一旦回归，
控制台里的图片会全部变成 401。
"""
from __future__ import annotations

import asyncio
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


@pytest.fixture()
def app_and_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")

    att = tmp_path / "data" / "attachments" / "qq_group_abc"
    att.mkdir(parents=True)
    (att / "shot.png").write_bytes(b"\x89PNG fake")
    # 附件目录之外的东西一律不该经由这个端点读到。
    (tmp_path / "data").joinpath("config.yaml").write_text("secret: yes", encoding="utf-8")

    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-att"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    return app, tmp_path


def _get(app, url: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(url)

    return asyncio.run(_run())


def test_serves_an_attachment_with_a_bearer_token(app_and_root) -> None:
    app, _ = app_and_root

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(
                "/v1/files/data/attachments/qq_group_abc/shot.png",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG fake"


def test_query_token_is_accepted(app_and_root) -> None:
    app, _ = app_and_root
    resp = _get(app, f"/v1/files/data/attachments/qq_group_abc/shot.png?token={_TOKEN}")
    assert resp.status_code == 200


def test_without_a_token_it_is_rejected(app_and_root) -> None:
    app, _ = app_and_root
    assert _get(app, "/v1/files/data/attachments/qq_group_abc/shot.png").status_code == 401


def test_a_missing_file_is_not_found(app_and_root) -> None:
    app, _ = app_and_root
    resp = _get(app, f"/v1/files/data/attachments/qq_group_abc/nope.png?token={_TOKEN}")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "rel",
    [
        "data/config.yaml",
        "data/attachments/../config.yaml",
        "data/attachments/../../data/config.yaml",
        "../config.yaml",
        "data",
        "data/attachments",
    ],
)
def test_anything_outside_the_attachment_tree_is_refused(app_and_root, rel: str) -> None:
    app, _ = app_and_root
    resp = _get(app, f"/v1/files/{rel}?token={_TOKEN}")
    assert resp.status_code == 404, f"{rel} 不该读得到"


def test_a_symlink_pointing_out_of_the_tree_is_refused(app_and_root) -> None:
    app, root = app_and_root
    secret = root / "data" / "config.yaml"
    link = root / "data" / "attachments" / "qq_group_abc" / "escape.yaml"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("这个环境建不了符号链接")
    resp = _get(app, f"/v1/files/data/attachments/qq_group_abc/escape.yaml?token={_TOKEN}")
    assert resp.status_code == 404
