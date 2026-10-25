"""qq_forward 的发起人只认任务：同群先后触发的两个人，各自的任务按各自的身份判。

会话目标会被同群后来触发的人覆盖，拿它认人等于让 A 的任务借 B 的身份；发起人一律按任务号
向 supervisor 查（GET /v1/tasks/{task_id}/origin），会话只用来定位「本群」。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, patch_imported, set_live_config  # noqa: E402
from clonoth_sdk import PlatformAuth, TaskOrigin  # noqa: E402
from clonoth_sdk.state import SessionState  # noqa: E402

_SESSION = "sess-shared-group"
_STABLE_KEY = "qq_group:stable-home"
_HOME_GROUP = 99
_OTHER_GROUP = 77
_ADMIN_QQ = 10001
_MEMBER_QQ = 30003
_ADMIN_TASK = "task-admin"
_MEMBER_TASK = "task-member"


class _Bot:
    self_id = "42"

    def __init__(self) -> None:
        self.group_msgs: list[int] = []

    async def send_group_msg(self, *, group_id: int, message: Any) -> dict[str, Any]:
        self.group_msgs.append(int(group_id))
        return {"message_id": len(self.group_msgs)}

    async def send_private_msg(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("这组用例不该私发")

    async def call_api(self, api: str, **kwargs: Any) -> dict[str, Any]:
        return {}


def _origin(task_id: str, user_id: int, *, is_admin: bool) -> TaskOrigin:
    return TaskOrigin(
        task_id=task_id, origin_task_id=task_id, conversation_key=_STABLE_KEY, channel="qq_group",
        platform_auth=PlatformAuth(platform="qq", user_id=str(user_id), is_admin=is_admin, group_scope=_STABLE_KEY),
    )


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, admin_users=frozenset({_ADMIN_QQ}), allowed_groups=frozenset({_HOME_GROUP, _OTHER_GROUP}))
    module.state._adapter.session_state = SessionState()
    monkeypatch.setattr(module.routing, "_remember_route_state", AsyncMock())
    return module


def _group_event(user_id: int, message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, message_id=message_id, group_id=_HOME_GROUP, time=1_700_000_000 + message_id,
        sender=SimpleNamespace(user_id=user_id, nickname=f"n{user_id}", card="", role="member"),
        anonymous=None, get_message=lambda: [],
    )


def _wire(runtime: Any, monkeypatch: pytest.MonkeyPatch, bot: _Bot) -> AsyncMock:
    """两个任务都在同一个会话里；supervisor 按任务号认人。"""
    origins = {
        _ADMIN_TASK: _origin(_ADMIN_TASK, _ADMIN_QQ, is_admin=True),
        _MEMBER_TASK: _origin(_MEMBER_TASK, _MEMBER_QQ, is_admin=False),
    }
    lookup = AsyncMock(side_effect=lambda task_id: origins.get(task_id))
    submitted: list[int] = []

    async def submit_inbound(**kwargs: Any) -> Any:
        submitted.append(1)
        return SimpleNamespace(session_id=_SESSION, accepted=True, inbound_seq=len(submitted))

    runtime.state._adapter.client = SimpleNamespace(get_task_origin=lookup, submit_inbound=submit_inbound)
    patch_imported(monkeypatch, runtime, "get_bot", lambda: bot)

    async def resolve(*args: Any, **kwargs: Any):
        return runtime.models.ProactiveTarget("group", _OTHER_GROUP, "别的群"), ""

    async def emojis(text: str, *args: Any, **kwargs: Any):
        return [runtime.state.MessageSegment.text(text)]

    monkeypatch.setattr(runtime.forward_bridge, "_forward_bridge_resolve_target", resolve)
    monkeypatch.setattr(runtime.state._adapter.emoji, "process_emojis", emojis)
    return lookup


def _trigger(runtime: Any, bot: _Bot, user_id: int, message_id: int) -> None:
    """走真实的提交路径：会话目标按入站时的平台数据登记，和 handler 拼的一样不带触发者的号。"""
    event = _group_event(user_id, message_id)
    platform_updates = {
        "bot": bot, "event": event, "type": "group", "group_id": _HOME_GROUP,
        "conversation_key": _STABLE_KEY, "_source_attachments": [],
    }
    accepted = asyncio.run(runtime.queueing._submit_or_preempt_inbound(
        bot=bot, event=event, channel="qq_group", real_conversation_key=f"qq_group:{_HOME_GROUP}",
        stable_conversation_key=_STABLE_KEY, inbound_text="正文", user_text="帮我通知别的群",
        attachments=[], is_dm=False, platform_updates=platform_updates,
    ))
    assert accepted


def _remind_other_group(runtime: Any, task_id: str) -> dict[str, Any]:
    return asyncio.run(runtime.forward_bridge._forward_bridge_execute({
        "session_id": _SESSION, "task_id": task_id, "request_id": f"req-{task_id}",
        "action": "remind", "target_type": "group", "target_ref": "别的群", "text": "开会",
    }))


@pytest.mark.parametrize("order", [("admin", "member"), ("member", "admin")])
def test_each_task_is_judged_by_its_own_requester(runtime: Any, monkeypatch: pytest.MonkeyPatch, order: tuple[str, str]) -> None:
    bot = _Bot()
    lookup = _wire(runtime, monkeypatch, bot)
    people = {"admin": _ADMIN_QQ, "member": _MEMBER_QQ}
    for index, who in enumerate(order, start=1):
        _trigger(runtime, bot, people[who], index)
    assert "user_id" not in runtime.state._adapter.session_targets[_SESSION]

    member = _remind_other_group(runtime, _MEMBER_TASK)
    admin = _remind_other_group(runtime, _ADMIN_TASK)

    assert member["ok"] is False and "管理员" in member["error"]
    assert admin["ok"] is True
    assert bot.group_msgs == [_OTHER_GROUP]
    assert [call.args[0] for call in lookup.await_args_list] == [_MEMBER_TASK, _ADMIN_TASK]


def test_the_later_member_never_inherits_the_earlier_admin(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """后触发的群员的任务调工具时，按群员自己的身份判，不管会话里先前登记的是谁。"""
    bot = _Bot()
    _wire(runtime, monkeypatch, bot)
    _trigger(runtime, bot, _ADMIN_QQ, 1)
    _trigger(runtime, bot, _MEMBER_QQ, 2)

    result = _remind_other_group(runtime, _MEMBER_TASK)

    assert result["ok"] is False
    assert bot.group_msgs == []


def test_persisted_group_targets_drop_the_trigger_user(runtime: Any) -> None:
    serialize = runtime.routing._serializable_target

    assert serialize({"type": "group", "group_id": 1, "user_id": 2, "conversation_key": "k"}) == {
        "type": "group", "group_id": 1, "conversation_key": "k",
    }
    assert serialize({"type": "private", "user_id": 2, "conversation_key": "k"}) == {
        "type": "private", "user_id": 2, "conversation_key": "k",
    }


def test_restored_group_bursts_carry_no_user_id(runtime: Any) -> None:
    bot = _Bot()
    data = {
        "is_dm": False, "scope": _STABLE_KEY, "refs": ["1"], "attachments": [], "group_id": _HOME_GROUP,
        "channel": "qq_group", "real_key": f"qq_group:{_HOME_GROUP}", "text": "t", "user_text": "t",
        "entry_node_id": "", "direct_interaction": True, "memory_user_ids": [],
    }
    event = _group_event(_MEMBER_QQ, 1)
    facts = runtime.inbound._inbound_facts(bot, event, text="t")

    group_item = runtime.queueing._restore_staged_inbound(bot, event, data, facts, {})
    private_item = runtime.queueing._restore_staged_inbound(bot, event, {**data, "is_dm": True}, facts, {})

    assert "user_id" not in group_item.platform_updates
    assert private_item.platform_updates["user_id"] == _MEMBER_QQ


def test_the_tool_sends_the_engine_task_id_not_one_from_its_arguments() -> None:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            body = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env.update({
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(server.server_port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "1",
            "CLONOTH_SESSION_ID": "session-test",
            "CLONOTH_TASK_ID": "task-from-engine",
            "PYTHONIOENCODING": "utf-8",
        })
        args = json.dumps({"op": "remind", "target_type": "self", "text": "hi", "task_id": "task-of-someone-else"})
        completed = subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "qq_forward.py")],
            input=args, text=True, encoding="utf-8", capture_output=True,
            cwd=_ROOT, env=env, timeout=10, check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [payload["task_id"] for payload in received] == ["task-from-engine"]


def test_the_tool_without_an_engine_task_id_sends_an_empty_one() -> None:
    """没有任务号时照样发出去，由 Bridge 按「查不到发起人」处理，不在工具里猜。"""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            body = json.dumps({"ok": False, "error": "denied"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {key: value for key, value in os.environ.items() if key != "CLONOTH_TASK_ID"}
        env.update({
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(server.server_port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "1",
            "CLONOTH_SESSION_ID": "session-test",
            "PYTHONIOENCODING": "utf-8",
        })
        subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "qq_forward.py")],
            input=json.dumps({"op": "remind", "target_type": "self", "text": "hi", "task_id": "forged"}),
            text=True, encoding="utf-8", capture_output=True, cwd=_ROOT, env=env, timeout=10, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [payload["task_id"] for payload in received] == [""]
