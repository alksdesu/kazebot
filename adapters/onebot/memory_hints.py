"""识别一轮消息里涉及哪些人，供按人加载长期记忆使用。

纯逻辑，不 import NoneBot：别名反查和名册判定都靠调用方注入，便于单测。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

# 别名由匿名化计数器产生：User + 字母编码（UserA / UserZ / UserAA / UserAF…）。
# 不能用 \b 划边界 —— 中文也是 \w，「他说UserAF很好」里 \b 不成立会漏掉。
# 显式要求两侧不是字母数字，这样 CJK 紧邻也能命中，而 UserA 不会从 UserAF 里切出来。
_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9])(User[A-Z]+)(?![A-Za-z0-9])")

# 显示名太短时在自由文本里搜索会大量误命中（有人叫「明」，「明天」就中了）。
_MIN_DISPLAY_NAME_LEN = 2

# 占位别名不对应任何一个人，进了 subjects 会让 engine 去建一份公共档案。
_PLACEHOLDER_ALIASES = frozenset({"UserUnknown", "AnonUnknown"})


def aliases_in_text(text: str) -> list[str]:
    """Return anonymized user aliases appearing in *text*, in first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _ALIAS_RE.finditer(str(text or "")):
        alias = match.group(1)
        if alias not in seen:
            seen.add(alias)
            result.append(alias)
    return result


def at_segment_user_ids(message: Iterable[Any] | None) -> list[str]:
    """Return the raw user ids of every `at` segment, skipping @全体成员."""
    result: list[str] = []
    for segment in message or []:
        seg_type = segment.get("type") if isinstance(segment, dict) else getattr(segment, "type", "")
        if str(seg_type) != "at":
            continue
        data = segment.get("data") if isinstance(segment, dict) else getattr(segment, "data", {})
        raw = str((data or {}).get("qq") or "").strip()
        # qq=all 是 @全体成员，不对应任何一个人。
        if raw and raw.lower() != "all" and raw not in result:
            result.append(raw)
    return result


def display_name_mentions(
    text: str, *, display_names: dict[str, str], exclude: Iterable[str] = (),
) -> list[str]:
    """Return enrolled aliases whose display name literally appears in *text*.

    只对已建档的人做这一步：候选集小，且这些人本来就该被记住。名字越长越可信，
    故按长度降序匹配，短名字让位给包含它的长名字。
    """
    body = str(text or "")
    skip = set(exclude)
    candidates = [
        (name, alias) for alias, name in display_names.items()
        if alias not in skip and len(str(name or "").strip()) >= _MIN_DISPLAY_NAME_LEN
    ]
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    result: list[str] = []
    for name, alias in candidates:
        if alias in result:
            continue
        if str(name).strip() in body:
            result.append(alias)
    return result


def collect_subjects(
    *,
    sender_alias: str,
    anonymized_text: str,
    at_user_ids: Iterable[str] = (),
    alias_of_user_id: Callable[[str], str] | None = None,
    display_names: dict[str, str] | None = None,
) -> list[str]:
    """Return every alias this turn involves, sender first.

    发起人排在最前：预算裁剪时他的档案最该留下。其余按 @ 段、文本里的别名、
    已建档者的显示名依次补充，全程去重并保持顺序稳定。
    """
    subjects: list[str] = []

    def _add(alias: Any) -> None:
        text = str(alias or "").strip()
        if text and text not in _PLACEHOLDER_ALIASES and text not in subjects:
            subjects.append(text)

    _add(sender_alias)
    if alias_of_user_id is not None:
        for user_id in at_user_ids:
            _add(alias_of_user_id(user_id))
    for alias in aliases_in_text(anonymized_text):
        _add(alias)
    if display_names:
        for alias in display_name_mentions(
            anonymized_text, display_names=display_names, exclude=subjects,
        ):
            _add(alias)
    return subjects
