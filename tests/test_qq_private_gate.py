"""私聊入口的闸门：未放行的人一个字都不该收到，命令分支排在闸门之后。

_handle_private_agent 现有测试没覆盖过，闸门位置与静默行为都靠这份钉住。
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

LISTED_QQ = 10001
STRANGER_QQ = 40004


class _Finished(Exception):
    pass


class _FakeMatcher:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def finish(self, message: Any = None, **_kwargs: Any) -> None:
        if message is not None:
            self.sent.append(message)
        raise _Finished


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, admin_users=frozenset({LISTED_QQ}), allow_private_friends=True)
    return module


def _private_event(user_id: Any, *, sub_type: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, sub_type=sub_type, message_id=1,
        sender=SimpleNamespace(role=""), get_message=lambda: [],
    )


def _wire_agent_path(runtime: Any, monkeypatch: pytest.MonkeyPatch, text: str) -> _FakeMatcher:
    """接好闸门之后那条链需要的桩，让命令分支可达。"""
    matcher = _FakeMatcher()
    monkeypatch.setattr(runtime, "_private_matcher", matcher)
    monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)
    monkeypatch.setattr(runtime, "_session_state", SimpleNamespace(), raising=False)
    monkeypatch.setattr(runtime, "_remember_message_for_reply_context", lambda event: None)

    async def fake_text(_bot: Any, _event: Any) -> str:
        return text

    monkeypatch.setattr(runtime, "_event_text_with_forward", fake_text)
    return matcher


def _run(runtime: Any, event: Any) -> None:
    try:
        asyncio.run(runtime._handle_private_agent(None, event))
    except _Finished:
        pass


# --- #49 命令分支排在闸门之后 ---


def test_a_stranger_never_reaches_the_command_branches(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_agent_path(runtime, monkeypatch, "/清除群记忆")
    calls: list[str] = []

    async def fake_clear(*, bot: Any, event: Any, user_text: str) -> str:
        calls.append(user_text)
        return "CLEARED"

    monkeypatch.setattr(runtime, "_maybe_handle_clear_group_memory_command", fake_clear)

    _run(runtime, _private_event(STRANGER_QQ))

    assert calls == []


def test_the_roster_still_gets_the_command_in_a_private_chat(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = _wire_agent_path(runtime, monkeypatch, "/清除群记忆")
    calls: list[str] = []

    async def fake_clear(*, bot: Any, event: Any, user_text: str) -> str:
        calls.append(user_text)
        return "CLEARED"

    monkeypatch.setattr(runtime, "_maybe_handle_clear_group_memory_command", fake_clear)

    _run(runtime, _private_event(LISTED_QQ))

    assert calls == ["/清除群记忆"]
    assert matcher.sent == ["CLEARED"]


# --- #45 未放行的人不该收到任何确认 ---


def test_a_stranger_gets_no_reply_at_all(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher = _FakeMatcher()
    monkeypatch.setattr(runtime, "_private_matcher", matcher)

    _run(runtime, _private_event(STRANGER_QQ))

    assert matcher.sent == []


def test_the_operator_can_put_a_notice_back(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    set_live_config(runtime, private_denied_reply="别找我")
    matcher = _FakeMatcher()
    monkeypatch.setattr(runtime, "_private_matcher", matcher)

    _run(runtime, _private_event(STRANGER_QQ))

    assert matcher.sent == ["别找我"]


def test_the_default_notice_is_empty(runtime: Any) -> None:
    assert runtime.live.private_denied_reply == ""


def test_an_unparsable_user_id_is_not_let_in(runtime: Any) -> None:
    assert runtime._is_private_allowed(_private_event("abc")) is False


def test_a_friend_still_gets_through(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher = _FakeMatcher()
    monkeypatch.setattr(runtime, "_private_matcher", matcher)

    # 闸门放行后紧接着的是「尚未初始化」检查（_client 默认为 None）。
    _run(runtime, _private_event(STRANGER_QQ, sub_type="friend"))

    assert matcher.sent == ["Clonoth Agent 尚未初始化，请稍后重试。"]
