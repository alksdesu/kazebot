"""文件认领和读戳的记账：supervisor 记谁正在改哪个文件、每个任务和会话读到过哪一版，engine 经 /v1/file_edits 来问。

认领挡别的还没结束的任务（跨会话也挡），同一任务再改不挡；挂起等子任务的任务照样持有，它同步等着的子任务可以在里层认领。
任务结束（完成、取消、被回收）就放掉认领，读戳并进会话层；终态钩子没赶上的由下一次请求顺带补收。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.file_claims import (  # noqa: E402
    FileClaimBook,
    FileLedger,
    Holder,
    TaskView,
    holder_payload,
    ledger_of,
    release_if_ended_locked,
)
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_reaper import reap_stuck_tasks_locked  # noqa: E402
from supervisor.types import RouteStatus, Task, TaskStatus  # noqa: E402
from toolbox import file_state  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_TOKEN = "file-claims-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_LIVE = frozenset({"running", "pending", "suspended"})


class _Tasks:
    """resolve 的替身：按任务号给出 TaskView，任务的状态由测试摆。"""

    def __init__(self) -> None:
        self.views: dict[str, TaskView] = {}

    def add(
        self, task_id: str, *, status: str = "running", node: str = "test.node", session: str = "sess",
        caller: str = "", created: float = 0.0,
    ) -> TaskView:
        view = TaskView(task_id, status in _LIVE, status, node, session, caller, created, None)
        self.views[task_id] = view
        return view

    def set(self, task_id: str, status: str, *, ended: float | None = None) -> None:
        live = status in _LIVE
        self.views[task_id] = replace(
            self.views[task_id], status=status, live=live, ended_at=None if live else ended,
        )

    def __call__(self, task_id: str) -> TaskView | None:
        return self.views.get(task_id)


def _entry(data: bytes = b"x\n", *, revealable: bool = True) -> file_state.Entry:
    return file_state.Entry(file_state.stamp_of(data, 1), revealable)


class TestTheClaimBook:
    def test_the_first_editor_claims_the_file(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()

        outcome = book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)

        assert outcome.ok and outcome.new
        assert book.held_by("t1") == ["/f"]

    def test_the_same_task_editing_again_is_not_a_new_claim(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)

        again = book.acquire("/f", tasks("t1"), "f", resolve=tasks, now=2)  # type: ignore[arg-type]

        assert again.ok and not again.new
        assert [holder.task_id for _path, holders in book.paths() for holder in holders] == ["t1"]

    def test_another_running_task_is_refused_and_told_who_holds_it(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1", node="bootstrap.coder"), "f", resolve=tasks, now=1)

        outcome = book.acquire("/f", tasks.add("t2"), "f", resolve=tasks, now=2)

        assert not outcome.ok
        assert outcome.holder == Holder("t1", "f", 1)
        assert outcome.view is not None and outcome.view.node_id == "bootstrap.coder"
        assert book.held_by("t2") == []

    def test_claims_hold_across_sessions(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1", session="sess-a"), "f", resolve=tasks, now=1)

        assert not book.acquire("/f", tasks.add("t2", session="sess-b"), "f", resolve=tasks, now=2).ok

    def test_other_files_are_not_affected(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)

        assert book.acquire("/g", tasks.add("t2"), "g", resolve=tasks, now=2).ok

    def test_a_holder_that_ended_no_longer_blocks_even_before_it_is_released(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)
        tasks.set("t1", "failed", ended=2)

        outcome = book.acquire("/f", tasks.add("t2"), "f", resolve=tasks, now=3)

        assert outcome.ok and outcome.new
        assert book.held_by("t1") == [] and book.held_by("t2") == ["/f"]

    def test_a_holder_the_supervisor_forgot_no_longer_blocks(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)
        del tasks.views["t1"]

        assert book.acquire("/f", tasks.add("t2"), "f", resolve=tasks, now=2).ok

    def test_a_suspended_holder_lets_the_child_it_waits_for_claim_inside(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")

        outcome = book.acquire("/f", tasks.add("child", caller="parent"), "f", resolve=tasks, now=2)

        assert outcome.ok and outcome.new
        assert [holder.task_id for holder in dict(book.paths())["/f"]] == ["parent", "child"]

    def test_a_grandchild_down_the_wait_chain_may_claim_inside_too(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")
        tasks.add("child", caller="parent", status="suspended")

        outcome = book.acquire("/f", tasks.add("grandchild", caller="child"), "f", resolve=tasks, now=2)

        assert outcome.ok and outcome.new

    def test_siblings_under_one_suspended_caller_still_exclude_each_other(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")
        book.acquire("/f", tasks.add("first", caller="parent"), "f", resolve=tasks, now=2)

        outcome = book.acquire("/f", tasks.add("second", caller="parent"), "f", resolve=tasks, now=3)

        assert not outcome.ok and outcome.holder is not None and outcome.holder.task_id == "first"

    def test_a_task_outside_the_wait_chain_is_refused_by_a_suspended_holder(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")

        outcome = book.acquire("/f", tasks.add("stranger"), "f", resolve=tasks, now=2)

        assert not outcome.ok
        assert outcome.view is not None and outcome.view.status == "suspended"

    def test_a_wait_chain_broken_by_an_ended_task_is_not_followed(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")
        tasks.add("middle", caller="parent")
        tasks.set("middle", "cancelled", ended=2)

        assert not book.acquire("/f", tasks.add("late", caller="middle"), "f", resolve=tasks, now=3).ok

    def test_a_cyclic_wait_chain_ends_in_a_refusal(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("holder"), "f", resolve=tasks, now=1)
        tasks.set("holder", "suspended")
        tasks.add("a", caller="b")
        tasks.add("b", caller="a")

        assert not book.acquire("/f", tasks("a"), "f", resolve=tasks, now=2).ok  # type: ignore[arg-type]

    def test_a_task_already_holding_further_out_cannot_claim_again_inside(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")
        book.acquire("/f", tasks.add("child", caller="parent"), "f", resolve=tasks, now=2)
        tasks.set("child", "suspended")

        outcome = book.acquire("/f", tasks("parent"), "f", resolve=tasks, now=3)  # type: ignore[arg-type]

        assert not outcome.ok and outcome.holder is not None and outcome.holder.task_id == "child"

    def test_release_drops_one_path_or_all_of_a_task(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        holder = tasks.add("t1")
        for path in ("/a", "/b", "/c"):
            book.acquire(path, holder, path, resolve=tasks, now=1)

        assert book.release("t1", "/a") == 1
        assert book.held_by("t1") == ["/b", "/c"]
        assert book.release("t1") == 2
        assert book.held_by("t1") == [] and book.paths() == []
        assert book.release("t1") == 0 and book.release("nobody", "/a") == 0

    def test_releasing_the_outer_holder_keeps_the_inner_one(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/f", tasks.add("parent"), "f", resolve=tasks, now=1)
        tasks.set("parent", "suspended")
        book.acquire("/f", tasks.add("child", caller="parent"), "f", resolve=tasks, now=2)

        book.release("parent")

        assert [holder.task_id for holder in dict(book.paths())["/f"]] == ["child"]

    def test_sweep_drops_the_claims_of_ended_tasks(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()
        book.acquire("/a", tasks.add("t1"), "a", resolve=tasks, now=1)
        book.acquire("/b", tasks.add("t2"), "b", resolve=tasks, now=1)
        tasks.set("t1", "completed", ended=2)

        assert book.sweep(tasks) == 1
        assert book.held_by("t1") == [] and book.held_by("t2") == ["/b"]

    def test_check_reports_a_holder_without_claiming(self) -> None:
        tasks, book = _Tasks(), FileClaimBook()

        assert book.check("/f", resolve=tasks).ok
        assert book.paths() == []
        book.acquire("/f", tasks.add("t1"), "f", resolve=tasks, now=1)
        refused = book.check("/f", resolve=tasks)
        assert not refused.ok and refused.holder is not None and refused.holder.task_id == "t1"


class TestTheLedger:
    def _begin(self, ledger: FileLedger, tasks: _Tasks, task_id: str, *, path: str = "/f", session: str = "sess",
               journal: str = "", now: float = 10) -> dict[str, Any]:
        return ledger.begin(
            task_id=task_id, session=session, path=path, display=path.lstrip("/"),
            journal_session=journal, resolve=tasks, now=now,
        )

    def test_begin_claims_and_hands_back_the_task_s_own_stamp(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        ledger.record(task_id="t1", session="sess", path="/f", entry=_entry(b"v1\n"), now=5)

        reply = self._begin(ledger, tasks, "t1", journal="root")

        assert reply == {
            "ok": True, "new_claim": True, "journal_session": "root",
            "stamp": file_state.entry_to_wire(_entry(b"v1\n")), "stamp_layer": "task", "foreign": False,
        }
        assert self._begin(ledger, tasks, "t1", journal="root")["new_claim"] is False

    def test_without_a_journal_root_the_own_session_is_used(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")

        assert self._begin(ledger, tasks, "t1", session="sess-own")["journal_session"] == "sess-own"

    def test_the_journal_session_of_a_task_is_fixed_on_first_use(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        self._begin(ledger, tasks, "t1", path="/a", journal="root-1")

        assert self._begin(ledger, tasks, "t1", path="/b", journal="root-2")["journal_session"] == "root-1"
        scope = ledger.scope("t1", journal_session="root-3", view=tasks("t1"))
        assert scope["journal_session"] == "root-1"

    def test_an_unknown_task_is_refused(self) -> None:
        assert self._begin(FileLedger(), _Tasks(), "ghost") == {"ok": False, "reason": "task_unknown"}

    def test_a_task_that_already_ended_is_refused(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        tasks.set("t1", "cancelled", ended=3)

        assert self._begin(ledger, tasks, "t1") == {"ok": False, "reason": "task_ended", "status": "cancelled"}

    def test_a_conflict_names_the_holder(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1", node="bootstrap.coder", session="sess-1")
        tasks.add("t2")
        self._begin(ledger, tasks, "t1", now=0)

        reply = self._begin(ledger, tasks, "t2")

        assert reply == {
            "ok": False, "reason": "claimed",
            "holder": {
                "task_id": "t1", "node_id": "bootstrap.coder", "status": "running", "session_id": "sess-1",
                "path": "f", "since": "1970-01-01T00:00:00+00:00",
            },
        }

    def test_without_a_task_id_it_only_checks(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()

        reply = self._begin(ledger, tasks, "")

        assert reply["ok"] is True and reply["new_claim"] is False
        assert ledger.claims.paths() == []
        tasks.add("t1")
        self._begin(ledger, tasks, "t1")
        assert self._begin(ledger, tasks, "")["reason"] == "claimed"

    def test_a_parallel_task_does_not_get_another_task_s_stamp(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        tasks.add("t2")
        ledger.record(task_id="t1", session="sess", path="/f", entry=_entry(), now=1)

        reply = self._begin(ledger, tasks, "t2")

        assert reply["stamp"] is None and reply["stamp_layer"] == ""

    def test_ending_a_task_releases_its_claims_and_shares_its_stamps(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        ledger.record(task_id="t1", session="sess", path="/f", entry=_entry(b"v2\n"), now=1)
        self._begin(ledger, tasks, "t1")
        tasks.set("t1", "completed", ended=2)

        ledger.end_task("t1", ended_at=2)

        assert ledger.claims.held_by("t1") == []
        tasks.add("t2", created=3)
        later = self._begin(ledger, tasks, "t2")
        assert later["ok"] and later["new_claim"]
        assert later["stamp"] == file_state.entry_to_wire(_entry(b"v2\n"))
        assert later["stamp_layer"] == "session" and later["foreign"] is False

    def test_a_stamp_shared_after_a_task_started_is_foreign_to_it(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t-early", created=1)
        tasks.add("t-writer", created=2)
        ledger.record(task_id="t-writer", session="sess", path="/f", entry=_entry(b"new\n"), now=3)
        tasks.set("t-writer", "completed", ended=4)
        ledger.end_task("t-writer", ended_at=4)

        reply = self._begin(ledger, tasks, "t-early")

        assert reply["stamp"] == file_state.entry_to_wire(_entry(b"new\n")) and reply["foreign"] is True

    def test_a_missed_end_is_caught_up_by_the_next_request(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1", created=0)
        ledger.record(task_id="t1", session="sess", path="/f", entry=_entry(), now=1)
        self._begin(ledger, tasks, "t1", now=1)
        # 终态钩子没跑：状态改了，记账还留着
        tasks.set("t1", "cancelled", ended=5)
        tasks.add("t-before", created=4)
        tasks.add("t-after", created=6)

        before = self._begin(ledger, tasks, "t-before", path="/f", now=9)

        assert before["ok"] and before["foreign"] is True
        assert ledger.claims.held_by("t1") == [] and ledger.stamps.task_ids() == []
        ledger.release(task_id="t-before", path="/f")
        assert self._begin(ledger, tasks, "t-after", now=10)["foreign"] is False

    def test_scope_answers_for_live_and_unknown_tasks(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1", node="bootstrap.coder")

        assert ledger.scope("t1", journal_session="root", view=tasks("t1")) == {
            "ok": True, "task_id": "t1", "journal_session": "root", "node_id": "bootstrap.coder", "status": "running",
        }
        assert ledger.scope("ghost", journal_session="root", view=None) == {"ok": False, "reason": "task_unknown"}

    def test_the_scope_of_an_ended_task_is_not_cached(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        tasks.set("t1", "completed", ended=1)

        ledger.scope("t1", journal_session="root-1", view=tasks("t1"))

        assert ledger.scope("t1", journal_session="root-2", view=tasks("t1"))["journal_session"] == "root-2"

    def test_release_reports_whether_anything_was_released(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        self._begin(ledger, tasks, "t1")

        assert ledger.release(task_id="t1", path="/f") == {"ok": True, "released": True}
        assert ledger.release(task_id="t1", path="/f") == {"ok": True, "released": False}

    def test_describe_lists_live_claims_and_filters_by_task(self) -> None:
        tasks, ledger = _Tasks(), FileLedger()
        tasks.add("t1")
        tasks.add("t2")
        self._begin(ledger, tasks, "t1", path="/a")
        self._begin(ledger, tasks, "t2", path="/b")

        rows = ledger.describe(tasks)
        mine = ledger.describe(tasks, task_id="t2")

        assert [row["path"] for row in rows] == ["/a", "/b"]
        assert [row["path"] for row in mine] == ["/b"]
        assert mine[0]["holders"][0]["task_id"] == "t2"

    def test_the_holder_payload_survives_a_missing_view(self) -> None:
        payload = holder_payload(Holder("t1", "a.txt", 0), None)

        assert payload == {
            "task_id": "t1", "node_id": "", "status": "", "session_id": "", "path": "a.txt",
            "since": "1970-01-01T00:00:00+00:00",
        }


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="file-claims"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


@pytest.fixture
def app(state: SupervisorState, tmp_path: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )


def _inbound(state: SupervisorState, session_id: str, **extra: Any) -> Task:
    """和 /v1/inbound 走同一条建任务的路，建出来的任务当作已被 worker 领走。"""
    payload = {
        "channel": "qq_group",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": "request",
        **extra,
    }
    event = state.eventlog.append(
        session_id=session_id, component="shell", type_="inbound_message", payload=payload,
    )
    state.record_inbound_message_event(event)
    task = next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])
    task.status = TaskStatus.running
    task.worker_id = "worker"
    return task


def _conversation(state: SupervisorState, key: str) -> tuple[str, Task]:
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=key)
    return session_id, _inbound(state, session_id, entry_node_id="qq.orchestrator")


def _delegate(state: SupervisorState, caller: Task, target: str) -> tuple[str, Task]:
    """异步委派：子节点跑在自己的 agent: 会话里，dispatch_origin 记着发起它的会话和任务。"""
    context = caller.input.get("task_context") or {}
    parent_key = str(context.get("route_conversation_key") or context.get("conversation_key") or "")
    session_id = state.get_or_create_session(channel="qq_group", conversation_key=f"agent:{target}:{parent_key}")
    child = _inbound(
        state, session_id, entry_node_id=target, dispatch_context_mode="accumulate",
        dispatch_origin={
            "parent_session_id": str(caller.input.get("parent_session_id") or caller.session_id),
            "caller_node_id": caller.node_id,
            "parent_conversation_key": parent_key,
            "context_mode": "accumulate",
            "caller_task_id": caller.task_id,
        },
    )
    return session_id, child


def _finish(state: SupervisorState, task: Task) -> None:
    state.complete_task(
        task_id=task.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "done"}},
    )


def _wait_for_child(state: SupervisorState, caller: Task, target: str) -> Task:
    """wait 委派走完成回报：先记成 completed，路由里才挂起、派出子任务。"""
    state.complete_task(
        task_id=caller.task_id, worker_id="worker",
        result={
            "action": "dispatch", "target_node": target,
            "dispatch_input": {"instruction": "help", "context_mode": "fresh"},
        },
    )
    child = state.tasks[str(caller.waiting_for_task_id)]
    child.status = TaskStatus.running
    child.worker_id = "worker"
    return child


def _call(app: Any, method: str, url: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, headers=_AUTH if auth else {}, **kwargs)

    return asyncio.run(run())


def _session_key(task: Task) -> str:
    """engine 里 file_state.session_key 取到的会话：有 child session 用它，入口分支归到父会话。"""
    return str(task.input.get("child_session_id") or task.input.get("parent_session_id") or task.session_id)


def _begin(app: Any, task: Task | None, path: Path, *, session: str = "") -> dict[str, Any]:
    response = _call(app, "POST", "/v1/file_edits/begin", json={
        "task_id": task.task_id if task is not None else "",
        "session": session or (_session_key(task) if task is not None else ""),
        "path": str(path),
        "display": path.name,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _stamp(app: Any, task: Task, path: Path, data: bytes, *, session: str = "") -> dict[str, Any]:
    response = _call(app, "POST", "/v1/file_edits/stamp", json={
        "task_id": task.task_id,
        "session": session or _session_key(task),
        "path": str(path),
        "stamp": file_state.stamp_to_wire(file_state.stamp_of(data, 1)),
        "revealable": True,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _held(state: SupervisorState, task: Task) -> list[str]:
    return ledger_of(state).claims.held_by(task.task_id)


class TestTheEndpoints:
    @pytest.mark.parametrize("method,url,body", [
        ("POST", "/v1/file_edits/begin", {"task_id": "t", "path": "/abs/a.txt"}),
        ("POST", "/v1/file_edits/stamp", {"task_id": "t", "path": "/abs/a.txt", "stamp": {}}),
        ("POST", "/v1/file_edits/release", {"task_id": "t", "path": "/abs/a.txt"}),
        ("GET", "/v1/file_edits/scope?task_id=t", None),
        ("GET", "/v1/file_edits/claims", None),
    ])
    def test_every_endpoint_needs_the_admin_token(self, app: Any, method: str, url: str, body: Any) -> None:
        response = _call(app, method, url, auth=False, json=body)

        assert response.status_code == 401

    def test_a_relative_path_is_rejected(self, app: Any) -> None:
        response = _call(app, "POST", "/v1/file_edits/begin", json={"task_id": "", "path": "a.txt"})

        assert response.status_code == 422

    def test_a_malformed_stamp_is_rejected(self, app: Any, tmp_path: Path) -> None:
        response = _call(app, "POST", "/v1/file_edits/stamp", json={
            "task_id": "t", "path": str(tmp_path / "a.txt"), "stamp": {"mtime_ns": 1, "size": 1, "sha256": "short"},
        })

        assert response.status_code == 422

    def test_a_release_without_a_task_is_rejected(self, app: Any, tmp_path: Path) -> None:
        response = _call(app, "POST", "/v1/file_edits/release", json={"task_id": " ", "path": str(tmp_path / "a")})

        assert response.status_code == 422

    def test_two_tasks_of_one_session_cannot_edit_the_same_file(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        session_id, first = _conversation(state, "qq_group:claims")
        second = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        path = tmp_path / "a.txt"

        granted = _begin(app, first, path)
        refused = _begin(app, second, path)
        again = _begin(app, first, path)

        assert granted["ok"] is True and granted["new_claim"] is True
        assert refused["ok"] is False and refused["reason"] == "claimed"
        holder = refused["holder"]
        assert holder["task_id"] == first.task_id and holder["node_id"] == first.node_id
        assert holder["status"] == "running" and holder["session_id"] == session_id and holder["path"] == "a.txt"
        assert again["ok"] is True and again["new_claim"] is False

    def test_claims_hold_across_sessions(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, mine = _conversation(state, "qq_group:claims-a")
        _other, theirs = _conversation(state, "qq_group:claims-b")
        path = tmp_path / "a.txt"
        _begin(app, mine, path)

        assert _begin(app, theirs, path)["reason"] == "claimed"

    def test_an_unknown_or_ended_task_cannot_claim(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, task = _conversation(state, "qq_group:claims-ended")
        _finish(state, task)

        ghost = _call(app, "POST", "/v1/file_edits/begin", json={"task_id": "ghost", "path": str(tmp_path / "a")})

        assert ghost.json() == {"ok": False, "reason": "task_unknown"}
        assert _begin(app, task, tmp_path / "a")["reason"] == "task_ended"

    def test_a_writer_without_a_task_is_only_checked(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, task = _conversation(state, "qq_group:claims-anon")
        path = tmp_path / "a.txt"

        assert _begin(app, None, path, session="sess-anon")["ok"] is True
        assert ledger_of(state).claims.paths() == []
        _begin(app, task, path)
        assert _begin(app, None, path, session="sess-anon")["reason"] == "claimed"

    def test_release_and_the_claim_listing(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, task = _conversation(state, "qq_group:claims-list")
        _begin(app, task, tmp_path / "a.txt")
        _begin(app, task, tmp_path / "b.txt")

        listed = _call(app, "GET", f"/v1/file_edits/claims?task_id={task.task_id}").json()["claims"]
        released = _call(app, "POST", "/v1/file_edits/release", json={
            "task_id": task.task_id, "path": str(tmp_path / "a.txt"),
        }).json()
        released_again = _call(app, "POST", "/v1/file_edits/release", json={
            "task_id": task.task_id, "path": str(tmp_path / "a.txt"),
        }).json()

        assert sorted(row["path"] for row in listed) == sorted(
            file_state.path_key(tmp_path / name) for name in ("a.txt", "b.txt")
        )
        assert listed[0]["holders"][0]["task_id"] == task.task_id
        assert released == {"ok": True, "released": True} and released_again == {"ok": True, "released": False}
        assert _held(state, task) == [file_state.path_key(tmp_path / "b.txt")]

    def test_the_journal_session_of_a_dispatched_child_is_the_root(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        root, entry = _conversation(state, "qq_group:claims-scope")
        child_session, child = _delegate(state, entry, "bob")

        child_scope = _call(app, "GET", f"/v1/file_edits/scope?task_id={child.task_id}").json()
        entry_scope = _call(app, "GET", f"/v1/file_edits/scope?task_id={entry.task_id}").json()
        ghost = _call(app, "GET", "/v1/file_edits/scope?task_id=ghost").json()
        begun = _begin(app, child, tmp_path / "a.txt", session=child_session)

        assert child_session != root
        assert child_scope["ok"] is True and child_scope["journal_session"] == root
        assert child_scope["node_id"] == child.node_id and child_scope["status"] == "running"
        assert entry_scope["journal_session"] == root
        assert ghost == {"ok": False, "reason": "task_unknown"}
        assert begun["journal_session"] == root

    def test_the_journal_session_of_a_wait_child_is_the_root(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        root, caller = _conversation(state, "qq_group:claims-wait-scope")
        child = _wait_for_child(state, caller, "bob")

        assert _call(app, "GET", f"/v1/file_edits/scope?task_id={child.task_id}").json()["journal_session"] == root


class TestReleasingClaims:
    def test_finishing_the_task_releases_its_claims(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        session_id, holder = _conversation(state, "qq_group:release-finish")
        other = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        _begin(app, holder, tmp_path / "a.txt")

        _finish(state, holder)

        assert _held(state, holder) == []
        assert _begin(app, other, tmp_path / "a.txt")["ok"] is True

    def test_cancelling_the_task_releases_its_claims(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, holder = _conversation(state, "qq_group:release-cancel")
        _begin(app, holder, tmp_path / "a.txt")

        assert state.cancel_single_task(holder.task_id)["cancelled_count"] == 1

        assert holder.status == TaskStatus.cancelled
        assert _held(state, holder) == []

    def test_a_cancel_the_worker_has_not_acknowledged_keeps_the_claims(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        # 租约还在时取消只是请求：worker 可能还在写，认领留到它回报
        _sid, holder = _conversation(state, "qq_group:release-cancel-requested")
        holder.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        _begin(app, holder, tmp_path / "a.txt")

        state.cancel_single_task(holder.task_id)

        assert holder.status == TaskStatus.running and holder.cancel_requested
        assert _held(state, holder) == [file_state.path_key(tmp_path / "a.txt")]
        state.complete_task(
            task_id=holder.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "late"}},
        )
        assert holder.status == TaskStatus.cancelled and _held(state, holder) == []

    def test_reaping_the_task_releases_its_claims(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        _sid, holder = _conversation(state, "qq_group:release-reap")
        _begin(app, holder, tmp_path / "a.txt")
        holder.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)

        with state._lock:
            reaped = reap_stuck_tasks_locked(state)

        assert holder.task_id in reaped and holder.status == TaskStatus.failed
        assert _held(state, holder) == []

    def test_stamps_reach_the_next_task_once_the_reader_ends(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        # 下一个任务可能落在另一个 worker 上：读戳在 supervisor 这里，换 worker 照样看得到
        session_id, reader = _conversation(state, "qq_group:stamps-next")
        path = tmp_path / "a.txt"
        assert _stamp(app, reader, path, b"v1\n")["layer"] == "task"
        _finish(state, reader)

        follower = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        reply = _begin(app, follower, path)

        assert reply["stamp"] == file_state.entry_to_wire(file_state.Entry(file_state.stamp_of(b"v1\n", 1), True))
        assert reply["stamp_layer"] == "session" and reply["foreign"] is False

    def test_a_running_task_s_stamps_stay_its_own(self, app: Any, state: SupervisorState, tmp_path: Path) -> None:
        session_id, reader = _conversation(state, "qq_group:stamps-parallel")
        parallel = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        _stamp(app, reader, tmp_path / "a.txt", b"v1\n")

        reply = _begin(app, parallel, tmp_path / "a.txt")

        assert reply["stamp"] is None

    def test_a_change_shared_by_a_parallel_task_is_foreign(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        session_id, writer = _conversation(state, "qq_group:stamps-foreign")
        early = _inbound(state, session_id, entry_node_id="qq.orchestrator")
        # 时钟粒度粗的平台上两次取时可能相同：把它的开始时刻明确拨到写入方结束之前
        early.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        _stamp(app, writer, tmp_path / "a.txt", b"v2\n")
        _finish(state, writer)

        reply = _begin(app, early, tmp_path / "a.txt")

        assert reply["stamp"] is not None and reply["foreign"] is True


class TestSuspendedTasks:
    def test_a_task_suspended_on_a_wait_dispatch_keeps_its_claims(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        _sid, caller = _conversation(state, "qq_group:suspend-hold")
        _other, stranger = _conversation(state, "qq_group:suspend-stranger")
        path = tmp_path / "a.txt"
        _begin(app, caller, path)

        child = _wait_for_child(state, caller, "bob")

        assert caller.status == TaskStatus.suspended
        assert _held(state, caller) == [file_state.path_key(path)]
        refused = _begin(app, stranger, path)
        assert refused["reason"] == "claimed" and refused["holder"]["status"] == "suspended"
        nested = _begin(app, child, path)
        assert nested["ok"] is True and nested["new_claim"] is True

        _finish(state, child)

        assert _held(state, child) == []
        assert caller.status == TaskStatus.pending
        assert _held(state, caller) == [file_state.path_key(path)]
        assert _begin(app, stranger, path)["holder"]["task_id"] == caller.task_id

    def test_a_dispatch_on_its_way_to_suspension_keeps_its_claims(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        _sid, caller = _conversation(state, "qq_group:suspend-transit")
        _begin(app, caller, tmp_path / "a.txt")
        caller.status = TaskStatus.completed
        caller.result = {"action": "dispatch", "target_node": "bob"}
        caller.route_status = RouteStatus.pending
        caller.updated_at = datetime.now(timezone.utc)

        with state._lock:
            release_if_ended_locked(state, caller)
        assert _held(state, caller) == [file_state.path_key(tmp_path / "a.txt")]

        caller.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        with state._lock:
            release_if_ended_locked(state, caller)
        assert _held(state, caller) == []

    def test_a_routed_dispatch_that_ended_up_terminal_releases(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        _sid, caller = _conversation(state, "qq_group:suspend-routed")
        _begin(app, caller, tmp_path / "a.txt")
        caller.status = TaskStatus.completed
        caller.result = {"action": "dispatch", "target_node": "bob"}
        caller.route_status = RouteStatus.routed
        caller.updated_at = datetime.now(timezone.utc)

        with state._lock:
            release_if_ended_locked(state, caller)

        assert _held(state, caller) == []


def _tool_context(http: httpx.AsyncClient, *, task_id: str, session: str, base_url: str) -> ToolContext:
    return ToolContext(
        supervisor_url=base_url, session_id=session, run_id="run-1", worker_id="worker",
        workspace_root=Path("."), http=http, registry=None, task_id=task_id,
    )


class TestTheEngineSide:
    """ToolContext 的四个方法：请求怎么发、回复怎么收，对着真的 supervisor app 走一遍。"""

    def test_the_tool_context_talks_to_the_real_endpoints(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        session_id, task = _conversation(state, "qq_group:engine-side")
        key = file_state.path_key(tmp_path / "a.txt")
        stamp = file_state.stamp_to_wire(file_state.stamp_of(b"v1\n", 1))

        async def go() -> tuple[dict[str, Any], ...]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=_AUTH) as http:
                ctx = _tool_context(http, task_id=task.task_id, session=session_id, base_url="http://test")
                recorded = await ctx.record_file_stamp(key, session=session_id, stamp=stamp, revealable=True)
                begun = await ctx.begin_file_edit(key, display="a.txt", session=session_id)
                scope = await ctx.file_edit_scope()
                released = await ctx.release_file_claim(key)
                return recorded, begun, scope, released

        recorded, begun, scope, released = asyncio.run(go())

        assert recorded == {"ok": True, "layer": "task"}
        assert begun["ok"] is True and begun["new_claim"] is True and begun["stamp_layer"] == "task"
        assert begun["stamp"]["sha256"] == stamp["sha256"] and begun["journal_session"] == session_id
        assert scope["ok"] is True and scope["journal_session"] == session_id
        assert released == {"ok": True, "released": True}
        assert _held(state, task) == []

    def test_the_requests_carry_the_task_and_the_session(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        async def go() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                ctx = _tool_context(http, task_id="task-1", session="branch-1", base_url="http://sup")
                await ctx.begin_file_edit("/abs/a.txt", display="a.txt", session="main-1")
                await ctx.record_file_stamp("/abs/a.txt", session="main-1", stamp={"x": 1}, revealable=False)
                await ctx.release_file_claim("/abs/a.txt")
                await ctx.file_edit_scope()

        asyncio.run(go())

        assert [(request.method, request.url.path) for request in seen] == [
            ("POST", "/v1/file_edits/begin"), ("POST", "/v1/file_edits/stamp"),
            ("POST", "/v1/file_edits/release"), ("GET", "/v1/file_edits/scope"),
        ]
        begin, stamp, release, scope = seen
        assert json.loads(begin.content) == {
            "task_id": "task-1", "session": "main-1", "path": "/abs/a.txt", "display": "a.txt",
        }
        assert json.loads(stamp.content) == {
            "task_id": "task-1", "session": "main-1", "path": "/abs/a.txt", "stamp": {"x": 1}, "revealable": False,
        }
        assert json.loads(release.content) == {"task_id": "task-1", "path": "/abs/a.txt"}
        assert scope.url.params["task_id"] == "task-1"

    def test_a_reply_that_is_not_an_object_or_an_http_error_raises(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/begin"):
                return httpx.Response(200, json=["not", "an", "object"])
            return httpx.Response(503, json={"detail": "down"})

        async def go() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                ctx = _tool_context(http, task_id="task-1", session="s", base_url="http://sup")
                with pytest.raises(ValueError):
                    await ctx.begin_file_edit("/abs/a.txt", display="a.txt", session="s")
                with pytest.raises(httpx.HTTPStatusError):
                    await ctx.file_edit_scope()
                with pytest.raises(httpx.HTTPStatusError):
                    await ctx.record_file_stamp("/abs/a.txt", session="s", stamp={}, revealable=True)

        asyncio.run(go())


class TestWithoutTheLedger:
    def test_terminal_hooks_are_harmless_before_the_ledger_exists(self, state: SupervisorState) -> None:
        _sid, task = _conversation(state, "qq_group:no-ledger")

        state.cancel_single_task(task.task_id)

        assert task.status == TaskStatus.cancelled
        assert getattr(state, "file_ledger", None) is None

    def test_the_ledger_is_created_once_per_supervisor(self, state: SupervisorState) -> None:
        assert ledger_of(state) is ledger_of(state)
