"""文件认领和读戳的记账：谁正在改哪个文件，每个任务、每个会话读到过哪一版。

认领只挡别的还没结束的任务，跨会话也算；挂起的任务照样持有，它在等的子任务可以在它里层接着认领。
任务一到终态就放掉认领，它记下的读戳并进会话层。engine 经 /v1/file_edits 的几个接口来问。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from toolbox import file_state

from .types import RouteStatus, Task, TaskStatus

log = logging.getLogger(__name__)

# complete_task 先把派发子任务的任务记成 completed，路由里才改成 suspended：这段路上它的认领不能放
_DISPATCH_TRANSIT_GRACE = timedelta(minutes=5)
# 沿等待链往上找的最多层数，防着坏数据里的环
_MAX_CHAIN_DEPTH = 32
_DISPLAY_CHARS = 500
_SUSPENDED = TaskStatus.suspended.value


@dataclass(frozen=True)
class TaskView:
    """记账要知道的任务情况。live：还没到终态，或正在挂起的路上；时间都是 epoch 秒。"""

    task_id: str
    live: bool
    status: str = ""
    node_id: str = ""
    session_id: str = ""
    caller_task_id: str = ""
    created_at: float = 0.0
    ended_at: float | None = None


Resolve = Callable[[str], TaskView | None]


@dataclass(frozen=True)
class Holder:
    task_id: str
    display: str
    since: float


@dataclass(frozen=True)
class ClaimOutcome:
    """ok 时 new 表示这次才认领；不 ok 时 holder 是挡住的那个持有者，view 是它的任务情况。"""

    ok: bool
    new: bool = False
    holder: Holder | None = None
    view: TaskView | None = None


def _waits_for(requester: TaskView, ancestor_id: str, resolve: Resolve) -> bool:
    """requester 沿同步委派（caller_task_id）往上能走到 ancestor_id；中途断在已结束的任务上就不算。"""
    current = requester
    for _ in range(_MAX_CHAIN_DEPTH):
        parent_id = current.caller_task_id
        if not parent_id:
            return False
        if parent_id == ancestor_id:
            return True
        parent = resolve(parent_id)
        if parent is None or not parent.live:
            return False
        current = parent
    return False


class FileClaimBook:
    """按规范化的绝对路径记谁认领了文件。一个文件可以有一串持有者：挂起的任务在外层，它同步等着的子任务在里层。

    不带锁，由 FileLedger 持锁调用。持有者是否还活着每次都按 resolve 现查，没赶上释放的已结束任务也挡不住别人。
    """

    def __init__(self) -> None:
        self._paths: dict[str, list[Holder]] = {}
        self._by_task: dict[str, set[str]] = {}

    def acquire(self, path: str, requester: TaskView, display: str, *, resolve: Resolve, now: float) -> ClaimOutcome:
        holders = self._live_holders(path, resolve)
        if not holders:
            self._push(path, Holder(requester.task_id, display, now))
            return ClaimOutcome(True, new=True)
        deepest = holders[-1]
        if deepest.task_id == requester.task_id:
            return ClaimOutcome(True)
        view = resolve(deepest.task_id)
        # 同步委派出去的子任务替挂起的调用方干活：调用方恢复之前改不了文件，不会撞车；兄弟子任务之间照样互斥
        nested = (
            view is not None
            and view.status == _SUSPENDED
            and all(holder.task_id != requester.task_id for holder in holders)
            and _waits_for(requester, deepest.task_id, resolve)
        )
        if nested:
            self._push(path, Holder(requester.task_id, display, now))
            return ClaimOutcome(True, new=True)
        return ClaimOutcome(False, holder=deepest, view=view)

    def check(self, path: str, *, resolve: Resolve) -> ClaimOutcome:
        """不登记、只看有没有别的任务正在改：没有任务号的写入方用它。"""
        holders = self._live_holders(path, resolve)
        if not holders:
            return ClaimOutcome(True)
        deepest = holders[-1]
        return ClaimOutcome(False, holder=deepest, view=resolve(deepest.task_id))

    def release(self, task_id: str, path: str | None = None) -> int:
        """放掉 task_id 在 path（不给就是全部）上的认领，返回放掉几处。"""
        owned = self._by_task.get(task_id)
        targets = [path] if path is not None else sorted(owned or ())
        released = 0
        for key in targets:
            holders = self._paths.get(key)
            if not holders:
                continue
            kept = [holder for holder in holders if holder.task_id != task_id]
            released += len(holders) - len(kept)
            self._store(key, kept)
            if owned is not None:
                owned.discard(key)
        if owned is not None and not owned:
            self._by_task.pop(task_id, None)
        return released

    def sweep(self, resolve: Resolve) -> int:
        """清掉已结束任务留下的认领，返回清掉几处。"""
        dropped = 0
        for key in list(self._paths):
            before = len(self._paths.get(key) or ())
            dropped += before - len(self._live_holders(key, resolve))
        return dropped

    def held_by(self, task_id: str) -> list[str]:
        return sorted(self._by_task.get(task_id) or ())

    def paths(self) -> list[tuple[str, tuple[Holder, ...]]]:
        return [(key, tuple(holders)) for key, holders in sorted(self._paths.items())]

    def _live_holders(self, path: str, resolve: Resolve) -> list[Holder]:
        holders = self._paths.get(path)
        if not holders:
            return []
        live = [holder for holder in holders if (view := resolve(holder.task_id)) is not None and view.live]
        if len(live) != len(holders):
            for holder in holders:
                if holder not in live:
                    self._forget(holder.task_id, path)
            self._store(path, live)
        return live

    def _push(self, path: str, holder: Holder) -> None:
        self._paths.setdefault(path, []).append(holder)
        self._by_task.setdefault(holder.task_id, set()).add(path)

    def _store(self, path: str, holders: list[Holder]) -> None:
        if holders:
            self._paths[path] = holders
        else:
            self._paths.pop(path, None)

    def _forget(self, task_id: str, path: str) -> None:
        owned = self._by_task.get(task_id)
        if owned is None:
            return
        owned.discard(path)
        if not owned:
            self._by_task.pop(task_id, None)


def _iso(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def holder_payload(holder: Holder, view: TaskView | None) -> dict[str, Any]:
    return {
        "task_id": holder.task_id,
        "node_id": view.node_id if view is not None else "",
        "status": view.status if view is not None else "",
        "session_id": view.session_id if view is not None else "",
        "path": holder.display,
        "since": _iso(holder.since),
    }


class FileLedger:
    """一个 supervisor 一份：认领加读戳。resolve 由调用方给；读的是 supervisor 的任务表时，调用方要持着 state._lock。"""

    def __init__(self, *, stamps: file_state.StampBook | None = None) -> None:
        self.claims = FileClaimBook()
        self.stamps = stamps if stamps is not None else file_state.StampBook()
        # 任务第一次问到的编辑日志会话：发起链上的任务被回收之后再算，答案可能变，一个任务的日志不能分两处记
        self._journal_sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def begin(
        self,
        *,
        task_id: str,
        session: str,
        path: str,
        display: str,
        journal_session: str,
        resolve: Resolve,
        now: float,
    ) -> dict[str, Any]:
        """认领 path 并取回 task_id 比对用的戳。没有任务号时只看有没有别的任务在改，不登记认领。"""
        with self._lock:
            self._sweep_locked(resolve, now)
            requester = resolve(task_id) if task_id else None
            if task_id:
                if requester is None:
                    return {"ok": False, "reason": "task_unknown"}
                if not requester.live:
                    return {"ok": False, "reason": "task_ended", "status": requester.status}
                outcome = self.claims.acquire(path, requester, display, resolve=resolve, now=now)
                journal = self._journal_sessions.setdefault(task_id, journal_session or session)
            else:
                outcome = self.claims.check(path, resolve=resolve)
                journal = journal_session or session
            if not outcome.ok:
                holder = holder_payload(outcome.holder, outcome.view) if outcome.holder is not None else {}
                return {"ok": False, "reason": "claimed", "holder": holder}
            lookup = self.stamps.lookup(
                session, task_id, path, started_at=requester.created_at if requester is not None else None,
            )
            return {
                "ok": True,
                "new_claim": outcome.new,
                "journal_session": journal,
                "stamp": file_state.entry_to_wire(lookup.entry),
                "stamp_layer": lookup.layer,
                "foreign": lookup.foreign,
            }

    def record(self, *, task_id: str, session: str, path: str, entry: file_state.Entry, now: float) -> dict[str, Any]:
        layer = self.stamps.record(session, task_id, path, entry, now=now)
        return {"ok": True, "layer": layer}

    def release(self, *, task_id: str, path: str) -> dict[str, Any]:
        with self._lock:
            return {"ok": True, "released": self.claims.release(task_id, path) > 0}

    def scope(self, task_id: str, *, journal_session: str, view: TaskView | None) -> dict[str, Any]:
        """编辑日志记在哪个会话。任务还活着时把答案定下来，之后的认领沿用同一个。"""
        if view is None:
            return {"ok": False, "reason": "task_unknown"}
        with self._lock:
            if view.live and journal_session:
                journal = self._journal_sessions.setdefault(task_id, journal_session)
            else:
                journal = self._journal_sessions.get(task_id, journal_session)
        return {
            "ok": True,
            "task_id": task_id,
            "journal_session": journal,
            "node_id": view.node_id,
            "status": view.status,
        }

    def end_task(self, task_id: str, *, ended_at: float) -> None:
        """任务到了终态：放掉它的全部认领，读戳并进会话层。"""
        with self._lock:
            self.claims.release(task_id)
            self.stamps.merge_task(task_id, at=ended_at)
            self._journal_sessions.pop(task_id, None)

    def sweep(self, resolve: Resolve, now: float) -> None:
        with self._lock:
            self._sweep_locked(resolve, now)

    def describe(self, resolve: Resolve, *, task_id: str = "") -> list[dict[str, Any]]:
        """还有效的认领，供排查和控制台看；给了 task_id 只列它持有的。"""
        with self._lock:
            self.claims.sweep(resolve)
            rows: list[dict[str, Any]] = []
            for path, holders in self.claims.paths():
                if task_id and all(holder.task_id != task_id for holder in holders):
                    continue
                rows.append({
                    "path": path,
                    "holders": [holder_payload(holder, resolve(holder.task_id)) for holder in holders],
                })
            return rows

    def _sweep_locked(self, resolve: Resolve, now: float) -> None:
        # 终态钩子没赶上的任务在这里补收：认领放掉，读戳按它结束的时刻并进会话层
        self.claims.sweep(resolve)
        for task_id in self.stamps.task_ids():
            view = resolve(task_id)
            if view is not None and view.live:
                continue
            ended = view.ended_at if view is not None and view.ended_at is not None else now
            self.stamps.merge_task(task_id, at=min(now, ended))
            self._journal_sessions.pop(task_id, None)
        for task_id in list(self._journal_sessions):
            view = resolve(task_id)
            if view is None or not view.live:
                self._journal_sessions.pop(task_id, None)


def _live_locked(state: Any, task: Task, now: datetime) -> bool:
    if not state._task_terminal(task):
        return True
    action = str((task.result or {}).get("action") or "").strip()
    return (
        task.status == TaskStatus.completed
        and action == "dispatch"
        and task.route_status != RouteStatus.routed
        and now - task.updated_at < _DISPATCH_TRANSIT_GRACE
    )


def _view_locked(state: Any, task_id: str, now: datetime) -> TaskView | None:
    task = state.tasks.get(str(task_id or "").strip())
    if task is None:
        return None
    live = _live_locked(state, task, now)
    return TaskView(
        task_id=task.task_id,
        live=live,
        status=task.status.value,
        node_id=str(task.node_id or task.tool_name or ""),
        # 入口分支随时会拆掉，告诉别的任务的是它归属的持久会话
        session_id=str(state._route_session_id_for_task_locked(task) or task.session_id),
        caller_task_id=str(task.caller_task_id or "").strip(),
        created_at=task.created_at.timestamp(),
        ended_at=None if live else task.updated_at.timestamp(),
    )


def _resolver_locked(state: Any, now: datetime) -> Resolve:
    return lambda task_id: _view_locked(state, task_id, now)


def _journal_session_locked(state: Any, task: Task | None, fallback: str) -> str:
    """派出去的子任务把改动记在会话家族的根会话下，父节点用 undo_edits 才看得到、撤得了。"""
    if task is None:
        return fallback
    family_root = getattr(state, "_task_family_root_locked", None)
    if not callable(family_root):
        return fallback
    try:
        root = str(family_root(task) or "").strip()
    except Exception:
        log.warning("finding the session family root of task %s failed", task.task_id, exc_info=True)
        return fallback
    return root or fallback


def ledger_of(state: Any) -> FileLedger:
    with state._lock:
        ledger = getattr(state, "file_ledger", None)
        if not isinstance(ledger, FileLedger):
            ledger = FileLedger()
            state.file_ledger = ledger
        return ledger


def release_if_ended_locked(state: Any, task: Task) -> None:
    """任务到了终态就放掉它的认领、把读戳并进会话层；正在挂起路上的任务不动。调用方持有 state._lock，不会抛异常。"""
    ledger = getattr(state, "file_ledger", None)
    if not isinstance(ledger, FileLedger):
        return
    try:
        now = datetime.now(timezone.utc)
        if _live_locked(state, task, now):
            return
        ledger.end_task(task.task_id, ended_at=now.timestamp())
    except Exception:
        log.exception("releasing the file claims of task %s failed", getattr(task, "task_id", "?"))


class FileEditBeginIn(BaseModel):
    task_id: str = ""
    session: str = ""
    path: str
    display: str = ""


class FileStampIn(BaseModel):
    task_id: str = ""
    session: str = ""
    path: str
    stamp: dict[str, Any]
    revealable: bool = False


class FileClaimReleaseIn(BaseModel):
    task_id: str
    path: str


def _wire_path(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or not os.path.isabs(text):
        raise HTTPException(status_code=422, detail="path 要是规范化之后的绝对路径")
    return file_state.path_key(Path(text))


def create_router(state: Any) -> APIRouter:
    from .admin_api import verify_admin_token

    router = APIRouter(prefix="/v1/file_edits", dependencies=[Depends(verify_admin_token)])
    ledger = ledger_of(state)

    @router.post("/begin")
    def begin(body: FileEditBeginIn) -> dict[str, Any]:
        path = _wire_path(body.path)
        task_id = body.task_id.strip()
        session = body.session.strip()
        display = (body.display.strip() or path)[:_DISPLAY_CHARS]
        with state._lock:
            now = datetime.now(timezone.utc)
            task = state.tasks.get(task_id) if task_id else None
            return ledger.begin(
                task_id=task_id,
                session=session,
                path=path,
                display=display,
                journal_session=_journal_session_locked(state, task, session),
                resolve=_resolver_locked(state, now),
                now=now.timestamp(),
            )

    @router.post("/stamp")
    def stamp(body: FileStampIn) -> dict[str, Any]:
        path = _wire_path(body.path)
        parsed = file_state.stamp_from_wire(body.stamp)
        if parsed is None:
            raise HTTPException(status_code=422, detail="stamp 要有整数的 mtime_ns、size 和 64 位十六进制的 sha256")
        return ledger.record(
            task_id=body.task_id.strip(),
            session=body.session.strip(),
            path=path,
            entry=file_state.Entry(parsed, body.revealable),
            now=time.time(),
        )

    @router.post("/release")
    def release(body: FileClaimReleaseIn) -> dict[str, Any]:
        task_id = body.task_id.strip()
        if not task_id:
            raise HTTPException(status_code=422, detail="task_id required")
        return ledger.release(task_id=task_id, path=_wire_path(body.path))

    @router.get("/scope")
    def scope(task_id: str = Query(...)) -> dict[str, Any]:
        tid = task_id.strip()
        with state._lock:
            now = datetime.now(timezone.utc)
            task = state.tasks.get(tid) if tid else None
            return ledger.scope(
                tid,
                journal_session=_journal_session_locked(state, task, ""),
                view=_view_locked(state, tid, now) if task is not None else None,
            )

    @router.get("/claims")
    def claims(task_id: str = Query("")) -> dict[str, Any]:
        with state._lock:
            now = datetime.now(timezone.utc)
            return {"claims": ledger.describe(_resolver_locked(state, now), task_id=task_id.strip())}

    return router
