"""当前登录账号的落盘记录，作为会话键摘要的作用域。

不并进 live state：那份是每几秒重写一次的展示态，而作用域参与哈希计算，
被覆盖或写半截就会让全部会话与记忆目录静默分叉到新命名空间。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from platforms.descriptor import HOOK_KNOWN_ACCOUNTS, STATE_ACTIVE_ACCOUNT, PlatformDescriptor
from platforms.ids import normalize_id
from platforms.registry import platform_hook, platform_state_path

logger = logging.getLogger(__name__)


def normalize_account(self_id: object, platform: PlatformDescriptor) -> str:
    """账号规范化成字符串 ID，形状跟平台描述走。取不到或不合法一律返回空串（= 不加作用域）。

    数字账号的平台遇到脏值宁可不加作用域，也不能把记忆分叉进算不回来的命名空间。
    """
    return normalize_id(self_id, platform.id_shape)


class ActiveAccountStore:
    """一个平台在一个工作区里记住的当前账号。文件名和账号 ID 的形状都取平台描述。"""

    def __init__(self, platform: PlatformDescriptor, workspace: Path) -> None:
        self.platform = platform
        self.path = platform_state_path(platform, STATE_ACTIVE_ACCOUNT, workspace)

    def normalize(self, self_id: object) -> str:
        return normalize_account(self_id, self.platform)

    def load(self) -> str:
        """读上次记住的账号。文件缺失或损坏都返回空串，退化成加隔离前的摘要。"""
        return self._read()[0]

    def accounts(self) -> tuple[str, ...]:
        """在这个工作区登录过的全部账号，按第一次登录的先后排。只记着当前账号的老文件就只有它一个。"""
        return self._read()[1]

    def save(self, self_id: object) -> bool:
        """记住当前账号并补进登录过的账号，返回是否真的换了号。空 self_id 不落盘，避免抹掉已知作用域。"""
        scope = self.normalize(self_id)
        if not scope:
            return False
        current, known = self._read()
        if scope == current:
            return False
        # 旧号的会话键还留在路由表里，离线迁移要靠这份名单认出它们是谁的。
        payload = {"self_id": scope, "accounts": list(_unique((*known, scope)))}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.error("cannot persist active bot scope %s", self.path, exc_info=True)
            return False
        return True

    def _read(self) -> tuple[str, tuple[str, ...]]:
        """(当前账号, 登录过的全部账号)。文件缺失或损坏都当作什么也没记。"""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "", ()
        except (OSError, ValueError):
            logger.error("cannot read active bot scope %s; running unscoped", self.path)
            return "", ()
        if not isinstance(payload, dict):
            return "", ()
        current = self.normalize(payload.get("self_id"))
        listed = payload.get("accounts")
        history = listed if isinstance(listed, list) else []
        return current, _unique(self.normalize(value) for value in (*history, current))


def account_candidates(
    platform: PlatformDescriptor, workspace: Path, *, extra: Iterable[object] = (),
) -> tuple[str, ...]:
    """离线迁移认会话键时要试的账号作用域。

    记下的当前账号和登录过的旧号、平台自己知道的账号，再加上调用方点名的；
    加账号隔离之前就有数据的平台，还要试不加作用域。
    """
    found = [*ActiveAccountStore(platform, workspace).accounts(), *_platform_accounts(platform, workspace), *extra]
    candidates = _unique(normalize_account(value, platform) for value in found)
    return (*candidates, "") if platform.legacy_conversation_keys else candidates


def _platform_accounts(platform: PlatformDescriptor, workspace: Path) -> list[object]:
    if HOOK_KNOWN_ACCOUNTS not in platform.hooks:
        return []
    return list(platform_hook(platform, HOOK_KNOWN_ACCOUNTS).known_accounts(Path(workspace)))


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
