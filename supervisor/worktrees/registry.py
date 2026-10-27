"""隔离副本的登记，落在 data/worktrees.json：每个副本的位置、仓库、基准提交、分支、用它的任务和状态。

只有 supervisor 写。内存里这份是准的，文件在启动时读一次；改动都整份替换写回，写不进去只记日志。
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REGISTRY_FILE = "worktrees.json"
VERSION = 1

CREATING = "creating"
READY = "ready"
FINISHED = "finished"
MERGING = "merging"
MERGED = "merged"
ABANDONED = "abandoned"
EXPIRED = "expired"
FAILED = "failed"
REMOVED = "removed"

STATES = frozenset({CREATING, READY, FINISHED, MERGING, MERGED, ABANDONED, EXPIRED, FAILED, REMOVED})
# 目录还在用或还留着等合回的
LIVE_STATES = frozenset({CREATING, READY, FINISHED, MERGING})
# 接着用（continue_from、异步结果回到原会话）时能直接沿用的
REUSABLE_STATES = frozenset({READY, FINISHED})
# 等清理线程删目录和分支的
CLEANUP_STATES = frozenset({MERGED, ABANDONED, EXPIRED, FAILED})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def registry_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "data" / REGISTRY_FILE


def _clean_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    worktree_id = str(raw.get("id") or "").strip()
    state = str(raw.get("state") or "").strip()
    if not worktree_id or state not in STATES:
        return None
    entry = dict(raw)
    owners = raw.get("owners")
    entry["owners"] = [str(item) for item in owners if str(item or "").strip()] if isinstance(owners, list) else []
    links = raw.get("links")
    entry["links"] = [str(item) for item in links if str(item or "").strip()] if isinstance(links, list) else []
    return entry


class WorktreeRegistry:
    """登记表。方法各自持自己的锁，不会回头去拿 supervisor 的锁；调用方可以持着 state._lock 来调。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self.load_error = ""

    def load(self) -> None:
        """读一次文件；读不出来时把坏文件改名留着，从空表开始，副本目录留给人手工处理。"""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                return
            except OSError as exc:
                self.load_error = f"{self.path} 读不出来：{exc}"
                log.error("%s", self.load_error)
                return
            try:
                data = json.loads(raw.decode("utf-8"))
                items = data.get("worktrees") if isinstance(data, dict) else None
                if not isinstance(items, dict):
                    raise ValueError("没有 worktrees 映射")
            except (UnicodeDecodeError, ValueError) as exc:
                self.load_error = f"{self.path} 格式不对（{exc}），已改名留存，登记从空表开始"
                log.error("%s", self.load_error)
                with contextlib.suppress(OSError):
                    os.replace(self.path, self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}"))
                return
            for key, value in items.items():
                entry = _clean_entry(value)
                if entry is not None and entry["id"] == key:
                    self._entries[key] = entry

    def _save_locked(self) -> None:
        payload = {"version": VERSION, "worktrees": self._entries}
        data = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as stream:
                stream.write(data)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("writing the worktree registry %s failed: %s", self.path, exc)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def get(self, worktree_id: str) -> dict[str, Any] | None:
        with self._lock:
            self.load()
            entry = self._entries.get(str(worktree_id or "").strip())
            return copy.deepcopy(entry) if entry is not None else None

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            self.load()
            return [copy.deepcopy(entry) for entry in self._entries.values()]

    def owned_by(self, task_id: str) -> dict[str, Any] | None:
        """task_id 是哪个副本的主人（建它的子任务，或接着用它的任务）；不是给 None。"""
        tid = str(task_id or "").strip()
        if not tid:
            return None
        with self._lock:
            self.load()
            for entry in self._entries.values():
                if tid in entry.get("owners", ()):
                    return copy.deepcopy(entry)
            return None

    def put(self, entry: dict[str, Any]) -> dict[str, Any]:
        cleaned = _clean_entry(entry)
        if cleaned is None:
            raise ValueError(f"登记项不完整：{entry!r}")
        with self._lock:
            self.load()
            cleaned.setdefault("created_at", now_iso())
            cleaned["updated_at"] = now_iso()
            self._entries[cleaned["id"]] = cleaned
            self._save_locked()
            return copy.deepcopy(cleaned)

    def update(self, worktree_id: str, **fields: Any) -> dict[str, Any] | None:
        """改登记项的几个字段并写回；没有这一项给 None。owners 给的是要追加的任务号。"""
        with self._lock:
            self.load()
            entry = self._entries.get(str(worktree_id or "").strip())
            if entry is None:
                return None
            for key, value in fields.items():
                if key == "owners":
                    for task_id in value or ():
                        if task_id and task_id not in entry["owners"]:
                            entry["owners"].append(str(task_id))
                    continue
                if key == "state" and value not in STATES:
                    raise ValueError(f"认不出的状态：{value!r}")
                entry[key] = value
            entry["updated_at"] = now_iso()
            self._save_locked()
            return copy.deepcopy(entry)

    def transition(self, worktree_id: str, expected: frozenset[str] | set[str], state: str, **fields: Any) -> dict[str, Any] | None:
        """状态在 expected 里时才改成 state；改成了返回改后的登记项，没改给 None。"""
        with self._lock:
            self.load()
            entry = self._entries.get(str(worktree_id or "").strip())
            if entry is None or entry.get("state") not in expected:
                return None
            return self.update(worktree_id, state=state, **fields)

    def remove(self, worktree_id: str) -> None:
        with self._lock:
            self.load()
            if self._entries.pop(str(worktree_id or "").strip(), None) is not None:
                self._save_locked()
