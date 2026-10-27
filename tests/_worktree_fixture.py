"""隔离副本测试共用的搭建：tmp 下真实的 git 仓库当工作区，supervisor 状态按 engine 的样子收派发和完成回报。

副本建在后台线程里，用 env.wait() 等它建完；每个测试结束时关掉副本服务的线程。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from engine.project_root import PROJECT_ROOT_KEY  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine, _default_policy_dict  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402
from supervisor.worktrees import WorktreeService, service_of  # noqa: E402
from supervisor.worktrees import git as wt_git  # noqa: E402

CONV = "qq_group:3003"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": True}
GITIGNORE = "data/\nconfig/\nnode_modules/\n"
BASE_FILES: dict[str, str] = {
    ".gitignore": GITIGNORE,
    "README.md": "hello\n",
    "app/main.py": "def main():\n    return 1\n",
    "app/old.py": "OLD = True\n",
    "engine/x.py": "X = 1\n",
}

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=str(repo), capture_output=True, check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def make_repo(path: Path, files: dict[str, str | bytes] | None = None, *, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Clonoth Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "commit.gpgsign", "false")
    if not commit:
        return path
    for rel, content in (BASE_FILES if files is None else files).items():
        write(path / rel, content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def branches(repo: Path) -> list[str]:
    # 当前分支前面是 *，在别的 worktree 里检出的分支前面是 +
    return [line.strip().lstrip("*+ ").strip() for line in git(repo, "branch", "--list").splitlines() if line.strip()]


def write_policy(workspace: Path, **overrides: Any) -> None:
    """在建 PolicyEngine 之前写好实例的策略：默认策略上改几项。"""
    data = _default_policy_dict()
    data.update(overrides)
    path = config_layout.policy_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def write_runtime(workspace: Path, data: dict[str, Any]) -> None:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def make_state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-worktrees"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def close_service(state: SupervisorState) -> None:
    service = getattr(state, "worktrees", None)
    if isinstance(service, WorktreeService):
        service.close()


def item(target: str = "a.node", **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "target": target, "instruction": f"do {target}", "context_mode": "fresh", **extra}


def tool_item(name: str = "read_file", **extra: Any) -> dict[str, Any]:
    return {"kind": "tool", "target": name, "arguments": {"files": [{"path": "README.md"}]}, **extra}


@dataclass
class Env:
    tmp: Path
    ws: Path
    state: SupervisorState
    extra_states: list[SupervisorState] = field(default_factory=list)

    @property
    def service(self) -> WorktreeService:
        return service_of(self.state)

    @property
    def root(self) -> Path:
        return (self.tmp / f"{self.ws.name}-worktrees").resolve()

    def wait(self, timeout: float = 120.0) -> None:
        assert self.service.wait_idle(timeout), "建或清理隔离副本超时"

    def entry(self, task_or_id: Task | str) -> dict[str, Any] | None:
        worktree_id = task_or_id.task_id if isinstance(task_or_id, Task) else str(task_or_id)
        return self.service.registry.get(worktree_id)

    def session(self, key: str = CONV, *, channel: str = "qq") -> str:
        return self.state.get_or_create_session(channel=channel, conversation_key=key)

    def caller(
        self,
        session_id: str,
        *,
        key: str = CONV,
        node: str = "test.orchestrator",
        channel: str = "qq",
        auth: dict[str, Any] | None = None,
    ) -> Task:
        """不在入口分支上的调用方：路由当场跑完，不另起线程。"""
        context = {"conversation_key": key, "channel": channel, "platform_auth": dict(AUTH if auth is None else auth)}
        with self.state._lock:
            return self.state._create_task_locked(
                session_id=session_id,
                session_generation=1,
                kind=TaskKind.node,
                node_id=node,
                input_data={"task_context": context},
                continuation={},
                source_inbound_seq=None,
            )

    def inbound(self, session_id: str, *, node: str = "test.orchestrator", text: str = "", **extra: Any) -> Task:
        payload = {
            "channel": "qq",
            "conversation_key": self.state.sessions[session_id].conversation_key,
            "text": text or f"run {node}",
            "entry_node_id": node,
            "platform_auth": dict(AUTH),
            **extra,
        }
        event = self.state.eventlog.append(
            session_id=session_id, component="shell", type_="inbound_message", payload=payload,
        )
        task = self.state.record_inbound_message_event(event)
        assert task is not None
        return task

    def async_child(self, session_id: str, caller: Task, *, node: str = "a.node", **origin: Any) -> Task:
        """照 engine 异步委派那样投一条带 dispatch_origin 的入站，子节点在自己的 agent: 会话里跑。"""
        agent_sid = self.state.get_or_create_session(
            channel="qq", conversation_key=f"agent:{node}:{CONV}:{uuid.uuid4()}",
        )
        dispatch_origin = {
            "parent_session_id": session_id, "caller_node_id": str(caller.node_id or ""),
            "parent_conversation_key": CONV, "context_mode": "fresh", "caller_task_id": caller.task_id,
            **origin,
        }
        return self.inbound(
            agent_sid, node=node, dispatch_context_mode="fresh", dispatch_origin=dispatch_origin,
        )

    def report(self, task: Task, result: dict[str, Any]) -> None:
        task.status = TaskStatus.running
        task.worker_id = "worker-1"
        task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.state.complete_task(task_id=task.task_id, worker_id="worker-1", result=result)

    def finish(self, task: Task, text: str = "done") -> None:
        self.report(task, {"action": "finish", "result": {"text": text}})

    def fail(self, task: Task, error: str = "broke") -> None:
        self.report(task, {"action": "fail", "error": error})

    def wait_batch(self, caller: Task, *items: dict[str, Any]) -> list[Task]:
        self.report(caller, {"action": "dispatch", "dispatch_batch": list(items)})
        return self.children(caller)

    def dispatch_single(self, caller: Task, target: str = "a.node", **dispatch_input: Any) -> Task:
        self.report(caller, {
            "action": "dispatch", "target_node": target,
            "dispatch_input": {"instruction": f"do {target}", **dispatch_input},
        })
        [child] = self.children(caller)
        return child

    def children(self, caller: Task) -> list[Task]:
        return sorted(
            (task for task in self.state.tasks.values() if task.caller_task_id == caller.task_id),
            key=lambda task: (task.batch_index if task.batch_index is not None else -1, task.created_at),
        )

    def callbacks(self) -> list[Task]:
        return [task for task in self.state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]

    def isolated_child(self, caller: Task, **extra: Any) -> Task:
        """派一个要求隔离的批次项并等副本建好。"""
        [child] = self.wait_batch(caller, item(isolation="worktree", **extra))
        self.wait()
        assert self.entry(child)["state"] == "ready", self.entry(child)
        return child

    def worktree_of(self, task: Task) -> Path:
        return Path(task.input[PROJECT_ROOT_KEY])

    def extra_state(self) -> SupervisorState:
        """同一个工作区上再起一个 supervisor 状态（重启之后）。"""
        state = make_state(self.ws)
        self.extra_states.append(state)
        return state


def entries(task: Task) -> list[dict[str, Any]]:
    return task.input["resume_data"]["entries"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    for name in (config_layout.INSTANCES_FILE_ENV, "CLONOTH_URL_PREFIX", "CLONOTH_TASK_MAX_RUNNING_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    ws = make_repo(tmp_path / "ws")
    state = make_state(ws)
    environment = Env(tmp_path, ws, state)
    try:
        yield environment
    finally:
        for extra in environment.extra_states:
            close_service(extra)
        close_service(state)


__all__ = [
    "AUTH", "BASE_FILES", "CONV", "Env", "GITIGNORE", "branches", "close_service", "entries", "env", "git", "head",
    "item", "make_repo", "make_state", "requires_git", "tool_item", "write", "write_policy", "write_runtime", "wt_git",
]
