"""卡死的 running 任务由 supervisor/task_reaper.py 一处判定、一处收尾。

判定分四条：跑过硬上限、心跳沉默太久、running 却没有租约、租约过期。前两条连挂着审批的
任务也照收——审批是合法阻塞，但 worker 都不在了再等下去只会把聊天卡死。收尾必须走正常的
完成路由，否则分支任务会永远悬在父会话上。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

import clonoth_runtime  # noqa: E402
from clonoth_runtime import (  # noqa: E402
    DEFAULT_TASK_MAX_RUNNING_SECONDS,
    TASK_REAP_INTERVAL_SECONDS,
    task_max_running_seconds,
)
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.scheduler import SchedulerThread  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_reaper import (  # noqa: E402
    HEARTBEAT_SILENCE_LIMIT,
    LEASE_GRACE,
    MISSING_LEASE_GRACE,
    reap_reason,
    reap_stuck_tasks_locked,
)
from supervisor.types import (  # noqa: E402
    Approval,
    ApprovalStatus,
    Task,
    TaskKind,
    TaskStatus,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_MAX_RUNNING = 3600.0

# tests/ 之外的代码只有 clonoth_runtime 该读这个变量，别处再读一次就会各有各的默认值。
_TIME_LIMIT_ENV = "CLONOTH_TASK_MAX_RUNNING_SECONDS"
_SKIP_DIRS = {".venv", "__pycache__", "_research", "tests", "node_modules", ".git", ".claude"}


@pytest.mark.parametrize("raw, expected", [
    (None, DEFAULT_TASK_MAX_RUNNING_SECONDS),
    ("7200", 7200.0),
    ("100", 300.0),
    ("abc", DEFAULT_TASK_MAX_RUNNING_SECONDS),
    ("", DEFAULT_TASK_MAX_RUNNING_SECONDS),
    ("   ", DEFAULT_TASK_MAX_RUNNING_SECONDS),
])
def test_task_max_running_seconds_env_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float,
) -> None:
    if raw is None:
        monkeypatch.delenv(_TIME_LIMIT_ENV, raising=False)
    else:
        monkeypatch.setenv(_TIME_LIMIT_ENV, raw)

    assert task_max_running_seconds() == expected


def test_only_clonoth_runtime_reads_the_time_limit_env() -> None:
    offenders: list[str] = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts) or rel.as_posix() == "clonoth_runtime.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _TIME_LIMIT_ENV in text:
            offenders.append(rel.as_posix())

    assert offenders == [], f"{_TIME_LIMIT_ENV} 只能由 clonoth_runtime.task_max_running_seconds() 读: {offenders}"


def _task(
    *,
    created_ago: timedelta = timedelta(seconds=0),
    silent_for: timedelta = timedelta(seconds=0),
    lease_in: timedelta | None = timedelta(seconds=120),
    session_id: str = "s-main",
    status: TaskStatus = TaskStatus.running,
) -> Task:
    return Task(
        task_id="t-1",
        session_id=session_id,
        kind=TaskKind.node,
        node_id="test.node",
        status=status,
        created_at=_NOW - created_ago,
        updated_at=_NOW - silent_for,
        lease_expires_at=None if lease_in is None else _NOW + lease_in,
    )


def _reason(task: Task, *, approval_pending: bool = False) -> str | None:
    return reap_reason(
        task, now=_NOW, max_running_sec=_MAX_RUNNING, approval_pending=approval_pending,
    )


def test_a_fresh_task_with_a_live_lease_is_kept() -> None:
    assert _reason(_task()) is None


@pytest.mark.parametrize("approval_pending", [False, True])
def test_age_over_the_hard_limit_reaps_even_while_an_approval_waits(approval_pending: bool) -> None:
    reason = _reason(
        _task(created_ago=timedelta(seconds=_MAX_RUNNING + 1)), approval_pending=approval_pending,
    )

    assert reason is not None
    assert "3601" in reason and "3600" in reason


def test_age_exactly_at_the_hard_limit_is_kept() -> None:
    assert _reason(_task(created_ago=timedelta(seconds=_MAX_RUNNING))) is None


@pytest.mark.parametrize("approval_pending", [False, True])
def test_heartbeat_silence_over_ten_minutes_reaps_even_while_an_approval_waits(
    approval_pending: bool,
) -> None:
    reason = _reason(
        _task(silent_for=HEARTBEAT_SILENCE_LIMIT + timedelta(seconds=1)),
        approval_pending=approval_pending,
    )

    assert reason is not None
    assert "10 分钟无响应" in reason


def test_heartbeat_silence_exactly_at_the_limit_is_kept() -> None:
    assert _reason(_task(silent_for=HEARTBEAT_SILENCE_LIMIT)) is None


def test_a_pending_approval_protects_an_expired_lease() -> None:
    task = _task(lease_in=-(LEASE_GRACE + timedelta(seconds=1)))

    assert _reason(task) is not None
    assert _reason(task, approval_pending=True) is None


def test_a_missing_lease_is_reaped_only_after_five_silent_minutes() -> None:
    assert _reason(_task(lease_in=None, silent_for=MISSING_LEASE_GRACE)) is None

    reason = _reason(_task(lease_in=None, silent_for=MISSING_LEASE_GRACE + timedelta(seconds=1)))
    assert reason is not None
    assert "没有租约" in reason


def test_a_missing_lease_is_protected_by_a_pending_approval() -> None:
    task = _task(lease_in=None, silent_for=MISSING_LEASE_GRACE + timedelta(seconds=1))

    assert _reason(task, approval_pending=True) is None


def test_a_pending_task_is_reaped_after_queue_ttl() -> None:
    task = _task(status=TaskStatus.pending, created_ago=timedelta(minutes=15, seconds=1))

    reason = _reason(task)

    assert reason is not None and "排队过久" in reason


def test_a_fresh_pending_task_is_kept() -> None:
    assert _reason(_task(status=TaskStatus.pending, created_ago=timedelta(minutes=15))) is None


def test_a_suspended_task_without_wait_target_is_reaped_after_silence() -> None:
    task = _task(status=TaskStatus.suspended, silent_for=timedelta(minutes=20, seconds=1))

    reason = _reason(task)

    assert reason is not None and "挂起过久" in reason


def test_a_suspended_task_waiting_for_a_terminal_child_gets_a_grace_period() -> None:
    task = _task(status=TaskStatus.suspended, silent_for=timedelta(minutes=4, seconds=59))
    task.waiting_for_task_id = "child"

    assert reap_reason(
        task,
        now=_NOW,
        max_running_sec=_MAX_RUNNING,
        approval_pending=False,
        waiting_task_terminal=True,
    ) is None
    assert reap_reason(
        task,
        now=_NOW + timedelta(minutes=5),
        max_running_sec=_MAX_RUNNING,
        approval_pending=False,
        waiting_task_terminal=True,
    ) is not None


def test_an_expired_lease_is_reaped_only_after_the_grace_minute() -> None:
    assert _reason(_task(lease_in=-LEASE_GRACE)) is None

    reason = _reason(_task(lease_in=-(LEASE_GRACE + timedelta(seconds=1))))
    assert reason == "任务租约过期，worker 已失联，按僵尸任务回收"


def test_the_hard_limit_is_read_from_the_caller_not_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_TIME_LIMIT_ENV, "99999")
    task = _task(created_ago=timedelta(seconds=400))

    assert reap_reason(task, now=_NOW, max_running_sec=300.0, approval_pending=False) is not None


def _make_state(workspace: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="reaper-test"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _running(
    state: SupervisorState,
    session_id: str,
    *,
    lease_in: timedelta | None,
    created_ago: timedelta = timedelta(seconds=30),
    silent_for: timedelta = timedelta(seconds=5),
    status: TaskStatus = TaskStatus.running,
    input_data: dict | None = None,
    now: datetime = _NOW,
) -> Task:
    with state._lock:
        task = state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.node",
            input_data=input_data or {"task_context": {"conversation_key": "test:reaper"}},
            continuation={},
        )
    task.status = status
    task.worker_id = "worker-1"
    task.created_at = now - created_ago
    task.updated_at = now - silent_for
    task.lease_expires_at = None if lease_in is None else now + lease_in
    return task


def _add_pending_approval(state: SupervisorState, session_id: str, approval_id: str = "a-1") -> None:
    state.approvals[approval_id] = Approval(
        approval_id=approval_id,
        session_id=session_id,
        operation="execute_command",
        status=ApprovalStatus.pending,
        fingerprint="fp",
        requested_at=_NOW,
    )


def _events(workspace: Path) -> list[dict]:
    path = workspace / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_an_expired_lease_task_is_failed_with_an_event_and_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(state, session_id, lease_in=-timedelta(minutes=5))
    routed: list[str] = []
    monkeypatch.setattr(
        SupervisorState,
        "_route_completed_task_locked",
        lambda self, t: routed.append(t.task_id),
    )

    with state._lock:
        reaped = reap_stuck_tasks_locked(state, now=_NOW)

    assert reaped == [task.task_id]
    assert routed == [task.task_id]
    assert task.status == TaskStatus.failed
    assert task.lease_expires_at is None
    assert task.updated_at == _NOW
    assert task.result["action"] == "fail"
    assert task.result["node_id"] == "test.node"
    assert "租约过期" in task.result["error"]

    completed = [e for e in _events(tmp_path) if e.get("type") == "task_completed"]
    assert [e["payload"]["task_id"] for e in completed] == [task.task_id]
    assert completed[0]["component"] == "supervisor"


@pytest.mark.parametrize("status", [TaskStatus.completed])
def test_terminal_tasks_are_not_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: TaskStatus,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(state, session_id, lease_in=-timedelta(hours=2), status=status)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        reaped = reap_stuck_tasks_locked(state, now=_NOW)

    assert reaped == []
    assert task.status == status


def test_session_scope_limits_which_tasks_are_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    wanted = state.get_or_create_session(channel="test", conversation_key="test:wanted")
    other = state.get_or_create_session(channel="test", conversation_key="test:other")
    in_scope = _running(state, wanted, lease_in=-timedelta(minutes=5))
    out_of_scope = _running(state, other, lease_in=-timedelta(minutes=5))
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        reaped = reap_stuck_tasks_locked(state, now=_NOW, session_ids={wanted})

    assert reaped == [in_scope.task_id]
    assert out_of_scope.status == TaskStatus.running


def test_an_approval_on_the_task_session_protects_it_from_lease_reaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(state, session_id, lease_in=-timedelta(minutes=5))
    _add_pending_approval(state, session_id)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == []
    assert task.status == TaskStatus.running


def test_an_approval_on_the_route_session_protects_a_branch_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:parent")
    branch = state.get_or_create_session(channel="test", conversation_key="test:branch")
    task = _running(
        state, branch, lease_in=-timedelta(minutes=5),
        input_data={"parent_session_id": parent, "task_context": {"conversation_key": "test:parent"}},
    )
    _add_pending_approval(state, parent)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == []
    assert task.status == TaskStatus.running


def test_an_approval_in_an_unrelated_session_protects_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    stranger = state.get_or_create_session(channel="test", conversation_key="test:stranger")
    task = _running(state, session_id, lease_in=-timedelta(minutes=5))
    _add_pending_approval(state, stranger)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == [task.task_id]


def test_an_approval_does_not_protect_a_task_past_the_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(
        state, session_id, lease_in=timedelta(minutes=2), created_ago=timedelta(seconds=_MAX_RUNNING + 60),
    )
    _add_pending_approval(state, session_id)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == [task.task_id]
    assert "运行过久" in task.result["error"]


def test_an_approval_does_not_protect_a_silent_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(
        state, session_id, lease_in=timedelta(minutes=2),
        silent_for=HEARTBEAT_SILENCE_LIMIT + timedelta(minutes=1),
    )
    _add_pending_approval(state, session_id)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == [task.task_id]
    assert "无响应" in task.result["error"]


def test_a_decided_approval_no_longer_protects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(state, session_id, lease_in=-timedelta(minutes=5))
    _add_pending_approval(state, session_id)
    state.approvals["a-1"].status = ApprovalStatus.allowed
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == [task.task_id]


def test_reaping_defaults_to_the_configured_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_TIME_LIMIT_ENV, "600")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(state, session_id, lease_in=timedelta(minutes=2), created_ago=timedelta(seconds=700))
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)

    with state._lock:
        assert reap_stuck_tasks_locked(state, now=_NOW) == [task.task_id]
    assert "上限 600 秒" in task.result["error"]


def test_running_tasks_endpoint_reaps_and_omits_the_stuck_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    # 端点用真实时钟判定，任务的时间戳也得贴着真实时钟造
    now = datetime.now(timezone.utc)
    stuck = _running(state, session_id, lease_in=-timedelta(minutes=5), now=now)
    alive = _running(state, session_id, lease_in=timedelta(minutes=2), now=now)
    monkeypatch.setattr(SupervisorState, "_route_completed_task_locked", lambda self, t: None)
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    async def _call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(f"/v1/sessions/{session_id}/running_tasks")

    response = asyncio.run(_call())

    assert response.status_code == 200
    listed = {item["task_id"] for item in response.json()["tasks"]}
    assert listed == {alive.task_id}
    assert stuck.status == TaskStatus.failed


def test_scheduler_cleanup_leaves_a_stale_running_task_alone(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reaper")
    task = _running(
        state, session_id, lease_in=None,
        created_ago=timedelta(hours=9), silent_for=timedelta(hours=9),
    )

    SchedulerThread(state=state, workspace_root=tmp_path)._cleanup()

    assert task.status == TaskStatus.running
    assert task.result == {}


def test_the_housekeeping_thread_reaps_and_prunes_sessions() -> None:
    """main.py 的后台线程只该做这两件事，换名字或漏掉一件都要在这里露出来。"""
    import inspect

    import supervisor.main as main_module

    source = inspect.getsource(main_module.main)
    assert "reap_stuck_tasks_locked(state)" in source
    assert "_cleanup_stale_sessions_locked()" in source
    assert 'name="supervisor-housekeeping"' in source
    assert "TASK_REAP_INTERVAL_SECONDS" in source


def test_dream_organizer_expires_one_reap_window_after_the_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.builtin.dream import DreamHandler

    monkeypatch.setenv(_TIME_LIMIT_ENV, "600")
    handler = DreamHandler()
    started = _NOW - timedelta(seconds=0)
    pending = {"dream_created_at": started.isoformat()}
    deadline = 600 + 2 * TASK_REAP_INTERVAL_SECONDS

    assert handler._organizer_expired(pending, now=started + timedelta(seconds=deadline)) is False
    assert handler._organizer_expired(pending, now=started + timedelta(seconds=deadline + 1)) is True


def test_dream_organizer_without_a_start_time_never_expires() -> None:
    from engine.builtin.dream import DreamHandler

    handler = DreamHandler()

    assert DreamHandler._organizer_expired(handler, {}, now=_NOW) is False


def test_dream_organizer_uses_the_shared_time_limit_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """期限的来源必须是 clonoth_runtime 那一个函数，不能在 dream 里再读一次环境变量。"""
    from engine.builtin import dream as dream_module

    monkeypatch.setattr(dream_module, "task_max_running_seconds", lambda: 300.0)
    handler = dream_module.DreamHandler()
    pending = {"dream_created_at": _NOW.isoformat()}
    deadline = 300 + 2 * TASK_REAP_INTERVAL_SECONDS

    assert handler._organizer_expired(pending, now=_NOW + timedelta(seconds=deadline)) is False
    assert handler._organizer_expired(pending, now=_NOW + timedelta(seconds=deadline + 1)) is True


def test_reap_interval_and_default_limit_are_module_constants() -> None:
    assert clonoth_runtime.DEFAULT_TASK_MAX_RUNNING_SECONDS == 3600.0
    assert clonoth_runtime.TASK_REAP_INTERVAL_SECONDS == 60.0
