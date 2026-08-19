"""当前登录 QQ 号的落盘记录，作为会话键摘要的作用域。

不并进 qq_live_state.json：那份是每几秒重写一次的展示态，而作用域参与哈希计算，
被覆盖或写半截就会让全部会话与记忆目录静默分叉到新命名空间。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILENAME = "onebot_active_bot.json"


def scope_file(workspace: Path) -> Path:
    return Path(workspace) / "data" / STATE_FILENAME


def normalize(self_id: object) -> str:
    """QQ 号规范化成纯数字串。取不到或不合法一律返回空串（= 不加作用域）。"""
    raw = str(self_id or "").strip()
    return raw if raw.isdigit() else ""


def load_scope(workspace: Path) -> str:
    """读上次记住的账号。文件缺失或损坏都返回空串，退化成加隔离前的摘要。"""
    path = scope_file(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        logger.error("cannot read active bot scope %s; running unscoped", path)
        return ""
    return normalize(payload.get("self_id") if isinstance(payload, dict) else "")


def save_scope(workspace: Path, self_id: object) -> bool:
    """记住当前账号，返回是否真的换了号。空 self_id 不落盘，避免抹掉已知作用域。"""
    scope = normalize(self_id)
    if not scope or scope == load_scope(workspace):
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
