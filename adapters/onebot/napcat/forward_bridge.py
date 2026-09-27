"""qq_forward 工具用的本地 HTTP 转发桥。"""
from __future__ import annotations

import logging
import contextlib
import hmac
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot
from platforms.ids import normalize_id
from ..config import (
    CLONOTH_WORKSPACE,
    FORWARD_BRIDGE_HOST,
    FORWARD_BRIDGE_PORT,
    FORWARD_BRIDGE_TOKEN,
    FORWARD_BRIDGE_TOKEN_FILE,
)
from adapters._shared import capability
from ..live_config import live, refresh as refresh_live_config
from ..forward_authz import (
    delivery_deny_reason as forward_delivery_deny_reason,
    file_deny_reason as forward_file_deny_reason,
    file_paths_deny_reason as forward_file_paths_deny_reason,
)

from . import anonymize, common, identity, message_text, models, outbound_attachments, permissions, proactive, sending, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _forward_bridge_token() -> str:
    """返回 Bridge 令牌：显式配置优先（data/config.yaml 的 platforms.qq，其次 env），否则读/自签令牌文件；
    都拿不到返回空串，Bridge 不启动。"""
    if FORWARD_BRIDGE_TOKEN:
        return FORWARD_BRIDGE_TOKEN
    if state._adapter.forward_bridge_token_cache:
        return state._adapter.forward_bridge_token_cache
    path = Path(FORWARD_BRIDGE_TOKEN_FILE)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except Exception:
        common.logger.error("qq_forward bridge token 文件不可读：%s", FORWARD_BRIDGE_TOKEN_FILE)
        return ""
    if existing:
        state._adapter.forward_bridge_token_cache = existing
        return existing
    try:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except Exception:
        common.logger.error("qq_forward bridge token 文件不可写：%s", FORWARD_BRIDGE_TOKEN_FILE)
        return ""
    state._adapter.forward_bridge_token_cache = token
    return token


def _forward_bridge_lookup_target(session_id: str) -> Dict[str, Any] | None:
    """在内存/持久化两张表中按单个 session_id 查找目标。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for source in (state._adapter.session_targets, state._adapter.persisted_session_targets):
        target = source.get(sid)
        if isinstance(target, dict) and target:
            return dict(target)
    return None


def _forward_bridge_session_target(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> Dict[str, Any] | None:
    """根据 Engine 传来的 session_id 找回该会话对应的真实 QQ 目标。

    2026-07-14 修复原因：入口任务实际运行在 entry branch session（branch_xxx），
    ctx.session_id 为 branch，而 session_targets / persisted_session_targets 只按
    用户可见的 parent session 登记。若直接拿 branch 去查会 miss，导致
    op=remind + target_type=current 在群聊中误报“当前会话不是群聊”。
    做法：依次尝试 parent_session_id -> 传入 session_id -> runtime_session_id，
    任一命中即返回，对旧调用（仅传 session_id）完全兼容。
    """
    for candidate in (parent_session_id, session_id, runtime_session_id):
        target = _forward_bridge_lookup_target(candidate)
        if target:
            return target
    return None


def _forward_bridge_origin_group_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if str(target.get("type") or "") == "group" and target.get("group_id") is not None:
        try:
            return int(target.get("group_id"))
        except Exception:
            return None
    return None


def _forward_bridge_origin_user_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    """当前会话触发者 QQ 号，用于把内容“私发给我”。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if target.get("user_id") is not None:
        try:
            return int(target.get("user_id"))
        except Exception:
            return None
    return None


def _forward_bridge_requester(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> capability.Requester:
    """转发桥的请求者身份。桥自己没有身份，发起人来自 session 归属。

    group_role 留空：拿群角色要多跑一次 get_group_member_info，而这条路上的
    cross_session 最宽只给到名单，群角色不参与判定。
    """
    uid = _forward_bridge_origin_user_id(
        session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
    )
    return capability.Requester(
        user_id=normalize_id(uid),
        listed_admin=uid is not None and permissions._is_admin_user(uid),
        group_id=normalize_id(_forward_bridge_origin_group_id(
            session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
        )),
    )


def _forward_bridge_record_ref(record: models.GroupContentRecord) -> str:
    """群消息的稳定标识：deque 淘汰后位置下标会整体左移，只有 seq 不变。"""
    return f"m{record.seq}" if record.seq > 0 else ""


def _forward_bridge_attachment_ref(record: Mapping[str, Any]) -> str:
    """最近附件的稳定标识：新附件入队和缺文件过滤都会让位置下标漂移，只有 seq 不变。"""
    seq = int(record.get("seq") or 0)
    return f"a{seq}" if seq > 0 else ""


def _forward_bridge_parse_refs(raw: Any, prefix: str) -> list[int]:
    """把模型给的 ref 列表容错解析成 seq：接受 "m41"/"#m41"/"41"/41，去空白、去前导 # 与前缀字母，去重保序，解析不出的丢弃。"""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[int] = set()
    out: list[int] = []
    for item in raw:
        token = str(item).strip().lstrip("#").strip()
        if token[:1].lower() == prefix:
            token = token[1:].strip()
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _forward_bridge_parse_record_refs(raw: Any) -> list[int]:
    """群消息 ref（m<seq>）的容错解析。"""
    return _forward_bridge_parse_refs(raw, "m")


def _forward_bridge_parse_attachment_refs(raw: Any) -> list[int]:
    """最近附件 ref（a<seq>）的容错解析。"""
    return _forward_bridge_parse_refs(raw, "a")


def _forward_bridge_read_origin(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
    op: str,
) -> tuple[Dict[str, Any] | None, str]:
    """读接口（list/recent）统一鉴门：来源必须可解析且仍在白名单内，否则给出拒绝文案。

    返回 (target, "") 放行并写审计日志（只记匿名别名）；(None, reason) 拒绝。
    """
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
    ttype = str(target.get("type") or "")
    if ttype == "group":
        try:
            gid = int(target.get("group_id"))
        except (TypeError, ValueError):
            return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
        # 群移出白名单后内存里的历史仍在，读接口必须重新过一次白名单
        if not permissions._is_group_allowed(gid):
            common.logger.warning(
                "qq_forward_read denied: group not allowed (op=%s origin=%s)",
                op, anonymize._anonymize_group_id(gid),
            )
            return None, "当前群不在允许列表中，无法读取历史。"
        common.logger.info(
            "qq_forward_read",
            extra={"op": op, "origin_type": "group", "origin": anonymize._anonymize_group_id(gid)},
        )
        return target, ""
    if ttype == "private":
        try:
            uid = int(target.get("user_id"))
        except (TypeError, ValueError):
            return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
        if not permissions._is_private_origin_allowed(uid):
            common.logger.warning(
                "qq_forward_read denied: private not allowed (op=%s origin=%s)",
                op, anonymize._anonymize_user_id(uid),
            )
            return None, "当前私聊不在允许列表中。"
        common.logger.info(
            "qq_forward_read",
            extra={"op": op, "origin_type": "private", "origin": anonymize._anonymize_user_id(uid)},
        )
        return target, ""
    return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"


def _forward_bridge_list_messages(
    session_id: str,
    query: str = "",
    limit: int = 30,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出当前会话（群）最近消息，供 AI 按 ref/关键词多选转发。

    每条带一个稳定 ref（m<seq>）；只包含匿名后可展示给模型的字段。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(state._adapter.group_content_records.get(int(group_id), ()))
    if not records:
        return []
    query_norm = str(query or "").strip().lower()
    limit = max(1, min(int(limit or 30), live.forward_bridge_max_messages))
    items: list[dict[str, Any]] = []
    for record in records:
        ref = _forward_bridge_record_ref(record)
        if not ref:
            # 拿不到稳定标识（seq<=0，多为热更新前的旧 record）就不给模型挑。
            continue
        line = record.formatted_line or ""
        text = record.text or ""
        if query_norm and query_norm not in line.lower() and query_norm not in text.lower():
            continue
        att_kinds = sorted({str(att.get("type") or "") for att in (record.attachments or []) if isinstance(att, dict)})
        items.append({
            "ref": ref,
            "preview": anonymize._anonymize_text_for_ai(message_text._compact_text(line or text, limit=200)),
            "sender": anonymize._anonymize_text_for_ai(record.sender_name or ""),
            "has_image": "image" in att_kinds,
            "has_file": "file" in att_kinds,
        })
    # 只回传最近 limit 条，保留时间顺序（旧→新）。
    return items[-limit:]


def _forward_bridge_origin_bucket_key(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> str:
    """把来源会话映射为“最近发送/生成附件”索引的桶 key。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return ""
    return outbound_attachments._sent_attachment_bucket_key(target)


def _forward_bridge_list_recent(
    session_id: str,
    *,
    only_images: bool = True,
    limit: int = 20,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出本会话最近由 Bot 发出/生成的附件（如生图产出的图片），带稳定 ref（a<seq>）。"""
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = outbound_attachments._recent_sent_attachment_records(bucket, only_images=only_images)
    limit = max(1, min(int(limit or 20), live.forward_bridge_max_messages))
    records = records[-limit:]
    items: list[dict[str, Any]] = []
    for rec in records:
        ref = _forward_bridge_attachment_ref(rec)
        if not ref:
            continue
        items.append({
            "ref": ref,
            "name": identity._sanitize_name(rec.get("name") or Path(str(rec.get("path") or "")).name, max_len=60),
            "type": str(rec.get("type") or "file"),
        })
    return items


def _forward_bridge_pick_recent_attachments(
    session_id: str,
    *,
    refs: list[int] | None,
    only_images: bool = False,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """按 ref（附件 seq）从最近发送/生成附件里挑选；无 ref 时默认取最新一个。

    给了 refs 却全落空 → 返回 []：退回最新一张会把没人要的图发出去。
    返回可直接发送的附件 dict（带绝对 path + name + type）。
    """
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = outbound_attachments._recent_sent_attachment_records(bucket, only_images=only_images)
    if not records:
        return []
    selected: list[dict[str, Any]]
    if refs:
        by_seq = {int(rec.get("seq") or 0): rec for rec in records if int(rec.get("seq") or 0) > 0}
        selected = [by_seq[seq] for seq in refs if seq in by_seq]
        if not selected:
            return []
    else:
        selected = [records[-1]]
    attachments: list[dict[str, Any]] = []
    for rec in selected:
        attachments.append({
            "type": str(rec.get("type") or "file"),
            "path": str(rec.get("path") or ""),
            "name": str(rec.get("name") or ""),
        })
    return attachments


def _forward_bridge_pick_records(
    session_id: str,
    *,
    refs: list[int] | None,
    query: str,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> models.ForwardSelection:
    """按 ref（群历史 seq）或关键词挑选要转发的群消息。

    给了 refs 却一条都没命中 → 带 error 的空选择；部分命中 → records 带命中项、
    missing_refs 记未命中项。没给 refs 时保留 query → 最近 N 条两级回退。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return models.ForwardSelection([])
    records = list(state._adapter.group_content_records.get(int(group_id), ()))
    if refs:
        by_seq = {r.seq: r for r in records if r.seq}
        picked: list[models.GroupContentRecord] = []
        missing: list[str] = []
        for seq in refs:
            record = by_seq.get(seq)
            if record is not None:
                picked.append(record)
            else:
                missing.append(f"m{seq}")
        if not picked:
            # ref 给了却一条都没命中就报错：回退到最近 N 条会把没人要的消息发给别人。
            return models.ForwardSelection([], tuple(missing), "所选消息已不在缓存中，请重新 op=list 并用返回的 ref 挑选。")
        return models.ForwardSelection(picked, tuple(missing))
    if not records:
        return models.ForwardSelection([])
    query_norm = str(query or "").strip().lower()
    if query_norm:
        matched = [r for r in records if query_norm in (r.formatted_line or "").lower() or query_norm in (r.text or "").lower()]
        if matched:
            return models.ForwardSelection(matched[-live.forward_bridge_max_messages:])
    return models.ForwardSelection(records[-live.forward_bridge_max_messages:])


async def _forward_bridge_resolve_target(
    bot: Bot,
    session_id: str,
    target_type: str,
    target_ref: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> tuple[models.ProactiveTarget | None, str]:
    """把工具给出的目标（含 self/current 语义）解析为真实 ProactiveTarget。"""
    target_type = str(target_type or "").strip().lower()
    ref = str(target_ref or "").strip()
    if target_type == "self" or ref in {"我", "自己", "me", "self"}:
        uid = _forward_bridge_origin_user_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if uid is None:
            return None, "无法确定当前用户，无法私发给你。"
        # 用户被移出私聊白名单后不应还能借 Bot 身份私发给他，与 current 分支同款复核。
        if not permissions._is_private_origin_allowed(uid):
            return None, "当前私聊不在允许列表中。"
        return models.ProactiveTarget("private", uid, identity._qq_profile_display_name(uid) or anonymize._anonymize_user_id(uid)), ""
    if target_type == "current" or ref in {"当前群", "本群", "这个群", "此群"}:
        gid = _forward_bridge_origin_group_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if gid is None:
            return None, "当前会话不是群聊，无法发送到本群。"
        if not permissions._is_group_allowed(gid):
            return None, "当前群不在允许的主动群聊目标中。"
        return models.ProactiveTarget("group", gid, "当前群"), ""
    # 其余交给既有的目标解析（支持显式 id 前缀与配置的显示名/别名）。
    resolve_type = "group" if target_type == "group" else "private"
    return await proactive._resolve_proactive_target(
        bot, _forward_bridge_requester(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        ),
        resolve_type, ref, capability_key="cross_session",
    )


async def _forward_bridge_records_to_nodes(
    bot: Bot,
    records: list[models.GroupContentRecord],
    *,
    include_images: bool,
    include_files: bool,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records[:live.forward_bridge_max_messages]:
        text = record.text or record.formatted_line or ""
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            attachments = proactive._filter_attachments_by_kind(
                record.attachments or [],
                include_images=include_images,
                include_files=include_files,
            )
        node = proactive._make_forward_node(
            bot,
            text,
            attachments,
            nickname=record.sender_name or "转发消息",
            user_id=record.sender_id or getattr(bot, "self_id", None),
        )
        if node:
            nodes.append(node)
    return nodes


def _forward_bridge_resolve_files(raw_paths: Any, display_names: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """把工具传来的仓库相对/绝对路径解析为可发送的 file 附件。

    Why: 管理员“把仓库里的 xx 文件发出来”需要把工作区内文件发到 QQ。
    How: 复用 _attachment_path_under_workspace 限定在工作区内，防止目录穿越。
    Purpose: 只允许发送工作区内存在的普通文件，逐个回报错误。
    """
    paths: list[str] = []
    if isinstance(raw_paths, str):
        paths = [raw_paths]
    elif isinstance(raw_paths, list):
        paths = [str(item) for item in raw_paths if str(item).strip()]
    names: list[str] = []
    if isinstance(display_names, str):
        names = [display_names]
    elif isinstance(display_names, list):
        names = [str(item) for item in display_names]

    attachments: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, raw in enumerate(paths):
        raw = str(raw or "").strip()
        if not raw:
            continue
        path = proactive._attachment_path_under_workspace(raw)
        if path is None:
            errors.append(f"{raw}：路径必须位于 Clonoth 工作区内。")
            continue
        if not path.exists() or not path.is_file():
            errors.append(f"{raw}：文件不存在。")
            continue
        rel_path = str(path)
        try:
            rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
        except Exception:
            pass
        # 黑名单按工作区相对路径判定，故必须在 relative_to 之后。
        deny_reason = forward_file_deny_reason(rel_path)
        if deny_reason:
            common.logger.warning("qq_forward blocked sensitive file: %s", rel_path)
            errors.append(deny_reason)
            continue
        display_name = names[idx].strip() if idx < len(names) and str(names[idx]).strip() else path.name
        attachments.append({"type": "file", "path": rel_path, "name": display_name})
    return attachments, errors


async def _forward_bridge_execute(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次自然语言转发/发送/提醒请求。仅在 Bot 进程内运行。

    The payload is mutated with ``_request_identity`` once. Re-entering the same
    request object or retrying with the same caller request_id is stable; a future
    independent request receives a new identity even when content is identical.
    """
    request_identity = str(
        payload.get("_request_identity")
        or payload.get("request_id")
        or payload.get("idempotency_key")
        or ""
    ).strip()
    if not request_identity:
        request_identity = uuid.uuid4().hex
    payload.setdefault("_request_identity", request_identity)
    try:
        bot = get_bot()
    except Exception:
        return {"ok": False, "error": "QQ Bot 尚未连接，无法执行转发。"}
    session_id = str(payload.get("session_id") or "").strip()
    # [2026-07-14] 入口任务运行在 branch session 上，因此工具会额外透传 parent/runtime
    # session id；这里全部收集下来供 target 解析做多级回退。
    parent_session_id = str(payload.get("parent_session_id") or "").strip()
    runtime_session_id = str(payload.get("runtime_session_id") or "").strip()
    action = str(payload.get("action") or "forward").strip().lower()
    target_type = str(payload.get("target_type") or "").strip().lower()
    target_ref = str(payload.get("target_ref") or "").strip()
    extra_text = sending._truncate_qq_text(str(payload.get("text") or "").strip())
    query = str(payload.get("query") or "").strip()
    include_images = bool(payload.get("include_images", True))
    include_files = bool(payload.get("include_files", True))
    if payload.get("message_refs") is None and payload.get("message_indices") is not None:
        # 位置下标和 seq 都是合法整数，静默当 ref 会选错消息发给第三方，宁可让模型重跑 op=list。
        return {"ok": False, "error": "message_indices 已不再支持，请重新 op=list 并用返回的 ref 填 message_refs。"}
    refs = _forward_bridge_parse_record_refs(payload.get("message_refs"))
    # “最近生成/发出的图片”系列参数：use_recent 启用；recent_refs 按 ref 挑选。
    use_recent = bool(payload.get("use_recent", False))
    if payload.get("recent_refs") is None and payload.get("recent_indices") is not None:
        return {"ok": False, "error": "recent_indices 已不再支持，请重新 op=recent 并用返回的 ref 填 recent_refs。"}
    recent_refs = _forward_bridge_parse_attachment_refs(payload.get("recent_refs"))
    if recent_refs:
        use_recent = True

    target, error = await _forward_bridge_resolve_target(
        bot,
        session_id,
        target_type,
        target_ref,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if target is None:
        return {"ok": False, "error": error or "目标解析失败。"}

    # 跨会话投递等于借 Bot 身份向任意人/群发消息，只有管理员可用；
    # 非管理员仍可把内容发回自己或本群，那些内容他本来就看得到。
    requester = _forward_bridge_requester(
        session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
    )
    origin_user_id = requester.user_id
    origin_group_id = requester.group_id
    is_admin = requester.listed_admin
    # 这项能力最宽只能给到名单，群角色不参与判定 —— 也就没必要为它多跑一次
    # get_group_member_info。走 allows 是为了让 off 档对名单里的人同样生效。
    may_cross = capability.allows("cross_session", permissions._grant_of("cross_session"), requester)
    cross_session_denied = forward_delivery_deny_reason(
        is_admin=may_cross,
        target_type=target.target_type,
        target_id=target.target_id,
        origin_user_id=origin_user_id,
        origin_group_id=origin_group_id,
    )
    if cross_session_denied:
        common.logger.warning(
            "qq_forward denied cross-session delivery: action=%s target=%s",
            action, target.target_type,
        )
        return {"ok": False, "error": cross_session_denied}

    send_target = proactive._target_to_send_dict(target)
    label = proactive._target_display_label(target.target_type, target.label)
    conversation_key = f"qq_forward:{target.target_type}:{target.target_id}"
    send_context = proactive._request_send_context(
        "qq_forward", request_identity, conversation_key,
    )

    # 纯提醒/通知：不涉及历史挑选，直接发一段文本。
    if action == "remind":
        body = extra_text
        if not body:
            return {"ok": False, "error": "提醒内容为空。"}
        await outbound_attachments._send_text_and_attachments(
            bot, send_target, body, [], send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送提醒。"}

    # 发送工作区文件：“把仓库里的 xx 文件发出来”。支持一次多个文件。
    if action == "file":
        raw_file_paths = payload.get("file_paths")
        file_paths_denied = forward_file_paths_deny_reason(
            is_admin=is_admin, raw_file_paths=raw_file_paths,
        )
        if file_paths_denied:
            common.logger.warning("qq_forward denied file exfiltration by non-admin")
            return {"ok": False, "error": file_paths_denied}
        attachments, file_errors = _forward_bridge_resolve_files(
            raw_file_paths,
            payload.get("file_names"),
        )
        # 允许 op=file 时不给 file_paths、而是从最近生成/发出的附件里挑（use_recent）。
        if not attachments and use_recent:
            recent_atts = _forward_bridge_pick_recent_attachments(
                session_id,
                refs=recent_refs,
                only_images=False,
                parent_session_id=parent_session_id,
                runtime_session_id=runtime_session_id,
            )
            attachments = _forward_bridge_resolve_files(
                [att.get("path") for att in recent_atts],
                [att.get("name") for att in recent_atts],
            )[0]
        if not attachments:
            hint = "；".join(file_errors) if file_errors else "请在 file_paths 里给出工作区内的文件路径，或用 use_recent 发送最近生成的图片。"
            return {"ok": False, "error": f"没有可发送的文件：{hint}"}
        if extra_text:
            await outbound_attachments._send_text_and_attachments(
                bot, send_target, extra_text, [], send_context=send_context,
            )
        await outbound_attachments._send_attachments(
            bot, send_target, attachments, send_context=send_context,
        )
        names = "、".join(identity._sanitize_name(att.get("name") or att.get("path"), max_len=40) for att in attachments)
        result = f"已向{label}发送 {len(attachments)} 个文件：{names}"
        if file_errors:
            result += "\n（部分文件未发送：" + "；".join(file_errors) + "）"
        return {"ok": True, "result": result}

    # use_recent：把“最近生成/发出的图片”作为附件直接发送（不依赖群历史）。
    if use_recent and action in {"send", "forward"}:
        recent_atts = _forward_bridge_pick_recent_attachments(
            session_id,
            refs=recent_refs,
            only_images=False,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        recent_atts = _forward_bridge_resolve_files(
            [att.get("path") for att in recent_atts],
            [att.get("name") for att in recent_atts],
        )[0]
        if not recent_atts:
            return {"ok": False, "error": "没有找到最近生成/发送的图片。可先用 op=recent 查看可选图片。"}
        await outbound_attachments._send_text_and_attachments(
            bot, send_target, extra_text, recent_atts, send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送 {len(recent_atts)} 张最近生成/发送的图片。"}

    selection = _forward_bridge_pick_records(
        session_id,
        refs=refs,
        query=query,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if selection.error:
        return {"ok": False, "error": selection.error}
    records = selection.records
    missing_note = (
        f"（其中 {len(selection.missing_refs)} 条已过期，未包含）"
        if selection.missing_refs else ""
    )

    if action == "send":
        # 直接把挑选到的消息拼成一段文本 + 附件发送（非合并转发卡片）。
        lines = [r.formatted_line or r.text for r in records if (r.formatted_line or r.text)]
        body = "\n".join(part for part in ([extra_text] if extra_text else []) + lines).strip()
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            for r in records:
                attachments.extend(proactive._filter_attachments_by_kind(
                    r.attachments or [],
                    include_images=include_images,
                    include_files=include_files,
                ))
        if not body and not attachments:
            return {"ok": False, "error": "没有可发送的内容。"}
        await outbound_attachments._send_text_and_attachments(
            bot, send_target, sending._truncate_qq_text(body), attachments,
            send_context=send_context,
        )
        return {"ok": True, "result": f"已发送到{label}（{len(records)} 条消息）。{missing_note}"}

    # 默认 action == "forward"：合并转发卡片，支持多选多条消息。
    nodes = await _forward_bridge_records_to_nodes(
        bot,
        records,
        include_images=include_images,
        include_files=include_files,
    )
    card_images: list[dict[str, Any]] = []
    if include_images:
        for r in records:
            card_images.extend(proactive._filter_attachments_by_kind(
                r.attachments or [], include_images=True, include_files=False,
            ))
    if extra_text:
        head = proactive._make_forward_node(bot, extra_text, None, nickname="Clonoth 通知")
        if head:
            nodes.insert(0, head)
    if not nodes:
        return {"ok": False, "error": "没有可转发的消息。请先用 list 查看上文消息并给出 message_refs 或 query。"}
    try:
        await proactive._send_forward_nodes(
            bot, target, nodes, send_context=send_context,
            image_attachments=card_images,
        )
    except Exception as exc:
        common.logger.warning("qq_forward bridge send forward failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"}
    return {"ok": True, "result": f"已向{label}发送合并转发（{len(nodes)} 条）。{missing_note}"}


def _forward_bridge_check_token(request: "Any") -> bool:
    token = _forward_bridge_token()
    if not token:
        return False
    presented = request.headers.get("X-Forward-Token", "")
    # 逐字节比较会把令牌问出来
    if not (presented and hmac.compare_digest(presented, token)):
        common.logger.warning(
            "qq_forward bridge rejected request: token mismatch (peer=%s)",
            getattr(request, "remote", ""),
        )
        return False
    return True


async def _forward_bridge_http_handler(request: "Any") -> "Any":
    from aiohttp import web  # 局部导入，未启用 Bridge 时不强依赖 aiohttp。

    # aiohttp 每个请求一个 task，在这里钉住整次转发用同一份配置。
    refresh_live_config()
    if not _forward_bridge_check_token(request):
        return web.json_response(
            {
                "ok": False,
                "error": "Bridge 令牌不匹配：qq_forward 工具只从 data/config.yaml 的 "
                "platforms.qq.forward_bridge_token、工作区 .env 或 data/onebot_forward_bridge_token "
                "读令牌，请确认它和 bot 用的是同一个。",
            },
            status=403,
        )
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)
    caller_request_id = str(
        payload.get("request_id")
        or request.headers.get("Idempotency-Key", "")
        or request.headers.get("X-Request-ID", "")
        or uuid.uuid4().hex
    ).strip()
    # The private field is internal-only: always overwrite it with the identity from
    # the public HTTP contract so a retry cannot accidentally fork the send claim.
    payload["request_id"] = caller_request_id
    payload["_request_identity"] = caller_request_id
    op = str(payload.get("op") or "").strip().lower()
    try:
        _parent_sid = str(payload.get("parent_session_id") or "")
        _runtime_sid = str(payload.get("runtime_session_id") or "")
        if op == "list":
            _origin, denied = _forward_bridge_read_origin(
                str(payload.get("session_id") or ""),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
                op="list",
            )
            if denied:
                return web.json_response({"ok": False, "error": denied}, status=403)
            messages = _forward_bridge_list_messages(
                str(payload.get("session_id") or ""),
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit") or live.forward_bridge_max_messages),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "messages": messages})
        if op == "recent":
            _origin, denied = _forward_bridge_read_origin(
                str(payload.get("session_id") or ""),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
                op="recent",
            )
            if denied:
                return web.json_response({"ok": False, "error": denied}, status=403)
            recent = _forward_bridge_list_recent(
                str(payload.get("session_id") or ""),
                only_images=bool(payload.get("only_images", True)),
                limit=int(payload.get("limit") or 20),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "recent": recent})
        if op in {"forward", "send", "remind", "file", ""}:
            if op:
                payload.setdefault("action", op)
            result = await _forward_bridge_execute(payload)
            status = 200 if result.get("ok") else 400
            return web.json_response(result, status=status)
        return web.json_response({"ok": False, "error": f"unknown op: {op}"}, status=400)
    except Exception as exc:
        common.logger.warning("qq_forward bridge handler error: %s", exc, exc_info=True)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _start_forward_bridge() -> None:
    """在 QQ Bot 进程内启动 qq_forward Bridge Server。"""
    if state._adapter.forward_bridge_started or not live.enable_forward_bridge:
        return
    try:
        from aiohttp import web
    except Exception:
        common.logger.warning("qq_forward bridge disabled: aiohttp 未安装。")
        return
    if not _forward_bridge_token():
        common.logger.error(
            "qq_forward bridge disabled: 无法确定 Bridge 令牌（%s 不可写）",
            FORWARD_BRIDGE_TOKEN_FILE,
        )
        return
    app = web.Application()
    app.router.add_post("/qq_forward", _forward_bridge_http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, FORWARD_BRIDGE_HOST, FORWARD_BRIDGE_PORT)
    await site.start()
    state._adapter.forward_bridge_runner = runner
    state._adapter.forward_bridge_site = site
    state._adapter.forward_bridge_started = True
    common.logger.info(
        "qq_forward bridge server started: http://%s:%s/qq_forward (token source=%s)",
        FORWARD_BRIDGE_HOST,
        FORWARD_BRIDGE_PORT,
        "env" if FORWARD_BRIDGE_TOKEN else "file",
    )


async def _stop_forward_bridge() -> None:
    if state._adapter.forward_bridge_runner is not None:
        with contextlib.suppress(Exception):
            await state._adapter.forward_bridge_runner.cleanup()
    state._adapter.forward_bridge_runner = None
    state._adapter.forward_bridge_site = None
    state._adapter.forward_bridge_started = False
