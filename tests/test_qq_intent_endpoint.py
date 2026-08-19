"""意愿判定端点：建一个 node task 等 engine 答完，等不到就放行。

supervisor 从不直接调模型，所以这条路是「建任务 → 在 HTTP 请求里等 → 结果从
task 路由旁路回来」。三件事必须钉死：
1. 判定任务不产生任何用户可见输出，也不触发轮摘要 / 记忆提取。
2. 超时要连任务一起取消 —— 留着就会一直占 engine worker。
3. 在飞上限之上不排队，直接判不接话。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import TaskStatus  # noqa: E402

_URL = "/v1/qq/intent/judge"
_TOKEN = "test-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch):
    # 判定端点每次调用都建一个 LLM 任务，所以它和相邻 admin 路由一样要 token。
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")


def _make_state(workspace: Path) -> SupervisorState:
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-qq-intent")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _app(state: SupervisorState, tmp_path: Path):
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )


def _body(**extra: Any) -> dict[str, Any]:
    body = {
        "conversation_key": "qq_group:hash-abc",
        "text": "那这样行吗",
        "context_lines": ["[12:00] 小明(user_a1): 在吗"],
        "bot_names": ["咪啪"],
        "speaker": "小明",
        "timeout_sec": 2.0,
    }
    body.update(extra)
    return body


def _pending_intent_tasks(state: SupervisorState) -> list[Any]:
    """还没被做掉的判定任务。已完成的留在 tasks 里，按标记捞会捞到上一轮那个。"""
    return [
        t for t in state.tasks.values()
        if t.input.get("_qq_intent") and t.status not in {
            TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled,
        }
    ]


def _judge(app, state: SupervisorState, body: dict[str, Any], *, answer: str | None = "yes|在追问",
           action: str = "finish") -> httpx.Response:
    """发一次判定请求，并在任务出现后立刻替 engine worker 把它做完。"""

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            request = asyncio.create_task(client.post(_URL, json=body, headers=_AUTH))
            if answer is not None or action != "finish":
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    pending = _pending_intent_tasks(state)
                    if pending:
                        result: dict[str, Any] = {"action": action}
                        if answer is not None:
                            result["result"] = {"text": answer}
                        state.complete_task(
                            task_id=pending[0].task_id, worker_id="worker-1", result=result,
                        )
                        break
            return await request

    return asyncio.run(_run())


class TestTheHappyPath:
    def test_a_yes_comes_back_as_agreed(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        response = _judge(_app(state, tmp_path), state, _body())

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["agreed"] is True
        assert payload["decided"] is True
        assert payload["reason"] == "在追问"
        assert payload["task_id"]

    def test_a_no_comes_back_as_not_agreed(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        response = _judge(_app(state, tmp_path), state, _body(), answer="no|群友之间在对话")

        payload = response.json()
        assert payload["agreed"] is False
        assert payload["decided"] is True

    def test_a_failed_task_is_reported_as_undecided(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        response = _judge(_app(state, tmp_path), state, _body(), answer=None, action="fail")

        payload = response.json()
        assert payload["agreed"] is False
        assert payload["decided"] is False

    def test_the_task_carries_the_configured_node_and_the_instruction(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _judge(_app(state, tmp_path), state, _body(node_id="qq.custom_intent"))

        task = next(t for t in state.tasks.values() if t.input.get("_qq_intent"))
        assert task.node_id == "qq.custom_intent"
        assert "那这样行吗" in task.input["instruction"]
        assert "咪啪" in task.input["instruction"]

    def test_the_task_is_a_system_task_on_a_fresh_child_session(self, tmp_path: Path) -> None:
        # use_context 必须是 False：判定要看的是群聊原文，不是这个会话的推理历史，
        # 带上历史等于每条群消息都按整段上下文计费。
        state = _make_state(tmp_path)
        _judge(_app(state, tmp_path), state, _body())

        task = next(t for t in state.tasks.values() if t.input.get("_qq_intent"))
        assert task.input["_system_task"] is True
        assert task.input["use_context"] is False
        assert task.input["context_mode"] == "fresh"
        assert task.input["child_session_id"]

    def test_the_session_is_shared_with_the_real_conversation(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        existing = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:hash-abc")

        _judge(_app(state, tmp_path), state, _body())

        task = next(t for t in state.tasks.values() if t.input.get("_qq_intent"))
        assert task.session_id == existing

    def test_a_missing_conversation_key_is_rejected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(_URL, json={"text": "在吗"}, headers=_AUTH)

        assert asyncio.run(_run()).status_code == 400

    def test_an_empty_text_is_rejected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(_URL, json={"conversation_key": "qq_group:x", "text": "  "}, headers=_AUTH)

        assert asyncio.run(_run()).status_code == 400


class TestAuth:
    def _post(self, app, body: dict[str, Any], headers: dict[str, str] | None) -> httpx.Response:
        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(_URL, json=body, headers=headers or {})

        return asyncio.run(_run())

    def test_no_token_is_rejected(self, tmp_path: Path) -> None:
        # 裸奔的话，任何人都能拿这个端点建 LLM 任务、把 engine worker 占满。
        state = _make_state(tmp_path)
        response = self._post(_app(state, tmp_path), _body(), None)

        assert response.status_code == 401
        assert [t for t in state.tasks.values() if t.input.get("_qq_intent")] == []

    def test_a_wrong_token_is_rejected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        response = self._post(_app(state, tmp_path), _body(), {"Authorization": "Bearer nope"})

        assert response.status_code == 401
        assert [t for t in state.tasks.values() if t.input.get("_qq_intent")] == []

    def test_the_query_token_also_works(self, tmp_path: Path) -> None:
        # verify_admin_token 同时认 ?token=，bot 侧走 Bearer，这里只确认没被收窄。
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                request = asyncio.create_task(
                    client.post(f"{_URL}?token={_TOKEN}", json=_body(timeout_sec=2.0)),
                )
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    pending = _pending_intent_tasks(state)
                    if pending:
                        state.complete_task(
                            task_id=pending[0].task_id, worker_id="worker-1",
                            result={"action": "finish", "result": {"text": "yes"}},
                        )
                        break
                return await request

        assert asyncio.run(_run()).status_code == 200


class TestTheJudgementNeverReachesTheUser:
    def test_no_outbound_is_produced(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _judge(_app(state, tmp_path), state, _body())

        session_id = next(t for t in state.tasks.values() if t.input.get("_qq_intent")).session_id
        outbound = [
            e for e in state.eventlog.list_all_events()
            if str(e.get("type") or "") == "outbound_message" and e.get("session_id") == session_id
        ]
        assert outbound == []

    def test_no_post_work_phases_are_planned(self, tmp_path: Path) -> None:
        # 每条不接话的群消息跑一次判定。记忆提取和轮摘要各自会短路，但 phase 本身
        # 要写两条 eventlog，活跃群一天几千条全是这个。
        state = _make_state(tmp_path)
        _judge(_app(state, tmp_path), state, _body())

        task = next(t for t in state.tasks.values() if t.input.get("_qq_intent"))
        assert [p for p in (task.post_work_phases or []) if p.get("kind") in {"hook", "turn_summary"}] == []


class TestTimeout:
    def _timed_out(self, tmp_path: Path) -> tuple[SupervisorState, httpx.Response]:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(_URL, json=_body(timeout_sec=1.0), timeout=10.0, headers=_AUTH)

        return state, asyncio.run(_run())

    def test_a_timeout_answers_not_agreed(self, tmp_path: Path) -> None:
        _state, response = self._timed_out(tmp_path)

        payload = response.json()
        assert payload["agreed"] is False
        assert payload["decided"] is False
        assert payload["error"] == "timeout"

    def test_a_timed_out_task_is_cancelled(self, tmp_path: Path) -> None:
        # 留着不管，模型慢的时候判定任务会一直堆积，把 engine worker 全占住。
        state, _response = self._timed_out(tmp_path)

        task = next(t for t in state.tasks.values() if t.input.get("_qq_intent"))
        assert task.status == TaskStatus.cancelled or task.cancel_requested

    def test_a_timeout_frees_the_conversation_slot(self, tmp_path: Path) -> None:
        state, _response = self._timed_out(tmp_path)

        assert state.qq_intent.inflight() == 0
        assert state.qq_intent.has_conversation("qq_group:hash-abc") is False


class TestTheInflightGate:
    def test_a_second_request_for_the_same_group_is_refused(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> dict[str, Any]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = asyncio.create_task(client.post(_URL, json=_body(timeout_sec=2.0), headers=_AUTH))
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    if _pending_intent_tasks(state):
                        break
                second = await client.post(_URL, json=_body(timeout_sec=2.0), headers=_AUTH)
                state.complete_task(
                    task_id=_pending_intent_tasks(state)[0].task_id,
                    worker_id="worker-1",
                    result={"action": "finish", "result": {"text": "yes"}},
                )
                await first
                return second.json()

        payload = asyncio.run(_run())
        assert payload["decided"] is False
        assert payload["error"] == "conversation_busy"
        assert payload["agreed"] is False
        # 被挡住的那次不该留下任务，否则闸门本身就成了任务源。
        assert len([t for t in state.tasks.values() if t.input.get("_qq_intent")]) == 1

    def test_a_different_group_is_refused_once_the_global_cap_is_reached(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> dict[str, Any]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = asyncio.create_task(client.post(_URL, json=_body(timeout_sec=2.0), headers=_AUTH))
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    if _pending_intent_tasks(state):
                        break
                second = await client.post(
                    _URL, json=_body(conversation_key="qq_group:hash-other", timeout_sec=2.0),
                    headers=_AUTH,
                )
                state.complete_task(
                    task_id=_pending_intent_tasks(state)[0].task_id,
                    worker_id="worker-1",
                    result={"action": "finish", "result": {"text": "yes"}},
                )
                await first
                return second.json()

        payload = asyncio.run(_run())
        assert payload["error"] == "max_inflight"
        assert payload["agreed"] is False

    def test_a_higher_cap_lets_another_group_through(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        async def _run() -> list[dict[str, Any]]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = asyncio.create_task(
                    client.post(_URL, json=_body(timeout_sec=3.0, max_inflight=2), headers=_AUTH),
                )
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    if _pending_intent_tasks(state):
                        break
                second = asyncio.create_task(client.post(
                    _URL, json=_body(conversation_key="qq_group:hash-other", timeout_sec=3.0, max_inflight=2),
                    headers=_AUTH,
                ))
                for _ in range(400):
                    await asyncio.sleep(0.005)
                    if len(_pending_intent_tasks(state)) >= 2:
                        break
                for task in _pending_intent_tasks(state):
                    state.complete_task(
                        task_id=task.task_id, worker_id="worker-1",
                        result={"action": "finish", "result": {"text": "yes"}},
                    )
                return [r.json() for r in await asyncio.gather(first, second)]

        payloads = asyncio.run(_run())
        assert [p["decided"] for p in payloads] == [True, True]

    def test_the_slot_is_released_after_a_normal_answer(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        _judge(app, state, _body())
        assert state.qq_intent.inflight() == 0

        second = _judge(app, state, _body(), answer="no")
        assert second.json()["decided"] is True
