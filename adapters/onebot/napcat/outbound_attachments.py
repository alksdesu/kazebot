"""附件发送、合并转发发图、已发附件记账与表态。"""
from __future__ import annotations

import logging
import asyncio
import base64
import contextlib
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from ..config import CLONOTH_WORKSPACE, IMAGE_FORWARD_MERGE_NICKNAME
from ..live_config import live
from ..send_contract import (
    IdempotencyClaim,
    OneBotAttachmentBatchError,
    OneBotSendContractError,
    OutboundSendContext,
    classify_send_exception,
    image_content_identity,
    make_idempotency_key,
    protected_claim_send,
)

from . import common, history, identity, media, models, proactive, queueing, routing, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _to_workspace_rel_path(raw_path: Any) -> str:
    """把绝对/任意路径尽量转为工作区相对 posix 路径，与其他附件字段保持一致。

    无法相对化（不在工作区内）时，直接返回原始绝对路径字符串。
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    try:
        p = Path(raw)
        if p.is_absolute():
            return p.relative_to(Path(CLONOTH_WORKSPACE)).as_posix()
        return p.as_posix()
    except Exception:
        return raw


def _resolve_attachment_path(attachment: Any) -> Optional[Path]:
    """把 Clonoth 附件解析为本地路径，兼容 dict 与字符串路径。"""
    # OneBot 适配层同时接受 dict 和 str，并统一解析 file://、绝对路径和工作区相对路径
    if isinstance(attachment, dict):
        raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    elif isinstance(attachment, str):
        raw_path = attachment
    else:
        raw_path = None
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


def _attachment_filename(attachment: Any) -> str:
    """从附件对象中提取展示文件名。"""
    if isinstance(attachment, dict):
        return str(attachment.get("name") or attachment.get("filename") or "")
    if isinstance(attachment, str):
        text = attachment[7:] if attachment.startswith("file://") else attachment
        return Path(text).name
    return ""


async def _send_attachment_path(
    bot: Bot,
    target: Dict[str, Any],
    path: Path,
    filename: str = "",
    *,
    send_context: OutboundSendContext | None = None,
) -> str:
    """Send one attachment under one persistent logical owner claim."""
    await state._adapter.features.check_send(bot, target)
    display_name = filename or path.name
    if not path.exists():
        raise OneBotSendContractError(f"attachment does not exist: {path}")
    raw_bytes = path.read_bytes()
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )

    if path.suffix.lower() in common._IMAGE_SUFFIXES:
        identity = image_content_identity(raw_bytes)
        context = context.child(identity)
        key = make_idempotency_key(
            target, identity, event_id=context.idempotency_key or context.event_id,
        )
        claim = await state._adapter.outbound_idempotency.begin(key)
        if not claim.acquired and claim.state == "pending":
            resolved = await state._adapter.outbound_idempotency.wait_for_resolution(key)
            claim = (
                await state._adapter.outbound_idempotency.begin(key)
                if resolved is None else IdempotencyClaim(key, False, resolved)
            )
        if not claim.acquired:
            return f"idempotent:{key}"
        heartbeat = asyncio.create_task(sending._heartbeat_idempotency_claim(claim))
        errors: list[BaseException] = []
        file_path = str(path.resolve())
        encoded = "base64://" + base64.b64encode(raw_bytes).decode("ascii")
        attempts = (
            MessageSegment.image(file=file_path),
            MessageSegment.image(file=encoded),
            MessageSegment.image(file=f"file://{file_path}"),
        )
        fields = sending._send_audit_fields(
            context, platform_message_id="", attempt=context.attempt,
            idempotency_key=key,
        )
        try:
            async def send_image() -> str:
                for index, message in enumerate(attempts, start=1):
                    try:
                        return await sending._send_qq_message_once(
                            bot, target, message, idempotency_key=key,
                        )
                    except Exception as exc:
                        if getattr(exc, "deferred", False):
                            raise
                        classified = classify_send_exception(exc)
                        if classified.ambiguous_ack:
                            raise classified from exc
                        errors.append(classified)
                failure = OneBotAttachmentBatchError(errors)
                common.logger.warning(
                    "onebot_send_failed",
                    extra={**fields, "retryable": failure.retryable, "send_error": str(failure)},
                )
                raise failure

            message_id = await protected_claim_send(
                state._adapter.outbound_idempotency,
                claim,
                send_image,
                sent_ttl=sending._sent_ttl_for_context(context),
                message_id_getter=lambda result: str(result or ""),
            )
            common.logger.info(
                "onebot_send_sent",
                extra=sending._send_audit_fields(
                    context, platform_message_id=message_id,
                    attempt=context.attempt, idempotency_key=key,
                ),
            )
            return message_id
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    identity = f"file:sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    context = context.child(identity)
    key = make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._adapter.outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._adapter.outbound_idempotency.wait_for_resolution(key)
        claim = (
            await state._adapter.outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return f"idempotent:{key}"
    heartbeat = asyncio.create_task(sending._heartbeat_idempotency_claim(claim))
    fields = sending._send_audit_fields(
        context, platform_message_id="", attempt=context.attempt,
        idempotency_key=key,
    )
    try:
        file_str = "base64://" + base64.b64encode(raw_bytes).decode("ascii")

        async def upload_file() -> Any:
            await state._adapter.features.check_send(bot, target)
            if target.get("type") == "group" and target.get("group_id") is not None:
                return await bot.call_api(
                    "upload_group_file", group_id=target["group_id"],
                    file=file_str, name=display_name,
                )
            if target.get("type") == "private" and target.get("user_id") is not None:
                return await bot.call_api(
                    "upload_private_file", user_id=target["user_id"],
                    file=file_str, name=display_name,
                )
            raise OneBotSendContractError(f"attachment target is invalid: {target!r}")

        result = await protected_claim_send(
            state._adapter.outbound_idempotency,
            claim,
            upload_file,
            sent_ttl=sending._sent_ttl_for_context(context),
            message_id_getter=sending._extract_sent_message_id,
        )
        message_id = sending._extract_sent_message_id(result)
        common.logger.info(
            "onebot_file_upload_sent",
            extra=sending._send_audit_fields(
                context, platform_message_id=message_id,
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        return message_id or f"uploaded:{key}"
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def _target_forward_kind(target: Dict[str, Any]) -> tuple[str, int] | None:
    """从发送 target 解析出合并转发所需的 (类型, id)。无法解析时返回 None。"""
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        try:
            return "group", int(target["group_id"])
        except (TypeError, ValueError):
            return None
    if ttype == "private" and target.get("user_id") is not None:
        try:
            return "private", int(target["user_id"])
        except (TypeError, ValueError):
            return None
    return None


async def _try_send_images_as_forward(
    bot: Bot,
    target: Dict[str, Any],
    image_attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """尝试把多张图片用合并转发（forward node）一次性发送。

    Why: NapCat/NTQQ 逐张 send_group_msg 发大图时，每张都要 base64 上传并
    等 NTQQ ack，单张常超 baseTimeout(10s) 而报 retcode=1200，多图连发还会
    把 WS 心跳拖断（已在日志中观察到 keepalive ping timeout）。
    How: send_group_forward_msg / send_private_forward_msg 只需一次 API 调用，
    把每张图包成一个 node 一次提交。Purpose: 大幅减少往返与逐张超时。

    返回 True 表示已通过合并转发发出；歧义 ack（可能已送达）抛出，交由既有
    ambiguous claim 机制处理；其余失败返回 False，由调用方回退到逐张直发。
    """
    kind = _target_forward_kind(target)
    if kind is None:
        return False
    forward_type, forward_id = kind
    self_id = getattr(bot, "self_id", "") or "10000"
    nodes: list[dict[str, Any]] = []
    for attachment in image_attachments:
        path = _resolve_attachment_path(attachment)
        if path is None or not path.exists():
            continue
        node = {
            "type": "node",
            "data": {
                "user_id": str(self_id),
                "nickname": identity._sanitize_name(IMAGE_FORWARD_MERGE_NICKNAME, max_len=32),
                "content": [
                    {"type": "image", "data": {"file": f"file://{str(path.resolve())}"}},
                ],
            },
        }
        nodes.append(node)
    if len(nodes) < 2:
        return False
    batch_identity = "forward:" + hashlib.sha256(
        "|".join(
            image_content_identity((_resolve_attachment_path(att) or Path()).read_bytes())
            for att in image_attachments
            if _resolve_attachment_path(att) is not None
        ).encode("utf-8")
    ).hexdigest()
    context = send_context.child(batch_identity) if send_context else OutboundSendContext()
    key = make_idempotency_key(
        target, batch_identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._adapter.outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._adapter.outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await state._adapter.outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key, False, resolved)
    if not claim.acquired:
        return True
    heartbeat = asyncio.create_task(sending._heartbeat_idempotency_claim(claim))
    try:
        call_timeout = min(240.0, max(60.0, len(nodes) * 45.0))

        async def send_forward() -> Any:
            await state._adapter.features.check_send(bot, target)
            if forward_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=forward_id, messages=nodes, _timeout=call_timeout,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=forward_id, messages=nodes, _timeout=call_timeout,
            )

        result = await protected_claim_send(
            state._adapter.outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=sending._sent_ttl_for_context(context),
            message_id_getter=sending._extract_sent_message_id,
        )
        message_id = sending._extract_sent_message_id(result)
        common.logger.info(
            "onebot_forward_sent",
            extra=sending._send_audit_fields(
                context, platform_message_id=message_id,
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        if message_id:
            # 合并转发只返回卡片一个 message_id，协议上没有逐张的，整批图都挂在卡片上
            _remember_message_attachments(message_id, list(image_attachments))
        return True
    except Exception as exc:
        classified = classify_send_exception(exc)
        common.logger.warning(
            "onebot_forward_failed",
            extra={
                **sending._send_audit_fields(context, platform_message_id="", attempt=context.attempt, idempotency_key=key),
                "retryable": classified.retryable, "send_error": str(classified),
            },
        )
        # 歧义 ack 可能已经送达，回退逐张会双发；其余失败都是真没发出去
        if classified.ambiguous_ack:
            raise classified from exc
        return False
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_attachments(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """发送 Clonoth 返回的附件列表。

    每发出一个附件，就把返回的 message_id -> 本地附件路径 登记下来，
    让用户引用这条（如绘图产物）时能把原图作为附件送回给 AI。
    """
    # [2026-07-17] 多图合并转发优化：当图片张数 ≥ 阈值时，优先用
    # send_group_forward_msg 一次性发送，避免逐张 sendMsg 超时与 WS 拖断。
    if live.enable_image_forward_merge:
        image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is not None
            and p.exists() and p.suffix.lower() in common._IMAGE_SUFFIXES
        ]
        non_image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is None
            or not p.exists() or p.suffix.lower() not in common._IMAGE_SUFFIXES
        ]
        if len(image_atts) >= live.image_forward_merge_threshold:
            merged = await _try_send_images_as_forward(
                bot, target, image_atts, send_context=send_context,
            )
            if merged:
                # 整批图已登记在卡片 message_id 上；协议上拿不到逐张映射。
                # 非图片附件继续逐个发送。
                if non_image_atts:
                    await _send_attachments_one_by_one(
                        bot, target, non_image_atts, send_context=send_context
                    )
                return
            # 合并转发失败，回退到逐张直发（下方通用循环）。
    await _send_attachments_one_by_one(
        bot, target, attachments, send_context=send_context
    )


async def _send_attachments_one_by_one(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """逐张/逐个发送附件，单个失败不阻断后续。"""
    # [2026-07-17] 批量发图（如一次生 4 张）时，单张 sendMsg 可能耗时较长；
    # 若其中一张抛异常（如 NapCat 超时/WS 短暂断开），旧逻辑会让整个循环
    # 中断，导致后续图片全部丢失（现象：“要 4 张只发出 1 张”）。
    # 这里逐张单独容错，单张失败不阻断后续图片发送。
    total = len(attachments)
    failures: list[BaseException] = []
    for idx, attachment in enumerate(attachments):
        path = _resolve_attachment_path(attachment)
        filename = _attachment_filename(attachment)
        if path is None:
            if filename:
                await sending._send_qq_message(
                    bot, target, f"Clonoth 生成了文件：{filename}",
                    send_context=(send_context.child(f"unsupported:{filename}") if send_context else None),
                )
            else:
                common.logger.warning("skip unsupported QQ attachment payload: %r", attachment)
            continue
        try:
            message_id = await _send_attachment_path(
                bot,
                target,
                path,
                filename=filename,
                send_context=send_context,
            )
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "deferred", False):
                raise
            classified = classify_send_exception(exc)
            failures.append(classified)
            common.logger.warning(
                "onebot_attachment_failed",
                extra={
                    "attachment_index": idx + 1, "attachment_total": total,
                    "attachment_name": filename or path.name,
                    "retryable": classified.retryable,
                    "event_id": (send_context.event_id if send_context else ""),
                    "attempt": (send_context.attempt if send_context else 1),
                    "idempotency_key": (send_context.idempotency_key if send_context else ""),
                },
            )
            continue
        if message_id:
            _remember_message_attachments(message_id, [attachment])
    if failures:
        raise OneBotAttachmentBatchError(failures)


def _sent_attachment_bucket_key(target: Dict[str, Any]) -> str:
    """为最近发送附件索引生成稳定的会话桶 key。

    优先用 group_id/user_id（Bot 进程内部值），避免 real/stable conversation_key
    不一致导致记录与查询对不上。
    """
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        return f"group:{int(target['group_id'])}"
    if ttype == "private" and target.get("user_id") is not None:
        return f"private:{int(target['user_id'])}"
    conv_key = str(target.get("conversation_key") or "")
    return f"conv:{conv_key}" if conv_key else ""


def _record_sent_attachments(target: Dict[str, Any], attachments: List[Any]) -> None:
    """把 Bot 发出/生成的附件记入会话级最近附件索引，供 qq_forward 后续检索。

    Why: 生图插件产出的图片文件名随机，用户说“把刚才那张图发给 xx”时
    AI 无从得知具体路径。How: 发送时把已解析的本地路径 + 显示名 + 时间戳
    按会话桶缓存。Purpose: 让后续转发能按“最近生成/发送”定位图片。
    """
    bucket = _sent_attachment_bucket_key(target)
    if not bucket or not attachments:
        return
    now = time.time()
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in common._IMAGE_SUFFIXES
        seq = state._adapter.sent_attachment_seq[bucket] + 1
        state._adapter.sent_attachment_seq[bucket] = seq
        state._adapter.recent_sent_attachments[bucket].append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
            "timestamp": now,
            "seq": seq,
        })
    state._adapter.recent_sent_attachments.touch(bucket)


def _remember_message_attachments(message_id: Any, attachments: List[Any]) -> None:
    """登记 Bot 已发出消息的 message_id -> 本地附件路径列表。

    仅记录能解析出本地路径的附件（图片/文件），供用户引用这条消息时回取原图。
    """
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    records: List[Dict[str, Any]] = []
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in common._IMAGE_SUFFIXES
        records.append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
        })
    if not records:
        return
    state._adapter.sent_message_attachments[mid] = records
    state._adapter.sent_message_attachments.move_to_end(mid)
    while len(state._adapter.sent_message_attachments) > models._MESSAGE_ATTACHMENT_MAX_ITEMS:
        state._adapter.sent_message_attachments.popitem(last=False)


def _message_attachment_records(message_id: Any, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回某条 Bot 已发消息登记的本地附件记录（过滤不存在的文件）。"""
    mid = str(message_id or "").strip()
    if not mid:
        return []
    records: list[dict[str, Any]] = []
    for item in state._adapter.sent_message_attachments.get(mid, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    media._touch_attachment_files(records)
    return records


def _recent_sent_attachment_records(bucket_key: str, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回会话最近发出/生成的附件（旧→新），可选只要图片，并过滤不存在的文件。"""
    bucket = str(bucket_key or "")
    if not bucket:
        return []
    records: list[dict[str, Any]] = []
    for item in state._adapter.recent_sent_attachments.get(bucket, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    media._touch_attachment_files(records)
    return records


# 附件按会话串行发送；锁表按会话桶增长，需要有界。
_ATTACHMENT_SEND_LOCK_MAX_KEYS = 256


@contextlib.asynccontextmanager
async def _attachment_send_order(target: Dict[str, Any]) -> AsyncIterator[None]:
    """按会话桶串行化附件发送，退出时回收空闲锁。"""
    key = _sent_attachment_bucket_key(target) or str(target.get("conversation_key") or "default")
    entry = state._adapter.attachment_send_locks.get(key)
    if entry is None:
        entry = models._AttachmentSendLock(asyncio.Lock())
        state._adapter.attachment_send_locks[key] = entry
    state._adapter.attachment_send_locks.move_to_end(key)
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        _prune_attachment_send_locks()


def _prune_attachment_send_locks() -> None:
    """只淘汰没人持有/等待的锁：丢掉在用的锁会让后来者拿到新锁，同会话串行就断了。"""
    overflow = len(state._adapter.attachment_send_locks) - _ATTACHMENT_SEND_LOCK_MAX_KEYS
    for stale_key, entry in list(state._adapter.attachment_send_locks.items()):
        if overflow <= 0:
            break
        if entry.users:
            continue
        del state._adapter.attachment_send_locks[stale_key]
        overflow -= 1


def _ensure_bucket_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 target 的 group_id/user_id，使最近附件登记 bucket 稳定可查。

    [2026-07-19] 生图（gpt_image_2 / gemini_image）经 emit_intermediate 后台
    推图时，走 send_to_channel fallback，其 target 可能只有 conversation_key
    而缺 group_id/user_id。若不补齐，_sent_attachment_bucket_key 会退化成
    conv:<key> 桶，而 qq_forward op=recent 用 group:<群号>/private:<qq> 桶查询，
    两者对不上导致查不到刚生成的图。这里从 conversation_key 反解出
    group_id/user_id 补进 target，保证登记与查询命中同一个桶。
    """
    ttype = str(target.get("type") or "")
    conv_key = routing._real_conversation_key(str(target.get("conversation_key") or ""))
    if ttype == "group" and target.get("group_id") is None and conv_key:
        gid = queueing._group_id_from_conversation_key(conv_key)
        if gid is not None:
            target["group_id"] = gid
    if ttype == "private" and target.get("user_id") is None and conv_key:
        uid = queueing._private_user_id_from_conversation_key(conv_key)
        if uid is not None:
            target["user_id"] = uid
    return target


async def _send_attachments_confirmed(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """串行发送附件，并仅在全部平台确认后登记最近附件索引。"""
    try:
        async with _attachment_send_order(target):
            await _send_attachments(
                bot, target, attachments, send_context=send_context
            )
            # Register/finish only after every platform-visible send is confirmed.
            # 清空上下文之前派出去的那轮除外：图照发，但索引不该留在刚清空的会话里。
            if not history._is_stale_delivery(target.get("group_id"), send_context):
                _record_sent_attachments(_ensure_bucket_target(dict(target)), attachments)
            await state._adapter.features.register_sent_attachments(bot, target, attachments, send_context)
    except Exception:
        common.logger.warning("onebot_attachment_batch_failed", exc_info=True)
        raise


async def _send_text_and_attachments(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    attachments: List[Any],
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> None:
    """统一发送最终文本/附件，并持久绑定回复所依据的来源图片。"""
    await state._adapter.features.check_send(bot, target)
    # 上下文被清空时这一轮还在飞，回复照发（群友问了总得有个答复），但一律不落缓存 ——
    # 它是上一段上下文的产物，落回去下一轮又会被带给模型，等于那次清空没生效。
    stale = target.get("type") == "group" and history._is_stale_delivery(target.get("group_id"), send_context)
    if stale:
        common.logger.info("skip context write-back for group %s: delivery predates the reset", target.get("group_id"))
    # 2026-05-01 修改原因：私聊没有群历史缓存，不应写入 _group_history；群聊
    # 仍保留原来的 Bot 回复入库逻辑，维持后续 @Bot 请求的上下文连续性。
    if text:
        if await sending._send_split_text(
            bot,
            target,
            text,
            source_attachments=source_attachments,
            send_context=send_context,
        ) and target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None and not stale:
                history._record_bot_reply(int(group_id), text)
    if attachments:
        # Delivery completion means the platform-confirmed final attachment send,
        # not merely scheduling a background task.  Failures propagate to the SDK
        # so its outbox cannot acknowledge/consume the trigger prematurely.
        await _send_attachments_confirmed(
            bot, target, attachments, send_context=send_context
        )
        return


async def _set_message_react(bot: Bot, event: Any, emoji_id: str, enabled: bool) -> bool:
    """设置或移除触发消息上的 QQ React，失败时静默返回 False。"""
    # 2026-05-03 修改原因：React 阶段切换需要多处复用 OneBot 扩展 API。
    # 做法是把单次 set_msg_emoji_like 调用包进独立函数，并强制 emoji_id 为 str；
    # 目的在于满足每次 API 调用都单独容错，避免 React 失败影响消息回复。
    if not live.enable_reactions or not bot or not event or not hasattr(event, "message_id"):
        return False
    try:
        if enabled and getattr(event, "group_id", None) is not None:
            metadata = await state._adapter.features.prepare(bot, event)
            await state._adapter.features.check_send(bot, {"type": "group", "group_id": event.group_id,
                                            "_response_purpose": "direct" if metadata.get("direct") else "ambient"})
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=int(event.message_id),
            emoji_id=str(emoji_id),
            set=enabled,
        )
        return True
    except Exception:
        return False


def _progress_mentions_search(record: str) -> bool:
    """判断进度记录是否与联网搜索工具相关。"""
    text = str(record or "").lower()
    return any(keyword in text for keyword in common._SEARCH_PROGRESS_KEYWORDS)


async def _maybe_send_search_progress_notice(
    bot: Bot,
    target: Dict[str, Any],
    platform_data: Dict[str, Any],
) -> None:
    """在 QQ 侧为长搜索任务发送低频可见进度提示。至多两条：开工一条，久等一条。"""
    now = time.time()
    # 答案已经在投递路上就闭嘴：两者同一秒并发时，「还在搜索」会紧贴着结果出现。
    if platform_data.get("_qq_final_reply_sent"):
        return
    request_identity = str(
        platform_data.setdefault("_qq_search_request_identity", uuid.uuid4().hex)
    )
    context = proactive._request_send_context(
        "search-progress", request_identity,
        str(target.get("conversation_key") or ""),
    )
    if not platform_data.get("_qq_search_notice_sent"):
        platform_data["_qq_search_notice_sent"] = True
        platform_data["_qq_search_notice_started_at"] = now
        try:
            await sending._send_qq_message(
                bot, target, common._SEARCH_PROGRESS_FIRST_NOTICE,
                send_context=context.child("first"),
            )
        except Exception:
            common.logger.debug("send QQ search progress notice failed", exc_info=True)
        return

    if platform_data.get("_qq_search_still_notice_sent"):
        return
    started_at = float(platform_data.get("_qq_search_notice_started_at") or now)
    waited = now - started_at
    if waited < common._SEARCH_PROGRESS_STILL_RUNNING_DELAY_SEC:
        return
    platform_data["_qq_search_still_notice_sent"] = True
    try:
        await sending._send_qq_message(
            bot, target,
            common._SEARCH_PROGRESS_STILL_RUNNING_NOTICE.format(waited=int(waited)),
            send_context=context.child("still"),
        )
    except Exception:
        common.logger.debug("send QQ search still-running notice failed", exc_info=True)


def _apply_delivery_context(target: Dict[str, Any], context: OutboundSendContext) -> None:
    purpose = getattr(context, "purpose", "direct")
    if purpose == "ambient" or "_response_purpose" not in target:
        target["_response_purpose"] = purpose
    reply_ref = str(getattr(context, "reply_message_id", "") or "")
    if reply_ref and reply_ref.lstrip("-").isdigit():
        target["reply_message_id"] = int(reply_ref)
    if getattr(context, "topic_id", ""):
        target["_topic_id"] = context.topic_id
