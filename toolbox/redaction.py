"""认密钥、打码：日志、诊断导出、审批预览和调试工具的输出共用这一套。

只认得出带前缀的密钥、挂在敏感键名下的值和地址里的账号密码，认不出的照原样，是兜底而不是放心外传原文的理由。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import IntEnum
from typing import Any

_KEEP_TAIL = 4

# 各家密钥的前缀。命中就整段打码，只留尾巴几位供人对号。
_TOKEN_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"\b(AIza[A-Za-z0-9_\-]{10,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{10,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
)

# 底下的数字也当密钥藏的键名：口令常是纯数字，YAML 里读出来就是 int。
_NUMERIC_SECRET_KEY_NAMES = r"api.?key|secret|password|passwd|authorization|bearer"
# 认得出是密钥的键名。日志按「键名=值」打码，诊断导出按配置里的键名收值、藏字段，审批预览和调试输出按它藏值，用的是这同一张表。
# token 不进上一档：它底下的数字多是 max_tokens 这类上限。
SENSITIVE_KEY_NAMES = rf"{_NUMERIC_SECRET_KEY_NAMES}|token"
SENSITIVE_KEY = re.compile(SENSITIVE_KEY_NAMES, re.IGNORECASE)
NUMERIC_SECRET_KEY = re.compile(_NUMERIC_SECRET_KEY_NAMES, re.IGNORECASE)

# 短于这个长度的值不当密钥：诊断导出拿收到的值按子串替换，太短的会把不相干的字也抹掉。
MIN_SECRET_CHARS = 6

# 整个值藏起来时换上的字样。
HIDDEN = "[已隐藏]"

# 键值对写法。键名认得出就不管值长什么样 —— 中转站的 key 可以是任意串。
_ASSIGN_PATTERN = re.compile(
    rf"((?:{SENSITIVE_KEY_NAMES})\s*[=:]\s*)(['\"]?)([^\s'\",;)]{{{MIN_SECRET_CHARS},}})",
    re.IGNORECASE,
)

# URL 里带的密钥，Gemini 那套 ?key= 就是这么传的。
_QUERY_PATTERN = re.compile(
    rf"([?&](?:key|api[_-]?key|access[_-]?token)=)([^\s&'\"]{{{MIN_SECRET_CHARS},}})",
    re.IGNORECASE,
)

_AUTH_SCHEME_PATTERN = re.compile(r"\b((?:Bearer|Basic)\s+)([^\s'\",;]+)", re.IGNORECASE)

# 地址里的 user:pass@ 整段藏掉，不留尾巴：留下的就是密码的最后几位。
_USERINFO_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")


def _mask(value: str) -> str:
    if len(value) <= _KEEP_TAIL:
        return "*" * len(value)
    return "*" * 6 + value[-_KEEP_TAIL:]


def redact(text: str) -> str:
    """把看得出是密钥的东西打码。宁可多打，看日志的人不需要那几位。"""
    text = _USERINFO_PATTERN.sub(lambda match: match.group(1) + HIDDEN + "@", text)
    text = _AUTH_SCHEME_PATTERN.sub(lambda match: match.group(1) + _mask(match.group(2)), text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: _mask(m.group(1)), text)
    text = _ASSIGN_PATTERN.sub(lambda m: m.group(1) + m.group(2) + _mask(m.group(3)), text)
    text = _QUERY_PATTERN.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
    return text


def assigned_values(text: str) -> list[str]:
    """按「敏感键名=值」认出来的值，按出现顺序。redact 按键名打码的就是这些。"""
    return [match.group(3) for match in _ASSIGN_PATTERN.finditer(text)]


class _Hide(IntEnum):
    """mask_sensitive 往下走时藏到哪一档，经过敏感键只升不降。"""

    NOTHING = 0
    TEXT = 1
    TEXT_AND_NUMBERS = 2


def _hide_below(key: Any) -> _Hide:
    name = str(key)
    if NUMERIC_SECRET_KEY.search(name):
        return _Hide.TEXT_AND_NUMBERS
    if SENSITIVE_KEY.search(name):
        return _Hide.TEXT
    return _Hide.NOTHING


def mask_sensitive(value: Any) -> Any:
    """value 的副本：键名认得出是密钥的那一支里，非空的字符串换成 HIDDEN，别处的字符串过 redact。

    口令、secret、api key、authorization、bearer 那一支里的数字也换掉：口令常是纯数字，YAML 读出来是 int。
    键名只认得出 token 的那一支，数字照给，多是 max_tokens 这类上限。
    键和层次不变，空值和布尔照原样，看的人分得清配没配、配在哪；元组和集合变成列表，原对象不动。
    """
    return _masked(value, _Hide.NOTHING)


def _masked(value: Any, hide: _Hide) -> Any:
    if isinstance(value, Mapping):
        return {key: _masked(item, max(hide, _hide_below(key))) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_masked(item, hide) for item in value]
    if isinstance(value, str):
        return HIDDEN if hide >= _Hide.TEXT and value.strip() else redact(value)
    if isinstance(value, (bytes, bytearray)):
        return HIDDEN if hide >= _Hide.TEXT and value else value
    # bool 是 int 的子类，开关照给。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return HIDDEN if hide >= _Hide.TEXT_AND_NUMBERS else value
    return value
