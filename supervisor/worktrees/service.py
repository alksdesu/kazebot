"""隔离副本的生命周期：派发时登记、锁外建 worktree、任务收尾算 diff、合回或放弃、到期和孤儿的清理。

git 命令和删目录都不在 state._lock 里做：要在锁里定的事（任务状态、登记项状态）先定下来，锁外跑完再回锁里收尾。
"""
from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config_layout
from engine.dispatch_batch import ISOLATION_WORKTREE, dispatch_isolation, dispatch_repo
from engine.dispatch_handle import parse_handle
from engine.project_root import (
    ISOLATION_CONTEXT_KEY,
    PROJECT_ORIGIN_KEY,
    PROJECT_ROOT_KEY,
    WORKTREE_KEY,
)
from toolbox import edit_journal, file_state
from toolbox._common import ORIGIN_ROOT_ENV, ORIGIN_VENV_ENV
from toolbox.text_files import replace_bytes

from .. import child_retention
from ..file_claims import _journal_session_locked, _live_locked, _resolver_locked, ledger_of
from ..instances import load_instances, url_prefix
from ..policy import PathScope
from ..types import Task, TaskStatus
from . import git
from .keys import (
    WORKTREE_OUTCOME_KEY,
    WORKTREE_OWNER_KEY,
    WORKTREE_PENDING_KEY,
)
from .registry import (
    ABANDONED,
    CLEANUP_STATES,
    CREATING,
    EXPIRED,
    FAILED,
    FINISHED,
    LIVE_STATES,
    MERGED,
    MERGING,
    READY,
    REMOVED,
    REUSABLE_STATES,
    WorktreeRegistry,
    now_iso,
    parse_time,
    registry_path,
)
from .settings import WorktreeSettings

log = logging.getLogger(__name__)

# 这几种结局收尾时算 diff；取消按放弃处理
DIFF_ACTIONS = frozenset({"finish", "ask", "fail", "preempted"})
# 任务真的结束了的结局，和 task_router 的 _TASK_END_ACTIONS 一致
END_ACTIONS = DIFF_ACTIONS | {"cancelled"}
_SWEEP_SEC = 30.0
_THREAD_JOIN_SEC = 5.0
_STOP_PROCESSES_WAIT_SEC = 15.0
# 结果里最多带这么多个文件的明细，登记里也一样
_OUTCOME_FILES = 200
# 清理连着失败这么多次就不再试，登记里留着原因等人处理
_MAX_CLEANUP_ATTEMPTS = 8
MERGE_JOURNAL_PREFIX = "merge-"

REASON_MERGED = "merged"
REASON_ABANDONED = "abandoned"
REASON_CANCELLED = "cancelled"
REASON_EXPIRED = "expired"
REASON_ORPHAN = "orphan"
REASON_FAILED = "failed"
# 清理前先把还没合回的改动存成补丁，留到登记过期
_KEEP_PATCH_REASONS = frozenset({REASON_CANCELLED, REASON_EXPIRED, REASON_ORPHAN})
_STATE_LABELS = {
    CREATING: "还在建",
    READY: "子任务还在用",
    FINISHED: "等合回",
    MERGING: "正在合回",
    MERGED: "已经合回",
    ABANDONED: "已经放弃",
    EXPIRED: "过了保留期",
    FAILED: "没建成",
    REMOVED: "已经清理",
}


class ProvisionError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolved(path: Path | str) -> Path:
    candidate = Path(path)
    try:
        return candidate.resolve()
    except (OSError, RuntimeError):
        return candidate.absolute()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _posix(path: Path | str | None) -> str:
    return Path(path).as_posix() if path else ""


@dataclass(frozen=True)
class IsolationPlan:
    """一个要派出去的子任务落在哪：不隔离、继承调用方的副本、接着用一个已有的副本、新建一个，或者当场拒绝。"""

    kind: str = ""
    worktree: str = ""
    keys: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    repo: str = ""
    origin_dir: str = ""
    requested_by: str = ""
    previous: str = ""
    previous_state: str = ""
    error: str = ""

    @property
    def refused(self) -> bool:
        return self.kind == "refuse"


NO_ISOLATION = IsolationPlan()


def service_of(state: Any) -> "WorktreeService":
    """state 上的副本服务；测试替身之类没走 register_features 的状态第一次用到时建一个。"""
    service = getattr(state, "worktrees", None)
    if isinstance(service, WorktreeService):
        return service
    with state._lock:
        service = getattr(state, "worktrees", None)
        if not isinstance(service, WorktreeService):
            service = WorktreeService(state)
            state.worktrees = service
        return service


class WorktreeService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.workspace_root = Path(state.workspace_root)
        self.registry = WorktreeRegistry(registry_path(self.workspace_root))
        self._jobs: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._provisioning: set[str] = set()
        self._merging: set[str] = set()
        self._cleaning: set[str] = set()
        self._busy = 0
        self._guard = threading.Condition()
        self._repo_locks: dict[str, threading.Lock] = {}
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._provisioner: threading.Thread | None = None
        self._housekeeper: threading.Thread | None = None
        self._recovered = False

    def start(self) -> None:
        """supervisor 启动时调：先清点登记（孤儿、合回到一半的、过期的），再起后台线程。"""
        if not self._recovered:
            self._recovered = True
            try:
                self.recover()
            except Exception:
                log.exception("checking the worktree registry at startup failed")
        self._halt.clear()
        self._ensure_threads(housekeeping=True)
        self._wake.set()

    def close(self) -> None:
        self._halt.set()
        self._wake.set()
        with self._guard:
            threads = [thread for thread in (self._provisioner, self._housekeeper) if thread is not None]
            self._provisioner = None
            self._housekeeper = None
        for thread in threads:
            thread.join(timeout=_THREAD_JOIN_SEC)

    stop = close

    def _ensure_threads(self, *, housekeeping: bool = False) -> None:
        with self._guard:
            if self._halt.is_set():
                return
            if self._provisioner is None or not self._provisioner.is_alive():
                self._provisioner = threading.Thread(target=self._provision_loop, name="worktree-provisioner", daemon=True)
                self._provisioner.start()
            if housekeeping and (self._housekeeper is None or not self._housekeeper.is_alive()):
                self._housekeeper = threading.Thread(target=self._housekeeping_loop, name="worktree-housekeeper", daemon=True)
                self._housekeeper.start()

    def _provision_loop(self) -> None:
        while not self._halt.is_set():
            try:
                worktree_id = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            # 出队就记成在建：清点线程看到 creating 而它不在任何一处时会重新排队，中间不能有空档
            with self._guard:
                self._queued.discard(worktree_id)
                self._provisioning.add(worktree_id)
                self._busy += 1
            try:
                self.provision(worktree_id)
            except Exception as exc:
                log.exception("provisioning worktree %s failed unexpectedly", worktree_id)
                # 不按建失败收场的话，清点线程会一直把它重新排上
                with contextlib.suppress(Exception):
                    self._provision_failed(worktree_id, f"建副本时出了意外（{type(exc).__name__}: {exc}）")
            finally:
                with self._guard:
                    self._provisioning.discard(worktree_id)
                    self._busy -= 1
                    self._guard.notify_all()
                self._wake.set()

    def _housekeeping_loop(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(_SWEEP_SEC)
            self._wake.clear()
            if self._halt.is_set():
                break
            with self._guard:
                self._busy += 1
            try:
                self.housekeeping()
            except Exception:
                log.exception("worktree housekeeping failed")
            finally:
                with self._guard:
                    self._busy -= 1
                    self._guard.notify_all()

    def wake(self) -> None:
        self._wake.set()

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """等排着的建副本任务都做完、手头的清理也做完；到点没完返回 False。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            while self._queued or self._busy or not self._jobs.empty():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(min(remaining, 0.1))
        return True

    def _enqueue(self, worktree_id: str) -> None:
        with self._guard:
            if worktree_id in self._queued:
                return
            self._queued.add(worktree_id)
        self._jobs.put(worktree_id)
        self._ensure_threads()

    def _repo_lock(self, top: Path) -> threading.Lock:
        key = os.path.normcase(str(top))
        with self._guard:
            lock = self._repo_locks.get(key)
            if lock is None:
                lock = self._repo_locks[key] = threading.Lock()
            return lock

    @contextlib.contextmanager
    def _patch_lock(self, entry: Mapping[str, Any]) -> Iterator[None]:
        """算、读、套一个副本的补丁时持着原仓库的锁；不能在 state._lock 里拿。"""
        repo = str(entry.get("repo") or "").strip()
        if not repo:
            yield
            return
        # 几个副本同时 git add 会往同一个对象库写同样的对象，Windows 上会撞；补丁文件也不能在读、套的时候被另一次计算换掉
        with self._repo_lock(Path(repo)):
            yield

    def settings(self) -> WorktreeSettings:
        return WorktreeSettings.load(self.workspace_root)

    def _live_locked(self, task: Task) -> bool:
        return _live_locked(self.state, task, _utcnow())

    def _users_locked(self, worktree_id: str) -> list[Task]:
        """还在用这个副本的任务：继承它的、接着用它的，以及还在等它建好的那个子任务。"""
        users: list[Task] = []
        for task in self.state.tasks.values():
            uses = task.input.get(WORKTREE_KEY) == worktree_id or (
                task.task_id == worktree_id and isinstance(task.input.get(WORKTREE_PENDING_KEY), dict)
            )
            if uses and self._live_locked(task):
                users.append(task)
        return users

    def _live_owner_locked(self, entry: Mapping[str, Any], *, besides: str = "") -> Task | None:
        for task_id in entry.get("owners", ()):
            if task_id == besides:
                continue
            task = self.state.tasks.get(task_id)
            if task is not None and self._live_locked(task):
                return task
        return None

    def caller_record_locked(self, task: Task | None) -> dict[str, Any] | None:
        """task 在哪个副本里、是不是它的主人；不在副本里给 None。"""
        if task is None:
            return None
        worktree_id = str(task.input.get(WORKTREE_KEY) or "").strip()
        if not worktree_id:
            return None
        return {"worktree": worktree_id, "owner": bool(task.input.get(WORKTREE_OWNER_KEY))}

    def _origin_dir_locked(self, caller: Task | None) -> str:
        """新副本里项目根放在哪个位置：调用方在原仓库里的位置，没有调用方或它不在副本里就是工作区根。"""
        if caller is not None:
            origin = caller.input.get(PROJECT_ORIGIN_KEY)
            if isinstance(origin, str) and origin.strip() and caller.input.get(WORKTREE_KEY):
                return origin.strip()
        return str(self.workspace_root)

    def plan_child_locked(
        self,
        *,
        caller: Task | None,
        request: Mapping[str, Any] | None = None,
        continued_task_id: str = "",
        source: Mapping[str, Any] | None = None,
        requested_by: str = "",
    ) -> IsolationPlan:
        """持锁调用。request 是派发项（isolation、repo 两个字段），continued_task_id 是 continue_from 接着的任务，
        source 是结果回到原会话、自动续跑时要沿用的副本（{"worktree", "owner"}）。"""
        wanted = dispatch_isolation(dict(request) if isinstance(request, Mapping) else None) == ISOLATION_WORKTREE
        repo_text = dispatch_repo(dict(request) if isinstance(request, Mapping) else None)
        dispatcher = requested_by or (caller.task_id if caller is not None else "")
        origin_dir = self._origin_dir_locked(caller)
        caller_worktree = str(caller.input.get(WORKTREE_KEY) or "").strip() if caller is not None else ""
        if wanted and caller_worktree:
            # 嵌套的副本从原仓库的 HEAD 起步、合回原仓库，绕开了外层副本收尾时的审
            return IsolationPlan(
                "refuse",
                worktree=caller_worktree,
                error=f"派发方已经在隔离副本 {caller_worktree} 里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派",
            )
        if wanted and repo_text:
            refusal = self._repo_refusal(repo_text)
            if refusal:
                return IsolationPlan("refuse", repo=repo_text, error=refusal)
        if continued_task_id:
            entry = self.registry.owned_by(continued_task_id)
            if entry is not None:
                if wanted and repo_text and not self._same_repo(repo_text, entry):
                    return IsolationPlan("create", repo=repo_text, origin_dir=origin_dir, requested_by=dispatcher)
                return self._reuse_or_recreate_locked(entry, dispatcher=dispatcher, keep_dispatcher=False)
        if wanted:
            return IsolationPlan("create", repo=repo_text, origin_dir=origin_dir, requested_by=dispatcher)
        if isinstance(source, Mapping) and str(source.get("worktree") or "").strip():
            entry = self.registry.get(str(source["worktree"]).strip())
            if entry is None:
                log.warning("worktree %s named for a continuation is not registered; running without it", source.get("worktree"))
                return NO_ISOLATION
            if source.get("owner"):
                return self._reuse_or_recreate_locked(
                    entry, dispatcher=dispatcher, keep_dispatcher=True, share_when_busy=True,
                )
            if entry.get("state") in REUSABLE_STATES and Path(str(entry.get("path") or "")).is_dir():
                return self._inherit_entry_plan(entry)
            return self._recreate_plan(entry, dispatcher=dispatcher)
        if caller is not None and caller.input.get(WORKTREE_KEY):
            return self._inherit_caller_plan(caller)
        return NO_ISOLATION

    def _repo_refusal(self, repo_text: str) -> str:
        """派发时就能看出不行的 repo：目录不存在，或者既不在工作区所在的仓库里、也不在 extra_roots 下。

        只看路径，不跑 git；拿不准的放行，建副本时按 git 认出的仓库根再核一遍。
        """
        candidate = Path(repo_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        candidate = _resolved(candidate)
        if not candidate.is_dir():
            return f"repo 指的目录不存在：{repo_text}"
        homes = [_resolved(self.workspace_root)]
        if self._has_code_links():
            homes.append(_resolved(config_layout.CODE_ROOT))
        # 工作区可能在仓库的子目录里：repo 写成仓库根也算
        if any(_inside(candidate, home) or _inside(home, candidate) for home in homes):
            return ""
        if any(_inside(candidate, root) for root in self._extra_roots()):
            return ""
        return f"repo {repo_text} 不在工作区所在的仓库里，也不在策略的 extra_roots 下，不能为它建隔离副本"

    def _same_repo(self, repo_text: str, entry: Mapping[str, Any]) -> bool:
        candidate = Path(repo_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        top = str(entry.get("repo") or "")
        return bool(top) and _inside(_resolved(candidate), _resolved(top))

    def _reuse_or_recreate_locked(
        self, entry: Mapping[str, Any], *, dispatcher: str, keep_dispatcher: bool, share_when_busy: bool = False,
    ) -> IsolationPlan:
        worktree_id = str(entry["id"])
        owner = self._live_owner_locked(entry)
        if entry.get("state") in REUSABLE_STATES and owner is not None:
            if share_when_busy and Path(str(entry.get("path") or "")).is_dir():
                # 结果回到原会话时主人还在做：这一轮跟着在同一个副本里做，收尾的 diff 仍由主人算
                return self._inherit_entry_plan(entry)
            return IsolationPlan(
                "refuse",
                worktree=worktree_id,
                error=f"隔离副本 {worktree_id} 正被任务 {owner.task_id}（{owner.node_id or '?'}）用着，不能同时再接着用它",
            )
        usable = (
            entry.get("state") in REUSABLE_STATES
            and worktree_id not in self._merging
            and Path(str(entry.get("path") or "")).is_dir()
        )
        if not usable:
            return self._recreate_plan(entry, dispatcher=dispatcher)
        root, origin = self._entry_roots(entry)
        context = self._context(entry, root, origin, inherited=False, reused=True)
        return IsolationPlan(
            "reuse",
            worktree=worktree_id,
            keys={
                PROJECT_ROOT_KEY: str(root),
                PROJECT_ORIGIN_KEY: str(origin),
                WORKTREE_KEY: worktree_id,
                WORKTREE_OWNER_KEY: True,
            },
            context=context,
            requested_by="" if keep_dispatcher else dispatcher,
        )

    def _recreate_plan(self, entry: Mapping[str, Any], *, dispatcher: str) -> IsolationPlan:
        """原来的副本已经合回、放弃或清理掉了：基于原仓库现在的 HEAD 另建一个。"""
        repo = str(entry.get("repo") or entry.get("repo_request") or "")
        origin_dir = str(entry.get("origin_dir") or "") or str(self.workspace_root)
        return IsolationPlan(
            "create",
            repo=repo,
            origin_dir=origin_dir,
            requested_by=dispatcher,
            previous=str(entry.get("id") or ""),
            previous_state=str(entry.get("state") or ""),
        )

    def _inherit_entry_plan(self, entry: Mapping[str, Any]) -> IsolationPlan:
        root, origin = self._entry_roots(entry)
        return IsolationPlan(
            "inherit",
            worktree=str(entry["id"]),
            keys={PROJECT_ROOT_KEY: str(root), PROJECT_ORIGIN_KEY: str(origin), WORKTREE_KEY: str(entry["id"])},
            context=self._context(entry, root, origin, inherited=True, reused=False),
        )

    @staticmethod
    def _inherit_caller_plan(caller: Task) -> IsolationPlan:
        keys = {
            key: caller.input[key]
            for key in (PROJECT_ROOT_KEY, PROJECT_ORIGIN_KEY, WORKTREE_KEY)
            if key in caller.input
        }
        raw_context = caller.input.get("task_context")
        caller_context = raw_context.get(ISOLATION_CONTEXT_KEY) if isinstance(raw_context, dict) else None
        context = dict(caller_context) if isinstance(caller_context, dict) else {
            "mode": ISOLATION_WORKTREE, "worktree": keys.get(WORKTREE_KEY, ""),
        }
        context.update({"inherited": True, "reused": False})
        return IsolationPlan("inherit", worktree=str(keys.get(WORKTREE_KEY) or ""), keys=keys, context=context)

    @staticmethod
    def _entry_roots(entry: Mapping[str, Any]) -> tuple[Path, Path]:
        rel = str(entry.get("project_rel") or "").strip("/")
        root = Path(str(entry["path"]))
        origin = Path(str(entry["repo"]))
        if rel and rel != ".":
            return root / rel, origin / rel
        return root, origin

    @staticmethod
    def _context(
        entry: Mapping[str, Any], root: Path, origin: Path, *, inherited: bool, reused: bool,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "mode": ISOLATION_WORKTREE,
            "worktree": str(entry.get("id") or ""),
            "path": root.as_posix(),
            "worktree_root": _posix(entry.get("path")),
            "origin": origin.as_posix(),
            "repo": _posix(entry.get("repo")),
            "branch": str(entry.get("branch") or ""),
            "base": str(entry.get("base") or ""),
            "workspace_repo": bool(entry.get("workspace_repo")),
            "inherited": inherited,
            "reused": reused,
            "env": {"origin_root": ORIGIN_ROOT_ENV, "origin_venv": ORIGIN_VENV_ENV},
        }
        links = entry.get("links")
        if isinstance(links, list) and links:
            context["links"] = list(links)
        previous = str(entry.get("previous") or "")
        if previous:
            context["previous"] = {"worktree": previous, "state": str(entry.get("previous_state") or "")}
        return context

    @staticmethod
    def apply_plan(child_input: dict[str, Any], plan: IsolationPlan) -> None:
        """建子任务之前把计划写进它的 input。"""
        if plan.kind in ("inherit", "reuse"):
            child_input.update(plan.keys)
            context = child_input.get("task_context")
            if not isinstance(context, dict):
                context = {}
                child_input["task_context"] = context
            context[ISOLATION_CONTEXT_KEY] = dict(plan.context)
        elif plan.kind == "create":
            child_input[WORKTREE_PENDING_KEY] = {
                "repo": plan.repo,
                "origin_dir": plan.origin_dir,
                "requested_by": plan.requested_by,
                "previous": plan.previous,
                "previous_state": plan.previous_state,
            }

    def register_child_locked(self, child: Task, plan: IsolationPlan) -> None:
        """子任务建出来之后：要新建的登记成 creating 交给后台去建，接着用的把它记成主人。"""
        if plan.kind == "create":
            self.registry.put({
                "id": child.task_id,
                "state": CREATING,
                "owners": [child.task_id],
                "repo_request": plan.repo,
                "origin_dir": plan.origin_dir,
                "requested_by": plan.requested_by,
                "node_id": str(child.node_id or child.tool_name or ""),
                "family_root": str(self.state._task_family_root_locked(child) or ""),
                "previous": plan.previous,
                "previous_state": plan.previous_state,
                "instance": self._instance_id(),
            })
            self._enqueue(child.task_id)
            return
        if plan.kind == "reuse":
            fields: dict[str, Any] = {"owners": [child.task_id], "until": None, "finished_at": None}
            if plan.requested_by:
                fields["requested_by"] = plan.requested_by
            self.registry.transition(plan.worktree, REUSABLE_STATES, READY, **fields)

    def provision(self, worktree_id: str) -> None:
        state = self.state
        with state._lock:
            entry = self.registry.get(worktree_id)
            if entry is None or entry.get("state") != CREATING:
                return
            task = state.tasks.get(worktree_id)
            if task is None or not self._live_locked(task) or task.cancel_requested:
                self.registry.transition(
                    worktree_id, {CREATING}, ABANDONED,
                    cleanup_reason=REASON_CANCELLED, note="子任务在副本建好之前就结束了",
                )
                self._wake.set()
                return
            spec = task.input.get(WORKTREE_PENDING_KEY)
            spec = dict(spec) if isinstance(spec, dict) else {}
        self._provision_outside_lock(worktree_id, spec)

    def _provision_outside_lock(self, worktree_id: str, spec: dict[str, Any]) -> None:
        settings = self.settings()
        try:
            if settings.root_problem:
                raise ProvisionError(f"隔离副本的存放目录配置不对：{settings.root_problem}")
            top, workspace_repo = self._resolve_repo(str(spec.get("repo") or ""), settings)
            key = git.repo_key(top)
            path = settings.root / key / worktree_id
            branch = git.branch_name(worktree_id)
            origin_dir = _resolved(str(spec.get("origin_dir") or self.workspace_root))
            rel = origin_dir.relative_to(top).as_posix() if _inside(origin_dir, top) and origin_dir != top else ""
            with self._repo_lock(top):
                base = git.head_commit(top)
                # 先登记位置再动 git：建到一半 supervisor 退出，重启后照登记清掉
                self.registry.update(
                    worktree_id,
                    repo=top.as_posix(), repo_key=key, workspace_repo=workspace_repo,
                    path=path.as_posix(), patch=path.with_name(f"{worktree_id}.patch").as_posix(),
                    branch=branch, base=base, root=settings.root.as_posix(),
                )
                git.add_worktree(top, path, branch, base)
                links = self._make_links(top, path, settings.link_dirs)
            project_rel = rel if rel and (path / rel).is_dir() else ""
        except (ProvisionError, git.GitError, OSError, ValueError) as exc:
            message = exc.message if isinstance(exc, git.GitError) else str(exc)
            self._provision_failed(worktree_id, message)
            return
        self._finalize(worktree_id, links=links, project_rel=project_rel)

    def _resolve_repo(self, repo_text: str, settings: WorktreeSettings) -> tuple[Path, bool]:
        """派发项的 repo 落到哪个仓库根，以及它是不是工作区所在的仓库。只认工作区所在仓库和 extra_roots 下的仓库。"""
        workspace_top = self.workspace_repo()
        if not repo_text:
            if workspace_top is None:
                raise ProvisionError("工作区不在 git 仓库里，没法建隔离副本")
            return workspace_top, True
        candidate = Path(repo_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        candidate = _resolved(candidate)
        if not candidate.exists():
            raise ProvisionError(f"repo 指的目录不存在：{repo_text}")
        top = git.toplevel(candidate)
        if top is None:
            raise ProvisionError(f"repo 指的目录不在 git 仓库里：{repo_text}")
        if _inside(top, settings.root):
            raise ProvisionError(f"repo 指的是一个隔离副本（{top.as_posix()}），不能拿副本再建副本")
        if workspace_top is not None and top == workspace_top:
            return top, True
        for root in self._extra_roots():
            if _inside(top, root):
                return top, False
        raise ProvisionError(
            f"repo {repo_text} 所在的仓库 {top.as_posix()} 不是工作区所在的仓库，也不在策略的 extra_roots 下，不能为它建隔离副本"
        )

    def _extra_roots(self) -> list[Path]:
        policy = getattr(self.state, "policy", None)
        reader = getattr(policy, "extra_roots", None)
        if callable(reader):
            try:
                return [_resolved(root) for root in reader()]
            except Exception:
                log.warning("reading extra_roots from the policy failed", exc_info=True)
        return []

    def workspace_repo(self) -> Path | None:
        """工作区所在的仓库根。多开实例的代码目录是指向共享代码的链接，那就是共享代码所在的仓库。"""
        start = config_layout.CODE_ROOT if self._has_code_links() else self.workspace_root
        return git.toplevel(start)

    def _has_code_links(self) -> bool:
        for name in config_layout.CODE_LINKS:
            link = self.workspace_root / name
            if not config_layout.is_dir_link(link):
                continue
            with contextlib.suppress(OSError, RuntimeError):
                if link.resolve(strict=True) == (config_layout.CODE_ROOT / name).resolve():
                    return True
        return False

    @staticmethod
    def _make_links(top: Path, path: Path, names: tuple[str, ...]) -> list[str]:
        made: list[str] = []
        for name in names:
            source = top / name
            link = path / name
            try:
                if not source.is_dir() or os.path.lexists(link):
                    continue
                link.parent.mkdir(parents=True, exist_ok=True)
                git.make_dir_link(link, source.resolve())
                made.append(name)
            except OSError as exc:
                log.warning("linking %s into worktree %s failed: %s", name, path, exc)
        return made

    def _provision_failed(self, worktree_id: str, message: str) -> None:
        log.warning("worktree %s could not be created: %s", worktree_id, message)
        with self.state._lock:
            self.registry.transition(
                worktree_id, {CREATING}, FAILED, error=message, cleanup_reason=REASON_FAILED,
            )
            task = self.state.tasks.get(worktree_id)
            if task is None or not isinstance(task.input.get(WORKTREE_PENDING_KEY), dict):
                return
            task.input[WORKTREE_PENDING_KEY] = None
            if self.state._task_terminal(task):
                return
            try:
                self.state._fail_task_locked(task, error=f"建隔离副本失败：{message}")
            except Exception:
                log.exception("failing task %s after its worktree could not be created failed", worktree_id)

    def _finalize(self, worktree_id: str, *, links: list[str], project_rel: str) -> None:
        state = self.state
        with state._lock:
            entry = self.registry.update(worktree_id, links=links, project_rel=project_rel)
            task = state.tasks.get(worktree_id)
            if entry is None or entry.get("state") != CREATING:
                return
            if task is None or not self._live_locked(task) or task.cancel_requested:
                self.registry.transition(
                    worktree_id, {CREATING}, ABANDONED,
                    cleanup_reason=REASON_CANCELLED, note="子任务在副本建好之前就结束了",
                )
                if task is not None and isinstance(task.input.get(WORKTREE_PENDING_KEY), dict):
                    task.input[WORKTREE_PENDING_KEY] = None
                self._wake.set()
                return
            root, origin = self._entry_roots(entry)
            task.input[PROJECT_ROOT_KEY] = str(root)
            task.input[PROJECT_ORIGIN_KEY] = str(origin)
            task.input[WORKTREE_KEY] = worktree_id
            task.input[WORKTREE_OWNER_KEY] = True
            task.input[WORKTREE_PENDING_KEY] = None
            context = task.input.get("task_context")
            if not isinstance(context, dict):
                context = {}
                task.input["task_context"] = context
            context[ISOLATION_CONTEXT_KEY] = self._context(entry, root, origin, inherited=False, reused=False)
            task.updated_at = _utcnow()
            self.registry.transition(worktree_id, {CREATING}, READY, ready_at=now_iso())
            try:
                state._event_task_snapshot("task_worktree_ready", task)
            except Exception:
                log.exception("persisting the worktree of task %s failed", worktree_id)
        log.info("worktree %s ready at %s (base %s)", worktree_id, entry.get("path"), str(entry.get("base"))[:12])

    def _instance_id(self) -> str:
        """本实例在多开清单里的 id；单实例部署、清单读不出来或找不到自己时给空串。"""
        if config_layout.data_root() is None:
            return ""
        prefix = url_prefix()
        try:
            rows = load_instances(self.workspace_root)
        except Exception:
            log.warning("reading the instance list failed", exc_info=True)
            return ""
        return next((str(row["id"]) for row in rows if row.get("path") == prefix), "")

    def merge_allowed(self, entry: Mapping[str, Any], settings: WorktreeSettings | None = None) -> bool:
        """外部仓库只看审批；工作区所在仓库在多开时只许配置里点名的实例合回。"""
        if not entry.get("workspace_repo"):
            return True
        if config_layout.data_root() is None:
            return True
        instance = self._instance_id()
        return bool(instance) and instance in (settings or self.settings()).merge_instances

    def needs_outcome_locked(self, task: Task) -> bool:
        if not task.input.get(WORKTREE_OWNER_KEY) or task.input.get(WORKTREE_OUTCOME_KEY):
            return False
        if task.status == TaskStatus.cancelled:
            return False
        return str((task.result or {}).get("action") or "").strip() in DIFF_ACTIONS

    def prepare_outcome(self, task: Task) -> None:
        """锁外算好主人任务收尾时的 diff，记到任务上和登记里。不是主人、已经算过、不是收尾的结局都不做。"""
        if not task.input.get(WORKTREE_OWNER_KEY):
            return
        with self.state._lock:
            if not self.needs_outcome_locked(task):
                return
            worktree_id = str(task.input.get(WORKTREE_KEY) or "").strip()
            entry = self.registry.get(worktree_id)
        settings = self.settings()
        if entry is None:
            view = {"id": worktree_id, "state": FINISHED, "error": "登记里没有这个副本了"}
        else:
            view = self._compute_view(entry, settings)
        with self.state._lock:
            if task.input.get(WORKTREE_OUTCOME_KEY):
                return
            task.input[WORKTREE_OUTCOME_KEY] = view
            if entry is not None:
                self._settle_finished_locked(entry, view, ended=task)
                self._announce_locked(task, view)

    def record_outcome_failure_locked(self, task: Task, message: str) -> None:
        """锁外算 diff 时出了意外：留一份写明原因的结果，副本照样转成等合回，合回时会重新算。"""
        if not self.needs_outcome_locked(task):
            return
        worktree_id = str(task.input.get(WORKTREE_KEY) or "").strip()
        entry = self.registry.get(worktree_id)
        view = self._base_view(entry) if entry is not None else {"id": worktree_id, "state": FINISHED}
        view["error"] = f"收尾时算改动出错（{message}）"
        task.input[WORKTREE_OUTCOME_KEY] = view
        if entry is not None:
            self._settle_finished_locked(entry, view, ended=task)

    def _compute_view(self, entry: Mapping[str, Any], settings: WorktreeSettings) -> dict[str, Any]:
        view = self._base_view(entry)
        path = Path(str(entry.get("path") or ""))
        patch = Path(str(entry.get("patch") or "")) if entry.get("patch") else None
        if not path.is_dir() or patch is None:
            view["error"] = "副本目录不在了" if patch is not None else "登记里没有补丁位置"
            return view
        try:
            with self._patch_lock(entry):
                summary = git.compute_diff(path, str(entry.get("base") or ""), patch, excluded=entry.get("links") or ())
                diff, truncated = git.read_head(patch, settings.inline_diff_chars)
        except (git.GitError, OSError) as exc:
            view["error"] = exc.message if isinstance(exc, git.GitError) else str(exc)
            return view
        view.update(self._summary_fields(summary))
        view["diff"] = diff
        view["diff_truncated"] = truncated or (settings.inline_diff_chars <= 0 and summary.patch_bytes > 0)
        view["merge_allowed"] = self.merge_allowed(entry, settings)
        return view

    @staticmethod
    def _summary_fields(summary: git.DiffSummary) -> dict[str, Any]:
        return {
            "files": [item.to_data() for item in summary.files[:_OUTCOME_FILES]],
            "file_count": len(summary.files),
            "added": summary.added,
            "deleted": summary.deleted,
            "patch_bytes": summary.patch_bytes,
            "digest": summary.digest,
        }

    @staticmethod
    def _base_view(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(entry.get("id") or ""),
            "state": FINISHED,
            "repo": _posix(entry.get("repo")),
            "branch": str(entry.get("branch") or ""),
            "base": str(entry.get("base") or ""),
            "path": _posix(entry.get("path")),
            "patch": _posix(entry.get("patch")),
        }

    def _settle_finished_locked(self, entry: Mapping[str, Any], view: Mapping[str, Any], *, ended: Task) -> None:
        worktree_id = str(entry["id"])
        stored = {key: value for key, value in view.items() if key != "diff"}
        finished = None
        if self._live_owner_locked(entry, besides=ended.task_id) is None:
            finished = self.registry.transition(
                worktree_id, {READY}, FINISHED, outcome=stored, finished_at=now_iso(), finished_by=ended.task_id,
            )
        if finished is None:
            self.registry.update(worktree_id, outcome=stored)
        self._wake.set()

    def _announce_locked(self, task: Task, view: Mapping[str, Any]) -> None:
        """在用户的会话里记一条副本收尾的事件：改了几个文件、补丁在哪。"""
        try:
            session_id = str(self.state._route_session_id_for_task_locked(task) or task.session_id)
            self.state.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="worktree_diff_ready",
                payload={
                    "task_id": task.task_id,
                    "node_id": str(task.node_id or ""),
                    "worktree": {key: view.get(key) for key in (
                        "id", "repo", "branch", "base", "path", "patch", "file_count", "added", "deleted", "error",
                    ) if view.get(key) not in (None, "")},
                },
            )
        except Exception:
            log.warning("recording the worktree outcome of task %s failed", task.task_id, exc_info=True)

    def task_ended_locked(self, task: Task) -> None:
        """任务到了终态（含取消）：主人被取消的副本按放弃处理，交给清理；正常收尾的转成等合回。不会抛异常。"""
        try:
            self._task_ended_locked(task)
        except Exception:
            log.exception("settling the worktree of task %s failed", getattr(task, "task_id", "?"))

    def _task_ended_locked(self, task: Task) -> None:
        if not self.state._task_terminal(task):
            return
        action = str((task.result or {}).get("action") or "").strip()
        # 派出子任务那次完成只是过渡，路由随后把它转成挂起，副本还在用
        if task.status == TaskStatus.completed and action not in END_ACTIONS:
            return
        pending = task.input.get(WORKTREE_PENDING_KEY)
        if isinstance(pending, dict):
            task.input[WORKTREE_PENDING_KEY] = None
            self.registry.transition(
                task.task_id, {CREATING}, ABANDONED,
                cleanup_reason=REASON_CANCELLED, note="子任务在副本建好之前就结束了",
            )
            self._wake.set()
            return
        worktree_id = str(task.input.get(WORKTREE_KEY) or "").strip()
        if not worktree_id or not task.input.get(WORKTREE_OWNER_KEY):
            if worktree_id:
                self._wake.set()
            return
        entry = self.registry.get(worktree_id)
        if entry is None:
            return
        cancelled = task.status == TaskStatus.cancelled or str((task.result or {}).get("action") or "") == "cancelled"
        if cancelled:
            if not task.input.get(WORKTREE_OUTCOME_KEY):
                view = self._base_view(entry)
                view.update({"state": ABANDONED, "note": "子任务被取消"})
                task.input[WORKTREE_OUTCOME_KEY] = view
            if self._live_owner_locked(entry, besides=task.task_id) is None:
                self.registry.transition(
                    worktree_id, {READY, FINISHED}, ABANDONED,
                    cleanup_reason=REASON_CANCELLED, note="子任务被取消", abandoned_by=task.task_id,
                )
        elif self._live_owner_locked(entry, besides=task.task_id) is None:
            self.registry.transition(worktree_id, {READY}, FINISHED, finished_at=now_iso(), finished_by=task.task_id)
        self._wake.set()

    def result_fields_locked(self, task: Task) -> dict[str, Any]:
        """交回父节点的结果里附带的副本说明：{"worktree": ...}；不是主人任务给空表。"""
        if not task.input.get(WORKTREE_OWNER_KEY):
            return {}
        view = task.input.get(WORKTREE_OUTCOME_KEY)
        if isinstance(view, dict) and view.get("id"):
            view = dict(view)
        else:
            entry = self.registry.get(str(task.input.get(WORKTREE_KEY) or ""))
            if entry is None:
                return {}
            view = self._base_view(entry)
            view["error"] = "收尾时没有算出改动；合回时会重新算一遍"
        handle = child_retention.continue_fields_locked(self.state, task).get("handle")
        view["handle"] = str(handle or view.get("id") or "")
        return {"worktree": view}

    @staticmethod
    def isolation_keys_locked(task: Task | None) -> dict[str, Any]:
        if task is None:
            return {}
        return {key: task.input.get(key) for key in (PROJECT_ROOT_KEY, PROJECT_ORIGIN_KEY, WORKTREE_KEY) if key in task.input}

    def path_scope(self, keys: Mapping[str, Any]) -> PathScope:
        """按任务 input 里的隔离键判路径的范围；登记对不上时给一个 invalid 的范围，路径操作一律拒绝。"""
        settings = self.settings()
        foreign = None if settings.root_problem else settings.root
        entries = self.registry.entries()
        mappings = tuple(
            (Path(str(entry["path"])), Path(str(entry["repo"])))
            for entry in entries
            if entry.get("path") and entry.get("repo") and entry.get("state") != REMOVED
        )
        worktree_id = str(keys.get(WORKTREE_KEY) or "").strip()
        if not worktree_id:
            return PathScope(foreign_root=foreign, mappings=mappings)
        entry = next((item for item in entries if item.get("id") == worktree_id), None)
        if entry is None or entry.get("state") not in REUSABLE_STATES or not entry.get("path") or not entry.get("repo"):
            state_label = _STATE_LABELS.get(str(entry.get("state")), str(entry.get("state"))) if entry else "没有登记"
            return PathScope(
                worktree=worktree_id, foreign_root=foreign, mappings=mappings,
                invalid=f"这个任务所在的隔离副本 {worktree_id} 用不了（{state_label}），不能再读写文件或跑命令",
            )
        root_text = str(keys.get(PROJECT_ROOT_KEY) or "").strip()
        origin_text = str(keys.get(PROJECT_ORIGIN_KEY) or "").strip()
        worktree_dir = _resolved(str(entry["path"]))
        repo = _resolved(str(entry["repo"]))
        same_place = False
        if root_text and origin_text:
            root = _resolved(root_text)
            origin = _resolved(origin_text)
            try:
                same_place = _inside(root, worktree_dir) and repo / root.relative_to(worktree_dir) == origin
            except ValueError:
                same_place = False
        if not same_place:
            return PathScope(
                worktree=worktree_id, foreign_root=foreign, mappings=mappings,
                invalid=f"这个任务记的项目根和隔离副本 {worktree_id} 的登记对不上，不能再读写文件或跑命令",
            )
        return PathScope(
            project_root=root, project_origin=origin, foreign_root=foreign, origin_repo=repo,
            worktree=worktree_id, mappings=mappings,
        )

    def _caller_locked(self, task_id: str) -> tuple[Task | None, str]:
        task = self.state.tasks.get(str(task_id or "").strip())
        if task is None:
            return None, "supervisor 里找不到发起合回的任务"
        if not self._live_locked(task):
            return None, "发起合回的任务已经结束了"
        return task, ""

    def _resolve_handle_locked(self, task: Task, handle: str) -> tuple[dict[str, Any] | None, str]:
        text = str(handle or "").strip()
        if not text:
            return None, "handle 不能为空：填子任务结果里给的句柄，或者隔离副本的登记号"
        parsed = parse_handle(text)
        entry = self.registry.owned_by(parsed.task_id) if parsed is not None else None
        if entry is None:
            entry = self.registry.get(text) or self.registry.owned_by(text)
        if entry is None:
            return None, f"找不到 {text} 对应的隔离副本：它没有要求隔离，或者登记已经过期清掉了"
        family = str(entry.get("family_root") or "")
        mine = str(self.state._task_family_root_locked(task) or "")
        if family and family != mine:
            return None, f"隔离副本 {entry['id']} 不是这个会话派出去的子任务的，不能动它"
        return entry, ""

    def _merge_target_locked(self, task_id: str, handle: str, *, for_merge: bool) -> tuple[Task | None, dict[str, Any] | None, str]:
        task, problem = self._caller_locked(task_id)
        if task is None:
            return None, None, problem
        if for_merge and task.input.get(WORKTREE_KEY):
            return task, None, "这个任务自己在隔离副本里，不能把别的副本合回原仓库；交给不在副本里的上级去合回"
        entry, problem = self._resolve_handle_locked(task, handle)
        if entry is None:
            return task, None, problem
        state = str(entry.get("state") or "")
        if state != FINISHED:
            label = _STATE_LABELS.get(state, state)
            return task, entry, f"隔离副本 {entry['id']} 现在{label}，不能合回"
        users = self._users_locked(str(entry["id"]))
        if users:
            names = "、".join(f"{user.task_id}（{user.node_id or user.tool_name or '?'}）" for user in users[:5])
            return task, entry, f"还有任务在用隔离副本 {entry['id']}：{names}；等它们结束（或取消它们）再合回"
        return task, entry, ""

    def preview(self, *, task_id: str, handle: str) -> dict[str, Any]:
        """合回前看一眼：重新算一遍 diff，列出要改的原仓库文件和这份 diff 的指纹；审批卡按它列文件。"""
        with self.state._lock:
            task, entry, problem = self._merge_target_locked(task_id, handle, for_merge=True)
        if problem or entry is None:
            return {"ok": False, "reason": "refused", "message": problem}
        settings = self.settings()
        with self._patch_lock(entry):
            summary, failure = self._fresh_diff(entry)
        if summary is None:
            return {"ok": False, "reason": "failed", "message": failure}
        if not self.merge_allowed(entry, settings):
            return {
                "ok": False, "reason": "instance_not_allowed",
                "message": f"本实例不能合回，补丁在 {_posix(entry.get('patch'))}",
                "patch": _posix(entry.get("patch")),
            }
        files, problem = self._merge_files(entry, summary)
        if problem:
            return {"ok": False, "reason": "refused", "message": problem}
        with self.state._lock:
            held = self._claimed_by_others_locked(task, files)
        if held:
            return {"ok": False, "reason": "claimed", "message": held}
        return {"ok": True, **self._merge_listing(entry, summary, files)}

    def _fresh_diff(self, entry: Mapping[str, Any]) -> tuple[git.DiffSummary | None, str]:
        """重新算一遍副本的补丁。调用方持着 _patch_lock。"""
        path = Path(str(entry.get("path") or ""))
        patch = Path(str(entry.get("patch") or ""))
        if not entry.get("path") or not path.is_dir():
            return None, f"隔离副本 {entry.get('id')} 的目录不在了"
        try:
            return git.compute_diff(path, str(entry.get("base") or ""), patch, excluded=entry.get("links") or ()), ""
        except (git.GitError, OSError) as exc:
            return None, f"算隔离副本 {entry.get('id')} 的改动失败：{exc.message if isinstance(exc, git.GitError) else exc}"

    @staticmethod
    def _merge_files(entry: Mapping[str, Any], summary: git.DiffSummary) -> tuple[list[dict[str, Any]], str]:
        repo = Path(str(entry["repo"]))
        if summary.gitlinks:
            return [], f"副本里改了子仓库（gitlink）：{'、'.join(summary.gitlinks[:10])}；这种改动合不回去，要在子仓库里另做"
        odd = [item.path for item in summary.files if "�" in item.path]
        if odd:
            return [], f"这些文件名不是 UTF-8，认不准要改哪个文件：{'、'.join(odd[:10])}"
        files = []
        for item in summary.files:
            target = repo / Path(*item.path.split("/"))
            files.append({**item.to_data(), "target": target.as_posix()})
        return files, ""

    @staticmethod
    def _merge_listing(entry: Mapping[str, Any], summary: git.DiffSummary, files: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "worktree": str(entry["id"]),
            "repo": _posix(entry.get("repo")),
            "branch": str(entry.get("branch") or ""),
            "base": str(entry.get("base") or ""),
            "patch": _posix(entry.get("patch")),
            "digest": summary.digest,
            "file_count": len(files),
            "added": summary.added,
            "deleted": summary.deleted,
            "files": files,
        }

    def _claimed_by_others_locked(self, task: Task, files: list[dict[str, Any]]) -> str:
        ledger = ledger_of(self.state)
        now = _utcnow()
        for item in files:
            key = file_state.path_key(Path(str(item["target"])))
            reply = ledger.begin(
                task_id="", session="", path=key, display=str(item["path"]), journal_session="",
                resolve=_resolver_locked(self.state, now), now=now.timestamp(),
            )
            holder = reply.get("holder") if isinstance(reply.get("holder"), dict) else {}
            if reply.get("ok") is not True and str(holder.get("task_id") or "") != task.task_id:
                return (
                    f"{item['path']} 正被任务 {holder.get('task_id') or '?'}（节点 {holder.get('node_id') or '?'}）改着，"
                    "这次没有合回；等那个任务结束后再合回"
                )
        return ""

    def merge(self, *, task_id: str, handle: str, digest: str) -> dict[str, Any]:
        """把副本的改动套到原仓库上。审批由调用方的工具先走完；digest 是审批时那份 diff 的指纹，对不上就不合。"""
        state = self.state
        with state._lock:
            task, entry, problem = self._merge_target_locked(task_id, handle, for_merge=True)
            if problem or entry is None or task is None:
                return {"ok": False, "reason": "refused", "message": problem}
            if not self.merge_allowed(entry):
                return {
                    "ok": False, "reason": "instance_not_allowed",
                    "message": f"本实例不能合回，补丁在 {_posix(entry.get('patch'))}",
                    "patch": _posix(entry.get("patch")),
                }
            worktree_id = str(entry["id"])
            if self.registry.transition(worktree_id, {FINISHED}, MERGING, merging_task=task.task_id, merge_started_at=now_iso()) is None:
                return {"ok": False, "reason": "refused", "message": f"隔离副本 {worktree_id} 刚被别处动过，没有合回"}
            with self._guard:
                self._merging.add(worktree_id)
            journal_session = _journal_session_locked(
                state, task, str(state._route_session_id_for_task_locked(task) or task.session_id),
            )
            node_id = str(task.node_id or "")
        try:
            with self._patch_lock(entry):
                return self._merge_outside_lock(task, entry, digest=digest, journal_session=journal_session, node_id=node_id)
        finally:
            with self._guard:
                self._merging.discard(worktree_id)
            with state._lock:
                self.registry.transition(worktree_id, {MERGING}, FINISHED, merging_task=None)
            self._wake.set()

    def _merge_outside_lock(
        self, task: Task, entry: dict[str, Any], *, digest: str, journal_session: str, node_id: str,
    ) -> dict[str, Any]:
        worktree_id = str(entry["id"])
        summary, failure = self._fresh_diff(entry)
        if summary is None:
            return {"ok": False, "reason": "failed", "message": failure}
        if summary.digest != str(digest or "").strip():
            return {
                "ok": False, "reason": "changed",
                "message": f"隔离副本 {worktree_id} 在审批之后又变了，这次没有合回；重新调用 merge_worktree，会按现在的改动重新审批",
            }
        files, problem = self._merge_files(entry, summary)
        if problem:
            return {"ok": False, "reason": "refused", "message": problem}
        if not files:
            self._mark_merged(worktree_id, task)
            return {"ok": True, **self._merge_listing(entry, summary, files), "merged": [], "message": "副本里没有改动，不用合回；副本会被清理"}
        targets = [Path(str(item["target"])) for item in files]
        journal_hit = [item["path"] for item, target in zip(files, targets) if edit_journal.inside_journal(self.workspace_root, target)]
        if journal_hit:
            return {"ok": False, "reason": "refused", "message": f"{edit_journal.JOURNAL_REFUSED_MESSAGE}：{'、'.join(journal_hit)}"}
        claimed, refusal = self._claim(task, files, journal_session)
        if refusal:
            return {"ok": False, "reason": "claimed", "message": refusal}
        try:
            outcome = self._apply(entry, summary, files, targets, journal_session=journal_session, task_id=task.task_id, node_id=node_id)
        finally:
            self._release(task, claimed)
        if outcome.get("ok"):
            self._mark_merged(worktree_id, task)
        return outcome

    def _claim(self, task: Task, files: list[dict[str, Any]], journal_session: str) -> tuple[list[str], str]:
        """按编辑工具的文件认领认领每个要改的文件；有别的任务在改就全部还回去并说明是谁。"""
        ledger = ledger_of(self.state)
        claimed: list[str] = []
        with self.state._lock:
            now = _utcnow()
            resolve = _resolver_locked(self.state, now)
            for item in files:
                key = file_state.path_key(Path(str(item["target"])))
                reply = ledger.begin(
                    task_id=task.task_id, session=journal_session, path=key, display=str(item["path"]),
                    journal_session=journal_session, resolve=resolve, now=now.timestamp(),
                )
                if reply.get("ok") is True:
                    if reply.get("new_claim"):
                        claimed.append(key)
                    continue
                holder = reply.get("holder") if isinstance(reply.get("holder"), dict) else {}
                for done in claimed:
                    ledger.release(task_id=task.task_id, path=done)
                if reply.get("reason") == "claimed":
                    return [], (
                        f"{item['path']} 正被任务 {holder.get('task_id') or '?'}（节点 {holder.get('node_id') or '?'}）改着，"
                        "这次没有合回；等那个任务结束后再合回"
                    )
                return [], f"认领 {item['path']} 失败（{reply.get('reason') or '没有说明原因'}），这次没有合回"
        return claimed, ""

    def _release(self, task: Task, claimed: list[str]) -> None:
        if not claimed:
            return
        ledger = ledger_of(self.state)
        for key in claimed:
            with contextlib.suppress(Exception):
                ledger.release(task_id=task.task_id, path=key)

    def _apply(
        self,
        entry: Mapping[str, Any],
        summary: git.DiffSummary,
        files: list[dict[str, Any]],
        targets: list[Path],
        *,
        journal_session: str,
        task_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        worktree_id = str(entry["id"])
        repo = Path(str(entry["repo"]))
        patch = Path(str(entry["patch"]))
        listing = self._merge_listing(entry, summary, files)
        scope = edit_journal.JournalScope(
            workspace_root=self.workspace_root, session=journal_session,
            task_id=f"{MERGE_JOURNAL_PREFIX}{worktree_id}", node_id=node_id,
        )
        with file_state.edit_locks(targets, self.workspace_root):
            try:
                conflicts = git.check_apply(repo, patch)
            except git.GitError as exc:
                return {"ok": False, "reason": "failed", "message": exc.message, **listing}
            if conflicts:
                return {
                    "ok": False, "reason": "conflict",
                    "message": f"原仓库在这期间被改过，隔离副本 {worktree_id} 的改动有 {len(conflicts)} 处套不上，什么都没有改；副本留着",
                    "conflicts": [item.to_data() for item in conflicts],
                    **listing,
                }
            snapshots = {target: file_state.take_snapshot(target) for target in targets}
            modes = {target: self._mode(target) for target in targets}
            pendings = [
                edit_journal.record_pre_image(
                    scope, target, snapshots[target], tool="merge_worktree", display=str(item["path"]),
                )
                for item, target in zip(files, targets)
            ]
            try:
                git.apply_patch(repo, patch)
            except Exception as exc:
                self._roll_back(targets, snapshots, modes)
                for pending in pendings:
                    pending.discard()
                message = exc.message if isinstance(exc, git.GitError) else str(exc)
                return {"ok": False, "reason": "failed", "message": f"套补丁中途失败，已经按原样放回：{message}", **listing}
            notes = []
            for target, pending in zip(targets, pendings):
                after = file_state.take_snapshot(target)
                notes.append(pending.record_post_image(after.stamp) if after.exists and after.stamp is not None else pending.record_deletion())
        undoable = all(note.undoable for note in notes)
        reasons = sorted({note.reason for note in notes if note.reason})
        return {
            "ok": True,
            **listing,
            "merged": [item["path"] for item in files],
            "undo_task_id": scope.task_id,
            "journal_session": journal_session,
            "undoable": undoable,
            "undo_notes": reasons,
            "task_id": task_id,
        }

    @staticmethod
    def _mode(path: Path) -> int | None:
        try:
            return os.stat(path).st_mode & 0o7777
        except OSError:
            return None

    @staticmethod
    def _roll_back(targets: list[Path], snapshots: dict[Path, file_state.FileSnapshot], modes: dict[Path, int | None]) -> None:
        for target in targets:
            snapshot = snapshots[target]
            try:
                if snapshot.exists:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    current = file_state.take_snapshot(target)
                    if current.stamp is None or snapshot.stamp is None or current.stamp.sha256 != snapshot.stamp.sha256:
                        replace_bytes(target, snapshot.data)
                    mode = modes.get(target)
                    if mode is not None:
                        with contextlib.suppress(OSError):
                            os.chmod(target, mode)
                elif os.path.lexists(target):
                    target.unlink()
            except OSError:
                log.exception("rolling back %s after a failed merge failed", target)

    def _mark_merged(self, worktree_id: str, task: Task) -> None:
        with self.state._lock:
            self.registry.transition(
                worktree_id, {MERGING, FINISHED}, MERGED,
                cleanup_reason=REASON_MERGED, merged_by=task.task_id, merged_at=now_iso(), merging_task=None,
            )
        self._wake.set()

    def abandon(self, *, task_id: str, handle: str) -> dict[str, Any]:
        with self.state._lock:
            task, problem = self._caller_locked(task_id)
            if task is None:
                return {"ok": False, "reason": "refused", "message": problem}
            entry, problem = self._resolve_handle_locked(task, handle)
            if entry is None:
                return {"ok": False, "reason": "refused", "message": problem}
            worktree_id = str(entry["id"])
            state = str(entry.get("state") or "")
            if state == MERGING or worktree_id in self._merging:
                return {"ok": False, "reason": "refused", "message": f"隔离副本 {worktree_id} 正在合回，不能放弃"}
            if state not in LIVE_STATES:
                return {
                    "ok": True, "worktree": worktree_id, "already": True,
                    "message": f"隔离副本 {worktree_id} {_STATE_LABELS.get(state, state)}，不用再放弃",
                }
            users = self._users_locked(worktree_id)
            if users:
                names = "、".join(f"{user.task_id}（{user.node_id or user.tool_name or '?'}）" for user in users[:5])
                return {
                    "ok": False, "reason": "refused",
                    "message": f"还有任务在用隔离副本 {worktree_id}：{names}；先取消它们再放弃",
                }
            self.registry.transition(
                worktree_id, {READY, FINISHED}, ABANDONED,
                cleanup_reason=REASON_ABANDONED, abandoned_by=task.task_id, note="父节点放弃了这份改动",
            )
        self._wake.set()
        return {
            "ok": True, "worktree": worktree_id, "branch": str(entry.get("branch") or ""),
            "patch": _posix(entry.get("patch")),
            "message": f"已放弃隔离副本 {worktree_id}，副本目录和分支 {entry.get('branch') or '?'} 会被清理",
        }

    def listing(self) -> list[dict[str, Any]]:
        rows = []
        for entry in sorted(self.registry.entries(), key=lambda item: str(item.get("created_at") or ""), reverse=True):
            outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            rows.append({
                key: entry.get(key) for key in (
                    "id", "state", "repo", "branch", "base", "path", "patch", "owners", "node_id", "requested_by",
                    "created_at", "ready_at", "finished_at", "until", "removed_at", "error", "note", "cleanup_reason",
                ) if entry.get(key) not in (None, "", [])
            } | {"file_count": outcome.get("file_count"), "added": outcome.get("added"), "deleted": outcome.get("deleted")})
        return rows

    def housekeeping(self) -> None:
        """往前推一格：给收尾的副本起算保留期、过期的标成到期，然后清理该清的、删掉过期的登记。"""
        for worktree_id in self.sweep():
            self.cleanup(worktree_id)
        self._prune_tombstones()

    def sweep(self) -> list[str]:
        """持锁过一遍登记，返回现在就该清理的副本。"""
        settings = self.settings()
        now = _utcnow()
        due: list[str] = []
        with self.state._lock:
            with self._guard:
                busy = set(self._provisioning) | set(self._merging) | set(self._cleaning) | set(self._queued)
            for entry in self.registry.entries():
                worktree_id = str(entry["id"])
                if worktree_id in busy:
                    continue
                state = str(entry.get("state") or "")
                if state == CREATING:
                    self._sweep_creating_locked(entry)
                elif state == READY:
                    if not self._users_locked(worktree_id) and self._live_owner_locked(entry) is None:
                        self.registry.transition(worktree_id, {READY}, FINISHED, finished_at=now_iso())
                elif state == FINISHED:
                    self._sweep_finished_locked(entry, now, settings.retain_grace)
                elif state == MERGING:
                    self.registry.transition(
                        worktree_id, {MERGING}, FINISHED, merging_task=None,
                        note="合回没有走完；原仓库里可能已经改了一部分，undo_edits 里有对应的记录",
                    )
                # 这一轮刚到期、刚判成没人要的，也在这一轮清掉
                current = self.registry.get(worktree_id)
                if current is not None and current.get("state") in CLEANUP_STATES and not self._users_locked(worktree_id):
                    due.append(worktree_id)
        return due

    def _sweep_creating_locked(self, entry: Mapping[str, Any]) -> None:
        worktree_id = str(entry["id"])
        task = self.state.tasks.get(worktree_id)
        if task is None or not self._live_locked(task) or task.cancel_requested:
            self.registry.transition(
                worktree_id, {CREATING}, ABANDONED,
                cleanup_reason=REASON_ORPHAN if task is None else REASON_CANCELLED,
                note="子任务在副本建好之前就结束了",
            )
            return
        # 后台线程没接着做（比如上一轮异常退出）：重新排上
        self._enqueue(worktree_id)

    def _sweep_finished_locked(self, entry: Mapping[str, Any], now: datetime, grace: timedelta) -> None:
        worktree_id = str(entry["id"])
        if self._users_locked(worktree_id):
            if entry.get("until"):
                self.registry.update(worktree_id, until=None)
            return
        until_text = str(entry.get("until") or "").strip()
        if not until_text:
            dispatcher = self.state.tasks.get(str(entry.get("requested_by") or ""))
            if dispatcher is not None and self._live_locked(dispatcher):
                return
            self.registry.update(worktree_id, until=(now + grace).isoformat(timespec="seconds"))
            return
        until = parse_time(until_text)
        if until is None or until <= now:
            self.registry.transition(
                worktree_id, {FINISHED}, EXPIRED, cleanup_reason=REASON_EXPIRED, note="过了保留期还没合回",
            )

    def recover(self) -> None:
        """supervisor 启动时清点：只管 engine.worktree.root 下登记过的副本；git 里有、登记里没有的不动。"""
        settings = self.settings()
        now = _utcnow()
        with self.state._lock:
            for entry in self.registry.entries():
                worktree_id = str(entry["id"])
                state = str(entry.get("state") or "")
                if state == REMOVED:
                    continue
                # 根配坏了先什么都不认：等配好了再清点，免得把整份登记都当成根外面的
                if entry.get("path") and not settings.root_problem and not self._under_root(entry, settings):
                    self.registry.update(
                        worktree_id, state=REMOVED, removed_at=now_iso(),
                        note=f"登记的目录 {entry.get('path')} 不在 engine.worktree.root（{settings.root.as_posix()}）下，没有清理",
                    )
                    continue
                owners = [self.state.tasks.get(task_id) for task_id in entry.get("owners", ())]
                present = [task for task in owners if task is not None]
                if state == CREATING:
                    self.registry.transition(
                        worktree_id, {CREATING}, ABANDONED,
                        cleanup_reason=REASON_ORPHAN, note="supervisor 重启时副本还没建好",
                    )
                elif state == READY:
                    # 主人还在任务表里：要么还活着，要么是等重新路由的终态任务，路由时会把副本转成等合回
                    if present:
                        continue
                    self.registry.transition(
                        worktree_id, {READY}, ABANDONED,
                        cleanup_reason=REASON_ORPHAN, note="supervisor 重启时用它的任务都没了",
                    )
                elif state == MERGING:
                    self.registry.transition(
                        worktree_id, {MERGING}, FINISHED, merging_task=None,
                        note=(
                            "合回中途 supervisor 退出了：原仓库里可能已经改了一部分，"
                            f"undo_edits 里任务 {MERGE_JOURNAL_PREFIX}{worktree_id} 的记录能撤销"
                        ),
                    )
                elif state == FINISHED and not entry.get("until"):
                    self.registry.update(worktree_id, until=(now + settings.retain_grace).isoformat(timespec="seconds"))
        self._wake.set()

    def _under_root(self, entry: Mapping[str, Any], settings: WorktreeSettings) -> bool:
        if settings.root_problem:
            return False
        path = _resolved(str(entry.get("path") or ""))
        expected = settings.root / str(entry.get("repo_key") or "") / str(entry.get("id") or "")
        return bool(entry.get("repo_key")) and os.path.normcase(str(path)) == os.path.normcase(str(_resolved(expected)))

    def _removal_problem(self, entry: Mapping[str, Any], settings: WorktreeSettings) -> str:
        """删之前核对：目录必须正是 <根>/<仓库键>/<登记号>，和工作区、原仓库都不重叠，里面的 .git 指回原仓库。"""
        if not entry.get("path"):
            return ""
        if not self._under_root(entry, settings):
            return f"登记的目录 {entry.get('path')} 不在 engine.worktree.root 下"
        path = _resolved(str(entry["path"]))
        protected = [self.workspace_root, config_layout.CODE_ROOT]
        if entry.get("repo"):
            protected.append(Path(str(entry["repo"])))
        for item in protected:
            guard = _resolved(item)
            if _inside(path, guard) or _inside(guard, path):
                return f"登记的目录 {path.as_posix()} 和 {guard.as_posix()} 重叠"
        if not os.path.lexists(path):
            return ""
        if git.is_link(path):
            return f"{path.as_posix()} 是一个链接"
        try:
            empty = not any(path.iterdir())
        except OSError:
            empty = False
        if empty:
            return ""
        if not entry.get("repo") or not git.worktree_belongs_to(path, Path(str(entry["repo"]))):
            return f"{path.as_posix()} 不像是原仓库的 worktree（.git 没有指回原仓库）"
        return ""

    def cleanup(self, worktree_id: str) -> bool:
        """删副本：先停它下面的常驻进程、拆掉链接，再 git worktree remove、删分支。删成了返回 True。"""
        with self._guard:
            if worktree_id in self._cleaning:
                return False
            self._cleaning.add(worktree_id)
        try:
            return self._cleanup(worktree_id)
        finally:
            with self._guard:
                self._cleaning.discard(worktree_id)

    def _cleanup(self, worktree_id: str) -> bool:
        with self.state._lock:
            entry = self.registry.get(worktree_id)
            if entry is None or entry.get("state") not in CLEANUP_STATES or self._users_locked(worktree_id):
                return False
        settings = self.settings()
        if settings.root_problem:
            log.warning("not removing worktree %s yet: %s", worktree_id, settings.root_problem)
            return False
        problem = self._removal_problem(entry, settings)
        if problem:
            log.error("not removing worktree %s: %s", worktree_id, problem)
            self.registry.update(worktree_id, state=REMOVED, removed_at=now_iso(), note=f"{problem}，目录没有动")
            return False
        path = Path(str(entry["path"])) if entry.get("path") else None
        repo = Path(str(entry["repo"])) if entry.get("repo") else None
        try:
            if path is not None:
                processes = getattr(self.state, "processes", None)
                stop = getattr(processes, "stop_under_root", None)
                if callable(stop):
                    stop(path, wait_sec=_STOP_PROCESSES_WAIT_SEC)
                self._keep_patch(entry)
                for link in git.links_under(path) if path.is_dir() else ():
                    git.remove_link(link)
            if repo is not None and repo.is_dir():
                with self._repo_lock(repo):
                    if path is not None and os.path.lexists(path):
                        git.remove_worktree(repo, path)
                    if path is not None and os.path.lexists(path):
                        git.remove_tree(path)
                    git.prune_worktrees(repo)
                    if entry.get("branch"):
                        git.delete_branch(repo, str(entry["branch"]))
            elif path is not None and os.path.lexists(path):
                git.remove_tree(path)
        except (git.GitError, OSError) as exc:
            attempts = int(entry.get("cleanup_attempts") or 0) + 1
            message = exc.message if isinstance(exc, git.GitError) else str(exc)
            log.warning("removing worktree %s failed (attempt %d): %s", worktree_id, attempts, message)
            fields: dict[str, Any] = {"cleanup_attempts": attempts, "cleanup_error": message}
            if attempts >= _MAX_CLEANUP_ATTEMPTS:
                fields.update(state=REMOVED, removed_at=now_iso(), note=f"清理连着失败 {attempts} 次，留给人手工处理：{message}")
            self.registry.update(worktree_id, **fields)
            return False
        self.registry.update(worktree_id, state=REMOVED, removed_at=now_iso(), cleanup_error="")
        log.info("worktree %s removed (%s)", worktree_id, entry.get("cleanup_reason") or "")
        return True

    def _keep_patch(self, entry: Mapping[str, Any]) -> None:
        """没合回就要删的副本（取消、到期、孤儿）：删之前把改动存成补丁，登记过期前都还在。"""
        if str(entry.get("cleanup_reason") or "") not in _KEEP_PATCH_REASONS:
            return
        path = Path(str(entry.get("path") or ""))
        if not entry.get("patch") or not entry.get("base") or not path.is_dir():
            return
        try:
            with self._patch_lock(entry):
                summary = git.compute_diff(path, str(entry["base"]), Path(str(entry["patch"])), excluded=entry.get("links") or ())
        except (git.GitError, OSError) as exc:
            log.warning("saving the changes of worktree %s before removing it failed: %s", entry.get("id"), exc)
            return
        self.registry.update(str(entry["id"]), kept_patch=bool(summary.files), outcome={
            **(entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}),
            **self._summary_fields(summary),
        })
        if not summary.files:
            with contextlib.suppress(OSError):
                Path(str(entry["patch"])).unlink()

    def _prune_tombstones(self) -> None:
        settings = self.settings()
        cutoff = _utcnow() - timedelta(days=settings.tombstone_days)
        for entry in self.registry.entries():
            if entry.get("state") != REMOVED:
                continue
            removed = parse_time(entry.get("removed_at"))
            if removed is None or removed > cutoff:
                continue
            patch = Path(str(entry.get("patch") or "")) if entry.get("patch") else None
            if patch is not None and self._under_root(entry, settings):
                with contextlib.suppress(OSError):
                    patch.unlink()
                with contextlib.suppress(OSError):
                    patch.parent.rmdir()
            self.registry.remove(str(entry["id"]))
