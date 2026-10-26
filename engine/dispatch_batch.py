"""同步委派批次（TaskAction.dispatch_batch）两端共用的约束：engine 组批次、supervisor 建子任务都按这里来。"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .attachments import is_allowed_attachment_path

MAX_DISPATCH_BATCH_ITEMS = 8
MAX_ITEM_ATTACHMENTS = 32

_ATTACHMENT_TYPES = frozenset({"image", "file"})


def _relative_attachment_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    path = raw.strip()
    normalized = path.replace("\\", "/")
    # 绝对路径和盘符拼到工作区下会跳出工作区，is_allowed_attachment_path 会先剥掉开头的 / 再判
    if not path or normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        return ""
    return path if is_allowed_attachment_path(path) else ""


def batch_item_attachments(raw: Any) -> list[dict[str, str]]:
    """批次项带的附件只留格式对、路径在 data/ 下的；这里不读盘，子节点读附件时还会按真实位置再判一次。"""
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if len(kept) >= MAX_ITEM_ATTACHMENTS:
            break
        if not isinstance(item, dict):
            continue
        path = _relative_attachment_path(item.get("path"))
        if not path or path in seen:
            continue
        mime = item.get("mime_type")
        mime = mime.strip() if isinstance(mime, str) else ""
        kind = str(item.get("type") or "").strip()
        if kind not in _ATTACHMENT_TYPES:
            kind = "image" if mime.startswith("image/") else "file"
        name = item.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else PurePosixPath(path.replace("\\", "/")).name
        attachment = {"type": kind, "path": path, "name": name}
        if mime:
            attachment["mime_type"] = mime
        kept.append(attachment)
        seen.add(path)
    return kept
