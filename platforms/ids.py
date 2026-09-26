"""平台侧 ID 的统一形态：一律是字符串，纯数字原样保留，其余只放行受控字符集。"""
from __future__ import annotations

import re
from typing import Any

from .descriptor import ID_NUMERIC, ID_STRING

# 不收冒号和斜杠：ID 会拼进 `group:<id>` 这类身份串和落盘路径。
ID_BODY = r"-?[A-Za-z0-9][A-Za-z0-9_.@\-]{0,127}"
_SHAPES: dict[str, re.Pattern[str]] = {
    ID_STRING: re.compile(ID_BODY),
    ID_NUMERIC: re.compile(r"[0-9]{1,128}"),
}


def normalize_id(value: Any, shape: str = ID_STRING) -> str:
    """规范化成字符串 ID；取不到或不合法一律返回空串。纯数字输入输出不变，已有摘要不受影响。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        return ""
    try:
        pattern = _SHAPES[shape]
    except KeyError:
        raise ValueError(f"unknown id shape: {shape!r}") from None
    return raw if pattern.fullmatch(raw) else ""


def same_id(left: Any, right: Any) -> bool:
    """两个 ID 是否指向同一个对象。任一方不合法都不算相同。"""
    a = normalize_id(left)
    return bool(a) and a == normalize_id(right)
