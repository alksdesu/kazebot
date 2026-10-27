"""supervisor 持有的常驻进程：任务结束、挂起恢复、engine 重排都不影响它；会话重置或不在了、空闲、到时、supervisor 退出时停掉。

进程都是真起的 Python 子进程；supervisor 重启后的清点核对启动指纹，PID 被复用时不误杀。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.processes.service as service_module  # noqa: E402
from engine.project_root import PROJECT_ROOT_KEY  # noqa: E402
from engine.tool_outputs import MAX_PATTERN_CHARS, session_dir_name  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.processes import osproc  # noqa: E402
from supervisor.processes.logs import ProcessLog  # noqa: E402
from supervisor.processes.service import (  # noqa: E402
    ProcessError,
    ProcessService,
    _Proc,
    limits_from,
    processes_dir,
)
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_group:3003"
OTHER_CONV = "qq_group:4004"
_TOKEN = "resident-processes-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

# 打出几行，再打一行收尾的，然后待着；最多待一分钟，测试出错没停掉也不会一直留着
_TALKER = """
import sys, time
out = sys.stdout.buffer
for index in range(int(sys.argv[1])):
    out.write(("line " + str(index + 1) + "\\n").encode())
    out.flush()
out.write(sys.argv[2].encode() + b"\\n")
out.flush()
for _ in range(600):
    time.sleep(0.1)
"""

_QUITTER = """
import sys
sys.stdout.buffer.write(b"bye\\n")
sys.stdout.buffer.flush()
sys.exit(int(sys.argv[1]))
"""

_MARKER = """
import pathlib, time
pathlib.Path("marker.txt").write_text("here", encoding="utf-8")
for _ in range(600):
    time.sleep(0.1)
"""


def _shell(*argv: str) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _runtime(workspace: Path, **processes: Any) -> None:
    path = config_layout.runtime_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"processes": {"stop_grace_sec": 0.5, **processes}}), encoding="utf-8")


def _wait_until(predicate: Callable[[], bool], timeout: float = 20.0, message: str = "condition not met in time") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.05)


def _kill_tree(popen: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(popen.pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(OSError):
            os.killpg(popen.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        popen.wait(timeout=10)


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / "talker.py").write_text(_TALKER, encoding="utf-8")
    (directory / "quitter.py").write_text(_QUITTER, encoding="utf-8")
    (directory / "marker.py").write_text(_MARKER, encoding="utf-8")
    return directory


def _talker(scripts: Path, lines: int = 1, last: str = "ready") -> str:
    return _shell(sys.executable, str(scripts / "talker.py"), str(lines), last)


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="resident-processes"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


@pytest.fixture
def service(state: SupervisorState, tmp_path: Path) -> Iterator[ProcessService]:
    _runtime(tmp_path)
    svc = ProcessService(state)
    state.processes = svc
    try:
        yield svc
    finally:
        svc.close()


def _session(state: SupervisorState, key: str = CONV) -> str:
    return state.get_or_create_session(channel="qq_group", conversation_key=key)


def _task(
    state: SupervisorState, session_id: str, *, node: str = "bootstrap.coder", caller: Task | None = None,
    **inputs: Any,
) -> Task:
    """一个已被 worker 领走、正在跑的节点任务。"""
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id,
            session_generation=state._current_session_generation_locked(session_id) or 1,
            kind=TaskKind.node,
            node_id=node,
            input_data=dict(inputs),
            caller_task_id=caller.task_id if caller is not None else None,
        )
        task.status = TaskStatus.running
        task.worker_id = "worker-1"
        task.claim_id = "claim-1"
    return task


def _start(service: ProcessService, task: Task, command: str, **kwargs: Any) -> dict[str, Any]:
    return service.start_process(task_id=task.task_id, command=command, **kwargs)["process"]


def _output(service: ProcessService, task: Task, process_id: str, **kwargs: Any) -> str:
    return service.read_output(task_id=task.task_id, process_id=process_id, **kwargs)["text"]


def _wait_for_line(service: ProcessService, task: Task, process_id: str, needle: str) -> None:
    _wait_until(lambda: needle in _output(service, task, process_id), message=f"{needle!r} never showed up")


def _by_id(service: ProcessService) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in service.list_processes()["processes"]}


def _fake_proc(tmp_path: Path, *, pattern: str = "") -> _Proc:
    log = ProcessLog(tmp_path / "fake", "p_fake", max_bytes=1 << 20, keep_files=1)
    log.open()
    return _Proc(
        id="p_fake", name="fake", command="fake", cwd=str(tmp_path), session_id="s", origin_session_id="s",
        task_id="t", node_id="n", tag="i/p_fake", created_at=time.time(), max_runtime_sec=60.0,
        ready_pattern=pattern, log=log, regex=re.compile(pattern) if pattern else None,
    )


class TestStartAndRead:
    def test_a_started_process_is_read_back_through_its_task(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        sid = _session(state)
        task = _task(state, sid)

        view = _start(service, task, _talker(scripts, 3, "ready"), name="dev server")

        assert view["status"] == "running" and view["id"].startswith("p_")
        assert view["name"] == "dev server" and view["pid"] > 0
        assert view["session_id"] == sid and view["origin_session_id"] == sid
        assert view["task_id"] == task.task_id and view["node_id"] == "bootstrap.coder"
        assert Path(view["cwd"]).resolve() == tmp_path.resolve()
        assert view["ready"] is None and view["log_error"] == ""
        _wait_for_line(service, task, view["id"], "ready")
        text = _output(service, task, view["id"])
        assert "   1 | line 1" in text and "   4 | ready" in text

    def test_a_search_is_matched_outside_the_process_lock(
        self, service: ProcessService, state: SupervisorState, scripts: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 模型写的正则可能很慢：匹配期间读输出的线程和巡检线程都要拿得到这个进程的锁
        task = _task(state, _session(state))
        view = _start(service, task, _talker(scripts, 3, "ready"))
        _wait_for_line(service, task, view["id"], "ready")
        proc = service._procs[view["id"]]
        held: list[bool] = []
        render = service_module.render_search

        def watched(found: Any, budget: int) -> Any:
            held.append(proc.lock.locked())
            return render(found, budget)

        monkeypatch.setattr(service_module, "render_search", watched)

        text = _output(service, task, view["id"], search="line 2")

        assert held == [False]
        assert "   2: line 2" in text

    def test_the_name_defaults_to_the_command(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        command = _talker(scripts, 0)

        view = _start(service, _task(state, _session(state)), command)

        assert view["name"] == command[:64]

    def test_the_record_on_disk_follows_the_process(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        task = _task(state, sid)
        command = _talker(scripts, 0)

        view = _start(service, task, command)
        meta = processes_dir(state.workspace_root) / session_dir_name(sid) / f"{view['id']}.json"
        record = json.loads(meta.read_text(encoding="utf-8"))

        assert record["status"] == "running" and record["pid"] == view["pid"] and record["fingerprint"]
        assert record["tag"] == osproc.process_tag(service.instance, view["id"])
        assert record["command"] == command and record["session_id"] == sid and record["task_id"] == task.task_id

        stopped = service.stop_process(task_id=task.task_id, process_id=view["id"])

        assert stopped["stopped"] is True
        record = json.loads(meta.read_text(encoding="utf-8"))
        assert (record["status"], record["stop_reason"]) == ("stopped", "stop")
        assert record["ended_at"] is not None and record["returncode"] is not None

    def test_stopping_twice_says_there_was_nothing_left(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        task = _task(state, _session(state))
        view = _start(service, task, _talker(scripts, 0))

        service.stop_process(task_id=task.task_id, process_id=view["id"])
        again = service.stop_process(task_id=task.task_id, process_id=view["id"])

        assert again["stopped"] is False and again["process"]["status"] == "stopped"


class TestLifetime:
    def test_the_process_outlives_the_task_that_started_it(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        launcher = _task(state, sid)
        view = _start(service, launcher, _talker(scripts, 2, "up"))
        _wait_for_line(service, launcher, view["id"], "up")

        launcher.status = TaskStatus.completed
        service.sweep()
        later = _task(state, sid)

        assert service.status(task_id=later.task_id, process_id=view["id"])["process"]["status"] == "running"
        assert "   3 | up" in _output(service, later, view["id"])
        stopped = service.stop_process(task_id=later.task_id, process_id=view["id"])
        assert stopped["stopped"] is True
        assert (stopped["process"]["status"], stopped["process"]["stop_reason"]) == ("stopped", "stop")
        assert stopped["process"]["stop_reason_text"] == "被 stop_process 停掉"

    def test_suspend_resume_and_an_engine_restart_leave_it_running(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        task = _task(state, _session(state))
        view = _start(service, task, _talker(scripts, 1, "up"))
        _wait_for_line(service, task, view["id"], "up")

        # 挂起等子任务：不归哪个 worker，领取作废
        task.status, task.worker_id, task.claim_id = TaskStatus.suspended, None, ""
        service.sweep()
        # 恢复后由另一个 worker 领走，领取换新
        task.status, task.worker_id, task.claim_id = TaskStatus.running, "worker-2", "claim-2"
        service.sweep()
        # engine 退出，任务排回队列
        task.status, task.worker_id, task.claim_id = TaskStatus.pending, None, ""
        service.sweep()

        assert service.status(task_id=task.task_id, process_id=view["id"])["process"]["status"] == "running"
        assert "   2 | up" in _output(service, task, view["id"])
        assert service.stop_process(task_id=task.task_id, process_id=view["id"])["stopped"] is True

    def test_a_sub_agent_of_the_same_conversation_reaches_it(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        caller = _task(state, sid)
        view = _start(service, caller, _talker(scripts, 0))
        child = _task(state, sid, node="bootstrap.code_reviewer", caller=caller)

        assert [item["id"] for item in service.list_processes(task_id=child.task_id)["processes"]] == [view["id"]]
        assert service.stop_process(task_id=child.task_id, process_id=view["id"])["stopped"] is True

    def test_a_process_an_async_sub_agent_started_belongs_to_the_conversation(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        caller = _task(state, sid)
        agent_sid = _session(state, f"agent:helper:{CONV}:fresh-1")
        origin = {"parent_session_id": sid, "caller_task_id": caller.task_id, "caller_node_id": "bootstrap.coder",
                  "context_mode": "fresh"}
        child = _task(state, agent_sid, node="helper", _dispatch_origin=origin)

        view = _start(service, child, _talker(scripts, 0))

        assert view["session_id"] == sid and view["origin_session_id"] == agent_sid
        # 用完即删的 agent 会话没了，进程照样归这段对话
        with state._lock:
            state._forget_session_locked(agent_sid)
        service.sweep()
        assert service.status(task_id=caller.task_id, process_id=view["id"])["process"]["status"] == "running"

    def test_resetting_the_conversation_stops_its_processes(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        mine = _start(service, _task(state, sid), _talker(scripts, 0))
        theirs = _start(service, _task(state, _session(state, OTHER_CONV)), _talker(scripts, 0))

        assert state.reset_session(session_id=sid)["ok"] is True
        service.sweep()

        views = _by_id(service)
        assert (views[mine["id"]]["status"], views[mine["id"]]["stop_reason"]) == ("stopped", "session_reset")
        assert views[theirs["id"]]["status"] == "running"

    def test_stopping_the_conversation_stops_its_processes_and_its_sub_agents_ones(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        caller = _task(state, sid)
        agent_sid = _session(state, f"agent:helper:{CONV}:accumulate")
        origin = {"parent_session_id": sid, "caller_task_id": caller.task_id, "caller_node_id": "bootstrap.coder",
                  "context_mode": "accumulate"}
        child = _task(state, agent_sid, node="helper", _dispatch_origin=origin)
        mine = _start(service, caller, _talker(scripts, 0))
        sub_agents = _start(service, child, _talker(scripts, 0))
        theirs = _start(service, _task(state, _session(state, OTHER_CONV)), _talker(scripts, 0))

        assert state.cancel_session(sid) is True
        service.sweep()

        views = _by_id(service)
        for view in (mine, sub_agents):
            assert (views[view["id"]]["status"], views[view["id"]]["stop_reason"]) == ("stopped", "session_cancelled")
        assert views[theirs["id"]]["status"] == "running"
        assert service.status(task_id=caller.task_id, process_id=mine["id"])["process"]["stop_reason_text"] == (
            "会话被停止，跟着停掉"
        )

    def test_a_reset_only_marks_and_leaves_the_stopping_to_the_sweep(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        view = _start(service, _task(state, sid), _talker(scripts, 0))

        assert service.stop_for_session(sid) == 1
        assert service.stop_for_session(sid) == 0
        assert _by_id(service)[view["id"]]["status"] == "running"

        service.sweep()

        assert _by_id(service)[view["id"]]["stop_reason"] == "session_reset"

    def test_a_conversation_that_disappears_takes_its_processes_along(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        view = _start(service, _task(state, sid), _talker(scripts, 0))

        with state._lock:
            state._forget_session_locked(sid)
        service.sweep()

        stopped = _by_id(service)[view["id"]]
        assert (stopped["status"], stopped["stop_reason"]) == ("stopped", "session_gone")

    def test_closing_the_supervisor_stops_everything(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        task = _task(state, _session(state))
        first = _start(service, task, _talker(scripts, 0))
        second = _start(service, task, _talker(scripts, 0))

        service.close()

        views = _by_id(service)
        assert {views[first["id"]]["stop_reason"], views[second["id"]]["stop_reason"]} == {"supervisor_exit"}
        with pytest.raises(ProcessError) as closing:
            _start(service, task, _talker(scripts, 0))
        assert closing.value.status == 503

    def test_a_process_that_exits_on_its_own_keeps_its_log_for_a_while(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        task = _task(state, _session(state))
        view = _start(service, task, _shell(sys.executable, str(scripts / "quitter.py"), "3"))
        proc = service._procs[view["id"]]
        _wait_until(lambda: proc.eof and proc.popen is not None and proc.popen.poll() is not None)

        service.sweep()

        exited = service.status(task_id=task.task_id, process_id=view["id"])["process"]
        assert (exited["status"], exited["returncode"], exited["stop_reason"]) == ("exited", 3, "")
        assert exited["output_closed"] is True and exited["idle_sec"] is None
        assert "   1 | bye" in _output(service, task, view["id"])

        proc.ended_at = time.time() - 3601
        service.sweep()

        with pytest.raises(ProcessError) as gone:
            service.status(task_id=task.task_id, process_id=view["id"])
        assert gone.value.status == 404
        assert not proc.meta_path.exists() and not proc.log.active_path.exists()


class TestLimits:
    def test_per_conversation_and_global_caps(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        _runtime(tmp_path, max_per_session=2, max_total=3)
        mine = _task(state, _session(state))
        theirs = _task(state, _session(state, OTHER_CONV))
        first = _start(service, mine, _talker(scripts, 0))
        _start(service, mine, _talker(scripts, 0))

        with pytest.raises(ProcessError) as per_session:
            _start(service, mine, _talker(scripts, 0))
        assert per_session.value.status == 409
        assert "上限 2" in per_session.value.message and first["id"] in per_session.value.message

        _start(service, theirs, _talker(scripts, 0))
        with pytest.raises(ProcessError) as total:
            _start(service, theirs, _talker(scripts, 0))
        assert total.value.status == 409 and "总数已到上限 3" in total.value.message

        service.stop_process(task_id=mine.task_id, process_id=first["id"])
        assert _start(service, theirs, _talker(scripts, 0))["status"] == "running"

    def test_a_process_nobody_reads_is_stopped_as_idle(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        _runtime(tmp_path, idle_timeout_minutes=1)
        task = _task(state, _session(state))
        view = _start(service, task, _talker(scripts, 0))
        proc = service._procs[view["id"]]

        proc.last_read = time.monotonic() - 61
        _output(service, task, view["id"])
        service.sweep()

        assert proc.status == "running"

        proc.last_read = time.monotonic() - 61
        service.sweep()

        assert (proc.status, proc.stop_reason) == ("stopped", "idle")

    def test_an_idle_limit_of_zero_never_stops_it(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        _runtime(tmp_path, idle_timeout_minutes=0)
        view = _start(service, _task(state, _session(state)), _talker(scripts, 0))
        proc = service._procs[view["id"]]

        proc.last_read = time.monotonic() - 7 * 24 * 3600
        service.sweep()

        assert proc.status == "running"

    def test_max_runtime_is_capped_and_enforced(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        _runtime(tmp_path, max_runtime_cap_sec=120, default_max_runtime_sec=100)
        task = _task(state, _session(state))

        capped = _start(service, task, _talker(scripts, 0), max_runtime_sec=10_000)
        default = _start(service, task, _talker(scripts, 0))

        assert (capped["max_runtime_sec"], default["max_runtime_sec"]) == (120.0, 100.0)

        service._procs[capped["id"]].started_mono -= 121
        service.sweep()

        views = _by_id(service)
        assert (views[capped["id"]]["status"], views[capped["id"]]["stop_reason"]) == ("stopped", "max_runtime")
        assert views[default["id"]]["status"] == "running"

    @pytest.mark.parametrize("raw, expected", [
        (None, 100.0), (True, 100.0), ("abc", 100.0), (float("nan"), 100.0), (0, 100.0), (-5, 100.0),
        (0.2, 1.0), (50, 50.0), ("75", 75.0), (10**9, 120.0),
    ])
    def test_the_requested_runtime_is_normalised(self, raw: Any, expected: float) -> None:
        limits = limits_from({"processes": {"max_runtime_cap_sec": 120, "default_max_runtime_sec": 100}})

        assert ProcessService._runtime(raw, limits) == expected

    def test_the_limits_fall_back_to_their_bounds(self) -> None:
        limits = limits_from({"processes": {
            "max_per_session": 0, "max_total": 10_000, "idle_timeout_minutes": -3, "log_keep_files": 99,
            "default_max_runtime_sec": 999_999, "max_runtime_cap_sec": 600,
        }})

        assert (limits.max_per_session, limits.max_total, limits.idle_timeout_sec) == (1, 256, 0.0)
        assert limits.log_keep_files == 20
        assert (limits.default_max_runtime_sec, limits.max_runtime_cap_sec) == (600.0, 600.0)


class TestReady:
    def test_the_matching_line_marks_it_ready(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        task = _task(state, _session(state))

        view = _start(service, task, _talker(scripts, 2, "Listening on 8080"), ready_pattern=r"Listening on \d+")

        _wait_until(lambda: service.status(task_id=task.task_id, process_id=view["id"])["process"]["ready"] is True)
        ready = service.status(task_id=task.task_id, process_id=view["id"])["process"]
        assert ready["ready_line"] == 3 and ready["ready_pattern"] == r"Listening on \d+"

    @pytest.mark.parametrize("pattern, needle", [("(", "不是合法的正则"), ("x" * (MAX_PATTERN_CHARS + 1), "太长")])
    def test_a_bad_pattern_is_refused_before_anything_starts(
        self, service: ProcessService, state: SupervisorState, scripts: Path, pattern: str, needle: str,
    ) -> None:
        with pytest.raises(ProcessError) as refused:
            _start(service, _task(state, _session(state)), _talker(scripts, 0), ready_pattern=pattern)

        assert refused.value.status == 422 and needle in refused.value.message
        assert service.list_processes()["processes"] == []

    def test_a_prompt_without_a_newline_counts(self, service: ProcessService, tmp_path: Path) -> None:
        proc = _fake_proc(tmp_path, pattern="password:")
        try:
            with proc.lock:
                service._record_output_locked(proc, b"booting\nEnter password:")
        finally:
            proc.log.close()

        assert proc.ready_line == 2 and proc.ready_at is not None

    def test_a_log_that_cannot_be_written_is_reported_and_the_pipe_still_drains(
        self, service: ProcessService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc = _fake_proc(tmp_path)
        calls: list[bytes] = []

        def broken(data: bytes) -> tuple[list[str], bool]:
            calls.append(data)
            raise OSError("disk full")

        monkeypatch.setattr(proc.log, "write", broken)
        try:
            with proc.lock:
                service._record_output_locked(proc, b"x\n")
                service._record_output_locked(proc, b"y\n")
        finally:
            proc.log.close()

        assert proc.log_error == "disk full" and calls == [b"x\n"]
        assert service._view(proc)["log_error"] == "disk full"


class TestAccess:
    def test_another_conversation_cannot_see_or_touch_it(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        mine = _task(state, _session(state))
        stranger = _task(state, _session(state, OTHER_CONV))
        view = _start(service, mine, _talker(scripts, 0))

        for call in (service.status, service.read_output, service.stop_process):
            with pytest.raises(ProcessError) as hidden:
                call(task_id=stranger.task_id, process_id=view["id"])
            assert hidden.value.status == 404
        assert service.list_processes(task_id=stranger.task_id)["processes"] == []
        assert service._procs[view["id"]].status == "running"
        # 不带任务的是控制台，看得到全部
        assert [item["id"] for item in service.list_processes()["processes"]] == [view["id"]]
        assert service.status(task_id="", process_id=view["id"])["process"]["status"] == "running"

    def test_who_may_start_one(self, service: ProcessService, state: SupervisorState, scripts: Path) -> None:
        sid = _session(state)
        finished = _task(state, sid)
        finished.status = TaskStatus.completed

        for task_id, status in (("", 422), ("missing-task", 404), (finished.task_id, 409)):
            with pytest.raises(ProcessError) as refused:
                service.start_process(task_id=task_id, command=_talker(scripts, 0))
            assert refused.value.status == status
        with pytest.raises(ProcessError) as empty:
            service.start_process(task_id=_task(state, sid).task_id, command="   ")
        assert empty.value.status == 422
        assert service.list_processes()["processes"] == []

    def test_a_task_whose_conversation_is_gone_cannot_start_one(
        self, service: ProcessService, state: SupervisorState, scripts: Path,
    ) -> None:
        sid = _session(state)
        task = _task(state, sid)
        with state._lock:
            state._forget_session_locked(sid)

        with pytest.raises(ProcessError) as refused:
            _start(service, task, _talker(scripts, 0))

        assert refused.value.status == 409 and "不能持有常驻进程" in refused.value.message

    def test_a_command_that_cannot_start_leaves_nothing_behind(
        self, service: ProcessService, state: SupervisorState, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = _task(state, _session(state))

        def refuse(*args: Any, **kwargs: Any) -> subprocess.Popen:
            raise OSError("no shell")

        monkeypatch.setattr(osproc, "spawn", refuse)

        with pytest.raises(ProcessError) as failed:
            service.start_process(task_id=task.task_id, command="anything")

        assert failed.value.status == 422 and "no shell" in failed.value.message
        assert service.list_processes()["processes"] == []
        base = processes_dir(state.workspace_root)
        assert not base.exists() or not any(base.rglob("*.log*"))

    def test_a_reset_while_it_starts_stops_it_at_once(
        self, service: ProcessService, state: SupervisorState, scripts: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid = _session(state)
        task = _task(state, sid)
        real_spawn = osproc.spawn

        def spawn_then_reset(*args: Any, **kwargs: Any) -> subprocess.Popen:
            popen = real_spawn(*args, **kwargs)
            service.stop_for_sessions({sid})
            return popen

        monkeypatch.setattr(osproc, "spawn", spawn_then_reset)

        view = _start(service, task, _talker(scripts, 0))

        assert (view["status"], view["stop_reason"]) == ("stopped", "session_reset")


class TestRootCleanup:
    def test_only_processes_under_the_root_are_stopped(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        worktree = tmp_path / "worktrees" / "feature"
        worktree.mkdir(parents=True)
        sid = _session(state)
        inside = _start(service, _task(state, sid, **{PROJECT_ROOT_KEY: str(worktree)}),
                        _shell(sys.executable, str(scripts / "marker.py")))
        outside = _start(service, _task(state, sid), _talker(scripts, 0))

        assert Path(inside["cwd"]) == worktree.resolve()
        _wait_until(lambda: (worktree / "marker.txt").exists(), message="the process did not run in its project root")

        assert service.stop_under_root(worktree) == 1

        views = _by_id(service)
        assert (views[inside["id"]]["status"], views[inside["id"]]["stop_reason"]) == ("stopped", "root_cleanup")
        assert views[outside["id"]]["status"] == "running"


class TestRecovery:
    @staticmethod
    def _record(
        service: ProcessService, session_id: str, process_id: str, *, pid: int, fingerprint: str, tag: str,
        status: str = "running", **extra: Any,
    ) -> Path:
        directory = service.base_dir / session_dir_name(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "version": 1, "id": process_id, "name": "left", "command": "left", "cwd": str(service.root),
            "session_id": session_id, "origin_session_id": session_id, "task_id": "task-old",
            "node_id": "bootstrap.coder", "tag": tag, "pid": pid, "fingerprint": fingerprint,
            "created_at": time.time() - 100, "max_runtime_sec": 3600.0, "ready_pattern": "", "ready_at": None,
            "ready_line": None, "status": status, "returncode": None, "ended_at": None, "stop_reason": "",
            "log": {"first_lines": [1], "lines_done": 0, "partial": False}, **extra,
        }
        path = directory / f"{process_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_a_process_left_by_the_last_run_is_stopped(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        sid = _session(state)
        tag = osproc.process_tag(service.instance, "p_left")
        popen = osproc.spawn(_talker(scripts, 0), cwd=tmp_path, env=dict(os.environ), tag=tag)
        # POSIX 上替它收尸：僵尸还算进程组成员，停的一方会一直等到宽限期满
        threading.Thread(target=popen.wait, daemon=True).start()
        try:
            fingerprint = osproc.fingerprint(popen.pid, include_exited=True)
            assert fingerprint
            path = self._record(service, sid, "p_left", pid=popen.pid, fingerprint=fingerprint, tag=tag)

            assert service.recover() >= 1

            assert popen.wait(timeout=15) is not None
        finally:
            if popen.poll() is None:
                _kill_tree(popen)

        record = json.loads(path.read_text(encoding="utf-8"))
        assert (record["status"], record["stop_reason"]) == ("stopped", "supervisor_restart")
        [listed] = service.list_processes()["processes"]
        assert listed["id"] == "p_left" and listed["stop_reason_text"] == "supervisor 重启，上次留下的进程已停掉"

    def test_a_reused_pid_is_left_alone(
        self, service: ProcessService, state: SupervisorState, scripts: Path, tmp_path: Path,
    ) -> None:
        sid = _session(state)
        # 占着这个 PID 的是别人起的进程：启动指纹对不上，环境标记也不是这一条登记的
        stranger = osproc.spawn(_talker(scripts, 0), cwd=tmp_path, env=dict(os.environ), tag="another-instance/p_x")
        threading.Thread(target=stranger.wait, daemon=True).start()
        try:
            path = self._record(
                service, sid, "p_reused", pid=stranger.pid, fingerprint="12345",
                tag=osproc.process_tag(service.instance, "p_reused"),
            )

            service.recover()
            time.sleep(0.5)

            assert stranger.poll() is None
        finally:
            _kill_tree(stranger)

        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "stopped"

    def test_old_records_and_stray_logs_go_recent_ones_stay_readable(
        self, service: ProcessService, state: SupervisorState,
    ) -> None:
        sid = _session(state)
        now = time.time()
        old = self._record(service, sid, "p_old", pid=0, fingerprint="", tag="", status="exited",
                           ended_at=now - 4000, returncode=0)
        (old.parent / "p_old.log").write_bytes(b"gone\n")
        (old.parent / "p_old.log.1").write_bytes(b"older\n")
        recent = self._record(service, sid, "p_new", pid=0, fingerprint="", tag="", status="exited",
                              ended_at=now - 60, returncode=0,
                              log={"first_lines": [1], "lines_done": 2, "partial": False})
        (recent.parent / "p_new.log").write_bytes(b"first\nsecond\n")
        stray_old = recent.parent / "p_orphan.log"
        stray_old.write_bytes(b"x\n")
        os.utime(stray_old, (now - 4000, now - 4000))
        stray_new = recent.parent / "p_fresh.log"
        stray_new.write_bytes(b"x\n")
        broken = recent.parent / "p_broken.json"
        broken.write_text("{not json", encoding="utf-8")

        assert service.recover() == 0

        assert not old.exists() and not (old.parent / "p_old.log").exists()
        assert not (old.parent / "p_old.log.1").exists()
        assert not stray_old.exists() and stray_new.exists() and not broken.exists()
        [kept] = service.list_processes()["processes"]
        assert (kept["id"], kept["status"], kept["returncode"]) == ("p_new", "exited", 0)
        text = service.read_output(task_id="", process_id="p_new")["text"]
        assert "   1 | first" in text and "   2 | second" in text


class TestHttpApi:
    @pytest.fixture
    def client(self, state: SupervisorState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
        monkeypatch.setattr(admin_api, "_admin_token", "")
        monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())
        _runtime(tmp_path)
        app = create_app(
            state=state, process_manager=None,
            config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
        )
        try:
            yield TestClient(app, headers=_AUTH)
        finally:
            state.processes.close()

    def test_the_endpoints_round_trip(self, client: TestClient, state: SupervisorState, scripts: Path) -> None:
        task = _task(state, _session(state))
        params = {"task_id": task.task_id}

        started = client.post("/v1/processes", json={
            "task_id": task.task_id, "command": _talker(scripts, 1, "up"), "name": "srv",
        })

        assert started.status_code == 200, started.text
        process_id = started.json()["process"]["id"]
        _wait_until(lambda: "up" in client.get(f"/v1/processes/{process_id}/output", params=params).json()["text"])
        assert [item["id"] for item in client.get("/v1/processes", params=params).json()["processes"]] == [process_id]
        status = client.get(f"/v1/processes/{process_id}", params=params).json()["process"]
        assert (status["name"], status["status"]) == ("srv", "running")
        page = client.get(f"/v1/processes/{process_id}/output", params={**params, "since": 1}).json()
        assert page["start_line"] == 2 and "   2 | up" in page["text"]
        found = client.get(f"/v1/processes/{process_id}/output", params={**params, "search": "up"}).json()
        assert "命中 1 行" in found["text"]
        stopped = client.post(f"/v1/processes/{process_id}/stop", json=params).json()
        assert stopped["stopped"] is True and stopped["process"]["status"] == "stopped"

    def test_errors_keep_their_status_codes(self, client: TestClient, state: SupervisorState) -> None:
        task = _task(state, _session(state))
        params = {"task_id": task.task_id}

        assert client.get("/v1/processes/p_missing", params=params).status_code == 404
        assert client.post("/v1/processes", json={"task_id": task.task_id, "command": ""}).status_code == 422
        assert client.post("/v1/processes", json={"task_id": "nope", "command": "x"}).status_code == 404
        assert client.get("/v1/processes/p_x/output", params={**params, "tail": 0}).status_code == 422
        assert client.get("/v1/processes", headers={"Authorization": "Bearer wrong"}).status_code == 401
