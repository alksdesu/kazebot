"""/v1/features：全局功能设置的原文读写、生效状态和控制台 schema。

生效值由 supervisor 当场算（全局那层不读 env）；「已生效」还要求每个在跑的 bot 都换上了当前文件，
判据和平台那边一样是 bot 实际采纳的指纹，不是它看到的指纹。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
from adapters._shared import features  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "/v1/features/config"
_NAME_ON = "trigger:\n  signals:\n    name: true\n"


@pytest.fixture
def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.delenv(features.FEATURES_CONFIG_ENV, raising=False)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    # 鉴权失败的退避表是模块级的，下面故意不带令牌的几次请求不能把锁带进别的测试。
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-features"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=kwargs.pop("headers", _AUTH), **kwargs)

        return asyncio.run(go())

    return request


def _features_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "features.yaml"


def _put(call: Any, content: str) -> httpx.Response:
    return call("PUT", f"{_BASE}/raw", json={"content": content})


def _state(call: Any) -> dict[str, Any]:
    response = call("GET", f"{_BASE}/state")
    assert response.status_code == 200, response.text
    return response.json()


def _qq(state: dict[str, Any]) -> dict[str, Any]:
    return next(entry for entry in state["platforms"] if entry["id"] == "qq")


def _publish_qq(tmp_path: Path, **overrides: Any) -> None:
    """模拟 QQ bot 写出的 data/qq_live_state.json，只放全局页读得到的那几项。"""
    payload: dict[str, Any] = {
        "published_at": time.time(),
        "paths": {"allowed_groups": "channels.allowed_groups", "signal_name": "trigger.signals.name"},
        **overrides,
    }
    target = tmp_path / "data" / "qq_live_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _defaults(tmp_path: Path, loaded: dict[str, Any], *, config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or _features_path(tmp_path)
    return {"config_path": str(path), "file": dict(loaded), "loaded": dict(loaded), "parse_error": ""}


class TestAuth:
    @pytest.mark.parametrize(("method", "suffix"), [("GET", "raw"), ("PUT", "raw"), ("GET", "state"), ("GET", "schema")])
    def test_every_endpoint_needs_the_admin_token(self, call: Any, method: str, suffix: str) -> None:
        kwargs = {"json": {"content": ""}} if method == "PUT" else {}

        assert call(method, f"{_BASE}/{suffix}", headers={}, **kwargs).status_code == 401


class TestRaw:
    def test_an_absent_file_is_a_normal_state(self, call: Any) -> None:
        assert call("GET", f"{_BASE}/raw").json() == {"content": "", "exists": False}

    def test_it_never_serves_the_example_as_the_current_file(self, call: Any, tmp_path: Path) -> None:
        example = tmp_path / "config" / "features.example.yaml"
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text("queue:\n  workers: 9\n", encoding="utf-8")

        assert call("GET", f"{_BASE}/raw").json()["exists"] is False

    def test_a_write_lands_in_the_global_file_verbatim(self, call: Any, tmp_path: Path) -> None:
        content = "# 全局默认\n" + _NAME_ON

        response = _put(call, content)

        assert response.status_code == 200
        assert response.json() == {"ok": True, "warnings": []}
        assert _features_path(tmp_path).read_text(encoding="utf-8") == content
        assert call("GET", f"{_BASE}/raw").json() == {"content": content, "exists": True}

    def test_the_path_override_is_honoured(self, call: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        elsewhere = tmp_path / "elsewhere" / "shared.yaml"
        monkeypatch.setenv(features.FEATURES_CONFIG_ENV, str(elsewhere))

        _put(call, _NAME_ON)

        assert elsewhere.read_text(encoding="utf-8") == _NAME_ON
        assert not _features_path(tmp_path).exists()

    def test_it_leaves_no_temp_file_behind(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)

        assert list(_features_path(tmp_path).parent.glob("features.yaml.*")) == []


class TestWriteValidation:
    def test_broken_yaml_is_rejected(self, call: Any, tmp_path: Path) -> None:
        response = _put(call, "trigger: [oops\n")

        assert response.status_code == 400
        assert "YAML 解析失败" in response.json()["detail"]
        assert not _features_path(tmp_path).exists()

    def test_a_non_mapping_top_level_is_rejected(self, call: Any) -> None:
        assert _put(call, "- 1\n- 2\n").status_code == 400

    def test_an_unconsumed_key_is_rejected_and_the_file_kept(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)

        response = _put(call, "trigger:\n  signals:\n    nmae: true\n")

        assert response.status_code == 400
        assert response.json()["detail"] == "无人消费的配置项：['trigger.signals.nmae']"
        assert _features_path(tmp_path).read_text(encoding="utf-8") == _NAME_ON

    def test_every_declared_path_is_accepted(self, call: Any) -> None:
        # 值随便填非空的：null 叶子不参与路径校验，全写 null 等于什么都没测。
        document: dict[str, Any] = {}
        for key in features.FEATURE_KEYS:
            cursor = document
            *parents, leaf = key.path.split(".")
            for part in parents:
                cursor = cursor.setdefault(part, {})
            cursor[leaf] = 1

        response = _put(call, json.dumps(document))

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        "content",
        ["version: 1\n" + _NAME_ON, "trigger:\nqueue:\n  workers: 2\n", "trigger:\n  signals:\n    name: null\n", ""],
    )
    def test_version_bare_sections_nulls_and_empty_files_pass(self, call: Any, content: str) -> None:
        assert _put(call, content).status_code == 200

    def test_a_platform_only_key_is_pointed_at_its_platform(self, call: Any, tmp_path: Path) -> None:
        """全局文件里写平台专属项不会有任何效果，报错要说清该写到哪份文件里。"""
        _publish_qq(tmp_path)

        response = _put(call, "channels:\n  allowed_groups: [1]\nqueue:\n  wrokers: 2\n")

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "无人消费的配置项：['queue.wrokers']；只能写在平台配置里：channels.allowed_groups（QQ · NapCat）"
        )

    def test_without_a_published_platform_the_key_is_just_unconsumed(self, call: Any) -> None:
        response = _put(call, "channels:\n  allowed_groups: [1]\n")

        assert response.json()["detail"] == "无人消费的配置项：['channels.allowed_groups']"


class TestState:
    def test_the_values_come_from_the_global_file(self, call: Any) -> None:
        _put(call, _NAME_ON)

        body = _state(call)

        assert body["published"] is True
        assert body["state"]["layer"] == "global"
        assert body["state"]["values"]["signal_name"] is True
        assert body["state"]["sources"]["signal_name"] == "global"
        assert body["state"]["sources"]["signal_keyword"] == "default"
        assert body["state"]["paths"] == {key.name: key.path for key in features.FEATURE_KEYS}
        assert "defaults" not in body["state"]

    def test_it_reads_as_applied_when_no_bot_is_running(self, call: Any) -> None:
        _put(call, _NAME_ON)

        body = _state(call)

        assert body["applied"] is True
        assert _qq(body) == {"id": "qq", "display_name": "QQ · NapCat", "published": False, "applied": False,
                             "reason": "bot 进程尚未公布生效配置"}

    def test_an_absent_file_is_applied_defaults(self, call: Any) -> None:
        body = _state(call)

        assert body["applied"] is True
        assert body["file"] == {"exists": False, "mtime_ns": 0, "size": 0}
        assert body["state"]["values"]["signal_name"] is False

    def test_a_bot_on_the_current_file_reads_as_applied(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)
        current = _state(call)["file"]
        _publish_qq(tmp_path, defaults=_defaults(tmp_path, current))

        body = _state(call)

        assert body["applied"] is True
        assert _qq(body)["applied"] is True
        assert _qq(body)["published"] is True and _qq(body)["stale"] is False

    def test_a_bot_on_the_previous_file_holds_it_back(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)
        previous = _state(call)["file"]
        _publish_qq(tmp_path, defaults=_defaults(tmp_path, previous))
        _put(call, _NAME_ON + "queue:\n  workers: 2\n")

        body = _state(call)

        assert body["applied"] is False
        assert _qq(body)["applied"] is False
        assert "reason" not in _qq(body)

    def test_a_stale_bot_does_not_hold_it_back(self, call: Any, tmp_path: Path) -> None:
        """失联的 bot 永远换不上新文件，算进去的话「已生效」就永远等不到。"""
        _put(call, _NAME_ON)
        _publish_qq(tmp_path, published_at=0, defaults=_defaults(tmp_path, {"exists": False, "mtime_ns": 0, "size": 0}))

        body = _state(call)

        assert _qq(body)["stale"] is True and _qq(body)["applied"] is False
        assert body["applied"] is True

    def test_a_bot_that_does_not_read_the_global_file_says_so(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)
        _publish_qq(tmp_path)

        body = _state(call)

        assert body["applied"] is False
        assert _qq(body)["applied"] is False
        assert _qq(body)["reason"] == "这个 bot 的版本还不读全局文件，更新并重启之后才会生效"

    def test_a_bot_reading_another_global_file_says_which(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)
        current = _state(call)["file"]
        other = tmp_path / "other" / "features.yaml"
        _publish_qq(tmp_path, defaults=_defaults(tmp_path, current, config_path=other))

        body = _state(call)

        assert _qq(body)["applied"] is False
        assert _qq(body)["reason"] == f"bot 读的是另一份全局文件：{other}"

    def test_the_same_global_file_spelled_another_way_is_not_another_file(self, call: Any, tmp_path: Path) -> None:
        # bot 和 supervisor 各自拼出的路径写法可以不同，指的是同一个文件就不该拦着。
        _put(call, _NAME_ON)
        current = _state(call)["file"]
        detour = tmp_path / "config" / ".." / "config" / "features.yaml"
        _publish_qq(tmp_path, defaults=_defaults(tmp_path, current, config_path=detour))

        body = _state(call)

        assert _qq(body)["applied"] is True and "reason" not in _qq(body)

    def test_a_broken_file_keeps_the_last_good_values_and_reads_as_pending(self, call: Any, tmp_path: Path) -> None:
        _put(call, _NAME_ON)
        assert _state(call)["state"]["values"]["signal_name"] is True

        _features_path(tmp_path).write_text("trigger: [oops\n", encoding="utf-8")
        body = _state(call)

        assert body["applied"] is False
        assert body["state"]["parse_error"]
        assert body["state"]["values"]["signal_name"] is True
        assert body["state"]["loaded"] != body["file"]

    def test_a_same_size_rewrite_in_the_same_tick_is_still_picked_up(self, call: Any, tmp_path: Path) -> None:
        """写完不作废缓存的话，mtime 粒度看不出这次改动，界面会一直显示旧值。"""
        _put(call, "queue:\n  workers: 4\n")
        assert _state(call)["state"]["values"]["queue_workers"] == 4
        stamp = _features_path(tmp_path).stat().st_mtime_ns

        _put(call, "queue:\n  workers: 5\n")
        os.utime(_features_path(tmp_path), ns=(stamp, stamp))

        assert _state(call)["state"]["values"]["queue_workers"] == 5


class TestSchema:
    def test_it_is_the_global_page_schema(self, call: Any) -> None:
        response = call("GET", f"{_BASE}/schema")

        assert response.status_code == 200
        assert response.json() == features.schema()
