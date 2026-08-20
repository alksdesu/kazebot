"""/整理记忆 的权限门与状态回执。

dream 动的是全部会话和人物档案，不限本群 —— 这道门给错人，等于让任何群成员随时
启动一轮全局记忆改写。回执也要分得清「已开始 / 还在跑 / 没接住」，否则用户只能干等。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402

_ADMIN_QQ = 10001
_MEMBER_QQ = 30003
_CONV = "qq_group:bc3f12b0621298e191a49fa7"


class _StubClient:
    def __init__(self, status: str = "started", boom: Exception | None = None) -> None:
        self.status = status
        self.boom = boom
        self.calls: list[dict[str, Any]] = []

    async def run_dream_now(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.boom is not None:
            raise self.boom
        return {"ok": self.status == "started", "status": self.status}


def _ask(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    user_id: int,
    text: str,
    client: Any,
) -> str | None:
    monkeypatch.setattr(runtime, "_client", client)
    return asyncio.run(runtime._maybe_handle_dream_command(
        event=SimpleNamespace(user_id=user_id), user_text=text, conversation_key=_CONV,
    ))


@pytest.fixture()
def admin_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime = load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({_ADMIN_QQ}))
    return runtime


class TestRecognition:
    @pytest.mark.parametrize("text", ["/整理记忆", "/记忆整理", "/整理一下记忆", "/dream", "/DREAM"])
    def test_the_aliases_all_land(self, admin_runtime, monkeypatch, text: str) -> None:
        client = _StubClient()

        assert _ask(admin_runtime, monkeypatch, _ADMIN_QQ, text, client) is not None

    @pytest.mark.parametrize("text", ["整理记忆", "帮我整理一下记忆好吗", "/清除群记忆", "", "/整理"])
    def test_everything_else_falls_through(self, admin_runtime, monkeypatch, text: str) -> None:
        # 返回 None 才会继续走聊天；误判会把普通对话吞掉。
        assert _ask(admin_runtime, monkeypatch, _ADMIN_QQ, text, _StubClient()) is None

    def test_a_non_command_never_reaches_the_client(self, admin_runtime, monkeypatch) -> None:
        client = _StubClient()

        _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "今天聊了好多", client)

        assert client.calls == []


class TestAuthorization:
    def test_a_member_is_refused(self, monkeypatch, tmp_path) -> None:
        runtime = load_runtime(monkeypatch, tmp_path)

        reply = _ask(runtime, monkeypatch, _MEMBER_QQ, "/整理记忆", _StubClient())

        assert reply is not None
        assert "开始整理" not in reply

    def test_a_refusal_never_triggers_anything(self, monkeypatch, tmp_path) -> None:
        runtime = load_runtime(monkeypatch, tmp_path)
        client = _StubClient()

        _ask(runtime, monkeypatch, _MEMBER_QQ, "/整理记忆", client)

        assert client.calls == []

    def test_the_admin_gets_through(self, admin_runtime, monkeypatch) -> None:
        client = _StubClient()

        _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", client)

        assert len(client.calls) == 1


class TestNotifyTarget:
    def test_the_conversation_is_handed_over(self, admin_runtime, monkeypatch) -> None:
        # 不带会话键的话，几分钟后跑完的摘要没有地方可去。
        client = _StubClient()

        _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", client)

        assert client.calls[0]["notify_conversation_key"] == _CONV


class TestReplies:
    def test_started_says_it_will_report_back(self, admin_runtime, monkeypatch) -> None:
        reply = _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", _StubClient("started"))

        assert "开始整理" in reply

    def test_busy_says_to_wait(self, admin_runtime, monkeypatch) -> None:
        reply = _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", _StubClient("busy"))

        assert "还在跑" in reply

    def test_unavailable_is_not_dressed_up_as_success(self, admin_runtime, monkeypatch) -> None:
        reply = _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", _StubClient("unavailable"))

        assert "开始整理" not in reply

    def test_a_transport_error_is_reported_not_raised(self, admin_runtime, monkeypatch) -> None:
        client = _StubClient(boom=RuntimeError("connection refused"))

        reply = _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", client)

        assert reply is not None
        assert "失败" in reply

    def test_an_uninitialised_client_says_so(self, admin_runtime, monkeypatch) -> None:
        reply = _ask(admin_runtime, monkeypatch, _ADMIN_QQ, "/整理记忆", None)

        assert reply is not None
        assert "开始整理" not in reply


class TestCatalog:
    def test_the_command_is_listed_for_the_admin(self, admin_runtime) -> None:
        # 命令表是 /帮助 的唯一数据源，漏了它用户就永远不知道这条命令存在。
        text = admin_runtime._help_text(SimpleNamespace(user_id=_ADMIN_QQ))

        assert "/整理记忆" in text

    def test_it_stays_hidden_from_members(self, monkeypatch, tmp_path) -> None:
        runtime = load_runtime(monkeypatch, tmp_path)

        assert "/整理记忆" not in runtime._help_text(SimpleNamespace(user_id=_MEMBER_QQ))
