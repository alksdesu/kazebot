"""排队、抢占、连发合并与会话目标解析。"""
from __future__ import annotations

import logging
import asyncio
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot, Event
from ..config import ENTRY_NODE_ID
from adapters._shared import capability
from adapters._shared.feature_gateway import TargetConversation
from adapters._shared.preemption import preempt_candidates
from adapters._shared.trigger_policy import SIGNAL_LLM_INTENT
from ..live_config import live, pinned as pinned_live_config
from ..attachment_policy import source_attachments_from_merged
from ..segments import at_segment_user_ids
from clonoth_sdk import TriggerInfo
from platforms.descriptor import CHANNEL_GROUP, CHANNEL_PRIVATE
from platforms.ids import same_id
from platforms.inbound import InboundFacts

from . import anonymize, common, history, identity, inbound, models, output_guide, permissions, routing, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


async def _try_preempt_running_task(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    inbound_text: str,
    attachments: Optional[List[Dict[str, Any]]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
    get_memory_hints: Callable[[], Dict[str, Any]] | None = None,
    guide: Dict[str, Any] | None = None,
) -> bool:
    """尝试把 QQ 新消息注入当前会话正在运行的入口任务；guide 是这一轮的输出说明。"""
    # 先查询当前 session 的入口任务，再用 preempt_task 注入新消息：让同一会话的
    # 新指令打断并接续旧任务，而不是在已有入口任务运行时并发开新任务。
    if state._adapter.client is None or state._adapter.session_state is None:
        return False

    existing_sid = state._adapter.session_state.get_session_id(conversation_key)
    if not existing_sid:
        return False

    try:
        running_tasks = await state._adapter.client.get_running_tasks(existing_sid)
    except Exception:
        common.logger.exception("preempt_v2: get running tasks failed")
        return False

    def trigger_of(task: Any) -> TriggerInfo | None:
        source_seq = task.source_inbound_seq or 0
        return state._adapter.session_state.get_trigger(source_seq) if source_seq else None

    def same_sender(trigger: TriggerInfo) -> bool:
        return same_id(
            identity._verified_sender_id(trigger.platform_data.get("event")), identity._verified_sender_id(event),
        )

    for rt, rt_trigger in preempt_candidates(
        running_tasks,
        trigger_of=trigger_of,
        is_dm=is_dm,
        same_sender=same_sender,
        route_hints=platform_updates.get("_route_hints") or {},
    ):
        rt_src_seq = rt.source_inbound_seq or 0
        try:
            preempt_args: Dict[str, Any] = {"message": inbound_text, "attachments": attachments}
            if get_memory_hints is not None:
                preempt_args["memory_hints"] = get_memory_hints()
            if guide is not None:
                preempt_args["output_guide"] = guide
            ok = await state._adapter.client.preempt_task(
                rt.task_id,
                **preempt_args,
            )
        except Exception:
            common.logger.exception("preempt_v2: preempt task failed")
            continue

        if not ok:
            continue

        sending._bind_qq_reply_waiter(conversation_key, rt_src_seq)

        # 打断不产生新的 inbound_seq：把旧 trigger 的平台对象换成这一条，最终回复才会引用新消息。
        fresh_platform = dict(platform_updates)
        fresh_platform["last_typing_time"] = time.time()
        rt_trigger.platform_data.update(fresh_platform)

        state._adapter.session_targets[existing_sid] = dict(fresh_platform)
        state._adapter.conversation_bots[conversation_key] = bot
        await routing._remember_route_state(
            stable_conversation_key=conversation_key,
            real_conversation_key=routing._real_conversation_key(conversation_key),
            session_id=existing_sid,
            target=fresh_platform,
        )
        common.logger.info("preempt_v2: injected into QQ task %s", rt.task_id[:8])
        return True

    return False


async def _submit_or_preempt_inbound(
    *,
    bot: Bot,
    event: Event,
    channel: str,
    real_conversation_key: str,
    stable_conversation_key: str,
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
    entry_node_id: str = "",
    history_watermark: int = -1,
    direct_interaction: bool = True,
    memory_user_ids: tuple[str, ...] = (),
) -> bool:
    """提交或打断 QQ 入站消息；对外使用稳定哈希 conversation_key，对内保留真实路由。"""
    if state._adapter.client is None or state._adapter.session_state is None:
        return False

    group_id = platform_updates.get("group_id") if platform_updates.get("type") == "group" else None
    watermark_generation = state._adapter.session_state.watermark_generation(int(group_id)) if group_id is not None else None
    memory_hints: Dict[str, Any] | None = None

    def get_memory_hints() -> Dict[str, Any]:
        nonlocal memory_hints
        if memory_hints is None:
            memory_hints = {"subjects": anonymize._collect_memory_subjects(
                event, user_text, stable_conversation_key, direct_interaction,
                mentioned_user_ids=memory_user_ids,
            )}
        return memory_hints

    # 说明随提交单独给，不拼进正文：正文会写进对话历史，说明却每轮都在变。
    guide = output_guide._turn_output_guide(
        stable_conversation_key, is_dm=is_dm, entry_node_id=entry_node_id,
    ).payload()

    # 显式命令自带入口节点，打断注入会落到正在跑的那个节点上。
    if live.enable_preempt and not entry_node_id:
        preempt_ok = await _try_preempt_running_task(
            bot=bot,
            event=event,
            conversation_key=stable_conversation_key,
            inbound_text=inbound_text,
            attachments=attachments or None,
            is_dm=is_dm,
            platform_updates=platform_updates,
            get_memory_hints=get_memory_hints,
            guide=guide,
        )
        if preempt_ok:
            # preempt 注入的消息会被写进 ConversationStore（engine/builtin/preempt.py），
            # 但不产生新的 inbound_seq，没有 inbound_accepted 可等，只能就地推进。
            if (
                group_id is not None and history_watermark >= 0
                and watermark_generation == state._adapter.session_state.watermark_generation(int(group_id))
            ):
                state._adapter.session_state.advance_watermark(int(group_id), history_watermark)
            if state._adapter.features.available:
                state._adapter.features.burst_store.remove(stable_conversation_key, platform_updates.get("_route_hints", {}).get("source_message_refs", []))
            return True

    # 匿名消息的发送者号是全群共用的占位号，报给 supervisor 就是空串：无法核验的人不认，也就不会被当成同一个人。
    sender_id = identity._verified_sender_id(event)
    result = await state._adapter.client.submit_inbound(
        channel=channel,
        conversation_key=stable_conversation_key,
        text=inbound_text,
        message_id=str(getattr(event, "message_id", "")),
        attachments=attachments or None,
        use_context=True,
        entry_node_id=entry_node_id or ENTRY_NODE_ID,
        # 以未兜底的原始 entry_node_id 为准：兜底值人人都非空，会把每条消息都钉住，视觉路由和 AI switch_node 全线失效。
        entry_node_pinned=bool(entry_node_id),
        platform_auth={
            "platform": "qq",
            "user_id": sender_id,
            "is_admin": bool(sender_id) and permissions._is_admin_user(sender_id),
            "group_role": permissions._requester(event).group_role if not is_dm else "",
            "group_scope": stable_conversation_key if not is_dm else "",
            "bot_scope": routing._active_bot_scope(),
        },
        route_hints={
            "platform": "qq",
            "channel": channel,
            "has_image": any(str(a.get("type") or "") == "image" for a in (attachments or [])),
            "target_type": "private" if is_dm else "group",
            **dict(platform_updates.get("_route_hints") or {}),
        },
        memory_hints=get_memory_hints(),
        output_guide=guide,
    )
    if not result.session_id or not result.accepted:
        return False

    history._note_inbound_seq(result.inbound_seq)
    sending._bind_qq_reply_waiter(stable_conversation_key, int(result.inbound_seq or 0))

    if group_id is not None and history_watermark >= 0:
        if result.inbound_seq:
            # 等 inbound_accepted 再推进：supervisor 没真正收下就推，那几行历史就没人再发了。
            state._adapter.session_state.register_watermark(
                int(result.inbound_seq), int(group_id), history_watermark,
                generation=watermark_generation,
            )
        else:
            if watermark_generation == state._adapter.session_state.watermark_generation(int(group_id)):
                state._adapter.session_state.advance_watermark(int(group_id), history_watermark)

    state._adapter.real_conversation_keys[stable_conversation_key] = real_conversation_key
    state._adapter.session_state.register_session(stable_conversation_key, result.session_id)
    target = dict(platform_updates)
    target["conversation_key"] = stable_conversation_key
    state._adapter.session_targets[result.session_id] = target
    state._adapter.conversation_bots[stable_conversation_key] = bot
    await routing._remember_route_state(
        stable_conversation_key=stable_conversation_key,
        real_conversation_key=real_conversation_key,
        session_id=result.session_id,
        target=target,
    )

    if result.inbound_seq:
        react_meta: Dict[str, Any] = {}
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=stable_conversation_key,
            session_id=result.session_id,
            is_dm=is_dm,
            platform_data={
                **platform_updates,
                "last_typing_time": time.time(),
                **react_meta,
            },
        )
        state._adapter.session_state.register_trigger(trigger)
    if state._adapter.features.available:
        state._adapter.features.burst_store.remove(stable_conversation_key, platform_updates.get("_route_hints", {}).get("source_message_refs", []))
    return True


def _queue_merge_identity(item: models.QueuedInbound) -> tuple[Any, ...] | None:
    facts = inbound._queued_facts(item)
    sender = facts.sender
    if not sender.platform_id or sender.anonymous:
        return None
    return (
        item.real_conversation_key, item.channel, item.is_dm, item.entry_node_id,
        facts.self_id, sender.platform_id, inbound._event_sender_role(item.event),
        sender.listed_admin, tuple(permissions._grant_of(cap.key) for cap in capability.CAPABILITIES),
    )


def _merge_queued_inbound(existing: models.QueuedInbound, item: models.QueuedInbound) -> None:
    """后来那条只接上它自己的引用和指令。

    水位留在前一条：后来那条带出的新历史行没有拼进来，推过去它们就再也不会发给模型。
    """
    previous_hints = dict(existing.platform_updates.get("_route_hints") or {})
    previous_purpose = existing.platform_updates.get("_response_purpose", "direct")
    previous_stamp = existing.platform_updates.get("_proactive_stamp")
    direct_response = "direct" in {previous_purpose, item.platform_updates.get("_response_purpose", "direct")}
    if direct_response and previous_purpose == "ambient":
        # 插话那件并进了正式请求，这一轮是在回人家的话，尾句换回执行指令。
        existing.text = inbound._as_direct_request(existing.text)
    correction = re.match(r"^(?:更正|纠正|刚才(?:说|写)错|不对[,，]|不是.+[，,])", item.user_text.strip())
    heading = "同一用户更正此前输入，以这条更正为准；已完成的外部操作不能据此假定已撤销" if correction else "排队期间追加消息"
    appended = f"【{heading}】\n{item.merge_segment}"
    existing.merge_text = f"{existing.merge_segment}\n\n{appended}"
    existing.text = f"{existing.text}\n\n{appended}"
    existing.direct_interaction = existing.direct_interaction or item.direct_interaction
    existing.attachments.extend(item.attachments)
    existing.memory_user_ids = tuple(dict.fromkeys((*existing.memory_user_ids, *item.memory_user_ids)))
    source_refs = list(dict.fromkeys([
        *existing.platform_updates.get("_route_hints", {}).get("source_message_refs", []),
        *item.platform_updates.get("_route_hints", {}).get("source_message_refs", []),
    ]))
    existing.event = item.event
    existing.facts = item.facts
    existing.platform_updates = dict(item.platform_updates)
    existing.platform_updates["_source_attachments"] = source_attachments_from_merged(existing.attachments)
    if source_refs:
        existing.platform_updates.setdefault("_route_hints", {})["source_message_refs"] = source_refs
    hints = existing.platform_updates.setdefault("_route_hints", {})
    actions = {previous_hints.get("response_action", "reply"), hints.get("response_action", "reply")}
    hints["response_action"] = "task" if "task" in actions else "reply" if "reply" in actions else "short_reply"
    existing.platform_updates["_response_purpose"] = "direct" if direct_response else "ambient"
    hints["delivery_purpose"] = existing.platform_updates["_response_purpose"]
    if previous_stamp is not None and existing.platform_updates.get("_proactive_stamp") != previous_stamp:
        # 两件并成一轮只会有一次回复，前一件记下的那笔主动回复退回去。
        inbound._refund_proactive(existing.platform_updates.get("group_id"), previous_stamp)
    existing.user_text = f"{existing.user_text}\n{item.user_text}"
    if state._adapter.features.available and source_refs:
        state._adapter.features.stage_burst(existing)


def _purpose_of(item: models.QueuedInbound) -> str:
    return str(item.platform_updates.get("_response_purpose") or "direct")


def _yields_to_queued(queued: models.QueuedInbound, incoming: models.QueuedInbound) -> bool:
    """同会话里已经有正式请求在排队，插话就不再排：等它轮到，正式请求的回复已经把话接过去了。

    合进正式请求也不行：后来那件的引用和来源会盖掉前一件，正式请求的回复就成了在回插话那条。
    """
    return _purpose_of(incoming) == "ambient" and _purpose_of(queued) == "direct"


def _drop_yielded_inbound(item: models.QueuedInbound) -> None:
    """让给正式请求的插话：撤销它记下的主动回复，连发暂存里的那几行也删掉，重启后不再恢复。"""
    common.logger.info(
        "QQ soft trigger dropped: a direct request is already queued in conversation=%s",
        item.real_conversation_key,
    )
    inbound._refund_proactive(item.platform_updates.get("group_id"), item.platform_updates.get("_proactive_stamp"))
    if state._adapter.features.available:
        refs = item.platform_updates.get("_route_hints", {}).get("source_message_refs", [])
        if refs:
            state._adapter.features.burst_store.remove(item.stable_conversation_key, refs)


def _ensure_delivery_purpose(item: models.QueuedInbound) -> None:
    """没有社区功能的部署也要把这一轮是直接找 bot 还是主动插话带下去：engine 和发送那一步都按它分。"""
    purpose = "ambient" if _purpose_of(item) == "ambient" else "direct"
    item.platform_updates["_response_purpose"] = purpose
    hints = item.platform_updates.get("_route_hints")
    if not isinstance(hints, dict):
        hints = {}
        item.platform_updates["_route_hints"] = hints
    hints.setdefault("delivery_purpose", purpose)


async def _enqueue_or_submit_inbound(item: models.QueuedInbound) -> bool:
    if not state._adapter.features.available:
        _ensure_delivery_purpose(item)
        return await _queue_or_submit_ready(item)
    metadata = await state._adapter.features.prepare(item.bot, item.event)
    if not metadata:
        _ensure_delivery_purpose(item)
        return await _queue_or_submit_ready(item)
    item.platform_updates["_route_hints"] = {
        "response_action": metadata.get("action", "reply"),
        "source_message_refs": [str(getattr(item.event, "message_id", ""))],
        "reply_message_id": str(getattr(item.event, "message_id", "")),
    }
    # 两边算的是同一件事（被 @、回复 bot、带前缀、显式信号），取并集只为旧调用方没带 _response_purpose 时也不降级。
    direct = metadata.get("direct") or item.platform_updates.get("_response_purpose") == "direct"
    item.platform_updates["_response_purpose"] = "direct" if direct else "ambient"
    item.platform_updates["_feature_actor"] = metadata["actor"]
    item.platform_updates["_route_hints"]["delivery_purpose"] = item.platform_updates["_response_purpose"]
    message = item.event.get_message() if hasattr(item.event, "get_message") else []
    item.memory_user_ids = tuple(dict.fromkeys((*item.memory_user_ids, *at_segment_user_ids(message))))
    identity = _queue_merge_identity(item)
    if metadata["policy"].get("settings", {}).get("merge_window_sec", 0) > 0:
        state._adapter.features.stage_burst(item)
    return await state._adapter.features.coordinator.submit(item, metadata["policy"].get("settings", {}), identity,
                                               _queue_or_submit_ready, _merge_queued_inbound)


async def _queue_or_submit_ready(item: models.QueuedInbound) -> bool:
    if not live.enable_queue:
        return await _submit_or_preempt_inbound(
            bot=item.bot,
            event=item.event,
            channel=item.channel,
            real_conversation_key=item.real_conversation_key,
            stable_conversation_key=item.stable_conversation_key,
            inbound_text=item.text,
            user_text=item.user_text,
            attachments=item.attachments,
            is_dm=item.is_dm,
            platform_updates=item.platform_updates,
            entry_node_id=item.entry_node_id,
            history_watermark=item.history_watermark,
            direct_interaction=item.direct_interaction,
            memory_user_ids=item.memory_user_ids,
        )

    message = item.event.get_message() if hasattr(item.event, "get_message") else []
    item.memory_user_ids = tuple(dict.fromkeys((*item.memory_user_ids, *at_segment_user_ids(message))))
    queued = await state._adapter.qq_queue.put(
        item.stable_conversation_key, item,
        identity_of=_queue_merge_identity, merge=_merge_queued_inbound, discard=_yields_to_queued,
    )
    if not queued:
        # 让出去不算没收下：调用方据返回值决定要不要在群里报错，这里不该报。
        _drop_yielded_inbound(item)
    return True


def _queue_worker_should_exit(worker_index: int) -> bool:
    """这个 worker 该退了吗。

    缩容时多出来的那几个立刻退，留下的接着干；整个队列被关掉时最后一批要先把已排队的
    消息清完再退 —— 关开关不该让排在队里的用户消息无声消失。
    只作为 qq_queue.take 的退出判定调用：持着队列锁读排队件数才不会过期。
    """
    target = live.queue_workers if live.enable_queue else 0
    if worker_index < target:
        return False
    return target > 0 or not state._adapter.qq_queue


async def _qq_queue_worker_forever(worker_index: int) -> None:
    """串行队列 worker。

    退出判定读实时配置而不是钉住的快照：这个 task 活得比任何一条消息都长，钉住就永远
    看不到运营者刚改的 workers 数量。取到件之后才钉住，处理这一条期间配置不再变。
    """
    while True:
        taken = await state._adapter.qq_queue.take(lambda: _queue_worker_should_exit(worker_index))
        if taken is None:
            return
        _, item = taken

        with pinned_live_config():
            reply_waiter: models.QueueReplyWaiter | None = None
            if live.queue_wait_for_reply:
                reply_waiter = models.QueueReplyWaiter(asyncio.Event())
                state._adapter.qq_waiting_replies[item.stable_conversation_key] = reply_waiter
            try:
                accepted = await _submit_or_preempt_inbound(
                    bot=item.bot,
                    event=item.event,
                    channel=item.channel,
                    real_conversation_key=item.real_conversation_key,
                    stable_conversation_key=item.stable_conversation_key,
                    inbound_text=item.text,
                    user_text=item.user_text,
                    attachments=item.attachments,
                    is_dm=item.is_dm,
                    platform_updates=item.platform_updates,
                    entry_node_id=item.entry_node_id,
                    history_watermark=item.history_watermark,
                    direct_interaction=item.direct_interaction,
                    memory_user_ids=item.memory_user_ids,
                )
                if accepted and reply_waiter is not None and reply_waiter.inbound_seq > 0:
                    try:
                        await asyncio.wait_for(reply_waiter.event.wait(), timeout=live.queue_reply_timeout)
                    except asyncio.TimeoutError:
                        common.logger.warning("QQ queue reply wait timed out conversation=%s timeout=%ss", item.real_conversation_key, live.queue_reply_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                common.logger.exception("failed to process QQ queue item conversation=%s", item.real_conversation_key)
            finally:
                if state._adapter.qq_waiting_replies.get(item.stable_conversation_key) is reply_waiter:
                    state._adapter.qq_waiting_replies.pop(item.stable_conversation_key, None)
                await state._adapter.qq_queue.release(item.stable_conversation_key)
            interval = live.queue_interval
        if interval > 0:
            await asyncio.sleep(interval)


def _group_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_group:{group_id} 会话键解析群号。"""
    prefix = "qq_group:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _private_user_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_private:{user_id} 会话键解析私聊用户号。"""
    prefix = "qq_private:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _target_from_conversation_key(conversation_key: str) -> Optional[Dict[str, Any]]:
    """根据 conversation_key 还原 QQ 回复目标，供无 trigger 的回调兜底使用。"""
    # 2026-05-01 修改原因：EventRouter 的 fallback 回调只有 conversation_key；
    # 这里集中解析 group/private 两种 key，避免发送逻辑继续硬编码群聊。
    conversation_key = routing._real_conversation_key(conversation_key)
    group_id = _group_id_from_conversation_key(conversation_key)
    if group_id is not None:
        return {"type": "group", "group_id": group_id}
    user_id = _private_user_id_from_conversation_key(conversation_key)
    if user_id is not None:
        return {"type": "private", "user_id": user_id}
    return None


def _target_from_platform_data(platform_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 trigger 平台数据中提取 QQ 回复目标，带上触发消息和发送者，第一条回复据此引用 + @。

    按显式 type 区分私聊和群聊：只看有没有 user_id 会把群成员误判成私聊目标。
    """
    event = platform_data.get("event")
    msg_id = getattr(event, "message_id", None) if event else None
    sender_id = getattr(event, "user_id", None) if event else None
    # 匿名消息的 user_id 是占位号，at 出去群里只会看到一串数字。
    if event is not None and identity._anonymous_identity(event):
        sender_id = None
    # @全体成员只替名单管理员发：任何能触发 bot 的人都能让模型写出 [at:all]。
    may_mention_everyone = event is not None and permissions._requester(event).listed_admin
    target_type = platform_data.get("type")
    conversation_key = str(platform_data.get("conversation_key") or "")
    if target_type == "private" and platform_data.get("user_id") is not None:
        t: Dict[str, Any] = {"type": "private", "user_id": int(platform_data["user_id"])}
        if "_feature_actor" in platform_data:
            t["_feature_actor"] = platform_data["_feature_actor"]
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        return t
    if target_type == "group" and platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if "_response_purpose" in platform_data:
            t["_response_purpose"] = platform_data["_response_purpose"]
        if platform_data.get("_trigger_source"):
            t["_trigger_source"] = platform_data["_trigger_source"]
        if "_feature_actor" in platform_data:
            t["_feature_actor"] = platform_data["_feature_actor"]
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        if may_mention_everyone:
            t[sending.MENTION_EVERYONE_FLAG] = True
        return t
    if platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if platform_data.get("_trigger_source"):
            t["_trigger_source"] = platform_data["_trigger_source"]
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        if may_mention_everyone:
            t[sending.MENTION_EVERYONE_FLAG] = True
        return t
    return None


def _feature_target(target: Mapping[str, Any]) -> TargetConversation:
    """QQ 发送目标落在哪个会话上，给社区功能网关算会话键和引用。"""
    is_group = target.get("type") == "group"
    return TargetConversation(
        kind=CHANNEL_GROUP if is_group else CHANNEL_PRIVATE,
        identity=target.get("group_id") if is_group else target.get("user_id"),
        reply_ref=str(target.get("reply_message_id") or ""),
    )


def _community_targets() -> Iterator[Dict[str, Any]]:
    """社区功能轮询安静期和通知的会话：放行名单里的每个群。"""
    for group_id in live.allowed_groups:
        yield {"type": "group", "group_id": int(group_id), "_response_purpose": "ambient"}


def _staged_event_fields(event: Any) -> Dict[str, Any]:
    """连发暂存行里跟着 QQ 事件走的字段。群号按事件里的 int 存，恢复时原样回填进发送目标。"""
    return {"event_time": getattr(event, "time", time.time()), "group_id": getattr(event, "group_id", None)}


def _restore_staged_inbound(
    bot: Bot, event: Event, data: Mapping[str, Any], facts: InboundFacts, actor: Dict[str, Any],
) -> models.QueuedInbound:
    """按连发暂存行和重放出来的事件重建排队项。群目标不带 user_id，和实时入站一致。"""
    platform = {"bot": bot, "event": event, "type": "private" if data["is_dm"] else "group",
                "conversation_key": data["scope"], "_route_hints": data.get("route_hints") or {"source_message_refs": data["refs"]},
                "_response_purpose": data.get("response_purpose", "direct"), "_feature_actor": actor,
                "_source_attachments": data["attachments"]}
    # 旧暂存行没记来源：插话那种按模型判断算，不引用；直接找 bot 的留空，照旧引用。
    source = str(data.get("trigger_source") or "")
    if not source and platform["_response_purpose"] == "ambient":
        source = SIGNAL_LLM_INTENT
    if source:
        platform["_trigger_source"] = source
    if data["is_dm"]:
        platform["user_id"] = event.user_id
    if data["group_id"] is not None:
        platform["group_id"] = data["group_id"]
    return models.QueuedInbound(None, bot, event, data["channel"], data["real_key"], data["scope"],
                                data["text"], data["attachments"], data["is_dm"], platform, data["user_text"],
                                entry_node_id=data["entry_node_id"], history_watermark=-1,
                                direct_interaction=data["direct_interaction"], memory_user_ids=tuple(data["memory_user_ids"]),
                                facts=facts, merge_text=str(data.get("merge_text") or ""))


def _get_fallback_bot() -> Optional[Bot]:
    """获取 fallback 发送用 Bot；EventRouter 可能在 matcher 外触发回调。"""
    if state._adapter.last_bot is not None:
        return state._adapter.last_bot
    try:
        return get_bot()
    except Exception:
        return None
