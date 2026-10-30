"""审批卡片的渲染与反查、引用快捷审批的入口、审批命令与回执的措辞、免审授权的回执与收回。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional
from nonebot.adapters.onebot.v11 import Bot, Event
from adapters._shared import approvals
from clonoth_sdk import APPROVAL_ALLOWED, APPROVAL_DENIED, APPROVAL_EXPIRED, APPROVAL_MISSING, APPROVAL_PENDING
from clonoth_sdk.types import (
    GRANT_NOTE_DISABLED,
    GRANT_NOTE_NO_APPROVER,
    GRANT_NOTE_NO_REQUESTER,
    GRANT_NOTE_NOT_REQUESTER,
    GRANT_NOTE_ONCE,
    GRANT_NOTE_PERMANENT_DISABLED,
    GRANT_NOTE_PERMANENT_NOT_ADMIN,
    ApprovalDecision,
)
from platforms.builtin import QQ_NAPCAT
from platforms.ids import normalize_id, same_id
from ..live_config import live
from ..segments import normalize_message_id

from . import common, identity, message_text, permissions, routing, sending, state


# 没人定案、由系统按拒绝收掉的审批（toolbox 等待超时、任务先结束了）附言里的固定说法；结构化字段里没有这一项。
_AUTO_DENIED_COMMENT_MARK = "auto-denied"
# toolbox 等审批超时收掉时附言里的说法，和任务结束后被收掉的区分开
_TIMED_OUT_COMMENT_MARK = "timed out"


def _book_decision(status: str, comment: str = "") -> str:
    """supervisor 的审批状态折成审批簿的定案。系统自动拒绝的记成失效：那不是谁批的，是没人批。"""
    if status == APPROVAL_ALLOWED:
        return approvals.DECISION_ALLOW
    if status == APPROVAL_DENIED and _AUTO_DENIED_COMMENT_MARK not in str(comment or "").lower():
        return approvals.DECISION_DENY
    return approvals.DECISION_EXPIRED


def _timed_out(status: str, comment: str = "") -> bool:
    """这次失效是不是因为等审批超时；任务先结束、被取消而收掉的不算。"""
    text = str(comment or "").lower()
    return status == APPROVAL_DENIED and _AUTO_DENIED_COMMENT_MARK in text and _TIMED_OUT_COMMENT_MARK in text


_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    APPROVAL_ALLOWED: "已同意",
    APPROVAL_DENIED: "已拒绝",
    APPROVAL_EXPIRED: "已失效",
    APPROVAL_PENDING: "仍待处理",
    APPROVAL_MISSING: "审批不存在",
})


def _decision_not_effective_notice(approval_id: str, status: str) -> str:
    """这次决定没有生效时的回执：别处已经定案、已超时，或 supervisor 上查无此审批。"""
    label = _STATUS_LABELS.get(status) or status or "未知"
    return f"该审批已被处理，当前状态：{label}（{approval_id}）"


# 卡片标题同时是“这条消息确实是审批卡片”的唯一证据，改文案要连反查一起改。
_APPROVAL_SUMMARY_HEADER = "【Clonoth 审批请求】"


_APPROVAL_CARD_ID_RE = re.compile(r"ID:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")


_APPROVAL_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# 引用了审批卡片但反查不出可决策目标时的回执：区别于“这就是一句普通聊天”。
_APPROVAL_REPLY_UNRESOLVED_HINT = (
    "这条审批已不在待处理列表（可能已被处理或已超时）。"
    "要处理其它审批请手动发送：/审批 同意 <ID>。"
)


_GRANT_DURATION_HINT = (
    "时长写法不对，这条审批没有处理。时长跟在审批 ID 后面，只认整数加单位："
    "30m、2h、1d、45分钟、2小时；只批这一次写「单次」，一直免审到收回写「永久」。\n"
    "例：/审批 同意 <ID> 2h"
)


_REVOKE_HINT = "提前收回：私聊发送 /审批 收回"


# 卡片发给发起这条请求的管理员本人时多这一句：永久授权只有他能拿到。
_PERMANENT_CARD_HINT = "这条是你自己发起的：回「同意 永久」，以后你发起的操作都不再审批，直到私聊发 /审批 收回。"


_GRANT_NOTE_TEXT: Mapping[str, str] = MappingProxyType({
    GRANT_NOTE_ONCE: "只同意了这一次，没有生成授权。",
    GRANT_NOTE_DISABLED: "只同意了这一次：策略的默认时长是 0，不带时长不授权；要授权就带上时长，例如「同意 30m」。",
    GRANT_NOTE_NOT_REQUESTER: "这条请求不是你发起的，只同意了这一次，没有生成授权。",
    GRANT_NOTE_NO_REQUESTER: "查不到这条请求是谁发起的，只同意了这一次，没有生成授权。",
    GRANT_NOTE_NO_APPROVER: "认不出审批人的身份，只同意了这一次，没有生成授权。",
    GRANT_NOTE_PERMANENT_NOT_ADMIN: "永久授权只给管理员名单里的人，只同意了这一次，没有生成授权。",
    GRANT_NOTE_PERMANENT_DISABLED: "策略关掉了永久授权，只同意了这一次，没有生成授权。",
})


# 要了永久、退成限时授权时补的一句，{minutes} 是实际给的时长。
_PERMANENT_FALLBACK_TEXT: Mapping[str, str] = MappingProxyType({
    GRANT_NOTE_PERMANENT_NOT_ADMIN: "永久授权只给管理员名单里的人，这次按上限 {minutes}授权。",
    GRANT_NOTE_PERMANENT_DISABLED: "策略关掉了永久授权，这次按上限 {minutes}授权。",
})


def _settled_approval_notice(approval_id: str) -> str:
    """引用/决策一条已处理审批时的回执文案。"""
    info = state._adapter.approvals.settled.get(approval_id) or {}
    decision = str(info.get("decision") or "")
    label = {
        approvals.DECISION_ALLOW: "已同意",
        approvals.DECISION_DENY: "已拒绝",
        approvals.DECISION_EXPIRED: "已超时失效" if info.get("timed_out") else "已失效",
    }.get(decision, "已处理")
    admin_id = info.get("admin_id")
    by = f"，由管理员 {admin_id}" if admin_id else ""
    return f"该审批已被处理：{approval_id}（{label}{by}）。"


def _extract_approval_id_from_text(text: str) -> str:
    """从被引用的审批卡片文本中提取 approval_id 候选；脱钩 pending，取不到返回空串。"""
    if not text:
        return ""
    m = _APPROVAL_CARD_ID_RE.search(text)
    if m:
        return m.group(1)
    m = _APPROVAL_UUID_RE.search(text)
    if m:
        return m.group(0)
    for aid in state._adapter.approvals.known_ids():
        if aid and aid in text:
            return aid
    return ""


async def _resolve_approval_target_by_quoted_text(bot: Bot, reply_message_id: Any) -> approvals.ApprovalReplyTarget:
    """get_msg 拉被引用消息，确认是 bot 自己发出的审批卡片后再取 approval_id。"""
    if reply_message_id is None:
        return approvals.ApprovalReplyTarget()
    try:
        reply_obj = await message_text._get_reply_message(bot, reply_message_id)
    except Exception:
        reply_obj = None
    if not isinstance(reply_obj, dict):
        return approvals.ApprovalReplyTarget()
    text = message_text._message_to_text_generic(message_text._payload_message(reply_obj))
    if _APPROVAL_SUMMARY_HEADER not in text:
        return approvals.ApprovalReplyTarget()
    sender = reply_obj.get("sender")
    sender_id = sender.get("user_id") if isinstance(sender, dict) else None
    if sender_id is None or normalize_message_id(sender_id) != normalize_message_id(getattr(bot, "self_id", None)):
        return approvals.ApprovalReplyTarget()
    return state._adapter.approvals.target_for(_extract_approval_id_from_text(text))


async def _resolve_approval_reply_target(bot: Bot, event: Event) -> approvals.ApprovalReplyTarget:
    """引用快捷审批的唯一入口：先 map 反查，再 get_msg 卡片校验，最后按 opt-in 唯一回退。"""
    book = state._adapter.approvals
    book.prune_expired()
    has_reply, reply_message_id = message_text._event_reply_reference(event)
    if not has_reply:
        return approvals.ApprovalReplyTarget()
    user_id = getattr(event, "user_id", None)
    target = book.resolve_card(reply_message_id, user_id)
    if target.matched():
        return target
    if permissions._can("approval", event):
        target = await _resolve_approval_target_by_quoted_text(bot, reply_message_id)
        if target.matched():
            return target
        if live.approval_reply_unique_fallback:
            only_id = book.unique_pending_for(user_id)
            if only_id:
                return approvals.ApprovalReplyTarget(approval_id=only_id, from_card=True)
    return approvals.ApprovalReplyTarget()


def _approval_summary(
    approval_id: str, operation: str, details: Dict[str, Any], *, recipient: Any = None,
) -> str:
    """生成发给 QQ 管理员 recipient 的审批摘要，限制长度并避免刷屏；请求是他本人发起的，多一句永久授权的说明。"""
    lines = [
        _APPROVAL_SUMMARY_HEADER,
        f"ID: {approval_id}",
        f"操作: {operation or 'unknown'}",
    ]
    for key in ("tool_name", "path", "command", "reason", "safety_level"):
        value = details.get(key)
        if value is not None and value != "":
            lines.append(f"{key}: {str(value)[:1000]}")
    args = details.get("args") or details.get("parameters")
    if args:
        lines.append(f"参数: {str(args)[:1500]}")
    lines.extend([
        "",
        "【快捷审批】直接引用(回复)本消息，发送“同意”或“拒绝”即可。",
        "同意时可带时长，如「同意 2h」：你自己发起的请求，这段时间里免审批；不带按默认时长，「同意 单次」只批这一次。",
    ])
    if recipient is not None and _requested_by(details, recipient):
        lines.append(_PERMANENT_CARD_HINT)
    lines.extend([
        f"或手动发送：/审批 同意 {approval_id}",
        f"　　　　　/审批 拒绝 {approval_id}",
    ])
    return sending._truncate_qq_text("\n".join(lines))


def _requested_by(details: Mapping[str, Any], admin_id: Any) -> bool:
    """这条审批是不是 admin_id 以管理员身份自己发起的。按 supervisor 写进 details 的发起人认，认法和它发永久授权时一致。"""
    auth = details.get("platform_auth")
    if not isinstance(auth, Mapping) or not bool(auth.get("is_admin")):
        return False
    me = _approver(admin_id)
    if str(auth.get("platform") or "").strip().lower() != me["platform"] or not same_id(auth.get("user_id"), me["user_id"]):
        return False
    theirs, mine = normalize_id(auth.get("bot_scope")), normalize_id(me["bot_scope"])
    return not (theirs and mine) or theirs == mine


def _parse_approval_command(text: str) -> Optional[approvals.ApprovalIntent]:
    """解析管理员私聊审批命令：/审批 同意 <ID> [时长]、/同意 <ID 前缀> [时长]。

    引用审批卡片回一个词那条路径不经过这里，仍然免斜杠 —— 卡片在，指向就是唯一的。
    """
    return approvals.parse_decision_command(common._strip_command_prefix(text))


def _is_revoke_command(text: str) -> bool:
    """/审批 收回：收回自己名下生效中的免审授权。和审批命令一样要带斜杠。"""
    return approvals.parse_revoke_command(common._strip_command_prefix(text))


_DAYS_LIKE_ID_RE = re.compile(r"([0-9]+)[dD]")


def _resolve_pending_approval_id(token: str, *, decision: str = "") -> tuple[Optional[str], str]:
    """允许管理员用完整 approval_id 或唯一前缀审批；命中已处理条目时告知而非误批。"""
    lookup = state._adapter.approvals.lookup(token)
    if lookup.outcome == approvals.LOOKUP_FOUND:
        return lookup.approval_id, ""
    if lookup.outcome == approvals.LOOKUP_NO_TOKEN:
        return None, "缺少审批 ID。"
    if lookup.outcome == approvals.LOOKUP_AMBIGUOUS:
        return None, f"审批 ID 前缀不唯一：{lookup.token}"
    if lookup.outcome == approvals.LOOKUP_SETTLED:
        return None, _settled_approval_notice(lookup.approval_id)
    days = _DAYS_LIKE_ID_RE.fullmatch(lookup.token)
    if days and decision == approvals.DECISION_ALLOW:
        # 「1d」「300d」既像 ID 前缀又像时长，按 ID 查过了；查不到就把时长该怎么写说清楚。
        return None, (
            f"没有找到待审批 ID：{lookup.token}。要带时长就写在 ID 后面：/审批 同意 <ID> {lookup.token}；"
            f"不指定 ID 时把单位写成「天」，例如「同意 {days[1]}天」。"
        )
    return None, f"没有找到待审批 ID：{lookup.token}"


def _approver(user_id: Any) -> Dict[str, str]:
    """提交审批决定时报给 supervisor 的审批人，认法和入站 platform_auth 一致，两边才对得上。"""
    return {
        "platform": QQ_NAPCAT.id,
        "user_id": identity._verified_user_id(user_id),
        "bot_scope": routing._active_bot_scope(),
    }


def _minutes_text(minutes: int) -> str:
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def _local_time(moment: datetime) -> str:
    return moment.astimezone(common._CST).strftime("%Y-%m-%d %H:%M")


def _grant_receipt(
    result: ApprovalDecision, requested_minutes: Optional[int], requested_permanent: bool = False,
) -> str:
    """同意之后授权的交代：生成了就写到期时间（或永久）和怎么收回，没生成、没按要的给就说为什么。"""
    grant = result.grant
    if grant is not None and grant.permanent:
        lines = ["已授权：以后你发起的操作都不再审批，敏感操作也一样，直到收回；移出管理员名单后自动失效。"]
        if not requested_permanent:
            lines.append("你原有的是永久授权，照旧有效。")
        lines.append(_REVOKE_HINT)
        return "\n".join(lines)
    if grant is None or grant.until is None:
        return _GRANT_NOTE_TEXT.get(result.grant_note, "")
    lines = [f"已授权到 {_local_time(grant.until)}：这段时间里你发起的操作免审批，敏感操作也一样。"]
    fallback = _PERMANENT_FALLBACK_TEXT.get(result.grant_note) if requested_permanent else None
    if fallback:
        lines.append(fallback.format(minutes=_minutes_text(grant.minutes)))
    elif requested_minutes is not None and grant.minutes < requested_minutes:
        lines.append(f"要的时长超过了上限，按上限 {_minutes_text(grant.minutes)}授权。")
    if grant.issued_at is not None and grant.until > grant.issued_at + timedelta(minutes=grant.minutes):
        lines.append("你原有的授权到期更晚，到期时间沿用原来的。")
    lines.append(_REVOKE_HINT)
    return "\n".join(lines)


def _decision_receipt(
    approval_id: str,
    decision: str,
    operation: Any,
    result: ApprovalDecision,
    requested_minutes: Optional[int],
    requested_permanent: bool = False,
) -> str:
    lines = [
        f"已{('同意' if decision == approvals.DECISION_ALLOW else '拒绝')}审批：{approval_id}",
        f"操作：{operation or 'unknown'}",
    ]
    if decision == approvals.DECISION_ALLOW:
        grant_text = _grant_receipt(result, requested_minutes, requested_permanent)
        if grant_text:
            lines.append(grant_text)
    return "\n".join(lines)


def _revoke_receipt(count: int) -> str:
    if count:
        return f"已收回你名下 {count} 条生效中的授权，之后你发起的操作照常审批。"
    return "你名下没有生效中的授权。"


async def _revoke_own_grants(user_id: Any) -> str:
    """收回本人名下生效中的授权，返回回执。"""
    try:
        revoked = await state._adapter.client.revoke_approval_grants(identity=_approver(user_id))
    except Exception as exc:
        common.logger.exception("revoke approval grants on QQ failed: admin=%s", user_id)
        return f"收回授权失败：{exc}"
    common.logger.info("approval grants revoked on QQ: admin=%s count=%s", user_id, len(revoked))
    return _revoke_receipt(len(revoked))
