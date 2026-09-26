"""AdapterCallbacks 协议与 EventRouter 实际传的参数必须一致。

is_final 曾经只靠运行时嗅探签名传给 QQ 适配器，协议里没有；照协议写的新适配器
拿不到它，分不清一条输出是不是这一轮的收尾。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.callbacks import AdapterCallbacks  # noqa: E402
from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.state import SessionState  # noqa: E402
from clonoth_sdk.types import Event  # noqa: E402


def _event(seq: int, event_type: str, **payload: Any) -> Event:
    return Event.from_dict(Event(
        seq=seq, event_id=f"event-{seq}", ts="", run_id="run", session_id="session",
        component="supervisor", type=event_type,
        payload={"text": str(seq), "conversation_key": "tg_group:abc", **payload},
    ).to_dict())


class _ProtocolShaped:
    """严格按协议签名实现，不带 **kwargs。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_to_channel(
        self, conversation_key: str, text: str, attachments: list[dict[str, Any]], *,
        node_id: str = "", is_final: bool = True, delivery_context: Any = None,
    ) -> None:
        self.calls.append({"key": conversation_key, "text": text, "is_final": is_final})


def _router(tmp_path: Path, callbacks: Any) -> EventRouter:
    return EventRouter(
        SimpleNamespace(), SessionState(), callbacks,
        BotConfig(base_url="http://local", workspace_root=tmp_path, conversation_key_prefix="tg_group"),
    )


def test_the_protocol_declares_is_final() -> None:
    parameter = inspect.signature(AdapterCallbacks.send_to_channel).parameters["is_final"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is True


def test_a_protocol_shaped_adapter_can_tell_intermediate_from_final(tmp_path: Path) -> None:
    callbacks = _ProtocolShaped()
    router = _router(tmp_path, callbacks)
    try:
        asyncio.run(router._handle_intermediate_reply(_event(1, "intermediate_reply", node_id="child.node")))
        asyncio.run(router._handle_outbound_message(_event(2, "outbound_message", node_id="child.node")))
    finally:
        router._outbound_store.close()

    assert [(call["text"], call["is_final"]) for call in callbacks.calls] == [("1", False), ("2", True)]
