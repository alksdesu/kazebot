"""配置模板里写了哪些点分路径，拿去和键声明表对账。"""
from __future__ import annotations

from typing import Any


def leaf_paths(document: Any, prefix: str = "") -> list[str]:
    """文档里存在的点分路径。

    按「键在不在」算而不是「值是不是 None」：三态开关的示例值就是 null，
    拿 _dig 的返回值判断会把它们全当成没写。
    """
    if isinstance(document, dict) and document:
        leaves: list[str] = []
        for key, value in document.items():
            leaves.extend(leaf_paths(value, f"{prefix}.{key}" if prefix else str(key)))
        return leaves
    return [prefix] if prefix else []
