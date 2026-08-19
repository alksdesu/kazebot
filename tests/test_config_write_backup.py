"""raw 配置端点覆盖写入前必须留一份 .bak。

这一层不解析不校验：前端表单出一次 bug 就能把 163 行的 runtime.yaml 截成三行，
而覆盖不可逆。备份失败时必须连写入一起拒绝 —— 没有退路的覆盖比保存失败危险得多。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

# 十二个写入点共用 _write_text，这几条覆盖不同目录布局与敏感度。
WRITE_TARGETS = (
    ("/runtime/raw", "config/runtime.yaml"),
    ("/policy/raw", "data/policy.yaml"),
    ("/schedules/raw", "data/schedules.yaml"),
    ("/config/raw", "data/config.yaml"),
    ("/mcp-clients/raw", "data/mcp_clients.yaml"),
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=tmp_path), prefix="/v1/admin/config")

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"/v1/admin/config{url}", **kwargs)

        return asyncio.run(go())

    return call


def _seed(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestTheBackupIsWritten:
    @pytest.mark.parametrize("url,rel", WRITE_TARGETS)
    def test_the_previous_content_lands_in_a_bak(self, client, tmp_path: Path, url: str, rel: str) -> None:
        _seed(tmp_path, rel, "version: 1\nkeep: me\n")

        response = client("PUT", url, json={"content": "version: 2\n"}, headers=_AUTH)

        assert response.status_code == 200, response.text
        assert (tmp_path / rel).read_text(encoding="utf-8") == "version: 2\n"
        assert (tmp_path / f"{rel}.bak").read_text(encoding="utf-8") == "version: 1\nkeep: me\n"

    def test_a_truncating_save_stays_recoverable(self, client, tmp_path: Path) -> None:
        # 真实事故形状：表单未加载就保存，整份配置被三行替换。
        full = "version: 1\nengine:\n  tool_mode: json\n  max_steps: 64\nproviders:\n  openai: {}\n"
        _seed(tmp_path, "config/runtime.yaml", full)

        client("PUT", "/runtime/raw", json={"content": "shell: {}\n"}, headers=_AUTH)

        assert (tmp_path / "config/runtime.yaml").read_text(encoding="utf-8") == "shell: {}\n"
        assert (tmp_path / "config/runtime.yaml.bak").read_text(encoding="utf-8") == full

    def test_a_first_write_needs_no_backup(self, client, tmp_path: Path) -> None:
        response = client("PUT", "/policy/raw", json={"content": "version: 1\n"}, headers=_AUTH)

        assert response.status_code == 200, response.text
        assert (tmp_path / "data/policy.yaml").exists()
        assert not (tmp_path / "data/policy.yaml.bak").exists()

    def test_the_backup_holds_only_the_last_version(self, client, tmp_path: Path) -> None:
        # 单份覆盖式，不按时间戳堆积：要防的是「刚才那一次误操作」。
        _seed(tmp_path, "data/policy.yaml", "gen: 1\n")

        client("PUT", "/policy/raw", json={"content": "gen: 2\n"}, headers=_AUTH)
        client("PUT", "/policy/raw", json={"content": "gen: 3\n"}, headers=_AUTH)

        assert (tmp_path / "data/policy.yaml.bak").read_text(encoding="utf-8") == "gen: 2\n"

    def test_node_files_are_backed_up_too(self, client, tmp_path: Path) -> None:
        _seed(tmp_path, "config/nodes/qq.orchestrator.yaml", "id: qq.orchestrator\nprompt: old\n")

        response = client(
            "PUT", "/nodes/qq.orchestrator/raw",
            json={"content": "id: qq.orchestrator\nprompt: new\n"}, headers=_AUTH,
        )

        assert response.status_code == 200, response.text
        backup = tmp_path / "config/nodes/qq.orchestrator.yaml.bak"
        assert backup.read_text(encoding="utf-8") == "id: qq.orchestrator\nprompt: old\n"


class TestBackupFailureBlocksTheWrite:
    def test_a_failed_backup_leaves_the_file_untouched(
        self, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 备份失败还硬写 = 保险没生效却给了保险的错觉。
        original = "version: 1\nkeep: me\n"
        _seed(tmp_path, "config/runtime.yaml", original)

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(admin_api.shutil, "copy2", _boom)
        response = client("PUT", "/runtime/raw", json={"content": "version: 2\n"}, headers=_AUTH)

        assert response.status_code == 500
        assert "备份" in response.json()["detail"]
        assert (tmp_path / "config/runtime.yaml").read_text(encoding="utf-8") == original
