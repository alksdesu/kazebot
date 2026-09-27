"""Persistent session registry — data/sessions.json.

将 session 信息独立持久化，不再依赖 eventlog 的 session_created 事件回放。
即使 eventlog 因文件轮转或内存截断丢失了 session_created 事件，
session 信息仍可从此文件完整恢复。

文件格式：
{
  "af4fdfc5-...": {
    "session_id": "af4fdfc5-...",
    "channel": "qq_private",
    "conversation_key": "qq_private:1491668801836548166",
    "created_at": "2026-04-15T...",
    "reset": false,
    "entry_node_id": "ereuna_main"
  },
  ...
}

写入策略：
- 内存中维护完整 registry 副本，写入时直接序列化内存数据
- 原子写入：先写临时文件，再 os.replace 覆盖目标文件
- 线程安全：所有写入方法由调用方在 SupervisorState._lock 内调用
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.conversation_migration import ConversationRekey, RekeyCount

from ._helpers import SessionInfo, _now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistrySnapshot:
    generation: int
    registry: dict[str, dict[str, Any]]


class SessionStore:
    """管理 data/sessions.json 的读写。

    线程安全：写入方法（on_session_created / on_session_reset / remove_session）
    由调用方在 SupervisorState._lock 内调用，无需自带锁。
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        # 内存中保存完整的 raw dict 副本，所有访问均经公共线程安全 API。
        self._registry: dict[str, dict[str, Any]] = {}
        self._generation = 0
        self._flushed_generation = 0

    # ------------------------------------------------------------------ #
    #  启动时加载
    # ------------------------------------------------------------------ #

    def load(self) -> tuple[dict[str, SessionInfo], dict[str, str], dict[tuple[str, str, str], str], dict[str, set[str]]]:
        """从 sessions.json 加载 session 注册表。

        Returns:
            (sessions, conversation_map, child_session_map, parent_children)
            - sessions: session_id -> SessionInfo（仅活跃 session，不含 reset）
            - conversation_map: conversation_key -> session_id
            - child_session_map: (parent_sid, node_id, context_key) -> child_session_id
            - parent_children: parent_session_id -> set of child_session_ids

        Child Session 隔离（Phase A）：启动时从 is_child=true 的 entry 重建映射。
        如果文件不存在或损坏，优雅降级为空 dict。
        """
        if not self._path.exists():
            logger.info("sessions.json not found; will be created on first session")
            # Child Session 隔离（Phase A）：返回 4-tuple 与签名一致
            return {}, {}, {}, {}

        try:
            raw = self._path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}, {}, {}, {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning(
                    "sessions.json: root is %s, expected dict; skipping",
                    type(data).__name__,
                )
                return {}, {}, {}, {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("sessions.json: load failed (%s); skipping", exc)
            return {}, {}, {}, {}

        sessions: dict[str, SessionInfo] = {}
        conv_map: dict[str, str] = {}
        # Child Session 隔离（Phase A）：重建 child session 映射
        child_map: dict[tuple[str, str, str], str] = {}
        parent_children: dict[str, set[str]] = {}

        for sid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # 无论 reset 与否，都保留到内存 registry（完整镜像）
            self._registry[sid] = entry

            # reset 的 session 不加入活跃集合
            if entry.get("reset"):
                continue

            # Child Session 隔离：重建 child session 映射表
            if entry.get("is_child"):
                parent_sid = str(entry.get("parent_session_id") or "")
                node_id = str(entry.get("node_id") or "")
                ctx_key = str(entry.get("context_key") or "")
                if parent_sid:
                    map_key = (parent_sid, node_id, ctx_key)
                    child_map[map_key] = sid
                    parent_children.setdefault(parent_sid, set()).add(sid)
                continue  # child session 不加入 sessions/conv_map

            try:
                created_str = entry.get("created_at")
                created_at = (
                    datetime.fromisoformat(created_str)
                    if isinstance(created_str, str)
                    else _now()
                )
                updated_str = entry.get("updated_at")
                updated_at = (
                    datetime.fromisoformat(updated_str)
                    if isinstance(updated_str, str) and updated_str
                    else created_at
                )
                info = SessionInfo(
                    session_id=str(entry.get("session_id") or sid),
                    channel=str(entry.get("channel") or ""),
                    conversation_key=str(entry.get("conversation_key") or ""),
                    created_at=created_at,
                    updated_at=updated_at,
                    # Why: older sessions.json files do not have entry_node_id.
                    # How: normalize a missing or empty value to "" during load.
                    # Purpose: schema extension remains backward compatible while
                    # still restoring per-session routing when the field exists.
                    entry_node_id=str(entry.get("entry_node_id") or ""),
                    # [AutoC 2026-06-01] Why: provider_override is a new optional
                    # sessions.json field. How: accept only dict values and copy
                    # them into SessionInfo. Purpose: old rows load as empty
                    # overrides and malformed rows cannot leak non-dict data.
                    provider_override=dict(entry.get("provider_override") or {}) if isinstance(entry.get("provider_override"), dict) else {},
                )
                sessions[info.session_id] = info
                if info.conversation_key:
                    conv_map[info.conversation_key] = info.session_id
            except Exception as exc:
                logger.warning("sessions.json: bad entry %s (%s); skipping", sid, exc)

        with self._lock:
            if self._registry:
                self._generation = 1
                self._flushed_generation = 1
        logger.info(
            "sessions.json: loaded %d active sessions, %d child sessions (%d total entries)",
            len(sessions),
            len(child_map),
            len(self._registry),
        )
        return sessions, conv_map, child_map, parent_children

    def reload(self) -> tuple[dict[str, SessionInfo], dict[str, str], dict[tuple[str, str, str], str], dict[str, set[str]]]:
        """丢掉内存副本重读 sessions.json，返回与 load 相同的四元组。

        只在离线改动（账号迁移改写 conversation_key）之后调用：内存才是权威，
        不重读的话下一次 flush 会把盘上刚改好的内容原样覆盖回去。
        """
        with self._lock:
            self._registry.clear()
        result = self.load()
        with self._lock:
            # 内存此刻等于盘上内容，标成已落盘，免得立刻回写一遍。
            self._generation += 1
            self._flushed_generation = self._generation
        return result

    # ------------------------------------------------------------------ #
    #  Thread-safe registry API
    # ------------------------------------------------------------------ #

    def _changed_locked(self) -> RegistrySnapshot:
        self._generation += 1
        return RegistrySnapshot(self._generation, copy.deepcopy(self._registry))

    def snapshot(self) -> RegistrySnapshot:
        """Copy one immutable registry generation without exposing shared dicts."""
        with self._lock:
            return RegistrySnapshot(self._generation, copy.deepcopy(self._registry))

    def get_entry(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._registry.get(session_id)
            return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def items_snapshot(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(sid, copy.deepcopy(entry)) for sid, entry in self._registry.items()]

    def keys_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._registry.keys())

    def upsert_entry(
        self, session_id: str, entry: dict[str, Any], *, flush: bool = True,
    ) -> RegistrySnapshot:
        with self._lock:
            self._registry[session_id] = copy.deepcopy(entry)
            snapshot = self._changed_locked()
        if flush:
            self.flush_snapshot(snapshot)
        return snapshot

    def set_entry_field(
        self, session_id: str, field: str, value: Any, *, flush: bool = True,
    ) -> RegistrySnapshot | None:
        with self._lock:
            entry = self._registry.get(session_id)
            if entry is None:
                return None
            entry[field] = copy.deepcopy(value)
            snapshot = self._changed_locked()
        if flush:
            self.flush_snapshot(snapshot)
        return snapshot

    def remove_sessions(
        self, session_ids: list[str] | set[str], *, flush: bool = True,
    ) -> RegistrySnapshot | None:
        with self._lock:
            changed = False
            for session_id in session_ids:
                changed = self._registry.pop(str(session_id), None) is not None or changed
            if not changed:
                return None
            snapshot = self._changed_locked()
        if flush:
            self.flush_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------ #
    #  写入接口
    # ------------------------------------------------------------------ #

    def on_session_created(self, info: SessionInfo) -> None:
        """Persist a new session through one versioned immutable snapshot."""
        with self._lock:
            self._registry[info.session_id] = {
                "session_id": info.session_id,
                "channel": info.channel,
                "conversation_key": info.conversation_key,
                "created_at": info.created_at.isoformat(),
                "updated_at": info.updated_at.isoformat() if info.updated_at else info.created_at.isoformat(),
                "reset": False,
                "entry_node_id": info.entry_node_id,
                "provider_override": dict(info.provider_override or {}),
            }
            snapshot = self._changed_locked()
        self.flush_snapshot(snapshot)

    def update_provider_override(self, session_id: str, provider_override: dict[str, Any]) -> None:
        """更新 session 的 provider_override 并落盘。"""
        self.set_entry_field(session_id, "provider_override", dict(provider_override or {}))

    def update_entry_node(self, session_id: str, entry_node_id: str) -> None:
        """更新 session 的入口节点并落盘。"""
        self.set_entry_field(session_id, "entry_node_id", entry_node_id)

    def on_session_reset(self, session_id: str) -> None:
        """将 session 标记为已重置。"""
        self.set_entry_field(session_id, "reset", True)

    def remove_session(self, session_id: str) -> None:
        """从 sessions.json 中物理删除一个 session 条目。

        [AutoC 2026-05-30] Why: branch 和 fresh/fork child session 完成后只标记 reset
        不删除，导致 sessions.json 无限膨胀（18000+ 条目）。
        How: 从 registry 中 pop 并 flush。
        Purpose: 立即释放已完成 branch 和已过期 child 的注册记录。
        """
        self.remove_sessions([session_id])

    def on_child_session_created(
        self,
        child_session_id: str,
        parent_session_id: str,
        node_id: str,
        context_key: str,
    ) -> None:
        """持久化一个新创建的 child session。

        Child Session 隔离（Phase A）：将 child session 信息写入 sessions.json，
        包含 is_child、parent_session_id、node_id、context_key 等字段，
        供 load() 时重建 child_session_map。
        """
        now_str = _now().isoformat()
        # Inherit parent's conversation_key so child tasks can resolve
        # the originating channel (e.g. for approval UI routing).
        with self._lock:
            parent_info = self._registry.get(parent_session_id)
            parent_conv_key = parent_info.get("conversation_key", "") if parent_info else ""
            self._registry[child_session_id] = {
                "session_id": child_session_id,
                "channel": "internal",
                "conversation_key": parent_conv_key,
                "created_at": now_str,
                "reset": False,
                "is_child": True,
                "parent_session_id": parent_session_id,
                "node_id": node_id,
                "context_key": context_key,
                "context_mode": "",
                "last_active_at": now_str,
            }
            snapshot = self._changed_locked()
        self.flush_snapshot(snapshot)

    def touch_updated_at(self, session_id: str, updated_at: datetime) -> None:
        """Sync updated_at into the registry (no immediate flush).

        [AutoC 2026-06-04] Why: _apply_inbound/outbound_message sets
        si.updated_at in memory, but the registry dict is never refreshed,
        so sessions.json keeps updated_at=None. On restart, load() falls back
        to created_at and sidebar ordering is stuck at creation time.
        How: update the registry entry in memory; periodic _flush persists it.
        Purpose: updated_at survives restarts without per-message disk writes.
        """
        with self._lock:
            entry = self._registry.get(session_id)
            if entry is not None and not entry.get("is_child") and not entry.get("reset"):
                entry["updated_at"] = updated_at.isoformat()
                self._generation += 1

    def update_last_active(self, child_session_id: str) -> None:
        """更新 child session 的 last_active_at 时间戳。

        Child Session 隔离（Phase A）：在 child session 被 accumulate 模式复用时，
        以及子 task 创建/完成时调用，用于 TTL 过期判定。
        """
        with self._lock:
            entry = self._registry.get(child_session_id)
            if entry is None or not entry.get("is_child"):
                return
            entry["last_active_at"] = _now().isoformat()
            snapshot = self._changed_locked()
        self.flush_snapshot(snapshot)

    # ------------------------------------------------------------------ #
    #  内部：原子写入
    # ------------------------------------------------------------------ #

    def flush_snapshot(self, snapshot: RegistrySnapshot) -> bool:
        """Write one immutable generation; stale snapshots never replace newer state."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".sessions_g{snapshot.generation}_",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(snapshot.registry, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            retry_snapshot: RegistrySnapshot | None = None
            with self._lock:
                if snapshot.generation <= self._flushed_generation:
                    return True
                if snapshot.generation < self._generation:
                    retry_snapshot = RegistrySnapshot(
                        self._generation, copy.deepcopy(self._registry),
                    )
                else:
                    os.replace(tmp_path, str(self._path))
                    tmp_path = None
                    self._flushed_generation = snapshot.generation
            if retry_snapshot is not None:
                # Flush the newest immutable view; never let the old file win.
                return self.flush_snapshot(retry_snapshot)
            return True
        except Exception as exc:
            logger.error("sessions.json: snapshot flush failed: %s", exc)
            return False
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _flush(self) -> bool:
        """Compatibility wrapper that snapshots under lock and writes lock-free."""
        return self.flush_snapshot(self.snapshot())

    def rekey_conversations(self, rekey: ConversationRekey, *, apply: bool) -> RekeyCount:
        """在线迁移用：直接改内存里的注册表再落盘。先改文件再重读的话，中间任何一次落盘都会拿旧键盖回去。"""
        with self._lock:
            registry = self._registry if apply else copy.deepcopy(self._registry)
            count = _rekey_registry(registry, rekey) if rekey else RekeyCount()
            if not apply or not count:
                return count
            snapshot = self._changed_locked()
        if not self.flush_snapshot(snapshot):
            raise OSError(f"sessions.json: rekeyed registry could not be written to {self._path}")
        return count


def session_registry_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / "data" / "sessions.json"


def rekey_session_registry(path: Path, rekey: ConversationRekey, *, apply: bool) -> RekeyCount:
    """会话注册表里挂在旧会话键下的条目改挂到新键，对话历史按 session_id 跟着走。

    新键下原来就有活跃会话时源会话胜出，目标那条标成已重置：同一个键两条活跃会话，加载时谁赢
    取决于文件里的先后。老式派发的 agent: 会话键里嵌着父会话键，一并换掉。
    """
    registry = _read_registry(path)
    if not registry or not rekey:
        return RekeyCount()
    count = _rekey_registry(registry, rekey)
    if apply and count:
        _write_registry(path, registry)
    return count


def _rekey_registry(registry: dict[str, Any], rekey: ConversationRekey) -> RekeyCount:
    moved: set[str] = set()
    claimed: set[str] = set()
    for session_id, row in registry.items():
        if not isinstance(row, dict):
            continue
        found = _rekeyed_conversation_key(str(row.get("conversation_key") or "").strip(), rekey)
        if found is None:
            continue
        old, new, rewritten = found
        row["conversation_key"] = rewritten
        rekey.rewrite(row, old, new, channels=("channel",))
        moved.add(session_id)
        if _is_active_root(row):
            claimed.add(rewritten)
    replaced = 0
    for session_id, row in registry.items():
        if session_id in moved or not isinstance(row, dict) or not _is_active_root(row):
            continue
        if str(row.get("conversation_key") or "").strip() in claimed:
            row["reset"] = True
            replaced += 1
    return RekeyCount(len(moved), replaced)


def _rekeyed_conversation_key(key: str, rekey: ConversationRekey) -> tuple[str, str, str] | None:
    """(旧会话键, 新会话键, 换过之后的这个键)；agent:<节点>[:<上下文>]:<父会话键>[:<uuid>] 按嵌着的父会话键认。"""
    new = rekey.key(key)
    if new is not None:
        return key, new, new
    if not key.startswith("agent:"):
        return None
    for old, new in rekey.items():
        if key.endswith(f":{old}"):
            return old, new, key[: -len(old)] + new
        if f":{old}:" in key:
            return old, new, key.replace(f":{old}:", f":{new}:", 1)
    return None


def _is_active_root(row: dict[str, Any]) -> bool:
    return not row.get("reset") and not row.get("is_child")


def _read_registry(path: Path) -> dict[str, Any] | None:
    """没有或空文件是 None；读不懂就抛：当成空表会让没搬的会话看起来像搬完了。"""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"sessions.json root is {type(data).__name__}, expected an object: {path}")
    return data


def _write_registry(path: Path, registry: dict[str, Any]) -> None:
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".sessions_rekey_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(registry, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
