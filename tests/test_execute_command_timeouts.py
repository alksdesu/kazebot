"""execute_command 的时限和杀进程：timeout_sec 夹到配置的上下限，超时或被取消时带回已经读到的输出，
连同子孙进程一起杀（POSIX 按进程组，Windows 按进程树）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from toolbox import _common, process_tree  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

# 包的 __init__ 把 execute_command 这个名字换成了函数，模块得从 sys.modules 里拿
exec_module = importlib.import_module("toolbox.builtins.execute_command")

_PARTIAL = "import sys, time; sys.stdout.write('partial line' + chr(10)); sys.stdout.flush(); time.sleep(30)"


class _Ctx(ToolContext):
    def __init__(self, workspace_root: Path, *, cancel_after: float | None = None) -> None:
        super().__init__(
            supervisor_url="http://unused", session_id="sess-exec", run_id="run-1", worker_id="worker-1",
            workspace_root=workspace_root, http=None, registry=None, task_id="task-1",  # type: ignore[arg-type]
        )
        self.cancel_after = cancel_after
        self._first_probe: float | None = None

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        if self.cancel_after is None:
            return False
        now = time.monotonic()
        if self._first_probe is None:
            self._first_probe = now
        return now - self._first_probe >= self.cancel_after


def _shell(*argv: str) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _execute(workspace: Path, code: str, ctx: _Ctx | None = None, **args: Any) -> dict[str, Any]:
    command = _shell(sys.executable, "-c", code)
    return asyncio.run(exec_module.execute_command({"command": command, **args}, ctx or _Ctx(workspace)))


def _runtime(workspace: Path, **execute_command: Any) -> dict[str, Any]:
    cfg = {"meta": {"execute_command": execute_command}}
    path = config_layout.runtime_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg


class TestTimeoutBounds:
    @pytest.mark.parametrize("raw, expected", [
        (None, 90.0), (True, 90.0), ("abc", 90.0), (float("nan"), 90.0), (0, 90.0), (-5, 90.0),
        (0.2, 1.0), (30, 30.0), ("45", 45.0), (10**9, 3600.0), (float("inf"), 3600.0),
    ])
    def test_the_shipped_bounds(self, raw: Any, expected: float) -> None:
        assert exec_module.command_timeout({}, raw) == expected

    def test_the_instance_can_narrow_them(self) -> None:
        cfg = {"meta": {"execute_command": {"min_timeout_sec": 5, "max_timeout_sec": 600, "default_timeout_sec": 120}}}

        assert exec_module.timeout_bounds(cfg) == (120.0, 5.0, 600.0)
        assert [exec_module.command_timeout(cfg, raw) for raw in (2, 300, 10_000, None)] == [5.0, 300.0, 600.0, 120.0]

    def test_the_default_stays_inside_the_bounds(self) -> None:
        cfg = {"meta": {"execute_command": {"min_timeout_sec": 5, "max_timeout_sec": 60, "default_timeout_sec": 90}}}

        assert exec_module.timeout_bounds(cfg) == (60.0, 5.0, 60.0)

    def test_a_maximum_below_the_minimum_collapses_onto_it(self) -> None:
        cfg = {"meta": {"execute_command": {"min_timeout_sec": 30, "max_timeout_sec": 10}}}

        assert exec_module.timeout_bounds(cfg) == (30.0, 30.0, 30.0)

    def test_a_zero_timeout_means_the_default_not_an_instant_kill(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, "print('hi')", timeout_sec=0)

        assert result["ok"] is True and result["data"]["output"].strip() == "hi"


class TestStoppedCommands:
    def test_a_timeout_brings_back_what_was_read(self, tmp_path: Path) -> None:
        started = time.monotonic()

        result = _execute(tmp_path, _PARTIAL, timeout_sec=2)

        assert time.monotonic() - started < 20
        assert result["ok"] is False and result["error"] == "timeout after 2.0s"
        assert result["timed_out"] is True and result["data"]["timed_out"] is True
        data = result["data"]
        assert data["result"].startswith(
            "ERROR: timeout after 2.0s; the command was killed. Output received before that:\n",
        )
        assert "partial line" in data["output"] and "partial line" in data["result"]

    def test_a_timeout_without_output_is_the_plain_error(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, "import time; time.sleep(30)", timeout_sec=1)

        assert result["error"] == "timeout after 1.0s"
        assert result["data"] == {"result": "ERROR: timeout after 1.0s", "timed_out": True}

    def test_a_cancelled_task_also_brings_back_what_was_read(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, _PARTIAL, _Ctx(tmp_path, cancel_after=2.0), timeout_sec=60)

        assert result["ok"] is False and result["cancelled"] is True and result["data"]["cancelled"] is True
        assert result["data"]["result"].startswith("ERROR: task cancelled; the command was killed.")
        assert "partial line" in result["data"]["output"]

    def test_the_instance_maximum_caps_what_the_model_asks_for(self, tmp_path: Path) -> None:
        _runtime(tmp_path, max_timeout_sec=1)

        result = _execute(tmp_path, "import time; time.sleep(30)", timeout_sec=600)

        assert result["error"] == "timeout after 1.0s"

    @pytest.mark.skipif(os.name != "nt", reason="taskkill 只在 Windows 上有")
    def test_on_windows_the_whole_tree_dies(self, tmp_path: Path) -> None:
        marker = tmp_path / "grandchild.pid"
        code = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(marker)!r}, 'w').write(str(child.pid)); "
            "time.sleep(60)"
        )

        result = _execute(tmp_path, code, timeout_sec=4)

        assert result["timed_out"] is True
        grandchild = int(marker.read_text())
        deadline = time.monotonic() + 15
        while (process_tree.win32().query(grandchild) or (0, False))[1] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not (process_tree.win32().query(grandchild) or (0, False))[1]


class _OsAs:
    """_common 眼里的 os 换成某个平台的样子；没换的照真的 os 走。"""

    def __init__(self, name: str, **overrides: Any) -> None:
        self.name = name
        self.__dict__.update(overrides)

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


class _Proc:
    def __init__(self, *, returncode: int | None = None, kill_error: Exception | None = None) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.kill_error = kill_error
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1
        if self.kill_error is not None:
            raise self.kill_error


class _Pinned:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.thread_name = ""

    def kill_tree_and_close(self) -> None:
        self.thread_name = threading.current_thread().name
        self.done.set()


class TestKillingTheTree:
    @pytest.fixture
    def on_windows(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        pinned = _Pinned()
        record = SimpleNamespace(pinned=pinned, pins=[], result=pinned)

        def pin(pid: int) -> Any:
            record.pins.append(pid)
            if isinstance(record.result, Exception):
                raise record.result
            return record.result

        monkeypatch.setattr(_common, "os", _OsAs("nt"))
        monkeypatch.setattr(_common.process_tree, "pin", pin)
        return record

    def test_a_running_process_is_pinned_and_its_tree_killed_in_the_background(self, on_windows: SimpleNamespace) -> None:
        proc = _Proc()

        _common.kill_process_group(proc)  # type: ignore[arg-type]

        assert on_windows.pinned.done.wait(5)
        assert on_windows.pins == [4242] and on_windows.pinned.thread_name == "kill-process-tree"
        assert proc.kills == 0

    @pytest.mark.parametrize("result", [None, OSError("access denied")])
    def test_without_a_handle_only_the_process_itself_is_killed(
        self, on_windows: SimpleNamespace, result: Any,
    ) -> None:
        on_windows.result = result
        proc = _Proc()

        _common.kill_process_group(proc)  # type: ignore[arg-type]

        assert proc.kills == 1

    def test_an_exited_process_is_not_looked_up_by_pid(self, on_windows: SimpleNamespace) -> None:
        proc = _Proc(returncode=0, kill_error=ProcessLookupError())

        _common.kill_process_group(proc)  # type: ignore[arg-type]

        assert on_windows.pins == [] and proc.kills == 1

    def test_terminating_waits_for_taskkill(self, on_windows: SimpleNamespace) -> None:
        proc = _Proc()

        asyncio.run(_common.terminate_process_tree(proc))  # type: ignore[arg-type]

        assert on_windows.pinned.done.is_set() and proc.kills == 1

    @pytest.fixture
    def on_posix(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        record = SimpleNamespace(signals=[], error=None)

        def killpg(pgid: int, sig: int) -> None:
            record.signals.append((pgid, sig))
            if record.error is not None:
                raise record.error

        def getpgid(pid: int) -> int:
            raise AssertionError("the group id is the child's pid; asking for it can hit a reused pid")

        monkeypatch.setattr(_common, "os", _OsAs("posix", killpg=killpg, getpgid=getpgid))
        monkeypatch.setattr(_common, "signal", SimpleNamespace(SIGKILL=9))
        return record

    def test_posix_kills_the_group_named_after_the_child(self, on_posix: SimpleNamespace) -> None:
        proc = _Proc()

        _common.kill_process_group(proc)  # type: ignore[arg-type]
        asyncio.run(_common.terminate_process_tree(proc))  # type: ignore[arg-type]

        assert on_posix.signals == [(4242, 9), (4242, 9)] and proc.kills == 0

    def test_posix_falls_back_to_the_process_when_the_group_is_gone(self, on_posix: SimpleNamespace) -> None:
        on_posix.error = ProcessLookupError()
        proc = _Proc()

        _common.kill_process_group(proc)  # type: ignore[arg-type]

        assert proc.kills == 1
