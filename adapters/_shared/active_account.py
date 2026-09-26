"""当前登录账号的落盘记录，作为会话键摘要的作用域。

不并进 live state：那份是每几秒重写一次的展示态，而作用域参与哈希计算，
被覆盖或写半截就会让全部会话与记忆目录静默分叉到新命名空间。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from platforms.descriptor import STATE_ACTIVE_ACCOUNT, PlatformDescriptor
from platforms.ids import normalize_id
from platforms.registry import platform_state_path

logger = logging.getLogger(__name__)


def normalize_account(self_id: object, platform: PlatformDescriptor) -> str:
    """账号规范化成字符串 ID，形状跟平台描述走。取不到或不合法一律返回空串（= 不加作用域）。

    数字账号的平台遇到脏值宁可不加作用域，也不能把记忆分叉进算不回来的命名空间。
    """
    return normalize_id(self_id, platform.id_shape)


class ActiveAccountStore:
    """一个平台在一个工作区里记住的当前账号。文件名和账号 ID 的形状都取平台描述。"""

    def __init__(self, platform: PlatformDescriptor, workspace: Path) -> None:
        path = platform_state_path(platform, STATE_ACTIVE_ACCOUNT, workspace)
        if path is None:
            raise LookupError(f"platform {platform.id!r} declares no active account file")
        self.platform = platform
        self.path = path

    def normalize(self, self_id: object) -> str:
        return normalize_account(self_id, self.platform)

    def load(self) -> str:
        """读上次记住的账号。文件缺失或损坏都返回空串，退化成加隔离前的摘要。"""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except (OSError, ValueError):
            logger.error("cannot read active bot scope %s; running unscoped", self.path)
            return ""
        return self.normalize(payload.get("self_id") if isinstance(payload, dict) else "")

    def save(self, self_id: object) -> bool:
        """记住当前账号，返回是否真的换了号。空 self_id 不落盘，避免抹掉已知作用域。"""
        scope = self.normalize(self_id)
        if not scope or scope == self.load():
            return False
        payload = {"self_id": scope}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.error("cannot persist active bot scope %s", self.path, exc_info=True)
            return False
        return True
