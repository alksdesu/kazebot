"""控制台直接改的 YAML 配置文件：读原文、解析校验、原子写回。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml
from fastapi import HTTPException
from pydantic import BaseModel

import config_layout

# 顶层的格式版本号，哪份配置文件都可以写，不对应任何键。
VERSION_PATH = "version"


class RawConfig(BaseModel):
    content: str
    # 打开编辑器时拿到的指纹。给了就先比对：中途被别处改过就 409，一个字节都不写。
    base_hash: str | None = None


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
    """原文连同指纹。指纹按落盘的原始字节算，空串表示文件还不存在。"""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        # 「还没接管」是正常状态，不是错误。刻意不回落到示例文件：
        # 那份是默认值，被当成当前配置保存下去就等于悄悄盖掉 .env 里的设置。
        return {"content": "", "exists": False, "hash": ""}
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return {"content": text, "exists": True, "hash": config_layout.bytes_hash(data)}


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


def write_raw(path: Path, content: str, *, base_hash: str | None = None) -> str:
    """整份写回，返回写完的指纹。

    和 config/ 下其余配置同一把锁、同一套同目录临时文件替换：bot 随时在读，读到半截 yaml 等于整份配置塌掉；
    共享组里几个实例的控制台改的是同一份文件，拿的也是同一把锁。给了 base_hash 就先比对，对不上 409。
    """
    try:
        return config_layout.write_config_text(path, content, base_hash=base_hash)
    except config_layout.ConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 {path.name} 失败，原文件没动：{exc}") from exc
