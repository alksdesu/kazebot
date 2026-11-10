"""用户那一轮收尾时补进对话的话：失败时发给用户的那句和留在历史里的说明，晚合并的那一轮开头标的起止时间。"""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Any

from engine.conversation_routing import LATE_MERGE_KEY, late_merge_meta
from engine.conversation_store import Message, MessageType
from engine.run_budget import parse_timestamp

from .task_traits import is_ambient_task, is_internal_task
from .types import Task

FAILED_TURN_MARK_KEY = "turn_failed"
FAILED_TURN_NOTICE = "[SYSTEM] 这一轮没做完，没有给出回复（{cause}）；用户那边只收到一句报错。"
_CAUSE_CHARS = 200
_DETAIL_CHARS = 160
# 这些类别的 error 是上游 API 答的话，原文带给用户；network/timeout/incomplete 的是本地传输层的英文，
# auth 的可能带密钥片段（群里人人看得到），都只说人话。
_DETAIL_KINDS = frozenset({
    "rate_limit", "overloaded", "server", "content_filter",
    "context_length", "bad_request", "invalid_response", "unknown",
})
# 模型调用失败按 provider 判的类别说：起因，和给用户的下一步
_KIND_TEXTS = {
    "network": ("连不上模型服务", "稍后再发一次试试"),
    "timeout": ("模型服务迟迟没有回应", "稍后再发一次试试"),
    "overloaded": ("模型服务正忙", "稍后再发一次试试"),
    "server": ("模型服务出错了", "稍后再发一次试试"),
    "rate_limit": ("模型服务的额度或频率到了上限", "过一会儿再试"),
    "incomplete": ("模型的回复中途断了", "再发一次试试"),
    "invalid_response": ("模型服务回来的内容读不了", "再发一次试试"),
    "content_filter": ("内容被模型服务拦下了", "换个说法试试"),
    "context_length": ("对话太长，模型装不下了", "压缩或重置会话后再试"),
    "auth": ("模型服务不认这个渠道的密钥", "需要管理员检查渠道配置"),
    "bad_request": ("模型服务拒绝了这次请求", "需要管理员看一下日志"),
    "unknown": ("调用模型时出错了", "稍后再发一次试试"),
}
_FALLBACK_CAUSE = "处理时出了问题"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cause_and_advice(result: Mapping[str, Any] | None) -> tuple[str, str]:
    """模型调用失败按类别说，上游答了话的类别把原话附在后面；别的失败用任务自己写的原因，太长截断。"""
    result = result if isinstance(result, Mapping) else {}
    kind = str(result.get("error_kind") or "").strip().lower()
    error = " ".join(str(result.get("error") or "").split())
    if kind in _KIND_TEXTS:
        cause, advice = _KIND_TEXTS[kind]
        if kind in _DETAIL_KINDS and error and error.lower() != kind:
            cause = f"{cause}（{_cut(error, _DETAIL_CHARS)}）"
        return cause, advice
    if not error:
        return _FALLBACK_CAUSE, "稍后再发一次试试"
    return _cut(error, _CAUSE_CHARS), ""


def failure_cause(result: Mapping[str, Any] | None) -> str:
    return _cause_and_advice(result)[0].rstrip("。")


def _from_user_message(task: Task) -> bool:
    """这一轮是用户发来的一条消息起的：不是交回的委派结果、自动续跑，也不是定时任务。"""
    return not str(task.input.get("inbound_message_type") or "").strip() and not str(task.input.get("schedule_id") or "").strip()


def failure_reply(task: Task, result: Mapping[str, Any] | None) -> str:
    """失败的那一轮发给用户的话：用户发来的那条说没能回复，别的起因说这一轮没做完。"""
    lead = "这条没能回复" if _from_user_message(task) else "这一轮没做完"
    cause, advice = _cause_and_advice(result)
    if advice:
        return f"{lead}：{cause.rstrip('。')}，{advice}。"
    return f"{lead}：{cause}" if cause.endswith(("。", "！", "？", "…")) else f"{lead}：{cause}。"


def _direct_user_round(task: Task) -> bool:
    """直接替用户干的那一轮：不是派出去的子任务和异步委派，也不是系统内部流程。"""
    return not (task.caller_task_id or "").strip() and not task.input.get("_dispatch_origin") and not is_internal_task(task)


def silent_failure(task: Task) -> bool:
    """群里没人叫 bot、它自己插话的那一轮失败了：没人在等它，什么也不发。"""
    return is_ambient_task(task)


def failure_notice(task: Task, outcome: str) -> str:
    """用户那一轮失败时留在历史里的说明；取消、打断、子任务、系统流程和没人叫 bot 的插话都不留，给空串。"""
    if outcome != "fail" or not _direct_user_round(task) or silent_failure(task):
        return ""
    return FAILED_TURN_NOTICE.format(cause=failure_cause(task.result))


def _opens_round(message: Message) -> bool:
    """一轮里用户那边进来的话：不算系统说明，也不算上下文压缩的摘要（它记的是压缩那一刻）。"""
    meta = message.meta if isinstance(message.meta, dict) else {}
    return (
        message.role == "user"
        and bool(message.source_task_id)
        and message.message_type != MessageType.SYSTEM
        and not meta.get("compressed_task_ids")
    )


def with_failure_notice(
    tail: Sequence[Message], notice: str, *, identity: str, task_id: str, node_id: str, now: datetime,
) -> list[Message]:
    """失败那一轮的尾段末尾接上说明。这一轮的输入没写进去时不接：历史里没有悬空的问题，单留一句说明反而看不懂。

    说明的 id 由收尾的 identity 定，收尾重试时按 id 去重，不会接两遍。
    """
    messages = list(tail)
    if not notice or not any(_opens_round(message) for message in messages):
        return messages
    messages.append(Message(
        id=str(uuid.uuid5(uuid.NAMESPACE_OID, f"{identity}:{FAILED_TURN_MARK_KEY}")),
        role="user",
        content=notice,
        message_type=MessageType.SYSTEM,
        created_at=now.isoformat(),
        meta={"message_type": MessageType.SYSTEM, FAILED_TURN_MARK_KEY: True},
        source_node_id=node_id,
        source_task_id=task_id,
    ))
    return messages


def mark_late_merge(
    tail: Sequence[Message], parent: Sequence[Message], *, zone: tzinfo, now: datetime,
) -> list[Message]:
    """比这一轮晚开始的轮已经先合并进了父会话：在这一轮开头那句输入的 meta 上记下它的起止时间。没有就原样返回。"""
    messages = list(tail)
    index = next((position for position, message in enumerate(messages) if _opens_round(message)), None)
    if index is None:
        return messages
    opener = messages[index]
    started = parse_timestamp(opener.created_at)
    if started is None:
        return messages
    own = {message.source_task_id for message in messages if message.source_task_id}
    overtaken = any(
        _opens_round(message)
        and message.source_task_id not in own
        and (moment := parse_timestamp(message.created_at)) is not None
        and moment > started
        for message in parent
    )
    if overtaken:
        meta = dict(opener.meta) if isinstance(opener.meta, dict) else {}
        meta[LATE_MERGE_KEY] = late_merge_meta(started, now, zone)
        messages[index] = replace(opener, meta=meta)
    return messages
