"""控制台侧的记忆增删改查。

写路径必须和 save_memory 走同一把跨进程锁与同一套默认值，否则人在页面上改的
和后台提取器写的会互相覆盖，或者存下一条永远召回不到的记忆。
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from engine.builtin.knowledge_inject import (
    _BOOK_NAME_RE,
    _DEFAULT_SCAN_DEPTH,
    _MEMORY_ID_RE,
    MemoryBookUnreadable,
    _invalidate_cache,
    _load_book,
    _memory_write_lock,
    _save_book,
    memory_entry_constant,
    memory_entry_enabled,
)
from engine.memory_namespace import (
    InvalidMemoryNamespace,
    is_memory_namespace,
    is_valid_namespace,
    is_valid_subject,
    memory_book_files,
    memory_dir,
    namespace_dirs,
    subject_from_namespace,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryAdminError",
    "MemoryListing",
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


@dataclass
class MemoryListing:
    """一个 namespace 里读得出来的条目，和读不出来的本。

    读不出来的本不能悄悄跳过：控制台的条数、本数就少了一截，看着像那些记忆没了。
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    # [{"book": 本名, "problem": 位置和问题}]，不带文件内容。
    unreadable_books: list[dict[str, str]] = field(default_factory=list)


def _valid_namespace(namespace: str) -> str:
    """空串表示 data/memory 根目录，那是 conversation_key 缺失时的故障落点，得能清理。"""
    text = str(namespace or "").strip()
    if not text:
        return ""
    if is_valid_namespace(text):
        return text
    raise MemoryAdminError(f"非法的记忆 namespace: {namespace}")


def _namespace_dir(workspace_root: Path, namespace: str) -> Path:
    """校验过的 namespace 目录。名字合规、却是链到 data/memory 外面的目录，同样不认。"""
    try:
        return memory_dir(Path(workspace_root), _valid_namespace(namespace))
    except InvalidMemoryNamespace as exc:
        raise MemoryAdminError(f"非法的记忆 namespace: {namespace}") from exc


def _unreadable(exc: MemoryBookUnreadable) -> MemoryAdminError:
    return MemoryAdminError(f"记忆本 {exc.path.name} 读不出来（{exc.reason}），先修好或删掉这个文件再改")


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


def _namespace_kind(namespace: str) -> str:
    if not namespace:
        return "orphan"
    if subject_from_namespace(namespace):
        return "subject"
    # 节点在 memory_book 里自己起名的目录，不属于任何一个会话。
    return "conversation" if is_memory_namespace(namespace) else "memory_book"


def _stamp_text(value: Any) -> str:
    """时间戳写成文本。手写的不加引号时 YAML 读出来是 datetime 或 date，和字符串放在一起取最大值会抛 TypeError。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return "" if value is None else str(value)


def list_namespaces(workspace_root: Path) -> list[dict[str, Any]]:
    """每个 namespace 一行，带条目数、读不出来的本与最后修改时间。"""
    result: list[dict[str, Any]] = []
    candidates: list[str] = [""]
    candidates.extend(directory.name for directory in namespace_dirs(Path(workspace_root)))
    for name in candidates:
        # 盘上的原名不合规就不列：入口会把首尾空白剥掉，点进去打开的是另一个目录。
        if name and not is_valid_namespace(name):
            continue
        try:
            listing = list_entries(workspace_root, name)
        except MemoryAdminError:
            continue
        entries = listing.entries
        # 空的子目录仍要列出来，人才能把它清掉；根目录没东西就没什么可看的，只剩读不出来的本也得列出来。
        if not entries and not listing.unreadable_books and not name:
            continue
        result.append({
            "namespace": name,
            "kind": _namespace_kind(name),
            "subject": subject_from_namespace(name),
            "entry_count": len(entries),
            "books": sorted({entry["book"] for entry in entries}),
            "unreadable_books": [item["book"] for item in listing.unreadable_books],
            "updated_at": max((_stamp_text(entry.get("updated_at")) for entry in entries), default=""),
        })
    return result


def list_entries(workspace_root: Path, namespace: str) -> MemoryListing:
    """某个 namespace 下全部记忆条目，附带它所在的 book；读不出来的本连同原因另列。"""
    directory = _namespace_dir(workspace_root, namespace)
    listing = MemoryListing()
    if not directory.is_dir():
        return listing
    for path in memory_book_files(directory):
        book = path.stem
        try:
            entries = _load_book(path)["entries"]
        except MemoryBookUnreadable as exc:
            listing.unreadable_books.append({"book": book, "problem": exc.reason})
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["book"] = book
            # 控制台按真假显示常驻：手写成字符串的开关先读成和注入侧一样的结论
            item["constant"] = memory_entry_constant(entry)
            item["enabled"] = memory_entry_enabled(entry)
            item.setdefault("scan_depth", _DEFAULT_SCAN_DEPTH)
            listing.entries.append(item)
    return listing


def upsert_entry(
    workspace_root: Path, namespace: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """新建或整条更新一条记忆。id 已存在即更新。"""
    namespace = _valid_namespace(namespace)
    directory = _namespace_dir(workspace_root, namespace)
    book = _valid_book(payload.get("book"))
    entry_id = _valid_entry_id(payload.get("id"))
    content = str(payload.get("content") or "").strip()
    if not content:
        raise MemoryAdminError("记忆内容不能为空")
    if len(content) > _MAX_CONTENT_LEN:
        raise MemoryAdminError(f"记忆内容超过 {_MAX_CONTENT_LEN} 字")

    keywords = _clean_keywords(payload.get("keywords"))
    constant = memory_entry_constant(payload)
    subject = str(payload.get("subject") or "").strip() or subject_from_namespace(namespace)
    if subject and not is_valid_subject(subject):
        raise MemoryAdminError(f"非法的 subject: {subject}")
    # 与 save_memory 同一条硬约束：三者皆空的条目注入侧会直接跳过，存了也读不到。
    if not keywords and not constant and not subject:
        raise MemoryAdminError("这条记忆永远不会被召回：至少填关键词，或勾选常驻，或指定 subject")

    now = datetime.now(timezone.utc).isoformat()
    path = directory / f"{book}.yaml"
    with _memory_write_lock(Path(workspace_root)):
        try:
            data = _load_book(path)
        except MemoryBookUnreadable as exc:
            raise _unreadable(exc) from exc
        data.setdefault("book", book)
        entries = data["entries"]
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
            "enabled": memory_entry_enabled(payload),
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
    directory = _namespace_dir(workspace_root, namespace)
    book = _valid_book(book)
    entry_id = _valid_entry_id(entry_id)
    path = directory / f"{book}.yaml"
    removed = False
    with _memory_write_lock(Path(workspace_root)):
        if not path.exists():
            return False
        try:
            data = _load_book(path)
        except MemoryBookUnreadable as exc:
            raise _unreadable(exc) from exc
        entries = data["entries"]
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
        _prune_hits(Path(workspace_root), namespace, entry_id)
    return removed


def clear_namespace(workspace_root: Path, namespace: str) -> int:
    """清空一个 namespace 的全部记忆，返回删掉的条目数。读不出来的本照删，不计条数。"""
    namespace = _valid_namespace(namespace)
    directory = _namespace_dir(workspace_root, namespace)
    if not directory.is_dir():
        return 0
    removed = 0
    with _memory_write_lock(Path(workspace_root)):
        for path in memory_book_files(directory):
            try:
                removed += len(_load_book(path)["entries"])
            except MemoryBookUnreadable as exc:
                logger.warning("clearing memory namespace %s drops an unreadable book: %s", namespace, exc)
            path.unlink(missing_ok=True)
        if namespace:
            # 根目录不能删，它是 data/memory 本身。
            with contextlib.suppress(OSError):
                directory.rmdir()
    _invalidate_cache(Path(workspace_root), memory_book=namespace)
    _prune_hits(Path(workspace_root), namespace)
    return removed


def _prune_hits(workspace_root: Path, namespace: str, entry_id: str = "") -> None:
    """摘掉命中戳。留着的话 14 天扫除会把新建的同 id 记忆当成老记忆判龄。"""
    from engine.memory_hit_cache import prune_entry_hits, prune_namespace_hits

    try:
        if entry_id:
            prune_entry_hits(workspace_root, namespace, entry_id)
        elif namespace:
            prune_namespace_hits(workspace_root, namespace)
    except Exception:
        logger.debug("prune hit cache failed", exc_info=True)
