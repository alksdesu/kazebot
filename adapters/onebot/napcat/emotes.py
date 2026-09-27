"""表情包库与收藏表情：收集、取图、接梗、摆给模型的清单。"""
from __future__ import annotations

import logging
import asyncio
import base64
import contextlib
import hashlib
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from platforms.ids import normalize_id
from platforms.inbound import InboundFacts
from ..config import CLONOTH_WORKSPACE, CUSTOM_FACE_METADATA_PATH, CUSTOM_FACE_NAMES_PATH
from ..live_config import live
from adapters._shared.send_guard import OutboundSendContext
from ..emoji_handler import (
    ResolvedSticker,
    extract_named_custom_face_metadata,
    extract_named_custom_face_names,
    load_custom_face_metadata,
    load_custom_face_names,
    write_custom_face_metadata,
    write_custom_face_names,
)
from stickers.collect import CollectConfig, StickerCollector
from stickers.combat import CombatConfig
from stickers.collect import sticker_root, store_path as sticker_store_path
from stickers.rank import rank
from stickers.store import Sticker, StickerStore
from stickers.tagger import StickerTagger, TaggerConfig

from . import anonymize, common, media, outbound_attachments, routing, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _load_custom_face_names_file() -> List[str]:
    """读取 AI 可见收藏表情名称文件。"""
    return load_custom_face_names(CUSTOM_FACE_NAMES_PATH)


def _load_custom_face_metadata_file() -> List[Dict[str, Any]]:
    """读取程序内部使用的收藏表情元数据文件（md5/resId/emojiId/url 等）。"""
    return load_custom_face_metadata(CUSTOM_FACE_METADATA_PATH)


def _current_custom_face_names() -> List[str]:
    """读取当前 AI 可见表情名，并同步进内存缓存。手动编辑文件后无需重启。"""
    state._adapter.custom_face_names = _load_custom_face_names_file()
    return list(state._adapter.custom_face_names)


def _current_custom_face_metadata() -> List[Dict[str, Any]]:
    """读取当前收藏表情内部元数据，并同步进内存缓存。"""
    state._adapter.custom_face_metadata = _load_custom_face_metadata_file()
    return list(state._adapter.custom_face_metadata)


def _sticker_store_handle() -> Optional[StickerStore]:
    """惰性开库。开不了就当没这个功能，不能连累消息处理。"""
    if state._adapter.sticker_store is None:
        try:
            state._adapter.sticker_store = StickerStore(sticker_store_path(Path(CLONOTH_WORKSPACE)))
        except Exception:
            common.logger.warning("表情包库打不开，相关功能停用", exc_info=True)
            return None
    return state._adapter.sticker_store


def _sticker_collect_config() -> CollectConfig:
    return CollectConfig(
        enabled=bool(live.sticker_collect),
        strategy=str(live.sticker_strategy),
        groups=tuple(normalize_id(group_id) for group_id in live.sticker_groups),
        auto_accept=bool(live.sticker_auto_accept),
        pending_limit=int(live.sticker_pending_limit),
        library_limit=int(live.sticker_library_limit),
        pending_ttl_sec=int(live.sticker_pending_ttl_days) * 86400,
        max_bytes=int(live.image_max_bytes),
    )


def _sticker_collector_handle() -> Optional[StickerCollector]:
    if state._adapter.sticker_collector is None:
        state._adapter.sticker_collector = StickerCollector(
            Path(CLONOTH_WORKSPACE),
            config=_sticker_collect_config,
            open_store=_sticker_store_handle,
        )
    return state._adapter.sticker_collector


def _collect_stickers(facts: InboundFacts) -> None:
    """把这条消息里的图丢进收集队列。只入队，落盘和查库都在后台。"""
    if not facts.attachments or not live.sticker_collect:
        return
    collector = _sticker_collector_handle()
    if collector is None:
        return
    try:
        collector.submit(
            facts.attachments,
            group_id=facts.conversation.raw_id,
            # 存化名而非真实 QQ 号：这张图会长期留在共享库里。
            user_key=facts.sender.alias,
            marked=facts.marked_sticker,
        )
    except Exception:
        common.logger.debug("表情包入队失败", exc_info=True)


# 超过这个大小就不发了：base64 会再胀三分之一，而表情包本来就该是小图。
_STICKER_SEND_MAX_BYTES = 3 * 1024 * 1024


def _sticker_conversation_key(target: Dict[str, Any], bot: Bot | None = None) -> str:
    kind = outbound_attachments._target_forward_kind(target)
    if kind is None:
        return ""
    prefix = "qq_group" if kind[0] == "group" else "qq_private"
    scope = state._adapter.active_account_store.normalize(getattr(bot, "self_id", None))
    if scope:
        return anonymize.CONVERSATION_KEYS.stable_key(f"{prefix}:{kind[1]}", bot_scope=scope)
    return routing._stable_conversation_key(f"{prefix}:{kind[1]}")


def _sticker_file_path(row: Sticker) -> Path | None:
    if not row.usable or not row.rel_path:
        return None
    try:
        root = sticker_root(Path(CLONOTH_WORKSPACE)).resolve()
        path = (Path(CLONOTH_WORKSPACE) / row.rel_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path if 0 < path.stat().st_size <= _STICKER_SEND_MAX_BYTES else None
    except (OSError, RuntimeError, ValueError):
        return None


def _read_sticker(row: Sticker) -> bytes:
    path = _sticker_file_path(row)
    if path is None:
        return b""
    try:
        with path.open("rb") as source:
            raw = source.read(_STICKER_SEND_MAX_BYTES + 1)
        return raw if 0 < len(raw) <= _STICKER_SEND_MAX_BYTES else b""
    except OSError:
        return b""


@contextlib.asynccontextmanager
async def _serialize_sticker_delivery(conversation_key: str) -> AsyncIterator[None]:
    lock, users = state._adapter.sticker_delivery_locks.get(conversation_key, (asyncio.Lock(), 0))
    state._adapter.sticker_delivery_locks[conversation_key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = state._adapter.sticker_delivery_locks[conversation_key]
        if users == 1:
            del state._adapter.sticker_delivery_locks[conversation_key]
        else:
            state._adapter.sticker_delivery_locks[conversation_key] = (lock, users - 1)


async def _reconcile_sticker_receipts(delivery_id: str = "") -> bool:
    try:
        receipts = await state._adapter.outbound_idempotency.pending_message_receipts(
            limit=100, delivery_id=delivery_id,
        )
        if not receipts:
            return True
        for receipt in receipts:
            store = _sticker_store_handle() if receipt.selected_images else None
            if receipt.selected_images and store is None:
                return False
            for digest in receipt.selected_images:
                await asyncio.to_thread(
                    store.record_sent, receipt.conversation_key, digest,
                    delivery_id=receipt.delivery_id, sent_at=int(receipt.confirmed_at),
                )
            await state._adapter.outbound_idempotency.ack_message_receipt(receipt.delivery_id)
        return not await state._adapter.outbound_idempotency.pending_message_receipts(
            limit=1, delivery_id=delivery_id,
        )
    except Exception:
        common.logger.warning("表情包发送回执记账失败，将稍后重试", exc_info=True)
        return False


async def _sticker_receipts_forever() -> None:
    while True:
        await _reconcile_sticker_receipts()
        await asyncio.sleep(10)


async def _resolve_sticker(name: str) -> ResolvedSticker | str:
    store = _sticker_store_handle()
    if store is None:
        return ""
    try:
        if not await _reconcile_sticker_receipts():
            return ""
        key = state._adapter.sticker_send_conversation.get("")
        skip = store.recently_sent(
            key, within_sec=int(live.sticker_repeat_window_sec),
        ) if key else set()
        selected = state._adapter.sticker_selected.get()
        if selected is not None:
            skip.update(selected)
        row = store.by_name(name)
        while True:
            if row is not None and row.sha256 not in skip:
                skip.add(row.sha256)
                raw = await asyncio.to_thread(_read_sticker, row)
                if raw:
                    if selected is not None:
                        selected.add(row.sha256)
                    return ResolvedSticker(
                        "base64://" + base64.b64encode(raw).decode("ascii"), row.sha256,
                    )
            row = store.by_tag(name, exclude=skip)
            if row is None:
                return ""
    except Exception:
        common.logger.warning("表情包取图失败: %s", name, exc_info=True)
        return ""


def _sticker_combat_config() -> CombatConfig:
    return CombatConfig(
        enabled=bool(live.sticker_combat),
        burst_probability=float(live.sticker_burst_probability),
    )


async def _maybe_join_sticker_combat(
    bot: Bot, event: GroupMessageEvent, *, has_image: bool, text: str,
) -> None:
    """群里连着刷图时跟一张。

    语境取群历史里最近的文本，取不到就不发 —— 为了接一张图再调一次多模态不划算，
    而没有语境的随机发图看着就是个乱按键的机器人。
    """
    config = _sticker_combat_config()
    if not config.enabled:
        return
    group = str(event.group_id)
    now = time.monotonic()
    if has_image:
        state._adapter.sticker_combat.on_image(group, user=str(event.user_id), now=now, config=config)
    else:
        if text.strip():
            state._adapter.sticker_combat.on_text(group, now=now)
        return
    if not state._adapter.sticker_combat.should_battle(group, now=now, config=config):
        return

    conversation_key = _sticker_conversation_key({"type": "group", "group_id": int(event.group_id)}, bot)
    async with _serialize_sticker_delivery(conversation_key):
        if not state._adapter.sticker_combat.should_battle(group, now=time.monotonic(), config=config):
            return
        history = list(state._adapter.group_history[int(event.group_id)])[-6:]
        context_text = " ".join(entry.text for entry in history if entry.text)
        target = {"type": "group", "group_id": int(event.group_id), "_response_purpose": "ambient"}
        message_id = str(getattr(event, "message_id", "") or "")
        context = OutboundSendContext(
            event_id=f"combat:{message_id}" if message_id else "",
            conversation_key=conversation_key, purpose="ambient",
        )
        selected_token = state._adapter.sticker_selected.set(set())
        try:
            if not await _send_combat_sticker(bot, target, context_text, context, 0):
                return
            sent_at = time.monotonic()
            state._adapter.sticker_combat.mark_battled(group, now=sent_at)
            state._adapter.sticker_combat.on_self_send(group, now=sent_at)
            if not state._adapter.sticker_combat.should_burst(
                group, now=sent_at, config=config, roll=random.random(),
            ):
                return
            await asyncio.sleep(0.9)
            if await _send_combat_sticker(bot, target, context_text, context, 1):
                state._adapter.sticker_combat.mark_burst(group, now=time.monotonic())
        finally:
            state._adapter.sticker_selected.reset(selected_token)


async def _send_combat_sticker(
    bot: Bot, target: Dict[str, Any], context_text: str,
    context: OutboundSendContext, index: int,
) -> str:
    async def build_plan() -> Dict[str, Any]:
        sticker = await _pick_combat_sticker(context_text, context.conversation_key)
        segments = [{
            "type": "image", "url": sticker.url, "emoji": True,
            "sticker_sha256": sticker.sha256,
        }] if isinstance(sticker, ResolvedSticker) else []
        return {
            "processed_segments": sending._freeze_sticker_segments(segments),
            "send_identity": f"combat:{index}",
            "selected_images": [sticker.sha256] if segments else [],
            "conversation_key": context.conversation_key,
        }

    try:
        return await sending._send_planned_sticker_message(
            bot, target, f"combat:{index}" if context.event_id else f"combat:{index}:{context_text}",
            build_plan, send_context=context,
        )
    except Exception:
        common.logger.warning("表情包接梗发送失败", exc_info=True)
        return ""


async def _pick_combat_sticker(context_text: str, conversation_key: str) -> ResolvedSticker | str:
    """按语境挑一张，挑不出就返回空串。"""
    store = _sticker_store_handle()
    if store is None or not context_text.strip():
        return ""
    try:
        usable = [row for row in store.all_usable() if _sticker_file_path(row) is not None]
        if not usable:
            return ""
        recent = store.recently_sent(
            conversation_key, within_sec=int(live.sticker_repeat_window_sec),
        )
        recent.update(state._adapter.sticker_selected.get() or ())
        result = rank(context_text, usable, recently_sent=recent, limit=len(usable))
        # generic 是闲聊闸门：语境不明确时宁可不发。
        if result.generic or result.best is None:
            return ""
        token = state._adapter.sticker_send_conversation.set(conversation_key)
        try:
            for candidate in result.items:
                sticker = await _resolve_sticker(candidate.name)
                if sticker:
                    return sticker
            return ""
        finally:
            state._adapter.sticker_send_conversation.reset(token)
    except Exception:
        common.logger.debug("表情包接梗选图失败", exc_info=True)
        return ""


def _start_sticker_tagger() -> None:
    """图库没启用也照跑：手动传的图同样要打标，只是平时没活会一直空转睡着。

    每轮循环都重新读一次开关，所以关掉之后正在跑的这一批做完就停，不用重启。
    """
    if state._adapter.sticker_tagger is None:
        state._adapter.sticker_tagger = StickerTagger(
            Path(CLONOTH_WORKSPACE),
            config=lambda: TaggerConfig(enabled=bool(live.sticker_auto_tag)),
            open_store=_sticker_store_handle,
        )
    state._adapter.sticker_tagger.start()


async def _stop_stickers() -> None:
    if state._adapter.sticker_tagger is not None:
        await state._adapter.sticker_tagger.stop()
        state._adapter.sticker_tagger = None
    if state._adapter.sticker_collector is not None:
        await state._adapter.sticker_collector.stop()
        state._adapter.sticker_collector = None
    if state._adapter.sticker_store is not None:
        state._adapter.sticker_store.close()
        state._adapter.sticker_store = None


def _sticker_prompt_entries(conversation_key: str) -> list[str]:
    """这一轮摆给模型看的表情包清单。

    不按群友那句话预筛。表情包配的是 bot 自己要回的语气，而候选是在模型开口之前
    就得定下来的 —— 拿对方说的话去筛，筛出来的情绪往往正好是反的。清单直接摊开，
    让唯一同时知道「对方说了什么」和「我要回什么」的角色去挑。
    """
    limit = int(live.sticker_prompt_limit)
    if limit <= 0 or random.random() >= float(live.sticker_send_probability):
        return []
    store = _sticker_store_handle()
    if store is None:
        return []
    try:
        recent = store.recently_sent(
            conversation_key, within_sec=int(live.sticker_repeat_window_sec),
        )
        usable = [
            row for row in store.all_usable()
            if row.sha256 not in recent and _sticker_file_path(row) is not None
        ]
        # 装不下就让发得最少的先上，轮着来；否则冷门图永远排在字典序后面没人见过。
        usable.sort(key=lambda row: (row.sent_count, row.name))
        return [
            f"{row.name}（{'、'.join(row.tags[:8]) or '无标签'}）"
            for row in usable[:limit]
        ]
    except Exception:
        common.logger.debug("表情包清单读取失败", exc_info=True)
        return []


async def _sync_custom_face_names_file(bot: Bot) -> List[str]:
    """从 NapCat 收藏表情详情同步已命名表情到 AI 名称文件和内部元数据文件。

    AI 名称文件只写去重后的基础名；内部元数据保留全部同名项（带 (1)/(2) 后缀）。
    """
    faces = await state._adapter.emoji.fetch_custom_face_details(bot, force=True)
    metadata = extract_named_custom_face_metadata(faces, state._adapter.bqbs)
    names = extract_named_custom_face_names(faces, state._adapter.bqbs)
    write_custom_face_names(CUSTOM_FACE_NAMES_PATH, names)
    write_custom_face_metadata(CUSTOM_FACE_METADATA_PATH, metadata)
    state._adapter.custom_face_names = names
    state._adapter.custom_face_metadata = metadata
    return names


def _custom_face_value(face: Dict[str, Any], *keys: str) -> Any:
    """按多个可能字段名取收藏表情字段；保留 0 这类合法值。"""
    if not isinstance(face, dict):
        return None
    for key in keys:
        if key not in face:
            continue
        value = face.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


async def _set_custom_face_desc(bot: Bot, face: Dict[str, Any], desc: str) -> tuple[bool, str]:
    """调用 NapCat set_custom_face_desc 给已有收藏表情设置描述。"""
    emoji_id = _custom_face_value(face, "emojiId", "emoji_id", "emoId", "emoid")
    res_id = _custom_face_value(face, "resId", "res_id", "id")
    md5 = _custom_face_value(face, "md5", "MD5")
    if emoji_id is None or not res_id or not md5:
        missing = []
        if emoji_id is None:
            missing.append("emoji_id")
        if not res_id:
            missing.append("res_id")
        if not md5:
            missing.append("md5")
        return False, "找到了该表情，但缺少重命名所需字段：" + "、".join(missing)
    try:
        await bot.call_api(
            "set_custom_face_desc",
            emoji_id=emoji_id,
            res_id=str(res_id),
            md5=str(md5),
            desc=desc,
        )
    except Exception as exc:
        common.logger.warning("set_custom_face_desc failed: %s", exc, exc_info=True)
        return False, "重命名收藏表情失败：当前 OneBot 实现可能不支持 set_custom_face_desc，或该表情资源信息已失效。"
    state._adapter.emoji.invalidate_custom_face_cache(bot)
    try:
        await _sync_custom_face_names_file(bot)
    except Exception:
        common.logger.warning("sync custom face names after rename failed", exc_info=True)
    return True, f"已将收藏表情重命名为：{desc}\n之后模型可用 [表情:{desc}] 调用。"


async def _add_custom_face_from_attachment(bot: Bot, alias: str, attachment: Dict[str, Any]) -> str:
    """调用 NapCat add_custom_face，并尽量把收藏描述设置为 alias。"""
    path = media._attachment_abs_path(attachment)
    if path is None or not path.exists():
        return "找不到要收藏的图片文件，请重新发送图片后再试。"

    content = path.read_bytes()
    md5 = hashlib.md5(content).hexdigest()
    try:
        await bot.call_api(
            "add_custom_face",
            file=str(path),
            md5=md5,
            file_name=path.name,
            is_origin=True,
        )
    except Exception as exc:
        common.logger.warning("add_custom_face failed: %s", exc, exc_info=True)
        return (
            "收藏表情失败：NapCat add_custom_face 调用失败。\n"
            "提示：NapCat 要求 file 是 NapCat 运行环境可访问的本地路径；"
            "如果 NoneBot 和 NapCat 分容器部署，请把 Clonoth data/attachments 挂载到相同路径。"
        )

    state._adapter.emoji.invalidate_custom_face_cache(bot)

    # NapCat 修改描述需要 emoji_id + res_id + md5。add_custom_face 的返回不一定包含这些字段，
    # 因此刷新列表后用 md5 反查新表情，再调用 set_custom_face_desc。
    try:
        await asyncio.sleep(0.8)
        faces = await state._adapter.emoji.fetch_custom_face_details(bot, force=True)
        target_face = None
        for face in faces:
            if isinstance(face, dict) and str(face.get("md5") or "").lower() == md5.lower():
                target_face = face
                break
        if isinstance(target_face, dict):
            emoji_id = _custom_face_value(target_face, "emojiId", "emoji_id", "emoId", "emoid")
            res_id = _custom_face_value(target_face, "resId", "res_id")
            if emoji_id is not None and res_id:
                await bot.call_api(
                    "set_custom_face_desc",
                    emoji_id=emoji_id,
                    res_id=str(res_id),
                    md5=md5,
                    desc=alias,
                )
                state._adapter.emoji.invalidate_custom_face_cache(bot)
                try:
                    await _sync_custom_face_names_file(bot)
                except Exception:
                    common.logger.warning("sync custom face names after add failed", exc_info=True)
    except Exception:
        # 描述设置失败不影响“已收藏”的主体结果，模型仍可用序号/md5/文件名兜底调用。
        common.logger.warning("set_custom_face_desc after add_custom_face failed", exc_info=True)

    return f"已尝试收藏表情：{alias}\n之后模型可用 [表情:{alias}] 调用；如描述设置失败，也可用 /表情列表 查看实际名称。"
