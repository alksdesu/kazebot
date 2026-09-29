"""原文编辑器的改动冲突检测：读的时候拿指纹，写的时候带回来比一遍。

共享组里几个实例的控制台开的是同一份文件。不比指纹，后保存的那个人就会把先保存的那份整份盖掉，
而界面上两边都显示保存成功。空指纹是「这份文件还不存在」，和「有文件但内容为空」不是一回事。
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from adapters._shared.live_config import BOOL, LiveConfig, LiveKey  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor import config_files, feature_config_api, platform_api  # noqa: E402
from supervisor.admin_api import create_admin_router  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-raw-hash-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_QQ_TEXT = "channels:\n  allowed_groups: [1]\n"
_QQ_OTHER = "channels:\n  allowed_groups: [2]\n"
_FEATURES_TEXT = "trigger:\n  signals:\n    name: true\n"
_FEATURES_OTHER = "trigger:\n  signals:\n    name: false\n"
_STALE = "0" * 64


def _seed(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(create_admin_router(workspace_root=tmp_path), prefix="/v1/admin/config")

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, f"/v1/admin/config{url}", headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-raw-hash"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


# ---------------------------------------------------------------------------
#  config_files 这一层
# ---------------------------------------------------------------------------

class TestReadRaw:
    def test_a_missing_file_has_no_hash(self, tmp_path: Path) -> None:
        assert config_files.read_raw(tmp_path / "nope.yaml") == {"content": "", "exists": False, "hash": ""}

    def test_an_empty_file_is_not_a_missing_file(self, tmp_path: Path) -> None:
        # 空指纹只表示「文件不存在」；空文件有自己的指纹，才能和别人抢先写进内容区分开。
        result = config_files.read_raw(_seed(tmp_path / "empty.yaml", ""))

        assert result["exists"] is True
        assert result["hash"] == hashlib.sha256(b"").hexdigest()

    def test_the_hash_is_over_the_bytes_on_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "crlf.yaml"
        path.write_bytes(b"version: 1\r\nkeep: me\r\n")

        result = config_files.read_raw(path)

        # 给前端的原文统一成 LF，指纹仍按落盘字节算，写入方比的也是字节。
        assert result["content"] == "version: 1\nkeep: me\n"
        assert result["hash"] == hashlib.sha256(b"version: 1\r\nkeep: me\r\n").hexdigest()


class TestWriteRaw:
    def test_it_returns_the_hash_of_what_landed(self, tmp_path: Path) -> None:
        path = tmp_path / "qq.yaml"

        new_hash = config_files.write_raw(path, _QQ_TEXT)

        assert new_hash == config_files.read_raw(path)["hash"]

    def test_a_matching_base_hash_goes_through(self, tmp_path: Path) -> None:
        path = _seed(tmp_path / "qq.yaml", _QQ_OTHER)
        base = config_files.read_raw(path)["hash"]

        config_files.write_raw(path, _QQ_TEXT, base_hash=base)

        assert path.read_text(encoding="utf-8") == _QQ_TEXT

    def test_a_stale_base_hash_is_a_conflict_and_writes_nothing(self, tmp_path: Path) -> None:
        path = _seed(tmp_path / "qq.yaml", _QQ_OTHER)
        before = path.read_bytes()

        with pytest.raises(HTTPException) as caught:
            config_files.write_raw(path, _QQ_TEXT, base_hash=_STALE)

        assert caught.value.status_code == 409
        assert path.read_bytes() == before

    def test_an_empty_base_hash_means_the_file_must_not_exist_yet(self, tmp_path: Path) -> None:
        path = tmp_path / "qq.yaml"

        config_files.write_raw(path, _QQ_TEXT, base_hash="")

        assert path.read_text(encoding="utf-8") == _QQ_TEXT
        with pytest.raises(HTTPException) as caught:
            config_files.write_raw(path, _QQ_OTHER, base_hash="")
        assert caught.value.status_code == 409
        assert path.read_text(encoding="utf-8") == _QQ_TEXT

    def test_no_base_hash_means_no_check(self, tmp_path: Path) -> None:
        path = _seed(tmp_path / "qq.yaml", _QQ_OTHER)

        config_files.write_raw(path, _QQ_TEXT)

        assert path.read_text(encoding="utf-8") == _QQ_TEXT

    def test_the_write_uses_the_shared_config_lock(self, tmp_path: Path) -> None:
        # 和 config/ 下其余配置同一把锁，共享组里几个实例改同一份时才排得上队。
        path = tmp_path / "config" / "qq.yaml"

        config_files.write_raw(path, _QQ_TEXT)

        assert config_layout._lock_path(path).exists()
        assert not (path.parent / f"{path.name}.rotate.lock").exists()
        assert not list(path.parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
#  平台配置：PUT /v1/platforms/{id}/config/raw
# ---------------------------------------------------------------------------

class TestThePlatformConfigEndpoint:
    def test_the_response_carries_the_new_hash(self, api: Any, tmp_path: Path) -> None:
        response = api("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT})

        assert response.status_code == 200, response.text
        assert response.json()["hash"] == config_files.read_raw(tmp_path / "config" / "qq.yaml")["hash"]

    def test_the_hash_from_the_reader_is_accepted_by_the_writer(self, api: Any, tmp_path: Path) -> None:
        _seed(tmp_path / "config" / "qq.yaml", _QQ_OTHER)
        base = api("GET", "/v1/platforms/qq/config/raw").json()["hash"]

        saved = api("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT, "base_hash": base})

        assert saved.status_code == 200, saved.text
        assert (tmp_path / "config" / "qq.yaml").read_text(encoding="utf-8") == _QQ_TEXT

    def test_a_stale_hash_is_rejected_and_the_file_survives(self, api: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "qq.yaml", _QQ_OTHER)
        base = api("GET", "/v1/platforms/qq/config/raw").json()["hash"]
        # 另一个实例的控制台抢先保存了一版。
        config_layout.write_config_text(path, "channels:\n  allowed_groups: [3]\n")

        response = api("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT, "base_hash": base})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == "channels:\n  allowed_groups: [3]\n"

    def test_an_empty_hash_stops_the_second_creator(self, api: Any, tmp_path: Path) -> None:
        opened = api("GET", "/v1/platforms/qq/config/raw").json()
        assert opened["hash"] == ""
        config_layout.write_config_text(tmp_path / "config" / "qq.yaml", _QQ_OTHER)

        response = api("PUT", "/v1/platforms/qq/config/raw", json={"content": _QQ_TEXT, "base_hash": ""})

        assert response.status_code == 409, response.text
        assert (tmp_path / "config" / "qq.yaml").read_text(encoding="utf-8") == _QQ_OTHER

    def test_the_function_takes_the_base_hash_too(self, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "qq.yaml", _QQ_OTHER)

        with pytest.raises(HTTPException) as caught:
            platform_api.write_config_raw(tmp_path, QQ_NAPCAT, _QQ_TEXT, base_hash=_STALE)

        assert caught.value.status_code == 409
        assert path.read_text(encoding="utf-8") == _QQ_OTHER


class TestTheLegacyQqEndpoint:
    def test_it_passes_the_base_hash_through(self, admin: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "qq.yaml", _QQ_OTHER)

        response = admin("PUT", "/qq/raw", json={"content": _QQ_TEXT, "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == _QQ_OTHER

    def test_a_matching_hash_saves_and_answers_with_the_new_one(self, admin: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "qq.yaml", _QQ_OTHER)
        base = config_files.read_raw(path)["hash"]

        response = admin("PUT", "/qq/raw", json={"content": _QQ_TEXT, "base_hash": base})

        assert response.status_code == 200, response.text
        assert response.json()["hash"] == config_files.read_raw(path)["hash"]


# ---------------------------------------------------------------------------
#  全局功能设置：PUT /v1/features/config/raw
# ---------------------------------------------------------------------------

class TestTheFeatureConfigEndpoint:
    def test_the_response_carries_the_new_hash(self, api: Any, tmp_path: Path) -> None:
        response = api("PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT})

        assert response.status_code == 200, response.text
        assert response.json()["hash"] == config_files.read_raw(tmp_path / "config" / "features.yaml")["hash"]

    def test_a_stale_hash_is_rejected_and_the_file_survives(self, api: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "features.yaml", _FEATURES_OTHER)

        response = api("PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT, "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == _FEATURES_OTHER

    def test_the_reader_hash_round_trips(self, api: Any, tmp_path: Path) -> None:
        _seed(tmp_path / "config" / "features.yaml", _FEATURES_OTHER)
        base = api("GET", "/v1/features/config/raw").json()["hash"]

        saved = api("PUT", "/v1/features/config/raw", json={"content": _FEATURES_TEXT, "base_hash": base})

        assert saved.status_code == 200, saved.text
        assert saved.json()["hash"] != base

    def test_the_function_takes_the_base_hash_too(self, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "features.yaml", _FEATURES_OTHER)

        with pytest.raises(HTTPException) as caught:
            feature_config_api.write_features_raw(tmp_path, _FEATURES_TEXT, base_hash=_STALE)

        assert caught.value.status_code == 409
        assert path.read_text(encoding="utf-8") == _FEATURES_OTHER


# ---------------------------------------------------------------------------
#  控制台自己那批 raw 端点
# ---------------------------------------------------------------------------

class TestTheAdminScheduleEndpoint:
    def test_a_missing_file_reads_back_with_an_empty_hash(self, admin: Any) -> None:
        body = admin("GET", "/schedules/raw").json()

        assert body["content"] == "schedules: []"
        assert body["hash"] == ""

    def test_saving_against_the_empty_hash_creates_it(self, admin: Any, tmp_path: Path) -> None:
        response = admin("PUT", "/schedules/raw", json={"content": "schedules: []\n", "base_hash": ""})

        assert response.status_code == 200, response.text
        assert (tmp_path / "data" / "schedules.yaml").read_text(encoding="utf-8") == "schedules: []\n"

    def test_an_existing_file_reports_its_hash(self, admin: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "data" / "schedules.yaml", "schedules: []\n")

        body = admin("GET", "/schedules/raw").json()

        assert body["hash"] == config_layout.file_hash(path)

    def test_a_stale_hash_is_rejected(self, admin: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "data" / "schedules.yaml", "schedules: []\n")

        response = admin("PUT", "/schedules/raw", json={"content": "schedules: [x]\n", "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == "schedules: []\n"


class TestTheAdminMcpEndpoint:
    def test_a_missing_file_reads_back_with_an_empty_hash(self, admin: Any) -> None:
        # 和 schedules 同一种形状：还没接管时也要给得出指纹，否则第一次保存没得比。
        body = admin("GET", "/mcp-clients/raw").json()

        assert body["content"] == "version: 1\nclients: {}\n"
        assert body["hash"] == ""

    def test_a_stale_hash_is_rejected(self, admin: Any, tmp_path: Path) -> None:
        path = _seed(tmp_path / "config" / "mcp.yaml", "version: 1\nclients: {}\n")

        response = admin("PUT", "/mcp-clients/raw", json={"content": "version: 2\n", "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == "version: 1\nclients: {}\n"


class TestTheAdminDrawtoolsEndpoint:
    @pytest.fixture
    def bare_code_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """没有任何示例模板的代码目录：实例那份和模板都不在时的形状才看得见。"""
        root = tmp_path / "code"
        (root / config_layout.LEGACY_DRAWTOOLS_DIR).mkdir(parents=True)
        monkeypatch.setattr(config_layout, "CODE_ROOT", root)
        return root

    def test_neither_own_nor_example_reads_back_with_an_empty_hash(self, admin: Any, bare_code_root: Path) -> None:
        body = admin("GET", "/drawtools/settings/raw").json()

        assert body["exists"] is False
        assert body["using_example"] is False
        assert body["content"] == ""
        assert body["hash"] == ""

    def test_saving_against_the_empty_hash_creates_the_instance_file(
        self, admin: Any, bare_code_root: Path, tmp_path: Path,
    ) -> None:
        response = admin("PUT", "/drawtools/settings/raw", json={"content": "api: {}\n", "base_hash": ""})

        assert response.status_code == 200, response.text
        assert (tmp_path / "config" / "drawtools" / "settings.yaml").read_text(encoding="utf-8") == "api: {}\n"

    def test_the_example_hash_is_what_a_first_save_must_match(
        self, admin: Any, bare_code_root: Path, tmp_path: Path,
    ) -> None:
        # 实例还没接管时编辑器里显示的是模板，指纹也按模板算。
        example = bare_code_root / config_layout.LEGACY_DRAWTOOLS_DIR / "settings.example.yaml"
        example.write_text("api: {}\n", encoding="utf-8")

        body = admin("GET", "/drawtools/settings/raw").json()

        assert body["using_example"] is True
        assert body["hash"] == config_layout.file_hash(example)
        stale = admin("PUT", "/drawtools/settings/raw", json={"content": "api: {a: 1}\n", "base_hash": _STALE})
        assert stale.status_code == 409, stale.text
        assert not (tmp_path / "config" / "drawtools" / "settings.yaml").exists()

    def test_a_stale_hash_on_an_existing_file_is_rejected(
        self, admin: Any, bare_code_root: Path, tmp_path: Path,
    ) -> None:
        path = _seed(tmp_path / "config" / "drawtools" / "settings.yaml", "api: {}\n")

        response = admin("PUT", "/drawtools/settings/raw", json={"content": "api: {a: 1}\n", "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_text(encoding="utf-8") == "api: {}\n"


# ---------------------------------------------------------------------------
#  bot 进程那侧的写入：LiveConfig.save
# ---------------------------------------------------------------------------

_LIVE_KEYS = (LiveKey("enabled", "feature.enabled", BOOL, False),)


class TestLiveConfigSave:
    def _config(self, path: Path) -> LiveConfig:
        return LiveConfig(_LIVE_KEYS, path=lambda: path)

    def test_it_leaves_no_eventlog_lock_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "qq.yaml"
        config = self._config(path)

        config.save(config.document_from({"enabled": True}))

        # 事件流那把锁是个目录，留在 config/ 里会被当成一份配置。
        assert not (path.parent / f"{path.name}.rotate.lock").exists()
        assert [item.name for item in path.parent.iterdir() if item.is_dir()] == []

    def test_the_content_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "qq.yaml"
        config = self._config(path)

        snapshot = config.save(config.document_from({"enabled": True}))

        assert snapshot["enabled"] is True
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"feature": {"enabled": True}}
        assert config.reload()["enabled"] is True

    def test_it_takes_the_same_lock_as_the_console(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "qq.yaml"
        config = self._config(path)

        config.save(config.document_from({"enabled": True}))

        assert config_layout._lock_path(path).exists()

    def test_an_existing_files_newline_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "qq.yaml"
        _seed(path, "")
        path.write_bytes(b"feature:\r\n  enabled: false\r\n")
        config = self._config(path)

        config.save(config.document_from({"enabled": True}, base=config.raw_document()))

        assert b"\r\n" in path.read_bytes()
        assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")

    def test_a_non_mapping_is_refused_before_anything_is_written(self, tmp_path: Path) -> None:
        path = tmp_path / "config" / "qq.yaml"
        config = self._config(path)

        with pytest.raises(TypeError):
            config.save(["feature"])  # type: ignore[arg-type]

        assert not path.exists()
