"""群复读判定：连着几个人刷同一句，Bot 也跟一条。

判定放在纯函数里，因为难的不是「文本相同」，而是那几条不该跟的边界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# 命令必须以 / 开头，跟着复读等于替别人把命令又发一遍。
_COMMAND_PREFIXES = ("/", "／")


@dataclass(frozen=True)
class EchoRules:
    """平台给的复读边界。正文里怎么写 @、回复、卡片，怎么渲染图片占位，每个平台都不一样。"""

    # @ 谁、回复谁、卡片分享都带明确指向，复读出去指向就错人了。
    unsafe_markers: tuple[str, ...] = ()
    # 占位符本身不是内容：三个人各发一张不同的图，正文都是同一个占位符，光比文本会判成复读。
    placeholder: re.Pattern[str] | None = None


@dataclass(frozen=True)
class EchoEntry:
    """一条参与复读判定的消息。key 是内容指纹，不是展示用的正文。"""

    key: str
    sender_id: str
    created_at: float


def is_echoable_text(text: str, *, max_length: int, rules: EchoRules = EchoRules()) -> bool:
    """这句话本身能不能被跟。"""
    value = str(text or "").strip()
    if not value or len(value) > max_length:
        return False
    if value.startswith(_COMMAND_PREFIXES):
        return False
    if rules.placeholder is not None and rules.placeholder.match(value):
        return False
    upper = value.upper()
    return not any(marker.upper() in upper for marker in rules.unsafe_markers)


def detect_echo(
    entries: Iterable[EchoEntry],
    *,
    threshold: int,
    now: float,
    max_age_seconds: float,
    already_echoed: str = "",
) -> str:
    """够不够复读；够就返回那条 key，不够返回空串。

    要求最后 threshold 条 key 完全相同、且分别来自不同的人 —— 同一个人连刷三条是
    刷屏不是接龙，跟上去只会火上浇油。
    """
    items = [item for item in entries if now - item.created_at <= max_age_seconds]
    if threshold < 2 or len(items) < threshold:
        return ""
    tail = items[-threshold:]
    key = tail[0].key
    if not key or any(item.key != key for item in tail):
        return ""
    # 跟过一次就打住：不然人再补一条又凑够数，Bot 会一路接力下去。
    if key == already_echoed:
        return ""
    senders = {item.sender_id for item in tail}
    if len(senders) < threshold:
        return ""
    return key
