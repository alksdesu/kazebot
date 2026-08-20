"""dream 的整理任务不该出现在控制台的「会话上下文」里。

它借 memory namespace 当 conversation_key 建任务（save_memory 靠它推目录），
supervisor 于是给它开一条 session。那条 session 反查出来的归属和真实群一模一样，
于是同一个群在页面上并排出现两行，点删除还没有意义。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
import engine.data_cleanup as cleanup  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.conversation_labels import is_internal_task_key, memory_namespace  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_GROUP_KEY = "qq_group:5edca2992de82f26297b3a68"
_TOKEN = "test-admin-token"


class TestInternalTaskKey:
    def test_a_namespace_shaped_key_is_internal(self) -> None:
        assert is_internal_task_key(memory_namespace(_GROUP_KEY))

    @pytest.mark.parametrize("key", ["user_UserB", "user_UserAX", "user_A", "user_a_b_2"])
    def test_a_profile_namespace_is_internal_too(self, key: str) -> None:
        # 人物档案整理任务借的是 user_<别名>，形态跟会话整理不一样。
        assert is_internal_task_key(key)

    @pytest.mark.parametrize("key", [
        _GROUP_KEY,
        "qq_private:3a323fe3b358d57c996bcba7",
        "web:console",
        "cli:default",
        "smoke",
        "",
    ])
    def test_real_conversation_keys_are_not(self, key: str) -> None:
        assert not is_internal_task_key(key)

    @pytest.mark.parametrize("key", [
        "conv_short",
        "conv_ZZZZZZZZZZZZZZZZZZZZZZZZ",
        "conv_7f777f6eb8df09542755b014x",
        "user_",
        "user_1abc",
        "user_a-b",
        "users_UserB",
    ])
    def test_lookalikes_are_not_swallowed(self, key: str) -> None:
        # 判据一旦放宽就会开始隐藏真实会话，那比多一行更糟。
        assert not is_internal_task_key(key)

    @pytest.mark.parametrize("key", [
        "conv_7f777f6eb8df09542755b014", "user_UserB", "user_A",
        "qq_group:5edca2992de82f26297b3a68", "qq_private:abc", "smoke", "",
        "conv_short", "user_1abc", "user_a-b", "users_UserB",
    ])
    def test_the_cleanup_side_agrees(self, key: str) -> None:
        # 两份独立实现 —— engine 不该为了一个正则去依赖 supervisor。靠这条锁住不漂移。
        assert bool(cleanup._TASK_CONVERSATION_KEY_RE.match(key)) is is_internal_task_key(key)

    def test_it_agrees_with_the_namespace_algorithm(self) -> None:
        # 真实会话键算出的 namespace 必然是内部形态 —— 两个函数说的是同一件事。
        assert is_internal_task_key(memory_namespace("qq_group:abc"))


def _seed(tmp_path: Path, sessions: dict[str, dict], conv_files: dict[str, str]) -> None:
    data = tmp_path / "data"
    (data / "conversations").mkdir(parents=True, exist_ok=True)
    (data / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    for session_id, body in conv_files.items():
        (data / "conversations" / f"{session_id}.jsonl").write_text(body, encoding="utf-8")


def _session(session_id: str, conversation_key: str, channel: str) -> dict:
    return {"session_id": session_id, "conversation_key": conversation_key, "channel": channel}


class TestConsoleListing:
    @pytest.fixture()
    def rows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
        monkeypatch.setattr(admin_api, "_admin_token", "")
        namespace = memory_namespace(_GROUP_KEY)
        _seed(
            tmp_path,
            {
                "real": _session("real", _GROUP_KEY, "qq_group"),
                "ghost": _session("ghost", namespace, "system"),
            },
            {"real": '{"role":"user"}\n', "ghost": '{"role":"user"}\n', "orphan": '{"role":"user"}\n'},
        )
        state = SupervisorState(
            workspace_root=tmp_path,
            eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-vis"),
            policy=PolicyEngine(workspace_root=tmp_path),
        )
        app = create_app(
            state=state,
            process_manager=None,
            config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
        )

        async def _call() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(
                    "/v1/admin/conversations", headers={"Authorization": f"Bearer {_TOKEN}"},
                )

        resp = asyncio.run(_call())
        assert resp.status_code == 200
        return resp.json()["conversations"]

    def test_the_dream_task_session_is_hidden(self, rows: list[dict]) -> None:
        assert "ghost" not in {row["session_id"] for row in rows}

    def test_the_real_group_session_still_shows(self, rows: list[dict]) -> None:
        assert "real" in {row["session_id"] for row in rows}

    def test_an_orphan_jsonl_still_shows(self, rows: list[dict]) -> None:
        # session 没了但文件还在的，藏起来就永远没人删。
        assert "orphan" in {row["session_id"] for row in rows}

    def test_the_group_appears_exactly_once(self, rows: list[dict]) -> None:
        keys = [row["conversation_key"] for row in rows if row["conversation_key"]]
        assert keys.count(_GROUP_KEY) == 1


class TestCleanup:
    @pytest.fixture()
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", False, raising=False)
        namespace = memory_namespace(_GROUP_KEY)
        _seed(
            tmp_path,
            {
                "real": _session("real", _GROUP_KEY, "qq_group"),
                "ghost": _session("ghost", namespace, "system"),
                "profile": _session("profile", "user_UserB", "system"),
            },
            {"real": "x\n", "ghost": "x\n", "profile": "x\n"},
        )
        return tmp_path

    def _age(self, tmp_path: Path, session_id: str, days: float) -> None:
        path = tmp_path / "data" / "conversations" / f"{session_id}.jsonl"
        old = time.time() - days * 24 * 3600
        os.utime(path, (old, old))

    def _exists(self, tmp_path: Path, session_id: str) -> bool:
        return (tmp_path / "data" / "conversations" / f"{session_id}.jsonl").exists()

    def test_an_aged_task_conversation_is_removed(self, env: Path) -> None:
        self._age(env, "ghost", 30)

        cleanup.purge_expired_task_conversations()

        assert not self._exists(env, "ghost")

    def test_a_fresh_one_is_kept(self, env: Path) -> None:
        cleanup.purge_expired_task_conversations()

        assert self._exists(env, "ghost")

    def test_a_real_conversation_is_never_touched(self, env: Path) -> None:
        # 群聊记录再老也归 14 天记忆策略和人工删除管，不该被这一段顺手带走。
        self._age(env, "real", 3650)

        cleanup.purge_expired_task_conversations()

        assert self._exists(env, "real")

    def test_a_profile_task_conversation_is_removed_too(self, env: Path) -> None:
        self._age(env, "profile", 30)

        cleanup.purge_expired_task_conversations()

        assert not self._exists(env, "profile")

    def test_a_broken_sessions_file_is_survivable(self, env: Path) -> None:
        (env / "data" / "sessions.json").write_text("{ not json", encoding="utf-8")
        self._age(env, "ghost", 30)

        cleanup.purge_expired_task_conversations()

        assert self._exists(env, "ghost")

    def test_a_missing_sessions_file_is_survivable(self, env: Path) -> None:
        (env / "data" / "sessions.json").unlink()

        cleanup.purge_expired_task_conversations()

        assert self._exists(env, "ghost")

    def test_zero_age_disables_it(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cleanup, "TASK_CONVERSATION_MAX_AGE", 0.0)
        self._age(env, "ghost", 30)

        cleanup.purge_expired_task_conversations()

        assert self._exists(env, "ghost")

    def test_it_is_wired_into_the_task_list(self) -> None:
        # 写了函数不注册 = 永远不跑，这个仓库栽过一次。
        source = (_ROOT / "engine" / "data_cleanup.py").read_text(encoding="utf-8")
        assert "(purge_expired_task_conversations, {})," in source
