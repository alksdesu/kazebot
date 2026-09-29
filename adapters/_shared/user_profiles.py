"""按人的资料：称呼、头衔、备注。只喂给模型看，不授予任何权限。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

import config_layout

from .yaml_loader import load_yaml

logger = logging.getLogger(__name__)

# 认的字段，各自按顺序接受的写法：后面几个是早先文档里的叫法。
_FIELDS = (
    ("display_name", ("display_name", "name")),
    ("address_as", ("address_as", "call_as")),
    ("title", ("title", "role")),
    ("note", ("note", "style_note")),
)


def _field(profile: Mapping[str, Any], spellings: tuple[str, ...]) -> str:
    value = next((profile.get(spelling) for spelling in spellings if profile.get(spelling)), "")
    return str(value or "").strip()


class ProfileFile:
    """一份按人资料文件，按修改时间、大小和 inode 认出改动。控制台和共享组里别的实例都会直接改它。"""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        # 最近一次读的是哪一版；None 是没读过或文件不在。
        self._stamp: tuple[int, int, int] | None = None

    def _current_stamp(self) -> tuple[int, int, int] | None:
        return config_layout.file_stamp(self.path) if self.path is not None else None

    def load(self) -> dict[str, dict[str, str]]:
        # 先记版本再读：读的途中又被改了，下一次比对就会认出来再读一遍，不会停在旧内容上。
        self._stamp = self._current_stamp()
        return load_profiles(self.path)

    def changed(self) -> bool:
        return self._current_stamp() != self._stamp


def load_profiles(path: Path | None) -> dict[str, dict[str, str]]:
    """从 JSON / YAML 读按人的资料，键和别名表一样是身份键。没有文件当没有，读坏了记一条警告也当没有。

    YAML 按文本读 on / off / yes / no：`address_as: no` 被读成布尔，bot 就会一直管人叫「False」。
    """
    if path is None:
        return {}
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
        data = json.loads(raw) if source.suffix.lower() == ".json" else load_yaml(raw)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("failed to load user profiles: %s", source, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    users = data.get("users") if isinstance(data.get("users"), dict) else data
    profiles: dict[str, dict[str, str]] = {}
    for raw_key, raw_profile in users.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_profile, dict):
            continue
        profile = {name: _field(raw_profile, spellings) for name, spellings in _FIELDS}
        profiles[key] = {name: value for name, value in profile.items() if value}
    return profiles
