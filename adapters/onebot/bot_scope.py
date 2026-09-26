"""当前登录 QQ 号的落盘记录，作为会话键摘要的作用域。

不并进 qq_live_state.json：那份是每几秒重写一次的展示态，而作用域参与哈希计算，
被覆盖或写半截就会让全部会话与记忆目录静默分叉到新命名空间。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from platforms.builtin import QQ_NAPCAT
from platforms.ids import normalize_id

logger = logging.getLogger(__name__)

STATE_FILENAME = "onebot_active_bot.json"


def scope_file(workspace: Path) -> Path:
    return Path(workspace) / "data" / STATE_FILENAME


def normalize(self_id: object, *, shape: str = QQ_NAPCAT.id_shape) -> str:
    """账号规范化成字符串 ID，形状跟平台描述走。取不到或不合法一律返回空串（= 不加作用域）。

    NapCat 的账号恒为纯数字，脏值宁可不加作用域，也不能把记忆分叉进算不回来的命名空间；
    账号是字符串的平台（如 QQ 官方的 openid）传 ID_STRING。
    """
    return normalize_id(self_id, shape)


def load_scope(workspace: Path, *, shape: str = QQ_NAPCAT.id_shape) -> str:
    """读上次记住的账号。文件缺失或损坏都返回空串，退化成加隔离前的摘要。"""
    path = scope_file(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        logger.error("cannot read active bot scope %s; running unscoped", path)
        return ""
    return normalize(payload.get("self_id") if isinstance(payload, dict) else "", shape=shape)


def save_scope(workspace: Path, self_id: object, *, shape: str = QQ_NAPCAT.id_shape) -> bool:
    """记住当前账号，返回是否真的换了号。空 self_id 不落盘，避免抹掉已知作用域。"""
    scope = normalize(self_id, shape=shape)
    if not scope or scope == load_scope(workspace, shape=shape):
        return False
    path = scope_file(workspace)
    payload = {"self_id": scope}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.error("cannot persist active bot scope %s", path, exc_info=True)
        return False
    return True
