"""适配器契约测试的驱动：QQ 和 fakechat 包成同一组操作，契约用例只说平台无关的话。

QQ 走真实的 rule → handler → 排队 → 发送链路，只替换网络边界：NapCat 换成记录发送的假 bot，Clonoth 核心换成记录请求的假 client。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Iterator

import pytest

from _fake_platform import (
    FAKE_CHAT,
    FAULT_HANG,
    FAULT_LOST_ACK,
    FAULT_REFUSED,
    FAULTS,
    ChatMessage,
    ChatServer,
    ChatUser,
    FakeChatAdapter,
)
from _onebot_harness import load_runtime, set_live_config
from clonoth_sdk.state import SessionState
from clonoth_sdk.types import InboundResult
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE, PlatformDescriptor
from platforms.registry import register_platform, unregister_platform


@dataclass(frozen=True)
class Delivery:
    """bot 在平台上发出的一条消息。kind 是会话种类，target 是群或收件人的平台 ID。"""

    kind: str
    target: str
    text: str
    message_id: str


class FakeClonoth:
    """假的 Clonoth 核心：记下 inbound 提交、审批决定和功能服务请求，inbound 序号从 1 递增。"""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.feature_requests: list[dict[str, Any]] = []
        self.approve_result = True
        self.quiet: dict[str, Any] = {}

    async def submit_inbound(self, **kwargs: Any) -> InboundResult:
        self.submissions.append(dict(kwargs))
        seq = len(self.submissions)
        return InboundResult(session_id=f"session-{seq}", inbound_seq=seq, accepted=True)

    async def approve(self, approval_id: str, *, decision: str, comment: str | None = None) -> bool:
        self.decisions.append({"approval_id": approval_id, "decision": decision, "comment": comment})
        return self.approve_result

    async def request_feature(
        self, method: str, path: str, *, actor: Any = None, body: Any = None, params: Any = None, **kwargs: Any,
    ) -> Any:
        self.feature_requests.append({"method": method, "path": path, "actor": actor, "body": body, "params": params})
        if path == "/v1/community/state":
            return {"quiet": dict(self.quiet), "settings": {}}
        return {}

    def requests_to(self, path: str) -> list[dict[str, Any]]:
        return [request for request in self.feature_requests if request["path"] == path]


class _Finished(BaseException):
    """NoneBot 的 FinishedException 是 BaseException：matcher.finish 之后的代码不会再跑。"""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.text = text


class _Matcher:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def finish(self, message: Any = None) -> None:
        text = "" if message is None else str(message)
        if text:
            self.said.append(text)
        raise _Finished(text)

    async def send(self, message: Any = None) -> None:
        if message is not None:
            self.said.append(str(message))


async def _until_finished(call: Awaitable[Any]) -> None:
    try:
        await call
    except _Finished:
        pass


def _plain_text(message: Any) -> str:
    """OneBot 消息里的纯文本。审批卡片这类发送直接传字符串，不是消息段。"""
    if isinstance(message, str):
        return message
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in message if isinstance(segment, dict) and segment.get("type") == "text"
    )


class _NapCat:
    """记录发送的 NapCat：每条发送记成 Delivery，可以预约下一次发送出错。"""

    def __init__(self, self_id: str) -> None:
        self.self_id = self_id
        self.sent: list[Delivery] = []
        self._faults: list[str] = []

    def fail_next(self, fault: str) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault: {fault!r}")
        self._faults.append(fault)

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_member_list":
            return []
        return {}

    async def send_group_msg(self, *, group_id: Any, message: Any) -> dict[str, str]:
        return await self._post(CHANNEL_GROUP, group_id, message)

    async def send_private_msg(self, *, user_id: Any, message: Any) -> dict[str, str]:
        return await self._post(CHANNEL_PRIVATE, user_id, message)

    async def _post(self, kind: str, target: Any, message: Any) -> dict[str, str]:
        fault = self._faults.pop(0) if self._faults else ""
        if fault == FAULT_REFUSED:
            raise ConnectionRefusedError("connection refused")
        message_id = str(901 + len(self.sent))
        self.sent.append(Delivery(kind, str(target), _plain_text(message), message_id))
        if fault == FAULT_LOST_ACK:
            # NapCat 回执超时的原文：消息可能已经发出去了。
            raise RuntimeError("sendMsg timeout: network error")
        if fault == FAULT_HANG:
            await asyncio.Event().wait()
        return {"message_id": message_id}


class ContractDriver:
    """契约用例看到的平台：发消息给 bot、看 bot 发了什么、拿到它的 SDK 回调。"""

    platform: PlatformDescriptor
    bot_account: str
    admin: str
    member: str
    room: str
    other_room: str
    client: FakeClonoth
    session_state: SessionState
    callbacks: Any

    async def group_message(
        self, text: str, *, sender: str, message_id: str, room: str = "", mention_bot: bool = False,
    ) -> None:
        raise NotImplementedError

    async def private_message(self, text: str, *, sender: str, message_id: str, reply_to: str = "") -> list[str]:
        """私聊 bot 一句，返回 bot 当场回给这个人的话。"""
        raise NotImplementedError

    def delivered(self) -> list[Delivery]:
        raise NotImplementedError

    def fail_next_send(self, fault: str) -> None:
        raise NotImplementedError

    def remove_admins(self) -> None:
        raise NotImplementedError

    @property
    def feature_host(self) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        return None

    @property
    def private_ids(self) -> frozenset[str]:
        """不能让模型看见的真实 ID。"""
        return frozenset({self.admin, self.member, self.room, self.other_room})

    def channel(self, kind: str) -> str:
        return next(channel.name for channel in self.platform.channels if channel.kind == kind)

    def delivered_to(self, kind: str, target: str) -> list[Delivery]:
        return [delivery for delivery in self.delivered() if delivery.kind == kind and delivery.target == target]


class QQDriver(ContractDriver):
    platform = QQ_NAPCAT
    bot_account = "90000"
    admin = "3141592"
    member = "2718281"
    room = "16180339"
    other_room = "14142135"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
        self.runtime = load_runtime(monkeypatch, workspace)
        set_live_config(
            self.runtime,
            admin_users=[int(self.admin)],
            allowed_groups=[int(self.room), int(self.other_room)],
            enable_reactions=False,
        )
        self.client = FakeClonoth()
        self.session_state = SessionState()
        self.runtime.state._adapter.client = self.client
        self.runtime.state._adapter.session_state = self.session_state
        self.bot = _NapCat(self.bot_account)
        self.callbacks = self.runtime.callbacks.TangQiuCallbacks()
        self._private_matcher = _Matcher()
        monkeypatch.setattr(self.runtime.handlers, "_private_matcher", self._private_matcher)
        # 线上是连上 NapCat 就认账号；等第一条触发消息再认，会把认号前记下的群历史当成换号清掉。
        asyncio.run(self.runtime.lifecycle._on_bot_connect(self.bot))

    def _message(self, segments: list[Any]) -> Any:
        return self.runtime.state.Message(segments)

    async def group_message(
        self, text: str, *, sender: str, message_id: str, room: str = "", mention_bot: bool = False,
    ) -> None:
        segment = self.runtime.state.MessageSegment
        message = self._message([*([segment.at(self.bot_account)] if mention_bot else []), segment.text(text)])
        event = SimpleNamespace(
            group_id=int(room or self.room), user_id=int(sender), message_id=int(message_id), time=int(time.time()),
            self_id=int(self.bot_account), message_type="group", sub_type="normal", raw_message=text,
            to_me=mention_bot, reply=None, anonymous=None,
            sender=SimpleNamespace(user_id=int(sender), nickname=f"member-{sender}", card="", role="member"),
            get_message=lambda: message,
        )
        permissions, handlers = self.runtime.permissions, self.runtime.handlers
        if await permissions._agent_group_rule(self.bot, event):
            await _until_finished(handlers._process_group_message(self.bot, event, _Matcher()))
        elif await permissions._allowed_group_rule(event):
            await handlers._handle_history(self.bot, event)

    async def private_message(self, text: str, *, sender: str, message_id: str, reply_to: str = "") -> list[str]:
        segment = self.runtime.state.MessageSegment
        message = self._message([*([segment.reply(int(reply_to))] if reply_to else []), segment.text(text)])
        event = SimpleNamespace(
            user_id=int(sender), message_id=int(message_id), time=int(time.time()), self_id=int(self.bot_account),
            message_type="private", sub_type="friend", raw_message=text, to_me=True, anonymous=None,
            reply=SimpleNamespace(message_id=int(reply_to), sender=SimpleNamespace(user_id=int(self.bot_account)))
            if reply_to else None,
            sender=SimpleNamespace(user_id=int(sender), nickname=f"user-{sender}", card="", role=""),
            get_message=lambda: message,
        )
        sent_before, said_before = len(self.bot.sent), len(self._private_matcher.said)
        await _until_finished(self.runtime.handlers._handle_private_agent(self.bot, event))
        sent = [
            delivery.text for delivery in self.bot.sent[sent_before:]
            if delivery.kind == CHANNEL_PRIVATE and delivery.target == sender
        ]
        return [*sent, *self._private_matcher.said[said_before:]]

    def delivered(self) -> list[Delivery]:
        return list(self.bot.sent)

    def fail_next_send(self, fault: str) -> None:
        self.bot.fail_next(fault)

    def remove_admins(self) -> None:
        set_live_config(self.runtime, admin_users=[])

    @property
    def feature_host(self) -> Any:
        return self.runtime.state._adapter.features.host


class FakeChatDriver(ContractDriver):
    platform = FAKE_CHAT
    bot_account = "bot-fake"
    admin = "u-admin"
    member = "u-member"
    room = "room-main"
    other_room = "room-side"

    def __init__(self, workspace: Path) -> None:
        self.client = FakeClonoth()
        self.server = ChatServer(self.bot_account)
        self.adapter = FakeChatAdapter(
            workspace, self.server, self.client, admins=[self.admin], rooms=[self.room, self.other_room],
        )
        self.session_state = self.adapter.session_state
        self.callbacks = self.adapter.callbacks

    async def group_message(
        self, text: str, *, sender: str, message_id: str, room: str = "", mention_bot: bool = False,
    ) -> None:
        await self.adapter.receive(ChatMessage(
            id=message_id, sender=ChatUser(sender, f"member-{sender}"), text=text, room=room or self.room,
            mentions=(self.bot_account,) if mention_bot else (),
        ))

    async def private_message(self, text: str, *, sender: str, message_id: str, reply_to: str = "") -> list[str]:
        before = len(self.server.posted)
        await self.adapter.receive(ChatMessage(
            id=message_id, sender=ChatUser(sender, f"user-{sender}"), text=text, reply_to=reply_to,
        ))
        return [posted.text for posted in self.server.posted[before:] if not posted.room and posted.user == sender]

    def delivered(self) -> list[Delivery]:
        return [
            Delivery(CHANNEL_GROUP if posted.room else CHANNEL_PRIVATE, posted.room or posted.user, posted.text, posted.id)
            for posted in self.server.posted
        ]

    def fail_next_send(self, fault: str) -> None:
        self.server.fail_next(fault)

    def remove_admins(self) -> None:
        self.adapter.admins = frozenset()

    @property
    def feature_host(self) -> Any:
        return self.adapter.features.host

    def close(self) -> None:
        self.adapter.close()


DRIVERS = ("qq", "fakechat")


def open_driver(name: str, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> ContractDriver:
    if name == "qq":
        return QQDriver(monkeypatch, workspace)
    if name == "fakechat":
        return FakeChatDriver(workspace)
    raise ValueError(f"unknown contract driver: {name!r}")


@contextmanager
def registered_fake_platform() -> Iterator[PlatformDescriptor]:
    """fakechat 只在用例期间登记进平台注册表：注册表是进程级的，漏注销会改掉别的用例的来源判定。"""
    register_platform(FAKE_CHAT)
    try:
        yield FAKE_CHAT
    finally:
        unregister_platform(FAKE_CHAT.id)
