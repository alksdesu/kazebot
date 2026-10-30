"""supervisor 对外新增和收紧的接口：任务来源、会话分页、附件响应头、按任务回查身份。"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.conversation_store import ConversationStore, Message  # noqa: E402
from supervisor import admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, Task, TaskKind, TaskStatus  # noqa: E402

_TOKEN = "sup-contract-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_QQ_KEY = "qq_group:sup-contract"
_ADMIN = {"platform": "qq", "is_admin": True, "user_id": "u-admin"}


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", OrderedDict())


def _state(workspace: Path) -> SupervisorState:
    state = SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-sup-contract"),
        policy=PolicyEngine(workspace_root=workspace),
    )
    state._defer_post_work_enqueue = True
    return state


def _app(state: SupervisorState, workspace: Path) -> Any:
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=workspace / "config" / "models.yaml", workspace_root=workspace),
    )


def _get(app: Any, url: str, **kwargs: Any) -> httpx.Response:
    # 没显式给 headers 就带管理员令牌；验令牌的负路径用 headers={} 强制不带
    kwargs.setdefault("headers", _AUTH)

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(url, **kwargs)

    return asyncio.run(_run())


def _entry_with_child(state: SupervisorState, platform_auth: dict[str, Any]) -> tuple[str, Task, Task]:
    sid = state.get_or_create_session(channel="qq_group", conversation_key=_QQ_KEY)
    with state._lock:
        entry = state._create_task_locked(
            session_id=sid,
            session_generation=1,
            kind=TaskKind.node,
            node_id="qq.orchestrator",
            input_data={"task_context": {
                "conversation_key": _QQ_KEY, "channel": "qq_group", "platform_auth": dict(platform_auth),
            }},
            continuation={},
            source_inbound_seq=None,
        )
        entry.status = TaskStatus.completed
        state._route_dispatch_locked(entry, {
            "action": "dispatch", "target_node": "bootstrap.executor",
            "dispatch_input": {"instruction": "do it", "context_mode": "fresh"},
        })
    child = state.tasks[str(entry.waiting_for_task_id)]
    assert "task_context" not in child.input
    return sid, entry, child


class TestTaskOrigin:
    def test_a_sync_child_reports_the_identity_of_its_entry_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _sid, entry, child = _entry_with_child(state, _ADMIN)

        response = _get(_app(state, tmp_path), f"/v1/tasks/{child.task_id}/origin", headers=_AUTH)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "task_id": child.task_id,
            "origin_task_id": entry.task_id,
            "conversation_key": _QQ_KEY,
            "channel": "qq_group",
            "platform_auth": _ADMIN,
        }

    def test_an_async_dispatch_traces_through_its_origin_record(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, entry, _child = _entry_with_child(state, _ADMIN)
        with state._lock:
            dispatched = state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="draw.node",
                input_data={"_dispatch_origin": {"caller_task_id": entry.task_id, "parent_session_id": sid}},
                continuation={}, source_inbound_seq=None,
            )

        origin = state.task_origin(dispatched.task_id)

        assert origin is not None
        assert origin.origin_task_id == entry.task_id and origin.platform_auth == _ADMIN

    def test_it_needs_the_admin_token_and_a_real_task(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _sid, _entry, child = _entry_with_child(state, _ADMIN)
        app = _app(state, tmp_path)

        assert _get(app, f"/v1/tasks/{child.task_id}/origin", headers={}).status_code == 401
        assert _get(app, "/v1/tasks/no-such-task/origin", headers=_AUTH).status_code == 404


class TestOpsRequestIdentity:
    def test_a_forged_admin_claim_is_ignored_when_the_task_says_otherwise(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, entry, _child = _entry_with_child(state, {"platform": "qq", "is_admin": False})

        out = state.request_operation(
            session_id=sid, op="write_file",
            parameters={"path": "notes.txt", "platform_auth": {"platform": "qq", "is_admin": True}},
            task_id=entry.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_a_sync_child_inherits_the_admin_identity_of_its_entry(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid, _entry, child = _entry_with_child(state, _ADMIN)

        out = state.request_operation(
            session_id=sid, op="execute_command", parameters={"command": "echo hi", "platform_auth": {}},
            task_id=child.task_id,
        )

        assert out.safety_level == SafetyLevel.auto
        assert out.reason.startswith("auto-approved: qq admin task")


class TestSessionPaging:
    def test_sessions_can_be_paged_with_an_offset(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        ids = []
        for index in range(3):
            sid = state.get_or_create_session(channel="web", conversation_key=f"web:page-{index}")
            state.sessions[sid].updated_at = base + timedelta(minutes=index)
            ids.append(sid)
        app = _app(state, tmp_path)

        page = _get(app, "/v1/sessions", params={"channel": "web", "limit": 1, "offset": 1})
        everything = _get(app, "/v1/sessions", params={"channel": "web"})

        assert [row["session_id"] for row in page.json()] == [ids[1]]
        assert [row["session_id"] for row in everything.json()] == [ids[2], ids[1], ids[0]]
        assert _get(app, "/v1/sessions", params={"offset": -1}).status_code == 422

    def test_history_records_carry_cursors_and_page_backwards(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="web", conversation_key="web:history")
        ConversationStore(tmp_path / "data" / "conversations").append_batch(sid, [
            Message(id=f"m{index}", role="user", content=f"line {index}", source_task_id="t")
            for index in range(5)
        ])
        app = _app(state, tmp_path)

        latest = _get(app, f"/v1/sessions/{sid}/history", params={"limit": 2}).json()
        older = _get(app, f"/v1/sessions/{sid}/history", params={"limit": 2, "before": latest[0]["cursor"]}).json()

        assert [(row["cursor"], row["id"]) for row in latest] == [(3, "m3"), (4, "m4")]
        assert [(row["cursor"], row["id"]) for row in older] == [(1, "m1"), (2, "m2")]


def _write_rows(workspace: Path, session_id: str, rows: list[dict[str, Any]]) -> None:
    path = workspace / "data" / "conversations" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"))


# 手改或从别处导入的存档：from_dict 不核类型，原样转出去前端整份历史都会读不出来
_HAND_WRITTEN = [
    {"id": 7, "role": "user", "content": None, "created_at": 5},
    {"id": "a", "role": "assistant", "content": {"type": "text", "text": "x"}, "tool_calls": {"name": "read_file"}},
    {"id": "b", "role": "assistant", "content": ["hi", 3, None, {"type": "text", "text": "y"}], "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.md"})}},
        "junk",
        {"id": "c2", "arguments": {}},
    ]},
    {"role": None, "content": "z", "meta": {"note": "kept"}},
]


class TestHandWrittenHistoryRows:
    def test_history_reads_each_row_the_way_the_model_reads_it(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="web", conversation_key="web:hand-written")
        _write_rows(tmp_path, sid, _HAND_WRITTEN)

        response = _get(_app(state, tmp_path), f"/v1/sessions/{sid}/history")

        assert response.status_code == 200, response.text
        rows = response.json()
        assert [(row["id"], row["role"], row["content"]) for row in rows] == [
            ("7", "user", []),
            ("a", "assistant", [{"type": "text", "text": "x"}]),
            ("b", "assistant", [{"type": "text", "text": "hi"}, {"type": "text", "text": "3"}, {"type": "text", "text": "y"}]),
            ("", "", "z"),
        ]
        assert rows[0]["created_at"] == "5"
        # chat/completions 形状的调用照样认出名字和参数，不是对象的、没名字的不给
        assert rows[2]["tool_calls"] == [{"id": "c1", "name": "read_file", "arguments": {"path": "a.md"}}]
        assert all("tool_calls" not in row for row in (rows[0], rows[1], rows[3]))

    def test_a_branch_merge_compares_and_sorts_rows_written_by_hand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="web", conversation_key="web:hand-branch")
        _write_rows(tmp_path, sid, [{"id": ["x"], "role": "user", "content": "parent", "created_at": 2}])
        _write_rows(tmp_path, "branch_hand", [{"id": "y", "role": "assistant", "content": "branch", "created_at": "1"}])
        monkeypatch.setattr(state, "_entry_branch_ids_for_parent_locked", lambda parent: {"branch_hand"})

        response = _get(_app(state, tmp_path), f"/v1/sessions/{sid}/history")

        assert response.status_code == 200, response.text
        assert [row["content"] for row in response.json()] == ["branch", "parent"]

    def test_task_filter_matches_the_id_the_row_is_shown_with(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        sid = state.get_or_create_session(channel="web", conversation_key="web:hand-task")
        _write_rows(tmp_path, sid, [{"id": "n", "role": "user", "content": "numbered", "source_task_id": 42}])

        response = _get(_app(state, tmp_path), f"/v1/sessions/{sid}/history", params={"task_id": "42"})

        assert [(row["source_task_id"], row["content"]) for row in response.json()] == [("42", "numbered")]

    def test_the_console_preview_reads_role_and_content_the_same_way(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_rows(tmp_path, "hand-preview", _HAND_WRITTEN)

        response = _get(_app(state, tmp_path), "/v1/admin/conversations/hand-preview/messages")

        assert response.status_code == 200, response.text
        messages = response.json()["messages"]
        assert [(row["role"], row["content"]) for row in messages] == [
            ("user", []),
            ("assistant", [{"type": "text", "text": "x"}]),
            ("assistant", [{"type": "text", "text": "hi"}, {"type": "text", "text": "3"}, {"type": "text", "text": "y"}]),
            ("", "z"),
        ]
        # 预览以外的字段照存档原样带着
        assert messages[1]["tool_calls"] == {"name": "read_file"}
        assert messages[3]["meta"] == {"note": "kept"}


class TestAttachmentHeaders:
    def _write(self, workspace: Path, name: str, data: bytes) -> str:
        path = workspace / "data" / "attachments" / "case" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"data/attachments/case/{name}"

    @pytest.mark.parametrize("name,inline", [
        ("pic.png", True),
        ("page.html", False),
        ("drawing.svg", False),
        ("notes.txt", False),
    ])
    def test_every_file_is_sandboxed_and_only_images_render_inline(
        self, tmp_path: Path, name: str, inline: bool,
    ) -> None:
        state = _state(tmp_path)
        rel = self._write(tmp_path, name, b"<html><script>alert(1)</script></html>")

        response = _get(_app(state, tmp_path), f"/v1/files/{rel}", params={"token": _TOKEN})

        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "sandbox"
        disposition = response.headers.get("content-disposition", "")
        assert disposition.startswith("attachment") is (not inline)

    def test_refusals_carry_the_same_headers(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        app = _app(state, tmp_path)

        missing = _get(app, "/v1/files/data/attachments/case/none.png", params={"token": _TOKEN})
        unauthorized = _get(app, "/v1/files/data/attachments/case/none.png", headers={})

        for response, status in ((missing, 404), (unauthorized, 401)):
            assert response.status_code == status
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-security-policy"] == "sandbox"
