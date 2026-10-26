"""测试里替 supervisor 记文件认领和读戳：记账用真的 FileLedger，任务的状态由测试自己摆。

同一个工作区（tmp_path）里的上下文共用一份，和真的 supervisor 一样跨任务、跨会话、跨 worker；时间用自增的计数，先后分得清。"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.file_claims import FileLedger, TaskView  # noqa: E402
from toolbox import file_state  # noqa: E402

_LIVE = frozenset({"running", "pending", "suspended"})


class SupervisorDown(ConnectionError):
    """连不上 supervisor：真的 ToolContext 这时抛的也是网络错误。"""


class FakeSupervisor:
    _by_workspace: dict[str, "FakeSupervisor"] = {}

    @classmethod
    def of(cls, workspace_root: Path | str) -> "FakeSupervisor":
        key = str(Path(workspace_root).resolve())
        found = cls._by_workspace.get(key)
        if found is None:
            found = cls._by_workspace[key] = cls()
        return found

    def __init__(self) -> None:
        self.ledger = FileLedger()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.journal_roots: dict[str, str] = {}
        self.down = False
        self._clock = itertools.count(1)

    def tick(self) -> float:
        return float(next(self._clock))

    def start(
        self,
        task_id: str,
        *,
        session: str = "",
        node: str = "test.node",
        caller: str = "",
        status: str = "running",
        journal_root: str = "",
    ) -> None:
        self.tasks[task_id] = {
            "status": status, "node": node, "session": session, "caller": caller,
            "created": self.tick(), "ended": None,
        }
        if journal_root:
            self.journal_roots[task_id] = journal_root

    def suspend(self, task_id: str) -> None:
        self.tasks[task_id]["status"] = "suspended"

    def resume(self, task_id: str) -> None:
        self.tasks[task_id]["status"] = "running"

    def end(self, task_id: str, status: str = "completed") -> None:
        """任务到了终态，走和 supervisor 终态钩子同一个入口：放认领、并读戳。"""
        task = self.tasks[task_id]
        task["status"] = status
        task["ended"] = self.tick()
        self.ledger.end_task(task_id, ended_at=task["ended"])

    def view(self, task_id: str) -> TaskView | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        live = task["status"] in _LIVE
        return TaskView(
            task_id=task_id,
            live=live,
            status=task["status"],
            node_id=task["node"],
            session_id=task["session"],
            caller_task_id=task["caller"],
            created_at=task["created"],
            ended_at=None if live else task["ended"],
        )

    def holders(self, path: Path) -> list[str]:
        key = file_state.path_key(path)
        return [holder.task_id for found, holders in self.ledger.claims.paths() if found == key for holder in holders]

    def stamp(self, session: str, task_id: str, path: Path) -> file_state.StampLookup:
        return self.ledger.stamps.lookup(session, task_id, file_state.path_key(path), started_at=None)

    def begin(self, task_id: str, path: str, display: str, session: str) -> dict[str, Any]:
        self._reachable()
        self._ensure(task_id, session)
        return self.ledger.begin(
            task_id=task_id,
            session=session,
            path=path,
            display=display,
            journal_session=self.journal_roots.get(task_id, ""),
            resolve=self.view,
            now=self.tick(),
        )

    def record(self, task_id: str, path: str, session: str, stamp: dict[str, Any], revealable: bool) -> dict[str, Any]:
        self._reachable()
        self._ensure(task_id, session)
        parsed = file_state.stamp_from_wire(stamp)
        assert parsed is not None, stamp
        return self.ledger.record(
            task_id=task_id, session=session, path=path, entry=file_state.Entry(parsed, revealable), now=self.tick(),
        )

    def release(self, task_id: str, path: str) -> dict[str, Any]:
        self._reachable()
        return self.ledger.release(task_id=task_id, path=path)

    def scope(self, task_id: str, session: str) -> dict[str, Any]:
        self._reachable()
        self._ensure(task_id, session)
        return self.ledger.scope(
            task_id, journal_session=self.journal_roots.get(task_id, ""), view=self.view(task_id),
        )

    def _ensure(self, task_id: str, session: str) -> None:
        # 没摆过的任务号按第一次露面时开始、正在跑算
        if task_id and task_id not in self.tasks:
            self.start(task_id, session=session)

    def _reachable(self) -> None:
        if self.down:
            raise SupervisorDown("supervisor unreachable")


class FakeFileEdits:
    """混进测试用的 ToolContext 子类：认领和读戳交给这个工作区的 FakeSupervisor，不走 HTTP。"""

    @property
    def supervisor(self) -> FakeSupervisor:
        return FakeSupervisor.of(self.workspace_root)  # type: ignore[attr-defined]

    async def begin_file_edit(self, path: str, *, display: str, session: str) -> dict[str, Any]:
        return self.supervisor.begin(self.task_id or "", path, display, session)  # type: ignore[attr-defined]

    async def record_file_stamp(
        self, path: str, *, session: str, stamp: dict[str, Any], revealable: bool,
    ) -> dict[str, Any]:
        return self.supervisor.record(self.task_id or "", path, session, stamp, revealable)  # type: ignore[attr-defined]

    async def release_file_claim(self, path: str) -> dict[str, Any]:
        return self.supervisor.release(self.task_id or "", path)  # type: ignore[attr-defined]

    async def file_edit_scope(self) -> dict[str, Any]:
        return self.supervisor.scope(self.task_id or "", file_state.session_key(self))  # type: ignore[attr-defined]
