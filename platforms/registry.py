"""平台注册表。

核心层、工具和适配器都从这里认平台，不再各写一份 qq_ 判断；前端经 /v1/platforms 读同一份。
"""
from __future__ import annotations

import importlib
import os
import re
import threading
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Collection, Mapping

from .builtin import BUILTIN_PLATFORMS
from .descriptor import (
    CONVERSATION_KINDS,
    STATE_ACTIVE_ACCOUNT,
    STATE_ALIASES,
    STATE_LIVE,
    STATE_ROUTES,
    ChannelDescriptor,
    PlatformDescriptor,
    normalize_name,
)

# 稳定会话键的摘要部分，与适配器侧 conversation_hash.digest 的输出同形。
DIGEST_PATTERN = r"[0-9a-f]{24}"
# 全平台共用的功能设置。核心层和取平台钩子一样经这里按需导入，不直接 import 适配器包。
FEATURES_MODULE = "adapters._shared.features"
# 平台自己的数据按平台 id 分子目录放在这下面，文件名不用再带平台前缀。
PLATFORM_DATA_ROOT = "data/platforms"
STANDARD_STATE_FILES: Mapping[str, str] = MappingProxyType({
    STATE_LIVE: "live.json",
    STATE_ROUTES: "routes.json",
    STATE_ALIASES: "aliases.json",
    STATE_ACTIVE_ACCOUNT: "active_account.json",
})
# 全平台共用的别名编号：记忆跨平台共享，UserA 在哪个平台都只能是同一个人。平台 id 不带点，不会和它撞名。
ALIAS_NUMBERING_FILE = "alias_numbering.json"

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


def workspace_path(raw: str, workspace_root: Path) -> Path:
    """env 里写的路径。相对写法按工作区算：bot 和 supervisor 都以工作区为 cwd，离线工具不一定。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(workspace_root) / path


def env_override(name: str, environ: Mapping[str, str] | None = None) -> str:
    """平台声明的某个改位置的环境变量眼下的值，没声明或没设都是空串。environ 不给就读进程环境。"""
    if not name:
        return ""
    source = os.environ if environ is None else environ
    return str(source.get(name, "") or "").strip()


def platform_config_path(
    platform: PlatformDescriptor, workspace_root: Path, *, environ: Mapping[str, str] | None = None,
) -> Path | None:
    if not platform.config_path:
        return None
    override = env_override(platform.config_env, environ)
    return workspace_path(override, workspace_root) if override else Path(workspace_root) / platform.config_path


def required_config_path(platform: PlatformDescriptor, workspace_root: Path) -> Path:
    path = platform_config_path(platform, workspace_root)
    if path is None:
        raise LookupError(f"platform {platform.id!r} has no config file")
    return path


def platform_data_relpath(platform: PlatformDescriptor) -> str:
    """平台数据目录相对工作区的写法。"""
    return f"{PLATFORM_DATA_ROOT}/{platform.id}"


def platform_data_dir(platform: PlatformDescriptor, workspace_root: Path) -> Path:
    """平台自己的数据目录：新平台的状态文件在这里，发送防重库、缓存这些落盘文件也放这里。"""
    return Path(workspace_root) / platform_data_relpath(platform)


def platform_state_path(
    platform: PlatformDescriptor, name: str, workspace_root: Path, *, environ: Mapping[str, str] | None = None,
) -> Path:
    """一项状态文件的位置：升级前就在用的老文件名照旧，其余在平台数据目录下用标准文件名。"""
    legacy = platform.state_files.get(name)
    if legacy:
        override = env_override(platform.state_env.get(name, ""), environ)
        return workspace_path(override, workspace_root) if override else Path(workspace_root) / "data" / legacy
    try:
        return platform_data_dir(platform, workspace_root) / STANDARD_STATE_FILES[name]
    except KeyError:
        raise LookupError(f"unknown platform state file: {name!r}") from None


def platform_profiles_path(
    platform: PlatformDescriptor, workspace_root: Path, *, environ: Mapping[str, str] | None = None,
) -> Path | None:
    """按人资料文件的位置，能用声明里的 env 改；平台没有资料文件返回 None。"""
    if not platform.profiles_path:
        return None
    override = env_override(platform.profiles_env, environ)
    return workspace_path(override, workspace_root) if override else Path(workspace_root) / platform.profiles_path


def alias_numbering_relpath() -> str:
    return f"{PLATFORM_DATA_ROOT}/{ALIAS_NUMBERING_FILE}"


def alias_numbering_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / alias_numbering_relpath()


def platform_secret_file(
    platform: PlatformDescriptor, name: str, workspace_root: Path, *, environ: Mapping[str, str] | None = None,
) -> Path | None:
    """一项密钥两处都没配时平台自己生成的那份文件，位置能用声明里的 env 改；这项密钥不自动生成返回 None。"""
    secret = platform.secret(name)
    if not secret.file:
        return None
    override = env_override(secret.file_env, environ)
    return workspace_path(override, workspace_root) if override else Path(workspace_root) / secret.file


class MissingPlatformHook(LookupError):
    """平台描述没声明这个钩子。钩子模块导入时自己抛的 KeyError 也是 LookupError，调用方只认这个才不会把坏模块当成没声明。"""


def platform_hook(platform: PlatformDescriptor, name: str) -> ModuleType:
    """按需导入平台策略模块。适配器包导入没有副作用，这里才能直接 import。"""
    module_path = platform.hooks.get(name)
    if not module_path:
        raise MissingPlatformHook(f"platform {platform.id!r} has no {name!r} hook")
    return importlib.import_module(module_path)


def features_module() -> ModuleType:
    return importlib.import_module(FEATURES_MODULE)
