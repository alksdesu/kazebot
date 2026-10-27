"""连同子孙一起停一个进程的 Windows 部分：taskkill /T /F 按 PID 认进程，停之前攥住句柄，PID 这期间换不了人。

POSIX 上调用方按进程组发信号，用不着这里。
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
from collections.abc import Iterator

log = logging.getLogger(__name__)

TASKKILL_TIMEOUT_SEC = 15.0


def taskkill_tree(pid: int) -> bool:
    """taskkill /T /F：从 pid 顺着父子关系往下全停掉。父进程得还活着，子孙才找得到。"""
    try:
        completed = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=TASKKILL_TIMEOUT_SEC, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("taskkill of process %s failed", pid, exc_info=True)
        return False
    return completed.returncode == 0


class PinnedProcess:
    """开着句柄的一个进程：句柄关掉之前，哪怕进程已经退出，这个 PID 也不会分给别人。"""

    def __init__(self, win: "Win32", pid: int, handle: int) -> None:
        self._win = win
        self.pid = pid
        self._handle: int | None = handle

    def alive(self) -> bool:
        return self._handle is not None and self._win.alive(self._handle)

    def created(self) -> int | None:
        return None if self._handle is None else self._win.created(self._handle)

    def kill_tree(self) -> bool:
        """还活着就连同子孙停掉；已经退出的不再按 PID 去找。"""
        return self.alive() and taskkill_tree(self.pid)

    def terminate(self) -> bool:
        """只停它自己：taskkill 用不了时的退路。"""
        return self._handle is not None and self._win.terminate(self._handle)

    def kill_tree_and_close(self) -> None:
        """在后台线程里跑：停完再放开句柄，调用方中途不等了也不会提前放掉 PID。"""
        try:
            if not self.kill_tree() and self.alive():
                self.terminate()
        finally:
            self.close()

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._win.close(handle)


def pin(pid: int) -> PinnedProcess | None:
    """攥住 pid 这个进程的句柄；打不开（已经没了、没有权限）给 None。只在 Windows 上用。"""
    win = win32()
    handle = win.open(pid, terminate=True)
    return None if handle is None else PinnedProcess(win, pid, handle)


class Win32:
    """要用到的几个 kernel32 调用，只在 Windows 上构造。"""

    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * self.MAX_PATH),
            ]

        self._entry_type = ProcessEntry
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        filetime = ctypes.POINTER(wintypes.FILETIME)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        entry_pointer = ctypes.POINTER(ProcessEntry)
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, entry_pointer)
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, entry_pointer)
        kernel32.Process32NextW.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._invalid_handle = wintypes.HANDLE(-1).value

    def open(self, pid: int, *, terminate: bool = False) -> int | None:
        access = self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE
        if terminate:
            access |= self.PROCESS_TERMINATE
        opened = self._kernel32.OpenProcess(access, False, pid)
        return opened or None

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    @contextlib.contextmanager
    def handle(self, pid: int) -> Iterator[int | None]:
        opened = self.open(pid)
        try:
            yield opened
        finally:
            if opened is not None:
                self.close(opened)

    def created(self, handle: int) -> int | None:
        times = [self._wintypes.FILETIME() for _ in range(4)]
        if not self._kernel32.GetProcessTimes(handle, *(self._ctypes.byref(item) for item in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def alive(self, handle: int) -> bool:
        return self._kernel32.WaitForSingleObject(handle, 0) == self.WAIT_TIMEOUT

    def terminate(self, handle: int) -> bool:
        return bool(self._kernel32.TerminateProcess(handle, 1))

    def query(self, pid: int) -> tuple[int, bool] | None:
        """(创建时刻, 是否还在跑)；打不开这个进程给 None。"""
        with self.handle(pid) as handle:
            if handle is None:
                return None
            created = self.created(handle)
            return None if created is None else (created, self.alive(handle))

    def snapshot(self) -> list[tuple[int, int]]:
        """系统里现在每个进程的 (pid, 父 pid)。"""
        snap = self._kernel32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snap or snap == self._invalid_handle:
            return []
        pairs: list[tuple[int, int]] = []
        try:
            entry = self._entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            more = self._kernel32.Process32FirstW(snap, self._ctypes.byref(entry))
            while more:
                pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                more = self._kernel32.Process32NextW(snap, self._ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snap)
        return pairs

    def descendants(self, root_pid: int, root_created: int) -> list[tuple[int, int]]:
        """root 名下还活着的子孙 (pid, 创建时刻)，父进程先退出、父子链断了的也算。

        父 pid 只是一个数：比父进程创建得早的不是它的孩子；根的 PID 已被别人占用时，占用之后才创建的也不是。
        """
        children: dict[int, list[int]] = {}
        for pid, parent in self.snapshot():
            if pid and pid != parent:
                children.setdefault(parent, []).append(pid)
        limit: int | None = None
        holder = self.query(root_pid)
        if holder is not None and holder[0] != root_created:
            limit = holder[0]
        found: list[tuple[int, int]] = []
        seen = {root_pid}
        stack: list[tuple[int, int, int | None]] = [(root_pid, root_created, limit)]
        while stack:
            parent, parent_created, parent_limit = stack.pop()
            for child in children.get(parent, ()):
                if child in seen:
                    continue
                info = self.query(child)
                if info is None:
                    continue
                created, running = info
                if created < parent_created or (parent_limit is not None and created >= parent_limit):
                    continue
                seen.add(child)
                if running:
                    found.append((child, created))
                stack.append((child, created, None))
        return found

    def kill_verified(self, pid: int, created: int) -> bool:
        """核对创建时刻后连同子孙停掉 pid；核对和 taskkill 期间一直攥着句柄。"""
        with self.handle(pid) as handle:
            if handle is None or self.created(handle) != created or not self.alive(handle):
                return False
            return taskkill_tree(pid)


_WIN32: Win32 | None = None


def win32() -> Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = Win32()
    return _WIN32
