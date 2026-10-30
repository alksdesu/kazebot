"""脚本工具往自己会话发中间回复的窄凭据。

脚本子进程拿不到管理令牌（safe_subprocess_env 剥掉了它）。engine 按会话派生一个 HMAC 凭据放进
CLONOTH_EVENT_TOKEN，脚本带 X-Clonoth-Event-Token，supervisor 只认它发到那一个会话的 intermediate_reply。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_runtime import AUTH_BACKOFF_MAX_SEC, retry_after_sec, session_event_token  # noqa: E402
from toolbox.registry import _script_tool_env  # noqa: E402


class TestSessionEventToken:
    def test_it_is_deterministic_for_a_session(self) -> None:
        assert session_event_token("admin", "sess-1") == session_event_token("admin", "sess-1")

    def test_a_different_session_gets_a_different_token(self) -> None:
        assert session_event_token("admin", "sess-1") != session_event_token("admin", "sess-2")

    def test_a_different_admin_token_gives_a_different_value(self) -> None:
        assert session_event_token("admin-a", "sess-1") != session_event_token("admin-b", "sess-1")

    def test_it_does_not_echo_the_admin_token(self) -> None:
        token = session_event_token("super-secret-admin", "sess-1")
        assert "super-secret-admin" not in token

    @pytest.mark.parametrize("admin,session", [("", "sess"), ("admin", ""), ("", "")])
    def test_missing_inputs_give_an_empty_token(self, admin: str, session: str) -> None:
        assert session_event_token(admin, session) == ""


class TestScriptEnvInjection:
    def _ctx(self, tmp_path: Path, **over) -> SimpleNamespace:
        base = dict(
            workspace_root=tmp_path, supervisor_url="http://sup", session_id="branch-1",
            parent_session_id="main-1", task_id="task-1", node_id="n", conversation_key="qq:1",
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_the_event_token_is_derived_for_the_parent_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-token")
        env = _script_tool_env(self._ctx(tmp_path))
        # 工具可能跑在入口分支里，中间回复按父会话路由，凭据也按父会话派生
        assert env["CLONOTH_EVENT_TOKEN"] == session_event_token("admin-token", "main-1")

    def test_without_a_parent_it_uses_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-token")
        env = _script_tool_env(self._ctx(tmp_path, parent_session_id=""))
        assert env["CLONOTH_EVENT_TOKEN"] == session_event_token("admin-token", "branch-1")

    def test_an_inherited_event_token_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 父进程环境里的旧值不能漏进来：它对应的是别的会话
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "admin-token")
        monkeypatch.setenv("CLONOTH_EVENT_TOKEN", "stale-from-parent")
        env = _script_tool_env(self._ctx(tmp_path))
        assert env["CLONOTH_EVENT_TOKEN"] != "stale-from-parent"

    def test_no_admin_token_means_no_event_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLONOTH_ADMIN_TOKEN", raising=False)
        env = _script_tool_env(self._ctx(tmp_path))
        assert "CLONOTH_EVENT_TOKEN" not in env


class TestRetryAfter:
    def test_it_reads_the_header(self) -> None:
        assert retry_after_sec({"Retry-After": "5"}, fallback=0.5) == 5.0

    def test_it_is_never_below_the_fallback(self) -> None:
        assert retry_after_sec({"Retry-After": "0"}, fallback=0.5) == 0.5

    def test_it_is_capped(self) -> None:
        assert retry_after_sec({"Retry-After": "9999"}, fallback=0.5) == AUTH_BACKOFF_MAX_SEC

    @pytest.mark.parametrize("value", ["", "soon", "nan", "inf"])
    def test_an_unreadable_header_falls_back(self, value: str) -> None:
        assert retry_after_sec({"Retry-After": value}, fallback=0.5) == 0.5

    def test_a_missing_header_falls_back(self) -> None:
        assert retry_after_sec({}, fallback=0.5) == 0.5
