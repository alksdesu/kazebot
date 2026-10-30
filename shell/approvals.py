"""shell 里审批定案的说法：人批的说同意或拒绝，没人定案、被系统收掉的说已失效和原因。"""
from __future__ import annotations

from typing import Any

# 没人定案、由系统按拒绝收掉的审批（任务先结束、等审批超时）附言以这个结尾
_AUTO_DENIED_COMMENT_MARK = "auto-denied"


def approval_settled_text(record: Any) -> str:
    """approval_decided 事件的载荷或审批回执，写成「已同意」「已拒绝」「已失效（原因）」。"""
    data = record if isinstance(record, dict) else {}
    reason = str(data.get("expired_reason") or "").strip()
    if reason:
        return f"已失效（{reason}）"
    if _AUTO_DENIED_COMMENT_MARK in str(data.get("comment") or "").lower():
        return "已失效"
    if data.get("decision") == "allow" or data.get("status") == "allowed":
        return "已同意"
    return "已拒绝"


def decision_took_effect(receipt: Any, decision: str) -> bool:
    """提交审批决定后的回执和这次决定对得上；回执读不懂时按生效算。"""
    if not isinstance(receipt, dict) or "status" not in receipt:
        return True
    expected = "allowed" if decision == "allow" else "denied"
    return receipt.get("status") == expected and not receipt.get("expired_reason")
