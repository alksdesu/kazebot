"""bot 向控制台公布的运行期事实。

这份 payload 会经 data/qq_live_state.json 进 GET /qq/state，任何持管理令牌的人都能读到，
所以密钥只能报「设没设」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime  # noqa: E402

_SECRET = "s3cr3t-hash-salt-value"
_BRIDGE_TOKEN = "bridge-token-do-not-leak"


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_runtime(monkeypatch, tmp_path)


class TestSecretsNeverLeave:
    def test_the_hash_secret_is_reported_as_a_flag_not_a_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = _load(monkeypatch, tmp_path, ONEBOT_CONVERSATION_HASH_SECRET=_SECRET)

        facts = runtime._live_runtime_facts()

        assert facts["environment"]["hash_secret_set"] is True
        assert _SECRET not in repr(facts)

    def test_an_unset_hash_secret_reads_as_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET", raising=False)
        runtime = _load(monkeypatch, tmp_path)

        assert runtime._live_runtime_facts()["environment"]["hash_secret_set"] is False

    def test_the_bridge_token_is_reported_as_a_flag_not_a_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = _load(monkeypatch, tmp_path, ONEBOT_FORWARD_BRIDGE_TOKEN=_BRIDGE_TOKEN)

        facts = runtime._live_runtime_facts()

        assert facts["forward_bridge_token_set"] is True
        assert _BRIDGE_TOKEN not in repr(facts)

    def test_the_admin_token_never_appears(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # 控制台自己就带着这个令牌，没有任何理由让 bot 再公布一遍。
        runtime = _load(monkeypatch, tmp_path, CLONOTH_ADMIN_TOKEN="admin-token-do-not-leak")

        assert "admin-token-do-not-leak" not in repr(runtime._live_runtime_facts())


class TestConnectionIsReportedHonestly:
    def test_no_bot_attached_reads_as_disconnected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # 夹具的 get_bot 返回 None 而不是抛 —— 「返回空」照样是没连上。
        runtime = _load(monkeypatch, tmp_path)

        assert runtime._live_runtime_facts()["onebot_connected"] is False

    def test_a_raising_get_bot_reads_as_disconnected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = _load(monkeypatch, tmp_path)

        def boom() -> None:
            raise ValueError("no bots connected")

        monkeypatch.setattr(runtime, "get_bot", boom)

        assert runtime._live_runtime_facts()["onebot_connected"] is False

    def test_an_attached_bot_reads_as_connected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = _load(monkeypatch, tmp_path)
        monkeypatch.setattr(runtime, "get_bot", lambda: object())

        assert runtime._live_runtime_facts()["onebot_connected"] is True


class TestTheShapeTheConsoleReads:
    def test_the_environment_block_names_where_things_live(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        runtime = _load(monkeypatch, tmp_path)

        env = runtime._live_runtime_facts()["environment"]

        assert env["workspace"] == str(tmp_path)
        assert env["supervisor_url"]

    def test_volatile_counters_stay_in_their_own_layer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # 心跳去重只看 volatile 之外的部分，把每秒都在动的计数挪出去会让文件被反复重写。
        runtime = _load(monkeypatch, tmp_path)

        facts = runtime._live_runtime_facts()

        assert set(facts["volatile"]) == {"queue_pending", "cached_groups", "history_gap_groups"}
        assert "queue_pending" not in facts

    def test_an_overflowing_group_shows_up_in_the_gap_counter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # 溢出只写日志的话，运维在控制台上看不出这个群的历史断过。
        runtime = _load(monkeypatch, tmp_path, ONEBOT_GROUP_HISTORY_MAX="2")
        for index in range(4):
            runtime._append_group_history(7, f"line{index}")

        assert runtime._live_runtime_facts()["volatile"]["history_gap_groups"] == 1


class TestDigestSaltModeIsReported:
    def test_a_clean_workspace_reports_salted_without_leaking_the_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET", raising=False)
        runtime = _load(monkeypatch, tmp_path)

        facts = runtime._live_runtime_facts()

        assert facts["environment"]["conversation_digest_salted"] is True
        # 自动生成的密钥只在 bot 进程内，绝不能出现在公布给控制台的 payload 里。
        assert runtime._CONVERSATION_SECRET
        assert runtime._CONVERSATION_SECRET not in repr(facts)

    def test_a_legacy_pinned_workspace_reports_unsalted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("ONEBOT_CONVERSATION_HASH_SECRET", raising=False)
        state = tmp_path / "data" / "onebot_plugin_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"real_conversation_keys": {}}', encoding="utf-8")

        runtime = _load(monkeypatch, tmp_path)

        env = runtime._live_runtime_facts()["environment"]
        assert env["conversation_digest_salted"] is False
        assert env["hash_secret_set"] is False
