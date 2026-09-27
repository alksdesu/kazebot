"""data/memory 下的目录名：会话记忆是 conv_<稳定键摘要>，人物档案是 user_<别名>。

engine、supervisor、适配器和离线迁移脚本都按这里算、按这里认，别处不再各抄一份。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

CONVERSATION_PREFIX = "conv_"
SUBJECT_PREFIX = "user_"

# 别名由适配层的匿名化计数器产生（UserA / UserAF / GroupB…）；限定字符集，拼进目录名时越不出 data/memory/。
_SUBJECT = r"[A-Za-z][A-Za-z0-9_]{0,63}"
_CONVERSATION_NAMESPACE = rf"{CONVERSATION_PREFIX}[0-9a-f]{{24}}"
_SUBJECT_RE = re.compile(_SUBJECT)
_MEMORY_NAMESPACE_RE = re.compile(rf"(?:{_CONVERSATION_NAMESPACE}|{SUBJECT_PREFIX}{_SUBJECT})")


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_valid_subject(subject: Any) -> bool:
    return _SUBJECT_RE.fullmatch(_text(subject)) is not None


def is_memory_namespace(value: Any) -> bool:
    """是不是两种目录名之一。真人会话键都带平台通道前缀和冒号，长不成这样。"""
    return _MEMORY_NAMESPACE_RE.fullmatch(_text(value)) is not None


def conversation_namespace(conversation_key: Any) -> str:
    """这个会话的记忆落在哪个目录，键为空时给空串。"""
    raw = _text(conversation_key)
    if not raw:
        return ""
    # 已经是目录名就原样返回：dream 拿要整理的目录名当 conversation_key 建任务，会话记忆和人物档案都是，
    # save_memory/delete_memory 才落回那个目录。
    if _MEMORY_NAMESPACE_RE.fullmatch(raw):
        return raw
    return CONVERSATION_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def subject_namespace(subject: Any) -> str:
    """一个人的档案目录名，别名不合规时给空串。"""
    text = _text(subject)
    return SUBJECT_PREFIX + text if is_valid_subject(text) else ""


def subject_from_namespace(namespace: Any) -> str:
    """subject_namespace 反过来：档案目录名还原成别名，其余给空串。"""
    text = _text(namespace)
    if not text.startswith(SUBJECT_PREFIX):
        return ""
    subject = text[len(SUBJECT_PREFIX):]
    return subject if _SUBJECT_RE.fullmatch(subject) else ""
