"""入站判定与组装：@ 与回复识别、入站事实、触发、意愿判断、inbound 文本。"""
from __future__ import annotations

import logging
import datetime as dt
import hashlib
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, PrivateMessageEvent
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE
from platforms.ids import normalize_id
from platforms.inbound import InboundConversation, InboundFacts, InboundSender
from ..config import FORWARD_BRIDGE_TOKEN, FORWARD_BRIDGE_TOKEN_FILE, GROUP_TRIGGER_MODES
from adapters._shared import capability
from adapters._shared.trigger_policy import (
    TriggerConfig,
    TriggerDecision,
    TriggerInput,
    evaluate as trigger_evaluate,
    matched_prefix,
)
from ..live_config import live
from ..segments import any_marked_segment
from clonoth_sdk import IntentVerdict

from . import anonymize, common, forward_bridge, history, identity, media, message_text, models, output_guide, permissions, routing, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


# 随机插话骰子的盐。每次启动换一份：常量盐会让外人按 message_id 算出哪条消息能触发插话。
_TRIGGER_ROLL_SALT = os.urandom(16)


# 按消息身份缓存的触发判定。只为「一条消息在同一次派发里被判两遍」而存在，所以很小。
_TRIGGER_DECISION_CACHE_MAX = 256


def _bot_self_id_candidates(event: Event | None, bot: Bot | None) -> set[str]:
    """收集 Bot 自身 QQ 号候选值，兼容不同 OneBot/NoneBot 对 self_id 的暴露方式。"""
    candidates: set[str] = set()
    for source in (bot, event):
        if source is None:
            continue
        for attr in ("self_id", "bot_id"):
            value = getattr(source, attr, None)
            if value is not None and str(value).strip():
                candidates.add(str(value).strip())
    return candidates


def _qq_matches_bot(qq: Any, bot_ids: set[str]) -> bool:
    value = str(qq or "").strip()
    return bool(value and value in bot_ids)


def _group_message_mentions_bot(message: Any, bot_ids: set[str]) -> bool:
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for segment in segments:
        seg_type, data = message_text._segment_type_and_data(segment)
        if seg_type != "at":
            continue
        if _qq_matches_bot(data.get("qq"), bot_ids):
            return True
    if isinstance(message, str):
        return _raw_message_mentions_bot(message, bot_ids)
    return False


def _raw_message_mentions_bot(raw_message: Any, bot_ids: set[str]) -> bool:
    raw = str(raw_message or "")
    if not raw or not bot_ids:
        return False
    for match in common._AT_QQ_RE.finditer(raw):
        if _qq_matches_bot(match.group(1), bot_ids):
            return True
    return False


def _event_replies_to_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """这条群消息是不是在回复 bot 自己的消息。

    必须看 event.reply 而不是 to_me：NoneBot 的 _check_reply / _check_at_me /
    _check_nickname 三个 preprocessor 都往同一个 to_me 里写，混在一起就没法单独关掉
    「被回复」。get_msg 失败时 _check_reply 会提前返回、reply 为 None，那种情况保守判为
    「不是回复 bot」—— 此时它也没置 to_me，不会漏触发。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return False
    sender = getattr(reply, "sender", None)
    sender_id = getattr(sender, "user_id", None)
    if sender_id is None:
        return False
    return str(sender_id) in _bot_self_id_candidates(event, bot)


def _event_at_mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """这条群消息是不是 @ 了 bot（区别于「回复了 bot」）。

    _check_at_me 命中首尾 @ 时会把那一段从消息里删掉，只留下 to_me，所以句中 @ 能从
    消息段看出来，首尾 @ 只能靠 to_me 推。扣掉「被回复」那个来源之后剩下的 to_me
    就是被剥掉的 @ —— 前提是没配 NoneBot 的 NICKNAME，那一条由启动告警盯着。
    """
    bot_ids = _bot_self_id_candidates(event, bot)
    try:
        if _group_message_mentions_bot(event.get_message(), bot_ids):
            return True
    except Exception:
        pass
    if _raw_message_mentions_bot(getattr(event, "raw_message", ""), bot_ids):
        return True
    if not _event_to_me(event):
        return False
    return not _event_replies_to_bot(event, bot)


def _event_to_me(event: GroupMessageEvent) -> bool:
    """NoneBot 置的 to_me。三个来源混在一起，只能当作「其中之一命中了」。"""
    try:
        if bool(getattr(event, "to_me", False)):
            return True
    except Exception:
        pass
    try:
        is_tome = getattr(event, "is_tome", None)
        return bool(callable(is_tome) and is_tome())
    except Exception:
        return False


def _event_identity(event: Event) -> tuple[str, ...]:
    """这条消息的身份。message_id 可能缺失、也可能被补发事件复用，所以四项一起取。"""
    return tuple(
        str(getattr(event, name, "") or "")
        for name in ("group_id", "user_id", "message_id", "time")
    )


def _trigger_roll(event: GroupMessageEvent) -> float:
    """随机插话的骰子，[0,1)。同一条消息掷几次都是同一个值。"""
    # 判定可能对同一条消息跑多遍，骰子必须同值：一遍掷中一遍没中的话，
    # 显式信号那条路和意愿判断那条路会双双放手，这条消息就没人处理了。
    identity = "|".join(_event_identity(event)).encode("utf-8")
    digest = hashlib.blake2b(_TRIGGER_ROLL_SALT + identity, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _event_segments(event: Any) -> List[tuple[str, Dict[str, Any]]]:
    try:
        return message_text._iter_segments(event.get_message())
    except Exception:
        return []


def _event_sender_role(event: Any) -> str:
    sender = getattr(event, "sender", None)
    role = sender.get("role", "") if isinstance(sender, dict) else getattr(sender, "role", "")
    return permissions._group_role(role)


def _inbound_facts(
    bot: Any,
    event: Any,
    *,
    text: str | None = None,
    attachments: Iterable[Mapping[str, Any]] = (),
) -> InboundFacts:
    """OneBot 事件翻译成平台无关的入站事实。text 为 None 时按消息段渲染正文。"""
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)
    self_id = getattr(bot, "self_id", None)
    anonymous = bool(identity._anonymous_identity(event))
    segments = _event_segments(event)
    is_group = group_id is not None
    if is_group:
        real_key = f"qq_group:{normalize_id(group_id)}"
        channel, kind, raw_id = "qq_group", CHANNEL_GROUP, group_id
    else:
        real_key = f"qq_private:{normalize_id(user_id)}"
        channel, kind, raw_id = "qq_private", CHANNEL_PRIVATE, user_id
    if text is None:
        text = message_text._message_to_text(event.get_message(), self_id) if hasattr(event, "get_message") else ""
    try:
        reply_to = str(message_text._extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None)) or "")
    except Exception:
        reply_to = ""
    return InboundFacts(
        sender=InboundSender(
            platform_id="" if anonymous else user_id,
            alias=identity._event_user_alias(event),
            display_name=identity._event_display_name(event),
            group_role="" if anonymous or not is_group else _event_sender_role(event),
            anonymous=anonymous,
            listed_admin=(not anonymous) and permissions._is_admin_user(user_id),
        ),
        conversation=InboundConversation(
            platform=QQ_NAPCAT.id, channel=channel, kind=kind, raw_id=raw_id,
            conversation_key=routing._stable_conversation_key(real_key), real_conversation_key=real_key,
        ),
        self_id=self_id,
        message_id=str(getattr(event, "message_id", "") or ""),
        text=text or "",
        text_segments=tuple(str(data.get("text") or "") for kind_, data in segments if kind_ == "text"),
        mentions=tuple(data.get("qq") for kind_, data in segments if kind_ == "at" and data.get("qq") != "all"),
        mentions_self=is_group and _event_at_mentions_bot(event, bot),
        reply_to=reply_to,
        reply_to_self=is_group and _event_replies_to_bot(event, bot),
        attachments=tuple(attachments or ()),
        marked_sticker=any_marked_segment(segments),
        sent_at=float(getattr(event, "time", None) or time.time()),
    )


async def _replay_event(bot: Any, facts: InboundFacts) -> models._ReplayedEvent:
    """把暂存的入站事实还原成排队链路能吃的事件；群角色现查，暂存时的可能已经变了。"""
    event = models._ReplayedEvent(
        user_id=int(facts.sender.platform_id),
        message_id=int(facts.message_id),
        time=facts.sent_at,
        sender=models._ReplayedSender(nickname=facts.sender.display_name),
        text=facts.text,
    )
    if facts.conversation.is_group:
        event.group_id = int(facts.conversation.raw_id)
        try:
            member = await bot.call_api("get_group_member_info", group_id=event.group_id, user_id=event.user_id)
            event.sender.role = str(member.get("role", "member"))
        except Exception:
            pass
    return event


def _queued_facts(item: models.QueuedInbound) -> InboundFacts:
    """排队项的入站事实；老路径构造的排队项没带，就地补一份。"""
    if item.facts is None:
        item.facts = _inbound_facts(item.bot, item.event, text=item.user_text)
    return item.facts


def _trigger_input(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerInput:
    """判定只吃入站事实；dry-run 从 JSON 构造同一个结构。"""
    return TriggerInput.from_facts(
        _inbound_facts(bot, event, text=text or ""),
        now=time.time(),
        roll=_trigger_roll(event),
    )


def _group_trigger_decision(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerDecision:
    """群消息的完整判定。命中信号与被否决的原因都带回来，供后续记账与试听对照。"""
    return trigger_evaluate(
        _trigger_input(event, bot, text),
        TriggerConfig.from_live(live),
        state._adapter.trigger_cooldown,
    )


def _group_trigger_decision_once(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerDecision:
    """同一条消息只判一次，两个 rule 共用这一份结论。

    NoneBot 会为一条群消息分别跑显式信号与意愿判断两个 rule，判定要遍历正文、装配一次
    配置快照、结算一次随机骰子 —— 判第二遍是纯重复，而且结论必须逐字相同。
    """
    key = _event_identity(event) + (text or "",)
    cached = state._adapter.trigger_decisions.get(key)
    if cached is not None:
        state._adapter.trigger_decisions.move_to_end(key)
        return cached
    decision = _group_trigger_decision(event, bot, text)
    if decision.triggered:
        # 接手了就没人会再判它（后面的 matcher 都被 block 掉）。这种结论存下来只会让重复
        # 投递的同一个事件绕过刚记下的冷却。
        return decision
    state._adapter.trigger_decisions[key] = decision
    while len(state._adapter.trigger_decisions) > _TRIGGER_DECISION_CACHE_MAX:
        state._adapter.trigger_decisions.popitem(last=False)
    return decision


def _group_should_trigger(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    return _group_trigger_decision(event, bot, text).triggered


async def _judge_llm_intent(bot: Bot, event: GroupMessageEvent, text: str) -> IntentVerdict:
    """问 supervisor：这条群消息是不是在跟 bot 说话。

    送过去的全是匿名化之后的文本 —— 群号和 QQ 号本来就不该进模型上下文，判定这条
    新链路没有理由成为例外。
    """
    if state._adapter.client is None:
        return IntentVerdict(error="client not ready")
    group_id = int(event.group_id)
    limit = int(live.llm_intent_context_messages)
    context_lines = [entry.text for entry in list(state._adapter.group_history[group_id])[-limit:]] if limit > 0 else []
    metadata = state._adapter.features.coordinator.metadata(state._adapter.features.actor(bot, event)) if state._adapter.features.available else {}
    topic = metadata.get("topic", {})
    if topic.get("context"):
        context_lines = [f"{line['label']}: {line['text']}" for line in topic["context"]]
    if metadata.get("policy", {}).get("settings", {}).get("response_policy_enabled"):
        reactions = output_guide._reaction_section(CHANNEL_GROUP)
        if reactions is not None:
            context_lines.append(reactions.text)
    speaker = anonymize._anonymize_text_for_ai(identity._event_display_name(event))
    return await state._adapter.client.judge_reply_intent(
        conversation_key=routing._stable_conversation_key(f"qq_group:{group_id}"),
        text=anonymize._anonymize_text_for_ai(text),
        channel="qq_group",
        context_lines=context_lines,
        bot_names=list(live.name_words),
        speaker=speaker,
        node_id=live.llm_intent_node_id,
        timeout_sec=float(live.llm_intent_timeout_sec),
        max_inflight=int(live.llm_intent_max_inflight),
    )


def _is_direct_bot_interaction(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    """这条群消息是不是本人主动找 bot（决定要不要给他建长期记忆档案）。

    和触发判定各算一次：全量 / 关键词 / 随机插话下 bot 会回一堆不是在找它的消息，但
    「在群里说了句话」不等于「主动找 bot」—— 照触发结论建档，群一活跃全员都有跨会话
    档案，注入预算被一堆无关的人占满。只认被 @、回复 bot、带触发前缀这三件事。
    """
    if _event_at_mentions_bot(event, bot) or _event_replies_to_bot(event, bot):
        return True
    return bool(matched_prefix(text, live.trigger_prefixes))


def _strip_trigger_prefix(text: str) -> str:
    value = (text or "").strip()
    for prefix in live.trigger_prefixes_strippable:
        if value.startswith(prefix):
            return value[len(prefix):].strip() or value
    return value


def _warn_startup_config() -> None:
    """启动时校验权限与白名单：空值都是 fail-closed，但静默会让人误以为部署成功。

    消息里指的是 qq.yaml 的路径而不是环境变量名：热载生效后运营者是去那里改的，
    报一个已经不是主来源的变量名只会让人找错地方。
    """
    if not live.admin_users:
        common.logger.warning(
            "permissions.admin_users is empty: only group owners/admins can use the "
            "capabilities granted to them, and every approval request will be auto-denied"
        )
    for cap in capability.CAPABILITIES:
        # 关掉的能力在群里只会回一句「已关闭」，日志里不提就查不到是谁关的。
        if permissions._grant_of(cap.key) == capability.OFF:
            common.logger.warning("permissions.capabilities.%s is off: nobody can use it", cap.key)
    if not live.allowed_groups:
        common.logger.warning("channels.allowed_groups is empty: the bot will not respond in any group")
    if live.enable_forward_bridge and not FORWARD_BRIDGE_TOKEN:
        if forward_bridge._forward_bridge_token():
            common.logger.info(
                "ONEBOT_FORWARD_BRIDGE_TOKEN is empty: using self-signed bridge token file %s",
                FORWARD_BRIDGE_TOKEN_FILE,
            )
        else:
            common.logger.warning(
                "qq_forward bridge token unavailable (%s not writable): bridge will not start",
                FORWARD_BRIDGE_TOKEN_FILE,
            )
    common.logger.info(
        "QQ permissions: admins=%d allowed_groups=%d allowed_private=%d allow_friends=%s",
        len(live.admin_users), len(live.allowed_groups), len(live.allowed_private_users), live.allow_private_friends,
    )


def _nonebot_nicknames() -> tuple[str, ...]:
    """NoneBot 自己的 NICKNAME 配置。空 = 没配。"""
    try:
        raw = getattr(common.driver.config, "nickname", None) or ()
        return tuple(str(item).strip() for item in raw if str(item).strip())
    except Exception:
        return ()


def _warn_trigger_config() -> None:
    """启动时校验触发配置：未知 mode 仍收紧到 mention_only，但不再静默。"""
    config = TriggerConfig.from_live(live)
    if not live.group_trigger_is_known:
        common.logger.warning(
            "unknown trigger.group_mode=%r, using mention_only; valid values: %s",
            live.group_trigger,
            ", ".join(sorted(GROUP_TRIGGER_MODES)),
        )
    if not live.trigger_prefixes:
        common.logger.warning("trigger.prefixes parsed empty; prefix mode can only trigger by @Bot")
    full_width = [p for p in live.trigger_prefixes if "，" in p]
    if full_width:
        common.logger.warning("trigger.prefixes separator must be a half-width comma, got %r", full_width)
    if config.name and not config.name_words:
        common.logger.warning("trigger.signals.name is on but trigger.name.words is empty; it can never match")
    if config.keyword and not config.keyword_words:
        common.logger.warning("trigger.signals.keyword is on but trigger.keyword.words is empty; it can never match")
    if config.random and config.random_probability <= 0:
        common.logger.warning("trigger.signals.random is on but the probability is 0; it can never match")
    if not config.cooldown_exempt_at and (config.cooldown_group_sec or config.cooldown_user_sec):
        common.logger.warning(
            "trigger.cooldown.exempt_at is off: users who @ the bot directly can be silently ignored"
        )
    nicknames = _nonebot_nicknames()
    if nicknames:
        # _check_nickname 命中后会把名字从正文里就地删掉，还会置 to_me —— 于是「用户点名了」
        # 这个信号在进模型前丢失，而且 @ 与点名再也分不开。名字触发要用 trigger.name。
        common.logger.warning(
            "NoneBot NICKNAME=%s strips the name out of the message body and sets to_me; "
            "use trigger.signals.name instead so both the original text and the signal survive",
            list(nicknames),
        )
    common.logger.info(
        "QQ trigger policy: signals=%s prefixes=%s cooldown=group %.0fs/user %.0fs exempt_at=%s llm_intent=%s",
        list(config.enabled_signals()), live.trigger_prefixes,
        config.cooldown_group_sec, config.cooldown_user_sec, config.cooldown_exempt_at,
        config.llm_intent,
    )


async def _auto_like_user(bot: Bot, user_id: int) -> None:
    if not live.enable_auto_like:
        return
    today = dt.datetime.now(common._CST).strftime("%Y-%m-%d")
    if state._adapter.auto_like_today.get(user_id) == today:
        return
    try:
        await bot.call_api("send_like", user_id=int(user_id), times=int(live.auto_like_times))
        state._adapter.auto_like_today[user_id] = today
    except Exception:
        common.logger.debug("send_like failed for user=%s", user_id, exc_info=True)


async def _build_inbound_text(
    event: GroupMessageEvent,
    bot: Bot,
    user_text: str,
    conversation_key: str,
    attachments: List[Dict[str, Any]],
    *,
    exclude_seq: int = 0,
) -> tuple[str, int]:
    """组装提交给 tangqiu_main 的群聊 inbound 文本，并匿名化 QQ 群号/用户号。

    第二个返回值是本次带出的最大历史序号，提交成功后据此推进水位。exclude_seq 是
    当前这条触发消息的序号，它由【当前用户指令】带出，不再进历史块。
    """
    group_id = int(event.group_id)
    pending = state._adapter.group_history.pending(group_id)
    watermark = pending.watermark
    # 水位仍然盖过被排除那一行：它已随指令块进了 durable history，下一轮不该再当历史重发。
    covered_seq = pending.covered_seq
    fresh = [entry for entry in pending.lines if entry.seq != exclude_seq]
    lost = pending.lost
    current_name = anonymize._anonymize_text_for_ai(identity._event_display_name(event))
    current_user = identity._event_user_alias(event)
    now = dt.datetime.now(common._CST).strftime("%Y-%m-%d %H:%M CST")

    parts: List[str] = ["【群聊上下文记录】"]
    metadata = state._adapter.features.coordinator.metadata(state._adapter.features.actor(bot, event)) if state._adapter.features.available else {}
    topic = metadata.get("topic", {})
    if topic.get("topic_id"):
        parts.extend(f"[{line['ref']}] {line['label']}: {line['text']}" for line in topic.get("context", [])
                     if str(line['ref']) != str(getattr(event, "message_id", "")))
        if len(parts) == 1:
            parts.append("（当前话题暂无前文）")
        covered_seq = watermark
    elif lost > 0:
        # 不说这一句，模型会把中间断掉的一段群聊当连续的推理。
        parts.append(f"（此前 {lost} 条群消息超出缓存上限，未包含在内）")
    if not topic.get("topic_id") and fresh:
        parts.extend(entry.text for entry in fresh)
    elif not topic.get("topic_id") and lost <= 0:
        # 已带过历史却没有新行，和「这个群从没说过话」是两回事，不能都说“暂无”。
        parts.append("（无新消息）" if watermark >= 0 else "（暂无）")

    reply_context, quoted_attachments = await history._build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])

    parts.extend([
        "",
        f"当前时间: {now}",
        "【当前用户身份】",
        *identity._user_identity_lines(event, current_name),
        "",
        "【当前用户指令】",
        f"{current_name}（{current_user}）: {anonymize._anonymize_text_for_ai(user_text)}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts), covered_seq


def _parse_direct_draw_command(user_text: str) -> Optional[str]:
    """解析 QQ 侧 /生图 直达命令；返回清理后的绘图需求，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    match = common._DRAW_DIRECT_RE.match(text)
    if not match:
        return None
    prompt = (match.group(1) or "").strip()
    if not prompt:
        prompt = "请根据上下文生成一张合适的图片"
    return prompt


async def _build_draw_direct_inbound_text(event: Event, user_text: str, is_dm: bool) -> str:
    """构造直达绘图节点的入站文本，避免普通入口 AI 再判断一次意图。"""
    now = dt.datetime.now(common._CST).strftime("%Y-%m-%d %H:%M CST")
    name = anonymize._anonymize_text_for_ai(identity._event_display_name(event))
    user = identity._event_user_alias(event)
    target = "私聊" if is_dm else "群聊"
    return "\n".join([
        f"当前时间: {now}",
        f"来源: QQ{target} / /生图 直达命令",
        "",
        "【当前用户身份】",
        *identity._user_identity_lines(event, name),
        "",
        "【绘图请求】",
        f"{name}（{user}）: {user_text}",
        "",
        "请直接按绘图节点规则处理：如果用户要求画图/生图则生成图片；如果用户明确只要提示词/tag则只给提示词。",
    ])


async def _build_private_inbound_text(event: PrivateMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的私聊 inbound 文本，并匿名化 QQ 用户号。"""
    fallback_text = await message_text._event_text_with_forward(bot, event)
    text = anonymize._anonymize_text_for_ai((user_text or fallback_text).strip() or "你好")
    name = anonymize._anonymize_text_for_ai(identity._event_display_name(event))
    user = identity._event_user_alias(event)
    now = dt.datetime.now(common._CST).strftime("%Y-%m-%d %H:%M CST")
    parts: List[str] = [f"当前时间: {now}"]
    reply_context, quoted_attachments = await history._build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    parts.extend([
        "",
        "【当前用户身份】",
        *identity._user_identity_lines(event, name),
        "",
        "【当前用户指令】",
        f"{name}（{user}）: {text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


# 拿不到可确认的图片时必须显式禁止模型从历史里猜，否则它会编出一张不存在的图。
_IMAGE_UNBOUND_HINT = (
    "当前消息或引用链未能绑定到可确认的图片；不得从聊天历史猜测图片内容，请让用户重新发送或直接引用原图。"
)


# 绘图节点只按文字出图，参考图用不上；丢弃时当场告知，别让用户以为图被采纳。
_DRAW_REFERENCE_IMAGE_NOTICE = "绘图暂不支持参考图，已忽略你发送的图片，仅按文字描述生成。"


def _apply_attachment_hints(
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    attachment_errors: List[str],
) -> str:
    """把图片处理提示挂到 inbound 文本上，并把附件路径回填进 [图片] 占位。"""
    if media._text_looks_like_image_query(user_text) and not attachments:
        # 就地 append：调用方在此之后不再读这个列表。
        attachment_errors.append(_IMAGE_UNBOUND_HINT)
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    for att in attachments:
        # 只有图片占 [图片] 位。文件附件混进来会顶掉后面那张图的位置，路径就对错人了。
        if att.get("type") != "image":
            continue
        inbound_text = inbound_text.replace(
            message_text.IMAGE_PLACEHOLDER, f"[{media._attachment_label(att)}: {att['path']}]", 1,
        )
    return inbound_text
