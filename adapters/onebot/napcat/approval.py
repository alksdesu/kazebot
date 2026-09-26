"""审批卡片的渲染与反查、引用快捷审批的入口、审批命令与回执的措辞。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from nonebot.adapters.onebot.v11 import Bot, Event
from adapters._shared import approvals
from ..live_config import live
from ..segments import normalize_message_id

from . import common, message_text, permissions, sending, state


# 卡片标题同时是“这条消息确实是审批卡片”的唯一证据，改文案要连反查一起改。
_APPROVAL_SUMMARY_HEADER = "【Clonoth 审批请求】"


_APPROVAL_CARD_ID_RE = re.compile(r"ID:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")


_APPROVAL_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# 引用了审批卡片但反查不出可决策目标时的回执：区别于“这就是一句普通聊天”。
_APPROVAL_REPLY_UNRESOLVED_HINT = (
    "这条审批已不在待处理列表（可能已被处理或已超时）。"
    "要处理其它审批请手动发送：审批 同意 <ID>。"
)


def _settled_approval_notice(approval_id: str) -> str:
    """引用/决策一条已处理审批时的回执文案。"""
    info = state._adapter.approvals.settled.get(approval_id) or {}
    decision = str(info.get("decision") or "")
    label = {
        approvals.DECISION_ALLOW: "已同意",
        approvals.DECISION_DENY: "已拒绝",
        approvals.DECISION_EXPIRED: "已超时失效",
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
    parts: List[str] = []
    for key in ("raw_message", "message"):
        val = reply_obj.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            for seg in val:
                data = seg.get("data") if isinstance(seg, dict) else None
                if isinstance(data, dict):
                    txt = data.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
    text = "\n".join(parts)
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
    has_reply, reply_message_id = message_text._scan_reply_reference(
        event.get_message(), getattr(event, "raw_message", None),
    )
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


def _approval_summary(approval_id: str, operation: str, details: Dict[str, Any]) -> str:
    """生成发给 QQ 管理员的审批摘要，限制长度并避免刷屏。"""
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
        f"或手动发送：审批 同意 {approval_id}",
        f"　　　　　审批 拒绝 {approval_id}",
    ])
    return sending._truncate_qq_text("\n".join(lines))


def _parse_approval_command(text: str) -> Optional[tuple[str, str]]:
    """解析管理员私聊审批命令，返回 (decision, approval_id_or_prefix)。

    引用审批卡片回一个词那条路径不经过这里，仍然免斜杠 —— 卡片在，指向就是唯一的。
    """
    return approvals.parse_command(common._strip_command_prefix(text))


def _resolve_pending_approval_id(token: str) -> tuple[Optional[str], str]:
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
    return None, f"没有找到待审批 ID：{lookup.token}"
