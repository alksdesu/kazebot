"""控制台原文编辑器：存之前先过一遍运行期读文件的那套校验。

用不了的拒掉、一个字节都不写：写坏了运行期只能沿用旧的或当成空的，界面上却显示保存成功。
没人读的键、调度时会跳过的条目照存，随响应带回提醒。
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

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402
from supervisor.config_store import check_models_text  # noqa: E402
from supervisor.policy import check_policy_text  # noqa: E402
from supervisor.scheduler import schedules_path  # noqa: E402

_TOKEN = "rt-raw-editor-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "/v1/admin/config"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "code"
    (root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
    (root / config_layout.LEGACY_DRAWTOOLS_DIR).mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def admin(ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=ws), prefix=_BASE)

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"{_BASE}{url}", headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


# 端点、落盘位置、一份用不了的内容。
_REFUSED = [
    ("/policy/raw", "config/policy.yaml", "read_file:\n  default: maybe\n"),
    ("/policy/raw", "config/policy.yaml", "read_file: [unclosed\n"),
    ("/policy/raw", "config/policy.yaml", "- a list\n"),
    ("/runtime/raw", "config/runtime.yaml", "engine: 5\n"),
    ("/runtime/raw", "config/runtime.yaml", "engine: [unclosed\n"),
    ("/config/raw", "config/models.yaml", "openai: [unclosed\n"),
    ("/config/raw", "config/models.yaml", "version: not-a-number\n"),
    ("/config/raw", "config/models.yaml", "- a list\n"),
    ("/schedules/raw", "data/schedules.yaml", "schedules: nope\n"),
    ("/schedules/raw", "data/schedules.yaml", "- a list\n"),
    ("/mcp-clients/raw", "config/mcp.yaml", "clients: 5\n"),
    ("/mcp-clients/raw", "config/mcp.yaml", "clients: [unclosed\n"),
    ("/nodes/scout/raw", "config/nodes/scout.yaml", "just a string\n"),
    ("/drawtools/settings/raw", "config/drawtools/settings.yaml", "- a list\n"),
]


class TestRefusals:
    @pytest.mark.parametrize(("url", "rel", "content"), _REFUSED)
    def test_content_the_runtime_could_not_use_is_refused_and_not_written(
        self, admin, ws: Path, url: str, rel: str, content: str,
    ) -> None:
        before = "# untouched\n"
        _write(ws / rel, before)

        response = admin("PUT", url, json={"content": content})

        assert response.status_code == 400, response.text
        assert (ws / rel).read_text(encoding="utf-8") == before
        assert not (ws / rel).with_name(f"{(ws / rel).name}.bak").exists()

    def test_a_new_node_with_unusable_content_is_not_created(self, admin, ws: Path) -> None:
        response = admin("POST", "/nodes", json={"id": "scout", "content": "[unclosed\n"})

        assert response.status_code == 400
        assert not (config_layout.nodes_dir(ws) / "scout.yaml").exists()

    def test_markdown_fragments_are_not_parsed_as_yaml(self, admin, ws: Path) -> None:
        # 节点目录里混着 Markdown 片段，正文里有冒号和方括号很正常。
        response = admin("PUT", "/node-files/shared.md/raw", json={"content": "note: [ not yaml\n"})

        assert response.status_code == 200, response.text


class TestReminders:
    def test_a_policy_key_nobody_reads_is_saved_with_a_reminder(self, admin, ws: Path) -> None:
        response = admin("PUT", "/policy/raw", json={"content": "version: 1\nread_flie:\n  default: auto\n"})

        assert response.status_code == 200, response.text
        assert any("read_flie" in line for line in response.json()["warnings"])
        assert config_layout.policy_path(ws).read_text(encoding="utf-8").startswith("version: 1\n")

    def test_schedule_entries_the_scheduler_would_skip_are_reported(self, admin, ws: Path) -> None:
        response = admin("PUT", "/schedules/raw", json={"content": "schedules:\n  - cron: '* * * * *'\n"})

        assert response.status_code == 200, response.text
        assert response.json()["warnings"]
        assert schedules_path(ws).is_file()

    def test_a_clean_save_carries_no_warnings_key(self, admin, ws: Path) -> None:
        response = admin("PUT", "/runtime/raw", json={"content": "engine:\n  max_steps: 10\n"})

        assert response.status_code == 200, response.text
        assert "warnings" not in response.json()

    def test_a_retired_runtime_key_comes_back_as_a_reminder(self, admin, ws: Path) -> None:
        response = admin("PUT", "/runtime/raw", json={"content": "engine:\n  history_limit: 50\n"})

        assert response.status_code == 200, response.text
        assert any("engine.history_limit" in line for line in response.json()["warnings"])


class TestTheChecksThemselves:
    @pytest.mark.parametrize("text", ["", "version: 2\n", "openai:\n  model: m\n", "custom:\n  base_url: x\n"])
    def test_models_that_load_are_accepted(self, text: str) -> None:
        assert check_models_text(text) == []

    @pytest.mark.parametrize("text", ["openai: [x\n", "- a\n", "version: abc\n", "openai: 5\n"])
    def test_models_that_would_not_load_are_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            check_models_text(text)

    @pytest.mark.parametrize("text", ["", "   \n", "[unclosed\n", "- a\n", "extra_roots: 5\n",
                                      "write_file:\n  rules: [{pattern: a, decision: sometimes}]\n"])
    def test_policies_that_would_not_load_are_refused(self, text: str) -> None:
        # 空文件也算：运行期把它当成写坏了，沿用上一份。
        with pytest.raises(ValueError):
            check_policy_text(text)

    def test_policy_rule_keys_nobody_reads_are_named(self) -> None:
        warnings = check_policy_text("read_file:\n  rules:\n    - {pattern: a, decision: deny, reson: typo}\n")

        assert len(warnings) == 1 and "read_file.rules[0].reson" in warnings[0]
