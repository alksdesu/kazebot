"""平台注册表。

核心层、工具和适配器都从这里认平台，不再各写一份 qq_ 判断；前端经 /v1/platforms 读同一份。
"""
from __future__ import annotations

import importlib
import os
import re
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Collection

from .builtin import BUILTIN_PLATFORMS
from .descriptor import (
    CONVERSATION_KINDS,
    ChannelDescriptor,
    PlatformDescriptor,
    normalize_name,
)

# 稳定会话键的摘要部分，与适配器侧 conversation_hash.digest 的输出同形。
DIGEST_PATTERN = r"[0-9a-f]{24}"
# 全平台共用的功能设置。核心层和取平台钩子一样经这里按需导入，不直接 import 适配器包。
FEATURES_MODULE = "adapters._shared.features"

_lock = threading.Lock()
_platforms: dict[str, PlatformDescriptor] = {}
_by_name: dict[str, PlatformDescriptor] = {}


def _index_locked() -> None:
    _by_name.clear()
    for platform in _platforms.values():
        for name in platform.names():
            _by_name[name] = platform


def register_platform(platform: PlatformDescriptor, *, replace: bool = False) -> None:
    """登记一个平台。平台 id、别名、通道名在所有平台之间都不能重名，否则来源判定会认错。"""
    with _lock:
        if platform.id in _platforms and not replace:
            raise ValueError(f"platform {platform.id!r} is already registered")
        claimed = {
            name: other.id
            for other in _platforms.values() if other.id != platform.id
            for name in other.names()
        }
        clash = sorted(name for name in platform.names() if name in claimed)
        if clash:
            raise ValueError(f"names already claimed by other platforms: {clash}")
        _platforms[platform.id] = platform
        _index_locked()


def unregister_platform(platform_id: Any) -> None:
    with _lock:
        _platforms.pop(normalize_name(platform_id), None)
        _index_locked()


def _register_builtins() -> None:
    for platform in BUILTIN_PLATFORMS:
        register_platform(platform)


_register_builtins()


def all_platforms() -> tuple[PlatformDescriptor, ...]:
    with _lock:
        return tuple(_platforms.values())


def get_platform(platform_id: Any) -> PlatformDescriptor:
    with _lock:
        platform = _platforms.get(normalize_name(platform_id))
    if platform is None:
        raise KeyError(platform_id)
    return platform


def find_platform(name: Any) -> PlatformDescriptor | None:
    """按平台 id、历史别名或会话通道名找平台，找不到返回 None。"""
    key = normalize_name(name)
    if not key:
        return None
    with _lock:
        return _by_name.get(key)


def split_conversation_key(conversation_key: Any) -> tuple[str, str]:
    """(前缀, 身份)。没有冒号或身份为空时前缀也返回空串。"""
    head, sep, identity = str(conversation_key or "").strip().partition(":")
    identity = identity.strip()
    return (normalize_name(head), identity) if sep and identity else ("", "")


def conversation_key_prefix(conversation_key: Any) -> str:
    return split_conversation_key(conversation_key)[0]


def conversation_channel(conversation_key: Any) -> tuple[PlatformDescriptor, ChannelDescriptor] | None:
    prefix = conversation_key_prefix(conversation_key)
    platform = find_platform(prefix)
    channel = platform.channel(prefix) if platform is not None else None
    return (platform, channel) if channel is not None else None


def is_platform_conversation(
    conversation_key: Any, *, kinds: Collection[str] = CONVERSATION_KINDS,
) -> bool:
    found = conversation_channel(conversation_key)
    return found is not None and found[1].kind in kinds


def platform_channels(
    *, kinds: Collection[str] = CONVERSATION_KINDS, hashed: bool | None = None,
) -> tuple[ChannelDescriptor, ...]:
    return tuple(
        channel
        for platform in all_platforms()
        for channel in platform.channels
        if channel.kind in kinds and (hashed is None or channel.hashed is hashed)
    )


def stable_key_pattern(*, kinds: Collection[str] = CONVERSATION_KINDS) -> re.Pattern[str] | None:
    """匹配「通道前缀:摘要」形态的稳定会话键；没有任何平台登记这类通道时返回 None。"""
    names = sorted({channel.name for channel in platform_channels(kinds=kinds, hashed=True)})
    if not names:
        return None
    return re.compile(rf"(?:{'|'.join(re.escape(name) for name in names)}):{DIGEST_PATTERN}")


def platform_config_path(platform: PlatformDescriptor, workspace_root: Path) -> Path | None:
    if not platform.config_path:
        return None
    override = os.environ.get(platform.config_env, "").strip() if platform.config_env else ""
    return Path(override) if override else Path(workspace_root) / platform.config_path


def required_config_path(platform: PlatformDescriptor, workspace_root: Path) -> Path:
    path = platform_config_path(platform, workspace_root)
    if path is None:
        raise LookupError(f"platform {platform.id!r} has no config file")
    return path


def platform_state_path(platform: PlatformDescriptor, name: str, workspace_root: Path) -> Path | None:
    file_name = platform.state_files.get(name)
    return Path(workspace_root) / "data" / file_name if file_name else None


def platform_hook(platform: PlatformDescriptor, name: str) -> ModuleType:
    """按需导入平台策略模块。适配器包导入没有副作用，这里才能直接 import。"""
    module_path = platform.hooks.get(name)
    if not module_path:
        raise LookupError(f"platform {platform.id!r} has no {name!r} hook")
    return importlib.import_module(module_path)


def features_module() -> ModuleType:
    return importlib.import_module(FEATURES_MODULE)
