"""这一轮随入站交给引擎的输出说明：分条、群里的表态、可用表情。"""
from __future__ import annotations

from typing import Dict, Optional

from adapters._shared.output_guide import GuideSection, OutputGuide, emoji_section, reaction_section, split_section
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE

from ..live_config import live
from . import common, emotes

_NATIVE_FACE_LABEL = "收藏表情"


def _split_section() -> Optional[GuideSection]:
    return split_section(QQ_NAPCAT)


def _reaction_section(kind: str) -> Optional[GuideSection]:
    """只列放行白名单：白名单外的 ID 发送时会被丢掉。表态开关关着时不给。"""
    catalog: Dict[str, str] = dict(common._REACT_MODEL_EMOJIS) if live.enable_reactions else {}
    return reaction_section(QQ_NAPCAT, kind, catalog)


def _emoji_section(conversation_key: str) -> Optional[GuideSection]:
    """收藏表情按名单给，最多 face_prompt_limit 个；表情包这一轮抽中了才给。"""
    names = emotes._current_custom_face_names()
    limit = int(live.face_prompt_limit)
    shown = names[:limit] if limit > 0 else []
    return emoji_section(
        QQ_NAPCAT,
        native_label=_NATIVE_FACE_LABEL,
        native_names=shown,
        native_hidden=len(names) - len(shown) if shown else 0,
        stickers=emotes._sticker_prompt_entries(conversation_key),
    )


def _turn_output_guide(conversation_key: str, *, is_dm: bool, entry_node_id: str = "") -> OutputGuide:
    if entry_node_id == common.DRAW_NODE_ID:
        # 绘图节点只出图或提示词，表情和表态用不上。
        return OutputGuide.of(_split_section())
    kind = CHANNEL_PRIVATE if is_dm else CHANNEL_GROUP
    return OutputGuide.of(_split_section(), _reaction_section(kind), _emoji_section(conversation_key))
