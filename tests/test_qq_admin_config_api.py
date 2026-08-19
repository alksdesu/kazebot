"""QQ 热载配置的管理端点。

热重载最容易出的问题是「文件写了但进程没读到，界面却显示成功」。这里钉住两条：
写入侧不接受自己都读不回来的内容，读取侧的「已生效」判据是 bot 跑着的值确实来自
当前这个文件版本，而不是「文件存在」或者「bot 看到过这个文件」。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
# admin router 整体挂在 /v1/admin/config 下，QQ 这几个端点跟着走同一个前缀。
_BASE = "/v1/admin/config/qq"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-qq-config"),
            policy=PolicyEngine(workspace_root=tmp_path),
        ),
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(go())

    return call


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "qq.yaml"


def _publish_state(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """模拟 bot 进程写出的 data/qq_live_state.json。"""
    path = _config_path(tmp_path)
    try:
        st = path.stat()
        loaded = {"exists": True, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    except OSError:
        loaded = {"exists": False, "mtime_ns": 0, "size": 0}
    payload: dict[str, Any] = {
        "config_path": str(path),
        "file": dict(loaded),
        "loaded": dict(loaded),
        "parse_error": "",
        "values": {"allowed_groups": [111]},
        "reload": {"allowed_groups": "live"},
        "notes": {},
        "paths": {
            "allowed_groups": "channels.allowed_groups",
            "admin_users": "permissions.admin_users",
            "queue_workers": "queue.workers",
        },
        "runtime": {"pid": 4242, "queue_workers_running": 0},
        "published_at": time.time(),
    }
    payload.update(overrides)
    target = tmp_path / "data" / "qq_live_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


_VALID = "channels:\n  allowed_groups:\n    - 111\n"


class TestAuth:
    def test_reading_requires_a_token(self, client: Any) -> None:
        assert client("GET", f"{_BASE}/raw").status_code == 401

    def test_writing_requires_a_token(self, client: Any) -> None:
        response = client("PUT", f"{_BASE}/raw", json={"content": _VALID})

        assert response.status_code == 401

    def test_state_requires_a_token(self, client: Any) -> None:
        assert client("GET", f"{_BASE}/state").status_code == 401


class TestReadRaw:
    def test_an_absent_file_is_a_normal_state_not_an_error(self, client: Any) -> None:
        response = client("GET", f"{_BASE}/raw", headers=_AUTH)

        assert response.status_code == 200
        assert response.json() == {"content": "", "exists": False}

    def test_it_never_serves_the_example_as_if_it_were_current(
        self, client: Any, tmp_path: Path,
    ) -> None:
        """回落到 qq.example.yaml 会让「打开就保存」悄悄用默认值覆盖 .env 的设置。"""
        response = client("GET", f"{_BASE}/raw", headers=_AUTH)

        assert response.json()["content"] == ""

    def test_it_returns_the_file_verbatim_including_comments(
        self, client: Any, tmp_path: Path,
    ) -> None:
        text = "# 只在 A 群说话\nchannels:\n  allowed_groups: [111]\n"
        path = _config_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

        response = client("GET", f"{_BASE}/raw", headers=_AUTH)

        assert response.json() == {"content": text, "exists": True}


class TestWriteRaw:
    def test_a_valid_document_is_written(self, client: Any, tmp_path: Path) -> None:
        response = client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8")) == {
            "channels": {"allowed_groups": [111]},
        }

    def test_broken_yaml_is_rejected_before_it_reaches_the_bot(
        self, client: Any, tmp_path: Path,
    ) -> None:
        """写坏的文件不会让 bot 崩，但会让它停在旧配置上，而界面显示保存成功。"""
        response = client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": "channels: [oops\n"})

        assert response.status_code == 400
        assert "YAML" in response.json()["detail"]
        assert not _config_path(tmp_path).exists()

    def test_a_non_mapping_top_level_is_rejected(self, client: Any) -> None:
        response = client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": "- a\n- b\n"})

        assert response.status_code == 400

    def test_an_empty_document_is_accepted(self, client: Any, tmp_path: Path) -> None:
        """清空等于「全部交回 .env」，是合法操作。"""
        response = client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": ""})

        assert response.status_code == 200
        assert _config_path(tmp_path).read_text(encoding="utf-8") == ""

    def test_a_rejected_write_leaves_the_previous_file_intact(
        self, client: Any, tmp_path: Path,
    ) -> None:
        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})

        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": "channels: [oops\n"})

        assert _config_path(tmp_path).read_text(encoding="utf-8") == _VALID

    def test_it_leaves_no_temp_file_behind(self, client: Any, tmp_path: Path) -> None:
        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})

        assert list(_config_path(tmp_path).parent.glob("qq.yaml.*")) == []

    def test_it_goes_through_an_atomic_replace(
        self, client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """bot 进程随时在读，直接覆写会让它读到半截 yaml —— 那等于整份配置塌掉。"""
        replaced: list[str] = []
        real_replace = admin_api.os.replace

        def spy(src: Any, dst: Any) -> Any:
            replaced.append(str(dst))
            return real_replace(src, dst)

        monkeypatch.setattr(admin_api.os, "replace", spy)

        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})

        assert replaced == [str(_config_path(tmp_path))]

    def test_an_unconsumed_key_is_rejected_when_the_schema_is_known(
        self, client: Any, tmp_path: Path,
    ) -> None:
        """拼错的键改了不会有任何效果，而界面只会显示保存成功。"""
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "channels:\n  alowed_groups: [111]\n"},
        )

        assert response.status_code == 400
        assert "channels.alowed_groups" in response.json()["detail"]

    def test_a_declared_key_passes_the_schema_check(self, client: Any, tmp_path: Path) -> None:
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "queue:\n  workers: 8\n"},
        )

        assert response.status_code == 200
        assert response.json()["warnings"] == []

    def test_the_version_marker_is_always_allowed(self, client: Any, tmp_path: Path) -> None:
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "version: 1\nqueue:\n  workers: 8\n"},
        )

        assert response.status_code == 200

    def test_an_empty_section_is_not_reported_as_a_typo(self, client: Any, tmp_path: Path) -> None:
        """`channels:` 后面什么都不写是常见写法，不该被当成拼错的键拒掉。"""
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "channels:\nqueue:\n  workers: 8\n"},
        )

        assert response.status_code == 200

    def test_an_explicit_null_hands_the_key_back_to_env(self, client: Any, tmp_path: Path) -> None:
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "channels:\n  allowed_groups: null\n"},
        )

        assert response.status_code == 200

    def test_a_typo_inside_a_known_section_is_still_caught(self, client: Any, tmp_path: Path) -> None:
        _publish_state(tmp_path)

        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "queue:\n  worker: 8\n"},
        )

        assert response.status_code == 400
        assert "queue.worker" in response.json()["detail"]

    def test_without_a_published_schema_it_says_what_it_could_not_check(
        self, client: Any, tmp_path: Path,
    ) -> None:
        """bot 没跑时仍要能改配置，但不能假装校验过键位。"""
        response = client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "channels:\n  alowed_groups: [111]\n"},
        )

        assert response.status_code == 200
        assert response.json()["warnings"] == ["bot 进程未公布配置结构，本次只校验了 YAML 语法"]


class TestState:
    def test_it_says_so_when_the_bot_never_published(self, client: Any) -> None:
        payload = client("GET", f"{_BASE}/state", headers=_AUTH).json()

        assert payload["published"] is False
        assert payload["reason"]
        assert payload["file"]["exists"] is False

    def test_a_matching_fingerprint_reads_as_applied(self, client: Any, tmp_path: Path) -> None:
        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})
        _publish_state(tmp_path)

        payload = client("GET", f"{_BASE}/state", headers=_AUTH).json()

        assert payload["published"] is True
        assert payload["applied"] is True
        assert payload["stale"] is False

    def test_a_newer_file_reads_as_pending(self, client: Any, tmp_path: Path) -> None:
        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})
        _publish_state(tmp_path)

        client(
            "PUT", f"{_BASE}/raw", headers=_AUTH,
            json={"content": "channels:\n  allowed_groups: [111, 222]\n"},
        )

        assert client("GET", f"{_BASE}/state", headers=_AUTH).json()["applied"] is False

    def test_a_kept_old_config_never_reads_as_applied(self, client: Any, tmp_path: Path) -> None:
        """文件写坏时 bot 用的是旧配置，这时候说「已生效」就是无声降级。

        bot 看到的文件指纹照旧等于当前文件，所以判据只能是 values 的来源版本。
        """
        client("PUT", f"{_BASE}/raw", headers=_AUTH, json={"content": _VALID})
        path = _config_path(tmp_path)
        stale = {"exists": True, "mtime_ns": path.stat().st_mtime_ns - 1, "size": 3}
        _publish_state(tmp_path, loaded=stale, parse_error="ParserError: ...")

        payload = client("GET", f"{_BASE}/state", headers=_AUTH).json()

        assert payload["applied"] is False
        assert payload["state"]["parse_error"]

    def test_an_old_publication_reads_as_stale(self, client: Any, tmp_path: Path) -> None:
        _publish_state(tmp_path, published_at=1.0)

        payload = client("GET", f"{_BASE}/state", headers=_AUTH).json()

        assert payload["stale"] is True
        assert payload["age_sec"] > 90.0

    def test_it_passes_through_the_effective_values_and_runtime_facts(
        self, client: Any, tmp_path: Path,
    ) -> None:
        _publish_state(tmp_path)

        state = client("GET", f"{_BASE}/state", headers=_AUTH).json()["state"]

        assert state["values"]["allowed_groups"] == [111]
        assert state["runtime"]["pid"] == 4242

    def test_a_corrupt_state_file_does_not_break_the_endpoint(
        self, client: Any, tmp_path: Path,
    ) -> None:
        target = tmp_path / "data" / "qq_live_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")

        payload = client("GET", f"{_BASE}/state", headers=_AUTH).json()

        assert payload["published"] is False


class TestBotAndSupervisorAgreeOnTheContract:
    """两侧各写一遍字段名就会漂移，而漂移的表现是「已生效」永远为 false。"""

    def test_the_bot_publishes_every_field_the_endpoint_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _onebot_harness import load_live_config, write_live_config

        module = load_live_config(monkeypatch, tmp_path)
        write_live_config(module, allowed_groups=[111])
        payload = module.state_payload()

        assert set(payload["file"]) == {"exists", "mtime_ns", "size"}
        assert payload["loaded"] == payload["file"]
        assert set(payload["paths"]) == set(module.LIVE_KEYS_BY_NAME)

    def test_the_loaded_fingerprint_lags_a_broken_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _onebot_harness import load_live_config, write_live_config

        module = load_live_config(monkeypatch, tmp_path)
        write_live_config(module, allowed_groups=[111])
        good = module.state_payload()["loaded"]

        module.config_path().write_text("channels: [oops\n", encoding="utf-8")
        module.invalidate()
        payload = module.state_payload()

        assert payload["loaded"] == good
        assert payload["file"] != good
        assert payload["values"]["allowed_groups"] == [111]

    def test_a_document_written_in_process_passes_the_endpoint_schema(
        self, client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """测试用 save() 写文件，生产写入走这个端点 —— 两侧认的点分路径必须是同一套。"""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _onebot_harness import load_live_config, write_live_config

        module = load_live_config(monkeypatch, tmp_path)
        write_live_config(module, allowed_groups=[111], queue_workers=8, grant_clear_memory="owner")
        _publish_state(tmp_path, paths=module.state_payload()["paths"])

        response = client(
            "PUT", f"{_BASE}/raw",
            json={"content": _config_path(tmp_path).read_text(encoding="utf-8")},
            headers=_AUTH,
        )

        assert response.status_code == 200, response.text
        assert response.json()["warnings"] == []


def _seed_trigger_policy(tmp_path: Path) -> None:
    """把判定模块放进这个 tmp 工作区，dry-run 按 workspace_root 找它。"""
    target = tmp_path / "adapters" / "onebot" / "trigger_policy.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "trigger_policy.py"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


_DRY_RUN_CONFIG = {
    "group_trigger": "mention_only",
    "signal_all": None, "signal_at": None, "signal_reply": None, "signal_prefix": None,
    "signal_name": True, "signal_keyword": False, "signal_random": False,
    "name_words": ["咪啪"], "name_anywhere": True, "keyword_words": [],
    "random_probability": 0.02, "trigger_prefixes": ["!", "！", "/"],
    "llm_intent_enabled": False, "llm_intent_timeout_sec": 8.0,
    "cooldown_group_sec": 0.0, "cooldown_user_sec": 0.0, "cooldown_exempt_at": True,
}


class TestTriggerDryRun:
    def test_it_requires_a_token(self, client: Any) -> None:
        response = client("POST", f"{_BASE}/trigger/dry-run", json={"messages": []})

        assert response.status_code == 401

    def test_messages_must_be_an_array(self, client: Any, tmp_path: Path) -> None:
        _seed_trigger_policy(tmp_path)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={"messages": "咪啪在吗", "config": _DRY_RUN_CONFIG},
        )

        assert response.status_code == 400

    def test_a_huge_batch_is_refused(self, client: Any, tmp_path: Path) -> None:
        _seed_trigger_policy(tmp_path)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={"messages": [{"text": "x"}] * 201, "config": _DRY_RUN_CONFIG},
        )

        assert response.status_code == 400

    def test_it_judges_a_batch(self, client: Any, tmp_path: Path) -> None:
        _seed_trigger_policy(tmp_path)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={
                "config": _DRY_RUN_CONFIG,
                "messages": [
                    {"text": "咪啪在吗", "user_id": 1, "group_id": 9},
                    {"text": "随便聊聊", "user_id": 2, "group_id": 9},
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert [r["triggered"] for r in payload["results"]] == [True, False]
        assert payload["results"][0]["signal"] == "name"

    def test_without_a_config_it_uses_what_the_bot_published(
        self, client: Any, tmp_path: Path,
    ) -> None:
        """「试听当前配置」不该要求调用方自己拼一份 —— 拼错了就等于试听骗人。"""
        _seed_trigger_policy(tmp_path)
        _publish_state(tmp_path, values=_DRY_RUN_CONFIG)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={"messages": [{"text": "咪啪在吗", "user_id": 1, "group_id": 9}]},
        )

        assert response.status_code == 200
        assert response.json()["results"][0]["triggered"] is True

    def test_without_a_config_and_without_a_published_snapshot_it_says_so(
        self, client: Any, tmp_path: Path,
    ) -> None:
        _seed_trigger_policy(tmp_path)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={"messages": [{"text": "咪啪在吗"}]},
        )

        assert response.status_code == 409

    def test_an_incomplete_config_is_a_client_error(self, client: Any, tmp_path: Path) -> None:
        """自拼 config 少了键是调用方的问题，不该报成 500。"""
        _seed_trigger_policy(tmp_path)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={"messages": [{"text": "x"}], "config": {"group_trigger": "all"}},
        )

        assert response.status_code == 400

    def test_cooldown_can_be_switched_off_for_the_preview(self, client: Any, tmp_path: Path) -> None:
        _seed_trigger_policy(tmp_path)
        config = dict(_DRY_RUN_CONFIG, cooldown_group_sec=30.0)

        response = client(
            "POST", f"{_BASE}/trigger/dry-run", headers=_AUTH,
            json={
                "config": config, "cooldown": False,
                "messages": [{"text": "咪啪", "user_id": 1, "group_id": 9}] * 2,
            },
        )

        assert [r["triggered"] for r in response.json()["results"]] == [True, True]
