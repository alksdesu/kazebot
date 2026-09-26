"""平台描述的数据结构。只有数据，不导入任何平台库。"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

CHANNEL_GROUP = "group"
CHANNEL_PRIVATE = "private"
CHANNEL_BRIDGE = "bridge"
CHANNEL_OTHER = "other"
CHANNEL_KINDS: frozenset[str] = frozenset({CHANNEL_GROUP, CHANNEL_PRIVATE, CHANNEL_BRIDGE, CHANNEL_OTHER})
# 真人对话的两种会话；bridge 是转发桥这类系统会话，other 是认不出种类时的兜底前缀。
CONVERSATION_KINDS: frozenset[str] = frozenset({CHANNEL_GROUP, CHANNEL_PRIVATE})

ID_NUMERIC = "numeric"
ID_STRING = "string"

STATE_LIVE = "live"
STATE_ROUTES = "routes"
STATE_ALIASES = "aliases"
STATE_ACTIVE_ACCOUNT = "active_account"

HOOK_TRIGGER_POLICY = "trigger_policy"


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class ChannelDescriptor:
    # 会话通道名，同时是会话键前缀。
    name: str
    kind: str
    # 会话键是否为「前缀:摘要」。转发桥这类会话直接用真实目标，没有摘要。
    hashed: bool = True

    def __post_init__(self) -> None:
        if self.kind not in CHANNEL_KINDS:
            raise ValueError(f"unknown channel kind: {self.kind!r}")


@dataclass(frozen=True)
class PlatformDescriptor:
    id: str
    display_name: str
    # 称呼这个平台的用户时用的前缀，比如提醒署名「QQ 12345」。
    short_name: str
    # platform_auth.platform 的历史取值，读到它们都算这个平台。
    aliases: frozenset[str] = frozenset()
    channels: tuple[ChannelDescriptor, ...] = ()
    id_shape: str = ID_STRING
    # 平台配置文件，相对工作区；空串表示这个平台没有热载配置。
    config_path: str = ""
    # 覆盖配置路径的环境变量。只给已经在用它的平台，新平台不加。
    config_env: str = ""
    # data/ 下由适配器写、核心只读的状态文件：live 公布生效配置与运行事实，
    # routes 是稳定会话键到真实会话键，aliases 是匿名别名表，
    # active_account 是当前登录账号，会话键摘要按它分作用域。
    state_files: Mapping[str, str] = field(default_factory=dict)
    # 核心层需要调用的平台策略：名字 → 模块路径，按需导入。
    hooks: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id_shape not in {ID_NUMERIC, ID_STRING}:
            raise ValueError(f"unknown id shape: {self.id_shape!r}")
        names = [channel.name for channel in self.channels]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate channel names in platform {self.id!r}")
        object.__setattr__(self, "state_files", MappingProxyType(dict(self.state_files)))
        object.__setattr__(self, "hooks", MappingProxyType(dict(self.hooks)))

    def names(self) -> frozenset[str]:
        return frozenset({self.id, *self.aliases, *(channel.name for channel in self.channels)})

    def channel(self, name: Any) -> ChannelDescriptor | None:
        key = normalize_name(name)
        return next((channel for channel in self.channels if channel.name == key), None)

    def public_dict(self) -> dict[str, Any]:
        """给控制台看的那一份：不含模块路径、环境变量名这类部署细节。"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "short_name": self.short_name,
            "id_shape": self.id_shape,
            "channels": [
                {"name": channel.name, "kind": channel.kind, "hashed": channel.hashed}
                for channel in self.channels
            ],
            "has_config": bool(self.config_path),
            "hooks": sorted(self.hooks),
        }
