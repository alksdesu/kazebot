"""群、私聊、管理员的准入判定，以及 NoneBot Rule。"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, PrivateMessageEvent
from platforms.ids import normalize_id
from .. import capability
from ..trigger_policy import TriggerConfig, evaluate as trigger_evaluate
from ..live_config import live, refresh as refresh_live_config

from . import common, identity, inbound, message_text, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _is_group_allowed(group_id: int) -> bool:
    """判断 Bot 能否在该群活动；空列表表示不允许任何群。

    入站响应与出站主动发送共用这一个判定：曾经出站侧各自写成
    `if live.allowed_groups and gid not in live.allowed_groups`，空名单时反而全放行，
    与入站的全拒结论相反，而空名单正是未配置时的默认状态。
    """
    try:
        return int(group_id) in live.allowed_groups
    except (TypeError, ValueError):
        return False


def _is_admin_user(user_id: Any) -> bool:
    """是否在 admin_users 名单里。

    这是身份，不是能力 —— 能力要过 `_can`。除了鉴权，这份名单还兼着私聊白名单、
    审批推送收件人、提交给 engine 的 platform_auth.is_admin，那几处要的就是名单本身，
    不受能力档位影响：把工作区文件读权限跟着群主身份放出去是另一回事。
    """
    try:
        return int(user_id) in live.admin_users
    except Exception:
        return False


def _requester(event: Any) -> capability.Requester:
    """把事件里的身份事实原样搬过来，判定规则一律留给 capability。

    群消息按有没有 group_id 认，不按事件类型 —— 私聊事件万一带了 sender.role，
    也进不了判定：没有 group_id 的请求者一律算普通用户（Requester.rank）。
    """
    user_id = getattr(event, "user_id", None)
    # 匿名消息的身份不可核验，一律按普通群员判定。
    anonymous = bool(identity._anonymous_identity(event))
    return capability.Requester(
        user_id=normalize_id(user_id),
        listed_admin=(not anonymous) and _is_admin_user(user_id),
        group_id=normalize_id(getattr(event, "group_id", None)),
        group_role="" if anonymous else str(getattr(getattr(event, "sender", None), "role", "") or ""),
    )


def _can(capability_key: str, event: Any) -> bool:
    """这个人有没有这项能力。目标范围另判，见 _target_scope_denied。"""
    return capability.allows(capability_key, _grant_of(capability_key), _requester(event))


def _grant_of(capability_key: str) -> str:
    return getattr(live, f"grant_{capability_key}")


def _capability_denial(capability_key: str, event: Any, fallback: str) -> str | None:
    """没有这项能力时回什么。None = 当作没有这个命令，一个字都不回。

    档位关掉这件事只有名单里的人听得懂，对别人说等于公布这项能力存在。
    """
    if _grant_of(capability_key) != capability.OFF:
        return fallback
    if not _is_admin_user(getattr(event, "user_id", None)):
        common.logger.info(
            "QQ capability %s is off; staying silent for user=%s",
            capability_key, getattr(event, "user_id", None),
        )
        return None
    cap = capability.BY_KEY.get(capability_key)
    return f"「{cap.name if cap else capability_key}」已在配置里关闭。"


def _as_qq_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_private_allowed(event: PrivateMessageEvent) -> bool:
    """判断 QQ 私聊是否允许接入 Clonoth。id 解析不出来一律不放行。"""
    user_id = _as_qq_id(getattr(event, "user_id", None))
    if user_id is None:
        return False
    if user_id in live.admin_users or user_id in live.allowed_private_users:
        return True
    if not live.allow_private_friends:
        return False
    return str(getattr(event, "sub_type", "") or "").lower() == "friend"


def _is_private_origin_allowed(user_id: int) -> bool:
    """来源私聊是否仍在白名单内。供 qq_forward 读接口与「私发给我」写侧共用。

    只认名单，比入站闸门严：这里没有事件对象可查 sub_type=friend，拿总开关顶替
    等于对任何人放行，而这两处是借 Bot 身份主动发出去，宽进不该等于宽出。
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in live.admin_users or uid in live.allowed_private_users


async def _allowed_group_rule(event: Event) -> bool:
    """把群类型和白名单放在规则层过滤，避免无关消息进入上下文缓存。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    # rule 是这条消息的最早入口，在这里钉住配置快照：整条处理链读到的都是同一份，
    # 运营者中途改白名单不会让处理到一半的消息换标准。
    refresh_live_config()
    return _is_group_allowed(int(event.group_id))


async def _group_trigger_text(bot: Bot, event: GroupMessageEvent) -> str:
    """触发判定看到的正文。不展开的话转发卡片只是一个占位符，关键词和意愿判断都判不中。"""
    if not live.forward_msg_expand_for_trigger:
        return message_text._message_to_text(event.get_message(), getattr(bot, "self_id", None)).strip()
    return (await message_text._event_text_with_forward(bot, event)).strip()


async def _agent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配允许群里的 Agent 触发消息。七个信号的判定见 trigger_policy。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    refresh_live_config()
    if not _is_group_allowed(int(event.group_id)):
        return False
    metadata = await state._adapter.features.prepare(bot, event)
    if state._adapter.features.blocked(metadata):
        return False
    if metadata and state._adapter.features.coordinator.can_continue(metadata["actor"], metadata.get("reply_ref", "")):
        return True
    text = await _group_trigger_text(bot, event)
    decision = inbound._group_trigger_decision_once(event, bot, text)
    if decision.blocked_by:
        common.logger.debug(
            "QQ trigger suppressed group=%s signal=%s by=%s",
            getattr(event, "group_id", None), decision.signal, decision.blocked_by,
        )
    if decision.triggered:
        # 在这里记冷却而不是等真的发出去：rule 返回 True 就意味着这条消息会被当成一轮
        # bot 发言处理（本地命令拦截也会产出回复）。handler 中途异常时会多冷却一轮，
        # 而「多等一会儿」是这两个方向里安全的那一个。
        state._adapter.trigger_cooldown.record(inbound._trigger_input(event, bot, text))
    return decision.triggered


async def _intent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配「显式信号都不命中、但开了意愿判断」的群消息。

    只判要不要问模型，不在这里问：rule 里做一次几秒的往返，会把同一条消息后面的
    matcher 连同群历史记录一起卡住整整那么久。
    """
    if not isinstance(event, GroupMessageEvent):
        return False
    refresh_live_config()
    if not _is_group_allowed(int(event.group_id)):
        return False
    metadata = await state._adapter.features.prepare(bot, event)
    if state._adapter.features.blocked(metadata):
        return False
    coordinated = metadata.get("policy", {}).get("settings", {}).get("response_policy_enabled", False)
    if not live.llm_intent_enabled and not coordinated:
        return False
    text = await _group_trigger_text(bot, event)
    if coordinated:
        config = replace(TriggerConfig.from_live(live), llm_intent=True)
        return trigger_evaluate(inbound._trigger_input(event, bot, text), config, state._adapter.trigger_cooldown).awaits_llm_intent()
    return inbound._group_trigger_decision_once(event, bot, text).awaits_llm_intent()


async def _private_message_rule(event: Event) -> bool:
    """只匹配 QQ 私聊消息，避免私聊请求被群聊白名单逻辑误拦截。"""
    # 2026-05-01 修改原因：私聊没有群号，也不需要 @Bot；这里单独识别
    # PrivateMessageEvent，使私聊入口和现有群聊入口互不影响。
    if not isinstance(event, PrivateMessageEvent):
        return False
    refresh_live_config()
    return True
