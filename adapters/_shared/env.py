"""启动期读一次的环境变量：带夹紧的数字、按优先级取第一个非空的名字，以及逗号列表。"""
from __future__ import annotations

import os


def env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = float(str(raw).strip()) if raw is not None else float(default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def env_first(*names: str, default: str = "") -> str:
    """按优先级读取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def split_list(raw: str) -> tuple[str, ...]:
    """半角逗号分隔的列表：去空白、去重，保持书写顺序。"""
    result: list[str] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if token and token not in result:
            result.append(token)
    return tuple(result)
