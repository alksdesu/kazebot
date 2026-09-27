"""表情包自动打标收进 supervisor：全系统只跑一份，开关听全局文件，全局没写时认各平台 bot 公布的兜底。"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.sticker_api as sticker_api  # noqa: E402
import supervisor.sticker_tagging as sticker_tagging  # noqa: E402
from adapters._shared import features  # noqa: E402
from adapters._shared.live_config import SOURCE_DEFAULT, SOURCE_ENV, SOURCE_LEGACY  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, STATE_LIVE, ChannelDescriptor, PlatformDescriptor  # noqa: E402
from platforms.registry import platform_state_path, register_platform, unregister_platform  # noqa: E402
from stickers.collect import store_path  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.features import feature_lifespan  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.sticker_tagging import AUTO_TAG, StickerTaggingService  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_LOGGER = "supervisor.sticker_tagging"

VAULT = PlatformDescriptor(
    id="vault",
    display_name="Vault Chat",
    short_name="Vault",
    channels=(ChannelDescriptor("vault_room", CHANNEL_GROUP),),
    config_path="config/vault.yaml",
    state_files={STATE_LIVE: "vault_live.json"},
)


@pytest.fixture(autouse=True)
def _global_file_in_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)


@pytest.fixture
def service(tmp_path: Path) -> StickerTaggingService:
    return StickerTaggingService(tmp_path, features.new_features_config(tmp_path))


@pytest.fixture
def vault() -> Iterator[PlatformDescriptor]:
    register_platform(VAULT)
    try:
        yield VAULT
    finally:
        unregister_platform(VAULT.id)


def _publish(
    root: Path, platform: PlatformDescriptor, value: Any, source: str, *, published_at: float | None = None,
) -> None:
    path = platform_state_path(platform, STATE_LIVE, root)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "published_at": time.time() if published_at is None else published_at,
        "values": {AUTO_TAG: value},
        "sources": {AUTO_TAG: source},
    }), encoding="utf-8")


def _write_global(service: StickerTaggingService, text: str) -> None:
    path = service._features_config.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    service._features_config.invalidate()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == _LOGGER and record.levelno >= logging.WARNING]


class TestSwitch:
    def test_it_is_on_by_default(self, service: StickerTaggingService) -> None:
        switch = service.switch()

        assert (switch.enabled, switch.set_globally, switch.fallbacks) == (True, False, ())

    def test_the_qq_env_that_turned_tagging_off_still_does(self, service: StickerTaggingService, tmp_path: Path) -> None:
        # 升级前靠 ONEBOT_STICKER_AUTO_TAG=0 关掉打标的部署，升级之后不能悄悄重新花钱。
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)

        switch = service.switch()

        assert switch.enabled is False
        assert [(fallback.platform, fallback.source) for fallback in switch.fallbacks] == [(QQ_NAPCAT.id, SOURCE_ENV)]

    def test_the_old_qq_yaml_line_still_counts(self, service: StickerTaggingService, tmp_path: Path) -> None:
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_LEGACY)

        assert service.switch().enabled is False

    def test_a_qq_default_is_not_a_setting(self, service: StickerTaggingService, tmp_path: Path) -> None:
        _publish(tmp_path, QQ_NAPCAT, True, SOURCE_DEFAULT)

        assert service.switch().fallbacks == ()

    def test_the_global_file_overrides_the_qq_fallback(self, service: StickerTaggingService, tmp_path: Path) -> None:
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)
        _write_global(service, "extensions:\n  sticker:\n    auto_tag: true\n")

        switch = service.switch()

        assert (switch.enabled, switch.set_globally) == (True, True)

    def test_a_bot_that_has_stopped_still_counts(self, service: StickerTaggingService, tmp_path: Path) -> None:
        # 写在配置和 env 里的设置不随 bot 停下而消失；不认的话，bot 一停就开始花钱。
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV, published_at=0.0)

        assert service.switch().enabled is False

    def test_another_platform_left_on_keeps_it_running(
        self, service: StickerTaggingService, tmp_path: Path, vault: PlatformDescriptor,
    ) -> None:
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)
        _publish(tmp_path, vault, True, SOURCE_LEGACY)

        assert service.switch().enabled is True

    def test_the_switch_is_read_every_round(self, service: StickerTaggingService, tmp_path: Path) -> None:
        assert service._tagger_config().enabled is True

        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)
        assert service._tagger_config().enabled is False

        _write_global(service, "extensions:\n  sticker:\n    auto_tag: true\n")
        assert service._tagger_config().enabled is True


class TestReports:
    def test_a_fallback_that_turns_it_off_is_explained_once(
        self, service: StickerTaggingService, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 全局页上这一项显示的是默认值「开」，得说清是 QQ 那边关的、该去哪改。
        _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for _ in range(3):
                service._tagger_config()

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "features.yaml 没写 extensions.sticker.auto_tag" in warnings[0]
        assert f"{QQ_NAPCAT.display_name} 的环境变量（关）" in warnings[0]

    def test_it_is_explained_again_when_the_reason_changes(
        self, service: StickerTaggingService, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _publish(tmp_path, QQ_NAPCAT, False, SOURCE_ENV)
            service._tagger_config()
            _publish(tmp_path, QQ_NAPCAT, False, SOURCE_LEGACY)
            service._tagger_config()

        warnings = _warnings(caplog)
        assert len(warnings) == 2
        assert f"{QQ_NAPCAT.display_name} 的 qq.yaml 老位置（关）" in warnings[1]

    def test_turning_it_off_in_the_global_file_is_not_a_warning(
        self, service: StickerTaggingService, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_global(service, "extensions:\n  sticker:\n    auto_tag: false\n")

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert service._tagger_config().enabled is False

        assert _warnings(caplog) == []

    def test_a_library_that_cannot_be_opened_is_reported_once_per_problem(
        self, service: StickerTaggingService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        opened = SimpleNamespace(error=OSError("只读文件系统"))

        def library_store(_root: Path) -> Any:
            if opened.error is not None:
                raise opened.error
            return "store"

        monkeypatch.setattr(sticker_tagging, "library_store", library_store)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert [service._open_store() for _ in range(3)] == [None, None, None]
            opened.error = None
            assert service._open_store() == "store"
            opened.error = OSError("只读文件系统")
            assert service._open_store() is None

        assert len(_warnings(caplog)) == 2


class _Service:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def close(self) -> None:
        self.events.append("close")


async def _eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10.0
    while not condition():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)


class TestLifespan:
    def test_the_supervisor_runs_one_tagger_and_stops_it_on_the_way_out(
        self, service: StickerTaggingService, tmp_path: Path,
    ) -> None:
        app = FastAPI(lifespan=feature_lifespan)
        app.state.state = SimpleNamespace(execution=_Service(), reminders=_Service(), sticker_tagging=service)

        async def go() -> asyncio.Task[None]:
            async with app.router.lifespan_context(app):
                task = service.tagger._task
                assert task is not None and not task.done()
                # 第一轮就去开了库：bot 不在线也照样打。
                await _eventually(lambda: store_path(tmp_path).is_file())
            return task

        task = asyncio.run(go())

        assert task.done()
        assert service.tagger._task is None
        # 打标停了之后图库连接跟着关掉。
        assert str(store_path(tmp_path)) not in sticker_api._STORES
        assert app.state.state.execution.events == ["start", "close"]


@pytest.fixture
def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-tagging"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    request.app = app  # type: ignore[attr-defined]
    return request


class TestSharedGlobalConfig:
    def test_a_console_save_reaches_the_tagger_even_when_the_file_looks_unchanged(
        self, call: Any, tmp_path: Path,
    ) -> None:
        # 同一个时间刻度里写进等长内容，文件指纹认不出来；路由和打标共用一份实例，路由写完作废的缓存打标这边也看得到。
        service = call.app.state.state.sticker_tagging
        path = tmp_path / "config" / "features.yaml"
        assert call("PUT", "/v1/features/config/raw", json={
            "content": "extensions:\n  sticker:\n    auto_tag: on\n",
        }).status_code == 200
        assert service.switch().enabled is True
        stamp = path.stat().st_mtime_ns

        assert call("PUT", "/v1/features/config/raw", json={
            "content": "extensions:\n  sticker:\n    auto_tag: no\n",
        }).status_code == 200
        os.utime(path, ns=(stamp, stamp))

        assert service.switch().enabled is False


def _imports_the_tagger(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module == "stickers.tagger":
                return True
            if node.module == "stickers" and any(alias.name == "tagger" for alias in node.names):
                return True
        if isinstance(node, ast.Import) and any(alias.name == "stickers.tagger" for alias in node.names):
            return True
    return False


def test_no_bot_process_runs_a_tagger_of_its_own() -> None:
    # 打标全系统只跑一份；bot 再起一份，多开时几份一起把看图渠道的频率限制打满。
    offenders = [
        path.relative_to(_ROOT).as_posix()
        for path in sorted((_ROOT / "adapters").rglob("*.py"))
        if "node_modules" not in path.parts and _imports_the_tagger(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
