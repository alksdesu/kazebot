"""AdapterCallbacks 的 QQ 实现。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from .. import capability
from ..live_config import live
from ..send_contract import OneBotSendContractError, OutboundSendContext, context_from_sources
from clonoth_sdk import ChildTaskState, MainTaskState, TriggerInfo

from . import approval, common, history, outbound_attachments, permissions, proactive, queueing, routing, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


class TangQiuCallbacks:
    """Clonoth SDK 的 QQ 平台回调实现。

    发送最终回复、附件和主节点中间回复；其余进度、审批、typing、子任务日志均静默。
    """

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        main_state: Optional[MainTaskState] = None,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点最终回复。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or queueing._get_fallback_bot()
        target = queueing._target_from_platform_data(platform_data) or queueing._target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_reply missing target for session={trigger.session_id}"
            )
        # 进度提示靠这个标记闭嘴。设在校验之后：发不出去的回复不该顺带把提示也堵掉。
        platform_data["_qq_final_reply_sent"] = True
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            main_state=main_state,
            platform_data=callback_data.get("platform_data"),
        )
        outbound_attachments._apply_delivery_context(target, send_context)
        await state._adapter.features.check_send(bot, target)
        # 提取 [REACT:ID] 标记
        final_text = text or ""
        if final_text:
            from ..emoji_handler import _extract_reactions
            final_text, reactions = _extract_reactions(final_text)
            if reactions:
                await self.add_reactions(trigger, reactions)
        source_attachments = platform_data.get("_source_attachments")
        if not isinstance(source_attachments, list):
            source_attachments = []
        await outbound_attachments._send_text_and_attachments(
            bot,
            target,
            final_text,
            attachments or [],
            source_attachments=source_attachments,
            send_context=send_context,
        )
        sending._mark_qq_reply_finished(trigger.inbound_seq)

    async def send_reply_attachment(self, session_id: str, path: str, *args: Any, **kwargs: Any) -> None:
        """兼容旧式 session_id 附件回调；当前 SDK 通常把附件放在 send_reply 中。"""
        target = state._adapter.session_targets.get(session_id) or state._adapter.persisted_session_targets.get(session_id)
        if not target:
            raise OneBotSendContractError(
                f"send_reply_attachment missing target for session={session_id}"
            )
        bot = target.get("bot") or queueing._get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_reply_attachment missing bot for session={session_id}"
            )
        await outbound_attachments._send_attachment_path(bot, target, Path(path))

    async def send_intermediate_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        *,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点中间回复，但不写入群历史。"""
        if not text:
            return
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or queueing._get_fallback_bot()
        target = queueing._target_from_platform_data(platform_data) or queueing._target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing target for session={trigger.session_id}"
            )
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            platform_data=callback_data.get("platform_data"),
        )
        outbound_attachments._apply_delivery_context(target, send_context)
        await state._adapter.features.check_send(bot, target)
        # 提取 [REACT:ID] 标记
        from ..emoji_handler import _extract_reactions
        text, reactions = _extract_reactions(text)
        if reactions:
            await self.add_reactions(trigger, reactions)
        if text:
            source_attachments = platform_data.get("_source_attachments")
            if not isinstance(source_attachments, list):
                source_attachments = []
            await sending._send_split_text(
                bot,
                target,
                text,
                source_attachments=source_attachments,
                send_context=send_context,
            )

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        node_id: str = "",
        is_final: bool = True,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送按会话路由的输出。"""
        target = queueing._target_from_conversation_key(conversation_key)
        if target is None:
            raise OneBotSendContractError(
                f"send_to_channel missing target for conversation={conversation_key}"
            )
        # [2026-07-19] 修复生图 op=recent 查不到图：_target_from_conversation_key
        # 只返回 {type, group_id/user_id}，缺 conversation_key，导致
        # _send_attachments_confirmed 的成功登记链路信息不全。
        # 这里补回 conversation_key（用真实会话 key），让附件登记 bucket 与
        # qq_forward op=recent 的查询 bucket 保持一致。
        if not target.get("conversation_key"):
            target["conversation_key"] = routing._real_conversation_key(conversation_key)
        bot = state._adapter.conversation_bots.get(conversation_key) or queueing._get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_to_channel missing bot for conversation={conversation_key}"
            )
        send_context = delivery_context or context_from_sources(
            platform_data=callback_data.get("platform_data"),
            conversation_key=conversation_key,
        )
        outbound_attachments._apply_delivery_context(target, send_context)
        await outbound_attachments._send_text_and_attachments(
            bot,
            target,
            text or "",
            attachments or [],
            send_context=send_context,
        )
        if is_final:
            sending._mark_qq_reply_finished(send_context.source_inbound_seq)

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        sending._mark_qq_reply_finished(trigger.inbound_seq)

    async def update_progress(self, trigger: TriggerInfo, state: MainTaskState) -> None:
        """根据主任务进度切换触发消息上的 React 表情。"""
        # 2026-05-03 修改原因：EventRouter 不展示 QQ 进度文本，但 QQ 侧需要补齐
        # React 链中的 LLM 推理和工具阶段。做法是读取 MainTaskState.stream_parts
        # 与 progress_records：首次流式文本进入 thinking，真实工具执行进度进入 tool；
        # 目的在于复用 SDK sweep 回调并用阶段元数据避免高频 stream_delta 抖动。
        platform_data = trigger.platform_data
        event = platform_data.get("event")
        if not event or not hasattr(event, "message_id"):
            return
        bot = platform_data.get("bot") or queueing._get_fallback_bot()
        if not bot:
            return

        target = queueing._target_from_platform_data(platform_data) or queueing._target_from_conversation_key(trigger.conversation_key)
        has_tool_progress = any("执行" in record and "个工具" in record for record in state.progress_records)
        # 只看最新一条：progress_records 是累积的，用 any() 扫全量的话，搜索工具跑完之后
        # 那条记录还留着，条件就永远成立 —— 后面明明在跑别的命令，报的还是「还在联网搜索中」。
        has_search_progress = bool(state.progress_records) and outbound_attachments._progress_mentions_search(
            state.progress_records[-1]
        )
        if has_tool_progress or has_search_progress:
            if has_search_progress and target:
                await outbound_attachments._maybe_send_search_progress_notice(bot, target, platform_data)
            return

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: Optional[TriggerInfo] = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        return None

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        return None

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: Dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """QQ 端审批必须由管理员私聊确认；不再自动放行。"""
        if state._adapter.client is None:
            common.logger.warning(
                "approval review skipped on QQ: client unavailable id=%s operation=%s",
                approval_id,
                operation,
            )
            return

        approval._prune_expired_approvals()
        state._adapter.pending_approvals[approval_id] = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "created_at": time.time(),
        }

        # 档位关掉和名单为空是同一种局面：没有任何人能作出决定，挂着只会等到超时。
        approval_off = permissions._grant_of("approval") == capability.OFF
        if approval_off or not live.admin_users:
            why = (
                "permissions.capabilities.approval is off"
                if approval_off
                else "CLONOTH_ADMIN_QQ_USERS is not configured"
            )
            try:
                await state._adapter.client.approve(
                    approval_id,
                    decision="deny",
                    comment=f"QQ adapter denied because {why}.",
                )
            except Exception:
                common.logger.exception("approval deny failed on QQ: id=%s operation=%s", approval_id, operation)
            state._adapter.pending_approvals.pop(approval_id, None)
            approval._record_settled_approval(approval_id, admin_id=0, decision="deny", operation=operation)
            common.logger.warning("approval denied on QQ: %s id=%s operation=%s", why, approval_id, operation)
            return

        bot = queueing._get_fallback_bot()
        if not bot:
            common.logger.warning("approval review pending but no QQ bot available: id=%s operation=%s", approval_id, operation)
            return

        summary = approval._approval_summary(approval_id, operation, details or {})
        delivered = 0
        for admin_id in live.admin_users:
            try:
                approval_context = proactive._request_send_context(
                    "approval-admin", f"{approval_id}:{admin_id}", conversation_key,
                )
                sent_mid = await sending._send_qq_message(
                    bot, {"type": "private", "user_id": int(admin_id)}, summary,
                    send_context=approval_context,
                )
                delivered += 1
                # 记录审批消息 id -> 卡片锚点，使管理员可直接引用该消息回复“审批同意/拒绝”。
                approval._remember_approval_message(sent_mid, approval_id, int(admin_id))
            except Exception:
                common.logger.exception("send approval request to QQ admin failed: admin=%s id=%s", admin_id, approval_id)

        target = queueing._target_from_conversation_key(conversation_key)
        if target:
            try:
                await sending._send_qq_message(
                    bot, target, "当前操作需要 Clonoth 管理员审批，已提交等待处理。",
                    send_context=proactive._request_send_context(
                        "approval", approval_id, conversation_key,
                    ),
                )
            except Exception:
                common.logger.debug("send approval pending notice failed: id=%s", approval_id, exc_info=True)

        common.logger.info(
            "approval pending on QQ: id=%s operation=%s admins_notified=%s",
            approval_id,
            operation,
            delivered,
        )

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: List[str]) -> None:
        """按白名单在触发消息上贴模型表态；开关与 API 容错由 _set_message_react 统一负责。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or queueing._get_fallback_bot()
        event = platform_data.get("event")
        if not bot or not event:
            return
        # 提示词里那句「一条回复最多一个」只是请求：模型能一次写好几个标记，中间回复和
        # 最终回复还会各解析一次。按触发消息记账，贴上一个就不再贴。
        if platform_data.get("_react_done"):
            return
        for emoji_id in reactions:
            eid = str(emoji_id or "").strip()
            if eid not in common._REACT_MODEL_EMOJIS:
                continue
            if await outbound_attachments._set_message_react(bot, event, eid, True):
                platform_data["_react_done"] = True
            break

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: List[TriggerInfo]) -> None:
        """上下文重置时同步清理 QQ 侧的上下文副本并重置高水位。"""
        for trigger in cleaned_triggers:
            sending._mark_qq_reply_finished(trigger.inbound_seq)
        target = queueing._target_from_conversation_key(conversation_key)
        if not target:
            return
        # compact 只是把早期原文换成摘要，缓存要留着：重置水位后下一轮重发这 20 行，
        # 是把摘要里丢掉的近期细节补回来最便宜的办法。clear 则连缓存一起丢。
        if reason != "compact":
            history._purge_conversation_side_state(conversation_key, target)
        group_id = target.get("group_id")
        if target.get("type") == "group" and group_id is not None:
            history._reset_group_history_watermark(int(group_id))
            if reason != "compact":
                # compact 不清缓存，也就没有「旧产物落回空缓存」这回事。
                history._raise_context_clear_barrier(int(group_id), cleaned_triggers)

    async def on_engine_restarted(self, payload: Dict[str, Any]) -> None:
        """Engine 重启后无法确认各群历史是否还在 engine 侧，全部重置以重发一次完整历史。"""
        # 序号由 supervisor 分配，重启后不保证还在原来那条线上；留着旧门槛会把之后
        # 每一条正常回复都判成过期。此时在飞的任务也已经全没了，门槛没有意义。
        # 放在 session_state 守卫之前：这件事跟水位存不存在无关。
        state._adapter.context_clear_barrier.clear()
        if state._adapter.session_state is None:
            return
        for group_id in list(state._adapter.group_history.keys()):
            history._reset_group_history_watermark(int(group_id))

    async def on_task_complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_fail(self, *args: Any, **kwargs: Any) -> None:
        return None
