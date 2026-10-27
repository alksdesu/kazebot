"""supervisor 持有的常驻进程：按会话家族的根会话归属，不跟任务走，任务挂起恢复、engine 重启都碰不到它们。

停的时机：stop_process、会话重置或会话已不在、太久没人读输出、到了运行时长上限、supervisor 退出。
每个进程一条记录 data/processes/<会话>/<id>.json，进程活着时它就是 PID 登记，supervisor 下次启动按它清点残留。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config
from engine.project_root import PROJECT_ROOT_KEY, task_project_root
from engine.tool_outputs import MAX_PATTERN_CHARS, session_dir_name, tool_output_limits
from toolbox._common import safe_subprocess_env

from . import osproc
from .logs import (
    DEFAULT_SINCE_LINES,
    DEFAULT_TAIL_LINES,
    OutputPage,
    ProcessLog,
    SearchSlice,
    display_line,
    page_since,
    page_tail,
    render_search,
    search_slice,
)

log = logging.getLogger(__name__)

STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
EXITED = "exited"
STOPPED = "stopped"
FAILED = "failed"
ACTIVE = frozenset({STARTING, RUNNING, STOPPING})
ENDED = frozenset({EXITED, STOPPED, FAILED})

REASON_STOP = "stop"
REASON_SESSION_RESET = "session_reset"
REASON_SESSION_CANCELLED = "session_cancelled"
REASON_SESSION_GONE = "session_gone"
REASON_IDLE = "idle"
REASON_MAX_RUNTIME = "max_runtime"
REASON_SUPERVISOR_EXIT = "supervisor_exit"
REASON_SUPERVISOR_RESTART = "supervisor_restart"
REASON_ROOT_CLEANUP = "root_cleanup"

REASON_TEXT = {
    REASON_STOP: "被 stop_process 停掉",
    REASON_SESSION_RESET: "会话重置，跟着停掉",
    REASON_SESSION_CANCELLED: "会话被停止，跟着停掉",
    REASON_SESSION_GONE: "所属会话已经不在，跟着停掉",
    REASON_IDLE: "太久没人读输出，按空闲停掉",
    REASON_MAX_RUNTIME: "到了运行时长上限，停掉",
    REASON_SUPERVISOR_EXIT: "supervisor 退出时停掉",
    REASON_SUPERVISOR_RESTART: "supervisor 重启，上次留下的进程已停掉",
    REASON_ROOT_CLEANUP: "所在目录要清理，停掉",
}

_META_VERSION = 1
_SWEEP_SEC = 1.0
_READ_CHUNK_BYTES = 64 * 1024
_READER_JOIN_SEC = 5.0
_THREAD_JOIN_SEC = 5.0
# 停一批进程时每个进程一个线程并行等，停完的总时长按宽限期再加这么多封顶
_STOP_MARGIN_SEC = 20.0
# 就绪判定只看每行开头这么多字符：正则写得不好时别拖住读输出的线程
_READY_SCAN_CHARS = 4096
_NAME_MAX_CHARS = 64
_LIST_COMMAND_CHARS = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


class ProcessError(Exception):
    """接口要回给调用方的错误：status 是 HTTP 状态码，message 原样给模型看。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Limits:
    max_per_session: int
    max_total: int
    idle_timeout_sec: float
    default_max_runtime_sec: float
    max_runtime_cap_sec: float
    log_max_bytes: int
    log_keep_files: int
    exited_retention_sec: float
    stop_grace_sec: float
    view_chars: int


def limits_from(cfg: dict[str, Any]) -> Limits:
    cap = get_float(cfg, "processes.max_runtime_cap_sec", 86400.0, min_value=60.0, max_value=30 * 86400.0)
    return Limits(
        max_per_session=get_int(cfg, "processes.max_per_session", 4, min_value=1, max_value=64),
        max_total=get_int(cfg, "processes.max_total", 16, min_value=1, max_value=256),
        idle_timeout_sec=get_float(cfg, "processes.idle_timeout_minutes", 60.0, min_value=0.0, max_value=7 * 24 * 60.0) * 60,
        default_max_runtime_sec=min(
            cap, get_float(cfg, "processes.default_max_runtime_sec", 14400.0, min_value=60.0, max_value=30 * 86400.0),
        ),
        max_runtime_cap_sec=cap,
        log_max_bytes=get_int(cfg, "processes.log_max_bytes", 4 * 1024 * 1024, min_value=64 * 1024, max_value=256 * 1024 * 1024),
        log_keep_files=get_int(cfg, "processes.log_keep_files", 2, min_value=0, max_value=20),
        exited_retention_sec=get_float(
            cfg, "processes.exited_retention_minutes", 60.0, min_value=0.0, max_value=7 * 24 * 60.0,
        ) * 60,
        stop_grace_sec=get_float(cfg, "processes.stop_grace_sec", 5.0, min_value=0.0, max_value=60.0),
        view_chars=tool_output_limits(cfg).view_chars,
    )


def processes_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "data" / "processes"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _clean_name(name: Any, command: str) -> str:
    text = _CONTROL_CHARS.sub(" ", str(name or "")).strip()
    if not text:
        text = _CONTROL_CHARS.sub(" ", command).strip()
    return text[:_NAME_MAX_CHARS]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _ready_hit(regex: re.Pattern[str], line: str) -> bool:
    return regex.search(display_line(line)[:_READY_SCAN_CHARS]) is not None


@dataclass(frozen=True)
class _Scope:
    """调用方任务的身份：owner 是它会话家族的根会话，origin 是它自己面向用户的会话。"""

    task_id: str
    owner: str
    origin: str
    node_id: str
    project_root: Any
    live: bool
    owner_alive: bool


@dataclass(eq=False)
class _Proc:
    id: str
    name: str
    command: str
    cwd: str
    session_id: str
    origin_session_id: str
    task_id: str
    node_id: str
    tag: str
    created_at: float
    max_runtime_sec: float
    ready_pattern: str
    log: ProcessLog
    regex: re.Pattern[str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    popen: subprocess.Popen | None = None
    pid: int = 0
    started: str = ""
    status: str = STARTING
    returncode: int | None = None
    ended_at: float | None = None
    stop_reason: str = ""
    stop_requested: str = ""
    ready_at: float | None = None
    ready_line: int | None = None
    eof: bool = False
    log_error: str = ""
    started_mono: float = field(default_factory=time.monotonic)
    last_read: float = field(default_factory=time.monotonic)
    reader: threading.Thread | None = None

    @property
    def meta_path(self) -> Path:
        return self.log.directory / f"{self.id}.json"


class ProcessService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.root = Path(state.workspace_root)
        self.base_dir = processes_dir(self.root)
        self.instance = osproc.instance_key(self.root)
        self._lock = threading.Lock()
        self._procs: dict[str, _Proc] = {}
        # 会话重置时记下的停止请求（进程 id → 原因），由巡检或起它的调用落到进程上
        self._stop_marks: dict[str, str] = {}
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovered = False
        self._closing = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            recover = not self._recovered
            self._recovered = True
            self._closing = False
        if recover:
            try:
                self.recover()
            except Exception:
                log.exception("cleaning up resident processes left by the last run failed")
        self._halt.clear()
        thread = threading.Thread(target=self._loop, name="resident-processes", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def close(self) -> None:
        self._halt.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
            self._thread = None
            self._closing = True
            procs = list(self._procs.values())
        if thread is not None:
            thread.join(timeout=_THREAD_JOIN_SEC)
        limits = self._limits()
        self._stop_all(self._claim_for_stop(procs, REASON_SUPERVISOR_EXIT), limits.stop_grace_sec)
        # 巡检线程手里可能还有正在停的：等它们停完再让 supervisor 退出
        self._await_ended(procs, limits.stop_grace_sec + _STOP_MARGIN_SEC)

    stop = close

    def _limits(self) -> Limits:
        return limits_from(load_runtime_config(self.root))

    def _loop(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(_SWEEP_SEC)
            self._wake.clear()
            if self._halt.is_set():
                break
            try:
                self.sweep()
            except Exception:
                log.exception("resident process sweep failed")

    def start_process(
        self,
        *,
        task_id: str,
        command: str,
        name: str = "",
        ready_pattern: str = "",
        max_runtime_sec: Any = None,
    ) -> dict[str, Any]:
        command = str(command or "")
        if not command.strip():
            raise ProcessError(422, "command 不能为空")
        pattern = str(ready_pattern or "")
        regex: re.Pattern[str] | None = None
        if pattern:
            if len(pattern) > MAX_PATTERN_CHARS:
                raise ProcessError(422, f"ready_pattern 太长：最多 {MAX_PATTERN_CHARS} 个字符")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise ProcessError(422, f"ready_pattern 不是合法的正则：{exc}") from None
        scope = self._scope(task_id)
        if not scope.live:
            raise ProcessError(409, "发起的任务已经结束，不能再起常驻进程")
        if not scope.owner_alive:
            raise ProcessError(409, "这个任务所在的会话不能持有常驻进程")
        limits = self._limits()
        runtime = self._runtime(max_runtime_sec, limits)
        cwd = task_project_root(
            {PROJECT_ROOT_KEY: scope.project_root} if scope.project_root is not None else {},
            self.root, task_id=scope.task_id,
        )
        process_id = f"p_{uuid.uuid4().hex[:10]}"
        proc = _Proc(
            id=process_id,
            name=_clean_name(name, command),
            command=command,
            cwd=str(cwd),
            session_id=scope.owner,
            origin_session_id=scope.origin,
            task_id=scope.task_id,
            node_id=scope.node_id,
            tag=osproc.process_tag(self.instance, process_id),
            created_at=time.time(),
            max_runtime_sec=runtime,
            ready_pattern=pattern,
            log=ProcessLog(
                self.base_dir / session_dir_name(scope.owner), process_id,
                max_bytes=limits.log_max_bytes, keep_files=limits.log_keep_files,
            ),
            regex=regex,
        )
        with self._lock:
            if self._closing:
                raise ProcessError(503, "supervisor 正在退出，不再起新进程")
            active = [item for item in self._procs.values() if item.status in ACTIVE]
            mine = [item for item in active if item.session_id == scope.owner]
            if len(mine) >= limits.max_per_session:
                raise ProcessError(
                    409,
                    f"这个会话已经有 {len(mine)} 个常驻进程（上限 {limits.max_per_session}）：{self._brief(mine)}。"
                    "先用 stop_process 停掉不用的",
                )
            if len(active) >= limits.max_total:
                raise ProcessError(409, f"常驻进程总数已到上限 {limits.max_total}，先停掉不用的")
            self._procs[process_id] = proc
        try:
            self._spawn(proc)
        except Exception as exc:
            with self._lock:
                self._procs.pop(process_id, None)
                self._stop_marks.pop(process_id, None)
            with proc.lock:
                proc.status = FAILED
                proc.log.close()
            self._discard_files(proc)
            if isinstance(exc, (OSError, ValueError)):
                raise ProcessError(422, f"进程起不来：{exc}") from None
            raise
        with self._lock:
            mark = self._stop_marks.pop(process_id, "")
        # 起的这一会儿里会话被重置、或要清理它所在的目录：起来了就当场停掉
        with proc.lock:
            if mark and not proc.stop_requested:
                proc.stop_requested = mark
            claim = bool(proc.stop_requested) and proc.status == RUNNING
            if claim:
                proc.status = STOPPING
                proc.stop_reason = proc.stop_requested
        if claim:
            self._stop_one(proc, limits.stop_grace_sec)
        return {"process": self._view(proc)}

    def status(self, *, task_id: str, process_id: str) -> dict[str, Any]:
        proc = self._get(self._scope_or_admin(task_id), process_id)
        with proc.lock:
            proc.last_read = time.monotonic()
            return {"process": self._view_locked(proc)}

    def list_processes(self, *, task_id: str = "") -> dict[str, Any]:
        scope = self._scope_or_admin(task_id)
        with self._lock:
            procs = list(self._procs.values())
        views = []
        for proc in sorted(procs, key=lambda item: item.created_at):
            if scope is not None and proc.session_id != scope.owner:
                continue
            with proc.lock:
                views.append(self._view_locked(proc))
        return {"processes": views}

    def read_output(
        self,
        *,
        task_id: str,
        process_id: str,
        tail: int | None = None,
        since: int | None = None,
        search: str = "",
        max_lines: int | None = None,
    ) -> dict[str, Any]:
        proc = self._get(self._scope_or_admin(task_id), process_id)
        budget = self._limits().view_chars
        found: SearchSlice | None = None
        page: OutputPage | None = None
        with proc.lock:
            proc.last_read = time.monotonic()
            try:
                if search:
                    found = search_slice(proc.log, search, since or 0)
                elif since is not None:
                    page = page_since(proc.log, since, max_lines or DEFAULT_SINCE_LINES, budget)
                else:
                    page = page_tail(proc.log, tail or DEFAULT_TAIL_LINES, budget)
            except ValueError as exc:
                raise ProcessError(422, str(exc)) from None
            view = self._view_locked(proc)
        if found is not None:
            # 模型写的正则可能很慢：放在锁外匹配，挡不住这个进程的读输出线程，也挡不住巡检线程
            page = render_search(found, budget)
        return {
            "process": view,
            "text": page.text,
            "first_line": page.first_line,
            "last_line": page.last_line,
            "start_line": page.start_line,
            "end_line": page.end_line,
            "next_since": page.next_since,
            "more": page.more,
        }

    def stop_process(self, *, task_id: str, process_id: str) -> dict[str, Any]:
        proc = self._get(self._scope_or_admin(task_id), process_id)
        claimed = self._claim_for_stop([proc], REASON_STOP)
        if claimed:
            self._stop_one(proc, self._limits().stop_grace_sec)
        return {"process": self._view(proc), "stopped": bool(claimed)}

    def stop_for_session(self, session_id: str, *, reason: str = REASON_SESSION_RESET) -> int:
        return self.stop_for_sessions({session_id}, reason=reason)

    def stop_for_cancelled_sessions(self, session_ids: Iterable[str]) -> int:
        """整段对话被停止（cancel_session）时调，和重置一样只记下、不等。"""
        return self.stop_for_sessions(session_ids, reason=REASON_SESSION_CANCELLED)

    def stop_for_sessions(self, session_ids: Iterable[str], *, reason: str = REASON_SESSION_RESET) -> int:
        """会话重置或被停止时调：只记下要停哪些、叫醒巡检线程，返回新记下的个数。

        调用方正攥着 supervisor 的锁，这里不等进程退出，也不拿进程锁：读输出的一方可能正拿着它读盘。
        """
        ids = {str(sid or "").strip() for sid in session_ids} - {""}
        if not ids:
            return 0
        marked = 0
        with self._lock:
            for proc in self._procs.values():
                if proc.session_id not in ids and proc.origin_session_id not in ids:
                    continue
                if proc.status in ENDED or proc.id in self._stop_marks:
                    continue
                self._stop_marks[proc.id] = reason
                marked += 1
        if marked:
            self._wake.set()
        return marked

    def _apply_stop_marks(self) -> None:
        """把记下的停止请求落到进程上；正在起的那几个由起它的调用当场停掉。"""
        with self._lock:
            marks, self._stop_marks = self._stop_marks, {}
            pending = [(self._procs.get(process_id), reason) for process_id, reason in marks.items()]
        for proc, reason in pending:
            if proc is None:
                continue
            with proc.lock:
                if proc.status in (STARTING, RUNNING) and not proc.stop_requested:
                    proc.stop_requested = reason

    def stop_under_root(self, root: Path | str, *, reason: str = REASON_ROOT_CLEANUP, wait_sec: float = 10.0) -> int:
        """停掉工作目录在 root 之下的全部进程，等它们停完再返回（清理目录之前调）。会阻塞，不要在 supervisor 的锁里调。"""
        try:
            target = Path(root).resolve()
        except (OSError, RuntimeError):
            return 0
        with self._lock:
            procs = [proc for proc in self._procs.values() if _is_under(Path(proc.cwd), target)]
        active = [proc for proc in procs if self._is_active(proc)]
        self._stop_all(self._claim_for_stop(active, reason))
        # 正在起的那几个由起它的调用当场停掉，正在停的由别的线程停完：都等到结束
        self._await_ended(active, wait_sec)
        return len(active)

    @staticmethod
    def _is_active(proc: _Proc) -> bool:
        with proc.lock:
            return proc.status in ACTIVE

    def _await_ended(self, procs: Iterable[_Proc], timeout_sec: float) -> None:
        waiting = list(procs)
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            with self._lock:
                registered = set(self._procs.values())
            waiting = [proc for proc in waiting if proc in registered and self._is_active(proc)]
            if not waiting or time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def sweep(self) -> None:
        self._apply_stop_marks()
        limits = self._limits()
        live_sessions = self._live_sessions()
        now = time.monotonic()
        wall = time.time()
        with self._lock:
            procs = list(self._procs.values())
        to_stop: list[_Proc] = []
        to_forget: list[_Proc] = []
        for proc in procs:
            with proc.lock:
                if proc.status == RUNNING:
                    reason = self._stop_reason_locked(proc, live_sessions, limits, now)
                    if reason:
                        proc.status = STOPPING
                        proc.stop_reason = reason
                        to_stop.append(proc)
                    elif proc.eof and proc.popen is not None and proc.popen.poll() is not None:
                        self._finish_locked(proc, EXITED, "", proc.popen.returncode)
                elif proc.status in ENDED:
                    ended = proc.ended_at if proc.ended_at is not None else wall
                    if wall - ended >= limits.exited_retention_sec:
                        to_forget.append(proc)
        self._stop_all(to_stop, limits.stop_grace_sec)
        for proc in to_forget:
            self._forget(proc)

    def _stop_reason_locked(self, proc: _Proc, live_sessions: set[str] | None, limits: Limits, now: float) -> str:
        if proc.stop_requested:
            return proc.stop_requested
        if live_sessions is not None and proc.session_id not in live_sessions:
            return REASON_SESSION_GONE
        if now - proc.started_mono >= proc.max_runtime_sec:
            return REASON_MAX_RUNTIME
        if limits.idle_timeout_sec > 0 and now - proc.last_read >= limits.idle_timeout_sec:
            return REASON_IDLE
        return ""

    def _live_sessions(self) -> set[str] | None:
        lock = getattr(self.state, "_lock", None)
        sessions = getattr(self.state, "sessions", None)
        if lock is None or sessions is None:
            return None
        with lock:
            return set(sessions)

    def recover(self) -> int:
        """清点上次运行留下的记录：还登记为在跑的核对身份后停掉，保留期内的留着给人读，过期的连日志删掉。返回停掉的个数。"""
        limits = self._limits()
        wall = time.time()
        records = self._scan_records()
        leftovers = [(path, data) for path, data in records if str(data.get("status") or "") in ACTIVE]
        reaped = 0
        threads: list[threading.Thread] = []
        results: list[bool] = []

        def reap_one(data: dict[str, Any]) -> None:
            pid = int(data.get("pid") or 0)
            tag = str(data.get("tag") or "")
            if pid <= 0 or not tag:
                return
            try:
                results.append(osproc.reap(pid, str(data.get("fingerprint") or ""), tag, grace_sec=limits.stop_grace_sec))
            except Exception:
                log.exception("stopping leftover process %s (pid %s) failed", data.get("id"), pid)

        for _path, data in leftovers:
            thread = threading.Thread(target=reap_one, args=(data,), name="process-reaper", daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=limits.stop_grace_sec + _STOP_MARGIN_SEC)
        reaped = sum(1 for found in results if found)
        try:
            stray = osproc.reap_instance(self.instance, grace_sec=limits.stop_grace_sec)
        except Exception:
            log.exception("stopping untracked leftover processes failed")
            stray = []
        if reaped or stray:
            log.warning(
                "stopped %d resident process(es) left by the last run (%d found only by their environment tag)",
                reaped + len(stray), len(stray),
            )
        for path, data in leftovers:
            data.update(status=STOPPED, stop_reason=REASON_SUPERVISOR_RESTART, ended_at=wall)
            self._write_json(path, data)
        for path, data in records:
            ended = data.get("ended_at")
            ended_at = float(ended) if isinstance(ended, (int, float)) else wall
            if wall - ended_at >= limits.exited_retention_sec:
                self._discard_record(path, data)
                continue
            proc = self._from_record(path, data, limits)
            if proc is not None:
                with self._lock:
                    self._procs.setdefault(proc.id, proc)
        self._remove_stray_logs(limits, wall)
        return reaped + len(stray)

    def _scan_records(self) -> list[tuple[Path, dict[str, Any]]]:
        found: list[tuple[Path, dict[str, Any]]] = []
        if not self.base_dir.is_dir():
            return found
        for path in sorted(self.base_dir.glob("*/*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("unreadable resident process record %s, removed", path)
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if isinstance(data, dict) and str(data.get("id") or "") == path.stem:
                found.append((path, data))
        return found

    def _from_record(self, path: Path, data: dict[str, Any], limits: Limits) -> _Proc | None:
        record = data.get("log") if isinstance(data.get("log"), dict) else {}
        try:
            first_lines = [int(item) for item in record.get("first_lines") or [1]]
            created_at = float(data.get("created_at") or 0.0)
            ended = data.get("ended_at")
            ended_at = float(ended) if isinstance(ended, (int, float)) else None
            returncode = data.get("returncode")
            proc = _Proc(
                id=path.stem,
                name=str(data.get("name") or ""),
                command=str(data.get("command") or ""),
                cwd=str(data.get("cwd") or ""),
                session_id=str(data.get("session_id") or ""),
                origin_session_id=str(data.get("origin_session_id") or ""),
                task_id=str(data.get("task_id") or ""),
                node_id=str(data.get("node_id") or ""),
                tag=str(data.get("tag") or ""),
                created_at=created_at,
                max_runtime_sec=float(data.get("max_runtime_sec") or limits.default_max_runtime_sec),
                ready_pattern=str(data.get("ready_pattern") or ""),
                log=ProcessLog(
                    path.parent, path.stem, max_bytes=limits.log_max_bytes, keep_files=limits.log_keep_files,
                    first_lines=first_lines, lines_done=int(record.get("lines_done") or 0),
                    partial=bool(record.get("partial")),
                ),
                pid=int(data.get("pid") or 0),
                started=str(data.get("fingerprint") or ""),
                status=str(data.get("status") or STOPPED),
                returncode=int(returncode) if isinstance(returncode, int) else None,
                ended_at=ended_at,
                stop_reason=str(data.get("stop_reason") or ""),
                ready_at=data.get("ready_at") if isinstance(data.get("ready_at"), (int, float)) else None,
                ready_line=data.get("ready_line") if isinstance(data.get("ready_line"), int) else None,
                eof=True,
            )
        except (TypeError, ValueError):
            log.warning("malformed resident process record %s, removed", path)
            self._discard_record(path, data)
            return None
        if proc.status not in ENDED:
            proc.status = STOPPED
        return proc

    def _remove_stray_logs(self, limits: Limits, wall: float) -> None:
        """没有记录对着的日志（记录写下之前就崩了）：过了保留期就删。"""
        if not self.base_dir.is_dir():
            return
        with self._lock:
            known = {(proc.log.directory, proc.id) for proc in self._procs.values()}
        for directory in self.base_dir.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.glob("*.log*"):
                process_id = path.name.split(".log", 1)[0]
                if (directory, process_id) in known or (directory / f"{process_id}.json").exists():
                    continue
                self._unlink_if_older(path, wall - limits.exited_retention_sec)
            # 写记录写到一半崩掉留下的临时文件
            for path in directory.glob(".*.tmp"):
                self._unlink_if_older(path, wall - limits.exited_retention_sec)
            with contextlib.suppress(OSError):
                directory.rmdir()

    @staticmethod
    def _unlink_if_older(path: Path, cutoff: float) -> None:
        with contextlib.suppress(OSError):
            if path.stat().st_mtime <= cutoff:
                path.unlink()

    def _scope(self, task_id: str) -> _Scope:
        tid = str(task_id or "").strip()
        if not tid:
            raise ProcessError(422, "缺少 task_id")
        state = self.state
        with state._lock:
            task = state.tasks.get(tid)
            if task is None:
                raise ProcessError(404, "任务不存在或已被清理")
            origin = str(state._route_session_id_for_task_locked(task) or task.session_id or "")
            owner = str(state._task_family_root_locked(task) or origin)
            task_input = task.input if isinstance(task.input, dict) else {}
            return _Scope(
                task_id=tid,
                owner=owner,
                origin=origin,
                node_id=str(task.node_id or ""),
                project_root=task_input.get(PROJECT_ROOT_KEY),
                live=not state._task_terminal(task),
                owner_alive=owner in state.sessions,
            )

    def _scope_or_admin(self, task_id: str) -> _Scope | None:
        """带了任务就只认它会话家族里的进程；不带的是控制台，看得到全部。"""
        return self._scope(task_id) if str(task_id or "").strip() else None

    def _get(self, scope: _Scope | None, process_id: str) -> _Proc:
        pid = str(process_id or "").strip()
        with self._lock:
            proc = self._procs.get(pid)
        if proc is None or (scope is not None and proc.session_id != scope.owner):
            raise ProcessError(404, f"没有这个进程：{pid}（list_processes 列得出当前会话的全部进程）")
        return proc

    @staticmethod
    def _runtime(raw: Any, limits: Limits) -> float:
        if raw is None or isinstance(raw, bool):
            return limits.default_max_runtime_sec
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return limits.default_max_runtime_sec
        if value != value or value <= 0:
            return limits.default_max_runtime_sec
        return min(max(value, 1.0), limits.max_runtime_cap_sec)

    @staticmethod
    def _brief(procs: list[_Proc]) -> str:
        return "、".join(f"{proc.id}（{proc.name}）" for proc in procs)

    def _spawn(self, proc: _Proc) -> None:
        with proc.lock:
            proc.log.open()
        popen = osproc.spawn(proc.command, cwd=Path(proc.cwd), env=safe_subprocess_env(), tag=proc.tag)
        started = osproc.fingerprint(popen.pid, include_exited=True) or ""
        reader = threading.Thread(target=self._pump, args=(proc,), name=f"process-{proc.id}", daemon=True)
        with proc.lock:
            proc.popen = popen
            proc.pid = popen.pid
            proc.started = started
            proc.started_mono = time.monotonic()
            proc.last_read = proc.started_mono
            proc.status = RUNNING
            proc.reader = reader
            self._write_meta_locked(proc)
        reader.start()

    def _pump(self, proc: _Proc) -> None:
        stream = proc.popen.stdout if proc.popen is not None else None
        try:
            while stream is not None:
                try:
                    chunk = stream.read1(_READ_CHUNK_BYTES)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                with proc.lock:
                    self._record_output_locked(proc, chunk)
        finally:
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
            with proc.lock:
                proc.eof = True
                if proc.status in ACTIVE:
                    self._write_meta_locked(proc)
            self._wake.set()

    def _record_output_locked(self, proc: _Proc, chunk: bytes) -> None:
        if proc.log_error:
            return
        try:
            lines, rotated = proc.log.write(chunk)
        except OSError as exc:
            # 盘写不进去也要接着读管道，不然进程写满管道就卡住了
            proc.log_error = str(exc) or type(exc).__name__
            log.warning("process %s: writing its output failed, discarding further output: %s", proc.id, exc)
            return
        if proc.regex is not None and proc.ready_at is None:
            first_number = proc.log.lines_done - len(lines) + 1
            for offset, line in enumerate(lines):
                if _ready_hit(proc.regex, line):
                    proc.ready_at = time.time()
                    proc.ready_line = first_number + offset
                    break
            else:
                partial = proc.log.partial_text()
                if partial and _ready_hit(proc.regex, partial):
                    proc.ready_at = time.time()
                    proc.ready_line = proc.log.last_line
            if proc.ready_at is not None:
                self._write_meta_locked(proc)
                return
        if rotated:
            self._write_meta_locked(proc)

    def _claim_for_stop(self, procs: Iterable[_Proc], reason: str) -> list[_Proc]:
        """把还在跑的标成正在停，返回这次真由调用方去停的那些；别的线程已经在停的不重复停。"""
        claimed: list[_Proc] = []
        for proc in procs:
            with proc.lock:
                if proc.status == RUNNING:
                    proc.status = STOPPING
                    proc.stop_reason = reason
                    claimed.append(proc)
                elif proc.status == STARTING and not proc.stop_requested:
                    proc.stop_requested = reason
        return claimed

    def _stop_all(self, procs: list[_Proc], grace_sec: float | None = None) -> None:
        if not procs:
            return
        grace = self._limits().stop_grace_sec if grace_sec is None else grace_sec
        if len(procs) == 1:
            self._stop_one(procs[0], grace)
            return
        threads = [
            threading.Thread(target=self._stop_one, args=(proc, grace), name=f"process-stop-{proc.id}", daemon=True)
            for proc in procs
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + grace + _STOP_MARGIN_SEC
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _stop_one(self, proc: _Proc, grace_sec: float) -> None:
        popen = proc.popen
        if popen is not None:
            try:
                osproc.stop_tree(popen, tag=proc.tag, started=proc.started, grace_sec=grace_sec)
            except Exception:
                log.exception("stopping process %s (pid %s) failed", proc.id, proc.pid)
        reader = proc.reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=_READER_JOIN_SEC)
            if reader.is_alive():
                log.warning("process %s: something outside its process tree still holds its output pipe", proc.id)
        returncode = popen.poll() if popen is not None else None
        with proc.lock:
            if proc.status in ENDED:
                return
            self._finish_locked(proc, STOPPED, proc.stop_reason or REASON_STOP, returncode)
        log.info("resident process %s stopped (%s)", proc.id, proc.stop_reason)

    def _finish_locked(self, proc: _Proc, status: str, reason: str, returncode: int | None) -> None:
        proc.status = status
        proc.stop_reason = reason
        proc.returncode = returncode
        proc.ended_at = time.time()
        proc.log.close()
        self._write_meta_locked(proc)

    def _forget(self, proc: _Proc) -> None:
        with self._lock:
            if self._procs.get(proc.id) is proc:
                self._procs.pop(proc.id, None)
                self._stop_marks.pop(proc.id, None)
        with proc.lock:
            proc.log.close()
        self._discard_files(proc)

    def _discard_files(self, proc: _Proc) -> None:
        for path in (*proc.log.paths(), proc.meta_path):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            proc.log.directory.rmdir()

    @staticmethod
    def _discard_record(path: Path, data: dict[str, Any]) -> None:
        process_id = str(data.get("id") or path.stem)
        candidates = [path, path.parent / f"{process_id}.log", *path.parent.glob(f"{process_id}.log.*")]
        for candidate in candidates:
            with contextlib.suppress(OSError):
                candidate.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()

    def _meta_locked(self, proc: _Proc) -> dict[str, Any]:
        return {
            "version": _META_VERSION,
            "id": proc.id,
            "name": proc.name,
            "command": proc.command,
            "cwd": proc.cwd,
            "session_id": proc.session_id,
            "origin_session_id": proc.origin_session_id,
            "task_id": proc.task_id,
            "node_id": proc.node_id,
            "tag": proc.tag,
            "pid": proc.pid,
            "fingerprint": proc.started,
            "created_at": proc.created_at,
            "max_runtime_sec": proc.max_runtime_sec,
            "ready_pattern": proc.ready_pattern,
            "ready_at": proc.ready_at,
            "ready_line": proc.ready_line,
            "status": proc.status,
            "returncode": proc.returncode,
            "ended_at": proc.ended_at,
            "stop_reason": proc.stop_reason,
            "log": proc.log.record(),
        }

    def _write_meta_locked(self, proc: _Proc) -> None:
        self._write_json(proc.meta_path, self._meta_locked(proc))

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError:
            log.warning("writing resident process record %s failed", path, exc_info=True)

    def _view(self, proc: _Proc) -> dict[str, Any]:
        with proc.lock:
            return self._view_locked(proc)

    @staticmethod
    def _view_locked(proc: _Proc) -> dict[str, Any]:
        now = time.monotonic()
        active = proc.status in ACTIVE
        end = proc.ended_at if proc.ended_at is not None else time.time()
        return {
            "id": proc.id,
            "name": proc.name,
            "status": proc.status,
            "command": proc.command if len(proc.command) <= _LIST_COMMAND_CHARS else f"{proc.command[:_LIST_COMMAND_CHARS]}…",
            "cwd": proc.cwd,
            "session_id": proc.session_id,
            "origin_session_id": proc.origin_session_id,
            "task_id": proc.task_id,
            "node_id": proc.node_id,
            "pid": proc.pid,
            "created_at": _iso(proc.created_at),
            "uptime_sec": round(max(0.0, end - proc.created_at), 1),
            "idle_sec": round(now - proc.last_read, 1) if active else None,
            "max_runtime_sec": proc.max_runtime_sec,
            "ready_pattern": proc.ready_pattern,
            "ready": (proc.ready_at is not None) if proc.ready_pattern else None,
            "ready_line": proc.ready_line,
            "output_closed": proc.eof,
            "returncode": proc.returncode,
            "stop_reason": proc.stop_reason,
            "stop_reason_text": REASON_TEXT.get(proc.stop_reason, ""),
            "ended_at": _iso(proc.ended_at),
            "first_line": proc.log.first_line,
            "last_line": proc.log.last_line,
            "log_error": proc.log_error,
        }
