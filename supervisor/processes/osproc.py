"""常驻进程的平台差异：起进程，认进程（启动指纹、环境标记），连同子孙一起停。

PID 会被复用：停之前都先核对启动指纹；Windows 上停的那一刻手里攥着进程句柄，PID 换不了人。
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from toolbox.process_tree import taskkill_tree, win32

log = logging.getLogger(__name__)

TAG_ENV = "CLONOTH_PROCESS_TAG"
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

_PS_TIMEOUT_SEC = 5.0
_POLL_SEC = 0.05
_PROC = Path("/proc")
# /proc/<pid>/stat 右括号之后的字段下标，从 state 数起
_STAT_STATE = 0
_STAT_PGRP = 2
_STAT_STARTTIME = 19
_DEAD_STATES = frozenset({"Z", "X"})


def instance_key(workspace_root: Path) -> str:
    """同一台机器上多开的实例各有一个；环境标记带着它，清点残留时不碰别的实例的进程。"""
    resolved = str(Path(workspace_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def process_tag(instance: str, process_id: str) -> str:
    return f"{instance}/{process_id}"


def spawn(command: str, *, cwd: Path, env: dict[str, str], tag: str) -> subprocess.Popen:
    """经系统 shell 起命令：自成一个进程组（Windows 上是新进程组、不开窗口），stdout 和 stderr 合成一路。"""
    options: dict[str, Any] = {
        "shell": True,
        "cwd": str(cwd),
        "env": {**env, TAG_ENV: tag},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def fingerprint(pid: int, *, include_exited: bool = False) -> str | None:
    """进程的启动指纹，PID 换了人就对不上；看不到这个进程时给 None。

    默认只认还在跑的；include_exited 为真时已退出但还没被收走的也给（刚起完就退出的 shell 要记下它的指纹）。
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        info = win32().query(pid)
        if info is None or not (info[1] or include_exited):
            return None
        return str(info[0])
    if IS_LINUX:
        return _linux_fingerprint(pid, include_exited=include_exited)
    return _ps_fingerprint(pid)


def stop_tree(popen: subprocess.Popen, *, tag: str, started: str, grace_sec: float) -> None:
    """停掉自己起的一个进程和它的子孙。popen 还在手里：Windows 上它攥着句柄；POSIX 上组里还有进程时组号不会被复用。"""
    if IS_WINDOWS:
        _windows_stop(popen, _filetime(started))
    else:
        _posix_stop(popen, tag, grace_sec)


def reap(pid: int, started: str, tag: str, *, grace_sec: float) -> bool:
    """上一次运行留下的进程：核对身份后连同子孙一起停。返回有没有认出要停的进程。"""
    if IS_WINDOWS:
        created = _filetime(started)
        return created is not None and _windows_reap(pid, created)
    return _posix_reap(pid, started, tag, grace_sec)


def reap_instance(instance: str, *, grace_sec: float) -> list[int]:
    """Linux：环境里带着本实例标记的进程一律停掉，登记丢了的也算。别的平台读不到别人的环境，什么也不做。"""
    prefix = f"{instance}/"
    targets = tagged_pids(lambda value: value.startswith(prefix))
    if not targets:
        return []
    _terminate_tagged(targets, lambda pid: _has_tag_prefix(pid, prefix), grace_sec)
    return targets


def tagged_pids(match: Callable[[str], bool]) -> list[int]:
    """Linux：环境里的标记满足 match 的进程。"""
    if not IS_LINUX:
        return []
    own = os.getpid()
    found: list[int] = []
    try:
        names = os.listdir(_PROC)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == own:
            continue
        value = _environ_tag(pid)
        if value is not None and match(value):
            found.append(pid)
    return found


def _filetime(started: str) -> int | None:
    try:
        return int(str(started or "").strip())
    except ValueError:
        return None


def _windows_stop(popen: subprocess.Popen, created: int | None) -> None:
    if popen.poll() is None:
        taskkill_tree(popen.pid)
    if created is not None:
        # shell 先退出了的话，taskkill /T 顺着父子链找不到它留下的子孙，按创建时刻从快照里认
        win = win32()
        for pid, child_created in win.descendants(popen.pid, created):
            win.kill_verified(pid, child_created)
    if popen.poll() is None:
        with contextlib.suppress(OSError):
            popen.kill()


def _windows_reap(pid: int, created: int) -> bool:
    win = win32()
    with win.handle(pid) as handle:
        # 根进程已经没了或 PID 换了人，就分不清哪些孤儿是它留下的：宁可不停，也不误杀
        if handle is None or win.created(handle) != created:
            return False
        if win.alive(handle):
            taskkill_tree(pid)
        for child, child_created in win.descendants(pid, created):
            win.kill_verified(child, child_created)
        return True


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        log.warning("not allowed to signal process group %s", pgid)
        return False
    return True


def _group_exists(pgid: int) -> bool:
    """组里还有进程。没有权限发信号的组不是我们起的，按不存在算。"""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _signal_pid(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def _process_group(pid: int) -> int | None:
    if IS_LINUX:
        fields = _linux_stat(pid)
        if fields is None or len(fields) <= _STAT_PGRP:
            return None
        try:
            return int(fields[_STAT_PGRP])
        except ValueError:
            return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _wait_gone(alive: Callable[[], bool], grace_sec: float, tick: Callable[[], None] | None = None) -> bool:
    """grace_sec 内等到 alive() 为假；tick 每轮先跑一次（给组长收尸）。等到了返回 True。"""
    deadline = time.monotonic() + max(0.0, grace_sec)
    while True:
        if tick is not None:
            tick()
        if not alive():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SEC)


def _posix_stop(popen: subprocess.Popen, tag: str, grace_sec: float) -> None:
    pgid = popen.pid
    # setsid 出去的子孙不在组里，Linux 上按环境标记找回来
    escaped = [pid for pid in tagged_pids(lambda value: value == tag) if _process_group(pid) != pgid]
    _signal_group(pgid, signal.SIGTERM)
    for pid in escaped:
        _signal_pid(pid, signal.SIGTERM)

    def alive() -> bool:
        return _group_exists(pgid) or any(_has_tag(pid, tag) for pid in escaped)

    if not _wait_gone(alive, grace_sec, tick=popen.poll):
        _signal_group(pgid, signal.SIGKILL)
        for pid in escaped:
            if _has_tag(pid, tag):
                _signal_pid(pid, signal.SIGKILL)
    popen.poll()


def _posix_reap(pid: int, started: str, tag: str, grace_sec: float) -> bool:
    group = pid if started and fingerprint(pid) == started else None
    tagged = tagged_pids(lambda value: value == tag)
    if group is None and not tagged:
        return False
    if group is not None:
        _signal_group(group, signal.SIGTERM)
    for target in tagged:
        _signal_pid(target, signal.SIGTERM)

    def alive() -> bool:
        return (group is not None and _group_exists(group)) or any(_has_tag(target, tag) for target in tagged)

    if not _wait_gone(alive, grace_sec):
        if group is not None:
            _signal_group(group, signal.SIGKILL)
        for target in tagged:
            if _has_tag(target, tag):
                _signal_pid(target, signal.SIGKILL)
    return True


def _terminate_tagged(targets: list[int], still_ours: Callable[[int], bool], grace_sec: float) -> None:
    for target in targets:
        _signal_pid(target, signal.SIGTERM)
    if _wait_gone(lambda: any(still_ours(t) for t in targets), grace_sec):
        return
    for target in targets:
        if still_ours(target):
            _signal_pid(target, signal.SIGKILL)


def _has_tag(pid: int, tag: str) -> bool:
    return _environ_tag(pid) == tag


def _has_tag_prefix(pid: int, prefix: str) -> bool:
    value = _environ_tag(pid)
    return value is not None and value.startswith(prefix)


def _environ_tag(pid: int) -> str | None:
    try:
        raw = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    marker = f"{TAG_ENV}=".encode("ascii")
    for item in raw.split(b"\0"):
        if item.startswith(marker):
            return item[len(marker):].decode("utf-8", "replace")
    return None


def _linux_stat(pid: int) -> list[str] | None:
    try:
        raw = (_PROC / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    # 第二个字段是带括号的进程名，里面可以有空格和括号，从最后一个右括号往后切
    end = raw.rfind(b")")
    if end < 0:
        return None
    return [field.decode("ascii", "replace") for field in raw[end + 1:].split()]


def _boot_id() -> str:
    try:
        return (_PROC / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _linux_fingerprint(pid: int, *, include_exited: bool = False) -> str | None:
    fields = _linux_stat(pid)
    if fields is None or len(fields) <= _STAT_STARTTIME:
        return None
    if fields[_STAT_STATE] in _DEAD_STATES and not include_exited:
        return None
    # starttime 按开机后的时钟滴答计，换一次开机就从头数，带上 boot_id 才不会撞
    return f"{_boot_id()}:{fields[_STAT_STARTTIME]}"


def _ps_fingerprint(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_PS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = completed.stdout.strip()
    return started if completed.returncode == 0 and started else None
