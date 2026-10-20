from __future__ import annotations

from dataclasses import replace
from typing import Any


_BOUNDARY_KEY = "_entry_branch_base"


def branch_base_count(messages: list[Any], branch_id: str, fallback: int) -> int:
    for index in range(len(messages) - 1, -1, -1):
        meta = getattr(messages[index], "meta", None)
        if isinstance(meta, dict) and meta.get(_BOUNDARY_KEY) == branch_id:
            return index + 1
    return fallback


def without_branch_boundary(messages: list[Any], branch_id: str) -> list[Any]:
    result = []
    for message in messages:
        meta = getattr(message, "meta", None)
        if isinstance(meta, dict) and meta.get(_BOUNDARY_KEY) == branch_id:
            message = replace(message, meta={key: value for key, value in meta.items() if key != _BOUNDARY_KEY})
        result.append(message)
    return result


def mark_branch_boundary(messages: list[Any], branch_id: str, base_count: int) -> list[Any]:
    if not 0 < base_count <= len(messages):
        raise ValueError("Branch base must identify an existing prefix")
    result = without_branch_boundary(messages, branch_id)
    boundary = result[base_count - 1]
    # 与分支内容一起原子写入，避免事件尚未持久化时重启丢失合并边界。
    result[base_count - 1] = replace(boundary, meta={**boundary.meta, _BOUNDARY_KEY: branch_id})
    return result
