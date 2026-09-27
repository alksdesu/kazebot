"""/v1/platforms：平台清单与各平台的热载配置、控制台 schema、试听端点。

旧的 /v1/admin/config/qq/* 留给没刷新的旧控制台和运维脚本，必须和新路径给出同一份结果。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor  # noqa: E402
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_TRIGGER_VALUES = {
    "group_trigger": "mention_only",
    "signal_all": None, "signal_at": None, "signal_reply": None, "signal_prefix": None,
    "signal_name": True, "signal_keyword": False, "signal_random": False,
    "name_words": ["咪啪"], "name_anywhere": True, "keyword_words": [],
    "random_probability": 0.02, "trigger_prefixes": ["!"],
    "llm_intent_enabled": False, "llm_intent_timeout_sec": 8.0,
    "cooldown_group_sec": 0.0, "cooldown_user_sec": 0.0, "cooldown_exempt_at": True,
}


@pytest.fixture
def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-platforms"),
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


@pytest.fixture
def chatless():
    platform = PlatformDescriptor(
        id="chatless", display_name="无配置平台", short_name="CL",
        channels=(ChannelDescriptor("cl_group", CHANNEL_GROUP),),
    )
    register_platform(platform)
    try:
        yield platform
    finally:
        unregister_platform(platform.id)


def test_the_listing_needs_the_admin_token(call) -> None:
    assert call("GET", "/v1/platforms", headers={}).status_code == 401


def test_the_listing_includes_qq(call) -> None:
    body = call("GET", "/v1/platforms").json()
    qq = next(item for item in body["platforms"] if item["id"] == "qq")
    assert qq["display_name"] == "QQ · NapCat"
    assert {channel["name"] for channel in qq["channels"]} >= {"qq_group", "qq_private"}
    assert qq["hooks"] == ["console_schema", "known_accounts", "trigger_policy"]
    assert qq["capabilities"]["mention"] == "native" and qq["capabilities"]["markdown"] is False


def test_an_unknown_platform_is_404(call) -> None:
    assert call("GET", "/v1/platforms/telegram").status_code == 404
    assert call("GET", "/v1/platforms/telegram/config/raw").status_code == 404


def test_a_platform_without_config_is_404(call, chatless) -> None:
    assert call("GET", "/v1/platforms/chatless").status_code == 200
    assert call("GET", "/v1/platforms/chatless/config/raw").status_code == 404
    response = call("POST", "/v1/platforms/chatless/trigger/dry-run", json={"messages": [], "config": _TRIGGER_VALUES})
    assert response.status_code == 404


def test_the_qq_console_schema_needs_no_running_bot(call, tmp_path: Path) -> None:
    from adapters.onebot import console_layout

    response = call("GET", "/v1/platforms/qq/config/schema")

    assert not (tmp_path / "data" / "qq_live_state.json").exists()
    assert response.status_code == 200
    assert response.json() == console_layout.schema()


def test_the_schema_needs_the_admin_token(call) -> None:
    assert call("GET", "/v1/platforms/qq/config/schema", headers={}).status_code == 401


def test_a_platform_without_a_console_layout_has_no_schema(call, chatless) -> None:
    response = call("GET", "/v1/platforms/chatless/config/schema")

    assert response.status_code == 404
    assert response.json()["detail"] == "无配置平台 没有控制台配置界面"
    assert call("GET", "/v1/platforms/telegram/config/schema").status_code == 404


def test_writing_and_reading_the_qq_config(call, tmp_path: Path) -> None:
    written = call("PUT", "/v1/platforms/qq/config/raw", json={"content": "channels:\n  allowed_groups: [1]\n"})
    assert written.status_code == 200
    assert (tmp_path / "config" / "qq.yaml").read_text(encoding="utf-8").startswith("channels:")

    raw = call("GET", "/v1/platforms/qq/config/raw").json()
    assert raw == {"content": "channels:\n  allowed_groups: [1]\n", "exists": True}


def test_the_legacy_routes_answer_the_same(call, tmp_path: Path) -> None:
    call("PUT", "/v1/admin/config/qq/raw", json={"content": "trigger: {}\n"})
    live = tmp_path / "data" / "qq_live_state.json"
    live.write_text(json.dumps({"published_at": 0, "values": _TRIGGER_VALUES}), encoding="utf-8")

    for suffix in ("raw", "state"):
        new = call("GET", f"/v1/platforms/qq/config/{suffix}").json()
        old = call("GET", f"/v1/admin/config/qq/{suffix}").json()
        new.pop("age_sec", None)
        old.pop("age_sec", None)
        assert new == old

    payload = {"messages": [{"text": "咪啪在吗", "user_id": 1, "group_id": 9}]}
    assert (
        call("POST", "/v1/platforms/qq/trigger/dry-run", json=payload).json()
        == call("POST", "/v1/admin/config/qq/trigger/dry-run", json=payload).json()
    )


_LIBRARY_LINE = "extensions:\n  sticker:\n    auto_accept: true\n"


def _seed_qq_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config" / "qq.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_a_platform_file_refuses_a_new_global_only_setting(call, tmp_path: Path) -> None:
    refused = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE})

    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "config/features.yaml" in detail
    assert "extensions.sticker.auto_accept" in detail
    assert not (tmp_path / "config" / "qq.yaml").exists()


def test_a_line_kept_from_before_the_split_still_saves(call, tmp_path: Path) -> None:
    # 控制台合并草稿时会把升级前那一行原样带回来，不能因此什么都存不进去。
    path = _seed_qq_file(tmp_path, _LIBRARY_LINE)

    kept = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE + "channels:\n  allowed_groups: [1]\n"})

    assert kept.status_code == 200
    assert "allowed_groups" in path.read_text(encoding="utf-8")


def test_changing_an_old_line_or_adding_beside_it_is_refused(call, tmp_path: Path) -> None:
    _seed_qq_file(tmp_path, _LIBRARY_LINE)

    changed = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE.replace("true", "false")})
    added = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE + "    library_limit: 500\n"})

    assert changed.status_code == 400
    assert "extensions.sticker.auto_accept" in changed.json()["detail"]
    assert added.status_code == 400
    assert "extensions.sticker.library_limit" in added.json()["detail"]
    assert "auto_accept" not in added.json()["detail"]


def test_dropping_an_old_line_is_always_allowed(call, tmp_path: Path) -> None:
    _seed_qq_file(tmp_path, _LIBRARY_LINE)

    nulled = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE.replace("true", "null")})
    removed = call("PUT", "/v1/platforms/qq/config/raw", json={"content": "channels: {}\n"})

    assert nulled.status_code == 200
    assert removed.status_code == 200


def test_a_broken_file_on_disk_counts_as_empty(call, tmp_path: Path) -> None:
    _seed_qq_file(tmp_path, "extensions: [\n" + _LIBRARY_LINE)

    refused = call("PUT", "/v1/platforms/qq/config/raw", json={"content": _LIBRARY_LINE})

    assert refused.status_code == 400
    assert "extensions.sticker.auto_accept" in refused.json()["detail"]


def test_the_legacy_route_refuses_the_same(call, tmp_path: Path) -> None:
    refused = call("PUT", "/v1/admin/config/qq/raw", json={"content": _LIBRARY_LINE})

    assert refused.status_code == 400
    assert "extensions.sticker.auto_accept" in refused.json()["detail"]


def test_the_dry_run_uses_the_published_values(call, tmp_path: Path) -> None:
    live = tmp_path / "data" / "qq_live_state.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps({"published_at": 0, "values": _TRIGGER_VALUES}), encoding="utf-8")

    out = call("POST", "/v1/platforms/qq/trigger/dry-run", json={"messages": [{"text": "咪啪在吗", "group_id": 9, "user_id": 1}]})

    assert out.status_code == 200
    assert out.json()["results"][0]["triggered"] is True
