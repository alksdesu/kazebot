"""平台侧 ID 的统一形态：一律是字符串，纯数字原样保留，其余只放行受控字符集。"""
from __future__ import annotations

import re
from typing import Any, Iterable

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


# 在文本里认已知 ID 时，两侧紧挨着的字符不能像是 ID 还没完，否则 12345 会命中 123456 的前半截。
# 字符串 ID 只看 ASCII 字母数字和下划线：@ 不算，「@某人」是聊天里的常见写法。
_TEXT_NEIGHBOURS: dict[str, str] = {
    ID_STRING: r"[A-Za-z0-9_]",
    ID_NUMERIC: r"\d",
}


def id_text_pattern(ids: Iterable[str], shape: str = ID_STRING) -> re.Pattern[str]:
    """匹配文本里任一已知 ID 的正则。长的排在前面，12345 与 123456 同在时先认长的。"""
    try:
        neighbour = _TEXT_NEIGHBOURS[shape]
    except KeyError:
        raise ValueError(f"unknown id shape: {shape!r}") from None
    keys = sorted(ids, key=len, reverse=True)
    if not keys:
        raise ValueError("id_text_pattern needs at least one id")
    return re.compile(rf"(?<!{neighbour})(?:{'|'.join(re.escape(key) for key in keys)})(?!{neighbour})")
