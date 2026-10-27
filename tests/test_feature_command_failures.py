"""功能命令失败时回给用户的话：supervisor 写明了原因就照转，其余记日志、回一句通用说法，异常原文和内部地址不发出去。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx
import pytest

from adapters._shared import execution_commands, material_commands, reminder_commands
from adapters._shared.feature_gateway import FeatureGateway
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor
from platforms.inbound import InboundConversation, InboundFacts, InboundSender

SUPERVISOR = "http://127.0.0.1:8765"
ACTOR = {
    "scope": "qq_group:1", "user_id": "42", "channel": "qq_group", "bot_scope": "bot",
    "message_id": "100", "display_name": "Alice",
}
_PLATFORM = PlatformDescriptor(
    id="chat", display_name="Chat", short_name="Chat", channels=(ChannelDescriptor("chat_room", CHANNEL_GROUP),),
)
_LOGGER = "adapters._shared.feature_failures"


def _logged(caplog: pytest.LogCaptureFixture) -> list[bool]:
    """feature_failures 记下的每条日志带没带异常。"""
    return [record.exc_info is not None for record in caplog.records if record.name == _LOGGER]


def _refused(status: int, **content: Any) -> httpx.HTTPStatusError:
    """和 SDK 客户端 raise_for_status 抛出的一样：异常原文里带着 supervisor 的地址。"""
    reply = httpx.Response(status, request=httpx.Request("POST", f"{SUPERVISOR}/v1/feature"), **content)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        reply.raise_for_status()
    return caught.value


class _Client:
    """社区状态照常给空，别的请求一律抛 error。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def request_feature(self, method: str, path: str, **_kwargs: Any) -> Any:
        if method == "GET" and path == "/v1/community/state":
            return {}
        raise self.error


FAILURES = [
    pytest.param(lambda: _refused(409, json={"detail": "这条已经处理过了"}), "这条已经处理过了", id="string"),
    pytest.param(
        lambda: _refused(400, json={"detail": {"code": "invalid_input", "message": "编号格式不对"}}),
        "编号格式不对（invalid_input，HTTP 400）", id="structured",
    ),
    pytest.param(lambda: _refused(500, text="Internal Server Error"), None, id="no-reason"),
    pytest.param(lambda: httpx.ConnectError("All connection attempts failed"), None, id="unreachable"),
    pytest.param(lambda: KeyError("revision"), None, id="malformed-reply"),
]

Command = Callable[[_Client], Awaitable[dict]]
COMMANDS = [
    pytest.param(
        lambda client: reminder_commands.handle(client, ACTOR, "/提醒列表"),
        "提醒操作未完成：", "提醒服务暂时出错，请稍后再试。", id="reminder-command",
    ),
    pytest.param(
        lambda client: reminder_commands.handle(client, ACTOR, "已完成", "m9"),
        "提醒操作未完成：", "提醒服务暂时出错，请稍后再试。", id="reminder-reply",
    ),
    pytest.param(
        lambda client: execution_commands.handle(client, ACTOR, "/任务 P0123456789ab"),
        "计划操作未完成：", "执行服务暂时出错，请稍后再试。", id="execution",
    ),
    pytest.param(
        lambda client: material_commands.handle(client, ACTOR, "/资料 列表"),
        "资料操作未完成：", "资料服务暂时出错，请稍后再试。", id="material",
    ),
]


@pytest.mark.parametrize("command, prefix, fallback", COMMANDS)
@pytest.mark.parametrize("failure, reason", FAILURES)
def test_a_failed_command_passes_on_only_the_supervisors_reason(
    caplog: pytest.LogCaptureFixture, command: Command, prefix: str, fallback: str,
    failure: Callable[[], BaseException], reason: str | None,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = asyncio.run(command(_Client(failure())))

    assert result["text"] == prefix + (reason or fallback)
    assert SUPERVISOR not in result["text"]
    # 没有写明原因的才记日志：原文留给运维看，不发给用户
    assert _logged(caplog) == ([] if reason else [True])


def _facts(text: str) -> InboundFacts:
    return InboundFacts(
        sender=InboundSender(platform_id="alice", display_name="Alice"),
        conversation=InboundConversation(
            platform=_PLATFORM.id, channel="chat_room", kind=CHANNEL_GROUP, raw_id="room-1",
            conversation_key="stable:chat_room:room-1", real_conversation_key="chat_room:room-1",
        ),
        self_id="bot-7", message_id="m1", text=text, sent_at=5.0,
    )


@pytest.mark.parametrize("failure, reason", FAILURES)
def test_the_gateway_passes_on_only_the_supervisors_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Callable[[], BaseException], reason: str | None,
) -> None:
    host = SimpleNamespace(client=_Client(failure()), event_router=None)
    gateway = FeatureGateway(host, platform=_PLATFORM, burst_path=tmp_path / "bursts.sqlite3")
    facts = _facts("/群规")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = asyncio.run(gateway.handle_command(None, facts, facts.text))

    assert result == {"text": reason or "操作未完成，请检查功能服务后重试。", "attachments": [], "control": False}
    assert _logged(caplog) == ([] if reason else [True])
