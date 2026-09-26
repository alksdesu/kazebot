"""/v1/platforms：平台清单与各平台的热载配置、试听端点。

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
    assert qq["hooks"] == ["trigger_policy"]


def test_an_unknown_platform_is_404(call) -> None:
    assert call("GET", "/v1/platforms/telegram").status_code == 404
    assert call("GET", "/v1/platforms/telegram/config/raw").status_code == 404


def test_a_platform_without_config_is_404(call, chatless) -> None:
    assert call("GET", "/v1/platforms/chatless").status_code == 200
    assert call("GET", "/v1/platforms/chatless/config/raw").status_code == 404
    response = call("POST", "/v1/platforms/chatless/trigger/dry-run", json={"messages": [], "config": _TRIGGER_VALUES})
    assert response.status_code == 404


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


def test_the_dry_run_uses_the_published_values(call, tmp_path: Path) -> None:
    live = tmp_path / "data" / "qq_live_state.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps({"published_at": 0, "values": _TRIGGER_VALUES}), encoding="utf-8")

    out = call("POST", "/v1/platforms/qq/trigger/dry-run", json={"messages": [{"text": "咪啪在吗", "group_id": 9, "user_id": 1}]})

    assert out.status_code == 200
    assert out.json()["results"][0]["triggered"] is True
