"""策略的改动冲突检测：读出来的内容和指纹出自同一份字节，保存时交回指纹。

policy.yaml 是整套权限判定的根，共享组里几个实例的控制台开的是同一份。不比指纹，后保存的那个人
就会把先保存的那份整份盖掉；内容和指纹分两次读文件，也会把 A 的规则配上 B 的指纹交出去。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
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
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_TOKEN = "test-policy-version-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_STALE = "0" * 64


def _policy(*roots: str, deny: str = "secret/**") -> dict[str, Any]:
    return {
        "version": 1,
        "extra_roots": list(roots),
        "read_file": {"default": "auto", "rules": [{"pattern": deny, "decision": "deny", "reason": "测试规则"}]},
    }


@pytest.fixture
def engine(tmp_path: Path) -> PolicyEngine:
    return PolicyEngine(workspace_root=tmp_path)


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    return config_layout.policy_path(tmp_path)


# ---------------------------------------------------------------------------
#  引擎
# ---------------------------------------------------------------------------

class TestExportVersioned:
    def test_the_hash_is_the_file_it_came_from(self, engine: PolicyEngine, policy_path: Path) -> None:
        policy, digest = engine.export_versioned()

        assert digest == config_layout.file_hash(policy_path)
        assert policy["version"] == 1

    def test_export_config_is_just_the_first_half(self, engine: PolicyEngine) -> None:
        assert engine.export_config() == engine.export_versioned()[0]

    def test_the_defaults_are_filled_in_for_the_editor(self, engine: PolicyEngine) -> None:
        # 文件里没写 sensitive_path_patterns 时运行期用的是内置清单，回原文会让界面显示成空。
        policy, _ = engine.export_versioned()

        assert policy["execute_command"]["sensitive_path_patterns"]

    def test_content_and_hash_come_from_one_read(self, engine: PolicyEngine, policy_path: Path) -> None:
        """分两次读文件，就会把 A 的规则配上 B 的指纹交出去，保存时凭空覆盖掉 B。"""
        engine.replace_config(_policy("/srv/a"))
        first, first_hash = engine.export_versioned()

        config_layout.write_config_text(policy_path, yaml.safe_dump(_policy("/srv/b"), sort_keys=False))

        second, second_hash = engine.export_versioned()
        assert first["extra_roots"] == ["/srv/a"] and first_hash != second_hash
        assert second["extra_roots"] == ["/srv/b"]
        assert second_hash == config_layout.file_hash(policy_path)

    def test_a_same_tick_rewrite_moves_both_together(self, engine: PolicyEngine, policy_path: Path) -> None:
        import os

        engine.replace_config(_policy("/srv/aa"))
        before = policy_path.stat()
        text = yaml.safe_dump(_policy("/srv/bb"), sort_keys=False, allow_unicode=True)
        assert len(text.encode("utf-8")) == before.st_size

        config_layout.write_config_text(policy_path, text)
        os.utime(policy_path, ns=(before.st_atime_ns, before.st_mtime_ns))

        policy, digest = engine.export_versioned()
        assert policy["extra_roots"] == ["/srv/bb"]
        assert digest == config_layout.file_hash(policy_path)


class TestReplaceConfig:
    def test_it_returns_what_landed_and_its_hash(self, engine: PolicyEngine, policy_path: Path) -> None:
        saved, digest = engine.replace_config(_policy("/srv/a"))

        assert saved["extra_roots"] == ["/srv/a"]
        assert digest == config_layout.file_hash(policy_path)
        assert engine.export_versioned() == (saved, digest)

    def test_the_hash_from_the_reader_is_accepted(self, engine: PolicyEngine, policy_path: Path) -> None:
        _, opened = engine.export_versioned()

        saved, digest = engine.replace_config(_policy("/srv/a"), base_hash=opened)

        assert saved["extra_roots"] == ["/srv/a"]
        assert digest != opened

    def test_a_stale_hash_is_a_conflict_and_writes_nothing(
        self, engine: PolicyEngine, policy_path: Path,
    ) -> None:
        engine.replace_config(_policy("/srv/a"))
        before = policy_path.read_bytes()

        with pytest.raises(config_layout.ConfigConflict):
            engine.replace_config(_policy("/srv/b"), base_hash=_STALE)

        assert policy_path.read_bytes() == before
        assert engine.export_versioned()[0]["extra_roots"] == ["/srv/a"]

    def test_another_instance_saving_first_wins(self, tmp_path: Path, policy_path: Path) -> None:
        mine = PolicyEngine(workspace_root=tmp_path)
        theirs = PolicyEngine(workspace_root=tmp_path)
        _, opened = mine.export_versioned()

        theirs.replace_config(_policy("/srv/theirs"))

        with pytest.raises(config_layout.ConfigConflict):
            mine.replace_config(_policy("/srv/mine"), base_hash=opened)
        assert mine.export_versioned()[0]["extra_roots"] == ["/srv/theirs"]

    def test_no_base_hash_means_no_check(self, engine: PolicyEngine) -> None:
        engine.replace_config(_policy("/srv/a"))

        saved, _ = engine.replace_config(_policy("/srv/b"))

        assert saved["extra_roots"] == ["/srv/b"]

    @pytest.mark.parametrize("bad", ["不是映射", ["列表"], 42, {"read_file": "不是段"}])
    def test_an_invalid_policy_writes_nothing(
        self, engine: PolicyEngine, policy_path: Path, bad: Any,
    ) -> None:
        # 解析不了的 yaml 会让重读静默退回内置默认，策略被换掉却看不出任何迹象。
        engine.replace_config(_policy("/srv/a"))
        before = policy_path.read_bytes()

        with pytest.raises(ValueError):
            engine.replace_config(bad)

        assert policy_path.read_bytes() == before

    def test_the_new_rules_take_effect_at_once(self, engine: PolicyEngine) -> None:
        assert engine.evaluate_read_file(path="secret/a.txt").safety_level == SafetyLevel.auto

        engine.replace_config(_policy(deny="secret/**"))

        assert engine.evaluate_read_file(path="secret/a.txt").safety_level == SafetyLevel.deny


class TestADeletedPolicyFile:
    def test_the_last_good_rules_stay_in_force(self, engine: PolicyEngine, policy_path: Path) -> None:
        engine.replace_config(_policy(deny="secret/**"))
        assert engine.evaluate_read_file(path="secret/a.txt").safety_level == SafetyLevel.deny

        policy_path.unlink()

        # 文件没了不等于放开权限：规则照旧用上一份。
        assert engine.evaluate_read_file(path="secret/a.txt").safety_level == SafetyLevel.deny
        assert engine.export_versioned()[0]["extra_roots"] == []

    def test_the_hash_becomes_the_empty_string(self, engine: PolicyEngine, policy_path: Path) -> None:
        engine.replace_config(_policy("/srv/a"))
        assert engine.export_versioned()[1]

        policy_path.unlink()

        assert engine.export_versioned()[1] == ""

    def test_saving_against_the_empty_hash_recreates_it(
        self, engine: PolicyEngine, policy_path: Path,
    ) -> None:
        engine.replace_config(_policy("/srv/a"))
        policy_path.unlink()
        _, opened = engine.export_versioned()
        assert opened == ""

        saved, digest = engine.replace_config(_policy("/srv/b"), base_hash=opened)

        assert policy_path.exists()
        assert saved["extra_roots"] == ["/srv/b"]
        assert digest == config_layout.file_hash(policy_path)

    def test_someone_recreating_it_first_is_a_conflict(
        self, engine: PolicyEngine, policy_path: Path,
    ) -> None:
        engine.replace_config(_policy("/srv/a"))
        policy_path.unlink()
        _, opened = engine.export_versioned()

        config_layout.write_config_text(policy_path, yaml.safe_dump(_policy("/srv/theirs"), sort_keys=False))

        with pytest.raises(config_layout.ConfigConflict):
            engine.replace_config(_policy("/srv/mine"), base_hash=opened)


class TestTheExportIsAtomic:
    def test_the_pair_is_never_mixed_across_versions(self, tmp_path: Path) -> None:
        """重读和导出不在同一把锁里的话，会交出 A 的规则配 B 的指纹。"""
        engine = PolicyEngine(workspace_root=tmp_path)
        path = config_layout.policy_path(tmp_path)
        versions = {
            config_layout.bytes_hash(
                yaml.safe_dump(_policy(root), sort_keys=False, allow_unicode=True).encode("utf-8"),
            ): [root]
            for root in ("/srv/a", "/srv/b")
        }
        texts = [yaml.safe_dump(_policy(root), sort_keys=False, allow_unicode=True) for root in ("/srv/a", "/srv/b")]
        config_layout.write_config_text(path, texts[0])
        stop = threading.Event()
        seen: list[tuple[list[str], str]] = []

        def writer() -> None:
            index = 0
            while not stop.is_set():
                config_layout.write_config_text(path, texts[index % 2])
                index += 1
                time.sleep(0)

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        deadline = time.monotonic() + 0.3
        try:
            while time.monotonic() < deadline:
                policy, digest = engine.export_versioned()
                seen.append((policy["extra_roots"], digest))
                # 让出一下，否则读的这一侧一口气跑完，写的那侧根本没被调度过。
                time.sleep(0)
        finally:
            stop.set()
            worker.join(timeout=30)

        for roots, digest in seen:
            # 指纹认得出的那一版，内容必须就是它。
            assert versions[digest] == roots, f"{digest} 的内容应当是 {versions[digest]}，拿到的是 {roots}"
        # 从头到尾只读到一个版本的话，这条用例什么都没验到。
        assert len({digest for _roots, digest in seen}) == 2


# ---------------------------------------------------------------------------
#  GET / PUT /v1/config/policy
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-policy"),
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


def _get_policy(api: Any) -> dict[str, Any]:
    response = api("GET", "/v1/config/policy")
    assert response.status_code == 200, response.text
    return response.json()


def _events(root: Path, type_: str) -> list[dict[str, Any]]:
    path = root / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [
        event for event in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if event.get("type") == type_
    ]


class TestThePolicyEndpoint:
    def test_the_reader_hands_out_a_hash(self, api: Any, tmp_path: Path) -> None:
        body = _get_policy(api)

        assert set(body) == {"policy", "hash"}
        assert body["hash"] == config_layout.file_hash(config_layout.policy_path(tmp_path))

    def test_that_hash_round_trips_through_a_save(self, api: Any, tmp_path: Path) -> None:
        opened = _get_policy(api)["hash"]

        response = api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/a"), "base_hash": opened})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["policy"]["extra_roots"] == ["/srv/a"]
        assert body["hash"] == config_layout.file_hash(config_layout.policy_path(tmp_path))
        assert body["hash"] != opened
        assert _get_policy(api)["hash"] == body["hash"]

    def test_a_stale_hash_is_a_conflict(self, api: Any, tmp_path: Path) -> None:
        api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/a")})
        path = config_layout.policy_path(tmp_path)
        before = path.read_bytes()

        response = api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/b"), "base_hash": _STALE})

        assert response.status_code == 409, response.text
        assert path.read_bytes() == before

    def test_an_invalid_policy_is_still_four_hundred(self, api: Any, tmp_path: Path) -> None:
        api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/a")})
        path = config_layout.policy_path(tmp_path)
        before = path.read_bytes()

        response = api("PUT", "/v1/config/policy", json={"policy": {"read_file": "不是段"}})

        assert response.status_code == 400, response.text
        assert path.read_bytes() == before

    def test_an_invalid_policy_with_a_good_hash_is_still_four_hundred(self, api: Any) -> None:
        # 校验在写之前，指纹对不对不改变这一步的结论。
        opened = _get_policy(api)["hash"]

        response = api("PUT", "/v1/config/policy", json={"policy": {"read_file": "不是段"}, "base_hash": opened})

        assert response.status_code == 400, response.text

    def test_a_save_without_a_hash_still_goes_through(self, api: Any) -> None:
        response = api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/a")})

        assert response.status_code == 200, response.text
        assert response.json()["policy"]["extra_roots"] == ["/srv/a"]

    def test_the_audit_entry_is_unchanged(self, api: Any, tmp_path: Path) -> None:
        # 策略是整套权限判定的根，改了什么必须能事后查。
        api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/a", deny="secret/**")})

        events = _events(tmp_path, "policy_updated")

        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["read_rules"] == 1
        assert payload["write_rules"] == 0
        assert payload["command_default"]
        assert payload["policy"]["extra_roots"] == ["/srv/a"]

    def test_a_refused_save_writes_no_audit_entry(self, api: Any, tmp_path: Path) -> None:
        api("PUT", "/v1/config/policy", json={"policy": _policy("/srv/b"), "base_hash": _STALE})
        api("PUT", "/v1/config/policy", json={"policy": {"read_file": "不是段"}})

        assert _events(tmp_path, "policy_updated") == []

    def test_the_saved_rules_are_in_force_for_the_next_call(self, api: Any, tmp_path: Path) -> None:
        api("PUT", "/v1/config/policy", json={"policy": _policy(deny="secret/**")})

        body = _get_policy(api)

        assert body["policy"]["read_file"]["rules"][0]["pattern"] == "secret/**"
