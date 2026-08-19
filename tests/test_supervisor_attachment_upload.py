"""Supervisor 附件上传端点的鉴权与可执行内容闸门。

这个端点会把请求方指定扩展名的字节落到 data/attachments 下，且落盘物可被 qq_forward
再发出去，所以既要 admin token 才能写，又要挡住可执行/脚本宿主类型的伪装上传。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"


def _make_app(workspace: Path):
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-upload")
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "data" / "config.yaml"),
    )


def _post(app, content: bytes, filename: str, *, token: str | None):
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return await client.post(
                "/v1/attachments/upload",
                params={"conversation_key": "web:1"},
                files={"file": (filename, content, "application/octet-stream")},
                headers=headers,
            )

    return asyncio.run(_run())


def _attachments(workspace: Path) -> list[Path]:
    root = workspace / "data" / "attachments"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_upload_requires_admin_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = _make_app(tmp_path)

    resp = _post(app, b"hello", "note.txt", token=None)

    assert resp.status_code == 401
    assert _attachments(tmp_path) == []


def test_upload_rejects_executable_extension(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = _make_app(tmp_path)

    resp = _post(app, b"MZ" + b"\x00" * 32, "x.exe", token=_TOKEN)

    assert resp.status_code == 415
    assert _attachments(tmp_path) == []


def test_upload_rejects_executable_disguised_as_pdf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = _make_app(tmp_path)

    resp = _post(app, b"MZ\x90\x00" + b"\x00" * 32, "report.pdf", token=_TOKEN)

    assert resp.status_code == 415
    assert _attachments(tmp_path) == []


def test_upload_accepts_normal_file_with_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = _make_app(tmp_path)

    resp = _post(app, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "pic.png", token=_TOKEN)

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "pic.png"
    assert len(_attachments(tmp_path)) == 1
