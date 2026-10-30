"""读后改前的校验：read_file 读到的文本文件记一个戳，改已有文件之前拿它比对；写盘前在跨进程锁里再比一次。

戳由 supervisor 记账：先看本任务自己记下的，没有再看会话里已结束的任务留下的；只读了部分行也记整份文件的戳。
改文件之前先向 supervisor 认领它，别的还没结束的任务正在改的文件改不了。"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

from file_lock import exclusive_lock

from .text_files import replace_bytes

logger = logging.getLogger(__name__)

STALE_MESSAGE = "文件在你上次读取后被修改过，请重新 read_file 后再改"
FOREIGN_MESSAGE = "文件在本任务开始之后被同一会话里的另一个任务改过，请重新 read_file 后再改"
CONCURRENT_MESSAGE = "文件在这次读取和写入之间被别处改过，没有写入；请重新 read_file 后再改"
UNAVAILABLE_MESSAGE = "连不上 supervisor，登记不了文件认领，这次没有写入"
STAMP_NOT_RECORDED_NOTE = "注意：改后的版本没能登记到 supervisor，再改这个文件之前先 read_file。"

JOURNAL_DIR = "data/edit_journal"
LOCK_SUBDIR = ".locks"
# 改文件的锁放在编辑日志目录里：编辑工具一律拒改那里，锁文件不会被一次全局替换整份换掉而失效
EDIT_LOCK_DIR = f"{JOURNAL_DIR}/{LOCK_SUBDIR}"
# 按路径哈希分到固定的一组锁文件上：同一个文件的改动排队，锁文件的个数不随文件数增长
_LOCK_STRIPES = 64
# 编辑日志目录里存着原文件的副本，同机的别的用户不能进
PRIVATE_DIR_MODE = 0o700

_MAX_TASK_ENTRIES = 4096
_MAX_TASK_LAYERS = 1024
_MAX_SESSION_ENTRIES = 16384
_SHA256 = re.compile(r"[0-9a-f]{64}")

_STATUS_LABELS = {
    "running": "运行中",
    "pending": "排队中",
    "suspended": "挂起等子任务",
    "completed": "正在派出子任务",
}


@dataclass(frozen=True)
class Stamp:
    mtime_ns: int
    size: int
    sha256: str


@dataclass(frozen=True)
class Entry:
    """对一个文件知道到哪一版。revealable：这一版的原文经读权限给模型看过，再回显它不算越权。"""

    stamp: Stamp
    revealable: bool


@dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    data: bytes = b""
    stamp: Stamp | None = None


@dataclass(frozen=True)
class Gate:
    """改之前和记下的戳比对的结论。vanished：读过之后文件没了；foreign：戳是本任务开始之后别的任务并进会话的。"""

    entry: Entry | None
    stale: bool = False
    vanished: bool = False
    foreign: bool = False

    @property
    def unread(self) -> bool:
        return self.entry is None

    @property
    def revealable(self) -> bool:
        return self.entry is not None and not self.stale and not self.vanished and self.entry.revealable

    @property
    def stale_message(self) -> str:
        return FOREIGN_MESSAGE if self.foreign else STALE_MESSAGE


class ConcurrentEdit(RuntimeError):
    """写盘前最后一次比对时，文件已经不是这次读到的那一版。"""


def session_key(ctx: Any) -> str:
    # 和 context_archive 的会话取法一致：入口分支归到父会话，accumulate 子节点归到 child session
    return str(getattr(ctx, "_context_archive_session_id", "") or getattr(ctx, "session_id", "") or "")


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


_path_key = path_key


def stamp_of(data: bytes, mtime_ns: int) -> Stamp:
    return Stamp(mtime_ns, len(data), hashlib.sha256(data).hexdigest())


def take_snapshot(path: Path) -> FileSnapshot:
    """整份读出文件和它的戳；不存在时 exists=False。是目录、没权限之类照常抛 OSError。"""
    try:
        with open(path, "rb") as stream:
            mtime_ns = os.fstat(stream.fileno()).st_mtime_ns
            data = stream.read()
    except FileNotFoundError:
        return FileSnapshot(False)
    return FileSnapshot(True, data, stamp_of(data, mtime_ns))


def gate_of(entry: Entry | None, snapshot: FileSnapshot, *, foreign: bool = False) -> Gate:
    """按内容哈希比：只动了 mtime（touch）不算改过。foreign 的戳不是本任务看到的，一律按过期算。"""
    if entry is None:
        return Gate(None)
    if not snapshot.exists or snapshot.stamp is None:
        return Gate(entry, vanished=True, foreign=foreign)
    return Gate(entry, stale=foreign or snapshot.stamp.sha256 != entry.stamp.sha256, foreign=foreign)


def stamp_to_wire(stamp: Stamp) -> dict[str, Any]:
    return {"mtime_ns": stamp.mtime_ns, "size": stamp.size, "sha256": stamp.sha256}


def _wire_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def stamp_from_wire(raw: Any) -> Stamp | None:
    if not isinstance(raw, dict):
        return None
    mtime_ns, size = _wire_int(raw.get("mtime_ns")), _wire_int(raw.get("size"))
    sha256 = str(raw.get("sha256") or "")
    if mtime_ns is None or size is None or size < 0 or not _SHA256.fullmatch(sha256):
        return None
    return Stamp(mtime_ns, size, sha256)


def entry_to_wire(entry: Entry | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {**stamp_to_wire(entry.stamp), "revealable": entry.revealable}


def entry_from_wire(raw: Any) -> Entry | None:
    stamp = stamp_from_wire(raw)
    if stamp is None:
        return None
    return Entry(stamp, bool(raw.get("revealable")))


@dataclass(frozen=True)
class StampRecord:
    """记账里的一条：seen_at 是看到这一版的时刻，merged_at 是并进会话层的时刻（任务层里恒为 0）。"""

    entry: Entry
    seen_at: float
    merged_at: float = 0.0


@dataclass(frozen=True)
class StampLookup:
    """layer：task 是本任务自己记的，session 是会话里已结束的任务留下的，空串是谁都没读过。"""

    entry: Entry | None = None
    layer: str = ""
    foreign: bool = False


class StampBook:
    """读戳的两层记账：任务层按任务号存，任务结束时并进会话层；同一会话里并行的任务互不刷新对方的戳。

    supervisor 持有一份。自带锁，可以在任意线程里调。没有任务号的记录直接进会话层。
    """

    def __init__(
        self,
        *,
        max_task_entries: int = _MAX_TASK_ENTRIES,
        max_task_layers: int = _MAX_TASK_LAYERS,
        max_session_entries: int = _MAX_SESSION_ENTRIES,
    ) -> None:
        self._tasks: OrderedDict[str, OrderedDict[tuple[str, str], StampRecord]] = OrderedDict()
        self._sessions: OrderedDict[tuple[str, str], StampRecord] = OrderedDict()
        self._max_task_entries = max(1, int(max_task_entries))
        self._max_task_layers = max(1, int(max_task_layers))
        self._max_session_entries = max(1, int(max_session_entries))
        self._lock = threading.Lock()

    def record(self, session: str, task_id: str, key: str, entry: Entry, *, now: float) -> str:
        """记下 session 里 task_id 看到的这一版，返回记进了哪一层。"""
        with self._lock:
            if not task_id:
                self._put_session_locked((session, key), StampRecord(entry, now, now))
                return "session"
            layer = self._tasks.get(task_id)
            if layer is None:
                layer = self._tasks[task_id] = OrderedDict()
                while len(self._tasks) > self._max_task_layers:
                    self._tasks.popitem(last=False)
            self._tasks.move_to_end(task_id)
            layer[(session, key)] = StampRecord(entry, now)
            layer.move_to_end((session, key))
            while len(layer) > self._max_task_entries:
                layer.popitem(last=False)
            return "task"

    def lookup(self, session: str, task_id: str, key: str, *, started_at: float | None) -> StampLookup:
        """先看本任务自己的，再看会话层；会话层那条是 started_at 之后才并进来的，就标成 foreign。"""
        with self._lock:
            layer = self._tasks.get(task_id) if task_id else None
            own = layer.get((session, key)) if layer is not None else None
            if own is not None:
                return StampLookup(own.entry, "task")
            shared = self._sessions.get((session, key))
            if shared is None:
                return StampLookup()
            self._sessions.move_to_end((session, key))
            foreign = bool(task_id) and started_at is not None and shared.merged_at > started_at
            return StampLookup(shared.entry, "session", foreign)

    def merge_task(self, task_id: str, *, at: float) -> int:
        """把任务层并进会话层，返回并了几条。会话层里看到得更晚的那一版留着，不让旧的读数盖掉新的。"""
        with self._lock:
            layer = self._tasks.pop(task_id, None)
            if not layer:
                return 0
            for key, record in layer.items():
                current = self._sessions.get(key)
                if current is not None and current.seen_at > record.seen_at:
                    continue
                # 任务被回收后它的 worker 还可能记进读戳：并入时刻不早于记下的时刻，期间开始的任务才会把它算作 foreign
                self._put_session_locked(key, replace(record, merged_at=max(at, record.seen_at)))
            return len(layer)

    def task_ids(self) -> list[str]:
        with self._lock:
            return list(self._tasks)

    def task_entries(self, task_id: str) -> int:
        with self._lock:
            return len(self._tasks.get(task_id) or ())

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _put_session_locked(self, key: tuple[str, str], record: StampRecord) -> None:
        self._sessions[key] = record
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_session_entries:
            self._sessions.popitem(last=False)


class EditBlocked(Exception):
    """这次改不了：别的任务正在改、本任务在 supervisor 那边已经结束，或者连不上 supervisor。data 并进工具结果的 data。"""

    def __init__(self, message: str, **data: Any) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    @property
    def unavailable(self) -> bool:
        return bool(self.data.get("unavailable"))


@dataclass(frozen=True)
class EditGrant:
    """supervisor 准了这次改：本任务认领了文件，附上比对用的戳和编辑日志该记在哪个会话。new_claim：这次才认领的。"""

    path_key: str
    entry: Entry | None = None
    foreign: bool = False
    journal_session: str = ""
    new_claim: bool = False

    def gate(self, snapshot: FileSnapshot) -> Gate:
        return gate_of(self.entry, snapshot, foreign=self.foreign)


def _holder_text(holder: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    task_id = str(holder.get("task_id") or "?")
    node_id = str(holder.get("node_id") or "?")
    status = str(holder.get("status") or "")
    since = str(holder.get("since") or "")
    who = f"任务 {task_id}（节点 {node_id}，{_STATUS_LABELS.get(status, status or '状态不详')}）"
    if since:
        who += f"从 {since} 起"
    data = {"taskId": task_id, "nodeId": node_id, "status": status, "since": since}
    session = str(holder.get("session_id") or "")
    if session:
        data["sessionId"] = session
    return who, data


def _refusal(reply: Any, display: str) -> EditBlocked:
    answer = reply if isinstance(reply, dict) else {}
    reason = str(answer.get("reason") or "")
    if reason == "claimed":
        holder = answer.get("holder") if isinstance(answer.get("holder"), dict) else {}
        who, data = _holder_text(holder)
        message = (
            f"{display} 正在被另一个任务修改，这次没有写入：{who}在改它。"
            "可以先改别的文件，或者等那个任务结束后重新 read_file 再改"
        )
        return EditBlocked(message, claimed=True, claimedBy=data)
    if reason == "task_ended":
        status = str(answer.get("status") or "")
        return EditBlocked(f"supervisor 那边本任务已经结束（{status or '终态'}），这次没有写入", taskEnded=True)
    if reason == "task_unknown":
        return EditBlocked("supervisor 里找不到本任务（可能刚重启过），这次没有写入", taskEnded=True)
    return EditBlocked(f"supervisor 没有准许这次改动（{reason or '没有说明原因'}），这次没有写入")


async def begin_edit(ctx: Any, path: Path, display: str) -> EditGrant:
    """改文件之前向 supervisor 认领它，顺带取回比对用的戳。改不了时抛 EditBlocked。"""
    key = path_key(path)
    begin = getattr(ctx, "begin_file_edit", None)
    if not callable(begin):
        raise EditBlocked(f"{UNAVAILABLE_MESSAGE}：当前上下文没有 supervisor 接口", unavailable=True)
    try:
        reply = await begin(key, display=display, session=session_key(ctx))
    except Exception as exc:
        logger.warning("claiming %s for an edit failed: %s", path, exc)
        raise EditBlocked(f"{UNAVAILABLE_MESSAGE}（{exc}）；稍后再试", unavailable=True) from None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise _refusal(reply, display)
    return EditGrant(
        key,
        entry_from_wire(reply.get("stamp")),
        foreign=bool(reply.get("foreign")),
        journal_session=str(reply.get("journal_session") or ""),
        new_claim=bool(reply.get("new_claim")),
    )


async def _record(ctx: Any, key: str, stamp: Stamp, *, revealable: bool) -> bool:
    record = getattr(ctx, "record_file_stamp", None)
    if not callable(record):
        return False
    try:
        reply = await record(key, session=session_key(ctx), stamp=stamp_to_wire(stamp), revealable=revealable)
    except Exception as exc:
        logger.warning("recording the stamp of %s failed: %s", key, exc)
        return False
    return isinstance(reply, dict) and reply.get("ok") is True


async def record_stamp(ctx: Any, path: Path, stamp: Stamp, *, revealable: bool) -> bool:
    """read_file 读到的版本记到本任务名下。记不上只返回 False：读照常返回，之后改这个文件时会被要求重读。"""
    return await _record(ctx, path_key(path), stamp, revealable=revealable)


async def finish_edit(ctx: Any, grant: EditGrant, stamp: Stamp | None, *, revealable: bool) -> bool:
    """改动落盘之后调：认领留着到任务结束，写成的新版本记到本任务名下（删文件时 stamp 为 None）。记不上返回 False。"""
    if stamp is None:
        return True
    return await _record(ctx, grant.path_key, stamp, revealable=revealable)


async def abandon_edit(ctx: Any, grant: EditGrant) -> None:
    """没有落盘：这次才拿的认领还回去，免得白白挡住别的任务；还不回去就等任务结束时一并放掉。"""
    if not grant.new_claim:
        return
    release = getattr(ctx, "release_file_claim", None)
    if not callable(release):
        return
    try:
        await release(grant.path_key)
    except Exception as exc:
        logger.warning("releasing the claim on %s failed: %s", grant.path_key, exc)


async def journal_session(ctx: Any) -> str:
    """编辑日志记在哪个会话：supervisor 给的发起链根会话，派出去的子任务的改动也记在那里。没有任务号时就是本会话。"""
    own = session_key(ctx)
    if not str(getattr(ctx, "task_id", "") or "").strip():
        return own
    scope = getattr(ctx, "file_edit_scope", None)
    if not callable(scope):
        raise EditBlocked("连不上 supervisor，查不到编辑日志记在哪个会话：当前上下文没有 supervisor 接口", unavailable=True)
    try:
        reply = await scope()
    except Exception as exc:
        logger.warning("asking for the edit journal session failed: %s", exc)
        raise EditBlocked(f"连不上 supervisor，查不到编辑日志记在哪个会话（{exc}）", unavailable=True) from None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return own
    return str(reply.get("journal_session") or "") or own


def edit_lock_path(path: Path, workspace_root: Path | str) -> Path:
    # edit- 前缀和编辑日志按会话目录取的锁文件分开
    stripe = zlib.crc32(path_key(path).encode("utf-8")) % _LOCK_STRIPES
    return Path(workspace_root) / EDIT_LOCK_DIR / f"edit-{stripe:02x}.lock"


@contextlib.contextmanager
def edit_lock(path: Path, workspace_root: Path | str) -> Iterator[None]:
    """改同一个文件的操作排队：本进程的线程之间、各个 engine worker 之间都排。

    只在 asyncio.to_thread 的线程里拿，持锁期间不能 await；同一个线程里不能套着拿第二把。
    """
    # exclusive_lock 按默认权限建缺的目录，编辑日志目录得抢在它前面按私有权限建好
    (Path(workspace_root) / JOURNAL_DIR).mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    with exclusive_lock(edit_lock_path(path, workspace_root)):
        yield


@contextlib.contextmanager
def edit_locks(paths: Iterable[Path], workspace_root: Path | str) -> Iterator[None]:
    """一次改好几个文件时拿齐它们的锁：同一把条带锁只拿一次，按锁文件排序拿，两个多文件操作不会互相等。

    和 edit_lock 一样只在线程里拿，持锁期间不能 await，也不能再套着拿 edit_lock。
    """
    (Path(workspace_root) / JOURNAL_DIR).mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    lock_paths = sorted({edit_lock_path(Path(path), workspace_root) for path in paths}, key=lambda item: item.name)
    with contextlib.ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(exclusive_lock(lock_path))
        yield


def commit_locked(
    path: Path, data: bytes, *, expected: FileSnapshot, new_file_mode: int | None = None,
) -> Stamp:
    """确认文件仍是 expected 那一版，再原子写入 data，返回新戳。调用方须持有 edit_lock(path, 工作区)。

    engine worker 之间由 edit_lock 挡住；这里再比一次，挡的是不走这把锁的写入方（用户的编辑器、execute_command 跑的脚本）。
    new_file_mode 只在新建文件时用：节点等私有文件落盘就是 0600。
    """
    current = take_snapshot(path)
    changed = current.exists != expected.exists or (
        current.stamp is not None and expected.stamp is not None
        and current.stamp.sha256 != expected.stamp.sha256
    )
    if changed:
        raise ConcurrentEdit(CONCURRENT_MESSAGE)
    replace_bytes(path, data, new_file_mode=new_file_mode)
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        mtime_ns = 0
    return stamp_of(data, mtime_ns)


async def reveal_allowed(ctx: Any, gate: Gate, path: str) -> bool:
    """结果里能不能带文件原文（候选片段、改动处的上下文），否则只给行号。

    本会话经读权限看过这一版就放行；没看过就按 read_file 策略问一次，auto 才放行，approval_required 和 deny 都不放。
    写权限不等于读权限：config/nodes/** 能批准改、却禁止读。只问结论不送审：为几行回显弹一张读审批卡，工具会一直挂到有人处理。
    """
    if gate.revealable:
        return True
    evaluate = getattr(ctx, "evaluate_op", None)
    if not callable(evaluate):
        return False
    try:
        verdict = await evaluate("read_file", {"path": path})
    except Exception as exc:
        logger.warning("read policy check for echoing %s failed; showing line numbers only: %s", path, exc)
        return False
    return isinstance(verdict, dict) and str(verdict.get("safety_level") or "") == "auto"
