"""审批 — 已改为内联覆盖层，在 app.py 中实现。此文件保留兼容导入。

ApprovalResult 仍可从此处导入。
"""
from __future__ import annotations


class ApprovalResult:
    """审批结果。"""
    def __init__(self, decision: str, approval_id: str) -> None:
        self.decision = decision
        self.approval_id = approval_id
