"""控制台直接改的 YAML 配置文件：读原文、解析校验、原子写回。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Iterable

import yaml
from fastapi import HTTPException
from pydantic import BaseModel

from engine.eventlog_rotation import eventlog_file_lock

# 顶层的格式版本号，哪份配置文件都可以写，不对应任何键。
VERSION_PATH = "version"


class RawConfig(BaseModel):
    content: str


def leaf_paths(document: Any, prefix: str = "") -> list[str]:
    if isinstance(document, dict):
        leaves: list[str] = []
        for key, value in document.items():
            leaves.extend(leaf_paths(value, f"{prefix}.{key}" if prefix else str(key)))
        return leaves
    if document is None:
        # 只写了段名没写值（`channels:`）不是配置项，别报成拼错的键。
        # 显式 null 同理，语义是「这个键交给下一层」。
        return []
    return [prefix] if prefix else []


def value_at(document: Any, path: str) -> Any:
    """点分路径上的值。中途不是映射、或者键不存在，都是 None。"""
    cursor = document
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        # 「还没接管」是正常状态，不是错误。刻意不回落到示例文件：
        # 那份是默认值，被当成当前配置保存下去就等于悄悄盖掉 .env 里的设置。
        return {"content": "", "exists": False}
    return {"content": path.read_text(encoding="utf-8"), "exists": True}


def parse_mapping(content: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败：{exc}") from exc
    if document is not None and not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="顶层必须是映射（key: value）")
    return document or {}


def unknown_paths(document: dict[str, Any], known: Iterable[str]) -> list[str]:
    """文档里没有任何键消费的叶子路径。这种键改了不会有任何效果，而界面只会显示保存成功。"""
    return sorted(set(leaf_paths(document)) - set(known) - {VERSION_PATH})


def write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录 tmp + replace：bot 进程随时在读，读到半截 yaml 等于整份配置塌掉。
    with eventlog_file_lock(path):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
