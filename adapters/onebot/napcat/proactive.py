"""主动发送与合并转发：目标解析、作用域夹紧、节点拼装。"""
from __future__ import annotations

import logging
import asyncio
import contextlib
import hashlib
import json
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List
from nonebot.adapters.onebot.v11 import Bot, Event
from ..config import CLONOTH_WORKSPACE
from adapters._shared import capability
from ..live_config import live
from ..send_contract import IdempotencyClaim, OutboundSendContext, make_idempotency_key, protected_claim_send

from . import anonymize, common, identity, media, message_text, models, outbound_attachments, permissions, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


_PROACTIVE_PRIVATE_KINDS = frozenset({"私聊", "好友", "private", "pm", "user"})


_PROACTIVE_GROUP_KINDS = frozenset({"群聊", "群", "group"})


_PROACTIVE_HELP_WORDS = frozenset({"主动发送帮助", "主动转发帮助", "通知帮助", "转发帮助"})


_PROACTIVE_LIST_WORDS = frozenset({"主动目标", "通知目标", "转发目标", "目标列表"})


_PROACTIVE_PRIVATE_WORDS = frozenset({"私信", "私聊通知"})


_PROACTIVE_GROUP_WORDS = frozenset({"群发", "群通知"})


_PROACTIVE_FILE_WORDS = frozenset({"发文件", "发送文件"})


# 英文别名并进同一个集合。加了斜杠之后中英已经对等，再单开 startswith 分支
# 就是第三份逐字相同的代码。
_PROACTIVE_SEND_WORDS = frozenset({"发送", "通知", "主动发送", "send"})


_PROACTIVE_FORWARD_WORDS = frozenset({"合并转发", "转发", "转发到", "转发给", "合并转发到", "合并转发给", "forward"})


def _normalize_target_ref(text: str) -> str:
    """把管理员输入的目标名规整为可匹配 token，不写入模型上下文。"""
    return re.sub(r"[\s_\-—]+", "", str(text or "").strip().lower())


def _proactive_help_text() -> str:
    return (
        "【管理员主动发送 / 转发命令】\n"
        "权限：仅 CLONOTH_ADMIN_QQ_USERS 中的管理员可用；命令在 QQ 适配器本地处理，不进入普通模型上下文。\n"
        "目标：使用联系人显示名/好友备注/群名/配置别名；目标列表不会展示真实 QQ 号。\n\n"
        "1) 查看可用目标\n"
        "   /主动目标 或 /主动目标 私聊 或 /主动目标 群\n"
        "2) 主动发文本（可在同条消息附图，图片会一起发送）\n"
        "   /私信 <联系人名> <内容>\n"
        "   /群发 <群名> <内容>\n"
        "   /发送 私聊 <联系人名> <内容>\n"
        "   /发送 群 <群名> <内容>\n"
        "3) 主动发本地文件（路径限制在 Clonoth 工作区内，推荐 data/attachments/）\n"
        "   /发文件 私聊 <联系人名> data/attachments/xxx.png\n"
        "   /发文件 群 <群名> data/attachments/xxx.zip 展示文件名.zip\n"
        "4) 合并转发\n"
        "   /合并转发 私聊 <联系人名> <内容>\n"
        "   /合并转发 群 <群名> <内容>\n"
        "   也可以引用一条消息/合并转发卡片后发送：/合并转发 群 <群名>\n"
        "   文本中用单独一行 --- 可拆成多条转发 node。"
    )


def _parse_proactive_command(text: str) -> dict[str, str] | None:
    """解析管理员主动发送/转发命令。

    返回字段：action(send/file/forward/list/help), target_type(private/group), target_ref, body。
    目标名按单个 token 解析；如群名含空格，请在配置里设置无空格别名/显示名。
    """
    raw = common._strip_command_prefix(text)
    if not raw:
        return None
    if raw in _PROACTIVE_HELP_WORDS:
        return {"action": "help"}
    parts = raw.split()
    if not parts:
        return None
    head = parts[0]
    head_l = head.lower()
    if head in _PROACTIVE_LIST_WORDS:
        kind = parts[1] if len(parts) >= 2 else ""
        return {"action": "list", "target_type": _canonical_proactive_target_type(kind)}
    if head in _PROACTIVE_PRIVATE_WORDS and len(parts) >= 3:
        return {"action": "send", "target_type": "private", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in _PROACTIVE_GROUP_WORDS and len(parts) >= 3:
        return {"action": "send", "target_type": "group", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head_l in _PROACTIVE_SEND_WORDS and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if head in _PROACTIVE_FILE_WORDS and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3]
            return {"action": "file", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head_l in _PROACTIVE_FORWARD_WORDS and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    return None


def _canonical_proactive_target_type(kind: str) -> str:
    token = str(kind or "").strip().lower()
    if not token:
        return ""
    if token in _PROACTIVE_PRIVATE_KINDS:
        return "private"
    if token in _PROACTIVE_GROUP_KINDS:
        return "group"
    return ""


def _target_display_label(target_type: str, name: str) -> str:
    prefix = "私聊" if target_type == "private" else "群聊"
    return f"{prefix}「{identity._sanitize_name(name, max_len=40)}」"


def _profile_aliases_for_user(user_id: Any) -> list[str]:
    profile = identity._qq_user_profile(user_id)
    aliases: list[str] = []
    for key in ("display_name", "address_as", "title"):
        value = str(profile.get(key) or "").strip()
        if value:
            aliases.append(value)
    return aliases


async def _safe_call_onebot_list(bot: Bot, api_name: str) -> list[dict[str, Any]]:
    try:
        data = await bot.call_api(api_name)
    except Exception:
        common.logger.debug("OneBot list API failed: %s", api_name, exc_info=True)
        return []
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _private_target_candidates(bot: Bot) -> list[models.ProactiveTarget]:
    """获取管理员可主动私聊的目标；返回值不包含真实 QQ 字符串展示。"""
    friend_rows = await _safe_call_onebot_list(bot, "get_friend_list") if live.allow_private_friends else []
    friend_ids: set[int] = set()
    names_by_id: dict[int, list[str]] = defaultdict(list)
    for row in friend_rows:
        try:
            uid = int(row.get("user_id"))
        except Exception:
            continue
        friend_ids.add(uid)
        for key in ("remark", "nickname", "card"):
            name = str(row.get(key) or "").strip()
            if name:
                names_by_id[uid].append(name)
    allowed_ids = set(live.admin_users) | set(live.allowed_private_users) | friend_ids
    # 只把 profile 作为别名来源；是否允许发送仍由 allowed_ids/friend list 决定。
    for raw_uid in state._adapter.qq_user_profiles:
        try:
            uid = int(raw_uid)
        except Exception:
            continue
        if uid in allowed_ids:
            names_by_id[uid].extend(_profile_aliases_for_user(uid))
    candidates: list[models.ProactiveTarget] = []
    for uid in sorted(allowed_ids):
        aliases = [a for a in dict.fromkeys(names_by_id.get(uid, []) + _profile_aliases_for_user(uid)) if a]
        label = aliases[0] if aliases else f"User{len(candidates) + 1}"
        candidates.append(models.ProactiveTarget("private", uid, label))
    return candidates


async def _group_target_candidates(bot: Bot) -> list[models.ProactiveTarget]:
    rows = await _safe_call_onebot_list(bot, "get_group_list")
    allowed = set(live.allowed_groups)
    candidates: list[models.ProactiveTarget] = []
    seen: set[int] = set()
    for row in rows:
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        if not permissions._is_group_allowed(gid):
            continue
        seen.add(gid)
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        candidates.append(models.ProactiveTarget("group", gid, identity._sanitize_name(name or f"Group{len(candidates) + 1}", max_len=40)))
    # 若 get_group_list 不可用，也允许配置白名单群通过“当前群/群名缓存”之外的显式候选参与解析。
    for gid in allowed:
        if gid not in seen:
            candidates.append(models.ProactiveTarget("group", int(gid), anonymize._anonymize_group_id(gid)))
    return candidates


def _candidate_alias_tokens(target: models.ProactiveTarget) -> set[str]:
    tokens = {_normalize_target_ref(target.label)}
    if target.target_type == "private":
        for alias in _profile_aliases_for_user(target.target_id):
            tokens.add(_normalize_target_ref(alias))
    elif target.target_type == "group":
        tokens.add(_normalize_target_ref(anonymize._anonymize_group_id(target.target_id)))
    return {t for t in tokens if t}


def _target_scope_denied(
    capability_key: str, requester: capability.Requester, target: models.ProactiveTarget,
) -> str:
    """目标不是群时传 None：群主在「本群」之外没有任何可主张的作用域。"""
    return capability.scope_deny_reason(
        capability_key, requester,
        target.target_id if target.target_type == "group" else None,
    )


def _scoped_targets(
    capability_key: str,
    requester: capability.Requester,
    candidates: list[models.ProactiveTarget],
) -> list[models.ProactiveTarget]:
    return [t for t in candidates if not _target_scope_denied(capability_key, requester, t)]


def _unresolved_target_text(target_type: str, ref_text: str) -> str:
    """够不着目标清单的人只能得到这一句：「没找到」和「有好几个」分开说，
    这个命令就是一台群名/好友昵称探测器。"""
    return f"没有找到{'私聊' if target_type == 'private' else '群聊'}目标：{ref_text}"


async def _resolve_proactive_target(
    bot: Bot,
    requester: capability.Requester,
    target_type: str,
    ref: str,
    *,
    capability_key: str,
) -> tuple[models.ProactiveTarget | None, str]:
    ref_text = str(ref or "").strip()
    if not ref_text:
        return None, "缺少目标名称。"
    ref_norm = _normalize_target_ref(ref_text)
    # 管理员显式输入 id 时允许解析，但目标列表/模型上下文不会展示真实 QQ 号。
    explicit = ref_text
    for prefix in ("qq:", "user:", "u:", "群:", "group:", "g:"):
        if explicit.lower().startswith(prefix):
            explicit = explicit[len(prefix):]
            break
    if explicit.isdigit():
        target_id = int(explicit)
        if target_type == "group":
            if not permissions._is_group_allowed(target_id):
                return None, "该群不在允许的主动群聊目标中。请先把群号加进 config/qq.yaml 的 channels.allowed_groups。"
            return models.ProactiveTarget("group", target_id, anonymize._anonymize_group_id(target_id)), ""
        allowed_private_ids = {t.target_id for t in await _private_target_candidates(bot)}
        if target_id not in allowed_private_ids:
            return None, "该私聊目标不在好友/管理员/允许私聊白名单中。"
        return models.ProactiveTarget("private", target_id, identity._qq_profile_display_name(target_id) or anonymize._anonymize_user_id(target_id)), ""

    current_gid = permissions._as_qq_id(requester.group_id) if requester.in_group else None
    if target_type == "group" and ref_norm in {_normalize_target_ref("当前群"), _normalize_target_ref("本群")} and current_gid is not None:
        gid = current_gid
        if not permissions._is_group_allowed(gid):
            return None, "当前群不在允许的主动群聊目标中。"
        return models.ProactiveTarget("group", gid, "当前群"), ""

    # 名字这条路先按作用域收候选：拿别人的群名来问「有没有这个群」本身就是一次探测，
    # 答案不能取决于那个名字。
    if target_type == "private":
        blanket = capability.scope_deny_reason(capability_key, requester, None)
        if blanket:
            return None, blanket
    candidates = _scoped_targets(
        capability_key, requester,
        await (_private_target_candidates(bot) if target_type == "private" else _group_target_candidates(bot)),
    )
    matches = [target for target in candidates if ref_norm in _candidate_alias_tokens(target)]
    if len(matches) == 1:
        return matches[0], ""
    if not capability.may_see_target_roster(requester):
        return None, _unresolved_target_text(target_type, ref_text)
    kind = "私聊" if target_type == "private" else "群聊"
    if not matches:
        return None, f"{_unresolved_target_text(target_type, ref_text)}\n可发送“主动目标 {kind}”查看可用名称。"
    labels = "、".join(identity._sanitize_name(m.label, max_len=24) for m in matches[:10])
    return None, f"目标名称不唯一：{ref_text}\n匹配到：{labels}\n请在配置中设置唯一显示名，或使用管理员显式 id 前缀。"


async def _proactive_target_list_text(bot: Bot, target_type: str = "") -> str:
    sections: list[str] = []
    if target_type in ("", "private"):
        privates = await _private_target_candidates(bot)
        names = [identity._sanitize_name(t.label, max_len=30) for t in privates if t.label]
        sections.append("【可主动私聊目标】\n" + ("、".join(names[:80]) if names else "（无；需好友列表、管理员或允许私聊白名单）"))
    if target_type in ("", "group"):
        groups = await _group_target_candidates(bot)
        names = [identity._sanitize_name(t.label, max_len=30) for t in groups if t.label]
        sections.append("【可主动群聊目标】\n" + ("、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"))
    return "\n\n".join(sections) + "\n\n提示：目标列表不展示真实 QQ 号；普通模型上下文也不会接触这些真实 id。"


def _target_to_send_dict(target: models.ProactiveTarget) -> Dict[str, Any]:
    if target.target_type == "private":
        return {"type": "private", "user_id": target.target_id}
    return {"type": "group", "group_id": target.target_id}


def _attachment_path_under_workspace(raw_path: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / path
    try:
        resolved = path.resolve()
        workspace = Path(CLONOTH_WORKSPACE).resolve()
        if workspace not in resolved.parents and resolved != workspace:
            return None
    except Exception:
        return None
    return resolved


def _parse_file_send_body(body: str) -> tuple[Dict[str, Any] | None, str]:
    parts = str(body or "").strip().split(maxsplit=1)
    if not parts:
        return None, "缺少文件路径。"
    path = _attachment_path_under_workspace(parts[0])
    if path is None:
        return None, "文件路径必须位于 Clonoth 工作区内。"
    if not path.exists() or not path.is_file():
        return None, f"文件不存在：{parts[0]}"
    display_name = parts[1].strip() if len(parts) > 1 else path.name
    rel_path = str(path)
    try:
        rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
    except Exception:
        pass
    return {"type": "file", "path": rel_path, "name": display_name}, ""


def _forward_media_text(message: Any, bot_self_id: Any = None) -> str:
    """为合并转发提取文本，去掉会重复出现的图片占位符。"""
    text = message_text._message_to_text_generic(message, bot_self_id)
    if media._iter_qq_image_sources(message):
        # 转发 node 会单独携带 image 段；留着占位就是卡片里先出现「[图片]」文字再跟真实图片。
        text = re.sub(r"\s+", " ", text.replace(message_text.IMAGE_PLACEHOLDER, "")).strip()
    return text


def _forward_content_segments(text: str = "", attachments: list[dict[str, Any]] | None = None, image_urls: list[str] | None = None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": {"text": str(text)}})
    for url in image_urls or []:
        if url:
            segments.append({"type": "image", "data": {"file": str(url)}})
    for att in attachments or []:
        path = outbound_attachments._resolve_attachment_path(att)
        if path and path.exists() and path.suffix.lower() in common._IMAGE_SUFFIXES:
            segments.append({"type": "image", "data": {"file": f"file://{str(path.resolve())}"}})
        elif att:
            name = outbound_attachments._attachment_filename(att) or "附件"
            segments.append({"type": "text", "data": {"text": f"[附件: {name}]"}})
    return segments


def _make_forward_node(bot: Bot, text: str = "", attachments: list[dict[str, Any]] | None = None, *, nickname: str = "Clonoth 通知", user_id: Any = None, image_urls: list[str] | None = None) -> dict[str, Any] | None:
    content = _forward_content_segments(text, attachments, image_urls)
    if not content:
        return None
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id or getattr(bot, "self_id", "") or "10000"),
            "nickname": identity._sanitize_name(nickname, max_len=32),
            "content": content,
        },
    }


def _append_attachment_notes(text: str, errors: list[str]) -> str:
    """把附件处理错误（图片太大/格式不支持）并进转发文本，避免被引用/合并转发路径静默丢图。"""
    notes = "\n".join(dict.fromkeys(errors))
    if not notes:
        return text
    return f"{text}\n{notes}" if text else notes


async def _forward_messages_to_nodes(bot: Bot, messages: list[dict[str, Any]], conversation_key: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in messages[:live.forward_msg_max_messages]:
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        sender_id = sender.get("user_id") or item.get("user_id") or getattr(bot, "self_id", "")
        nickname = sender.get("card") or sender.get("nickname") or "转发消息"
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = _forward_media_text(content, getattr(bot, "self_id", None))
        image_sources = media._iter_qq_image_sources(content)
        attachments: list[dict[str, Any]] = []
        if image_sources:
            attachments, errors = await media._image_sources_to_attachments(image_sources, conversation_key)
            text = _append_attachment_notes(text, errors)
        node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
        if node:
            nodes.append(node)
    return nodes


async def _forward_nodes_from_reply(bot: Bot, event: Event, conversation_key: str) -> list[dict[str, Any]]:
    reply_message_id = message_text._extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply = await message_text._resolve_reply_payload(bot, event, reply_message_id)
    if not reply:
        cached_nodes = media._forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
        common.logger.info("forward reply skipped: no reply object/id=%s", reply_message_id)
        return []
    message = reply.get("message") if reply.get("message") is not None else reply.get("raw_message")
    sources = message_text._extract_forward_sources(message)
    if sources:
        nodes: list[dict[str, Any]] = []
        for source in sources[:3]:
            messages = await message_text._forward_source_messages(bot, source)
            if messages:
                nodes.extend(await _forward_messages_to_nodes(bot, messages, conversation_key))
        if nodes:
            return nodes
    sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else {}
    sender_id = sender.get("user_id") or reply.get("user_id") or getattr(bot, "self_id", "")
    nickname = sender.get("card") or sender.get("nickname") or "引用消息"
    text = _forward_media_text(message, getattr(bot, "self_id", None))
    image_sources = media._iter_qq_image_sources(message)
    attachments: list[dict[str, Any]] = []
    if image_sources:
        attachments, errors = await media._image_sources_to_attachments(image_sources, conversation_key)
        text = _append_attachment_notes(text, errors)
        if attachments and reply_message_id is not None:
            media._remember_reply_attachments(reply_message_id, conversation_key, str(sender_id or ""), attachments)
    if not attachments and reply_message_id is not None:
        cached_nodes = media._forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
    node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
    return [node] if node else []


def _request_send_context(
    scope: str, request_identity: str, conversation_key: str = "",
) -> OutboundSendContext:
    digest = hashlib.sha256(request_identity.encode("utf-8", "ignore")).hexdigest()
    return OutboundSendContext(
        conversation_key=conversation_key,
        idempotency_key=f"request:{scope}:{digest}",
    )


async def _send_forward_nodes(
    bot: Bot,
    target: models.ProactiveTarget,
    nodes: list[dict[str, Any]],
    *,
    send_context: OutboundSendContext | None = None,
    image_attachments: list[Any] | None = None,
) -> None:
    target_dict = _target_to_send_dict(target)
    await state._adapter.features.check_send(bot, target_dict)
    identity = "forward-nodes:" + hashlib.sha256(
        json.dumps(nodes, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    context = (send_context or OutboundSendContext()).child(identity)
    key = make_idempotency_key(
        target_dict, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._adapter.outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._adapter.outbound_idempotency.wait_for_resolution(key)
        claim = (
            await state._adapter.outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return
    heartbeat = asyncio.create_task(sending._heartbeat_idempotency_claim(claim))
    try:
        async def send_forward() -> Any:
            await state._adapter.features.check_send(bot, target_dict)
            if target.target_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=int(target.target_id), messages=nodes,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=int(target.target_id), messages=nodes,
            )

        result = await protected_claim_send(
            state._adapter.outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=sending._sent_ttl_for_context(context),
            message_id_getter=sending._extract_sent_message_id,
        )
        if image_attachments:
            message_id = sending._extract_sent_message_id(result)
            if message_id:
                # 合并转发只返回卡片一个 message_id，整批图都挂在卡片上
                outbound_attachments._remember_message_attachments(message_id, list(image_attachments))
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def _filter_attachments_by_kind(
    attachments: List[Dict[str, Any]],
    *,
    include_images: bool,
    include_files: bool,
) -> List[Dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "")
        if kind == "image" and include_images:
            selected.append(dict(att))
        elif kind == "file" and include_files:
            selected.append(dict(att))
        elif include_images and include_files:
            selected.append(dict(att))
    return selected


async def _maybe_handle_proactive_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理管理员主动私聊/群聊发送、文件发送、合并转发等显式命令请求。

    注意：这里只处理带明确命令前缀（如“合并转发/私信/群发/转发到”）的结构化命令。
    自然语言转发需求（例如“把上面聊到的 xxx 转发给我”）不在此拦截，而是原样交给
    AI（qq.orchestrator 节点）分析，由其调用 qq_forward 工具挑选并转发对应条目，
    避免与“让 QQ Bot 分析哪些条目需要转发”的初衷冲突。
    """
    command = _parse_proactive_command(user_text)
    if command is None:
        return None
    if not permissions._can("proactive", event):
        # 不向没有这项能力的人暴露“主动发送/转发”能力、目标列表或命令名称。
        return permissions._capability_denial("proactive", event, "该请求不可用。")
    requester = permissions._requester(event)
    action = command.get("action") or ""
    if action == "help":
        return _proactive_help_text()
    if action == "list":
        if not capability.may_see_target_roster(requester):
            # 目标清单里是 bot 待过的每个群和能私聊的每个人。群主的权限只到自己那个群，
            # 这份清单本身就超出了他该知道的范围。
            return "你只能向本群发送，没有可选目标清单。"
        return await _proactive_target_list_text(bot, command.get("target_type") or "")

    target_type = command.get("target_type") or ""
    target, error = await _resolve_proactive_target(
        bot, requester, target_type, command.get("target_ref") or "",
        capability_key="proactive",
    )
    if target is None:
        return error or "目标解析失败。"
    scope_denied = _target_scope_denied("proactive", requester, target)
    if scope_denied:
        return scope_denied
    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)
    request_id = str(getattr(event, "message_id", "") or uuid.uuid4().hex)
    send_context = _request_send_context(
        "proactive", f"{conversation_key}:{request_id}", conversation_key,
    )

    if action == "send":
        body = str(command.get("body") or "").strip()
        attachments = current_attachments or []
        if not body and not attachments:
            return "发送内容为空。"
        await outbound_attachments._send_text_and_attachments(
            bot, send_target, body, attachments, send_context=send_context,
        )
        return f"已发送到{label}。"

    if action == "file":
        attachment, file_error = _parse_file_send_body(command.get("body") or "")
        if attachment is None:
            return file_error
        await outbound_attachments._send_attachments(
            bot, send_target, [attachment], send_context=send_context,
        )
        return f"已向{label}发送文件：{attachment.get('name') or attachment.get('path')}"

    if action == "forward":
        body = str(command.get("body") or "").strip()
        payload_attachments = current_attachments or []
        nodes: list[dict[str, Any]] = []
        if body:
            chunks = [chunk.strip() for chunk in re.split(r"\n\s*---\s*\n", body) if chunk.strip()]
            for chunk in chunks or [body]:
                node = _make_forward_node(bot, chunk, payload_attachments if not nodes else None)
                if node:
                    nodes.append(node)
        if not nodes:
            nodes = await _forward_nodes_from_reply(bot, event, conversation_key)
        if not nodes and payload_attachments:
            node = _make_forward_node(bot, "", payload_attachments)
            if node:
                nodes.append(node)
        if not nodes:
            return "没有可转发内容。请提供文本，或引用一条消息/合并转发卡片。"
        card_images = _filter_attachments_by_kind(
            payload_attachments, include_images=True, include_files=False,
        ) if payload_attachments else []
        try:
            await _send_forward_nodes(
                bot, target, nodes, send_context=send_context,
                image_attachments=card_images,
            )
        except Exception as exc:
            common.logger.warning("send forward message failed: %s", exc, exc_info=True)
            return "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"
        return f"已向{label}发送合并转发（{len(nodes)} 条 node）。"

    return None
