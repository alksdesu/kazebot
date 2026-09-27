"""会话键摘要的盐：显式配置优先，否则用工作区里自动生成并钉住的密钥。

无盐 SHA256 的输入只有「前缀 + 一串数字」，枚举一遍就能反推真实群号和账号。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from clonoth_runtime import platform_secret
from platforms.descriptor import CHANNEL_OTHER, STATE_ROUTES, ChannelDescriptor, PlatformDescriptor
from platforms.registry import platform_secret_file, platform_state_path

logger = logging.getLogger(__name__)

# 已有历史状态的部署里，密钥文件写这个标记，把它钉在无盐摘要上。
# 摘要一变，记忆目录与 supervisor 会话就全部对不上，不能在启动时静默发生。
LEGACY_MARKER = "legacy-unsalted"
_SECRET_BYTES = 32
_SECRET_RE = re.compile(r"[0-9a-fA-F]{64,}")
# 平台描述里摘要密钥的名字。接稳定会话键的平台都要声明它，并给出自动生成的文件位置。
SECRET_NAME = "conversation_hash_secret"
# 离线工具读到的密钥从哪来：显式配置、钉住的文件、首次启动会钉成无盐、还没定下来。
SECRET_EXPLICIT = "explicit"
SECRET_PINNED = "pinned"
SECRET_LEGACY = "legacy"
SECRET_UNSETTLED = "unsettled"


def scoped_key(conversation_key: str, bot_scope: str) -> str:
    """按 bot 账号给会话键加作用域。空 scope 原样返回，摘要与加账号隔离前逐字相同。"""
    raw = str(conversation_key or "").strip()
    scope = str(bot_scope or "").strip()
    return f"bot{scope}|{raw}" if scope else raw


def digest(conversation_key: str, secret: str, *, bot_scope: str = "") -> str:
    """会话键的 24 位摘要；secret 为空时退化成可枚举的裸 SHA256。

    bot_scope 是当前登录的 bot 账号：拌进摘要输入，同一个群在不同 bot 号下就落到
    不同的会话键与记忆目录，换号自动隔离、换回来自动复原。
    """
    raw = scoped_key(conversation_key, bot_scope).encode("utf-8")
    key = str(secret or "").encode("utf-8")
    if key:
        return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(raw).hexdigest()[:24]


class ConversationKeys:
    """真实会话键 → 交给核心的稳定键：渠道前缀取平台声明，摘要拌进密钥和账号作用域。"""

    __slots__ = ("_hashed", "_fallback", "_secret", "_platform", "_unscoped_allowed")

    def __init__(self, platform: PlatformDescriptor, secret: str) -> None:
        hashed = tuple(channel for channel in platform.channels if channel.hashed)
        fallback = next((channel for channel in hashed if channel.kind == CHANNEL_OTHER), None)
        if fallback is None:
            raise ValueError(f"platform {platform.id!r} declares no hashed channel for unrecognized conversations")
        self._hashed = hashed
        self._fallback = fallback
        self._secret = secret
        self._platform = platform.id
        # 新平台漏传账号，两个号看到的同一个群会并成一个会话、一份记忆。
        self._unscoped_allowed = platform.legacy_conversation_keys

    def channel_of(self, real_key: str) -> ChannelDescriptor:
        """真实会话键落在哪个渠道。不做摘要的渠道和认不出的前缀都归兜底渠道。"""
        return next(
            (channel for channel in self._hashed if real_key.startswith(f"{channel.name}:")), self._fallback,
        )

    def stable_key(self, real_key: str, *, bot_scope: str) -> str:
        """bot_scope 是当前登录的账号。只有加账号隔离之前就有数据的平台可以给空串。"""
        if not str(bot_scope or "").strip() and not self._unscoped_allowed:
            raise ValueError(f"platform {self._platform!r} needs the bot account to derive a conversation key")
        return f"{self.channel_of(real_key).name}:{digest(real_key, self._secret, bot_scope=bot_scope)}"


def rekey(old_stable: str, real_key: str, secret: str, *, bot_scope: str = "") -> str:
    """离线迁移用：保留旧稳定键的渠道前缀，只按新的密钥或作用域重算摘要。"""
    return f"{old_stable.split(':', 1)[0]}:{digest(real_key, secret, bot_scope=bot_scope)}"


def has_digest_derived_state(workspace: Path, route_state_file: Path) -> bool:
    """这个工作区里是否已经存在按旧摘要落盘的状态。"""
    if route_state_file.exists():
        return True
    memory_root = workspace / "data" / "memory"
    try:
        return any(memory_root.glob("conv_*"))
    except OSError:
        return False


def resolve_secret(
    *,
    explicit_secret: str,
    secret_file: Path,
    workspace: Path,
    route_state_file: Path,
    allow_legacy: bool,
    legacy_hint: str = "",
) -> tuple[str, bool]:
    """返回 (secret, salted)。首次启动时决定并钉住模式，之后只读文件。

    allow_legacy 只给加盐之前就有数据的平台。新平台一律生成新密钥：同一工作区里别的平台留下的记忆目录不是它的存量。
    """
    if explicit_secret:
        # 显式配置优先，既不读也不写自动密钥文件。
        return (explicit_secret, True)

    pinned = _read_pinned_secret(secret_file)
    if pinned is not None:
        return pinned

    if allow_legacy and has_digest_derived_state(workspace, route_state_file):
        # 存量部署：钉在无盐上让摘要保持不变，加盐迁移交离线脚本。
        logger.warning(
            "existing conversation state found without a hash secret; pinning legacy "
            "unsalted digests.%s", f" {legacy_hint}" if legacy_hint else "",
        )
        result = _pin_mode(secret_file, LEGACY_MARKER)
    else:
        result = _pin_mode(secret_file, secrets.token_hex(_SECRET_BYTES))

    if result is not None:
        return result
    return ("", False)


def secret_file(platform: PlatformDescriptor, workspace: Path, *, environ: Mapping[str, str] | None = None) -> Path:
    """平台自动生成、钉住摘要密钥的那份文件，位置能用声明里的 env 改。"""
    path = platform_secret_file(platform, SECRET_NAME, Path(workspace), environ=environ)
    if path is None:
        raise LookupError(f"platform {platform.id!r} declares no generated file for {SECRET_NAME}")
    return path


def platform_conversation_secret(
    platform: PlatformDescriptor, workspace: Path, *, legacy_hint: str = "",
) -> tuple[str, bool]:
    """平台运行时用的摘要密钥，返回 (secret, salted)：显式配置优先，否则首次启动时生成并钉住。"""
    root = Path(workspace)
    return resolve_secret(
        explicit_secret=platform_secret(root, platform, SECRET_NAME).value,
        secret_file=secret_file(platform, root),
        workspace=root,
        route_state_file=platform_state_path(platform, STATE_ROUTES, root),
        allow_legacy=platform.legacy_conversation_keys,
        legacy_hint=legacy_hint,
    )


@dataclass(frozen=True)
class CurrentSecret:
    """离线工具读到的摘要密钥和它的来处。无盐的值是空串。"""

    value: str
    source: str

    @property
    def settled(self) -> bool:
        """平台还没起过时密钥要到首次启动才生成，算不出它将来的会话键。"""
        return self.source != SECRET_UNSETTLED


def current_secret(
    platform: PlatformDescriptor, workspace: Path, *, environ: Mapping[str, str] | None = None,
) -> CurrentSecret:
    """平台眼下在用的摘要密钥，和运行时同一个顺序，但不写任何文件。给离线迁移用。"""
    root = Path(workspace)
    explicit = platform_secret(root, platform, SECRET_NAME, environ=environ).value
    if explicit:
        return CurrentSecret(explicit, SECRET_EXPLICIT)
    pinned = _read_pinned_secret(secret_file(platform, root, environ=environ))
    if pinned is not None:
        return CurrentSecret(pinned[0], SECRET_PINNED)
    routes = platform_state_path(platform, STATE_ROUTES, root, environ=environ)
    if platform.legacy_conversation_keys and has_digest_derived_state(root, routes):
        # 运行时首次启动会钉在无盐上，算出来的键一样。
        return CurrentSecret("", SECRET_LEGACY)
    return CurrentSecret("", SECRET_UNSETTLED)


def _read_pinned_secret(secret_file: Path) -> tuple[str, bool] | None:
    """读已钉住的密钥文件。不存在返回 None；损坏一律降级但不改写文件。"""
    if not secret_file.exists():
        return None
    try:
        content = secret_file.read_text(encoding="utf-8").strip()
    except OSError as read_error:
        # 读不出但文件在，绝不能当成干净工作区去生成新密钥：那会让现存记忆目录全成孤儿。
        logger.error("cannot read conversation hash secret %s: %s", secret_file, read_error)
        return ("", False)
    if content == LEGACY_MARKER:
        return ("", False)
    if _SECRET_RE.fullmatch(content):
        return (content, True)
    # 文件在但内容既不是标记也不是合法密钥：降级而不改写，避免把现存摘要全换掉。
    logger.error("conversation hash secret %s is malformed; running unsalted", secret_file)
    return ("", False)


def _pin_mode(secret_file: Path, content: str) -> tuple[str, bool] | None:
    """写入密钥/标记并返回解析结果；被并发抢先则回读，彻底写失败返回 None。"""
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        # open("x") 存在即失败，避免覆盖另一个进程刚钉住的模式。
        with open(secret_file, "x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        # 另一个进程在检查之后抢先钉住了模式，读它的而不是覆盖。
        return _read_pinned_secret(secret_file)
    except OSError as write_error:
        # 只读盘/权限：降级为无盐而不是拒绝启动，控制台会常驻显示未加盐。
        logger.error("cannot write conversation hash secret %s: %s", secret_file, write_error)
        return None
    _restrict_permissions(secret_file)
    return ("", False) if content == LEGACY_MARKER else (content, True)


def _restrict_permissions(secret_file: Path) -> None:
    # Windows 上 chmod 只改只读位，真正的隔离靠 data/ 目录本身的 ACL。
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
