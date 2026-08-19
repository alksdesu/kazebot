"""控制台侧的记忆增删改查。

写路径必须和 save_memory 走同一把跨进程锁与同一套默认值，否则人在页面上改的
和后台提取器写的会互相覆盖，或者存下一条永远召回不到的记忆。
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine import memory_subjects
from engine.builtin.knowledge_inject import (
    _BOOK_NAME_RE,
    _DEFAULT_SCAN_DEPTH,
    _MEMORY_ID_RE,
    _MEMORY_NAMESPACE_RE,
    _invalidate_cache,
    _load_book,
    _memory_write_lock,
    _save_book,
    memory_dir,
)

__all__ = [
    "MemoryAdminError",
    "clear_namespace",
    "delete_entry",
    "list_entries",
    "list_namespaces",
    "upsert_entry",
]

# 手动条目不参与 14 天淘汰，也不被 dream 整理 —— 人写进去的就该由人删。
_MANUAL_SOURCE = "manual"
_MAX_CONTENT_LEN = 4000
_MAX_KEYWORDS = 32
_MAX_KEYWORD_LEN = 128


class MemoryAdminError(ValueError):
    """消息可以直接回给控制台。"""


def _valid_namespace(namespace: str) -> str:
    """空串表示 data/memory 根目录，那是 conversation_key 缺失时的故障落点，得能清理。"""
    text = str(namespace or "").strip()
    if not text:
        return ""
    if _MEMORY_NAMESPACE_RE.match(text):
        return text
    if text.startswith("user_") and memory_subjects.is_valid_subject(text[len("user_"):]):
        return text
    raise MemoryAdminError(f"非法的记忆 namespace: {namespace}")


def _valid_book(book: str) -> str:
    text = str(book or "").strip() or "default"
    if not _BOOK_NAME_RE.match(text):
        raise MemoryAdminError(f"非法的记忆本名: {book}")
    return text


def _valid_entry_id(entry_id: str) -> str:
    text = str(entry_id or "").strip()
    if not _MEMORY_ID_RE.match(text):
        raise MemoryAdminError(f"非法的记忆 id: {entry_id}")
    return text


def _namespace_subject(namespace: str) -> str:
    return namespace[len("user_"):] if namespace.startswith("user_") else ""


def _clean_keywords(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(part or "").strip() for part in raw]
    else:
        items = []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen or len(item) > _MAX_KEYWORD_LEN:
            continue
        seen.add(item)
        result.append(item)
    return result[:_MAX_KEYWORDS]


def list_namespaces(workspace_root: Path) -> list[dict[str, Any]]:
    """每个 namespace 一行，带条目数与最后修改时间。"""
    root = memory_dir(Path(workspace_root))
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    candidates: list[str] = [""]
    candidates.extend(sorted(child.name for child in root.iterdir() if child.is_dir()))
    for name in candidates:
        try:
            namespace = _valid_namespace(name)
        except MemoryAdminError:
            continue
        entries = list_entries(workspace_root, namespace)
        # 空的子目录仍要列出来，人才能把它清掉；根目录没东西就没什么可看的。
        if not entries and not namespace:
            continue
        result.append({
            "namespace": namespace,
            "kind": "subject" if namespace.startswith("user_") else ("conversation" if namespace else "orphan"),
            "subject": _namespace_subject(namespace),
            "entry_count": len(entries),
            "books": sorted({entry["book"] for entry in entries}),
            "updated_at": max((entry.get("updated_at") or "" for entry in entries), default=""),
        })
    return result


def list_entries(workspace_root: Path, namespace: str) -> list[dict[str, Any]]:
    """某个 namespace 下全部记忆条目，附带它所在的 book。"""
    namespace = _valid_namespace(namespace)
    directory = memory_dir(Path(workspace_root), namespace)
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        book = path.stem
        for entry in _load_book(path).get("entries") or []:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["book"] = book
            item.setdefault("scan_depth", _DEFAULT_SCAN_DEPTH)
            result.append(item)
    return result


def upsert_entry(
    workspace_root: Path, namespace: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """新建或整条更新一条记忆。id 已存在即更新。"""
    namespace = _valid_namespace(namespace)
    book = _valid_book(payload.get("book"))
    entry_id = _valid_entry_id(payload.get("id"))
    content = str(payload.get("content") or "").strip()
    if not content:
        raise MemoryAdminError("记忆内容不能为空")
    if len(content) > _MAX_CONTENT_LEN:
        raise MemoryAdminError(f"记忆内容超过 {_MAX_CONTENT_LEN} 字")

    keywords = _clean_keywords(payload.get("keywords"))
    constant = bool(payload.get("constant"))
    subject = str(payload.get("subject") or "").strip() or _namespace_subject(namespace)
    if subject and not memory_subjects.is_valid_subject(subject):
        raise MemoryAdminError(f"非法的 subject: {subject}")
    # 与 save_memory 同一条硬约束：三者皆空的条目注入侧会直接跳过，存了也读不到。
    if not keywords and not constant and not subject:
        raise MemoryAdminError("这条记忆永远不会被召回：至少填关键词，或勾选常驻，或指定 subject")

    now = datetime.now(timezone.utc).isoformat()
    path = memory_dir(Path(workspace_root), namespace) / f"{book}.yaml"
    with _memory_write_lock(Path(workspace_root)):
        data = _load_book(path)
        data.setdefault("book", book)
        entries = data.setdefault("entries", [])
        found = -1
        for index, existing in enumerate(entries):
            if isinstance(existing, dict) and str(existing.get("id") or "").strip() == entry_id:
                found = index
                break
        entry: dict[str, Any] = {
            "id": entry_id,
            "content": content,
            "keywords": keywords,
            "constant": constant,
            "enabled": bool(payload.get("enabled", True)),
            "priority": int(payload.get("priority") or 0),
            "scan_depth": int(payload.get("scan_depth") or _DEFAULT_SCAN_DEPTH),
        }
        if subject:
            entry["subject"] = subject
        if found >= 0:
            old = entries[found]
            entry["created_at"] = str(old.get("created_at") or "").strip() or now
            # 改过内容就归人管：自动条目一旦人工编辑过，不该再被 dream 悄悄改写或淘汰。
            entry["source"] = _MANUAL_SOURCE
            entry["updated_at"] = now
            entries[found] = entry
        else:
            entry["created_at"] = now
            entry["updated_at"] = now
            entry["source"] = _MANUAL_SOURCE
            entries.append(entry)
        _save_book(path, data)
    _invalidate_cache(Path(workspace_root), memory_book=namespace)
    entry["book"] = book
    return entry


def delete_entry(workspace_root: Path, namespace: str, book: str, entry_id: str) -> bool:
    """删掉一条记忆。返回是否真的删到了。"""
    namespace = _valid_namespace(namespace)
    book = _valid_book(book)
    entry_id = _valid_entry_id(entry_id)
    path = memory_dir(Path(workspace_root), namespace) / f"{book}.yaml"
    removed = False
    with _memory_write_lock(Path(workspace_root)):
        if not path.exists():
            return False
        data = _load_book(path)
        entries = data.get("entries") or []
        kept = [
            entry for entry in entries
            if not (isinstance(entry, dict) and str(entry.get("id") or "").strip() == entry_id)
        ]
        removed = len(kept) != len(entries)
        if removed:
            data["entries"] = kept
            if kept:
                _save_book(path, data)
            else:
                # 空本留在盘上会被当成一本正常的空记忆反复读，删掉更干净。
                path.unlink(missing_ok=True)
    if removed:
        _invalidate_cache(Path(workspace_root), memory_book=namespace)
    return removed


def clear_namespace(workspace_root: Path, namespace: str) -> int:
    """清空一个 namespace 的全部记忆，返回删掉的条目数。"""
    namespace = _valid_namespace(namespace)
    directory = memory_dir(Path(workspace_root), namespace)
    if not directory.is_dir():
        return 0
    removed = 0
    with _memory_write_lock(Path(workspace_root)):
        for path in sorted(directory.glob("*.yaml")):
            removed += len(_load_book(path).get("entries") or [])
            path.unlink(missing_ok=True)
        if namespace:
            # 根目录不能删，它是 data/memory 本身。
            with contextlib.suppress(OSError):
                directory.rmdir()
    _invalidate_cache(Path(workspace_root), memory_book=namespace)
    return removed
