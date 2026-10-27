"""功能命令失败时回给用户的话：supervisor 给了原因就照转，其余只记日志，不把异常原文和内部地址发出去。"""
from __future__ import annotations

import logging

from clonoth_sdk.refusals import error_detail

logger = logging.getLogger(__name__)


def user_failure(error: BaseException, fallback: str) -> str:
    """supervisor 拒绝时写明的原因；没有写明的记下异常，回 fallback。"""
    reason = error_detail(error)
    if reason:
        return reason
    logger.warning("feature command failed", exc_info=error)
    return fallback
