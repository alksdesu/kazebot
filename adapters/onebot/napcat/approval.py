"""审批卡片、引用快捷审批与审批命令解析。"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional
from nonebot.adapters.onebot.v11 import Bot, Event
from ..config import PENDING_APPROVAL_TTL_SECONDS
from ..live_config import live

from . import common, message_text, models, permissions, sending, state


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


_APPROVAL_MSG_MAP_MAX = 500


_SETTLED_APPROVAL_MAX = 200


def _normalize_msg_id(message_id: Any) -> str:
    """将消息 id 归一化为稳定字符串 key。

    Why: NapCat/OneBot 上报的 reply id 与 send_private_msg 返回的
    message_id 可能分别以 int / 带小数点浮点字符串 / 带空白的形式出现，直接
    str() 对比会因“-123 vs -123.0”等差异而反查失败。How: 先 strip，再尝试按
    整数归一化（容忍 '-123.0' 这类）。Purpose: 写入与反查使用同一归一化规则，
    避免引用快捷审批因 id 格式差异而失败。
    """
    if message_id is None:
        return ""
    key = str(message_id).strip()
    if not key:
        return ""
    try:
        return str(int(float(key))) if any(c in key for c in ".") else str(int(key))
    except (ValueError, TypeError):
        return key


def _remember_approval_message(message_id: Any, approval_id: str, admin_id: int) -> None:
    """记录审批消息 id -> 卡片锚点（approval_id + 收件管理员），供引用回复审批时反查校验。"""
    if not approval_id:
        return
    key = _normalize_msg_id(message_id)
    if not key:
        return
    try:
        admin = int(admin_id)
    except (TypeError, ValueError):
        return
    state._adapter.approval_message_ids[key] = models._ApprovalCard(approval_id=approval_id, admin_id=admin)
    state._adapter.approval_message_ids.move_to_end(key)
    while len(state._adapter.approval_message_ids) > _APPROVAL_MSG_MAP_MAX:
        state._adapter.approval_message_ids.popitem(last=False)


def _approval_card_recipients(approval_id: str) -> set[int]:
    """这条审批的卡片实际发给了哪些管理员，用于唯一回退时校验收件人。"""
    if not approval_id:
        return set()
    return {
        card.admin_id
        for card in state._adapter.approval_message_ids.values()
        if card.approval_id == approval_id
    }


def _record_settled_approval(
    approval_id: str, *, admin_id: int, decision: str, operation: str = "",
) -> None:
    """登记一条已决策/已失效的审批，供重复决策与引用已处理卡片时告知而非误批。"""
    if not approval_id:
        return
    state._adapter.settled_approvals[approval_id] = {
        "decision": decision,
        "admin_id": admin_id,
        "at": time.time(),
        "operation": operation,
    }
    state._adapter.settled_approvals.move_to_end(approval_id)
    while len(state._adapter.settled_approvals) > _SETTLED_APPROVAL_MAX:
        state._adapter.settled_approvals.popitem(last=False)


def _settled_approval_notice(approval_id: str) -> str:
    """引用/决策一条已处理审批时的回执文案。"""
    info = state._adapter.settled_approvals.get(approval_id) or {}
    decision = str(info.get("decision") or "")
    label = {"allow": "已同意", "deny": "已拒绝", "expired": "已超时失效"}.get(decision, "已处理")
    admin_id = info.get("admin_id")
    by = f"，由管理员 {admin_id}" if admin_id else ""
    return f"该审批已被处理：{approval_id}（{label}{by}）。"


def _prune_expired_approvals() -> None:
    """把超过 TTL 的待审批移出 pending 并登记为已失效；engine 侧已 auto-deny，这里同步收尾。"""
    if not state._adapter.pending_approvals:
        return
    now = time.time()
    expired: List[str] = []
    for aid, info in state._adapter.pending_approvals.items():
        created = float(info.get("created_at") or 0) or now
        if now - created > PENDING_APPROVAL_TTL_SECONDS:
            expired.append(aid)
    for aid in expired:
        info = state._adapter.pending_approvals.pop(aid, {})
        _record_settled_approval(
            aid, admin_id=0, decision="expired", operation=str(info.get("operation") or ""),
        )


def _resolve_approval_target_by_reply(reply_message_id: Any, user_id: int) -> models._ApprovalReplyTarget:
    """按 message_id 反查审批卡片：命中且收件人是本人时确认 from_card，再落 pending / settled。"""
    key = _normalize_msg_id(reply_message_id)
    if not key:
        return models._ApprovalReplyTarget()
    card = state._adapter.approval_message_ids.get(key)
    if card is None:
        return models._ApprovalReplyTarget()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return models._ApprovalReplyTarget()
    if card.admin_id != uid:
        return models._ApprovalReplyTarget()
    if card.approval_id in state._adapter.pending_approvals:
        return models._ApprovalReplyTarget(approval_id=card.approval_id, from_card=True)
    if card.approval_id in state._adapter.settled_approvals:
        return models._ApprovalReplyTarget(settled_id=card.approval_id, from_card=True)
    return models._ApprovalReplyTarget(from_card=True)


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
    for aid in list(state._adapter.pending_approvals) + list(state._adapter.settled_approvals):
        if aid and aid in text:
            return aid
    return ""


async def _resolve_approval_target_by_quoted_text(bot: Bot, reply_message_id: Any) -> models._ApprovalReplyTarget:
    """get_msg 拉被引用消息，确认是 bot 自己发出的审批卡片后再取 approval_id。"""
    if reply_message_id is None:
        return models._ApprovalReplyTarget()
    try:
        reply_obj = await message_text._get_reply_message(bot, reply_message_id)
    except Exception:
        reply_obj = None
    if not isinstance(reply_obj, dict):
        return models._ApprovalReplyTarget()
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
        return models._ApprovalReplyTarget()
    sender = reply_obj.get("sender")
    sender_id = sender.get("user_id") if isinstance(sender, dict) else None
    if sender_id is None or _normalize_msg_id(sender_id) != _normalize_msg_id(getattr(bot, "self_id", None)):
        return models._ApprovalReplyTarget()
    aid = _extract_approval_id_from_text(text)
    if aid and aid in state._adapter.pending_approvals:
        return models._ApprovalReplyTarget(approval_id=aid, from_card=True)
    if aid and aid in state._adapter.settled_approvals:
        return models._ApprovalReplyTarget(settled_id=aid, from_card=True)
    return models._ApprovalReplyTarget(from_card=True)


async def _resolve_approval_reply_target(bot: Bot, event: Event) -> models._ApprovalReplyTarget:
    """引用快捷审批的唯一入口：先 map 反查，再 get_msg 卡片校验，最后按 opt-in 唯一回退。"""
    _prune_expired_approvals()
    has_reply, reply_message_id = message_text._scan_reply_reference(
        event.get_message(), getattr(event, "raw_message", None),
    )
    if not has_reply:
        return models._ApprovalReplyTarget()
    user_id = getattr(event, "user_id", None)
    target = _resolve_approval_target_by_reply(reply_message_id, user_id)
    if target.matched():
        return target
    if permissions._can("approval", event):
        target = await _resolve_approval_target_by_quoted_text(bot, reply_message_id)
        if target.matched():
            return target
        if live.approval_reply_unique_fallback and len(state._adapter.pending_approvals) == 1:
            only_id = next(iter(state._adapter.pending_approvals))
            uid = permissions._as_qq_id(user_id)
            if uid is not None and uid in _approval_card_recipients(only_id):
                return models._ApprovalReplyTarget(approval_id=only_id, from_card=True)
    return models._ApprovalReplyTarget()


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


_APPROVAL_ALLOW_VERBS = frozenset({
    "同意", "批准", "通过", "允许", "allow", "approve", "approved", "ok", "yes", "y",
})


_APPROVAL_DENY_VERBS = frozenset({
    "拒绝", "驳回", "不同意", "不批准", "deny", "reject", "rejected", "no", "n",
})


_APPROVAL_COMMAND_PREFIXES = ("审批", "approval")


_APPROVAL_VERB_STRIP = " 　。，、！!.~"


# 形状与 approval_id（uuid4）绑定：裸动词命令只在 token 像 id 时才当审批，避免劫持普通私聊。
_APPROVAL_TOKEN_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{3,}$")


def _approval_verb(token: str) -> Optional[str]:
    """两个入口唯一的动词判定：整词匹配，返回 'allow' / 'deny' / None。"""
    word = (token or "").strip().strip(_APPROVAL_VERB_STRIP).lower()
    if not word:
        return None
    if word in _APPROVAL_DENY_VERBS:
        return "deny"
    if word in _APPROVAL_ALLOW_VERBS:
        return "allow"
    return None


def _parse_approval_command(text: str) -> Optional[tuple[str, str]]:
    """解析管理员私聊审批命令，返回 (decision, approval_id_or_prefix)。

    引用审批卡片回一个词那条路径不经过这里，仍然免斜杠 —— 卡片在，指向就是唯一的。
    """
    normalized = re.sub(r"\s+", " ", common._strip_command_prefix(text))
    if not normalized:
        return None
    parts = normalized.split(" ")
    if len(parts) < 2:
        return None
    if parts[0] in _APPROVAL_COMMAND_PREFIXES and len(parts) >= 3:
        decision = _approval_verb(parts[1])
        token = parts[2]
        explicit_prefix = True
    else:
        decision = _approval_verb(parts[0])
        token = parts[1]
        explicit_prefix = False
    if decision is None:
        return None
    # 裸动词命令 token 必须像 approval_id，否则“通过 微信发给我”会劫持普通私聊。
    if not explicit_prefix and not _APPROVAL_TOKEN_RE.match(token):
        return None
    return decision, token


def _parse_approval_reply_verb(text: str) -> Optional[str]:
    """引用回复里的审批意图；整句必须就是一个动词。返回 'allow' / 'deny' / None。"""
    # 子串匹配会让 know/note/now 命中 no，改整词。
    normalized = re.sub(r"\s+", "", (text or "").strip()).lower()
    if not normalized:
        return None
    for prefix in _APPROVAL_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].lstrip(":：")
    return _approval_verb(normalized)


def _unique_pending_approval_for(user_id: Any) -> str:
    """恰好一条待审批、且那张卡片确实发给本人时返回它的 id，否则空串。"""
    _prune_expired_approvals()
    if len(state._adapter.pending_approvals) != 1:
        return ""
    only_id = next(iter(state._adapter.pending_approvals))
    uid = permissions._as_qq_id(user_id)
    if uid is None or uid not in _approval_card_recipients(only_id):
        return ""
    return only_id


def _pending_approvals_for(user_id: Any) -> int:
    """本人收到过卡片的待审批条数，用于裸动词歧义时给出可操作的提示。"""
    uid = permissions._as_qq_id(user_id)
    if uid is None:
        return 0
    return sum(1 for aid in state._adapter.pending_approvals if uid in _approval_card_recipients(aid))


def _resolve_pending_approval_id(token: str) -> tuple[Optional[str], str]:
    """允许管理员用完整 approval_id 或唯一前缀审批；命中已处理条目时告知而非误批。"""
    _prune_expired_approvals()
    token = (token or "").strip()
    if not token:
        return None, "缺少审批 ID。"
    if token in state._adapter.pending_approvals:
        return token, ""
    matches = [aid for aid in state._adapter.pending_approvals if aid.startswith(token)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"审批 ID 前缀不唯一：{token}"
    settled = [aid for aid in state._adapter.settled_approvals if aid == token or aid.startswith(token)]
    if len(settled) == 1:
        return None, _settled_approval_notice(settled[0])
    return None, f"没有找到待审批 ID：{token}"
