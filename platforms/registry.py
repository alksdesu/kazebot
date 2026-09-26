"""平台注册表。

每个接入平台一份纯数据描述，核心层、工具和适配器都从这里认平台，不再各写一份 qq_ 判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class PlatformDescriptor:
    id: str
    display_name: str
    # platform_auth.platform 的历史取值，读到它们都算这个平台。
    aliases: frozenset[str] = frozenset()
    # 会话通道名，也是会话键的前缀。
    channels: frozenset[str] = frozenset()

    def names(self) -> frozenset[str]:
        return frozenset({self.id, *self.aliases, *self.channels})


QQ_NAPCAT = PlatformDescriptor(
    id="qq",
    display_name="QQ · NapCat",
    aliases=frozenset({"onebot", "onebot11"}),
    channels=frozenset({"qq_group", "qq_private", "qq_forward"}),
)

_BUILTIN: tuple[PlatformDescriptor, ...] = (QQ_NAPCAT,)


def builtin_platforms() -> tuple[PlatformDescriptor, ...]:
    return _BUILTIN


def find_platform(name: Any) -> PlatformDescriptor | None:
    """按平台 id、历史别名或会话通道名找平台，找不到返回 None。"""
    key = normalize_name(name)
    if not key:
        return None
    for platform in _BUILTIN:
        if key in platform.names():
            return platform
    return None
