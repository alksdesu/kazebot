"""到点收尾的入口任务：交付时说明接下来怎么办，在同一会话按轮数和总寿命自动接着做；委派出去的子任务把收尾交还父节点，不自己续。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from engine.node import load_node  # noqa: E402
from engine.run_budget import (  # noqa: E402
    AUTO_CONTINUE_KEY,
    STEP_LIMIT_NOTICE,
    TASK_LIFETIME_FACTOR,
    TIME_LIMIT_NOTICE,
)
from supervisor import auto_continue  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.task_reaper import reap_reason  # noqa: E402
from supervisor.task_traits import node_auto_continues  # noqa: E402
from supervisor.types import InboundMessageIn, Task, TaskKind, TaskStatus  # noqa: E402

CONV = "qq_private:42"
WORKER = "worker.coder"
CHAT = "chat.entry"
AUTH = {"platform": "qq", "user_id": "u-1", "is_admin": False}
HINTS = {"reply_message_id": "m-1"}
PLAN = [
    {"content": "读需求", "status": "completed"},
    {"content": "改代码", "status": "in_progress"},
    {"content": "补测试", "status": "pending"},
]
DONE = [{"content": "读需求", "status": "completed"}]
_MAX = 3600.0


@pytest.fixture(autouse=True)
def _default_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", raising=False)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / f"{WORKER}.yaml").write_text("type: ai\nname: worker\nauto_continue: true\nprompt: Work.\n", encoding="utf-8")
    (nodes / f"{CHAT}.yaml").write_text("type: ai\nname: chat\nprompt: Chat.\n", encoding="utf-8")
    return tmp_path


def _runtime(workspace: Path, body: str) -> None:
    (workspace / "config" / "runtime.yaml").write_text(body, encoding="utf-8")


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-auto-continue"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _session(state: SupervisorState) -> str:
    return state.get_or_create_session(channel="qq_private", conversation_key=CONV)


def _inbound(state: SupervisorState, session_id: str, *, node: str = WORKER, text: str = "把导出模块重写一遍", **extra: Any) -> Task:
    payload = {
        "channel": "qq_private",
        "conversation_key": state.sessions[session_id].conversation_key,
        "text": text,
        "entry_node_id": node,
        "platform_auth": dict(AUTH),
        "route_hints": dict(HINTS),
        **extra,
    }
    event = state.eventlog.append(session_id=session_id, component="shell", type_="inbound_message", payload=payload)
    state.record_inbound_message_event(event)
    return next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])


def _wrapup(text: str = "改完了导出的前半部分", *, reason: str = "steps", todos: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """engine 到点收尾时交回的结果：正文末尾带「任务到此停止」，打上收尾标记。"""
    items = [dict(item) for item in todos or []]
    notice = TIME_LIMIT_NOTICE if reason == "time" else STEP_LIMIT_NOTICE
    flag = "time_limit_reached" if reason == "time" else "step_limit_reached"
    return {
        "action": "finish",
        "result": {
            "text": f"{text}\n\n{notice}",
            flag: True,
            "wrapup": {
                "reason": reason, "steps_used": 150, "max_steps": 150, "todos": items,
                "unfinished": sum(1 for item in items if item["status"] != "completed"),
            },
        },
        "_step_count": 150,
    }


def _report(state: SupervisorState, task: Task, result: dict[str, Any]) -> None:
    task.status = TaskStatus.running
    task.worker_id = "engine-0"
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    state.complete_task(task_id=task.task_id, worker_id="engine-0", result=result)


def _continuations(state: SupervisorState) -> list[Task]:
    return [
        task for task in state.tasks.values()
        if task.input.get("inbound_message_type") == auto_continue.AUTO_CONTINUE_MESSAGE_TYPE
    ]


def _outbound_texts(state: SupervisorState) -> list[str]:
    return [
        str((event.get("payload") or {}).get("text") or "")
        for event in state.eventlog.iter_persisted_events()
        if event.get("type") == "outbound_message"
    ]


class TestEntryTasks:
    def test_a_wrapped_up_task_says_so_and_the_next_round_starts_in_the_same_session(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _inbound(state, sid)

        _report(state, first, _wrapup(todos=PLAN))

        assert _outbound_texts(state)[-1] == "改完了导出的前半部分\n\n（这一段到了步数上限，接下来自动接着做：第 1/3 轮）"
        [second] = _continuations(state)
        assert second.status == TaskStatus.pending
        assert second.node_id == WORKER
        assert second.input["parent_session_id"] == sid
        assert second.session_id.startswith("branch_") and second.session_id != first.session_id
        assert second.input[AUTO_CONTINUE_KEY] == {
            "round": 1,
            "max_rounds": 3,
            "chain_started_at": first.created_at.isoformat(),
            "origin_task_id": first.task_id,
            "previous_task_id": first.task_id,
            "reason": "steps",
            "todos": PLAN,
        }
        context = second.input["task_context"]
        assert context["platform_auth"] == AUTH and context["route_hints"] == HINTS

    def test_the_next_round_is_told_where_the_last_one_stopped(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))

        _report(state, first, _wrapup(todos=PLAN))

        [second] = _continuations(state)
        assert second.input["instruction"] == "\n".join([
            "[自动续跑 第 1/3 轮] 上一段到了步数上限，先停了下来，现在接着做同一个任务。",
            "上一段最后交代的进度：",
            "改完了导出的前半部分",
            "待办清单：",
            "1. [已完成] 读需求",
            "2. [进行中] 改代码",
            "3. [待办] 补测试",
            "先按上面的进度和待办核对实际做到了哪里，再接着做没完成的部分，做的过程中用 update_todos 更新清单。"
            "如果其实已经全部做完，直接用 finish 说明结果。",
        ])

    def test_the_next_round_sees_the_history_of_the_last_one(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _inbound(state, sid)
        store = ConversationStore(workspace / "data" / "conversations")
        store.append_batch(first.session_id, [
            Message(id="m-work", role="assistant", content="改了 export.py 的前半部分", source_task_id=first.task_id),
        ])

        _report(state, first, _wrapup())

        [second] = _continuations(state)
        assert "m-work" in [message.id for message in store.load(sid)]
        assert "m-work" in [message.id for message in store.load(second.session_id)]

    def test_the_chain_ends_when_a_round_finishes_the_work(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))
        _report(state, first, _wrapup(todos=PLAN))
        [second] = _continuations(state)

        _report(state, second, {"action": "finish", "result": {"text": "导出模块重写完了，测试也补上了"}})

        assert len(_continuations(state)) == 1
        assert second.status == TaskStatus.completed
        assert _outbound_texts(state)[-2:] == [
            "改完了导出的前半部分\n\n（这一段到了步数上限，接下来自动接着做：第 1/3 轮）",
            "导出模块重写完了，测试也补上了",
        ]

    def test_a_time_wrapup_names_the_run_time(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))

        _report(state, first, _wrapup(reason="time"))

        assert _outbound_texts(state)[-1].endswith("（这一段到了运行时长上限，接下来自动接着做：第 1/3 轮）")
        [second] = _continuations(state)
        assert second.input[AUTO_CONTINUE_KEY]["reason"] == "time"
        assert second.input["instruction"].startswith("[自动续跑 第 1/3 轮] 上一段到了运行时长上限")

    def test_rounds_are_counted_along_the_chain_and_stop_at_the_limit(self, workspace: Path) -> None:
        _runtime(workspace, "engine:\n  auto_continue:\n    max_rounds: 2\n")
        state = _state(workspace)
        first = _inbound(state, _session(state))

        _report(state, first, _wrapup("第一段"))
        [second] = _continuations(state)
        _report(state, second, _wrapup("第二段"))
        third = next(task for task in _continuations(state) if task.task_id != second.task_id)
        _report(state, third, _wrapup("第三段"))

        assert len(_continuations(state)) == 2
        chain = third.input[AUTO_CONTINUE_KEY]
        assert chain["round"] == 2 and chain["max_rounds"] == 2
        assert chain["origin_task_id"] == first.task_id and chain["previous_task_id"] == second.task_id
        assert chain["chain_started_at"] == first.created_at.isoformat()
        assert _outbound_texts(state)[-3:] == [
            "第一段\n\n（这一段到了步数上限，接下来自动接着做：第 1/2 轮）",
            "第二段\n\n（这一段到了步数上限，接下来自动接着做：第 2/2 轮）",
            "第三段\n\n（已经自动接着做了 2 轮，先停在这里。需要的话说「继续」接着做。）",
        ]

    def test_zero_rounds_turns_it_off(self, workspace: Path) -> None:
        _runtime(workspace, "engine:\n  auto_continue:\n    max_rounds: 0\n")
        state = _state(workspace)
        first = _inbound(state, _session(state))

        _report(state, first, _wrapup())

        assert _continuations(state) == []
        assert _outbound_texts(state)[-1] == f"改完了导出的前半部分\n\n{STEP_LIMIT_NOTICE}"

    def test_a_chain_near_the_end_of_its_lifetime_stops(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))
        # 总寿命只剩 10 分钟，不够一段跑完再留出 6 分钟收尾
        first.created_at = datetime.now(timezone.utc) - timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR - 600)

        _report(state, first, _wrapup())

        assert _continuations(state) == []
        assert _outbound_texts(state)[-1] == (
            "改完了导出的前半部分\n\n（这项任务累计运行的时间快到上限了，先停在这里。需要的话说「继续」接着做。）"
        )

    def test_a_node_without_auto_continue_stops_when_its_list_is_done(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state), node=CHAT)

        _report(state, first, _wrapup(todos=DONE))

        assert _continuations(state) == []
        assert _outbound_texts(state)[-1] == f"改完了导出的前半部分\n\n{STEP_LIMIT_NOTICE}"

    def test_unfinished_todos_continue_any_node_on_the_same_node(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _inbound(state, sid, node=CHAT)
        state.session_entry_overrides[sid] = WORKER

        _report(state, first, _wrapup(todos=PLAN))

        [second] = _continuations(state)
        assert second.node_id == CHAT

    def test_a_plain_finish_changes_nothing(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))

        _report(state, first, {"action": "finish", "result": {"text": "全部做完了"}})

        assert _continuations(state) == []
        assert _outbound_texts(state)[-1] == "全部做完了"

    def test_a_cancelled_conversation_does_not_continue(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        first = _inbound(state, sid)
        state._cancelled_sessions.add(sid)

        _report(state, first, _wrapup())

        assert _continuations(state) == []

    def test_the_short_reply_request_and_the_schedule_are_handled(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(
            state, _session(state), schedule_id="daily-report",
            route_hints={"reply_message_id": "m-1", "response_action": "short_reply"},
        )

        _report(state, first, _wrapup())

        [second] = _continuations(state)
        # 续跑那一轮要正常干活，不是简短回应；定时任务续跑仍算定时任务
        assert second.input["task_context"]["route_hints"] == {"reply_message_id": "m-1"}
        assert second.input["schedule_id"] == "daily-report"

    def test_starting_the_same_round_twice_creates_one_task(self, workspace: Path) -> None:
        state = _state(workspace)
        first = _inbound(state, _session(state))
        _report(state, first, _wrapup())
        plan = auto_continue.ContinuePlan(go=True, note="", round=1, max_rounds=3)

        with state._lock:
            again = auto_continue.start_locked(state, first, plan)

        assert again is None
        assert len(_continuations(state)) == 1


def _caller(state: SupervisorState, session_id: str) -> Task:
    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=1,
            kind=TaskKind.node,
            node_id="test.orchestrator",
            input_data={"task_context": {"conversation_key": CONV, "channel": "qq_private", "platform_auth": dict(AUTH)}},
            continuation={},
            source_inbound_seq=None,
        )


class TestDelegatedChildren:
    def test_a_wait_child_hands_its_wrapup_back_to_the_caller(self, workspace: Path) -> None:
        state = _state(workspace)
        caller = _caller(state, _session(state))
        with state._lock:
            caller.status = TaskStatus.completed
            state._route_dispatch_locked(caller, {"action": "dispatch", "dispatch_batch": [
                {"kind": "node", "target": WORKER, "instruction": "重写导出", "context_mode": "fresh", "call_id": "w1"},
            ]})
        [child] = [task for task in state.tasks.values() if task.caller_task_id == caller.task_id]

        _report(state, child, _wrapup(todos=PLAN))

        assert _continuations(state) == []
        assert caller.status == TaskStatus.pending
        [entry] = caller.input["resume_data"]["entries"]
        assert entry["status"] == "finish"
        assert entry["wrapup"] == "steps" and entry["continuable"] is True
        assert entry["handle"] == f"{child.task_id}@{child.input['child_session_id']}"

    def test_an_async_child_hands_its_wrapup_back_through_the_callback(self, workspace: Path) -> None:
        state = _state(workspace)
        sid = _session(state)
        caller = _inbound(state, sid, node=CHAT)
        agent_sid = state.get_or_create_session(channel="qq_private", conversation_key=f"agent:{WORKER}:{CONV}:x1")
        child = _inbound(
            state, agent_sid, node=WORKER, text="重写导出",
            dispatch_context_mode="fresh",
            dispatch_origin={
                "parent_session_id": sid, "caller_node_id": CHAT, "parent_conversation_key": CONV,
                "context_mode": "fresh", "caller_task_id": caller.task_id,
            },
        )

        _report(state, child, _wrapup(reason="time"))

        assert _continuations(state) == []
        [callback] = [task for task in state.tasks.values() if task.input.get("inbound_message_type") == "dispatch_result"]
        assert callback.node_id == CHAT
        assert callback.input["inbound_child_handle"] == f"{child.task_id}@{agent_sid}"
        assert callback.input["inbound_child_continuable"] is True
        assert callback.input["inbound_child_wrapup"] == "time"


class TestChainRecord:
    def test_the_record_is_rebuilt_from_known_fields_only(self) -> None:
        started = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        chain = auto_continue.chain_input({
            "round": "2", "max_rounds": 1, "chain_started_at": "2026-03-01T12:00:00Z",
            "origin_task_id": "o" * 80, "previous_task_id": "p1", "reason": "bored",
            "todos": PLAN, "extra": "dropped",
        })

        assert chain == {
            "round": 2,
            "max_rounds": 2,
            "chain_started_at": started.isoformat(),
            "origin_task_id": "o" * 64,
            "previous_task_id": "p1",
            "reason": "steps",
            "todos": PLAN,
        }

    @pytest.mark.parametrize("raw", [
        pytest.param(None, id="none"),
        pytest.param("round 2", id="text"),
        pytest.param({"round": 0, "chain_started_at": "2026-03-01T12:00:00Z"}, id="round-zero"),
        pytest.param({"round": "x", "chain_started_at": "2026-03-01T12:00:00Z"}, id="round-not-a-number"),
        pytest.param({"round": 1}, id="no-start"),
        pytest.param({"round": 1, "chain_started_at": "soon"}, id="unreadable-start"),
    ])
    def test_anything_malformed_is_dropped_whole(self, raw: Any) -> None:
        assert auto_continue.chain_input(raw) is None

    def test_invalid_todos_are_dropped(self) -> None:
        chain = auto_continue.chain_input({
            "round": 1, "chain_started_at": "2026-03-01T12:00:00Z", "todos": [{"content": "", "status": "pending"}],
        })

        assert chain is not None and chain["todos"] == []

    def test_external_callers_cannot_send_a_chain(self) -> None:
        message = InboundMessageIn(conversation_key=CONV, text="hi", auto_continue={"round": 1})  # type: ignore[call-arg]

        assert "auto_continue" not in message.model_dump()

    @pytest.mark.parametrize("config,rounds", [
        pytest.param({}, 3, id="default"),
        pytest.param({"engine": {"auto_continue": {"max_rounds": 5}}}, 5, id="configured"),
        pytest.param({"engine": {"auto_continue": {"max_rounds": 99}}}, 20, id="capped"),
        pytest.param({"engine": {"auto_continue": {"max_rounds": -1}}}, 0, id="negative"),
        pytest.param({"engine": {"auto_continue": {"max_rounds": "x"}}}, 3, id="not-a-number"),
        pytest.param(None, 3, id="not-a-dict"),
    ])
    def test_the_round_limit_is_read_and_clamped(self, config: Any, rounds: int) -> None:
        assert auto_continue.max_rounds(config) == rounds


class TestDeliveredText:
    @pytest.mark.parametrize("notice", [STEP_LIMIT_NOTICE, TIME_LIMIT_NOTICE])
    def test_the_stop_notice_is_replaced(self, notice: str) -> None:
        result = {"text": f"做了一半\n\n{notice}", "step_limit_reached": True}

        delivered = auto_continue.with_note(result, "（接着做）")

        assert delivered["text"] == "做了一半\n\n（接着做）"
        assert result["text"] == f"做了一半\n\n{notice}"
        assert delivered["step_limit_reached"] is True

    def test_text_without_a_notice_gets_the_note_appended(self) -> None:
        assert auto_continue.with_note({"text": "做了一半"}, "（接着做）")["text"] == "做了一半\n\n（接着做）"

    def test_an_empty_result_is_just_the_note(self) -> None:
        assert auto_continue.with_note({}, "（接着做）") == {"text": "（接着做）"}
        assert auto_continue.with_note(None, "（接着做）") == {"text": "（接着做）"}

    def test_no_note_leaves_the_result_alone(self) -> None:
        assert auto_continue.with_note({"text": "x"}, "") == {"text": "x"}

    def test_a_long_wrapup_is_quoted_by_its_beginning(self) -> None:
        plan = auto_continue.ContinuePlan(go=True, note="", round=1, max_rounds=3, wrapup_text="字" * 2500)

        text = auto_continue.continuation_text(plan)

        assert "字" * 2000 + "…（完整的收尾说明见上一条回复）" in text
        assert "字" * 2001 not in text
        assert "待办清单" not in text


class TestLifetimeAndNodes:
    def test_the_reaper_counts_the_lifetime_from_the_start_of_the_chain(self) -> None:
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        chain_start = now - timedelta(seconds=_MAX * TASK_LIFETIME_FACTOR + 60)

        def _task(started: datetime) -> Task:
            return Task(
                task_id="t-1", session_id="s-1", kind=TaskKind.node, node_id=WORKER, status=TaskStatus.running,
                created_at=now - timedelta(minutes=1), updated_at=now, claimed_at=now - timedelta(minutes=1),
                lease_expires_at=now + timedelta(minutes=2),
                input={AUTO_CONTINUE_KEY: {"round": 2, "chain_started_at": started.isoformat()}},
            )

        reason = reap_reason(_task(chain_start), now=now, max_running_sec=_MAX, approval_pending=False)

        assert reason is not None and "总上限" in reason
        assert reap_reason(_task(now - timedelta(hours=1)), now=now, max_running_sec=_MAX, approval_pending=False) is None

    def test_the_shipped_coder_runs_long_and_continues_on_its_own(self, tmp_path: Path) -> None:
        node = load_node(tmp_path, "bootstrap.coder")

        assert node is not None and node.extra.get("max_steps") == 150
        assert node_auto_continues(tmp_path, "bootstrap.coder") is True

    def test_only_nodes_that_declare_it_continue_on_their_own(self, workspace: Path) -> None:
        assert node_auto_continues(workspace, WORKER) is True
        assert node_auto_continues(workspace, CHAT) is False
        assert node_auto_continues(workspace, "missing.node") is False
        assert node_auto_continues(workspace, "") is False
