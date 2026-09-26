"""唯一的 OneBotAdapter 实例，以及 FeatureGateway 需要的插件能力。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from platforms.inbound import InboundFacts
from ..adapter import OneBotAdapter
from ..feature_gateway import FeatureGateway
from ..config import CLONOTH_WORKSPACE
from adapters._shared.trigger_policy import TriggerDecision
from ..live_config import live
from ..send_contract import OutboundSendContext
from clonoth_sdk import ClonothClient, EventRouter

from . import anonymize, inbound, message_text, models, permissions, proactive, queueing, routing, sending


class _OneBotFeatureHost:
    """FeatureGateway 对插件的全部依赖。函数在调用时按模块名解析，替换模块属性照样生效。"""

    Message = Message
    MessageSegment = MessageSegment
    QueuedInbound = models.QueuedInbound
    live = live
    workspace = Path(CLONOTH_WORKSPACE)

    @property
    def client(self) -> Optional[ClonothClient]:
        return _adapter.client

    @property
    def event_router(self) -> Optional[EventRouter]:
        return _adapter.event_router

    def inbound_facts(self, bot: Any, event: Any) -> InboundFacts:
        return inbound._inbound_facts(bot, event)

    async def replay_event(self, bot: Any, facts: InboundFacts) -> Any:
        return await inbound._replay_event(bot, facts)

    def stable_conversation_key(self, real_conversation_key: str) -> str:
        return routing._stable_conversation_key(real_conversation_key)

    def anonymize_text_for_ai(self, text: str) -> str:
        return anonymize._anonymize_text_for_ai(text)

    def message_to_text(self, message: Any, bot_self_id: Any = None) -> str:
        return message_text._message_to_text(message, bot_self_id)

    def is_group_allowed(self, group_id: int) -> bool:
        return permissions._is_group_allowed(group_id)

    def is_private_allowed(self, event: Any) -> bool:
        return permissions._is_private_allowed(event)

    def is_direct_bot_interaction(self, event: Any, bot: Any, text: str) -> bool:
        return inbound._is_direct_bot_interaction(event, bot, text)

    def group_trigger_decision_once(self, event: Any, bot: Any, text: str) -> TriggerDecision:
        return inbound._group_trigger_decision_once(event, bot, text)

    def get_fallback_bot(self) -> Optional[Bot]:
        return queueing._get_fallback_bot()

    def request_send_context(self, scope: str, request_identity: str, conversation_key: str = "") -> OutboundSendContext:
        return proactive._request_send_context(scope, request_identity, conversation_key)

    async def group_trigger_text(self, bot: Any, event: Any) -> str:
        return await permissions._group_trigger_text(bot, event)

    async def event_text_with_forward(self, bot: Any, event: Any) -> str:
        return await message_text._event_text_with_forward(bot, event)

    async def queue_or_submit_ready(self, item: models.QueuedInbound) -> bool:
        return await queueing._queue_or_submit_ready(item)

    async def send_qq_message(
        self, bot: Any, target: Dict[str, Any], message: Any, *, send_context: OutboundSendContext | None = None,
    ) -> Any:
        return await sending._send_qq_message(bot, target, message, send_context=send_context)


_adapter = OneBotAdapter(
    workspace=Path(CLONOTH_WORKSPACE), live=live, features=FeatureGateway(_OneBotFeatureHost()),
)
