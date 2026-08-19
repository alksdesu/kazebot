"""后台静默压缩的会话寻址：child session 的历史也必须压得动。

child session 从不进 SupervisorState.sessions，而 _compact_target_session_id 优先
返回 child_session_id —— 端点直接按 target 查 sessions 就会 404，等于 dispatch 出去的
每个子节点都只剩硬阈值可用。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import TaskStatus  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-compact-async")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


class TestAnchorResolution:
    def test_a_real_session_resolves_to_itself(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:anchor-self")

        with state._lock:
            assert state.anchor_session_for_locked(sid) == sid

    def test_a_child_session_resolves_to_its_parent(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:anchor-child")
        child, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")

        assert child not in state.sessions
        with state._lock:
            assert state.anchor_session_for_locked(child) == parent

    def test_a_nested_child_resolves_to_the_root_anchor(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:anchor-nested")
        child, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")
        grandchild, _ = state.get_or_create_child_session(child, "grand.node", "case", "fresh")

        with state._lock:
            assert state.anchor_session_for_locked(grandchild) == parent

    def test_an_unknown_session_resolves_to_empty(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)

        with state._lock:
            assert state.anchor_session_for_locked("no-such-session") == ""
            assert state.anchor_session_for_locked("") == ""

    def test_a_cycle_cannot_spin_forever(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)

        with state._lock:
            state.parent_children = {"a": {"b"}, "b": {"a"}}
            assert state.anchor_session_for_locked("a") == ""


def _post(app, body: dict) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/v1/tasks/compact-async", json=body)

    return asyncio.run(_run())


def _app(state: SupervisorState, tmp_path: Path):
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )


class TestCompactAsyncEndpoint:
    def _body(self, target: str, **extra) -> dict:
        body = {
            "target_session_id": target,
            "instruction": "summarize the old turns",
            "caller_node_id": "qq.orchestrator",
        }
        body.update(extra)
        return body

    def test_a_child_session_target_is_accepted(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:compact-child")
        child, _ = state.get_or_create_child_session(parent, "bootstrap.executor", "case", "accumulate")

        response = _post(app, self._body(child))

        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        assert task_id
        task = state.tasks[task_id]
        # task 挂在 anchor 上，但压缩结果要落回 child 自己的历史。
        assert task.session_id == parent
        assert task.input["_compact_target_session_id"] == child

    def test_a_plain_session_target_still_works(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-plain")

        response = _post(app, self._body(sid))

        assert response.status_code == 200, response.text
        task = state.tasks[response.json()["task_id"]]
        assert task.session_id == sid
        assert task.input["_compact_target_session_id"] == sid

    def test_an_unknown_session_is_rejected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)

        assert _post(app, self._body("no-such-session")).status_code == 404

    def test_sizing_fields_reach_the_task(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-sizing")

        response = _post(app, self._body(sid, keep_recent=9, keep_recent_tokens=30000, threshold_tokens=120000))

        task = state.tasks[response.json()["task_id"]]
        assert task.input["_compact_keep_recent"] == 9
        assert task.input["_compact_keep_recent_tokens"] == 30000
        assert task.input["_compact_threshold_tokens"] == 120000

    def test_a_second_request_for_the_same_child_is_deduped(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:compact-dedupe")
        child, _ = state.get_or_create_child_session(parent, "bootstrap.executor", "case", "accumulate")

        first = _post(app, self._body(child))
        second = _post(app, self._body(child))

        assert first.json()["task_id"]
        assert second.json()["task_id"] == ""

    def test_a_finished_compact_does_not_block_the_next_one(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:compact-again")
        child, _ = state.get_or_create_child_session(parent, "bootstrap.executor", "case", "accumulate")

        first_id = _post(app, self._body(child)).json()["task_id"]
        with state._lock:
            state.tasks[first_id].status = TaskStatus.completed

        assert _post(app, self._body(child)).json()["task_id"]

    def test_missing_instruction_is_rejected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-empty")

        response = _post(app, {"target_session_id": sid, "instruction": "   "})

        assert response.status_code == 400

    def test_an_open_breaker_stops_the_request(self, tmp_path: Path) -> None:
        """压缩模型持续不可用时 engine 每一步都会再请求一次，每次都要起一个 compactor task。

        熔断计数只能放在这一侧：engine 是多个独立 worker 进程，各自的计数器互相看不见。
        """
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-breaker")
        for _ in range(3):
            state.compact_breaker.record_failure(sid)

        response = _post(app, self._body(sid))

        assert response.status_code == 200, response.text
        assert response.json()["task_id"] == ""
        assert "breaker" in response.json()["reason"]

    def test_a_breaker_open_on_another_session_does_not_block_this_one(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        app = _app(state, tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-other")
        for _ in range(3):
            state.compact_breaker.record_failure("unrelated-session")

        assert _post(app, self._body(sid)).json()["task_id"]

    def test_a_successful_compact_closes_the_breaker(self, tmp_path: Path) -> None:
        # 失败计数不清零的话，一个偶发失败会在后续每次失败时越来越接近永久熔断。
        state = _make_state(tmp_path)
        sid = state.get_or_create_session(channel="test", conversation_key="test:compact-reopen")
        state.compact_breaker.record_failure(sid)
        state.compact_breaker.record_failure(sid)

        state.compact_breaker.record_success(sid)

        assert state.compact_breaker.failure_count(sid) == 0
