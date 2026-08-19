"""会话键摘要的盐：显式环境变量优先，否则用工作区里自动生成并钉住的密钥。

无盐 SHA256 的输入只有「前缀 + 十位数字」，枚举一遍就能反推真实群号/QQ 号。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

# 已有历史状态的部署里，密钥文件写这个标记，把它钉在无盐摘要上。
# 摘要一变，记忆目录与 supervisor 会话就全部对不上，不能在启动时静默发生。
LEGACY_MARKER = "legacy-unsalted"
_SECRET_BYTES = 32
_SECRET_RE = re.compile(r"[0-9a-fA-F]{64,}")


def digest(conversation_key: str, secret: str) -> str:
    """会话键的 24 位摘要；secret 为空时退化成可枚举的裸 SHA256。"""
    raw = str(conversation_key or "").strip().encode("utf-8")
    key = str(secret or "").encode("utf-8")
    if key:
        return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(raw).hexdigest()[:24]


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
    *, env_secret: str, secret_file: Path, workspace: Path, route_state_file: Path,
) -> tuple[str, bool]:
    """返回 (secret, salted)。首次启动时决定并钉住模式，之后只读文件。"""
    if env_secret:
        # 显式配置优先，既不读也不写自动密钥文件。
        return (env_secret, True)

    pinned = _read_pinned_secret(secret_file)
    if pinned is not None:
        return pinned

    if has_digest_derived_state(workspace, route_state_file):
        # 存量部署：钉在无盐上让摘要保持不变，加盐迁移交离线脚本。
        logger.warning(
            "existing conversation state found without a hash secret; pinning legacy "
            "unsalted digests. Run deploy/migrate_qq_conversation_hash.py to salt them.",
        )
        result = _pin_mode(secret_file, LEGACY_MARKER)
    else:
        result = _pin_mode(secret_file, secrets.token_hex(_SECRET_BYTES))

    if result is not None:
        return result
    return ("", False)


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
